import logging
from typing import Any

from app.models.task import AgentResult, Task, TaskStatus
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
            "must have id, title, description, assigned_agent, priority, status, depends_on. "
            "IDs must be exactly TASK-001, TASK-002, and so on in sequence. "
            "Allowed agents: backend, frontend. Allowed statuses: pending, assigned, blocked, "
            "in_progress, review, completed, failed. Model dependencies explicitly."
        )
        llm_response = await self.llm_service.generate_json(system_prompt, requirement)
        normalized_tasks = self._normalize_task_ids(llm_response.get("tasks", []))
        tasks = [Task.model_validate(task) for task in normalized_tasks]
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
        if result.agent.value == "backend":
            changed = set(result.files_created + result.files_modified)
            has_task_implementation = any(
                not path.startswith("backend/tests/") for path in changed
            )
            has_task_tests = bool(result.task_test_paths) and set(
                result.task_test_paths
            ).issubset(changed)
            evidence_is_complete = bool(
                has_task_implementation
                and has_task_tests
                and result.acceptance_criteria
                and result.acceptance_criteria_satisfied
                and result.task_tests
                and result.task_tests.passed
                and result.tests
                and result.tests.passed
                and result.dependencies
                and result.dependencies.success
            )
            if evidence_is_complete:
                result.status = TaskStatus.COMPLETED
                task.status = TaskStatus.COMPLETED
                logger.info("MANAGER: %s completed", task.id)
            else:
                result.status = TaskStatus.FAILED
                task.status = TaskStatus.FAILED
                logger.info("MANAGER: %s failed", task.id)
        else:
            task.status = result.status
        return result

    def _validate_dependencies(self, tasks: list[Task]) -> None:
        task_ids = {task.id for task in tasks}
        for task in tasks:
            unknown = [dependency for dependency in task.depends_on if dependency not in task_ids]
            if unknown:
                raise ValueError(f"{task.id} depends on unknown tasks: {', '.join(unknown)}")
