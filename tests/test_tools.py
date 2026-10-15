import asyncio
import sys
from pathlib import Path

import pytest

from app.tools.test_runner import BackendTestRunner
from app.tools.workspace import WorkspacePathError, WorkspaceTool


def test_workspace_create_read_update_and_list(tmp_path: Path) -> None:
    workspace = WorkspaceTool(tmp_path)
    workspace.create_file("backend/app.py", "version = 1\n")
    workspace.update_file("backend/app.py", "version = 2\n")

    assert workspace.read_file("backend/app.py") == "version = 2\n"
    assert workspace.list_files() == ["backend/app.py"]


@pytest.mark.parametrize("path", ["../outside.py", "../../outside.py", "/tmp/outside.py"])
def test_workspace_rejects_paths_outside_root(tmp_path: Path, path: str) -> None:
    workspace = WorkspaceTool(tmp_path / "workspace")

    with pytest.raises(WorkspacePathError):
        workspace.write_file(path, "unsafe\n")


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_root.mkdir()
    (workspace_root / "link").symlink_to(outside, target_is_directory=True)
    workspace = WorkspaceTool(workspace_root)

    with pytest.raises(WorkspacePathError):
        workspace.write_file("link/escaped.py", "unsafe\n")


def test_runner_executes_only_backend_pytest_target(tmp_path: Path) -> None:
    tests_dir = tmp_path / "backend/tests"
    tests_dir.mkdir(parents=True)
    (tmp_path / "backend/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "backend/generated.py").write_text("VALUE = 42\n", encoding="utf-8")
    (tests_dir / "test_generated.py").write_text(
        "from backend.generated import VALUE\n\n"
        "def test_generated():\n    assert VALUE == 42\n",
        encoding="utf-8",
    )

    result = asyncio.run(BackendTestRunner(tmp_path, timeout=10).run())

    assert result.passed
    assert result.exit_code == 0
    assert "1 passed" in result.summary


def test_runner_uses_safe_deterministic_subprocess_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"1 passed\n", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "app.tools.test_runner.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(BackendTestRunner(tmp_path).run())

    assert result.passed
    assert captured["command"] == (
        sys.executable,
        "-m",
        "pytest",
        str(tmp_path / "backend/tests"),
        "-q",
    )
    run_root = captured["kwargs"]["cwd"]
    assert run_root != tmp_path.resolve()
    assert not run_root.exists()  # Only this invocation's temporary directory is removed.
    assert captured["kwargs"]["env"]["PYTHONPATH"] == str(tmp_path.resolve())
    assert captured["kwargs"]["env"]["DATABASE_URL"] == f"sqlite:///{run_root / 'test.db'}"
    assert captured["kwargs"]["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert captured["kwargs"]["env"]["PYTHONNOUSERSITE"] == "1"
    assert "shell" not in captured["kwargs"]


def test_runner_isolates_hardcoded_relative_sqlite_files(tmp_path: Path) -> None:
    tests_dir = tmp_path / "backend/tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_database.py").write_text(
        "import os\n"
        "import sqlite3\n"
        "def test_database():\n"
        "    assert os.environ['DATABASE_URL'].startswith('sqlite:////')\n"
        "    connection = sqlite3.connect('test_auth_api.db')\n"
        "    connection.execute('CREATE TABLE roles (id INTEGER)')\n"
        "    connection.close()\n",
        encoding="utf-8",
    )
    persisted = tmp_path / "test_auth_api.db"
    persisted.write_bytes(b"existing-user-data")
    default_database = tmp_path / "test.db"
    default_database.write_bytes(b"existing-default-data")
    runner = BackendTestRunner(tmp_path, timeout=10)

    first = asyncio.run(runner.run())
    second = asyncio.run(runner.run())

    assert first.passed and second.passed, (first, second)
    assert persisted.read_bytes() == b"existing-user-data"
    assert default_database.read_bytes() == b"existing-default-data"


def test_runner_serializes_concurrent_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    active = 0
    peak = 0
    directories = []

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            nonlocal active
            await asyncio.sleep(0.02)
            active -= 1
            return b"1 passed\n", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        directories.append(kwargs["cwd"])
        assert kwargs["cwd"] != tmp_path
        assert command[3] == str(tmp_path / "backend/tests")
        return FakeProcess()

    monkeypatch.setattr(
        "app.tools.test_runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )

    async def run_both():
        return await asyncio.gather(BackendTestRunner(tmp_path).run(), BackendTestRunner(tmp_path).run())

    results = asyncio.run(run_both())
    assert all(result.passed for result in results)
    assert peak == 1
    assert directories[0] != directories[1]
    assert all(not path.exists() for path in directories)


def test_runner_preserves_generated_virtualenv_interpreter_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_python = tmp_path / "backend/.venv/bin/python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    captured: dict = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"1 passed\n", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    monkeypatch.setattr(
        "app.tools.test_runner.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = asyncio.run(
        BackendTestRunner(tmp_path).run(python_executable=venv_python)
    )

    assert result.passed
    assert captured["command"][0] == str(venv_python.absolute())


@pytest.mark.parametrize(
    "unsafe_target",
    [
        "../outside.py",
        "/tmp/test_outside.py",
        "backend/tests/test_safe.py --collect-only",
        "backend/tests/test_safe.py;rm",
    ],
)
def test_runner_rejects_arbitrary_or_outside_targets(
    tmp_path: Path, unsafe_target: str
) -> None:
    runner = BackendTestRunner(tmp_path)

    with pytest.raises(ValueError, match="Invalid backend test path"):
        asyncio.run(runner.run([unsafe_target]))
