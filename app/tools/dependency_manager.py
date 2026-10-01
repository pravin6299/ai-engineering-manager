import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from app.models.task import DependencyEvidence

logger = logging.getLogger(__name__)


class DependencyManager:
    approved_packages = {
        "aiosqlite": set(),
        "bcrypt": set(),
        "email-validator": set(),
        "fastapi": set(),
        "httpx": set(),
        "passlib": {"bcrypt"},
        "pydantic": set(),
        "pydantic-settings": set(),
        "pyjwt": set(),
        "pytest": set(),
        "pytest-asyncio": set(),
        "python-jose": {"cryptography"},
        "python-multipart": set(),
        "requests": set(),
        "sqlalchemy": set(),
        "uvicorn": {"standard"},
    }

    def __init__(self, workspace_root: Path | str = "workspace", timeout: float = 180) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.backend_root = self.workspace_root / "backend"
        self.venv_root = self.backend_root / ".venv"
        self.timeout = timeout

    async def prepare(self) -> DependencyEvidence:
        try:
            requested = self._read_requirements()
            logger.info("DEPENDENCY MANAGER: requested %s", ", ".join(requested))
            approved = self._validate_requirements(requested)
            logger.info("DEPENDENCY MANAGER: approved %s", ", ".join(approved))
        except (OSError, ValueError) as exc:
            return DependencyEvidence(
                success=False,
                summary=str(exc),
                stderr=str(exc),
            )

        created, evidence = await self._ensure_virtualenv()
        if evidence is not None:
            return evidence

        python_executable = self._venv_python()
        installed_versions, list_result = await self._installed_package_versions(
            python_executable
        )
        if list_result[0] != 0:
            return self._failure(
                requested, approved, "Could not inspect generated virtualenv", list_result
            )

        parsed = [(item, Requirement(item)) for item in approved]
        already_installed = [
            item
            for item, requirement in parsed
            if self._requirement_is_installed(requirement, installed_versions)
        ]
        missing = [item for item, _ in parsed if item not in already_installed]
        if already_installed:
            logger.info(
                "DEPENDENCY MANAGER: already installed %s",
                ", ".join(already_installed),
            )

        installed: list[str] = []
        stdout = list_result[1]
        stderr = list_result[2]
        if missing:
            logger.info("DEPENDENCY MANAGER: installing %s", ", ".join(missing))
            install_result = await self._run(
                python_executable, "-m", "pip", "install", *missing
            )
            stdout = f"{stdout}\n{install_result[1]}".strip()
            stderr = f"{stderr}\n{install_result[2]}".strip()
            if install_result[0] != 0:
                return self._failure(
                    requested,
                    approved,
                    "Approved dependency installation failed",
                    install_result,
                    already_installed,
                )
            installed = missing
            logger.info("DEPENDENCY MANAGER: installation successful")
        else:
            logger.info("DEPENDENCY MANAGER: installation successful")

        return DependencyEvidence(
            success=True,
            requested=requested,
            approved=approved,
            already_installed=already_installed,
            installed=installed,
            summary="Generated backend dependencies are ready",
            stdout=stdout[-4000:],
            stderr=stderr[-4000:],
            python_executable=str(python_executable),
        )

    async def resolve_compatibility(self, failure_output: str) -> DependencyEvidence:
        logger.info("DEPENDENCY MANAGER: attempting compatibility resolution")
        try:
            requested = self._read_requirements()
            approved = self._validate_requirements(requested)
            resolution_requirements = self._compatibility_requirements(
                approved, failure_output
            )
            resolution_requirements = self._validate_requirements(
                resolution_requirements
            )
        except (OSError, ValueError) as exc:
            return DependencyEvidence(
                success=False,
                summary=str(exc),
                stderr=str(exc),
            )

        python_executable = self._venv_python()
        if not python_executable.is_file():
            return DependencyEvidence(
                success=False,
                requested=requested,
                approved=approved,
                summary="Generated backend virtualenv is not available",
                stderr=failure_output[-4000:],
            )

        before_check = await self._run(python_executable, "-m", "pip", "check")
        logger.info(
            "DEPENDENCY MANAGER: installing compatible %s",
            ", ".join(resolution_requirements),
        )
        install_result = await self._run(
            python_executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            *resolution_requirements,
        )
        if install_result[0] != 0:
            return self._failure(
                requested,
                resolution_requirements,
                "Approved compatibility resolution failed",
                install_result,
            )

        after_check = await self._run(python_executable, "-m", "pip", "check")
        combined_stdout = (
            f"{before_check[1]}\n{install_result[1]}\n{after_check[1]}"
        ).strip()
        combined_stderr = (
            f"{before_check[2]}\n{install_result[2]}\n{after_check[2]}"
        ).strip()
        if after_check[0] != 0:
            return self._failure(
                requested,
                resolution_requirements,
                "Resolved dependencies still fail pip check",
                after_check,
            )

        versions, list_result = await self._installed_package_versions(
            python_executable
        )
        if list_result[0] != 0:
            return self._failure(
                requested,
                resolution_requirements,
                "Could not inspect resolved dependency versions",
                list_result,
            )
        try:
            pinned = self._pin_requirements(resolution_requirements, versions)
            self._validate_requirements(pinned)
            requirements_path = self.backend_root / "requirements.txt"
            requirements_path.write_text(
                "\n".join(pinned) + "\n", encoding="utf-8"
            )
        except (OSError, ValueError) as exc:
            return DependencyEvidence(
                success=False,
                requested=requested,
                approved=resolution_requirements,
                summary=f"Could not persist compatible dependency constraints: {exc}",
                stderr=str(exc),
                python_executable=str(python_executable),
            )
        logger.info("DEPENDENCY MANAGER: compatibility resolution successful")
        return DependencyEvidence(
            success=True,
            requested=requested,
            approved=pinned,
            installed=pinned,
            summary="Approved dependency compatibility resolution completed",
            stdout=f"{combined_stdout}\n{list_result[1]}"[-4000:],
            stderr=combined_stderr[-4000:],
            python_executable=str(python_executable),
        )

    @staticmethod
    def _compatibility_requirements(
        approved: list[str], failure_output: str
    ) -> list[str]:
        text = failure_output.lower()
        passlib_bcrypt_failure = (
            "passlib" in text
            and "bcrypt" in text
            and any(
                signal in text
                for signal in (
                    "__about__",
                    "trapped error reading bcrypt version",
                    "password cannot be longer than 72 bytes",
                    "password cannot be longer",
                )
            )
        )
        if not passlib_bcrypt_failure:
            return approved

        resolved: list[str] = []
        saw_passlib = False
        saw_bcrypt = False
        for value in approved:
            requirement = Requirement(value)
            name = canonicalize_name(requirement.name)
            if name == "passlib":
                extras = (
                    f"[{','.join(sorted(requirement.extras))}]"
                    if requirement.extras
                    else ""
                )
                resolved.append(f"{requirement.name}{extras}==1.7.4")
                saw_passlib = True
            elif name == "bcrypt":
                resolved.append(f"{requirement.name}==4.0.1")
                saw_bcrypt = True
            else:
                resolved.append(value)
        if saw_passlib and not saw_bcrypt:
            resolved.append("bcrypt==4.0.1")
        logger.info(
            "DEPENDENCY MANAGER: selected controlled passlib/bcrypt compatibility profile"
        )
        return resolved

    def _read_requirements(self) -> list[str]:
        path = self.backend_root / "requirements.txt"
        if not path.is_file():
            raise ValueError("Generated backend/requirements.txt is required")
        requirements = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not requirements:
            raise ValueError("Generated backend/requirements.txt is empty")
        return requirements

    def _validate_requirements(self, requested: list[str]) -> list[str]:
        approved: list[str] = []
        for value in requested:
            try:
                requirement = Requirement(value)
            except InvalidRequirement as exc:
                raise ValueError(f"Invalid dependency requirement: {value}") from exc
            name = canonicalize_name(requirement.name)
            allowed_extras = self.approved_packages.get(name)
            if (
                allowed_extras is None
                or requirement.url
                or requirement.marker is not None
                or not set(requirement.extras).issubset(allowed_extras)
            ):
                raise ValueError(f"Dependency is not approved: {value}")
            approved.append(value)
        return approved

    async def _ensure_virtualenv(self) -> tuple[bool, DependencyEvidence | None]:
        python_executable = self._venv_python()
        if python_executable.is_file():
            return False, None
        self.backend_root.mkdir(parents=True, exist_ok=True)
        result = await self._run(
            Path(sys.executable), "-m", "venv", str(self.venv_root)
        )
        if result[0] != 0:
            return True, DependencyEvidence(
                success=False,
                summary="Could not create generated backend virtualenv",
                stdout=result[1][-4000:],
                stderr=result[2][-4000:],
            )
        return True, None

    @staticmethod
    def _requirement_is_installed(
        requirement: Requirement, installed_versions: dict[str, str]
    ) -> bool:
        version = installed_versions.get(canonicalize_name(requirement.name))
        if version is None:
            return False
        return not requirement.specifier or requirement.specifier.contains(
            version, prereleases=True
        )

    async def _installed_packages(
        self, python_executable: Path
    ) -> tuple[set[str], tuple[int, str, str]]:
        result = await self._run(
            python_executable, "-m", "pip", "list", "--format=json"
        )
        if result[0] != 0:
            return set(), result
        try:
            packages = json.loads(result[1])
        except json.JSONDecodeError:
            return set(), (1, result[1], "pip list returned invalid JSON")
        return {
            canonicalize_name(package["name"]) for package in packages
        }, result

    async def _installed_package_versions(
        self, python_executable: Path
    ) -> tuple[dict[str, str], tuple[int, str, str]]:
        result = await self._run(
            python_executable, "-m", "pip", "list", "--format=json"
        )
        if result[0] != 0:
            return {}, result
        try:
            packages = json.loads(result[1])
        except json.JSONDecodeError:
            return {}, (1, result[1], "pip list returned invalid JSON")
        return {
            canonicalize_name(package["name"]): package["version"]
            for package in packages
        }, result

    @staticmethod
    def _pin_requirements(
        requirements: list[str], installed_versions: dict[str, str]
    ) -> list[str]:
        pinned: list[str] = []
        for value in requirements:
            requirement = Requirement(value)
            name = canonicalize_name(requirement.name)
            version = installed_versions.get(name)
            if not version:
                raise ValueError(
                    f"Resolved dependency is not installed: {requirement.name}"
                )
            extras = (
                f"[{','.join(sorted(requirement.extras))}]"
                if requirement.extras
                else ""
            )
            pinned.append(f"{requirement.name}{extras}=={version}")
        return pinned

    async def _run(self, executable: Path, *arguments: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *arguments,
            cwd=self.workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.defpath,
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PYTHONNOUSERSITE": "1",
            },
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return 124, "", f"Dependency command timed out after {self.timeout:g} seconds"
        return (
            process.returncode or 0,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )

    def _venv_python(self) -> Path:
        directory = "Scripts" if os.name == "nt" else "bin"
        executable = "python.exe" if os.name == "nt" else "python"
        return self.venv_root / directory / executable

    def _failure(
        self,
        requested: list[str],
        approved: list[str],
        summary: str,
        result: tuple[int, str, str],
        already_installed: list[str] | None = None,
    ) -> DependencyEvidence:
        return DependencyEvidence(
            success=False,
            requested=requested,
            approved=approved,
            already_installed=already_installed or [],
            summary=summary,
            stdout=result[1][-4000:],
            stderr=result[2][-4000:],
            python_executable=str(self._venv_python()),
        )
