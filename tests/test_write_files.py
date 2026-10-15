import asyncio
import hashlib
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

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


def post(api, files, agent="backend", token="test-token", modify_key=None):
    main, _ = api
    response = asyncio.run(main.write_files(
        WriteFilesRequest(task_id="TASK-002", agent=agent, files=files),
        x_tool_api_key=token,
        x_tool_modify_key=modify_key,
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


def test_frontend_file_uses_single_frontend_prefix(api):
    response = post(
        api,
        [{"path": "frontend/src/App.jsx", "content": "export default function App() {}\n"}],
        agent="frontend",
    )
    assert response[0] == 200
    assert response[1]["files_written"] == ["frontend/src/App.jsx"]
    assert (api[1] / "frontend/src/App.jsx").is_file()
    assert not (api[1] / "frontend/frontend/src/App.jsx").exists()


@pytest.mark.parametrize("agent,path", [
    ("backend", "frontend/src/App.jsx"),
    ("frontend", "backend/app/main.py"),
    ("frontend", "frontend/../backend/app/main.py"),
    ("frontend", "../frontend/src/App.jsx"),
    ("frontend", "/tmp/App.jsx"),
    ("frontend", "app/orchestrator/workflow.py"),
    ("frontend", "frontend/src/../../app/main.py"),
])
def test_agent_cannot_write_outside_its_directory(api, agent, path):
    response = post(api, [{"path": path, "content": "bad"}], agent=agent)
    assert response[0] == 400
    assert response[1]["files_rejected"] == [path]
    assert response[1]["files_written"] == []


def test_frontend_rejects_symlink_into_backend(api):
    workspace = api[1]
    (workspace / "frontend").mkdir()
    (workspace / "backend").mkdir()
    (workspace / "frontend/src").symlink_to(workspace / "backend", target_is_directory=True)
    response = post(api, [{"path": "frontend/src/main.jsx", "content": "bad"}], agent="frontend")
    assert response[0] == 400
    assert not (workspace / "backend/main.jsx").exists()


def test_frontend_mixed_batch_writes_nothing(api):
    response = post(api, [
        {"path": "frontend/src/App.jsx", "content": "ok"},
        {"path": "backend/app/main.py", "content": "bad"},
    ], agent="frontend")
    assert response[0] == 400
    assert not (api[1] / "frontend/src/App.jsx").exists()


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


def test_frontend_cannot_write_backend_file(api):
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
            "path": "backend/app/test_demo.py",
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
    assert response.json()["files_written"] == ["backend/app/test_demo.py"]
    assert (workspace / "backend/app/test_demo.py").read_text() == payload["files"][0]["content"]
    assert not (workspace / "backend/backend/app/test_demo.py").exists()


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


def test_existing_file_requires_trusted_authorization_and_hash(api, monkeypatch):
    monkeypatch.setenv("N8N_TOOL_MODIFY_KEY", "trusted-update-token")
    path = "backend/app/main.py"
    original = "app = 'validated'\n"
    assert post(api, [{"path": path, "content": original}])[0] == 200
    sha = hashlib.sha256(original.encode()).hexdigest()
    update = {"path": path, "content": "app = 'new'\n", "allow_modify": True, "expected_sha256": sha}

    for file, key, reason in (
        ({"path": path, "content": "bad"}, None, "existing_file_without_authorization"),
        (update, None, "existing_file_without_authorization"),
        (update, "wrong-token", "existing_file_without_authorization"),
        ({**update, "expected_sha256": None}, "trusted-update-token", "missing_expected_hash"),
        ({**update, "expected_sha256": "invalid"}, "trusted-update-token", "invalid_expected_hash"),
    ):
        status, body = post(api, [file], modify_key=key)
        assert status == 400
        assert body["files_written"] == []
        assert body["rejections"] == [{"path": path, "reason": reason}]
        assert (api[1] / path).read_text() == original

    status, body = post(api, [update], modify_key="trusted-update-token")
    assert status == 200
    assert body["files_written"] == [path]
    assert (api[1] / path).read_text() == "app = 'new'\n"

    status, body = post(api, [update], modify_key="trusted-update-token")
    assert status == 409
    assert body["rejections"] == [{"path": path, "reason": "hash_mismatch"}]
    assert (api[1] / path).read_text() == "app = 'new'\n"


@pytest.mark.parametrize("path", ["backend/models.py", "backend/db.py", "backend/main.py", "backend/app/data.db"])
def test_legacy_or_unsafe_backend_type_rejected(api, path):
    status, body = post(api, [{"path": path, "content": "bad"}])
    assert status == 400
    assert body["rejections"] == [{"path": path, "reason": "unsafe_file_type"}]
    assert not (api[1] / path).exists()


def test_existing_frontend_file_also_requires_authorization(api):
    path = "frontend/src/App.jsx"
    assert post(api, [{"path": path, "content": "old"}], agent="frontend")[0] == 200
    status, body = post(api, [{"path": path, "content": "new"}], agent="frontend")
    assert status == 400
    assert body["rejections"][0]["reason"] == "existing_file_without_authorization"
    assert (api[1] / path).read_text() == "old"


def test_concurrent_new_file_writes_allow_only_one(api):
    path = "backend/app/only_once.py"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, api, [{"path": path, "content": "first"}])
        second = pool.submit(post, api, [{"path": path, "content": "second"}])
        results = [first.result(), second.result()]
    assert sorted(status for status, _ in results) == [200, 400]
    assert (api[1] / path).read_text() in {"first", "second"}


def test_concurrent_authorized_updates_use_hash_as_compare_and_swap(api, monkeypatch):
    monkeypatch.setenv("N8N_TOOL_MODIFY_KEY", "trusted-update-token")
    path = "backend/app/main.py"
    assert post(api, [{"path": path, "content": "original"}])[0] == 200
    expected = hashlib.sha256(b"original").hexdigest()

    def update(content):
        return post(api, [{
            "path": path,
            "content": content,
            "allow_modify": True,
            "expected_sha256": expected,
        }], modify_key="trusted-update-token")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(update, "first")
        second = pool.submit(update, "second")
        results = [first.result(), second.result()]
    assert sorted(status for status, _ in results) == [200, 409]
    assert (api[1] / path).read_text() in {"first", "second"}


def test_update_key_is_not_returned_or_logged(api, monkeypatch, caplog):
    trusted_key = "trusted-update-token"
    monkeypatch.setenv("N8N_TOOL_MODIFY_KEY", trusted_key)
    path = "backend/app/main.py"
    assert post(api, [{"path": path, "content": "original"}])[0] == 200
    status, body = post(api, [{
        "path": path, "content": "bad", "allow_modify": True,
        "expected_sha256": "0" * 64,
    }], modify_key=trusted_key)
    assert status == 409
    assert trusted_key not in caplog.text + json.dumps(body)


def test_http_update_uses_project_context_hash(api, monkeypatch):
    main, workspace = api
    monkeypatch.setenv("N8N_TOOL_MODIFY_KEY", "trusted-update-token")
    path = "backend/app/main.py"
    assert post(api, [{"path": path, "content": "original"}])[0] == 200

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            context = await client.get(
                "/tools/project-context",
                headers={"X-Tool-API-Key": "test-token"},
            )
            expected = next(
                item["sha256"] for item in context.json()["file_metadata"]
                if item["path"] == path
            )
            return await client.post(
                "/tools/write-files",
                headers={
                    "X-Tool-API-Key": "test-token",
                    "X-Tool-Modify-Key": "trusted-update-token",
                },
                json={
                    "task_id": "TASK-002", "agent": "backend",
                    "files": [{
                        "path": path, "content": "updated", "allow_modify": True,
                        "expected_sha256": expected,
                    }],
                },
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json()["files_written"] == [path]
    assert (workspace / path).read_text() == "updated"


def test_multifile_validation_and_commit_failure_leave_files_unchanged(api, monkeypatch):
    original_path = "backend/app/main.py"
    assert post(api, [{"path": original_path, "content": "original"}])[0] == 200
    original_hash = hashlib.sha256(b"original").hexdigest()
    new_path = "backend/app/new.py"
    update = {"path": original_path, "content": "updated", "allow_modify": True, "expected_sha256": original_hash}
    monkeypatch.setenv("N8N_TOOL_MODIFY_KEY", "trusted-update-token")

    status, body = post(api, [update, {"path": "backend/models.py", "content": "bad"}], modify_key="trusted-update-token")
    assert status == 400
    assert body["files_written"] == []
    assert (api[1] / original_path).read_text() == "original"

    module = importlib.import_module("app.tools.safe_write")
    real_link = module.os.link

    def fail_second(source, target, **kwargs):
        if str(target).endswith("new.py"):
            raise OSError("simulated commit failure")
        return real_link(source, target, **kwargs)

    monkeypatch.setattr(module.os, "link", fail_second)
    status, body = post(api, [update, {"path": new_path, "content": "new"}], modify_key="trusted-update-token")
    assert status == 500
    assert body["files_written"] == []
    assert (api[1] / original_path).read_text() == "original"
    assert not (api[1] / new_path).exists()
