import asyncio
import importlib

import httpx
import pytest

from app.models.task import RunEvidence
from app.tools.workspace import WorkspaceTool


@pytest.fixture
def api(tmp_path, monkeypatch):
    main = importlib.import_module("app.main")
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(main, "workspace_tool", WorkspaceTool(workspace))
    monkeypatch.setenv("N8N_TOOL_API_KEY", "test-secret")
    return main, workspace


def post(api, headers=None, payload=None):
    main, _ = api

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            return await client.post(
                "/tools/run-tests",
                headers=headers or {},
                json=payload or {"task_id": "TASK-002", "agent": "backend"},
            )

    return asyncio.run(request())


def make_test(workspace, body):
    tests = workspace / "backend/tests"
    tests.mkdir(parents=True)
    (tests / "test_example.py").write_text(body, encoding="utf-8")


def test_successful_backend_pytest(api):
    make_test(api[1], "def test_success():\n    assert True\n")
    response = post(api, {"X-Tool-API-Key": "test-secret"})
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["exit_code"] == 0
    assert "1 passed" in response.json()["stdout"]
    assert response.json()["message"] == "Tests passed"


def test_failing_pytest_returns_json(api):
    make_test(api[1], "def test_failure():\n    assert 1 == 2\n")
    response = post(api, {"X-Tool-API-Key": "test-secret"})
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["exit_code"] == 1
    assert "AssertionError" in response.json()["stdout"]
    assert response.json()["message"] == "Tests failed"


def test_missing_or_wrong_key_does_not_run_tests(api, monkeypatch):
    calls = []

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            calls.append("created")

    monkeypatch.setattr(api[0], "BackendTestRunner", FakeRunner)
    for headers in ({}, {"X-Tool-API-Key": "wrong"}):
        response = post(api, headers)
        assert response.status_code == 401
        assert response.json()["message"] == "Unauthorized"
    assert calls == []


def test_only_backend_and_no_client_paths_or_commands(api):
    make_test(api[1], "def test_ok():\n    assert True\n")
    headers = {"X-Tool-API-Key": "test-secret"}
    response = post(api, headers, {"task_id": "TASK-002", "agent": "frontend"})
    assert response.status_code == 400
    for extra in ({"path": "../outside"}, {"path": "/tmp/outside"}, {"command": "echo unsafe"}):
        payload = {"task_id": "TASK-002", "agent": "backend", **extra}
        assert post(api, headers, payload).status_code == 422


def test_missing_or_symlinked_backend_tests_rejected(api, tmp_path):
    headers = {"X-Tool-API-Key": "test-secret"}
    assert post(api, headers).status_code == 400
    outside = tmp_path / "outside"
    outside.mkdir()
    (api[1] / "backend").mkdir()
    (api[1] / "backend/tests").symlink_to(outside, target_is_directory=True)
    assert post(api, headers).status_code == 400


def test_timeout_and_execution_error_are_structured(api, monkeypatch):
    make_test(api[1], "def test_ok():\n    assert True\n")

    class TimeoutRunner:
        def __init__(self, workspace_root):
            assert workspace_root == api[1]

        async def run(self, test_paths=None, python_executable=None):
            assert test_paths is None
            return RunEvidence(passed=False, exit_code=124, summary="Tests timed out after 30 seconds")

    monkeypatch.setattr(api[0], "BackendTestRunner", TimeoutRunner)
    response = post(api, {"X-Tool-API-Key": "test-secret"})
    assert response.status_code == 200
    assert response.json()["exit_code"] == 124
    assert "timed out" in response.json()["message"]

    class ErrorRunner(TimeoutRunner):
        async def run(self, test_paths=None, python_executable=None):
            raise OSError("internal details")

    monkeypatch.setattr(api[0], "BackendTestRunner", ErrorRunner)
    response = post(api, {"X-Tool-API-Key": "test-secret"})
    assert response.status_code == 500
    assert response.json()["exit_code"] is None
    assert "internal details" not in response.text


def test_openapi_has_authenticated_route(api):
    operation = api[0].app.openapi()["paths"]["/tools/run-tests"]["post"]
    assert any(parameter["name"] == "x-tool-api-key" for parameter in operation["parameters"])
    assert "200" in operation["responses"]
