import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from app.models.task import RunEvidence

logger = logging.getLogger(__name__)


def find_node_toolchain() -> tuple[Path, Path] | None:
    candidates: list[Path] = []
    nvm_bin = os.getenv("NVM_BIN")
    if nvm_bin:
        candidates.append(Path(nvm_bin))
    nvm_root = Path(os.getenv("NVM_DIR") or Path.home() / ".nvm") / "versions" / "node"
    if nvm_root.is_dir():
        versions = [
            path for path in nvm_root.iterdir()
            if path.is_dir() and re.fullmatch(r"v\d+\.\d+\.\d+", path.name)
        ]
        versions.sort(
            key=lambda path: tuple(int(part) for part in path.name[1:].split(".")),
            reverse=True,
        )
        candidates.extend(path / "bin" for path in versions)
    npm_on_path = shutil.which("npm")
    if npm_on_path:
        candidates.append(Path(npm_on_path).parent)
    for bin_dir in candidates:
        node, npm = bin_dir / "node", bin_dir / "npm"
        if all(path.is_file() and os.access(path, os.X_OK) for path in (node, npm)):
            return node, npm
    return None


class FrontendTestRunner:
    def __init__(self, workspace_root: Path | str = "workspace", timeout: float = 60) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.frontend_root = self.workspace_root / "frontend"
        self.timeout = timeout

    async def run(self, test_paths: list[str]) -> RunEvidence:
        targets = self._validated_targets(test_paths)
        try:
            executable = self._vitest_executable()
        except ValueError as exc:
            return RunEvidence(passed=False, exit_code=127, summary=str(exc), stderr=str(exc))
        logger.info("FRONTEND TEST RUNNER: running %s", ", ".join(targets))
        try:
            toolchain = find_node_toolchain()
            node = toolchain[0] if toolchain else shutil.which("node")
            runtime_path = os.pathsep.join((str(Path(node).parent), os.defpath)) if node else os.defpath
            process = await asyncio.create_subprocess_exec(str(executable), "run", *targets, "--reporter=default", "--config", ".agent-vitest.config.mjs", cwd=self.frontend_root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={"CI": "1", "NO_COLOR": "1", "PATH": runtime_path})
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            exit_code = process.returncode or 0
        except TimeoutError:
            process.kill()
            await process.communicate()
            return RunEvidence(passed=False, exit_code=124, summary=f"Frontend tests timed out after {self.timeout:g} seconds")
        except OSError as exc:
            return RunEvidence(passed=False, exit_code=127, summary="Frontend test runner could not start", stderr=str(exc))
        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        return RunEvidence(passed=exit_code == 0, exit_code=exit_code, summary=self._summary(stdout, stderr, exit_code), stdout=stdout[-4000:], stderr=stderr[-4000:])

    @staticmethod
    def _validated_targets(test_paths: list[str]) -> tuple[str, ...]:
        if not test_paths:
            raise ValueError("Frontend task test paths are required")
        targets = []
        for value in test_paths:
            path = Path(value)
            normalized = path.as_posix()
            if path.is_absolute() or ".." in path.parts or not normalized.startswith("frontend/") or not re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", normalized):
                raise ValueError(f"Invalid frontend test path: {value}")
            targets.append(normalized.removeprefix("frontend/"))
        return tuple(targets)

    def _vitest_executable(self) -> Path:
        name = "vitest.cmd" if os.name == "nt" else "vitest"
        executable = (self.frontend_root / "node_modules" / ".bin" / name).resolve()
        try:
            executable.relative_to(self.frontend_root)
        except ValueError as exc:
            raise ValueError("Vitest executable escapes frontend workspace") from exc
        if not executable.is_file():
            raise ValueError("Approved frontend Vitest executable is not installed")
        return executable

    @staticmethod
    def _summary(stdout: str, stderr: str, exit_code: int) -> str:
        match = re.search(r"Tests\s+(\d+\s+(?:passed|failed))", f"{stdout}\n{stderr}")
        return match.group(1) if match else ("Frontend tests passed" if exit_code == 0 else "Frontend tests failed")
