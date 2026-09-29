import logging

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.models.task import AgentName, AgentResult, ProjectResponse, Task, TaskStatus

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

    async def create_project(self, requirement: str) -> ProjectResponse:
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
                if task.assigned_agent == AgentName.BACKEND:
                    dependency_context = [
                        {
                            "task_id": dependency_id,
                            "title": task_map[dependency_id].title,
                            "description": task_map[dependency_id].description,
                            "status": task_map[dependency_id].status.value,
                            "files_created": results_by_id[dependency_id].files_created,
                            "files_modified": results_by_id[dependency_id].files_modified,
                        }
                        for dependency_id in task.depends_on
                        if dependency_id in results_by_id
                    ]
                    result = await self.agents[task.assigned_agent].process(
                        task, dependency_context
                    )
                else:
                    result = await self.agents[task.assigned_agent].process(task)
                reviewed = self.manager_agent.review_result(task, result)
                results.append(reviewed)
                results_by_id[task.id] = reviewed

        return ProjectResponse(requirement=requirement, tasks=tasks, results=results)

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
