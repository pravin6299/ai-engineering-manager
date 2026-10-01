import ast
import hashlib
import json
import logging
import re
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.models.task import (
    AgentName,
    AgentResult,
    AcceptanceResult,
    BackendImplementation,
    BackendRepair,
    CompletionGateResult,
    DependencyEvidence,
    ExecutionStage,
    FailureCategory,
    RepairAttemptEvidence,
    RunEvidence,
    Task,
    TaskStatus,
)
from app.services.llm import (
    GenerationRecoveryMetadata,
    LLMGenerationValidationError,
    LLMImplementationValidationError,
    LLMService,
)
from app.services.failure_classifier import FailureClassifier
from app.tools.dependency_manager import DependencyManager
from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspacePathError, WorkspaceTool

logger = logging.getLogger(__name__)


class StructuredOutputCorrectionError(ValueError):
    def __init__(self, message: str, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


class BackendDeveloperAgent:
    name = AgentName.BACKEND

    def __init__(
        self,
        llm_service: LLMService,
        workspace: WorkspaceTool,
        test_runner: BackendTestRunner,
        dependency_manager: DependencyManager,
        max_attempts: int = 3,
        max_format_corrections: int = 2,
    ) -> None:
        self.llm_service = llm_service
        self.workspace = workspace
        self.test_runner = test_runner
        self.dependency_manager = dependency_manager
        self.max_repair_attempts = min(max(max_attempts, 1), 3)
        self.max_format_corrections = min(max(max_format_corrections, 0), 2)
        self.failure_classifier = FailureClassifier()

    async def process(
        self,
        task: Task,
        completed_dependencies: list[dict[str, Any]] | None = None,
        completed_regression_test_paths: list[str] | None = None,
    ) -> AgentResult:
        logger.info("BACKEND AGENT: processing %s", task.id)
        logger.info("BACKEND AGENT: inspecting workspace")
        baseline = self._change_baseline()
        acceptance_criteria = self._derive_acceptance_criteria(task)
        dependency_context = completed_dependencies or []
        regression_test_paths = list(
            dict.fromkeys(completed_regression_test_paths or [])
        )
        task_tests: RunEvidence | None = None
        regression_tests: RunEvidence | None = None
        dependency_evidence: DependencyEvidence | None = None
        failure_context: dict[str, Any] = {}
        task_test_paths: list[str] = []
        touched_paths: set[str] = set()
        attempted_changes: list[dict[str, Any]] = []
        repair_history: list[RepairAttemptEvidence] = []
        format_correction_attempts = 0
        generation_metadata: GenerationRecoveryMetadata | None = None

        for repair_attempt in range(0, self.max_repair_attempts + 1):
            workspace_file_list = self._workspace_file_list()
            is_repair = repair_attempt > 0
            current_snapshot = (
                self._repair_snapshot(
                    workspace_file_list,
                    task_test_paths,
                    failure_context,
                    attempted_changes,
                )
                if is_repair
                else self._workspace_snapshot()
            )
            phase = (
                f"repair_attempt_{repair_attempt}"
                if is_repair
                else "initial_generation"
            )
            if is_repair:
                logger.info("BACKEND AGENT: stage=%s", ExecutionStage.CODE_REPAIR.value)
                logger.info("BACKEND AGENT: code repair attempt %d", repair_attempt)
            else:
                logger.info("BACKEND AGENT: stage=%s", ExecutionStage.GENERATION.value)
                logger.info("BACKEND AGENT: initial generation")
            try:
                needs_initial_tests = not task_test_paths
                system_prompt = self._system_prompt(is_repair)
                user_prompt = self._user_prompt(
                        task=task,
                        workspace_snapshot=current_snapshot,
                        workspace_file_list=workspace_file_list,
                        acceptance_criteria=acceptance_criteria,
                        completed_dependencies=dependency_context,
                        task_test_paths=task_test_paths,
                        failure_context=failure_context,
                        dependency_evidence=dependency_evidence,
                        repair_attempt=repair_attempt,
                        attempted_changes=attempted_changes,
                    )
                if is_repair:
                    implementation, corrections, repair_analysis = (
                        await self._generate_implementation(
                            system_prompt,
                            user_prompt,
                            task,
                            is_repair=True,
                        )
                    )
                else:
                    implementation, corrections, repair_analysis = (
                        await self._generate_initial_implementation(
                            system_prompt,
                            user_prompt,
                            task,
                            current_snapshot,
                            workspace_file_list,
                        )
                    )
                    generation_metadata = (
                        self.llm_service.last_generation_metadata
                    )
                    if generation_metadata:
                        logger.info(
                            "BACKEND AGENT: initial generation using %s",
                            generation_metadata.provider_used,
                        )
                format_correction_attempts += corrections
                self._validate_implementation(
                    implementation=implementation,
                    existing_task_tests=task_test_paths,
                    require_generated_test=needs_initial_tests,
                    workspace_files=set(current_snapshot),
                )
                comparison_snapshot = dict(current_snapshot)
                for generated_file in implementation.files:
                    if (
                        generated_file.path not in comparison_snapshot
                        and generated_file.path in workspace_file_list
                    ):
                        comparison_snapshot[generated_file.path] = (
                            self.workspace.read_file(generated_file.path)
                        )
                attempt_changes = self._changed_paths(
                    implementation, comparison_snapshot
                )
                if not attempt_changes:
                    raise ValueError(
                        "Repair returned no meaningful file content changes"
                        if is_repair
                        else "Implementation returned no meaningful file content changes"
                    )
                changed_implementation = [
                    path
                    for path in attempt_changes
                    if not path.startswith("backend/tests/")
                ]
                if not changed_implementation and not is_repair:
                    raise ValueError(
                        "Initial implementation must create or modify implementation files"
                    )
                if repair_analysis:
                    self._validate_repair_analysis(
                        repair_analysis,
                        attempt_changes,
                        set(current_snapshot),
                        failure_context,
                    )

                supplied_tests = implementation.task_test_paths
                if needs_initial_tests:
                    changed_tests = set(supplied_tests) & set(attempt_changes)
                    if not supplied_tests or changed_tests != set(supplied_tests):
                        raise ValueError(
                            "A valid initial implementation must create a task-specific test"
                        )
                    task_test_paths = list(supplied_tests)
                elif supplied_tests:
                    task_test_paths = list(
                        dict.fromkeys(task_test_paths + supplied_tests)
                    )
                if not task_test_paths:
                    raise ValueError(
                        "A task-specific backend/tests/test_*.py path is required"
                    )

                self._write_changed_files(implementation, comparison_snapshot)
                touched_paths.update(attempt_changes)
                attempt_record = {
                    "phase": phase,
                    "changed_files": attempt_changes,
                    "implementation_files": changed_implementation,
                    "outcome": "written",
                }
                if repair_analysis:
                    details = self._failure_details(failure_context)
                    attempt_record.update(
                        {
                            "failure_category": repair_analysis.failure_category.value,
                            "root_cause": repair_analysis.root_cause,
                            "repair_strategy": repair_analysis.repair_strategy,
                            "failure_signature_before": details["failure_signature"],
                        }
                    )
                    repair_history.append(
                        RepairAttemptEvidence(
                            repair_attempt=repair_attempt,
                            failure_category=repair_analysis.failure_category,
                            root_cause=repair_analysis.root_cause,
                            repair_strategy=repair_analysis.repair_strategy,
                            files_modified=attempt_changes,
                            test_result_before=details["test_result"],
                            failure_signature_before=details["failure_signature"],
                        )
                    )
                attempted_changes.append(attempt_record)
            except LLMGenerationValidationError as exc:
                generation_metadata = self.llm_service.last_generation_metadata
                logger.warning(
                    "BACKEND AGENT: generation recovery exhausted: %s", exc
                )
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.FAILED,
                    0,
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                    regression_test_paths,
                    format_correction_attempts,
                    repair_history,
                    generation_metadata,
                )
            except StructuredOutputCorrectionError as exc:
                format_correction_attempts += exc.attempts
                logger.warning("BACKEND AGENT: structured output correction failed: %s", exc)
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.FAILED,
                    max(0, repair_attempt - (1 if is_repair else 0)),
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                    regression_test_paths,
                    format_correction_attempts,
                    repair_history,
                    generation_metadata,
                )
            except (WorkspacePathError, OSError, ValueError) as exc:
                attempted_changes.append(
                    {
                        "phase": phase,
                        "proposed_files": [
                            item.get("path", "")
                            for item in response.get("files", [])
                            if isinstance(item, dict)
                        ]
                        if "response" in locals() and isinstance(response, dict)
                        else [],
                        "outcome": "rejected",
                        "reason": str(exc),
                    }
                )
                if not is_repair:
                    failure_context = {
                        "message": f"Generation or validation failed: {exc}",
                        "pytest_stdout": "",
                        "pytest_stderr": "",
                    }
                else:
                    attempted_changes[-1]["repair_validation_error"] = str(exc)
                logger.warning("BACKEND AGENT: %s rejected: %s", phase, exc)
                continue

            logger.info(
                "BACKEND AGENT: stage=%s",
                ExecutionStage.DEPENDENCY_PREPARATION.value,
            )
            dependency_evidence = await self.dependency_manager.prepare()
            if not dependency_evidence.success:
                logger.warning(
                    "BACKEND AGENT: dependency preparation failed: %s",
                    dependency_evidence.summary,
                )
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.FAILED,
                    repair_attempt,
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                    regression_test_paths,
                    format_correction_attempts,
                    repair_history,
                    generation_metadata,
                )

            python_executable = dependency_evidence.python_executable
            logger.info("BACKEND AGENT: stage=%s", ExecutionStage.TEST_EXECUTION.value)
            task_tests = await self.test_runner.run(
                task_test_paths, python_executable=python_executable
            )
            if is_repair and repair_history:
                repair_history[-1].test_result_after = task_tests.summary
                repair_history[-1].failure_signature_after = (
                    self._failure_signature(task_tests)
                )
                attempted_changes[-1]["test_result_after"] = task_tests.summary
                attempted_changes[-1]["failure_signature_after"] = (
                    repair_history[-1].failure_signature_after
                )
                if (
                    repair_history[-1].failure_signature_before
                    == repair_history[-1].failure_signature_after
                ):
                    attempted_changes[-1]["strategy_outcome"] = (
                        "same_failure_signature_strategy_failed"
                    )
            if not task_tests.passed:
                classification = self.failure_classifier.classify(task_tests)
                if classification.category == FailureCategory.DEPENDENCY_FAILURE:
                    dependency_evidence = await self.dependency_manager.resolve_compatibility(
                        f"{task_tests.stdout}\n{task_tests.stderr}"
                    )
                    if dependency_evidence.success:
                        logger.info("TEST RUNNER: rerunning task tests")
                        python_executable = dependency_evidence.python_executable
                        task_tests = await self.test_runner.run(
                            task_test_paths, python_executable=python_executable
                        )
                        if task_tests.passed:
                            classification = None
                        else:
                            classification = self.failure_classifier.classify(task_tests)
                    if (
                        not dependency_evidence.success
                        or classification
                        and classification.category
                        in {
                            FailureCategory.DEPENDENCY_FAILURE,
                            FailureCategory.ENVIRONMENT_FAILURE,
                        }
                    ):
                        files_created, files_modified = self._classify_task_changes(
                            baseline, touched_paths
                        )
                        return self._result(
                            task,
                            TaskStatus.FAILED,
                            repair_attempt,
                            files_created,
                            files_modified,
                            acceptance_criteria,
                            task_test_paths,
                            task_tests,
                            regression_tests,
                            dependency_evidence,
                            regression_test_paths,
                            format_correction_attempts,
                            repair_history,
                            generation_metadata,
                        )
                elif classification.category == FailureCategory.ENVIRONMENT_FAILURE:
                    files_created, files_modified = self._classify_task_changes(
                        baseline, touched_paths
                    )
                    return self._result(
                        task,
                        TaskStatus.FAILED,
                        repair_attempt,
                        files_created,
                        files_modified,
                        acceptance_criteria,
                        task_test_paths,
                        task_tests,
                        regression_tests,
                        dependency_evidence,
                        regression_test_paths,
                        format_correction_attempts,
                        repair_history,
                        generation_metadata,
                    )
            if not task_tests.passed:
                failure_context = self._failure_context(
                    "Task-specific tests failed", task_tests
                )
                attempted_changes[-1]["outcome"] = "task_tests_failed"
                logger.info("BACKEND AGENT: analyzing test failure")
                continue

            if regression_test_paths:
                regression_tests = await self.test_runner.run(
                    regression_test_paths,
                    python_executable=python_executable,
                )
            else:
                logger.info(
                    "TEST RUNNER: no completed backend regression tests to run"
                )
                regression_tests = RunEvidence(
                    passed=True,
                    exit_code=0,
                    summary="No completed backend regression tests",
                )
            if not regression_tests.passed:
                classification = self.failure_classifier.classify(regression_tests)
                if classification.category == FailureCategory.DEPENDENCY_FAILURE:
                    dependency_evidence = await self.dependency_manager.resolve_compatibility(
                        f"{regression_tests.stdout}\n{regression_tests.stderr}"
                    )
                    if dependency_evidence.success:
                        logger.info("TEST RUNNER: rerunning completed regression tests")
                        python_executable = dependency_evidence.python_executable
                        regression_tests = await self.test_runner.run(
                            regression_test_paths,
                            python_executable=python_executable,
                        )
                        if regression_tests.passed:
                            classification = None
                        else:
                            classification = self.failure_classifier.classify(
                                regression_tests
                            )
                    if (
                        not dependency_evidence.success
                        or classification
                        and classification.category
                        in {
                            FailureCategory.DEPENDENCY_FAILURE,
                            FailureCategory.ENVIRONMENT_FAILURE,
                            FailureCategory.TEST_FAILURE,
                        }
                    ):
                        files_created, files_modified = self._classify_task_changes(
                            baseline, touched_paths
                        )
                        return self._result(
                            task,
                            TaskStatus.FAILED,
                            repair_attempt,
                            files_created,
                            files_modified,
                            acceptance_criteria,
                            task_test_paths,
                            task_tests,
                            regression_tests,
                            dependency_evidence,
                            regression_test_paths,
                            format_correction_attempts,
                            repair_history,
                            generation_metadata,
                        )
                elif classification.category in {
                    FailureCategory.ENVIRONMENT_FAILURE,
                    FailureCategory.TEST_FAILURE,
                }:
                    files_created, files_modified = self._classify_task_changes(
                        baseline, touched_paths
                    )
                    return self._result(
                        task,
                        TaskStatus.FAILED,
                        repair_attempt,
                        files_created,
                        files_modified,
                        acceptance_criteria,
                        task_test_paths,
                        task_tests,
                        regression_tests,
                        dependency_evidence,
                        regression_test_paths,
                        format_correction_attempts,
                        repair_history,
                        generation_metadata,
                    )
            if regression_tests.passed:
                logger.info("BACKEND AGENT: task ready for review")
                files_created, files_modified = self._classify_task_changes(
                    baseline, touched_paths
                )
                return self._result(
                    task,
                    TaskStatus.REVIEW,
                    repair_attempt,
                    files_created,
                    files_modified,
                    acceptance_criteria,
                    task_test_paths,
                    task_tests,
                    regression_tests,
                    dependency_evidence,
                    regression_test_paths,
                    format_correction_attempts,
                    repair_history,
                    generation_metadata,
                )
            failure_context = self._failure_context(
                "Completed-task backend regression tests failed", regression_tests
            )
            attempted_changes[-1]["outcome"] = "regression_tests_failed"
            logger.info("BACKEND AGENT: analyzing test failure")

        files_created, files_modified = self._classify_task_changes(
            baseline, touched_paths
        )
        return self._result(
            task,
            TaskStatus.FAILED,
            self.max_repair_attempts,
            files_created,
            files_modified,
            acceptance_criteria,
            task_test_paths,
            task_tests,
            regression_tests,
            dependency_evidence,
            regression_test_paths,
            format_correction_attempts,
            repair_history,
            generation_metadata,
        )

    @staticmethod
    def _system_prompt(is_repair: bool) -> str:
        model = BackendRepair if is_repair else BackendImplementation
        schema = json.dumps(model.model_json_schema())
        common = (
            "You are a controlled backend coding agent. Implement only the CURRENT TASK. "
            "Return strict JSON. Every file needs a workspace-relative path under backend/. "
            "Never return markdown, commands, secrets, deployment configuration, or paths "
            "outside backend/. The response MUST match this JSON schema exactly: "
            f"{schema}. Each files item must contain path and content. "
        )
        if not is_repair:
            return common + (
                "This is the initial implementation. Generate task-specific implementation, "
                "backend/requirements.txt, and at least one task test under "
                "backend/tests/test_*.py. Include each task test in task_test_paths."
            )
        return common + (
            "This is a repair attempt. First diagnose the exact failure from the traceback, "
            "current source, current tests, and acceptance criteria. Return failure_category, "
            "root_cause, files_to_modify, repair_strategy, and files. Patch the existing "
            "implementation rather than regenerating unrelated files. Normally modify "
            "application source. Never weaken a valid test merely to pass it. A test change "
            "requires test_change_justification explaining concrete evidence that the "
            "generated test is invalid (such as its SyntaxError or missing fixture). "
            "Preserve valid assertions; never edit completed-task regression tests. "
            "Reuse the same task tests and make meaningful progress."
        )

    async def _generate_initial_implementation(
        self,
        system_prompt: str,
        user_prompt: str,
        task: Task,
        current_snapshot: dict[str, str],
        workspace_file_list: list[str],
    ) -> tuple[BackendImplementation, int, None]:
        def validate(response: dict[str, Any]) -> None:
            implementation = BackendImplementation.model_validate(response)
            self._validate_implementation(
                implementation=implementation,
                existing_task_tests=[],
                require_generated_test=True,
                workspace_files=set(current_snapshot),
            )
            for generated_file in implementation.files:
                if not generated_file.path.endswith(".py"):
                    continue
                try:
                    ast.parse(generated_file.content, filename=generated_file.path)
                except SyntaxError as exc:
                    raise LLMImplementationValidationError(
                        f"{generated_file.path} has invalid Python syntax at line "
                        f"{exc.lineno}: {exc.msg}"
                    ) from exc
            comparison = dict(current_snapshot)
            for generated_file in implementation.files:
                if (
                    generated_file.path not in comparison
                    and generated_file.path in workspace_file_list
                ):
                    comparison[generated_file.path] = self.workspace.read_file(
                        generated_file.path
                    )
            changes = self._changed_paths(implementation, comparison)
            if not changes:
                raise LLMImplementationValidationError(
                    "implementation contains no file content changes"
                )
            if not any(
                not path.startswith("backend/tests/") for path in changes
            ):
                raise LLMImplementationValidationError(
                    "initial implementation contains no source changes"
                )
            changed_tests = set(implementation.task_test_paths) & set(changes)
            if (
                not implementation.task_test_paths
                or changed_tests != set(implementation.task_test_paths)
            ):
                raise LLMImplementationValidationError(
                    "initial implementation contains no generated task-specific tests"
                )

        def correction_prompt(
            error: LLMImplementationValidationError,
            invalid_response: dict[str, Any],
        ) -> tuple[str, str]:
            request = json.loads(user_prompt)
            request.update(
                {
                    "operation": "backend_implementation",
                    "attempt_type": "generation_correction",
                    "generation_validation_error": str(error),
                    "invalid_generation": invalid_response,
                    "required_schema": BackendImplementation.model_json_schema(),
                    "generation_correction_instructions": (
                        "Return a corrected complete initial implementation. Include "
                        "meaningful task-specific source changes and generated "
                        "backend/tests/test_*.py coverage. This is not code repair; "
                        "there is no traceback and no files have been applied."
                    ),
                }
            )
            return (
                system_prompt
                + " Correct the complete initial generation using the validation error.",
                json.dumps(request),
            )

        response = await self.llm_service.generate_json_with_validation(
            system_prompt,
            user_prompt,
            validate,
            correction_prompt,
        )
        return await self._generate_implementation(
            system_prompt,
            user_prompt,
            task,
            is_repair=False,
            initial_response=response,
        )

    async def _generate_implementation(
        self,
        system_prompt: str,
        user_prompt: str,
        task: Task,
        is_repair: bool,
        initial_response: dict[str, Any] | None = None,
    ) -> tuple[BackendImplementation, int, BackendRepair | None]:
        response = (
            initial_response
            if initial_response is not None
            else await self.llm_service.generate_json(system_prompt, user_prompt)
        )
        model = BackendRepair if is_repair else BackendImplementation
        for correction_attempt in range(self.max_format_corrections + 1):
            try:
                validated = model.model_validate(response)
                if isinstance(validated, BackendRepair):
                    implementation = BackendImplementation(
                        files=validated.files,
                        task_test_paths=validated.task_test_paths,
                    )
                    return implementation, correction_attempt, validated
                return validated, correction_attempt, None
            except ValidationError as exc:
                if correction_attempt >= self.max_format_corrections:
                    raise StructuredOutputCorrectionError(
                        f"{model.__name__} remained invalid after bounded corrections",
                        correction_attempt,
                    ) from exc
                logger.info(
                    "BACKEND AGENT: format correction attempt %d",
                    correction_attempt + 1,
                )
                response = await self.llm_service.generate_json(
                    "Correct only the structured response format. Return JSON only and do "
                    "not redesign or regenerate the implementation or repair.",
                    json.dumps(
                        {
                            "operation": "backend_format_correction",
                            "task_id": task.id,
                            "required_schema": model.model_json_schema(),
                            "validation_errors": exc.errors(include_url=False),
                            "invalid_response": response,
                            "instruction": (
                                f"Return only a corrected {model.__name__} object. "
                                "Preserve provided diagnosis and source content exactly."
                            ),
                        }
                    ),
                )
        raise StructuredOutputCorrectionError(
            "Unreachable format correction state", self.max_format_corrections
        )

    @staticmethod
    def _user_prompt(
        task: Task,
        workspace_snapshot: dict[str, str],
        workspace_file_list: list[str],
        acceptance_criteria: list[str],
        completed_dependencies: list[dict[str, Any]],
        task_test_paths: list[str],
        failure_context: dict[str, Any],
        dependency_evidence: DependencyEvidence | None,
        repair_attempt: int,
        attempted_changes: list[dict[str, Any]],
    ) -> str:
        existing_tests = {
            path: workspace_snapshot[path]
            for path in task_test_paths
            if path in workspace_snapshot
        }
        relevant_source = {
            path: content
            for path, content in workspace_snapshot.items()
            if not path.startswith("backend/tests/")
        }
        details = BackendDeveloperAgent._failure_details(failure_context)
        same_signature = bool(
            attempted_changes
            and attempted_changes[-1].get("strategy_outcome")
            == "same_failure_signature_strategy_failed"
        )
        return json.dumps(
            {
                "operation": (
                    "backend_repair" if repair_attempt else "backend_implementation"
                ),
                "attempt_number": repair_attempt,
                "attempt_type": (
                    f"repair_attempt_{repair_attempt}"
                    if repair_attempt
                    else "initial_generation"
                ),
                "original_task": task.model_dump(mode="json"),
                "current_task": {
                    "id": task.id,
                    "title": task.title,
                    "description": task.description,
                },
                "acceptance_criteria": acceptance_criteria,
                "existing_workspace_file_list": workspace_file_list,
                "relevant_existing_file_contents": workspace_snapshot,
                "relevant_source_files": relevant_source,
                "relevant_test_files": existing_tests,
                "existing_task_test_paths": task_test_paths,
                "existing_task_tests": existing_tests,
                "exact_failing_test_paths": task_test_paths if repair_attempt else [],
                "pytest_stdout": failure_context.get("pytest_stdout", ""),
                "pytest_stderr": failure_context.get("pytest_stderr", ""),
                "failure_message": failure_context.get("message", ""),
                "exception_type": details["exception_type"],
                "exception_message": details["exception_message"],
                "traceback": details["traceback"],
                "failure_signature": details["failure_signature"],
                "previous_strategy_failed_with_same_signature": same_signature,
                "previous_attempted_changes": attempted_changes,
                "completed_dependencies": completed_dependencies,
                "repair_instructions": (
                    "Analyze the exact exception and traceback, diagnose the root cause, "
                    "then patch only relevant existing files so the same tests pass. Do not "
                    "regenerate unrelated files or weaken valid tests. If the previous "
                    "failure signature is unchanged, choose a different repair strategy."
                    if repair_attempt
                    else "Generate the initial task implementation and task-specific tests."
                ),
                "dependency_installation": (
                    dependency_evidence.model_dump(mode="json")
                    if dependency_evidence
                    else None
                ),
            }
        )

    @staticmethod
    def _derive_acceptance_criteria(task: Task) -> list[str]:
        return [
            f"Implement the current task: {task.title}.",
            f"Satisfy the requested behavior: {task.description}",
            "Register the task behavior with the FastAPI application when routing is required.",
            "Add task-specific pytest coverage for successful behavior and key validation.",
            "Pass task-specific tests and regressions owned by completed backend tasks.",
        ]

    def _workspace_snapshot(self) -> dict[str, str]:
        snapshot: dict[str, str] = {}
        remaining = 20_000
        for path in self._workspace_file_list()[:80]:
            if remaining <= 0:
                break
            if (
                "__pycache__" in path
                or "/.venv/" in f"/{path}"
                or path.endswith((".pyc", ".pyo"))
            ):
                continue
            try:
                content = self.workspace.read_file(path)[:remaining]
            except (OSError, UnicodeError):
                continue
            snapshot[path] = content
            remaining -= len(content)
        return snapshot

    def _workspace_file_list(self) -> list[str]:
        return [
            path
            for path in self.workspace.list_files()
            if "__pycache__" not in path
            and "/.venv/" not in f"/{path}"
            and not path.endswith((".pyc", ".pyo"))
        ]

    def _repair_snapshot(
        self,
        workspace_files: list[str],
        task_test_paths: list[str],
        failure_context: dict[str, Any],
        attempted_changes: list[dict[str, Any]],
    ) -> dict[str, str]:
        workspace_set = set(workspace_files)
        failure_text = "\n".join(
            str(failure_context.get(key, ""))
            for key in ("message", "pytest_stdout", "pytest_stderr")
        )
        relevant = set(task_test_paths)
        for attempt in attempted_changes:
            relevant.update(attempt.get("implementation_files", []))
            relevant.update(attempt.get("changed_files", []))

        for match in re.findall(r"backend/[A-Za-z0-9_./-]+\.py", failure_text):
            normalized = match.rstrip("\'\"),]:")
            if normalized in workspace_set:
                relevant.add(normalized)
        traceback_names = set(
            re.findall(r"(?:^|[/\\])([A-Za-z_][A-Za-z0-9_]*\.py)", failure_text)
        )
        for path in workspace_files:
            if PurePosixPath(path).name in traceback_names:
                relevant.add(path)

        relevant &= workspace_set
        for _ in range(3):
            discovered: set[str] = set()
            for path in sorted(relevant):
                if not path.endswith(".py"):
                    continue
                try:
                    content = self.workspace.read_file(path)
                except (OSError, UnicodeError):
                    continue
                discovered.update(
                    self._imported_workspace_paths(path, content, workspace_set)
                )
            additions = discovered - relevant
            if not additions:
                break
            relevant.update(additions)

        if "backend/app/main.py" in workspace_set and any(
            path.startswith("backend/app/") for path in relevant
        ):
            relevant.add("backend/app/main.py")

        snapshot: dict[str, str] = {}
        for path in sorted(relevant):
            try:
                snapshot[path] = self.workspace.read_file(path)
            except (OSError, UnicodeError):
                continue
        return snapshot

    @staticmethod
    def _imported_workspace_paths(
        source_path: str,
        content: str,
        workspace_files: set[str],
    ) -> set[str]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return set()
        modules: set[str] = set()
        source_parts = list(PurePosixPath(source_path).with_suffix("").parts)
        package_parts = source_parts[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    keep = max(0, len(package_parts) - node.level + 1)
                    prefix = package_parts[:keep]
                    module_parts = node.module.split(".") if node.module else []
                    base = ".".join(prefix + module_parts)
                else:
                    base = node.module or ""
                if base:
                    modules.add(base)
                for alias in node.names:
                    if alias.name != "*":
                        modules.add(".".join(part for part in (base, alias.name) if part))

        paths: set[str] = set()
        for module in modules:
            if not module.startswith("backend"):
                continue
            candidate = module.replace(".", "/")
            for path in (f"{candidate}.py", f"{candidate}/__init__.py"):
                if path in workspace_files:
                    paths.add(path)
        return paths

    @staticmethod
    def _failure_details(failure_context: dict[str, Any]) -> dict[str, str]:
        stdout = str(failure_context.get("pytest_stdout", ""))
        stderr = str(failure_context.get("pytest_stderr", ""))
        traceback = "\n".join(part for part in (stdout, stderr) if part)
        matches = re.findall(
            r"(?m)^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*(.+)$",
            traceback,
        )
        if matches:
            exception_type, exception_message = matches[-1]
        else:
            exception_type = ""
            lines = [line.strip() for line in traceback.splitlines() if line.strip()]
            exception_message = lines[-1] if lines else str(
                failure_context.get("message", "")
            )
        normalized_message = re.sub(r"\s+", " ", exception_message).strip()
        signature = (
            f"{exception_type or 'UNKNOWN'}:{normalized_message}"[:500]
            if traceback or normalized_message
            else ""
        )
        return {
            "exception_type": exception_type,
            "exception_message": exception_message,
            "traceback": traceback,
            "failure_signature": signature,
            "test_result": str(failure_context.get("message", "")),
        }

    @classmethod
    def _failure_signature(cls, evidence: RunEvidence) -> str:
        return cls._failure_details(
            {
                "message": evidence.summary,
                "pytest_stdout": evidence.stdout,
                "pytest_stderr": evidence.stderr,
            }
        )["failure_signature"] if not evidence.passed else "PASS"

    def _validate_repair_analysis(
        self,
        repair: BackendRepair,
        changed_paths: list[str],
        relevant_paths: set[str],
        failure_context: dict[str, Any],
    ) -> None:
        for item in repair.files:
            if item.path.endswith(".py"):
                try:
                    ast.parse(item.content, filename=item.path)
                except SyntaxError as exc:
                    raise ValueError(
                        f"Repair still has invalid Python syntax in {item.path} at line {exc.lineno}: {exc.msg}"
                    ) from exc
        declared = set(repair.files_to_modify)
        generated = {item.path for item in repair.files}
        for path in declared:
            self._validate_generated_path(path)
        if not generated.issubset(declared):
            undeclared = sorted(generated - declared)
            raise ValueError(
                "Repair contains undeclared patch files: " + ", ".join(undeclared)
            )
        if not set(changed_paths).issubset(relevant_paths):
            raise ValueError("Repair may modify only traceback-relevant existing files")
        changed_tests = [
            path for path in changed_paths if path.startswith("backend/tests/")
        ]
        changed_source = [
            path for path in changed_paths if not path.startswith("backend/tests/")
        ]
        if changed_tests:
            justification = (repair.test_change_justification or "").lower()
            if not any(
                term in justification
                for term in ("contradict", "inconsistent", "invalid test", "test is invalid")
            ):
                raise ValueError(
                    "Changing a test requires explicit evidence that it contradicts "
                    "the task or acceptance criteria"
                )
        if changed_tests and not changed_source:
            failure_text = "\n".join(
                str(failure_context.get(key, ""))
                for key in ("pytest_stdout", "pytest_stderr")
            ).lower()
            named_test = any(path.lower() in failure_text for path in changed_tests)
            invalid_test = (
                "syntaxerror" in failure_text
                or "fixture " in failure_text and " not found" in failure_text
            )
            if not named_test or not invalid_test:
                raise ValueError("Test-only repair requires pytest evidence that the current task test is invalid")
        if not changed_source and not changed_tests:
            raise ValueError("Repair made no meaningful progress")

    def _validate_implementation(
        self,
        implementation: BackendImplementation,
        existing_task_tests: list[str],
        require_generated_test: bool,
        workspace_files: set[str],
    ) -> None:
        generated_paths = {item.path for item in implementation.files}
        for item in implementation.files:
            self._validate_generated_path(item.path)
        for test_path in implementation.task_test_paths:
            self._validate_generated_path(test_path)
            path = PurePosixPath(test_path)
            if (
                not test_path.startswith("backend/tests/test_")
                or path.suffix != ".py"
                or (
                    test_path not in generated_paths
                    and test_path not in workspace_files
                    and test_path not in existing_task_tests
                )
            ):
                raise ValueError(
                    "Task tests must be generated or existing backend/tests/test_*.py files"
                )
        if require_generated_test and not set(
            implementation.task_test_paths
        ).issubset(generated_paths):
            raise ValueError(
                "Initial task tests must be generated backend/tests/test_*.py files"
            )

    @staticmethod
    def _changed_paths(
        implementation: BackendImplementation,
        current_snapshot: dict[str, str],
    ) -> list[str]:
        return [
            item.path
            for item in implementation.files
            if current_snapshot.get(item.path) != item.content
        ]

    def _write_changed_files(
        self,
        implementation: BackendImplementation,
        current_snapshot: dict[str, str],
    ) -> None:
        for generated_file in implementation.files:
            if current_snapshot.get(generated_file.path) == generated_file.content:
                continue
            self.workspace.write_file(generated_file.path, generated_file.content)

    def _classify_task_changes(
        self, baseline: dict[str, str], touched_paths: set[str]
    ) -> tuple[list[str], list[str]]:
        current = self._change_baseline(touched_paths)
        created = sorted(
            path for path in touched_paths if path not in baseline and path in current
        )
        modified = sorted(
            path
            for path in touched_paths
            if path in baseline and current.get(path) != baseline[path]
        )
        return created, modified

    def _change_baseline(
        self, paths: set[str] | None = None
    ) -> dict[str, str]:
        candidates = sorted(paths) if paths is not None else self._workspace_file_list()
        baseline: dict[str, str] = {}
        for path in candidates:
            try:
                content = self.workspace.read_file(path)
            except (OSError, UnicodeError):
                continue
            baseline[path] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return baseline

    @staticmethod
    def _validate_generated_path(path: str) -> None:
        normalized = path.replace("\\", "/")
        parsed = PurePosixPath(normalized)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or not parsed.parts
            or parsed.parts[0] != "backend"
        ):
            raise WorkspacePathError("Backend agent may only write under backend/")

    @staticmethod
    def _failure_context(label: str, evidence: RunEvidence) -> dict[str, str]:
        return {
            "message": label,
            "pytest_stdout": evidence.stdout,
            "pytest_stderr": evidence.stderr,
        }

    def _result(
        self,
        task: Task,
        status: TaskStatus,
        attempts: int,
        files_created: list[str],
        files_modified: list[str],
        acceptance_criteria: list[str],
        task_test_paths: list[str],
        task_tests: RunEvidence | None,
        regression_tests: RunEvidence | None,
        dependencies: DependencyEvidence | None,
        regression_test_paths: list[str],
        format_correction_attempts: int,
        repair_history: list[RepairAttemptEvidence],
        generation_metadata: GenerationRecoveryMetadata | None,
    ) -> AgentResult:
        files_changed = files_created + files_modified
        acceptance_results = self._evaluate_acceptance_criteria(
            task,
            acceptance_criteria,
            files_created,
            files_modified,
            task_test_paths,
            task_tests,
            regression_tests,
        )
        criteria_satisfied = bool(acceptance_results) and all(
            result.satisfied
            for result in acceptance_results
            if result.required and result.applicable
        )
        implementation_evidence = any(
            not path.startswith("backend/tests/") for path in files_changed
        )
        tests_passed = bool(task_test_paths and task_tests and task_tests.passed)
        regressions_passed = bool(regression_tests and regression_tests.passed)
        dependencies_passed = bool(dependencies and dependencies.success)
        no_unresolved_failure = status == TaskStatus.REVIEW
        gates = {
            "implementation_evidence": implementation_evidence,
            "task_specific_changes": bool(files_changed),
            "task_tests": tests_passed,
            "regression_tests": regressions_passed,
            "dependencies": dependencies_passed,
            "acceptance_criteria": criteria_satisfied,
            "no_unresolved_failure": no_unresolved_failure,
        }
        rejection_reason = self._first_rejection_reason(gates)
        completion_gate = CompletionGateResult(
            **gates,
            passed=all(gates.values()),
            rejection_reason=rejection_reason,
        )
        return AgentResult(
            task_id=task.id,
            agent=self.name,
            status=status,
            summary=(
                "Task-specific backend implementation ready after initial generation "
                f"and {attempts} repair attempt(s)."
                if status == TaskStatus.REVIEW
                else "Backend implementation failed after initial generation and "
                f"{attempts} repair attempt(s)."
            ),
            proposed_implementation={
                "scope": "backend",
                "current_task": task.id,
                "dangerous_actions_performed": False,
            },
            safety_notes=[
                "Writes were confined to the configured workspace.",
                "Dependencies were restricted to an approved allowlist.",
                "Only current-task tests and completed-task regression tests were executed.",
                "No deployment, production access, Git operation, or arbitrary shell was used.",
            ],
            files_changed=files_changed,
            files_created=files_created,
            files_modified=files_modified,
            acceptance_criteria=acceptance_criteria,
            acceptance_results=acceptance_results,
            acceptance_criteria_satisfied=criteria_satisfied,
            completion_gate=completion_gate,
            rejection_reason=rejection_reason,
            task_test_paths=task_test_paths,
            regression_test_paths=regression_test_paths,
            task_tests=task_tests,
            tests=regression_tests,
            dependencies=dependencies,
            generation_attempts=(
                generation_metadata.generation_attempts
                if generation_metadata
                else 1
            ),
            format_correction_attempts=format_correction_attempts,
            generation_correction_attempts=(
                generation_metadata.generation_correction_attempts
                if generation_metadata
                else 0
            ),
            provider_fallback_count=(
                generation_metadata.provider_fallback_count
                if generation_metadata
                else 0
            ),
            code_repair_attempts=attempts,
            attempts=attempts,
            execution_stage=self._result_stage(
                status,
                attempts,
                task_tests,
                dependencies,
                format_correction_attempts,
            ),
            failure_stage=(
                self._result_stage(
                    status,
                    attempts,
                    task_tests,
                    dependencies,
                    format_correction_attempts,
                )
                if status == TaskStatus.FAILED
                else None
            ),
            repair_history=repair_history,
        )

    @staticmethod
    def _result_stage(
        status: TaskStatus,
        repair_attempts: int,
        task_tests: RunEvidence | None,
        dependencies: DependencyEvidence | None,
        format_corrections: int,
    ) -> ExecutionStage:
        if status == TaskStatus.REVIEW:
            return ExecutionStage.MANAGER_REVIEW
        if task_tests is not None:
            return (
                ExecutionStage.CODE_REPAIR
                if repair_attempts
                else ExecutionStage.TEST_EXECUTION
            )
        if dependencies is not None:
            return ExecutionStage.DEPENDENCY_PREPARATION
        if format_corrections:
            return ExecutionStage.FORMAT_VALIDATION
        return ExecutionStage.IMPLEMENTATION_VALIDATION

    def _evaluate_acceptance_criteria(
        self,
        task: Task,
        criteria: list[str],
        files_created: list[str],
        files_modified: list[str],
        task_test_paths: list[str],
        task_tests: RunEvidence | None,
        regression_tests: RunEvidence | None,
    ) -> list[AcceptanceResult]:
        changed = files_created + files_modified
        implementation_files = [
            path for path in changed if not path.startswith("backend/tests/")
        ]
        workspace_files = set(self._workspace_file_list())
        existing_tests = [path for path in task_test_paths if path in workspace_files]
        task_passed = bool(task_tests and task_tests.passed)
        regression_passed = bool(regression_tests and regression_tests.passed)
        task_text = f"{task.title} {task.description}".lower()
        routing_required = any(
            term in task_text
            for term in (" api", "endpoint", "route", "router", "http ")
        )
        return [
            AcceptanceResult(
                criterion=criteria[0],
                satisfied=bool(implementation_files),
                evidence=implementation_files,
            ),
            AcceptanceResult(
                criterion=criteria[1],
                satisfied=task_passed,
                evidence=[task_tests.summary] if task_tests else [],
            ),
            AcceptanceResult(
                criterion=criteria[2],
                applicable=routing_required,
                satisfied=task_passed if routing_required else True,
                evidence=(
                    ["Routing behavior covered by passing task tests"]
                    if routing_required and task_passed
                    else ["Not applicable to this non-routing task"]
                    if not routing_required
                    else []
                ),
            ),
            AcceptanceResult(
                criterion=criteria[3],
                satisfied=bool(existing_tests) and task_passed,
                evidence=existing_tests
                + ([task_tests.summary] if task_tests and task_passed else []),
            ),
            AcceptanceResult(
                criterion=criteria[4],
                satisfied=regression_passed,
                evidence=[regression_tests.summary] if regression_tests else [],
            ),
        ]

    @staticmethod
    def _first_rejection_reason(gates: dict[str, bool]) -> str | None:
        reason_codes = {
            "implementation_evidence": "missing_implementation_evidence",
            "task_specific_changes": "missing_task_specific_changes",
            "task_tests": "task_tests_missing_or_failed",
            "regression_tests": "completed_task_regressions_failed",
            "dependencies": "dependency_preparation_failed",
            "acceptance_criteria": "required_acceptance_criterion_unsatisfied",
            "no_unresolved_failure": "unresolved_execution_failure",
        }
        return next(
            (reason_codes[name] for name, passed in gates.items() if not passed),
            None,
        )
