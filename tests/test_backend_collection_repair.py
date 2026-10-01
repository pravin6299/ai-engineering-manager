import asyncio
from pathlib import Path

from app.agents.backend import BackendDeveloperAgent
from app.models.task import RunEvidence, Task, TaskStatus
from app.tools.workspace import WorkspaceTool
from tests.test_projects import (
    SequenceCodingLLM,
    StubDependencyManager,
    StubTestRunner,
    repair_data,
    task_data,
)


def implementation(test_content):
    return {
        "task_test_paths": ["backend/tests/test_feature.py"],
        "files": [
            {"path": "backend/app/feature.py", "content": "VALUE = 1\n"},
            {"path": "backend/tests/test_feature.py", "content": test_content},
        ],
    }


def test_initial_invalid_test_syntax_is_corrected_before_write(tmp_path: Path):
    invalid = implementation("fn_setup_db():\n    assert True\n")
    corrected = implementation("def test_feature():\n    assert True\n")
    llm = SequenceCodingLLM([invalid, corrected])
    runner = StubTestRunner(True)
    agent = BackendDeveloperAgent(llm, WorkspaceTool(tmp_path), runner, StubDependencyManager())

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert result.generation_correction_attempts == 1
    assert result.code_repair_attempts == 0
    assert "invalid Python syntax" in llm.requests[1]["generation_validation_error"]
    assert "test_feature.py" in llm.requests[1]["generation_validation_error"]
    assert (tmp_path / "backend/tests/test_feature.py").read_text() == corrected["files"][1]["content"]
    assert runner.calls == [["backend/tests/test_feature.py"]]


def test_invalid_task_fixture_enters_bounded_test_repair(tmp_path: Path):
    test_path = "backend/tests/test_feature.py"
    initial = implementation(
        "from backend.app.feature import VALUE\n"
        "def test_feature(missing_db):\n    assert VALUE == 1\n"
    )
    repaired_test = (
        "from backend.app.feature import VALUE\n"
        "def test_feature():\n    assert VALUE == 1\n"
    )
    repair = repair_data(
        [{"path": test_path, "content": repaired_test}],
        category="VALIDATION_ERROR",
        root_cause="The generated test requests a fixture that does not exist.",
        strategy="Remove the invalid fixture argument and preserve the assertion.",
        test_change_justification="Generated test is invalid: pytest reports fixture missing_db not found.",
    )
    llm = SequenceCodingLLM([initial, repair])
    failed = RunEvidence(
        passed=False, exit_code=4, summary="1 error",
        stdout=f"ERROR {test_path}::test_feature\nfixture 'missing_db' not found",
    )
    runner = StubTestRunner(True, results=[failed, RunEvidence(passed=True, exit_code=0, summary="1 passed")])
    agent = BackendDeveloperAgent(llm, WorkspaceTool(tmp_path), runner, StubDependencyManager())

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.REVIEW
    assert result.code_repair_attempts == 1
    assert runner.calls == [[test_path], [test_path]]
    assert llm.requests[1]["pytest_stdout"] == failed.stdout
    assert llm.requests[1]["existing_task_tests"][test_path] == initial["files"][1]["content"]
    assert (tmp_path / test_path).read_text() == repaired_test


def test_test_only_repair_cannot_weaken_valid_assertion(tmp_path: Path):
    test_path = "backend/tests/test_feature.py"
    initial = implementation("def test_feature():\n    assert False\n")
    weakened = repair_data(
        [{"path": test_path, "content": "def test_feature():\n    assert True\n"}],
        test_change_justification="The test is invalid.",
    )
    llm = SequenceCodingLLM([initial, weakened, weakened, weakened])
    failed = RunEvidence(
        passed=False, exit_code=1, summary="1 failed",
        stdout=f"FAILED {test_path}::test_feature\nAssertionError: assert False",
    )
    runner = StubTestRunner(False, results=[failed])
    agent = BackendDeveloperAgent(llm, WorkspaceTool(tmp_path), runner, StubDependencyManager())

    result = asyncio.run(agent.process(Task.model_validate(task_data("TASK-001"))))

    assert result.status == TaskStatus.FAILED
    assert result.code_repair_attempts == 3
    assert (tmp_path / test_path).read_text() == initial["files"][1]["content"]
    assert runner.calls == [[test_path]]
