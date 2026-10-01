import logging

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.models.task import (
    AgentName,
    AgentResult,
    ProjectResponse,
    Task,
    TaskStatus,
    TaskTestOwnership,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        manager_agent: ManagerAgent,
        backend_agent: BackendDeveloperAgent,
        frontend_agent: FrontendDeveloperAgent,
    ) -> None:
        self.manager_agent = manager_agent
        self.agents = {
            AgentName.BACKEND: backend_agent,
            AgentName.FRONTEND: frontend_agent,
        }
        self.backend_test_ownership: dict[str, TaskTestOwnership] = {}
        self.frontend_test_ownership: dict[str, TaskTestOwnership] = {}

    async def create_project(self, requirement: str) -> ProjectResponse:
        self.backend_test_ownership = {}
        self.frontend_test_ownership = {}
        tasks = await self.manager_agent.analyze_requirement(requirement)
        self._validate_acyclic(tasks)
        results: list[AgentResult] = []
        results_by_id: dict[str, AgentResult] = {}
        task_map = {task.id: task for task in tasks}

        for task in tasks:
            task.status = TaskStatus.BLOCKED if task.depends_on else TaskStatus.ASSIGNED

        while True:
            eligible = [
                task
                for task in tasks
                if task.status in {TaskStatus.ASSIGNED, TaskStatus.BLOCKED}
                and all(
                    task_map[dependency].status == TaskStatus.COMPLETED
                    for dependency in task.depends_on
                )
            ]
            if not eligible:
                break

            for task in eligible:
                if task.depends_on:
                    logger.info("ORCHESTRATOR: %s dependency satisfied", task.id)
                task.status = TaskStatus.IN_PROGRESS
                logger.info("MANAGER: %s assigned to %s", task.id, task.assigned_agent.value)
                logger.info(
                    "ORCHESTRATOR: assigning %s to %s",
                    task.id,
                    task.assigned_agent.value,
                )
                dependency_context = [
                    {
                        "task_id": dependency_id,
                        "title": task_map[dependency_id].title,
                        "description": task_map[dependency_id].description,
                        "status": task_map[dependency_id].status.value,
                        "provides": task_map[dependency_id].provides,
                        "files_created": results_by_id[dependency_id].files_created,
                        "files_modified": results_by_id[dependency_id].files_modified,
                    }
                    for dependency_id in task.depends_on
                    if dependency_id in results_by_id
                ]
                regression_paths = (
                    self._completed_backend_regression_paths()
                    if task.assigned_agent == AgentName.BACKEND
                    else self._completed_frontend_regression_paths()
                )
                result = await self.agents[task.assigned_agent].process(
                    task,
                    dependency_context,
                    regression_paths,
                )
                reviewed = self.manager_agent.review_result(task, result)
                results.append(reviewed)
                results_by_id[task.id] = reviewed
                if task.assigned_agent == AgentName.BACKEND:
                    self.backend_test_ownership[task.id] = TaskTestOwnership(
                        task_id=task.id,
                        test_paths=reviewed.task_test_paths,
                        completion_status=reviewed.status,
                    )

                elif task.assigned_agent == AgentName.FRONTEND:
                    self.frontend_test_ownership[task.id] = TaskTestOwnership(
                        task_id=task.id,
                        test_paths=reviewed.task_test_paths,
                        completion_status=reviewed.status,
                    )

        return ProjectResponse(requirement=requirement, tasks=tasks, results=results)

    def _completed_backend_regression_paths(self) -> list[str]:
        return list(
            dict.fromkeys(
                path
                for ownership in self.backend_test_ownership.values()
                if ownership.completion_status == TaskStatus.COMPLETED
                for path in ownership.test_paths
            )
        )

    def _completed_frontend_regression_paths(self) -> list[str]:
        return list(
            dict.fromkeys(
                path
                for ownership in self.frontend_test_ownership.values()
                if ownership.completion_status == TaskStatus.COMPLETED
                for path in ownership.test_paths
            )
        )

    @staticmethod
    def _validate_acyclic(tasks: list[Task]) -> None:
        task_map = {task.id: task for task in tasks}
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(task: Task) -> None:
            if task.id in permanent:
                return
            if task.id in temporary:
                raise ValueError(f"Circular dependency detected at {task.id}")
            temporary.add(task.id)
            for dependency_id in task.depends_on:
                visit(task_map[dependency_id])
            temporary.remove(task.id)
            permanent.add(task.id)

        for task in tasks:
            visit(task)
