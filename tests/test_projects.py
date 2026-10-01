import asyncio
import json
from pathlib import Path

import pytest

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.models.task import (
    AgentName,
    DependencyEvidence,
    FailureCategory,
    RunEvidence,
    Task,
    TaskStatus,
    TaskTestOwnership,
)
from app.orchestrator.workflow import Orchestrator
from app.services.llm import GroqLLMService, LLMService
from app.services.failure_classifier import FailureClassifier
from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspaceTool


class PlanningLLM(LLMService):
    def __init__(self, tasks: list[dict] | None = None) -> None:
        self.tasks = tasks or []

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        return {"tasks": self.tasks}


class TaskAwareCodingLLM(LLMService):
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        request = json.loads(user_prompt)
        self.requests.append(request)
        task_id = request["current_task"]["id"]
        feature = "auth" if task_id == "TASK-001" else "students"
        if request["attempt_number"]:
            path = f"backend/app/{feature}.py"
            return repair_data(
                [
                    {
                        "path": path,
                        "content": (
                            f"TASK_ID = '{task_id}'\n"
                            f"REPAIR_ATTEMPT = {request['attempt_number']}\n"
                        ),
                    }
                ]
            )
        return {
            "task_test_paths": [f"backend/tests/test_{feature}.py"],
            "files": [
                {
                    "path": f"backend/app/{feature}.py",
                    "content": f"TASK_ID = '{task_id}'\n",
                },
                {
                    "path": f"backend/tests/test_{feature}.py",
                    "content": f"def test_{feature}():\n    assert '{task_id}'\n",
                },
            ],
        }


class StaticCodingLLM(LLMService):
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.calls += 1
        return self.response


class SequenceCodingLLM(LLMService):
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.pending_frontend_files: dict[str, dict] = {}

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        request = json.loads(user_prompt)
        self.requests.append(request)
        if request.get("operation") in {"frontend_plan", "frontend_plan_correction"}:
            response = self.responses.pop(0)
            if "files" not in response:
                return response
            self.pending_frontend_files = {item["path"]: item for item in response["files"]}
            existing = set(request.get("existing_frontend_paths", []))
            paths = list(self.pending_frontend_files)
            return {
                "summary": response["summary"],
                "files_to_create": [path for path in paths if path not in existing],
                "files_to_modify": [path for path in paths if path in existing],
                "dependencies": response["dependencies"],
                "task_test_paths": response["task_test_paths"],
            }
        if request.get("operation") in {"frontend_file", "frontend_file_correction"}:
            return self.pending_frontend_files[request["target_path"]]
        return self.responses.pop(0)


class StubTestRunner:
    def __init__(self, passed: bool, results=None, events=None) -> None:
        self.passed = passed
        self.results = list(results or [])
        self.events = events
        self.calls: list[list[str] | None] = []

    async def run(self, test_paths=None, python_executable=None) -> RunEvidence:
        if self.events is not None:
            self.events.append("pytest")
        self.calls.append(test_paths)
        if self.results:
            return self.results.pop(0)
        return RunEvidence(
            passed=self.passed,
            exit_code=0 if self.passed else 1,
            summary="1 passed" if self.passed else "1 failed",
        )


class StubDependencyManager:
    def __init__(self, events=None, evidence=None, resolution_evidence=None) -> None:
        self.events = events
        self.calls = 0
        self.evidence = evidence or DependencyEvidence(
            success=True,
            requested=["pytest"],
            approved=["pytest"],
            already_installed=["pytest"],
            summary="ready",
        )
        self.resolution_evidence = resolution_evidence or self.evidence
        self.resolution_calls = 0

    async def prepare(self) -> DependencyEvidence:
        self.calls += 1
        if self.events is not None:
            self.events.append("dependencies")
        return self.evidence

    async def resolve_compatibility(self, failure_output: str) -> DependencyEvidence:
        self.resolution_calls += 1
        return self.resolution_evidence


class FrontendCodingLLM(LLMService):
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        request = json.loads(user_prompt)
        self.requests.append(request)
        task_id = request["task"]["id"]
        feature = "auth" if task_id == "TASK-001" else "dashboard"
        source = f"frontend/src/{feature}.jsx"
        test = f"frontend/src/{feature}.test.jsx"
        if request["operation"] in {"frontend_plan", "frontend_plan_correction"}:
            return {
                "summary": f"Implemented {feature}",
                "files_to_create": [source, test],
                "files_to_modify": [],
                "dependencies": ["react", "vitest", "@testing-library/react"],
                "task_test_paths": [test],
            }
        if request["target_path"] == source:
            return {"path": source, "content": f"export const TASK_ID = '{task_id}';\n"}
        return {"path": test, "content": f"test('{feature}', () => expect('{task_id}').toBeTruthy());\n"}


