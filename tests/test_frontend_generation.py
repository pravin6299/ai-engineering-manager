import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.models.task import FrontendImplementationPlan, RunEvidence, Task, TaskStatus
from app.services.llm import LLMGenerationValidationError, LLMRouter
from app.tools.workspace import WorkspaceTool
from tests.test_frontend import _initial
from tests.test_projects import StubTestRunner, frontend_agent, task_data


def plan():
    return {
        "summary": "Authentication",
        "files_to_create": ["frontend/src/auth.jsx", "frontend/src/auth.test.jsx"],
        "files_to_modify": [],
        "dependencies": ["react", "vitest"],
        "task_test_paths": ["frontend/src/auth.test.jsx"],
    }


class RecordingProvider:
    def __init__(self, name, outcomes):
        self.provider = name
        self.model = "test-model"
        self.outcomes = list(outcomes)
        self.calls = []
        self.last_retry_count = 0

    async def generate_json(self, system_prompt, user_prompt):
        self.calls.append(json.loads(user_prompt))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def run_task(tmp_path, llm, runner=None):
    return asyncio.run(frontend_agent(
        tmp_path, llm=llm, runner=runner or StubTestRunner(True)
    ).process(Task.model_validate(task_data("TASK-003", agent="frontend"))))


def test_plan_has_no_source_contents():
    with pytest.raises(ValidationError):
        FrontendImplementationPlan.model_validate({**plan(), "files": _initial()["files"]})


def test_frontend_generates_files_individually_and_runs_tests_afterward(tmp_path):
    files = _initial()["files"]
    provider = RecordingProvider("gemini", [plan(), *files])
    workspace = WorkspaceTool(tmp_path)
    workspace.write_file("frontend/src/unrelated.jsx", "UNRELATED_SECRET" * 10000)

    class CheckingRunner(StubTestRunner):
        async def run(self, test_paths=None, python_executable=None):
            assert (tmp_path / "frontend/src/auth.jsx").is_file()
            assert (tmp_path / "frontend/src/auth.test.jsx").is_file()
            return await super().run(test_paths, python_executable)

    runner = CheckingRunner(True)
    result = run_task(tmp_path, LLMRouter([provider]), runner)

    assert result.status == TaskStatus.REVIEW
    assert [call["operation"] for call in provider.calls] == [
        "frontend_plan", "frontend_file", "frontend_file"
    ]
    assert [call["target_path"] for call in provider.calls[1:]] == plan()["files_to_create"]
    assert "UNRELATED_SECRET" not in json.dumps(provider.calls)
    assert all(len(json.dumps(call)) < 10000 for call in provider.calls)
    assert runner.calls == [["frontend/src/auth.test.jsx"]]


def test_later_file_failure_keeps_earlier_valid_file_and_skips_repair(tmp_path):
    wrong = {"path": "frontend/src/wrong.test.jsx", "content": "test('wrong',()=>{});"}
    provider = RecordingProvider("gemini", [plan(), _initial()["files"][0], wrong, wrong])
    runner = StubTestRunner(True)

    result = run_task(tmp_path, LLMRouter([provider]), runner)

    assert result.status == TaskStatus.FAILED
    assert (tmp_path / "frontend/src/auth.jsx").is_file()
    assert not (tmp_path / "frontend/src/auth.test.jsx").exists()
    assert result.files_created == ["frontend/src/auth.jsx"]
    assert [call.get("target_path") for call in provider.calls[1:]] == [
        "frontend/src/auth.jsx", "frontend/src/auth.test.jsx", "frontend/src/auth.test.jsx"
    ]
    assert result.code_repair_attempts == 0
    assert runner.calls == []


def test_invalid_file_is_corrected_without_code_repair(tmp_path):
    wrong = {"path": "frontend/src/wrong.test.jsx", "content": "test('wrong',()=>{});"}
    provider = RecordingProvider("gemini", [plan(), _initial()["files"][0], wrong, _initial()["files"][1]])

    result = run_task(tmp_path, LLMRouter([provider]))

    assert result.status == TaskStatus.REVIEW
    assert provider.calls[-1]["operation"] == "frontend_file_correction"
    assert result.generation_correction_attempts == 1
    assert result.code_repair_attempts == 0
    assert "invalid_generation" not in provider.calls[-1]


def test_provider_fallback_retries_only_current_file(tmp_path):
    wrong = {"path": "frontend/src/wrong.jsx", "content": "wrong"}
    gemini = RecordingProvider("gemini", [plan(), wrong, wrong, _initial()["files"][1]])
    ollama = RecordingProvider("ollama", [_initial()["files"][0]])

    result = run_task(tmp_path, LLMRouter([gemini, ollama]))

    assert result.status == TaskStatus.REVIEW
    assert [call.get("target_path") for call in gemini.calls[1:]] == [
        "frontend/src/auth.jsx", "frontend/src/auth.jsx", "frontend/src/auth.test.jsx"
    ]
    assert len(ollama.calls) == 1
    assert ollama.calls[0]["target_path"] == "frontend/src/auth.jsx"
    assert result.provider_fallback_count == 1


def test_ollama_file_context_is_bounded_and_excludes_unrelated_tests(tmp_path):
    agent = frontend_agent(tmp_path)
    task = Task.model_validate(task_data("TASK-003", agent="frontend"))
    snapshot = {
        "frontend/src/unrelated.jsx": "X" * 100000,
        "frontend/src/unrelated.test.jsx": "INVALID_TEST" * 100000,
    }
    system, user = agent._file_prompt(
        task, agent._acceptance_criteria(task), "frontend/src/auth.jsx",
        snapshot, FrontendImplementationPlan.model_validate(plan()), {"routes": [], "schemas": {}},
    )
    assert len((system + user).encode()) < 10000
    assert "INVALID_TEST" not in user
    assert "X" * 2000 not in user


