import hashlib
import importlib

import httpx
import pytest

from app.tools.workspace import WorkspaceTool


@pytest.fixture
def context_api(tmp_path, monkeypatch):
    main = importlib.import_module("app.main")
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(main, "workspace_tool", WorkspaceTool(workspace))
    monkeypatch.setenv("N8N_TOOL_API_KEY", "test-secret")
    app_root = workspace / "backend/app"
    app_root.mkdir(parents=True)
    (app_root / "main.py").write_text("app = object()\n", encoding="utf-8")
    (app_root / "models.py").write_text("class User: pass\n", encoding="utf-8")
    return main, workspace


def get_context(context_api, params=None, key="test-secret"):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=context_api[0].app), base_url="http://test"
        ) as client:
            return await client.get(
                "/tools/project-context",
                params=params,
                headers={"X-Tool-API-Key": key} if key is not None else {},
            )

    import asyncio
    return asyncio.run(request())


def test_context_lists_backend_app_files_and_hashes(context_api):
    response = get_context(context_api)
    assert response.status_code == 200
    data = response.json()
    assert data["project_root"] == str(context_api[1] / "backend")
    assert data["canonical_app"] == "backend.app.main"
    assert data["existing_files"] == ["backend/app/main.py", "backend/app/models.py"]
    assert data["file_contents"] == {}
    assert {entry["path"]: entry["sha256"] for entry in data["file_metadata"]}[
        "backend/app/main.py"
    ] == hashlib.sha256(b"app = object()\n").hexdigest()
    assert {entry["path"]: entry["size"] for entry in data["file_metadata"]}[
        "backend/app/main.py"
    ] == len(b"app = object()\n")
    assert any("backend.app.database" in rule for rule in data["architecture_rules"])


def test_selected_file_contents_and_nested_source(context_api):
    router = context_api[1] / "backend/app/routers/students.py"
    router.parent.mkdir()
    router.write_text("router = None\n", encoding="utf-8")
    response = get_context(context_api, [("files", "backend/app/main.py"), ("files", "backend/app/routers/students.py")])
    assert response.status_code == 200
    assert response.json()["file_contents"] == {
        "backend/app/main.py": "app = object()\n",
        "backend/app/routers/students.py": "router = None\n",
    }


@pytest.mark.parametrize(
    "path,status",
    [
        ("backend/app/missing.py", 404),
        ("backend/app/../main.py", 400),
        ("/tmp/main.py", 400),
        ("backend/tests/test_auth.py", 400),
        ("backend/app/.env", 400),
        ("backend/app/secrets.py", 400),
        ("backend/app/data.db", 400),
        ("app/main.py", 400),
    ],
)
def test_rejects_missing_or_unauthorized_paths(context_api, path, status):
    assert get_context(context_api, {"files": path}).status_code == status


def test_rejects_symlink_escape(context_api, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'outside'\n", encoding="utf-8")
    (context_api[1] / "backend/app/link.py").symlink_to(outside)
    response = get_context(context_api, {"files": "backend/app/link.py"})
    assert response.status_code == 400
    assert "outside" not in response.text


def test_rejects_embedded_credentials(context_api):
    secret = "private-value-that-must-not-leak"
    (context_api[1] / "backend/app/auth.py").write_text(
        f"SECRET_KEY = {secret!r}\n", encoding="utf-8"
    )
    response = get_context(context_api, {"files": "backend/app/auth.py"})
    assert response.status_code == 400
    assert secret not in response.text
    assert "backend/app/auth.py" in get_context(context_api).json()["existing_files"]


@pytest.mark.parametrize(
    "source",
    [
        "SECRET_KEY = os.getenv('SECRET_KEY', 'fallback-secret')\n",
        "config = {'api_key': 'embedded-secret'}\n",
        "client = make_client(api_key='embedded-secret')\n",
    ],
)
def test_rejects_nested_literal_credentials(context_api, source):
    (context_api[1] / "backend/app/config.py").write_text(source, encoding="utf-8")
    response = get_context(context_api, {"files": "backend/app/config.py"})
    assert response.status_code == 400
    assert "embedded-secret" not in response.text
    assert "fallback-secret" not in response.text


def test_content_and_response_size_limits(context_api, monkeypatch):
    source = context_api[1] / "backend/app/main.py"
    source.write_text("x" * 17_000, encoding="utf-8")
    assert get_context(context_api, {"files": "backend/app/main.py"}).status_code == 413

    module = importlib.import_module("app.tools.project_context")
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)
    assert get_context(context_api).status_code == 413


def test_requires_same_tool_key(context_api):
    assert get_context(context_api, key=None).status_code == 401
    assert get_context(context_api, key="wrong").status_code == 401
