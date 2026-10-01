import hashlib
import json
import logging
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.models.task import (
    AcceptanceResult,
    AgentName,
    AgentResult,
    CompletionGateResult,
    DependencyEvidence,
    ExecutionStage,
    FrontendGeneratedFile,
    FrontendImplementationPlan,
    FrontendImplementation,
    FrontendRepair,
    RepairAttemptEvidence,
    RunEvidence,
    Task,
    TaskStatus,
)
from app.services.failure_classifier import FailureClassifier
from app.services.llm import (
    GenerationRecoveryMetadata,
    LLMGenerationValidationError,
    LLMImplementationValidationError,
    LLMService,
)
from app.tools.api_contract import BackendAPIContractCollector
from app.tools.frontend_dependency_manager import FrontendDependencyManager
from app.tools.frontend_test_runner import FrontendTestRunner
from app.tools.workspace import WorkspacePathError, WorkspaceTool

logger = logging.getLogger(__name__)


class FrontendDeveloperAgent:
    name = AgentName.FRONTEND

    def __init__(
        self,
        llm_service: LLMService | None = None,
        workspace: WorkspaceTool | None = None,
        test_runner: FrontendTestRunner | None = None,
        dependency_manager: FrontendDependencyManager | None = None,
        api_contract_collector: BackendAPIContractCollector | None = None,
        max_attempts: int = 3,
        max_format_corrections: int = 2,
    ) -> None:
        self.llm_service = llm_service
        self.workspace = workspace
        self.test_runner = test_runner
        self.dependency_manager = dependency_manager
        self.api_contract_collector = api_contract_collector
        self.max_repair_attempts = min(max(max_attempts, 1), 3)
        self.max_format_corrections = min(max(max_format_corrections, 0), 2)
        self.failure_classifier = FailureClassifier()

    async def process(
        self,
        task: Task,
        completed_dependencies: list[dict[str, Any]] | None = None,
        completed_regression_test_paths: list[str] | None = None,
    ) -> AgentResult:
        logger.info("FRONTEND AGENT: processing %s", task.id)
        if not all(
            (
                self.llm_service,
                self.workspace,
                self.test_runner,
                self.dependency_manager,
                self.api_contract_collector,
            )
        ):
            raise RuntimeError("FrontendDeveloperAgent is not fully configured")
        baseline = self._baseline()
        criteria = self._acceptance_criteria(task)
        dependencies_context = completed_dependencies or []
        regression_paths = list(dict.fromkeys(completed_regression_test_paths or []))
        api_contract = self.api_contract_collector.collect()
        task_paths: list[str] = []
        task_tests = None
        regression_tests = None
        dependency_evidence = None
        repair_history: list[RepairAttemptEvidence] = []
        generation_metadata: GenerationRecoveryMetadata | None = None
        format_corrections = 0
        failure: RunEvidence | None = None
        touched: set[str] = set()

        snapshot = self._snapshot()
        logger.info("FRONTEND AGENT: stage=PLAN_VALIDATION task=%s", task.id)
        try:
            system, user = self._plan_prompt(task, criteria, snapshot, dependencies_context)
            response = await self.llm_service.generate_json_with_validation(
                system,
                user,
                lambda raw: self._validate_plan(raw, snapshot, regression_paths),
                lambda error, raw: self._bounded_correction(system, user, error, "frontend_plan_correction"),
            )
            plan = FrontendImplementationPlan.model_validate(response)
            generation_metadata = self._combine_generation_metadata(
                generation_metadata, self.llm_service.last_generation_metadata
            )
            task_paths = list(plan.task_test_paths)
            for path in plan.files_to_create + plan.files_to_modify:
                logger.info("FRONTEND AGENT: stage=FILE_GENERATION path=%s", path)
                file_snapshot = self._snapshot()
                file_system, file_user = self._file_prompt(
                    task, criteria, path, file_snapshot, plan, api_contract
                )
                file_response = await self.llm_service.generate_json_with_validation(
                    file_system,
                    file_user,
                    lambda raw, target=path: self._validate_file(raw, target, file_snapshot),
                    lambda error, raw: self._bounded_correction(
                        file_system, file_user, error, "frontend_file_correction"
                    ),
                )
                generated = FrontendGeneratedFile.model_validate(file_response)
                generation_metadata = self._combine_generation_metadata(
                    generation_metadata, self.llm_service.last_generation_metadata
                )
                if file_snapshot.get(path) != generated.content:
                    self.workspace.write_file(path, generated.content)
                    touched.add(path)
        except (LLMGenerationValidationError, ValidationError, ValueError, WorkspacePathError) as exc:
            logger.warning("FRONTEND AGENT: generation failed: %s", exc)
            generation_metadata = generation_metadata or self.llm_service.last_generation_metadata
            return self._result(
                task, TaskStatus.FAILED, criteria, baseline, touched, task_paths,
                task_tests, regression_tests, dependency_evidence, regression_paths,
                0, format_corrections, generation_metadata, repair_history,
                ExecutionStage.IMPLEMENTATION_VALIDATION,
            )

        if not any(path in touched for path in plan.files_to_create + plan.files_to_modify if path not in task_paths):
            logger.warning("FRONTEND AGENT: generation produced no task-specific source changes")
            return self._result(
                task, TaskStatus.FAILED, criteria, baseline, touched, task_paths,
                task_tests, regression_tests, dependency_evidence, regression_paths,
                0, format_corrections, generation_metadata, repair_history,
                ExecutionStage.IMPLEMENTATION_VALIDATION,
            )
        dependencies = list(plan.dependencies)

        dependency_evidence = await self.dependency_manager.prepare(dependencies)
        if not dependency_evidence.success:
            return self._result(
                task, TaskStatus.FAILED, criteria, baseline, touched, task_paths,
                task_tests, regression_tests, dependency_evidence, regression_paths,
                0, format_corrections, generation_metadata, repair_history,
                ExecutionStage.DEPENDENCY_PREPARATION,
            )

        task_tests = await self.test_runner.run(task_paths)
        repair_calls = 0
        while not task_tests.passed and repair_calls < self.max_repair_attempts:
            failure = task_tests
            classification = self.failure_classifier.classify(failure)
            repair_calls += 1
            logger.info("FRONTEND AGENT: code repair attempt %d", repair_calls)
            repair_snapshot = self._snapshot()
            repair_system, repair_user = self._repair_prompt(
                task, criteria, repair_snapshot, task_paths, failure,
                classification, repair_history, dependency_evidence, repair_calls, api_contract,
            )
            try:
                raw = await self.llm_service.generate_json(repair_system, repair_user)
                repair, corrections = await self._parse_response(
                    raw, FrontendRepair, task
                )
                format_corrections += corrections
                repair_impl = FrontendImplementation(
                    summary=repair.repair_strategy,
                    files=repair.files,
                    dependencies=[],
                    task_test_paths=repair.task_test_paths,
                )
                self._validate_repair(repair, repair_impl, repair_snapshot)
                repair_changes = self._changed(repair.files, repair_snapshot)
                self._write(repair.files, repair_snapshot)
                touched.update(repair_changes)
                repair_history.append(
                    RepairAttemptEvidence(
                        repair_attempt=repair_calls,
                        failure_category=repair.failure_category,
                        root_cause=repair.root_cause,
                        repair_strategy=repair.repair_strategy,
                        files_modified=repair_changes,
                        test_result_before=failure.summary,
                        failure_signature_before=self._signature(failure),
                    )
                )
            except (ValidationError, ValueError, WorkspacePathError) as exc:
                logger.warning(
                    "FRONTEND AGENT: repair %d rejected: %s", repair_calls, exc
                )
                continue
            task_tests = await self.test_runner.run(task_paths)
            repair_history[-1].test_result_after = task_tests.summary
            repair_history[-1].failure_signature_after = self._signature(task_tests)

        repairs_used = len(repair_history)
        if not task_tests or not task_tests.passed:
            return self._result(
                task, TaskStatus.FAILED, criteria, baseline, touched, task_paths,
                task_tests, regression_tests, dependency_evidence, regression_paths,
                repair_calls,
                format_corrections, generation_metadata, repair_history,
                ExecutionStage.CODE_REPAIR,
            )

        if regression_paths:
            regression_tests = await self.test_runner.run(regression_paths)
        else:
            regression_tests = RunEvidence(
                passed=True,
                exit_code=0,
                summary="No completed frontend regression tests",
            )
        if not regression_tests.passed:
            return self._result(
                task, TaskStatus.FAILED, criteria, baseline, touched, task_paths,
                task_tests, regression_tests, dependency_evidence, regression_paths,
                repairs_used, format_corrections, generation_metadata, repair_history,
                ExecutionStage.TEST_EXECUTION,
            )
        return self._result(
            task, TaskStatus.REVIEW, criteria, baseline, touched, task_paths,
            task_tests, regression_tests, dependency_evidence, regression_paths,
            repairs_used, format_corrections, generation_metadata, repair_history,
            ExecutionStage.MANAGER_REVIEW,
        )

    @staticmethod
    def _compact_task(task):
        return {"id": task.id, "title": task.title[:160], "description": task.description[:1200]}

    @staticmethod
    def _combine_generation_metadata(previous, current):
        if current is None:
            return previous
        if previous is None:
            return current
        return GenerationRecoveryMetadata(
            provider_used=current.provider_used,
            model_used=current.model_used,
            generation_attempts=previous.generation_attempts + current.generation_attempts,
            generation_correction_attempts=(
                previous.generation_correction_attempts + current.generation_correction_attempts
            ),
            provider_fallback_count=previous.provider_fallback_count + current.provider_fallback_count,
            escalation_reasons=previous.escalation_reasons + current.escalation_reasons,
        )


    def _plan_prompt(self, task, criteria, snapshot, dependencies):
        system = (
            "Plan one React/Vite frontend task. Return only JSON matching "
            + json.dumps(FrontendImplementationPlan.model_json_schema())
            + ". No source code or file contents. All paths must start with frontend/. "
            "Create/modify task-specific source and tests. Use only approved npm packages."
        )
        user = json.dumps({
            "operation": "frontend_plan",
            "task": self._compact_task(task),
            "acceptance_criteria": [criterion[:900] for criterion in criteria[:3]],
            "completed_dependencies": [
                {"task_id": item.get("task_id"), "title": str(item.get("title", ""))[:100]}
                for item in dependencies[:6]
            ],
            "existing_frontend_paths": sorted(snapshot)[:40],
            "approved_dependencies": sorted(FrontendDependencyManager.approved_packages),
        })
        return system, user

    def _validate_plan(self, raw, snapshot, regression_paths):
        try:
            plan = FrontendImplementationPlan.model_validate(raw)
            targets = plan.files_to_create + plan.files_to_modify
            if not targets or len(targets) > 16 or len(set(targets)) != len(targets):
                raise ValueError("Frontend plan requires 1-16 unique target files")
            if len(plan.dependencies) > 20 or len(plan.task_test_paths) > 8 or len(plan.summary) > 300:
                raise ValueError("Frontend plan exceeds compact plan budget")
            FrontendDependencyManager._validate_dependencies(plan.dependencies)
            for path in targets:
                self._validate_planned_path(path, snapshot)
            if any(path in snapshot for path in plan.files_to_create):
                raise ValueError("Planned create target already exists; use files_to_modify")
            if any(path not in snapshot for path in plan.files_to_modify):
                raise ValueError("Planned modify target does not exist; use files_to_create")
            if not any(path not in plan.task_test_paths for path in targets):
                raise ValueError("Frontend plan requires a task-specific source file")
            if not plan.task_test_paths:
                raise ValueError("Frontend plan requires task-specific tests")
            for path in plan.task_test_paths:
                FrontendTestRunner._validated_targets([path])
                if path in regression_paths or path not in targets and path not in snapshot:
                    raise ValueError("Task test must be planned or already exist and cannot be a completed regression")
        except (ValidationError, ValueError, WorkspacePathError) as exc:
            raise LLMImplementationValidationError(str(exc)) from exc

    def _validate_planned_path(self, path, snapshot):
        self._validate_path(path)
        if len(path.encode("utf-8")) > 160:
            raise ValueError("Frontend path exceeds context budget")
        if path in {"frontend/package.json", "frontend/.agent-vitest.config.mjs", "frontend/.agent-vitest.setup.js"}:
            raise ValueError("Frontend dependency manager owns this file")
        if not PurePosixPath(path).suffix or len(PurePosixPath(path).parts) < 2:
            raise ValueError("Frontend plan target must be a file")
        if PurePosixPath(path).suffix in {".ts", ".tsx"} and not any(
            PurePosixPath(existing).suffix in {".ts", ".tsx"} for existing in snapshot
        ):
            raise ValueError("TypeScript requires an existing TypeScript frontend")

    def _file_prompt(self, task, criteria, path, snapshot, plan, api_contract):
        is_test = path in plan.task_test_paths
        system = (
            "Generate exactly one React/Vite frontend file. Return only JSON with "
            "path and content. The path must be exactly the requested frontend/ path. "
            "Use React Testing Library/Vitest for tests. Do not return commands or other files."
        )
        target_name = PurePosixPath(path).stem.lower().replace(".test", "")
        related = sorted(
            (
                candidate for candidate in snapshot
                if candidate != path and candidate.startswith("frontend/src/")
                and (
                    target_name in PurePosixPath(candidate).stem.lower()
                    or candidate in plan.files_to_create + plan.files_to_modify
                    and ".test." not in candidate and ".spec." not in candidate
                )
            ),
            key=lambda candidate: (
                target_name not in candidate.lower(),
                PurePosixPath(candidate).parent != PurePosixPath(path).parent,
                candidate,
            ),
        )[:2]
        related_contents = {candidate: snapshot[candidate][:1000] for candidate in related}
        contract = {
            "routes": [
                {key: str(route.get(key, ""))[:100] for key in ("method", "path", "handler")}
                for route in api_contract.get("routes", [])[:6]
            ],
            "schemas": {
                str(name)[:80]: [str(field)[:80] for field in fields[:12]]
                for name, fields in list(api_contract.get("schemas", {}).items())[:6]
            },
        } if any(token in path.lower() for token in ("auth", "login", "register", "student", "dashboard")) else {}
        user = json.dumps({
            "operation": "frontend_file",
            "task": self._compact_task(task),
            "acceptance_criteria": [criterion[:900] for criterion in criteria[:2]],
            "target_path": path,
            "target_existing_content": snapshot.get(path, "")[:2500],
            "related_frontend_contents": related_contents,
            "planned_paths": (plan.files_to_create + plan.files_to_modify)[:16],
            "task_test_paths": plan.task_test_paths[:8],
            "backend_api_contract": contract,
            "required_interfaces": "Use imports/exports compatible with listed planned paths and related files.",
            "instruction": "Return one complete file. Preserve valid existing tests; implement only the current task." if is_test else "Return one complete task-specific source file.",
        })
        return system, user

    def _validate_file(self, raw, path, snapshot):
        try:
            generated = FrontendGeneratedFile.model_validate(raw)
            if generated.path != path:
                raise ValueError(f"File response path must exactly match {path}")
            self._validate_path(generated.path)
            if not generated.content.strip():
                raise ValueError("Generated frontend file is empty")
            if path not in snapshot or snapshot[path] != generated.content:
                return
            if not (".test." in path or ".spec." in path):
                raise ValueError("Generated source is unchanged")
        except (ValidationError, ValueError, WorkspacePathError) as exc:
            raise LLMImplementationValidationError(str(exc)) from exc

    @staticmethod
    def _bounded_correction(system, user, error, operation):
        request = json.loads(user)
        request["operation"] = operation
        request["validation_error"] = str(error)[:500]
        request["instruction"] = "Correct only this plan/file response. Return the required JSON shape and exact paths."
        return system, json.dumps(request)

    async def _parse_response(self, raw, model, task):
        response = raw
        for correction in range(self.max_format_corrections + 1):
            try:
                return model.model_validate(response), correction
            except ValidationError as exc:
                if correction >= self.max_format_corrections:
                    raise
                response = await self.llm_service.generate_json(
                    "Correct only the frontend JSON response format.",
                    json.dumps({
                        "operation": "frontend_format_correction",
                        "task_id": task.id,
                        "required_schema": model.model_json_schema(),
                        "validation_errors": [
                            {"loc": item["loc"], "msg": item["msg"][:300]}
                            for item in exc.errors(include_url=False)[:5]
                        ],
                        "invalid_response_keys": sorted(response) if isinstance(response, dict) else [],
                    }),
                )
        raise ValueError("Unreachable frontend format correction state")

    def _validate_implementation(self, implementation, snapshot, initial, regression_paths=()):
        FrontendDependencyManager._validate_dependencies(implementation.dependencies)
        paths = {item.path for item in implementation.files}
        existing_typescript = any(
            PurePosixPath(path).suffix in {".ts", ".tsx"} for path in snapshot
        )
        for item in implementation.files:
            self._validate_path(item.path)
            if item.path in {
                "frontend/package.json",
                "frontend/.agent-vitest.config.mjs",
                "frontend/.agent-vitest.setup.js",
            }:
                raise ValueError("Frontend dependency manager owns this file")
            if not existing_typescript and PurePosixPath(item.path).suffix in {".ts", ".tsx"}:
                raise ValueError("TypeScript requires an existing TypeScript frontend")
        for test in implementation.task_test_paths:
            self._validate_test_path(test)
            if test not in paths and test not in snapshot:
                raise ValueError("Frontend task test must be generated or already exist")
        changed = self._changed(implementation.files, snapshot)
        source = [path for path in changed if path not in implementation.task_test_paths]
        changed_tests = set(changed) & set(implementation.task_test_paths)
        if not source:
            raise ValueError("Frontend implementation requires source changes")
        if initial and (
            not implementation.task_test_paths
            or any(path in regression_paths for path in implementation.task_test_paths)
            or any(path not in changed_tests and path not in snapshot for path in implementation.task_test_paths)
        ):
            raise ValueError("Frontend generation requires task-specific tests, not completed-task regression tests")

    def _repair_prompt(self, task, criteria, snapshot, paths, failure, classification, history, dependency, attempt, api_contract):
        tests = {path: snapshot[path][:1800] for path in paths[:2] if path in snapshot}
        stems = {PurePosixPath(path).stem.lower().replace(".test", "") for path in paths}
        sources = {
            path: content[:1800] for path, content in snapshot.items()
            if path not in paths and path.startswith("frontend/src/")
            and not any(token in path for token in (".test.", ".spec."))
            and any(stem in path.lower() for stem in stems)
        }
        sources = dict(list(sorted(sources.items()))[:3])
        return (
            "Diagnose and repair a React/Vite failure. Return JSON matching "
            + json.dumps(FrontendRepair.model_json_schema()),
            json.dumps({
                "operation": "frontend_repair",
                "attempt_number": attempt,
                "original_task": self._compact_task(task),
                "acceptance_criteria": criteria[:3],
                "failing_test_paths": paths[:4],
                "pytest_stdout": failure.stdout[-1600:],
                "pytest_stderr": failure.stderr[-2400:],
                "failure_classification": classification.model_dump(mode="json"),
                "relevant_frontend_contents": sources,
                "relevant_test_contents": tests,
                "previous_repair_history": [
                    {"root_cause": item.root_cause[:200], "repair_strategy": item.repair_strategy[:200]}
                    for item in history[-2:]
                ],
                "dependency_status": {"success": dependency.success, "summary": dependency.summary[:300]} if dependency else None,
                "backend_api_contract": {"routes": api_contract.get("routes", [])[:6]},
                "instruction": "Patch the failing existing source. Do not weaken valid tests.",
            }),
        )

    def _validate_repair(self, repair, implementation, snapshot):
        declared = set(repair.files_to_modify)
        generated = {item.path for item in repair.files}
        if not generated.issubset(declared):
            raise ValueError("Frontend repair contains undeclared files")
        self._validate_implementation(implementation, snapshot, initial=False)
        changes = self._changed(repair.files, snapshot)
        tests = [path for path in changes if ".test." in path or ".spec." in path]
        if tests and not repair.test_change_justification:
            raise ValueError("Frontend test changes require explicit justification")

    @staticmethod
    def _validate_path(path):
        parsed = PurePosixPath(path.replace("\\", "/"))
        if parsed.is_absolute() or ".." in parsed.parts or not parsed.parts or parsed.parts[0] != "frontend":
            raise WorkspacePathError("Frontend agent may write only under frontend/")

    @classmethod
    def _validate_test_path(cls, path):
        cls._validate_path(path)
        if not any(token in path for token in (".test.", ".spec.")):
            raise ValueError("Frontend task tests must be *.test.* or *.spec.* files")

    def _snapshot(self):
        result = {}
        for path in self.workspace.list_files():
            if path.startswith("frontend/") and "node_modules/" not in path:
                try:
                    result[path] = self.workspace.read_file(path)
                except (OSError, UnicodeError):
                    pass
        return result

    def _baseline(self):
        return {
            path: hashlib.sha256(content.encode()).hexdigest()
            for path, content in self._snapshot().items()
        }

    @staticmethod
    def _changed(files, snapshot):
        return [item.path for item in files if snapshot.get(item.path) != item.content]

    def _write(self, files, snapshot):
        for item in files:
            if snapshot.get(item.path) != item.content:
                self.workspace.write_file(item.path, item.content)

    def _classify(self, baseline, touched):
        current = self._baseline()
        created = sorted(path for path in touched if path not in baseline and path in current)
        modified = sorted(path for path in touched if path in baseline and current.get(path) != baseline[path])
        return created, modified

    @staticmethod
    def _acceptance_criteria(task):
        return [
            f"Implement the current frontend task: {task.title}.",
            f"Satisfy the requested frontend behavior: {task.description}",
            "Add task-specific frontend test coverage.",
            "Pass current frontend tests and completed frontend regressions.",
        ]

    @staticmethod
    def _signature(evidence):
        if evidence.passed:
            return "PASS"
        text = f"{evidence.stdout}\n{evidence.stderr}".strip()
        return hashlib.sha256(text.encode()).hexdigest() if text else evidence.summary

    def _result(self, task, status, criteria, baseline, touched, paths, task_tests, regressions, dependencies, regression_paths, repairs, format_corrections, generation, history, stage):
        created, modified = self._classify(baseline, touched)
        changed = created + modified
        source = [path for path in changed if path not in paths]
        tests_ok = bool(paths and task_tests and task_tests.passed)
        regressions_ok = bool(regressions and regressions.passed)
        deps_ok = bool(dependencies and dependencies.success)
        acceptance_results = [
            AcceptanceResult(criterion=criteria[0], satisfied=bool(source), evidence=source),
            AcceptanceResult(criterion=criteria[1], satisfied=tests_ok, evidence=[task_tests.summary] if task_tests else []),
            AcceptanceResult(criterion=criteria[2], satisfied=tests_ok, evidence=paths),
            AcceptanceResult(criterion=criteria[3], satisfied=tests_ok and regressions_ok, evidence=[regressions.summary] if regressions else []),
        ]
        acceptance_ok = all(item.satisfied for item in acceptance_results)
        gates = {
            "implementation_evidence": bool(source),
            "task_specific_changes": bool(changed),
            "task_tests": tests_ok,
            "regression_tests": regressions_ok,
            "dependencies": deps_ok,
            "acceptance_criteria": acceptance_ok,
            "no_unresolved_failure": status == TaskStatus.REVIEW,
        }
        reasons = {
            "implementation_evidence": "missing_implementation_evidence",
            "task_specific_changes": "missing_task_specific_changes",
            "task_tests": "task_tests_missing_or_failed",
            "regression_tests": "completed_task_regressions_failed",
            "dependencies": "dependency_preparation_failed",
            "acceptance_criteria": "required_acceptance_criterion_unsatisfied",
            "no_unresolved_failure": "unresolved_execution_failure",
        }
        reason = (
            "dependency_preparation_failed"
            if stage == ExecutionStage.DEPENDENCY_PREPARATION and not deps_ok
            else next((reasons[key] for key, value in gates.items() if not value), None)
        )
        metadata = generation
        return AgentResult(
            task_id=task.id,
            agent=self.name,
            status=status,
            summary="Frontend implementation ready for Manager review." if status == TaskStatus.REVIEW else f"Frontend implementation failed: {reason}",
            proposed_implementation={"scope": "frontend", "backend_api_contract_used": True, "dangerous_actions_performed": False},
            safety_notes=["Writes confined to workspace/frontend.", "Only allowlisted npm packages and fixed Vitest execution are permitted.", "Backend context is read-only."],
            files_changed=changed,
            files_created=created,
            files_modified=modified,
            acceptance_criteria=criteria,
            acceptance_results=acceptance_results,
            acceptance_criteria_satisfied=acceptance_ok,
            completion_gate=CompletionGateResult(**gates, passed=all(gates.values()), rejection_reason=reason),
            rejection_reason=reason,
            task_test_paths=paths,
            regression_test_paths=regression_paths,
            task_tests=task_tests,
            tests=regressions,
            dependencies=dependencies,
            generation_attempts=metadata.generation_attempts if metadata else 1,
            format_correction_attempts=format_corrections,
            generation_correction_attempts=metadata.generation_correction_attempts if metadata else 0,
            provider_fallback_count=metadata.provider_fallback_count if metadata else 0,
            code_repair_attempts=repairs,
            attempts=repairs,
            execution_stage=stage,
            failure_stage=stage if status == TaskStatus.FAILED else None,
            repair_history=history,
        )