class StubFrontendDependencyManager:
    def __init__(self, success: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.success = success

    async def prepare(self, requested: list[str]) -> DependencyEvidence:
        self.calls.append(requested)
        return DependencyEvidence(
            success=self.success,
            requested=requested,
            approved=requested if self.success else [],
            summary="ready" if self.success else "dependency failed",
        )


class StubAPIContractCollector:
    def __init__(self, contract=None) -> None:
        self.contract = contract or {
            "routes": [{"method": "POST", "path": "/auth/login"}],
            "schemas": {"LoginRequest": ["email", "password"]},
        }

    def collect(self):
        return self.contract


def frontend_agent(
    tmp_path: Path,
    llm: LLMService | None = None,
    runner: StubTestRunner | None = None,
    dependencies: StubFrontendDependencyManager | None = None,
) -> FrontendDeveloperAgent:
    return FrontendDeveloperAgent(
        llm_service=llm or FrontendCodingLLM(),
        workspace=WorkspaceTool(tmp_path),
        test_runner=runner or StubTestRunner(True),
        dependency_manager=dependencies or StubFrontendDependencyManager(),
        api_contract_collector=StubAPIContractCollector(),
    )


def task_data(
    task_id: str,
    agent: str = "backend",
    depends_on: list[str] | None = None,
) -> dict:
    title = (
        "Create authentication API"
        if task_id == "TASK-001"
        else "Create core backend domain APIs"
    )
    return {
        "id": task_id,
        "title": title,
        "description": f"Implement task-specific behavior for {task_id}.",
        "assigned_agent": agent,
        "priority": "high",
        "status": "assigned",
        "depends_on": depends_on or [],
    }


def repair_data(
    files: list[dict],
    *,
    category: str = "RUNTIME_ERROR",
    root_cause: str = "The current implementation does not satisfy the failing test.",
    strategy: str = "Patch the relevant implementation and rerun the same tests.",
    test_change_justification: str | None = None,
) -> dict:
    return {
        "failure_category": category,
        "root_cause": root_cause,
        "files_to_modify": [item["path"] for item in files],
        "repair_strategy": strategy,
        "files": files,
        "task_test_paths": [],
        "test_change_justification": test_change_justification,
    }


def backend_agent(
    tmp_path: Path,
    passed: bool = True,
    llm: LLMService | None = None,
) -> BackendDeveloperAgent:
    return BackendDeveloperAgent(
        llm_service=llm or TaskAwareCodingLLM(),
        workspace=WorkspaceTool(tmp_path),
        test_runner=StubTestRunner(passed),
        dependency_manager=StubDependencyManager(),
    )


def test_manager_normalizes_llm_task_ids_and_dependencies() -> None:
    raw_tasks = [
        {**task_data("TASK-001"), "id": "B1"},
        {**task_data("TASK-002"), "id": "F1", "depends_on": ["B1"]},
    ]

    tasks = asyncio.run(
        ManagerAgent(PlanningLLM(raw_tasks)).analyze_requirement("Build a project")
    )

    assert [task.id for task in tasks] == ["TASK-001", "TASK-002"]
    assert tasks[1].depends_on == ["TASK-001"]


def test_manager_adds_dependency_for_required_authentication_capability() -> None:
    auth = {
        **task_data("TASK-001"),
        "title": "Create authentication API",
        "provides": ["Authentication", "JWT Tokens"],
    }
    dashboard = {
        **task_data("TASK-002"),
        "title": "Create dashboard API",
        "description": "Return records for the authenticated user.",
        "requires": ["authentication"],
    }

    tasks = asyncio.run(
        ManagerAgent(PlanningLLM([auth, dashboard])).analyze_requirement("Build API")
    )

    assert tasks[0].provides == ["authentication", "jwt_tokens"]
    assert tasks[1].requires == ["authentication"]
    assert tasks[1].depends_on == ["TASK-001"]


def test_manager_rejects_semantic_dependency_cycles() -> None:
    first = {
        **task_data("TASK-001", depends_on=["TASK-002"]),
        "provides": ["first_capability"],
    }
    second = {
        **task_data("TASK-002", depends_on=["TASK-001"]),
        "provides": ["second_capability"],
    }

    with pytest.raises(ValueError, match="Circular dependency"):
        asyncio.run(
            ManagerAgent(PlanningLLM([first, second])).analyze_requirement("Build API")
        )


def test_string_only_files_are_corrected_without_consuming_repair_budget(
    tmp_path: Path,
) -> None:
    invalid = {
        "files": ["backend/app/auth.py", "backend/tests/test_auth.py"],
        "task_test_paths": ["backend/tests/test_auth.py"],
    }
    valid = {
        "files": [
            {"path": "backend/app/auth.py", "content": "AUTH = True\n"},
            {
                "path": "backend/tests/test_auth.py",
                "content": "def test_auth(): assert True\n",
            },
        ],
        "task_test_paths": ["backend/tests/test_auth.py"],
    }
    llm = SequenceCodingLLM([invalid, valid])

    result = asyncio.run(
        backend_agent(tmp_path, llm=llm).process(
            Task.model_validate(task_data("TASK-001"))
        )
    )

    assert result.status == TaskStatus.REVIEW
    assert result.generation_attempts == 1
    assert result.format_correction_attempts == 1
    assert result.attempts == 0
    correction = llm.requests[1]
    assert correction["operation"] == "backend_format_correction"
    assert correction["required_schema"]["properties"]["files"]
    assert "validation_errors" in correction


def test_format_correction_is_bounded_and_valid_output_needs_no_correction(
    tmp_path: Path,
) -> None:
    invalid = {
        "files": ["backend/app/auth.py"],
        "task_test_paths": ["backend/tests/test_auth.py"],
    }
    invalid_llm = SequenceCodingLLM([invalid, invalid, invalid])
    failed = asyncio.run(
        backend_agent(tmp_path / "invalid", llm=invalid_llm).process(
            Task.model_validate(task_data("TASK-001"))
        )
    )

    valid_llm = TaskAwareCodingLLM()
    passed = asyncio.run(
        backend_agent(tmp_path / "valid", llm=valid_llm).process(
            Task.model_validate(task_data("TASK-001"))
        )
    )

    assert failed.status == TaskStatus.FAILED
    assert failed.format_correction_attempts == 2
    assert failed.attempts == 0
    assert len(invalid_llm.requests) == 3
    assert passed.format_correction_attempts == 0
    assert len(valid_llm.requests) == 1


def test_dependency_failure_recovery_reruns_same_tests_without_repair_attempt(
    tmp_path: Path,
) -> None:
    dependency_failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout=(
            "passlib trapped error reading bcrypt version; bcrypt backend "
            "compatibility error: password cannot be longer than 72 bytes"
        ),
    )
    passed = RunEvidence(passed=True, exit_code=0, summary="1 passed")
    runner = StubTestRunner(True, results=[dependency_failure, passed])
    resolved = DependencyEvidence(
        success=True,
        requested=["passlib[bcrypt]", "bcrypt"],
        approved=["passlib[bcrypt]==1.7.4", "bcrypt==4.0.1"],
        installed=["passlib[bcrypt]==1.7.4", "bcrypt==4.0.1"],
        summary="resolved",
    )
    dependencies = StubDependencyManager(resolution_evidence=resolved)
    agent = BackendDeveloperAgent(
        TaskAwareCodingLLM(), WorkspaceTool(tmp_path), runner, dependencies
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert FailureClassifier().classify(dependency_failure).category == (
        FailureCategory.DEPENDENCY_FAILURE
    )
    assert result.status == TaskStatus.REVIEW
    assert result.attempts == 0
    assert dependencies.resolution_calls == 1
    assert runner.calls == [
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_auth.py"],
    ]


def test_backend_agent_tracks_current_task_created_and_modified_files(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("backend/app/main.py", "app = 'before'\n")
    workspace.write_file("backend/app/auth.py", "AUTH = True\n")
    response = {
        "task_test_paths": ["backend/tests/test_students.py"],
        "files": [
            {"path": "backend/app/main.py", "content": "app = 'after'\n"},
            {"path": "backend/app/students.py", "content": "STUDENTS = True\n"},
            {
                "path": "backend/tests/test_students.py",
                "content": "def test_students():\n    assert True\n",
            },
        ],
    }
    agent = BackendDeveloperAgent(
        StaticCodingLLM(response), workspace, StubTestRunner(True), StubDependencyManager()
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-002"))))

    assert result.files_created == [
        "backend/app/students.py",
        "backend/tests/test_students.py",
    ]
    assert result.files_modified == ["backend/app/main.py"]
    assert "backend/app/auth.py" not in result.files_changed
    assert result.task_test_paths == ["backend/tests/test_students.py"]


def test_backend_agent_ignores_binary_workspace_artifacts(tmp_path: Path) -> None:
    cache = tmp_path / "backend/__pycache__"
    cache.mkdir(parents=True)
    (cache / "main.pyc").write_bytes(b"\xa7\r\r\n\x00")

    result = asyncio.run(
        backend_agent(tmp_path).process(Task.model_validate(task_data("TASK-001")))
    )

    assert result.status == TaskStatus.REVIEW


def test_backend_agent_stops_after_three_failed_attempts(tmp_path: Path) -> None:
    runner = StubTestRunner(False)
    llm = TaskAwareCodingLLM()
    agent = BackendDeveloperAgent(
        llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.FAILED
    assert result.attempts == 3
    assert len(runner.calls) == 4
    assert len(llm.requests) == 4


def test_dependency_manager_runs_before_pytest(tmp_path: Path) -> None:
    events: list[str] = []
    agent = BackendDeveloperAgent(
        TaskAwareCodingLLM(),
        WorkspaceTool(tmp_path),
        StubTestRunner(True, events=events),
        StubDependencyManager(events=events),
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert events == ["dependencies", "pytest"]


def test_repair_reuses_task_test_and_receives_failure_context(tmp_path: Path) -> None:
    initial = {
        "task_test_paths": ["backend/tests/test_auth.py"],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"},
            {
                "path": "backend/tests/test_auth.py",
                "content": "from backend.app.auth import VALUE\n\ndef test_auth():\n    assert VALUE == 'fixed'\n",
            },
        ],
    }
    repair = repair_data(
        [{"path": "backend/app/auth.py", "content": "VALUE = 'fixed'\n"}],
        root_cause="The source constant does not match the required value.",
    )
    llm = SequenceCodingLLM([initial, repair])
    failed = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="FAILED test_auth.py::test_auth",
        stderr="assert broken == fixed",
    )
    runner = StubTestRunner(
        True,
        results=[failed, RunEvidence(passed=True, exit_code=0, summary="1 passed"),
                 RunEvidence(passed=True, exit_code=0, summary="1 passed")],
    )
    dependencies = StubDependencyManager()
    agent = BackendDeveloperAgent(
        llm, WorkspaceTool(tmp_path), runner, dependencies
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert result.attempts == 1
    assert result.task_test_paths == ["backend/tests/test_auth.py"]
    assert runner.calls == [
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_auth.py"],
    ]
    assert result.files_created == [
        "backend/app/auth.py",
        "backend/tests/test_auth.py",
    ]
    assert llm.requests[0]["attempt_type"] == "initial_generation"
    assert llm.requests[1]["attempt_type"] == "repair_attempt_1"
    assert llm.requests[1]["existing_task_test_paths"] == [
        "backend/tests/test_auth.py"
    ]
    assert llm.requests[1]["pytest_stdout"] == failed.stdout
    assert llm.requests[1]["pytest_stderr"] == failed.stderr
    assert llm.requests[1]["existing_task_tests"]["backend/tests/test_auth.py"]
    assert llm.requests[1]["relevant_existing_file_contents"][
        "backend/app/auth.py"
    ] == "VALUE = 'broken'\n"
    assert llm.requests[1]["previous_attempted_changes"][0]["outcome"] == (
        "task_tests_failed"
    )
    assert llm.requests[1]["dependency_installation"]["success"] is True
    assert dependencies.calls == 2


def test_repair_uses_each_new_failure_and_reruns_the_same_task_tests(
    tmp_path: Path,
) -> None:
    test_path = "backend/tests/test_feature.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/feature.py", "content": "STATE = 'initial'\n"},
            {
                "path": test_path,
                "content": (
                    "def test_endpoint_exists(): pass\n\n"
                    "def test_model_accepts_fields(): pass\n"
                ),
            },
        ],
    }
    endpoint_repair = repair_data(
        [{"path": "backend/app/feature.py", "content": "STATE = 'endpoint-repaired'\n"}],
        category="ROUTING_ERROR",
        root_cause="The endpoint implementation is missing.",
    )
    model_repair = repair_data(
        [{"path": "backend/app/feature.py", "content": "STATE = 'model-repaired'\n"}],
        category="VALIDATION_ERROR",
        root_cause="The model does not accept the tested field.",
    )
    llm = SequenceCodingLLM([initial, endpoint_repair, model_repair])
    endpoint_failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="assert response.status_code == 200\nE assert 404 == 200",
        stderr="",
    )
    model_failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="TypeError: 'field_name' is an invalid keyword argument for Model",
        stderr="model construction failed",
    )
    passed = RunEvidence(passed=True, exit_code=0, summary="2 passed")
    runner = StubTestRunner(
        True,
        results=[endpoint_failure, model_failure, passed, passed],
    )
    dependencies = StubDependencyManager()
    agent = BackendDeveloperAgent(
        llm, WorkspaceTool(tmp_path), runner, dependencies
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert result.attempts == 2
    assert runner.calls == [[test_path], [test_path], [test_path]]
    assert llm.requests[1]["pytest_stdout"] == endpoint_failure.stdout
    assert llm.requests[2]["pytest_stdout"] == model_failure.stdout
    assert llm.requests[2]["pytest_stderr"] == model_failure.stderr
    assert llm.requests[1]["existing_task_tests"][test_path] == initial["files"][1][
        "content"
    ]
    assert llm.requests[2]["relevant_existing_file_contents"][
        "backend/app/feature.py"
    ] == "STATE = 'endpoint-repaired'\n"
    assert len(llm.requests[2]["previous_attempted_changes"]) == 2
    assert set(llm.requests[2]["relevant_existing_file_contents"]) == {
        "backend/app/feature.py",
        "backend/tests/test_feature.py",
    }
    assert dependencies.calls == 3


def test_repair_that_only_changes_tests_is_rejected(tmp_path: Path) -> None:
    test_path = "backend/tests/test_feature.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/feature.py", "content": "VALUE = 'broken'\n"},
            {"path": test_path, "content": "def test_feature(): assert False\n"},
        ],
    }
    test_only_repairs = [
        repair_data(
            [
                {
                    "path": test_path,
                    "content": f"def test_feature(): assert {number}\n",
                }
            ]
        )
        for number in range(1, 4)
    ]
    llm = SequenceCodingLLM([initial, *test_only_repairs])
    agent = BackendDeveloperAgent(
        llm,
        WorkspaceTool(tmp_path),
        StubTestRunner(False),
        StubDependencyManager(),
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.FAILED
    assert result.attempts == 3
    assert len(llm.requests) == 4
    assert result.files_created == [
        "backend/app/feature.py",
        "backend/tests/test_feature.py",
    ]


def test_unchanged_repair_is_not_reported_as_modified_and_attempts_are_bounded(
    tmp_path: Path,
) -> None:
    initial = {
        "task_test_paths": ["backend/tests/test_auth.py"],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"},
            {"path": "backend/tests/test_auth.py", "content": "def test_auth(): pass\n"},
        ],
    }
    unchanged = repair_data(
        [{"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"}]
    )
    llm = SequenceCodingLLM([initial, unchanged, unchanged, unchanged])
    agent = BackendDeveloperAgent(
        llm,
        WorkspaceTool(tmp_path),
        StubTestRunner(False),
        StubDependencyManager(),
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.FAILED
    assert result.attempts == 3
    assert len(llm.requests) == 4
    assert result.files_modified == []


def test_approved_dependency_preparation_does_not_consume_repair_attempt(
    tmp_path: Path,
) -> None:
    evidence = DependencyEvidence(
        success=True,
        requested=["fastapi", "pytest"],
        approved=["fastapi", "pytest"],
        installed=["fastapi"],
        already_installed=["pytest"],
        summary="installed",
    )
    agent = BackendDeveloperAgent(
        TaskAwareCodingLLM(),
        WorkspaceTool(tmp_path),
        StubTestRunner(True),
        StubDependencyManager(evidence=evidence),
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert result.attempts == 0
    assert result.dependencies and result.dependencies.installed == ["fastapi"]


def test_dependency_failure_does_not_consume_repair_attempt(tmp_path: Path) -> None:
    evidence = DependencyEvidence(
        success=False,
        requested=["unknown-package"],
        approved=[],
        summary="Dependency is not approved: unknown-package",
    )
    runner = StubTestRunner(True)
    agent = BackendDeveloperAgent(
        TaskAwareCodingLLM(),
        WorkspaceTool(tmp_path),
        runner,
        StubDependencyManager(evidence=evidence),
    )

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.FAILED
    assert result.attempts == 0
    assert runner.calls == []


def test_task_two_cannot_complete_using_only_task_one_tests(tmp_path: Path) -> None:
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("backend/tests/test_auth.py", "def test_auth():\n    assert True\n")
    llm = StaticCodingLLM(
        {
            "task_test_paths": ["backend/tests/test_auth.py"],
            "files": [
                {"path": "backend/app/students.py", "content": "STUDENTS = True\n"},
                {
                    "path": "backend/tests/test_auth.py",
                    "content": "def test_auth():\n    assert True\n",
                },
            ],
        }
    )

    runner = StubTestRunner(True)
    result = asyncio.run(
        BackendDeveloperAgent(
            llm, workspace, runner, StubDependencyManager()
        ).process(
            Task.model_validate(task_data("TASK-002"))
        )
    )
    reviewed = ManagerAgent(PlanningLLM()).review_result(
        Task.model_validate(task_data("TASK-002")), result
    )

    assert reviewed.status == TaskStatus.FAILED
    assert reviewed.acceptance_criteria_satisfied is False
    assert llm.calls == 2
    assert reviewed.generation_correction_attempts == 1
    assert reviewed.code_repair_attempts == 0
    assert reviewed.failure_stage.value == "IMPLEMENTATION_VALIDATION"
    assert runner.calls == []
    assert reviewed.repair_history == []


def test_sequential_tasks_report_distinct_work_and_dependency_context(
    tmp_path: Path,
) -> None:
    tasks = [task_data("TASK-001"), task_data("TASK-002", depends_on=["TASK-001"])]
    coding_llm = TaskAwareCodingLLM()
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        backend_agent(tmp_path, llm=coding_llm),
        FrontendDeveloperAgent(),
    )

    response = asyncio.run(orchestrator.create_project("Build a school API"))

    first, second = response.results
    assert first.status == TaskStatus.COMPLETED
    assert second.status == TaskStatus.COMPLETED
    assert set(first.files_changed).isdisjoint(second.files_changed)
    assert first.task_test_paths == ["backend/tests/test_auth.py"]
    assert second.task_test_paths == ["backend/tests/test_students.py"]
    assert coding_llm.requests[1]["current_task"]["id"] == "TASK-002"
    assert coding_llm.requests[1]["completed_dependencies"][0]["task_id"] == "TASK-001"
    assert "backend/app/auth.py" in coding_llm.requests[1][
        "existing_workspace_file_list"
    ]
    assert (
        coding_llm.requests[1]["relevant_existing_file_contents"][
            "backend/app/auth.py"
        ]
        == "TASK_ID = 'TASK-001'\n"
    )


def test_completed_task_tests_become_owned_regressions_and_future_tests_are_excluded(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file(
        "backend/tests/test_future.py",
        "def test_future_behavior():\n    assert False\n",
    )
    tasks = [task_data("TASK-001"), task_data("TASK-002", depends_on=["TASK-001"])]
    runner = StubTestRunner(True)
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        BackendDeveloperAgent(
            TaskAwareCodingLLM(),
            workspace,
            runner,
            StubDependencyManager(),
        ),
        FrontendDeveloperAgent(),
    )

    response = asyncio.run(orchestrator.create_project("Build an API"))

    first, second = response.results
    assert first.status == TaskStatus.COMPLETED
    assert first.attempts == 0
    assert first.regression_test_paths == []
    assert second.status == TaskStatus.COMPLETED
    assert second.regression_test_paths == ["backend/tests/test_auth.py"]
    assert runner.calls == [
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_students.py"],
        ["backend/tests/test_auth.py"],
    ]
    assert "backend/tests/test_future.py" not in {
        path for call in runner.calls for path in call or []
    }
    assert orchestrator.backend_test_ownership["TASK-001"].completion_status == (
        TaskStatus.COMPLETED
    )


def test_later_task_cannot_break_completed_tests_and_failed_tests_are_not_registered(
    tmp_path: Path,
) -> None:
    tasks = [
        task_data("TASK-001"),
        task_data("TASK-002", depends_on=["TASK-001"]),
        task_data("TASK-003", depends_on=["TASK-002"]),
    ]
    passed = RunEvidence(passed=True, exit_code=0, summary="1 passed")
    regression_failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="completed TASK-001 behavior regressed",
    )
    runner = StubTestRunner(
        True,
        results=[
            passed,
            passed,
            regression_failure,
            passed,
            regression_failure,
            passed,
            regression_failure,
            passed,
            regression_failure,
        ],
    )
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        BackendDeveloperAgent(
            TaskAwareCodingLLM(),
            WorkspaceTool(tmp_path),
            runner,
            StubDependencyManager(),
        ),
        FrontendDeveloperAgent(),
    )

    response = asyncio.run(orchestrator.create_project("Build an API"))

    assert response.tasks[0].status == TaskStatus.COMPLETED
    assert response.tasks[1].status == TaskStatus.FAILED
    assert response.tasks[2].status == TaskStatus.BLOCKED
    assert response.results[1].attempts == 3
    assert runner.calls == [
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_students.py"],
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_students.py"],
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_students.py"],
        ["backend/tests/test_auth.py"],
        ["backend/tests/test_students.py"],
        ["backend/tests/test_auth.py"],
    ]
    assert orchestrator.backend_test_ownership["TASK-002"].completion_status == (
        TaskStatus.FAILED
    )
    assert orchestrator._completed_backend_regression_paths() == [
        "backend/tests/test_auth.py"
    ]


