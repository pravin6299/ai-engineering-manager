import asyncio
from pathlib import Path

import pytest

from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.models.task import RunEvidence, Task, TaskStatus
from app.orchestrator.workflow import Orchestrator
from app.tools.api_contract import BackendAPIContractCollector
from app.tools.frontend_dependency_manager import FrontendDependencyManager
from app.tools.frontend_test_runner import FrontendTestRunner, find_node_toolchain
from app.tools.workspace import WorkspaceTool
from app.services.llm import LLMRouter
from tests.test_llm import MockProvider
from tests.test_projects import (
    PlanningLLM, SequenceCodingLLM, StubAPIContractCollector,
    StubFrontendDependencyManager, StubTestRunner, backend_agent,
    frontend_agent, task_data,
)


@pytest.mark.parametrize("value", [
    "react", "react@18.2.0", "react-router-dom", "vite@5.4.0", "vitest",
    "@vitejs/plugin-react", "@testing-library/react@14.0.0",
    "@testing-library/jest-dom", "@testing-library/user-event", "jsdom",
])
def test_frontend_dependency_allowlist(value, tmp_path):
    assert FrontendDependencyManager(tmp_path)._validate_dependencies([value]) == [value]


@pytest.mark.parametrize("value", [
    "unknown", "https://x/pkg", "git+https://x/repo", "file:../pkg",
    "../pkg", "./pkg", "--registry=x", "react;rm", "react && bad",
    "react$(id)", "react" + chr(96) + "id" + chr(96),
])
def test_frontend_dependency_rejects_unsafe(value, tmp_path):
    with pytest.raises(ValueError, match="not approved|Invalid"):
        FrontendDependencyManager(tmp_path)._validate_dependencies([value])


def test_node_toolchain_uses_matching_nvm_installation(tmp_path, monkeypatch):
    nvm_root = tmp_path / "nvm"
    for version in ("v18.16.0", "v22.12.0"):
        bin_dir = nvm_root / "versions/node" / version / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("node", "npm"):
            executable = bin_dir / name
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
    monkeypatch.setenv("NVM_DIR", str(nvm_root))
    monkeypatch.delenv("NVM_BIN", raising=False)
    monkeypatch.setattr("app.tools.frontend_test_runner.shutil.which", lambda _: None)

    assert find_node_toolchain() == (
        nvm_root / "versions/node/v22.12.0/bin/node",
        nvm_root / "versions/node/v22.12.0/bin/npm",
    )


