import asyncio
from pathlib import Path

import pytest

from app.tools.dependency_manager import DependencyManager


def test_dependency_manager_installs_only_missing_approved_packages(
    tmp_path: Path, monkeypatch
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "pytest==8.1.1\nfastapi==0.110.0\n", encoding="utf-8"
    )
    manager = DependencyManager(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def virtualenv_ready():
        return False, None

    async def installed_versions(python_executable):
        return {"pytest": "8.1.1"}, (0, '[{"name":"pytest","version":"8.1.1"}]', "")

    async def run(executable, *arguments):
        calls.append(arguments)
        return 0, "installed", ""

    monkeypatch.setattr(manager, "_ensure_virtualenv", virtualenv_ready)
    monkeypatch.setattr(
        manager, "_installed_package_versions", installed_versions
    )
    monkeypatch.setattr(manager, "_run", run)

    evidence = asyncio.run(manager.prepare())

    assert evidence.success
    assert evidence.already_installed == ["pytest==8.1.1"]
    assert evidence.installed == ["fastapi==0.110.0"]
    assert calls == [("-m", "pip", "install", "fastapi==0.110.0")]


def test_dependency_manager_rejects_unapproved_dependency(tmp_path: Path) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "unapproved-package==1.0\n", encoding="utf-8"
    )

    evidence = asyncio.run(DependencyManager(tmp_path).prepare())

    assert not evidence.success
    assert "not approved" in evidence.summary


@pytest.mark.parametrize(
    "requirement",
    [
        "pytest-asyncio",
        "pytest-asyncio==0.23.6",
        "uvicorn[standard]",
        "uvicorn[standard]==0.28.0",
        "pydantic-settings==2.2.1",
        "SQLAlchemy==2.0.28",
        "SQLALCHEMY==2.0.28",
        "pytest>=8.0,<9",
        "PyJWT==2.8.0",
        "requests==2.31.0",
        "Requests==2.31.0",
    ],
)
def test_dependency_manager_approves_pinned_normalized_packages(
    tmp_path: Path, requirement: str
) -> None:
    manager = DependencyManager(tmp_path)

    assert manager._validate_requirements([requirement]) == [requirement]


@pytest.mark.parametrize(
    "requirement",
    [
        "unknown-package",
        "unknown-package==1.0",
        "unknown-package[extra]",
        "fastapi[all]",
        "requests[security]",
        "requests @ https://example.com/requests.whl",
        "git+https://example.com/project.git",
        "example @ git+https://example.com/project.git",
        "https://example.com/package.whl",
        "fastapi @ https://example.com/package.whl",
        "../local-package",
        "./package",
        "-e ./package",
        "-r requirements-dev.txt",
        "--index-url https://example.com/simple",
        "--extra-index-url https://example.com/simple",
        "--trusted-host example.com",
        'fastapi; python_version >= "3.11"',
        "fastapi; malicious-expression",
        "fastapi==1.0; echo unsafe",
        "fastapi==1.0 && echo unsafe",
        "fastapi==1.0 || echo unsafe",
        "fastapi$(echo unsafe)",
        "fastapi`echo unsafe`",
        "fastapi==$PACKAGE_VERSION",
        "fastapi==${PACKAGE_VERSION}",
    ],
)
def test_dependency_manager_rejects_unsafe_or_unknown_specifications(
    tmp_path: Path, requirement: str
) -> None:
    manager = DependencyManager(tmp_path)

    with pytest.raises(ValueError, match="(?:not approved|Invalid dependency)"):
        manager._validate_requirements([requirement])


def test_compatibility_resolution_pins_only_allowlisted_direct_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "passlib[bcrypt]>=1.7\nbcrypt>=4\n", encoding="utf-8"
    )
    manager = DependencyManager(tmp_path)
    python_executable = manager._venv_python()
    python_executable.parent.mkdir(parents=True)
    python_executable.write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    async def run(executable, *arguments):
        calls.append(arguments)
        return 0, "ok", ""

    async def installed_versions(executable):
        return (
            {"passlib": "1.7.4", "bcrypt": "4.0.1"},
            (0, '[{"name":"passlib","version":"1.7.4"}]', ""),
        )

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_installed_package_versions", installed_versions)

    evidence = asyncio.run(
        manager.resolve_compatibility(
            "passlib trapped error reading bcrypt version: "
            "module bcrypt has no attribute __about__"
        )
    )

    assert evidence.success
    assert (backend / "requirements.txt").read_text(encoding="utf-8") == (
        "passlib[bcrypt]==1.7.4\nbcrypt==4.0.1\n"
    )
    assert calls[0] == ("-m", "pip", "check")
    assert calls[1][:4] == (
        "-m",
        "pip",
        "install",
        "--upgrade",
    )
    assert "--upgrade-strategy" not in calls[1]
    assert calls[2] == ("-m", "pip", "check")


def test_compatibility_resolution_rejects_unsafe_requirement_before_subprocess(
    tmp_path: Path, monkeypatch
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "unknown-package @ https://example.com/package.whl\n", encoding="utf-8"
    )
    manager = DependencyManager(tmp_path)
    called = False

    async def run(executable, *arguments):
        nonlocal called
        called = True
        return 0, "", ""

    monkeypatch.setattr(manager, "_run", run)

    evidence = asyncio.run(manager.resolve_compatibility("dependency failure"))

    assert not evidence.success
    assert "not approved" in evidence.summary
    assert called is False


def test_prepare_reinstalls_mismatched_pinned_version(
    tmp_path: Path, monkeypatch
) -> None:
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "requirements.txt").write_text(
        "bcrypt==4.0.1\n", encoding="utf-8"
    )
    manager = DependencyManager(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def virtualenv_ready():
        return False, None

    async def installed_versions(python_executable):
        return {"bcrypt": "5.0.0"}, (0, "[]", "")

    async def run(executable, *arguments):
        calls.append(arguments)
        return 0, "installed", ""

    monkeypatch.setattr(manager, "_ensure_virtualenv", virtualenv_ready)
    monkeypatch.setattr(
        manager, "_installed_package_versions", installed_versions
    )
    monkeypatch.setattr(manager, "_run", run)

    evidence = asyncio.run(manager.prepare())

    assert evidence.success
    assert evidence.already_installed == []
    assert evidence.installed == ["bcrypt==4.0.1"]
    assert calls == [("-m", "pip", "install", "bcrypt==4.0.1")]
