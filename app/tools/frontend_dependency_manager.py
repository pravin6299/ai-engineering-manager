import asyncio
import json
import logging
import os
import re
from pathlib import Path

from app.models.task import DependencyEvidence
from app.tools.frontend_test_runner import FrontendTestRunner, find_node_toolchain

logger = logging.getLogger(__name__)


class FrontendDependencyManager:
    approved_packages = {"react", "react-dom", "react-router-dom", "vite", "vitest", "@vitejs/plugin-react", "@testing-library/react", "@testing-library/jest-dom", "@testing-library/user-event", "jsdom"}
    required_packages = ("react", "react-dom", "vite", "vitest", "@vitejs/plugin-react", "@testing-library/react", "@testing-library/jest-dom", "jsdom")
    package_pattern = re.compile(r"^(?P<name>(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*)(?P<version>@(?:[~^]?\d+(?:\.\d+){0,2}(?:-[a-z0-9.-]+)?|latest))?$", re.IGNORECASE)

    def __init__(self, workspace_root: Path | str = "workspace", timeout: float = 180) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.frontend_root = self.workspace_root / "frontend"
        self.timeout = timeout

    async def prepare(self, requested: list[str]) -> DependencyEvidence:
        try:
            requested_approved = self._validate_dependencies(requested)
            requested_names = {
                self.package_pattern.fullmatch(value).group("name").lower()
                for value in requested_approved
            }
            approved = self._validate_dependencies(
                requested_approved
                + [name for name in self.required_packages if name not in requested_names]
            )
            self._ensure_package_json()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return DependencyEvidence(success=False, summary=str(exc), stderr=str(exc))
        logger.info("FRONTEND DEPENDENCY MANAGER: requested %s", ", ".join(requested))
        logger.info("FRONTEND DEPENDENCY MANAGER: approved %s", ", ".join(approved))
        toolchain = find_node_toolchain()
        if not toolchain:
            return DependencyEvidence(success=False, requested=requested, approved=approved, summary="A matching Node/npm toolchain is not available")
        npm = str(toolchain[1])
        result = await self._run(Path(npm), "install", "--ignore-scripts", "--no-audit", "--no-fund", *approved)
        if result[0] != 0:
            return DependencyEvidence(success=False, requested=requested, approved=approved, summary="Approved frontend dependency installation failed", stdout=result[1][-4000:], stderr=result[2][-4000:], python_executable=npm)
        try:
            FrontendTestRunner(self.workspace_root)._vitest_executable()
        except ValueError as exc:
            return DependencyEvidence(success=False, requested=requested, approved=approved, summary=str(exc), stdout=result[1][-4000:], stderr=str(exc), python_executable=npm)
        return DependencyEvidence(success=True, requested=requested, approved=approved, installed=approved, summary="Frontend dependencies are ready", stdout=result[1][-4000:], stderr=result[2][-4000:], python_executable=npm)

    @classmethod
    def _validate_dependencies(cls, requested: list[str]) -> list[str]:
        approved = []
        for value in requested:
            if not isinstance(value, str) or not value or value.startswith(("-", ".", "/", "~")) or any(token in value for token in ("http:", "https:", "git+", "file:", "$", "`", ";", "&&", "||", " ")):
                raise ValueError(f"Frontend dependency is not approved: {value}")
            match = cls.package_pattern.fullmatch(value)
            if not match or match.group("name").lower() not in cls.approved_packages:
                raise ValueError(f"Frontend dependency is not approved: {value}")
            approved.append(value)
        return approved

    def _ensure_package_json(self) -> None:
        self.frontend_root.mkdir(parents=True, exist_ok=True)
        path = self.frontend_root / "package.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError("frontend/package.json must contain an object")
        data.update({"name": str(data.get("name") or "generated-frontend"), "private": True, "type": "module"})
        data["scripts"] = {"test": "vitest run"}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        (self.frontend_root / ".agent-vitest.config.mjs").write_text(
            "export default { test: { globals: true, environment: 'jsdom', setupFiles: ['./.agent-vitest.setup.js'] } };\n", encoding="utf-8"
        )
        (self.frontend_root / ".agent-vitest.setup.js").write_text("import '@testing-library/jest-dom/vitest';\n", encoding="utf-8")
    async def _run(self, executable: Path, *arguments: str) -> tuple[int, str, str]:
        runtime_path = os.pathsep.join((str(executable.parent), os.defpath))
        process = await asyncio.create_subprocess_exec(str(executable), *arguments, cwd=self.frontend_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"PATH": runtime_path, "npm_config_ignore_scripts": "true"})
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return 124, "", f"Frontend dependency installation timed out after {self.timeout:g} seconds"
        return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