def test_review_ownership_is_not_in_completed_regression_set(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM()),
        backend_agent(tmp_path),
        FrontendDeveloperAgent(),
    )
    orchestrator.backend_test_ownership = {
        "TASK-001": TaskTestOwnership(
            task_id="TASK-001",
            test_paths=["backend/tests/test_review.py"],
            completion_status=TaskStatus.REVIEW,
        )
    }

    assert orchestrator._completed_backend_regression_paths() == []


def test_local_fallback_builds_and_verifies_distinct_backend_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    llm = GroqLLMService()
    workspace = WorkspaceTool(tmp_path)
    orchestrator = Orchestrator(
        ManagerAgent(llm),
        BackendDeveloperAgent(
            llm,
            workspace,
            BackendTestRunner(tmp_path),
            StubDependencyManager(),
        ),
        frontend_agent(tmp_path),
    )

    response = asyncio.run(
        orchestrator.create_project(
            "Build a school management system with authentication and student dashboard"
        )
    )

    first, second = response.results[:2]
    assert first.status == TaskStatus.COMPLETED
    assert first.task_test_paths == ["backend/tests/test_auth.py"]
    assert second.status == TaskStatus.COMPLETED
    assert second.task_test_paths == ["backend/tests/test_students.py"]
    assert "backend/app/routes/students.py" in second.files_created
    assert "backend/app/main.py" in second.files_modified
    assert first.tests and first.tests.passed
    assert second.task_tests and second.task_tests.passed
    assert second.tests and second.tests.passed


