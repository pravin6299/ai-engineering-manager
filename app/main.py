import logging
import os
from hmac import compare_digest
from pathlib import Path

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.orchestrator.workflow import Orchestrator
from app.models.task import ProjectRequest, ProjectResponse
from app.models.run_tests import RunTestsRequest, RunTestsResponse
from app.models.write_files import WriteFilesRequest, WriteFilesResponse
from app.services.llm import create_llm_service
from app.tools.api_contract import BackendAPIContractCollector
from app.tools.dependency_manager import DependencyManager
from app.tools.frontend_dependency_manager import FrontendDependencyManager
from app.tools.frontend_test_runner import FrontendTestRunner
from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspacePathError, WorkspaceTool

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
logger.info(
    "STARTUP: n8n tool API key %s",
    "configured" if os.getenv("N8N_TOOL_API_KEY") else "not configured",
)

app = FastAPI(title="AI Engineering Manager", version="0.2.0")

llm_service = create_llm_service()
logger.info(
    "STARTUP: LLM router configuration %s",
    "detected" if llm_service.configuration_detected else "not detected; local fallback enabled",
)
manager_agent = ManagerAgent(llm_service=llm_service)
workspace_tool = WorkspaceTool(ROOT_DIR / "workspace")
orchestrator = Orchestrator(
    manager_agent=manager_agent,
    backend_agent=BackendDeveloperAgent(
        llm_service=llm_service,
        workspace=workspace_tool,
        test_runner=BackendTestRunner(workspace_tool.root),
        dependency_manager=DependencyManager(workspace_tool.root),
    ),
    frontend_agent=FrontendDeveloperAgent(
        llm_service=llm_service,
        workspace=workspace_tool,
        test_runner=FrontendTestRunner(workspace_tool.root),
        dependency_manager=FrontendDependencyManager(workspace_tool.root),
        api_contract_collector=BackendAPIContractCollector(workspace_tool),
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/projects", response_model=ProjectResponse)
async def create_project(request: ProjectRequest) -> ProjectResponse:
    return await orchestrator.create_project(request.requirement)


def _write_files_error(
    task_id: str, status_code: int, message: str, rejected: list[str] | None = None
) -> JSONResponse:
    evidence = WriteFilesResponse(
        task_id=task_id,
        success=False,
        files_written=[],
        files_rejected=rejected or [],
        message=message,
    )
    return JSONResponse(status_code=status_code, content=evidence.model_dump())


def _valid_tool_api_key(provided_key: str | None) -> bool:
    configured_key = os.getenv("N8N_TOOL_API_KEY")
    return bool(
        configured_key
        and provided_key
        and compare_digest(provided_key.encode("utf-8"), configured_key.encode("utf-8"))
    )


def _validate_write_path(path: str, agent: str) -> None:
    parts = path.split("/")
    if (
        len(parts) < 2
        or parts[0] != agent
        or "\\" in path
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
        or any(part in {"node_modules", "__pycache__", "venv", "env"} for part in parts)
    ):
        raise WorkspacePathError(f"Path must be a regular file under {agent}/")
    resolved = workspace_tool._resolve(path)
    try:
        resolved.relative_to(workspace_tool.root / agent)
    except ValueError as exc:
        raise WorkspacePathError("Path escapes agent workspace") from exc
    if resolved.is_dir():
        raise WorkspacePathError("Path refers to a directory")


@app.post(
    "/tools/write-files",
    response_model=WriteFilesResponse,
    responses={
        400: {"model": WriteFilesResponse},
        401: {"model": WriteFilesResponse},
        500: {"model": WriteFilesResponse},
    },
)
async def write_files(request: WriteFilesRequest, x_tool_api_key: str | None = Header(default=None)):
    if not _valid_tool_api_key(x_tool_api_key):
        return _write_files_error(request.task_id, 401, "Unauthorized")
    if request.agent not in {"backend", "frontend"}:
        return _write_files_error(
            request.task_id, 400, "Only backend and frontend agents are supported",
            [file.path for file in request.files],
        )

    rejected: list[str] = []
    seen: set[str] = set()
    for file in request.files:
        try:
            _validate_write_path(file.path, request.agent)
            if file.path in seen:
                raise WorkspacePathError("Duplicate file path")
            seen.add(file.path)
        except (WorkspacePathError, OSError):
            rejected.append(file.path)
    if rejected:
        return _write_files_error(request.task_id, 400, "Unsafe file path rejected; no files written", rejected)

    written: list[str] = []
    for file in request.files:
        try:
            _validate_write_path(file.path, request.agent)
            workspace_tool.write_file(file.path, file.content)
        except (WorkspacePathError, OSError):
            evidence = WriteFilesResponse(
                task_id=request.task_id,
                success=False,
                files_written=written,
                files_rejected=[file.path],
                message="File write failed",
            )
            return JSONResponse(status_code=500, content=evidence.model_dump())
        written.append(file.path)
    return WriteFilesResponse(
        task_id=request.task_id,
        success=True,
        files_written=written,
        files_rejected=[],
        message="Files written successfully",
    )


def _run_tests_error(task_id: str, status_code: int, message: str) -> JSONResponse:
    result = RunTestsResponse(
        task_id=task_id, success=False, exit_code=None,
        stdout="", stderr="", message=message,
    )
    return JSONResponse(status_code=status_code, content=result.model_dump())


@app.post(
    "/tools/run-tests",
    response_model=RunTestsResponse,
    responses={
        400: {"model": RunTestsResponse},
        401: {"model": RunTestsResponse},
        500: {"model": RunTestsResponse},
    },
)
async def run_tests(request: RunTestsRequest, x_tool_api_key: str | None = Header(default=None)):
    if not _valid_tool_api_key(x_tool_api_key):
        return _run_tests_error(request.task_id, 401, "Unauthorized")
    if request.agent != "backend":
        return _run_tests_error(request.task_id, 400, "Only the backend agent is supported")

    try:
        backend_root = workspace_tool._resolve("backend")
        tests_root = workspace_tool._resolve("backend/tests")
        tests_root.relative_to(backend_root)
        if backend_root != workspace_tool.root / "backend" or tests_root != backend_root / "tests":
            raise WorkspacePathError("Backend test path resolves through a symlink")
        if not backend_root.is_dir() or not tests_root.is_dir():
            return _run_tests_error(request.task_id, 400, "Backend tests directory not found")
    except (WorkspacePathError, ValueError, OSError):
        return _run_tests_error(request.task_id, 400, "Backend tests directory is unsafe")

    runner = BackendTestRunner(workspace_tool.root)
    venv_python = backend_root / ".venv" / "bin" / "python"
    try:
        evidence = await runner.run(
            python_executable=venv_python if venv_python.is_file() else None,
        )
    except (OSError, ValueError) as exc:
        logger.warning("TEST RUNNER: execution error: %s", type(exc).__name__)
        return _run_tests_error(request.task_id, 500, "Test execution failed")

    return RunTestsResponse(
        task_id=request.task_id,
        success=evidence.passed,
        exit_code=evidence.exit_code,
        stdout=evidence.stdout,
        stderr=evidence.stderr,
        message=(
            evidence.summary if evidence.exit_code == 124
            else "Tests passed" if evidence.passed else "Tests failed"
        ),
    )