def test_frontend_npm_install_is_fixed_and_shell_free(tmp_path, monkeypatch):
    manager = FrontendDependencyManager(tmp_path)
    captured = {}
    class Process:
        returncode = 0
        async def communicate(self): return b"ok", b""
    async def fake_exec(*command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        executable = tmp_path / "frontend/node_modules/.bin/vitest"
        executable.parent.mkdir(parents=True)
        executable.write_text("", encoding="utf-8")
        return Process()
    monkeypatch.setattr("app.tools.frontend_dependency_manager.find_node_toolchain", lambda: (Path("/usr/bin/node"), Path("/usr/bin/npm")))
    monkeypatch.setattr("app.tools.frontend_dependency_manager.asyncio.create_subprocess_exec", fake_exec)
    assert asyncio.run(manager.prepare(["react", "vitest"])).success
    assert captured["command"] == (
        "/usr/bin/npm", "install", "--ignore-scripts", "--no-audit",
        "--no-fund", "react", "vitest", "react-dom", "vite",
        "@vitejs/plugin-react", "@testing-library/react", "@testing-library/jest-dom", "jsdom",
    )
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["env"]["PATH"].startswith("/usr/bin")
    assert "jest-dom/vitest" in (tmp_path / "frontend/.agent-vitest.setup.js").read_text()
    assert "environment: 'jsdom'" in (tmp_path / "frontend/.agent-vitest.config.mjs").read_text()


def test_frontend_installs_test_toolchain_for_empty_dependency_list(tmp_path, monkeypatch):
    manager = FrontendDependencyManager(tmp_path)
    commands = []

    class Process:
        returncode = 0
        async def communicate(self):
            return b"installed", b""

    async def fake_exec(*command, **kwargs):
        commands.append(command)
        executable = tmp_path / "frontend/node_modules/.bin/vitest"
        executable.parent.mkdir(parents=True)
        executable.write_text("", encoding="utf-8")
        return Process()

    monkeypatch.setattr("app.tools.frontend_dependency_manager.find_node_toolchain", lambda: (Path("/usr/bin/node"), Path("/usr/bin/npm")))
    monkeypatch.setattr("app.tools.frontend_dependency_manager.asyncio.create_subprocess_exec", fake_exec)

    evidence = asyncio.run(manager.prepare([]))

    assert evidence.success
    assert evidence.requested == []
    assert evidence.approved == list(manager.required_packages)
    assert commands[0][5:] == manager.required_packages


def test_frontend_install_does_not_claim_success_without_vitest(tmp_path, monkeypatch):
    manager = FrontendDependencyManager(tmp_path)

    class Process:
        returncode = 0
        async def communicate(self):
            return b"installed", b""

    async def fake_exec(*command, **kwargs):
        return Process()

    monkeypatch.setattr("app.tools.frontend_dependency_manager.find_node_toolchain", lambda: (Path("/usr/bin/node"), Path("/usr/bin/npm")))
    monkeypatch.setattr("app.tools.frontend_dependency_manager.asyncio.create_subprocess_exec", fake_exec)

    evidence = asyncio.run(manager.prepare([]))

    assert not evidence.success
    assert "Vitest executable is not installed" in evidence.summary


def test_frontend_runner_reports_missing_vitest_without_raising(tmp_path):
    evidence = asyncio.run(FrontendTestRunner(tmp_path).run(["frontend/src/auth.test.jsx"]))
    assert not evidence.passed
    assert evidence.exit_code == 127
    assert "Vitest executable is not installed" in evidence.stderr


def test_frontend_runner_is_fixed_and_shell_free(tmp_path, monkeypatch):
    executable = tmp_path / "frontend/node_modules/.bin/vitest"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    captured = {}
    class Process:
        returncode = 0
        async def communicate(self): return b"Tests 1 passed", b""
    async def fake_exec(*command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Process()
    monkeypatch.setattr("app.tools.frontend_test_runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.tools.frontend_test_runner.find_node_toolchain", lambda: (Path("/opt/nvm/bin/node"), Path("/opt/nvm/bin/npm")))
    result = asyncio.run(FrontendTestRunner(tmp_path).run(["frontend/src/auth.test.jsx"]))
    assert result.passed
    assert captured["command"][0] == str(executable.resolve())
    assert captured["command"][1:] == ("run", "src/auth.test.jsx", "--reporter=default", "--config", ".agent-vitest.config.mjs")
    assert "shell" not in captured["kwargs"]
    assert captured["kwargs"]["env"]["PATH"].startswith("/opt/nvm/bin")


@pytest.mark.parametrize("path", ["../x.test.jsx", "/tmp/x.test.jsx", "backend/x.test.jsx", "frontend/src/app.jsx"])
def test_frontend_runner_rejects_paths(path):
    with pytest.raises(ValueError):
        FrontendTestRunner._validated_targets([path])


def _initial(test_path="frontend/src/auth.test.jsx"):
    return {
        "summary": "auth", "dependencies": ["react", "vitest"],
        "task_test_paths": [test_path],
        "files": [
            {"path": "frontend/src/auth.jsx", "content": "export const state='bad';"},
            {"path": test_path, "content": "test('auth',()=>{});"},
        ],
    }


def test_frontend_generation_context_and_backend_contract(tmp_path):
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("frontend/src/authHelpers.jsx", "export const old=true;\n")
    llm = SequenceCodingLLM([_initial()])
    agent = FrontendDeveloperAgent(
        llm, workspace, StubTestRunner(True), StubFrontendDependencyManager(),
        StubAPIContractCollector(),
    )
    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    request = llm.requests[0]
    assert request["operation"] == "frontend_plan"
    assert "relevant_existing_frontend_contents" not in request
    assert "frontend/src/authHelpers.jsx" in request["existing_frontend_paths"]
    assert llm.requests[1]["related_frontend_contents"]["frontend/src/authHelpers.jsx"]
    assert llm.requests[1]["backend_api_contract"]["routes"][0]["path"] == "/auth/login"
    assert result.status == TaskStatus.REVIEW


def test_frontend_cannot_write_backend(tmp_path):
    invalid = _initial()
    invalid["files"][0]["path"] = "backend/app/main.py"
    result = asyncio.run(frontend_agent(
        tmp_path, llm=SequenceCodingLLM([invalid, invalid])
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.FAILED
    assert not (tmp_path / "backend/app/main.py").exists()
    assert result.code_repair_attempts == 0


@pytest.mark.parametrize("owned_path", [
    "frontend/package.json",
    "frontend/.agent-vitest.config.mjs",
    "frontend/.agent-vitest.setup.js",
])
def test_frontend_cannot_overwrite_dependency_manager_files(tmp_path, owned_path):
    invalid = _initial()
    invalid["files"][0]["path"] = owned_path
    result = asyncio.run(frontend_agent(
        tmp_path, llm=SequenceCodingLLM([invalid, invalid])
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.FAILED
    assert not (tmp_path / owned_path).exists()
def test_frontend_generation_correction_not_code_repair(tmp_path):
    invalid = _initial()
    invalid["files"] = [invalid["files"][1]]
    llm = SequenceCodingLLM([invalid, _initial()])
    result = asyncio.run(frontend_agent(tmp_path, llm=llm).process(
        Task.model_validate(task_data("TASK-001", agent="frontend"))
    ))
    assert result.status == TaskStatus.REVIEW
    assert result.generation_correction_attempts == 1
    assert result.code_repair_attempts == 0
    assert llm.requests[1]["operation"] == "frontend_plan_correction"


def test_frontend_reuses_existing_task_test_after_failed_run(tmp_path):
    workspace = WorkspaceTool(tmp_path)
    test_path = "frontend/src/auth.test.jsx"
    workspace.write_file(test_path, "test('auth',()=>{});")
    response = _initial()
    response["files"] = [response["files"][0]]
    llm = SequenceCodingLLM([response])
    runner = StubTestRunner(True)

    result = asyncio.run(frontend_agent(
        tmp_path, llm=llm, runner=runner
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))

    assert result.status == TaskStatus.REVIEW
    assert result.task_test_paths == [test_path]
    assert runner.calls == [[test_path]]
    assert [request["operation"] for request in llm.requests] == ["frontend_plan", "frontend_file"]


def test_frontend_cannot_claim_completed_regression_as_current_test(tmp_path):
    workspace = WorkspaceTool(tmp_path)
    test_path = "frontend/src/auth.test.jsx"
    workspace.write_file(test_path, "test('auth',()=>{});")
    response = _initial()
    response["files"] = [response["files"][0]]
    llm = SequenceCodingLLM([response, response])
    runner = StubTestRunner(True)

    result = asyncio.run(frontend_agent(
        tmp_path, llm=llm, runner=runner
    ).process(
        Task.model_validate(task_data("TASK-001", agent="frontend")),
        completed_regression_test_paths=[test_path],
    ))

    assert result.status == TaskStatus.FAILED
    assert runner.calls == []
    assert len(llm.requests) == 2


def test_frontend_does_not_reparse_validated_initial_generation(tmp_path, monkeypatch):
    agent = frontend_agent(tmp_path, llm=SequenceCodingLLM([_initial()]))
    async def unexpected_format_correction(*args):
        raise AssertionError("Initial generation must not enter format correction")
    monkeypatch.setattr(agent, "_parse_response", unexpected_format_correction)
    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.REVIEW

def _small_plan():
    return {
        "summary": "auth",
        "files_to_create": ["frontend/src/auth.jsx", "frontend/src/auth.test.jsx"],
        "files_to_modify": [],
        "dependencies": ["react", "vitest"],
        "task_test_paths": ["frontend/src/auth.test.jsx"],
    }


def test_frontend_invalid_shape_and_path_escalate_to_next_provider(tmp_path):
    invalid_shape = {"summary": "incomplete", "files": []}
    invalid_path = _small_plan()
    invalid_path["files_to_create"] = ["backend/app/main.py", "frontend/src/auth.test.jsx"]
    gemini = MockProvider("gemini", [invalid_shape, invalid_path, *_initial()["files"]])
    ollama = MockProvider("ollama", [_small_plan()])
    router = LLMRouter([gemini, ollama])

    result = asyncio.run(frontend_agent(tmp_path, llm=router).process(
        Task.model_validate(task_data("TASK-001", agent="frontend"))
    ))

    assert result.status == TaskStatus.REVIEW
    assert [gemini.calls, ollama.calls] == [4, 1]
    assert result.generation_correction_attempts == 1
    assert result.provider_fallback_count == 1
    assert result.code_repair_attempts == 0
    assert not (tmp_path / "backend/app/main.py").exists()


def test_frontend_falls_through_malformed_ollama_without_cross_provider_format_call(tmp_path):
    no_source = _small_plan()
    no_source["files_to_create"] = ["frontend/src/auth.test.jsx"]
    invalid_path = _small_plan()
    invalid_path["files_to_create"] = ["backend/app/main.py", "frontend/src/auth.test.jsx"]
    malformed = {"summary": "incomplete", "files": []}
    gemini = MockProvider("gemini", [no_source, invalid_path, *_initial()["files"]])
    ollama = MockProvider("ollama", [malformed, malformed])
    groq = MockProvider("groq", [_small_plan()])
    router = LLMRouter([gemini, ollama, groq])

    result = asyncio.run(frontend_agent(tmp_path, llm=router).process(
        Task.model_validate(task_data("TASK-001", agent="frontend"))
    ))

    assert result.status == TaskStatus.REVIEW
    assert [gemini.calls, ollama.calls, groq.calls] == [4, 2, 1]
    assert result.provider_fallback_count == 2
    assert result.code_repair_attempts == 0
    assert not (tmp_path / "backend/app/main.py").exists()


def test_frontend_rejects_unapproved_dependency_before_writing(tmp_path):
    invalid = _initial()
    invalid["dependencies"] = ["react", "axios"]
    llm = SequenceCodingLLM([invalid, invalid])
    dependencies = StubFrontendDependencyManager()
    runner = StubTestRunner(True)

    result = asyncio.run(frontend_agent(
        tmp_path, llm=llm, runner=runner, dependencies=dependencies
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))

    assert result.status == TaskStatus.FAILED
    assert result.generation_correction_attempts == 1
    assert result.code_repair_attempts == 0
    assert dependencies.calls == []
    assert runner.calls == []
    assert not (tmp_path / "frontend/src/auth.jsx").exists()
    assert "axios" in llm.requests[1]["validation_error"]
    assert "react" in llm.requests[0]["approved_dependencies"]


def test_frontend_corrects_unapproved_dependency_before_writing(tmp_path):
    invalid = _initial()
    invalid["dependencies"] = ["react", "axios"]
    llm = SequenceCodingLLM([invalid, _initial()])
    dependencies = StubFrontendDependencyManager()

    result = asyncio.run(frontend_agent(
        tmp_path, llm=llm, dependencies=dependencies
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))

    assert result.status == TaskStatus.REVIEW
    assert result.generation_correction_attempts == 1
    assert result.code_repair_attempts == 0
    assert dependencies.calls == [["react", "vitest"]]


def test_frontend_failure_repairs_and_reruns_same_test(tmp_path):
    repair = {
        "failure_category": "RENDER_ERROR", "root_cause": "Bad render state",
        "files_to_modify": ["frontend/src/auth.jsx"],
        "repair_strategy": "Correct component state",
        "files": [{"path": "frontend/src/auth.jsx", "content": "export const state='good';"}],
        "task_test_paths": [],
    }
    failed = RunEvidence(passed=False, exit_code=1, summary="1 failed", stdout="render failed")
    runner = StubTestRunner(True, results=[failed, RunEvidence(passed=True, exit_code=0, summary="1 passed")])
    result = asyncio.run(frontend_agent(
        tmp_path, llm=SequenceCodingLLM([_initial(), repair]), runner=runner
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.REVIEW
    assert runner.calls == [["frontend/src/auth.test.jsx"], ["frontend/src/auth.test.jsx"]]
    assert result.code_repair_attempts == 1


def test_frontend_failure_classification_reaches_repair_prompt(tmp_path):
    repair = {
        "failure_category": "RENDER_ERROR", "root_cause": "Missing button",
        "files_to_modify": ["frontend/src/auth.jsx"],
        "repair_strategy": "Render the expected button",
        "files": [{"path": "frontend/src/auth.jsx", "content": "export const state=good;"}],
        "task_test_paths": [],
    }
    llm = SequenceCodingLLM([_initial(), repair])
    failed = RunEvidence(passed=False, exit_code=1, summary="1 failed", stdout="Unable to find role=button")
    runner = StubTestRunner(True, results=[failed, RunEvidence(passed=True, exit_code=0, summary="1 passed")])
    asyncio.run(frontend_agent(tmp_path, llm=llm, runner=runner).process(
        Task.model_validate(task_data("TASK-001", agent="frontend"))
    ))
    repair_request = next(request for request in llm.requests if request["operation"] == "frontend_repair")
    assert repair_request["failure_classification"]["category"] == "RENDER_ERROR"


def test_javascript_frontend_rejects_new_typescript(tmp_path):
    invalid = _initial()
    invalid["files"][0]["path"] = "frontend/src/auth.tsx"
    result = asyncio.run(frontend_agent(
        tmp_path, llm=SequenceCodingLLM([invalid, invalid])
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.FAILED
    assert not (tmp_path / "frontend/src/auth.tsx").exists()


def test_frontend_repairs_are_bounded_at_three(tmp_path):
    repairs = [
        {
            "failure_category": "ASSERTION_FAILURE",
            "root_cause": f"Failure {index}",
            "files_to_modify": ["frontend/src/auth.jsx"],
            "repair_strategy": f"Repair {index}",
            "files": [{"path": "frontend/src/auth.jsx", "content": f"export const state={index};"}],
            "task_test_paths": [],
        }
        for index in range(1, 4)
    ]
    failure = RunEvidence(passed=False, exit_code=1, summary="1 failed", stdout="assertion failed")
    runner = StubTestRunner(False, results=[failure, failure, failure, failure])
    result = asyncio.run(frontend_agent(
        tmp_path, llm=SequenceCodingLLM([_initial(), *repairs]), runner=runner
    ).process(Task.model_validate(task_data("TASK-001", agent="frontend"))))
    assert result.status == TaskStatus.FAILED
    assert result.code_repair_attempts == 3
    assert len(result.repair_history) == 3
    assert runner.calls == [["frontend/src/auth.test.jsx"]] * 4


def test_future_frontend_tests_are_excluded(tmp_path):
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("frontend/src/future.test.jsx", "test(future,()=>{throw Error()});")
    runner = StubTestRunner(True)
    result = asyncio.run(frontend_agent(tmp_path, runner=runner).process(
        Task.model_validate(task_data("TASK-001", agent="frontend")),
        completed_regression_test_paths=[],
    ))
    assert result.status == TaskStatus.REVIEW
    assert runner.calls == [["frontend/src/auth.test.jsx"]]
    assert "frontend/src/future.test.jsx" not in result.regression_test_paths


def test_frontend_regressions_and_dependency_unlock(tmp_path):
    tasks = [
        task_data("TASK-001", agent="frontend"),
        task_data("TASK-002", agent="frontend", depends_on=["TASK-001"]),
    ]
    runner = StubTestRunner(True)
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)), backend_agent(tmp_path),
        frontend_agent(tmp_path, runner=runner),
    )
    response = asyncio.run(orchestrator.create_project("Build frontend"))
    assert [item.status for item in response.results] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    assert runner.calls == [
        ["frontend/src/auth.test.jsx"],
        ["frontend/src/dashboard.test.jsx"],
        ["frontend/src/auth.test.jsx"],
    ]


def test_frontend_dependency_failure_blocks_child(tmp_path):
    tasks = [
        task_data("TASK-001", agent="frontend"),
        task_data("TASK-002", agent="frontend", depends_on=["TASK-001"]),
    ]
    response = asyncio.run(Orchestrator(
        ManagerAgent(PlanningLLM(tasks)), backend_agent(tmp_path),
        frontend_agent(tmp_path, dependencies=StubFrontendDependencyManager(False)),
    ).create_project("Build frontend"))
    assert response.tasks[0].status == TaskStatus.FAILED
    assert response.tasks[1].status == TaskStatus.BLOCKED
    assert response.results[0].rejection_reason == "dependency_preparation_failed"
    assert response.results[0].dependencies and not response.results[0].dependencies.success


def test_backend_contract_collector_read_only(tmp_path):
    workspace = WorkspaceTool(tmp_path)
    source = (
        "from fastapi import APIRouter\nfrom pydantic import BaseModel\n"
        "router=APIRouter()\nclass Login(BaseModel):\n    email: str\n"
        "@router.post('/auth/login')\ndef login(payload: Login): return {}\n"
    )
    workspace.write_file("backend/app/auth.py", source)
    contract = BackendAPIContractCollector(workspace).collect()
    assert contract["routes"][0]["path"] == "/auth/login"
    assert contract["schemas"]["Login"] == ["email"]
    assert workspace.read_file("backend/app/auth.py") == source