def test_failed_dependency_keeps_child_blocked(tmp_path: Path) -> None:
    tasks = [task_data("TASK-001"), task_data("TASK-002", depends_on=["TASK-001"])]
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        backend_agent(tmp_path, passed=False),
        FrontendDeveloperAgent(),
    )

    response = asyncio.run(orchestrator.create_project("Build an API"))

    assert response.tasks[0].status == TaskStatus.FAILED
    assert response.tasks[1].status == TaskStatus.BLOCKED
    assert [result.task_id for result in response.results] == ["TASK-001"]


def test_frontend_agent_writes_tests_and_completes(tmp_path: Path) -> None:
    tasks = [task_data("TASK-001", agent="frontend")]
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        backend_agent(tmp_path),
        frontend_agent(tmp_path),
    )

    response = asyncio.run(orchestrator.create_project("Build a screen"))

    assert response.tasks[0].status == TaskStatus.COMPLETED
    assert response.results[0].agent == AgentName.FRONTEND
    assert response.results[0].files_changed == [
        "frontend/src/auth.jsx",
        "frontend/src/auth.test.jsx",
    ]
    assert response.results[0].task_tests
    assert response.results[0].completion_gate
    assert response.results[0].completion_gate.passed


def test_dependency_cycle_is_rejected(tmp_path: Path) -> None:
    tasks = [
        task_data("TASK-001", depends_on=["TASK-002"]),
        task_data("TASK-002", depends_on=["TASK-001"]),
    ]
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        backend_agent(tmp_path),
        FrontendDeveloperAgent(),
    )

    with pytest.raises(ValueError, match="Circular dependency"):
        asyncio.run(orchestrator.create_project("Build an API"))


