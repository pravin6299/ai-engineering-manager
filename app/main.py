import logging
from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.orchestrator.workflow import Orchestrator
from app.models.task import ProjectRequest, ProjectResponse
from app.services.llm import ProviderLLMService, create_llm_service
from app.tools.dependency_manager import DependencyManager
from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspaceTool

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

app = FastAPI(title="AI Engineering Manager", version="0.2.0")

llm_service = create_llm_service()
logger.info(
    "STARTUP: %s configuration %s",
    llm_service.provider,
    "detected"
    if isinstance(llm_service, ProviderLLMService)
    and llm_service.configuration_detected
    else "not detected; local fallback enabled",
)
manager_agent = ManagerAgent(llm_service=llm_service)
workspace_tool = WorkspaceTool("workspace")
orchestrator = Orchestrator(
    manager_agent=manager_agent,
    backend_agent=BackendDeveloperAgent(
        llm_service=llm_service,
        workspace=workspace_tool,
        test_runner=BackendTestRunner(workspace_tool.root),
        dependency_manager=DependencyManager(workspace_tool.root),
    ),
    frontend_agent=FrontendDeveloperAgent(),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/projects", response_model=ProjectResponse)
async def create_project(request: ProjectRequest) -> ProjectResponse:
    return await orchestrator.create_project(request.requirement)
