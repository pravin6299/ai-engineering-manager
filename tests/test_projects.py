import asyncio
import json
from pathlib import Path

import pytest

from app.agents.backend import BackendDeveloperAgent
from app.agents.frontend import FrontendDeveloperAgent
from app.agents.manager import ManagerAgent
from app.models.task import AgentName, DependencyEvidence, RunEvidence, Task, TaskStatus
from app.orchestrator.workflow import Orchestrator
from app.services.llm import GroqLLMService, LLMService
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

    async def generate_json(self, system_prompt: str, user_prompt: str) -> dict:
        self.requests.append(json.loads(user_prompt))
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
    def __init__(self, events=None, evidence=None) -> None:
        self.events = events
        self.calls = 0
        self.evidence = evidence or DependencyEvidence(
            success=True,
            requested=["pytest"],
            approved=["pytest"],
            already_installed=["pytest"],
            summary="ready",
        )

    async def prepare(self) -> DependencyEvidence:
        self.calls += 1
        if self.events is not None:
            self.events.append("dependencies")
        return self.evidence


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
    assert len(runner.calls) == 1
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
    assert events == ["dependencies", "pytest", "pytest"]


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
    repair = {
        "task_test_paths": [],
        "files": [
            {"path": "backend/app/auth.py", "content": "VALUE = 'fixed'\n"},
        ],
    }
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
        None,
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
    endpoint_repair = {
        "task_test_paths": [],
        "files": [
            {"path": "backend/app/feature.py", "content": "STATE = 'endpoint-repaired'\n"}
        ],
    }
    model_repair = {
        "task_test_paths": [],
        "files": [
            {"path": "backend/app/feature.py", "content": "STATE = 'model-repaired'\n"}
        ],
    }
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
    assert runner.calls == [[test_path], [test_path], [test_path], None]
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
        {
            "task_test_paths": [test_path],
            "files": [
                {
                    "path": test_path,
                    "content": f"def test_feature(): assert {number}\n",
                }
            ],
        }
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
    unchanged = {
        "task_test_paths": [],
        "files": [{"path": "backend/app/auth.py", "content": "VALUE = 'broken'\n"}],
    }
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

    result = asyncio.run(
        BackendDeveloperAgent(
            llm, workspace, StubTestRunner(True), StubDependencyManager()
        ).process(
            Task.model_validate(task_data("TASK-002"))
        )
    )
    reviewed = ManagerAgent(PlanningLLM()).review_result(
        Task.model_validate(task_data("TASK-002")), result
    )

    assert reviewed.status == TaskStatus.FAILED
    assert reviewed.acceptance_criteria_satisfied is False
    assert llm.calls == 4


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
        FrontendDeveloperAgent(),
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


def test_frontend_agent_remains_proposal_only(tmp_path: Path) -> None:
    tasks = [task_data("TASK-001", agent="frontend")]
    orchestrator = Orchestrator(
        ManagerAgent(PlanningLLM(tasks)),
        backend_agent(tmp_path),
        FrontendDeveloperAgent(),
    )

    response = asyncio.run(orchestrator.create_project("Build a screen"))

    assert response.tasks[0].status == TaskStatus.REVIEW
    assert response.results[0].agent == AgentName.FRONTEND
    assert response.results[0].files_changed == []


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