def _database_task() -> Task:
    return Task.model_validate(
        {
            "id": "TASK-001",
            "title": "Design Database Schema and Setup ORM",
            "description": "Create database models, schemas, and ORM relationships.",
            "assigned_agent": "backend",
            "priority": "high",
            "status": "assigned",
            "depends_on": [],
            "provides": ["database_schema"],
            "requires": [],
        }
    )


def _database_implementation() -> dict:
    return {
        "task_test_paths": ["backend/tests/test_database_schema.py"],
        "files": [
            {
                "path": "backend/app/models.py",
                "content": "class User:\n    pass\n",
            },
            {
                "path": "backend/app/schemas.py",
                "content": "class UserSchema:\n    pass\n",
            },
            {
                "path": "backend/tests/test_database_schema.py",
                "content": "def test_database_schema():\n    assert True\n",
            },
        ],
    }


def test_non_routing_backend_task_completes_with_zero_repairs(
    tmp_path: Path,
) -> None:
    task = _database_task()
    agent = BackendDeveloperAgent(
        StaticCodingLLM(_database_implementation()),
        WorkspaceTool(tmp_path),
        StubTestRunner(True),
        StubDependencyManager(),
    )

    result = asyncio.run(agent.process(task))
    reviewed = ManagerAgent(PlanningLLM()).review_result(task, result)

    assert reviewed.status == TaskStatus.COMPLETED
    assert reviewed.attempts == 0
    assert reviewed.acceptance_criteria_satisfied is True
    route_result = next(
        item
        for item in reviewed.acceptance_results
        if "FastAPI application" in item.criterion
    )
    assert route_result.applicable is False
    assert route_result.satisfied is True
    assert reviewed.completion_gate and reviewed.completion_gate.passed
    assert reviewed.rejection_reason is None