def test_http_413_falls_back_without_retrying_same_payload():
    request = httpx.Request("POST", "https://provider.example/generate")
    error = httpx.HTTPStatusError("too large", request=request, response=httpx.Response(413, request=request))
    gemini = RecordingProvider("gemini", [error])
    ollama = RecordingProvider("ollama", [{"path": "frontend/src/auth.jsx", "content": "ok"}])
    router = LLMRouter([gemini, ollama])

    response = asyncio.run(router.generate_json_with_validation(
        "system", json.dumps({"operation": "frontend_file", "target_path": "frontend/src/auth.jsx"}),
        lambda raw: None, lambda error, raw: ("system", "{}"),
    ))

    assert response["content"] == "ok"
    assert len(gemini.calls) == 1
    assert len(ollama.calls) == 1
    assert "CONTEXT_TOO_LARGE" in router.last_generation_metadata.escalation_reasons


def test_groq_oversized_request_is_rejected_before_network_call():
    groq = RecordingProvider("groq", [])
    router = LLMRouter([groq])
    with pytest.raises(LLMGenerationValidationError, match="CONTEXT_TOO_LARGE"):
        asyncio.run(router.generate_json_with_validation(
            "system", "X" * 13000, lambda raw: None, lambda error, raw: ("system", "{}")
        ))
    assert groq.calls == []
@pytest.mark.parametrize("unsafe_path", [
    "../escape.jsx", "frontend/../../backend/app/main.py", "/tmp/escape.jsx",
])
def test_frontend_plan_rejects_path_traversal_before_writing(tmp_path, unsafe_path):
    invalid = plan()
    invalid["files_to_create"] = [unsafe_path, "frontend/src/auth.test.jsx"]
    provider = RecordingProvider("gemini", [invalid, invalid])

    result = run_task(tmp_path, LLMRouter([provider]))

    assert result.status == TaskStatus.FAILED
    assert result.code_repair_attempts == 0
    assert not (tmp_path / "frontend/src/auth.test.jsx").exists()
    assert not (tmp_path / "backend/app/main.py").exists()


def test_groq_http_413_is_not_retried(tmp_path):
    request = httpx.Request("POST", "https://provider.example/generate")
    error = httpx.HTTPStatusError("too large", request=request, response=httpx.Response(413, request=request))
    groq = RecordingProvider("groq", [error])

    with pytest.raises(LLMGenerationValidationError, match="CONTEXT_TOO_LARGE"):
        asyncio.run(LLMRouter([groq]).generate_json_with_validation(
            "system", "{}", lambda raw: None, lambda error, raw: ("system", "{}")
        ))
    assert len(groq.calls) == 1


def test_frontend_repair_prompt_is_bounded(tmp_path):
    agent = frontend_agent(tmp_path)
    task = Task.model_validate(task_data("TASK-003", agent="frontend"))
    failure = RunEvidence(passed=False, exit_code=1, summary="1 failed", stdout="A" * 100000, stderr="B" * 100000)
    snapshot = {
        "frontend/src/auth.jsx": "S" * 100000,
        "frontend/src/auth.test.jsx": "T" * 100000,
        "frontend/src/unrelated.jsx": "U" * 100000,
    }
    system, user = agent._repair_prompt(
        task, agent._acceptance_criteria(task), snapshot, ["frontend/src/auth.test.jsx"],
        failure, agent.failure_classifier.classify(failure), [], None, 1, {"routes": []},
    )
    assert len((system + user).encode()) < 12000
    assert "U" * 100 not in user

def test_direct_groq_call_rejects_oversized_repair_context():
    groq = RecordingProvider("groq", [])
    router = LLMRouter([groq])
    with pytest.raises(Exception, match="CONTEXT_TOO_LARGE"):
        asyncio.run(router.generate_json("system", "X" * 13000))
    assert groq.calls == []


def test_direct_groq_http_413_is_not_retried():
    request = httpx.Request("POST", "https://provider.example/generate")
    error = httpx.HTTPStatusError("too large", request=request, response=httpx.Response(413, request=request))
    groq = RecordingProvider("groq", [error])
    with pytest.raises(Exception, match="CONTEXT_TOO_LARGE"):
        asyncio.run(LLMRouter([groq]).generate_json("system", "{}"))
    assert len(groq.calls) == 1


def test_file_prompt_includes_prior_planned_source_interface_only(tmp_path):
    agent = frontend_agent(tmp_path)
    task = Task.model_validate(task_data("TASK-003", agent="frontend"))
    planned = FrontendImplementationPlan(
        summary="Login",
        files_to_create=[
            "frontend/src/services/auth.js",
            "frontend/src/pages/Login.jsx",
            "frontend/src/pages/Login.test.jsx",
        ],
        task_test_paths=["frontend/src/pages/Login.test.jsx"],
    )
    snapshot = {
        "frontend/src/services/auth.js": "export async function login() {}",
        "frontend/src/unrelated.jsx": "UNRELATED_SOURCE",
        "frontend/src/unrelated.test.jsx": "UNRELATED_TEST",
    }
    _, user = agent._file_prompt(
        task, agent._acceptance_criteria(task), "frontend/src/pages/Login.jsx",
        snapshot, planned, {"routes": [], "schemas": {}},
    )
    context = json.loads(user)["related_frontend_contents"]
    assert context == {"frontend/src/services/auth.js": "export async function login() {}"}
