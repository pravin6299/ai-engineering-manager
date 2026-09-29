import logging

from app.models.task import AgentName, AgentResult, Task, TaskStatus

logger = logging.getLogger(__name__)


class FrontendDeveloperAgent:
    name = AgentName.FRONTEND

    async def process(self, task: Task) -> AgentResult:
        logger.info("FRONTEND AGENT: processing %s", task.id)
        return AgentResult(
            task_id=task.id,
            agent=self.name,
            status=TaskStatus.REVIEW,
            summary=f"Frontend proposal prepared for {task.title}.",
            proposed_implementation={
                "scope": "frontend",
                "deliverables": [
                    "screen and route proposal",
                    "component breakdown",
                    "API integration expectations",
                    "client-side state considerations",
                ],
                "dangerous_actions_performed": False,
            },
            safety_notes=[
                "No shell commands executed.",
                "No repositories, deployments, production systems, or external services modified.",
            ],
        )