def test_task_test_tracking_is_not_limited_by_prompt_snapshot(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceTool(tmp_path)
    for index in range(90):
        workspace.write_file(
            f"backend/app/context_{index:03d}.py",
            "VALUE = " + repr("x" * 300) + "\n",
        )
    task = _database_task()
    agent = BackendDeveloperAgent(
        StaticCodingLLM(_database_implementation()),
        workspace,
        StubTestRunner(True),
        StubDependencyManager(),
    )

    result = asyncio.run(agent.process(task))

    assert "backend/tests/test_database_schema.py" in result.files_created
    assert "backend/app/models.py" in result.files_created
    assert "backend/app/schemas.py" in result.files_created


def test_manager_uses_passing_task_test_evidence_not_changed_file_membership(
    tmp_path: Path,
) -> None:
    task = _database_task()
    agent = BackendDeveloperAgent(
        StaticCodingLLM(_database_implementation()),
        WorkspaceTool(tmp_path),
        StubTestRunner(True),
        StubDependencyManager(),
    )
    result = asyncio.run(agent.process(task))
    result.files_created.remove("backend/tests/test_database_schema.py")
    result.files_changed.remove("backend/tests/test_database_schema.py")

    reviewed = ManagerAgent(PlanningLLM()).review_result(task, result)

    assert reviewed.status == TaskStatus.COMPLETED
    assert reviewed.task_test_paths == ["backend/tests/test_database_schema.py"]
    assert reviewed.task_tests and reviewed.task_tests.passed


def test_missing_required_acceptance_evidence_has_rejection_reason(
    tmp_path: Path,
) -> None:
    task = _database_task()
    agent = BackendDeveloperAgent(
        StaticCodingLLM(_database_implementation()),
        WorkspaceTool(tmp_path),
        StubTestRunner(True),
        StubDependencyManager(),
    )
    result = asyncio.run(agent.process(task))
    result.acceptance_results[0].satisfied = False

    reviewed = ManagerAgent(PlanningLLM()).review_result(task, result)

    assert reviewed.status == TaskStatus.FAILED
    assert reviewed.rejection_reason == "required_acceptance_criterion_unsatisfied"
    assert reviewed.completion_gate
    assert reviewed.completion_gate.rejection_reason == reviewed.rejection_reason
    assert reviewed.summary.endswith(reviewed.rejection_reason)


def test_orm_traceback_collects_imported_source_and_requires_structured_diagnosis(
    tmp_path: Path,
) -> None:
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("backend/app/models.py", "RELATIONSHIP = 'broken'\n")
    test_path = "backend/tests/test_auth_rbac.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/auth.py", "content": "AUTH = True\n"},
            {
                "path": test_path,
                "content": (
                    "from backend.app import models\n\n"
                    "def test_relationship():\n"
                    "    assert models.RELATIONSHIP == 'fixed'\n"
                ),
            },
        ],
    }
    repair = repair_data(
        [{"path": "backend/app/models.py", "content": "RELATIONSHIP = 'fixed'\n"}],
        category="ORM_CONFIGURATION",
        root_cause="The mapped relationship has no matching foreign-key configuration.",
        strategy="Correct the relevant ORM mapping and preserve the existing test.",
    )
    llm = SequenceCodingLLM([initial, repair])
    failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout=(
            'File "/project/workspace/backend/app/models.py", line 12, in configure\n'
            "sqlalchemy.exc.NoForeignKeysError: Could not determine join condition"
        ),
        stderr="relationship configuration failed",
    )

    class ObservingRunner(StubTestRunner):
        async def run(self, test_paths=None, python_executable=None) -> RunEvidence:
            if self.calls:
                assert workspace.read_file("backend/app/models.py") == (
                    "RELATIONSHIP = 'fixed'\n"
                )
            return await super().run(test_paths, python_executable)

    runner = ObservingRunner(
        True,
        results=[
            failure,
            RunEvidence(passed=True, exit_code=0, summary="1 passed"),
        ],
    )
    task = Task.model_validate({**task_data("TASK-001"), "title": "Setup Authentication API"})
    result = asyncio.run(
        BackendDeveloperAgent(llm, workspace, runner, StubDependencyManager()).process(task)
    )

    assert result.status == TaskStatus.REVIEW
    assert runner.calls == [[test_path], [test_path]]
    repair_prompt = llm.requests[1]
    assert repair_prompt["exact_failing_test_paths"] == [test_path]
    assert repair_prompt["exception_type"] == "sqlalchemy.exc.NoForeignKeysError"
    assert "Could not determine join condition" in repair_prompt["exception_message"]
    assert failure.stdout in repair_prompt["traceback"]
    assert repair_prompt["relevant_source_files"]["backend/app/models.py"] == (
        "RELATIONSHIP = 'broken'\n"
    )
    assert repair_prompt["relevant_test_files"][test_path] == initial["files"][1]["content"]
    assert result.repair_history[0].failure_category.value == "ORM_CONFIGURATION"
    assert result.repair_history[0].files_modified == ["backend/app/models.py"]
    assert result.repair_history[0].failure_signature_after == "PASS"


