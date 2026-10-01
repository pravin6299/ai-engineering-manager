import logging
import re
from typing import Any

from app.models.task import CompletionGateResult, AgentResult, ExecutionStage, Task, TaskStatus
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


class ManagerAgent:
    def __init__(self, llm_service: LLMService) -> None:
        self.llm_service = llm_service

    async def analyze_requirement(self, requirement: str) -> list[Task]:
        logger.info("MANAGER: analyzing requirement")
        system_prompt = (
            "You are a Phase 2 AI Engineering Manager. Break requirements into backend "
            "and frontend tasks only. Return strict JSON with a 'tasks' array. Each task "
            "must have id, title, description, assigned_agent, priority, status, depends_on, "
            "provides, and requires. provides/requires are normalized capability names such "
            "as authentication, jwt_tokens, or a domain API capability. "
            "IDs must be exactly TASK-001, TASK-002, and so on in sequence. "
            "Allowed agents: backend, frontend. Allowed statuses: pending, assigned, blocked, "
            "in_progress, review, completed, failed. Model dependencies explicitly."
        )
        llm_response = await self.llm_service.generate_json(system_prompt, requirement)
        normalized_tasks = self._normalize_task_ids(llm_response.get("tasks", []))
        tasks = [Task.model_validate(task) for task in normalized_tasks]
        self._apply_semantic_dependencies(tasks)
        self._validate_dependencies(tasks)
        logger.info("MANAGER: created tasks")
        return tasks

    @staticmethod
    def _normalize_task_ids(raw_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        id_map: dict[str, str] = {}
        for index, raw_task in enumerate(raw_tasks, start=1):
            raw_id = raw_task.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise ValueError(f"Task at position {index} has no valid id")
            if raw_id in id_map:
                raise ValueError(f"Duplicate task id from Manager LLM: {raw_id}")
            id_map[raw_id] = f"TASK-{index:03d}"

        normalized: list[dict[str, Any]] = []
        for raw_task in raw_tasks:
            raw_id = raw_task["id"]
            dependencies = raw_task.get("depends_on", [])
            if not isinstance(dependencies, list):
                raise ValueError(f"{raw_id} depends_on must be a list")
            unknown = [
                dependency
                for dependency in dependencies
                if not isinstance(dependency, str) or dependency not in id_map
            ]
            if unknown:
                raise ValueError(f"{raw_id} depends on unknown tasks: {unknown}")
            normalized.append(
                {
                    **raw_task,
                    "id": id_map[raw_id],
                    "depends_on": [id_map[dependency] for dependency in dependencies],
                }
            )
        return normalized

    def review_result(self, task: Task, result: AgentResult) -> AgentResult:
        logger.info("MANAGER: reviewing result for %s", task.id)
        if result.agent.value in {"backend", "frontend"}:
            changed = set(result.files_created + result.files_modified)
            gates = {
                "implementation_evidence": any(
                    not path.startswith("backend/tests/") for path in changed
                ),
                "task_specific_changes": bool(changed),
                "task_tests": bool(
                    result.task_test_paths
                    and result.task_tests
                    and result.task_tests.passed
                ),
                "regression_tests": bool(result.tests and result.tests.passed),
                "dependencies": bool(
                    result.dependencies and result.dependencies.success
                ),
                "acceptance_criteria": bool(
                    result.acceptance_results
                    and result.acceptance_criteria_satisfied
                    and all(
                        criterion.satisfied
                        for criterion in result.acceptance_results
                        if criterion.required and criterion.applicable
                    )
                ),
                "no_unresolved_failure": result.status == TaskStatus.REVIEW,
            }
            reason = (
                "dependency_preparation_failed"
                if result.failure_stage == ExecutionStage.DEPENDENCY_PREPARATION
                and not gates["dependencies"]
                else self._completion_rejection_reason(gates)
            )
            passed = all(gates.values())
            result.completion_gate = CompletionGateResult(
                **gates,
                passed=passed,
                rejection_reason=reason,
            )
            result.rejection_reason = reason
            logger.info(
                "COMPLETION GATE %s: implementation_evidence=%s "
                "task_specific_changes=%s task_tests=%s regression_tests=%s "
                "dependencies=%s acceptance_criteria=%s "
                "no_unresolved_failure=%s reason=%s",
                task.id,
                self._gate_label(gates["implementation_evidence"]),
                self._gate_label(gates["task_specific_changes"]),
                self._gate_label(gates["task_tests"]),
                self._gate_label(gates["regression_tests"]),
                self._gate_label(gates["dependencies"]),
                self._gate_label(gates["acceptance_criteria"]),
                self._gate_label(gates["no_unresolved_failure"]),
                reason or "none",
            )
            if passed:
                result.status = TaskStatus.COMPLETED
                task.status = TaskStatus.COMPLETED
                result.summary = f"Task-specific {result.agent.value} implementation completed."
                logger.info("MANAGER: %s completed", task.id)
            else:
                result.status = TaskStatus.FAILED
                task.status = TaskStatus.FAILED
                result.summary = f"Manager review rejected completion: {reason}"
                logger.info("MANAGER: %s failed: %s", task.id, reason)
        else:
            task.status = result.status
        return result

    @staticmethod
    def _gate_label(passed: bool) -> str:
        return "PASS" if passed else "FAIL"

    @staticmethod
    def _completion_rejection_reason(gates: dict[str, bool]) -> str | None:
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

    def _validate_dependencies(self, tasks: list[Task]) -> None:
        task_ids = {task.id for task in tasks}
        for task in tasks:
            unknown = [dependency for dependency in task.depends_on if dependency not in task_ids]
            if unknown:
                raise ValueError(f"{task.id} depends on unknown tasks: {', '.join(unknown)}")
            if task.id in task.depends_on:
                raise ValueError(f"{task.id} cannot depend on itself")

        task_map = {task.id: task for task in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError(f"Circular dependency detected at {task_id}")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency_id in task_map[task_id].depends_on:
                visit(dependency_id)
            visiting.remove(task_id)
            visited.add(task_id)

        for task in tasks:
            visit(task.id)

    def _apply_semantic_dependencies(self, tasks: list[Task]) -> None:
        for task in tasks:
            task.provides = self._normalized_capabilities(task.provides)
            task.requires = self._normalized_capabilities(task.requires)
            text = f"{task.title} {task.description}".lower()
            if "auth" in text and any(
                verb in text for verb in ("create", "implement", "build", "design")
            ):
                task.provides = list(dict.fromkeys(task.provides + ["authentication"]))
            if any(
                phrase in text
                for phrase in (
                    "authenticated user",
                    "requires login",
                    "require login",
                    "jwt protected",
                    "jwt-protected",
                )
            ):
                task.requires = list(dict.fromkeys(task.requires + ["authentication"]))

        providers: dict[str, list[str]] = {}
        for task in tasks:
            for capability in task.provides:
                providers.setdefault(capability, []).append(task.id)

        for task in tasks:
            dependencies = list(task.depends_on)
            for capability in task.requires:
                producer = next(
                    (
                        task_id
                        for task_id in providers.get(capability, [])
                        if task_id != task.id
                    ),
                    None,
                )
                if producer and producer not in dependencies:
                    dependencies.append(producer)
            task.depends_on = dependencies

    @staticmethod
    def _normalized_capabilities(capabilities: list[str]) -> list[str]:
        normalized = []
        for capability in capabilities:
            value = re.sub(r"[^a-z0-9]+", "_", capability.strip().lower()).strip("_")
            if value and value not in normalized:
                normalized.append(value)
        return normalized
