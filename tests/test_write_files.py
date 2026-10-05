import asyncio
import importlib
import json

import httpx
import pytest
from app.models.write_files import WriteFilesRequest, WriteFilesResponse
from app.tools.workspace import WorkspaceTool


@pytest.fixture
def api(tmp_path, monkeypatch):
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "workspace_tool", WorkspaceTool(tmp_path / "workspace"))
    monkeypatch.setenv("N8N_TOOL_API_KEY", "test-token")
    return main, tmp_path / "workspace"


def post(api, files, agent="backend", token="test-token"):
    main, _ = api
    response = asyncio.run(main.write_files(
        WriteFilesRequest(task_id="TASK-002", agent=agent, files=files),
        x_tool_api_key=token,
    ))
    if isinstance(response, WriteFilesResponse):
        return 200, response.model_dump()
    return response.status_code, json.loads(response.body)


def test_valid_backend_file(api):
    response = post(api, [{"path": "backend/app/main.py", "content": "app = 1\n"}])
    assert response[0] == 200
    assert response[1] == {
        "task_id": "TASK-002", "success": True,
        "files_written": ["backend/app/main.py"], "files_rejected": [],
        "message": "Files written successfully",
    }
    assert (api[1] / "backend/app/main.py").read_text() == "app = 1\n"


def test_multiple_nested_files(api):
    files = [
        {"path": "backend/app/routes/students.py", "content": "route = True\n"},
        {"path": "backend/tests/test_students.py", "content": "def test_student(): pass\n"},
    ]
    response = post(api, files)
    assert response[0] == 200
    assert response[1]["files_written"] == [file["path"] for file in files]
    for file in files:
        assert (api[1] / file["path"]).read_text() == file["content"]


@pytest.mark.parametrize("path", [
    "backend/../frontend/app.py", "../backend/app.py", "/tmp/unsafe.py",
    "frontend/app/main.py", "app/main.py", "", "backend/", "backend//app.py",
    "backend/./app.py", "backend/../../app/main.py", "backend\\app\\main.py",
])
def test_unsafe_paths_rejected(api, path):
    response = post(api, [{"path": path, "content": "unsafe"}])
    assert response[0] == 400
    assert response[1]["files_written"] == []
    assert response[1]["files_rejected"] == [path]


def test_invalid_batch_writes_nothing(api):
    response = post(api, [
        {"path": "backend/app/good.py", "content": "ok"},
        {"path": "../app/main.py", "content": "bad"},
    ])
    assert response[0] == 400
    assert not (api[1] / "backend/app/good.py").exists()


def test_symlink_escape_rejected(api):
    workspace = api[1]
    (workspace / "backend").mkdir()
    (workspace / "frontend").mkdir()
    (workspace / "backend/app").symlink_to(workspace / "frontend", target_is_directory=True)
    response = post(api, [{"path": "backend/app/main.py", "content": "bad"}])
    assert response[0] == 400
    assert not (workspace / "frontend/main.py").exists()


def test_manager_source_not_writable(api):
    response = post(api, [{"path": "app/agents/manager.py", "content": "bad"}])
    assert response[0] == 400
    assert not (api[1] / "app/agents/manager.py").exists()


def test_backend_agent_only(api):
    response = post(api, [{"path": "backend/app/main.py", "content": "bad"}], agent="frontend")
    assert response[0] == 400
    assert not (api[1] / "backend/app/main.py").exists()


def test_token_required(api, monkeypatch):
    files = [{"path": "backend/app/main.py", "content": "bad"}]
    assert post(api, files, token="wrong")[0] == 401
    assert post(api, files, token=None)[0] == 401
    monkeypatch.delenv("N8N_TOOL_API_KEY")
    assert post(api, files)[0] == 401
    assert not (api[1] / "backend/app/main.py").exists()


def test_normal_app_initializes_file_writer():
    main = importlib.import_module("app.main")
    assert main.workspace_tool.root == main.ROOT_DIR / "workspace"
    assert any(route.path == "/tools/write-files" for route in main.app.routes)


def test_http_integration_writes_to_existing_workspace(api):
    main, workspace = api
    payload = {
        "task_id": "TEST-001",
        "agent": "backend",
        "files": [{
            "path": "backend/test_demo.py",
            "content": "print('n8n backend agent test')",
        }],
    }

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            return await client.post(
                "/tools/write-files", json=payload,
                headers={"X-Tool-API-Key": "test-token"},
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["files_written"] == ["backend/test_demo.py"]
    assert (workspace / "backend/test_demo.py").read_text() == payload["files"][0]["content"]
    assert not (workspace / "backend/backend/test_demo.py").exists()


def test_http_authentication_and_secret_redaction(api, caplog):
    main, workspace = api
    payload = {
        "task_id": "TASK-483", "agent": "backend",
        "files": [{"path": "backend/app/main.py", "content": "ok"}],
    }

    async def request(headers):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            return await client.post("/tools/write-files", json=payload, headers=headers)

    for headers in ({}, {"X-Tool-API-Key": "incorrect-secret"}):
        response = asyncio.run(request(headers))
        assert response.status_code == 401
        assert response.json()["message"] == "Unauthorized"
        assert response.json()["files_written"] == []
    assert not (workspace / "backend/app/main.py").exists()

    response = asyncio.run(request({"X-Tool-API-Key": "test-token"}))
    assert response.status_code == 200
    assert (workspace / "backend/app/main.py").read_text() == "ok"
    output = caplog.text + response.text
    assert "test-token" not in output
    assert "incorrect-secret" not in output


def test_openapi_documents_endpoint(api):
    schema = api[0].app.openapi()
    assert "/tools/write-files" in schema["paths"]
    operation = schema["paths"]["/tools/write-files"]["post"]
    assert operation["requestBody"]["content"]["application/json"]
    assert any(parameter["name"] == "x-tool-api-key" for parameter in operation["parameters"])
    assert "200" in operation["responses"]