def test_same_failure_signature_marks_previous_strategy_failed(tmp_path: Path) -> None:
    test_path = "backend/tests/test_feature.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/feature.py", "content": "STATE = 0\n"},
            {"path": test_path, "content": "def test_feature(): assert False\n"},
        ],
    }
    repairs = [
        repair_data(
            [{"path": "backend/app/feature.py", "content": f"STATE = {number}\n"}],
            root_cause="The runtime state remains invalid.",
            strategy=f"Try repair strategy {number}.",
        )
        for number in (1, 2)
    ]
    repeated_failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="RuntimeError: relationship configuration failed",
    )
    runner = StubTestRunner(
        True,
        results=[
            repeated_failure,
            repeated_failure,
            RunEvidence(passed=True, exit_code=0, summary="1 passed"),
        ],
    )
    llm = SequenceCodingLLM([initial, *repairs])

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
        ).process(Task.model_validate(task_data("TASK-001")))
    )

    assert result.status == TaskStatus.REVIEW
    assert llm.requests[2]["previous_strategy_failed_with_same_signature"] is True
    assert llm.requests[2]["pytest_stdout"] == repeated_failure.stdout
    assert (
        llm.requests[2]["previous_attempted_changes"][-1]["strategy_outcome"]
        == "same_failure_signature_strategy_failed"
    )
    assert len(result.repair_history) == 2
    assert (
        result.repair_history[0].failure_signature_before
        == result.repair_history[0].failure_signature_after
    )


