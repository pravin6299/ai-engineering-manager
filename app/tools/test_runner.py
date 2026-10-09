import asyncio
import logging
import re
import sys
from pathlib import Path

from app.models.task import RunEvidence

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 8000


def truncate_test_output(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text

    half = MAX_OUTPUT_CHARS // 2
    return text[:half] + "\n\n... OUTPUT TRUNCATED ...\n\n" + text[-half:]


class BackendTestRunner:
    def __init__(self, workspace_root: Path | str = "workspace", timeout: float = 30) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.timeout = timeout

    async def run(
        self,
        test_paths: list[str] | None = None,
        python_executable: str | Path | None = None,
    ) -> RunEvidence:
        targets = self._validated_targets(test_paths)
        executable = self._validated_python(python_executable)
        logger.info(
            "TEST RUNNER: running %s",
            "task tests" if test_paths else "complete backend test suite",
        )
        command = (str(executable), "-m", "pytest", *targets, "-q")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self.workspace_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": str(self.workspace_root),
                },
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )
            exit_code = process.returncode or 0
        except TimeoutError:
            process.kill()
            await process.communicate()
            return RunEvidence(
                passed=False,
                exit_code=124,
                summary=f"Tests timed out after {self.timeout:g} seconds",
            )

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        summary = self._summary(stdout, stderr, exit_code)
        logger.info("TEST RUNNER: %s", summary)
        return RunEvidence(
            passed=exit_code == 0,
            exit_code=exit_code,
            summary=summary,
            stdout=truncate_test_output(stdout),
            stderr=truncate_test_output(stderr),
        )

    @staticmethod
    def _validated_targets(test_paths: list[str] | None) -> tuple[str, ...]:
        if not test_paths:
            return ("backend/tests",)
        targets: list[str] = []
        for test_path in test_paths:
            path = Path(test_path)
            normalized = path.as_posix()
            if (
                path.is_absolute()
                or ".." in path.parts
                or not normalized.startswith("backend/tests/test_")
                or path.suffix != ".py"
            ):
                raise ValueError(f"Invalid backend test path: {test_path}")
            targets.append(normalized)
        return tuple(targets)

    def _validated_python(self, python_executable: str | Path | None) -> Path:
        current = Path(sys.executable)
        if python_executable is None:
            return current
        requested = Path(python_executable).absolute()
        venv_directory = "Scripts" if sys.platform == "win32" else "bin"
        venv_name = "python.exe" if sys.platform == "win32" else "python"
        approved = (
            self.workspace_root / "backend" / ".venv" / venv_directory / venv_name
        ).absolute()
        if requested != approved or not requested.is_file():
            raise ValueError("TestRunner received an unapproved Python executable")
        return requested

    @staticmethod
    def _summary(stdout: str, stderr: str, exit_code: int) -> str:
        combined = f"{stdout}\n{stderr}"
        matches = re.findall(r"(?:^|\s)(\d+ (?:passed|failed|error|errors))(?:[ ,]|$)", combined)
        if matches:
            return ", ".join(dict.fromkeys(matches))
        return "Tests passed" if exit_code == 0 else "Tests failed"