def test_repair_requires_root_cause_analysis_before_patch(tmp_path: Path) -> None:
    initial = {
        "task_test_paths": ["backend/tests/test_auth.py"],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"},
            {"path": "backend/tests/test_auth.py", "content": "def test_auth(): pass\n"},
        ],
    }
    malformed_repair = {
        "files": [{"path": "backend/app/auth.py", "content": "VALUE = 'fixed'\n"}],
        "task_test_paths": [],
    }
    llm = SequenceCodingLLM(
        [initial, malformed_repair, malformed_repair, malformed_repair]
    )
    runner = StubTestRunner(False)

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
        ).process(Task.model_validate(task_data("TASK-001")))
    )

    assert result.status == TaskStatus.FAILED
    assert result.attempts == 0
    assert result.format_correction_attempts == 2
    assert runner.calls == [["backend/tests/test_auth.py"]]


def test_generation_validation_correction_precedes_tests_and_code_repair(
    tmp_path: Path,
) -> None:
    test_path = "backend/tests/test_domain.py"
    invalid_generation = {
        "task_test_paths": [test_path],
        "files": [
            {
                "path": test_path,
                "content": "def test_domain():\n    assert True\n",
            }
        ],
    }
    corrected_generation = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/domain.py", "content": "READY = True\n"},
            {
                "path": test_path,
                "content": "def test_domain():\n    assert True\n",
            },
        ],
    }
    llm = SequenceCodingLLM([invalid_generation, corrected_generation])
    runner = StubTestRunner(True)

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
        ).process(Task.model_validate(task_data("TASK-002")))
    )

    assert result.status == TaskStatus.REVIEW
    assert result.generation_attempts == 1
    assert result.generation_correction_attempts == 1
    assert result.provider_fallback_count == 0
    assert result.code_repair_attempts == 0
    assert result.attempts == 0
    assert runner.calls == [[test_path]]
    correction = llm.requests[1]
    assert correction["attempt_type"] == "generation_correction"
    assert "no source changes" in correction["generation_validation_error"]
    assert correction["traceback"] == ""
    assert "required_schema" in correction
    assert "traceback-relevant" not in correction[
        "generation_correction_instructions"
    ]


def test_repair_declared_candidate_superset_allows_safe_partial_patch(
    tmp_path: Path,
) -> None:
    test_path = "backend/tests/test_auth.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"},
            {"path": test_path, "content": "def test_auth(): assert False\n"},
        ],
    }
    repair = repair_data(
        [{"path": "backend/app/auth.py", "content": "VALUE = 'fixed'\n"}],
    )
    repair["files_to_modify"].append("backend/app/main.py")
    failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="RuntimeError: authentication failed",
    )
    llm = SequenceCodingLLM([initial, repair])
    runner = StubTestRunner(
        True,
        results=[
            failure,
            RunEvidence(passed=True, exit_code=0, summary="1 passed"),
        ],
    )

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
        ).process(Task.model_validate(task_data("TASK-001")))
    )

    assert result.status == TaskStatus.REVIEW
    assert result.code_repair_attempts == 1
    assert runner.calls == [[test_path], [test_path]]


def test_rejected_repair_preserves_original_pytest_failure_for_next_attempt(
    tmp_path: Path,
) -> None:
    test_path = "backend/tests/test_auth.py"
    initial = {
        "task_test_paths": [test_path],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"},
            {"path": test_path, "content": "def test_auth(): assert False\n"},
        ],
    }
    undeclared_patch = repair_data(
        [{"path": "backend/app/auth.py", "content": "VALUE = 'wrong'\n"}],
    )
    undeclared_patch["files_to_modify"] = ["backend/app/main.py"]
    valid_patch = repair_data(
        [{"path": "backend/app/auth.py", "content": "VALUE = 'fixed'\n"}],
    )
    failure = RunEvidence(
        passed=False,
        exit_code=1,
        summary="1 failed",
        stdout="RuntimeError: original pytest failure",
        stderr="original traceback",
    )
    llm = SequenceCodingLLM([initial, undeclared_patch, valid_patch])
    runner = StubTestRunner(
        True,
        results=[
            failure,
            RunEvidence(passed=True, exit_code=0, summary="1 passed"),
        ],
    )

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, WorkspaceTool(tmp_path), runner, StubDependencyManager()
        ).process(Task.model_validate(task_data("TASK-001")))
    )

    assert result.status == TaskStatus.REVIEW
    assert llm.requests[2]["pytest_stdout"] == failure.stdout
    assert llm.requests[2]["pytest_stderr"] == failure.stderr
    previous = llm.requests[2]["previous_attempted_changes"][-1]
    assert "undeclared patch files" in previous["repair_validation_error"]
