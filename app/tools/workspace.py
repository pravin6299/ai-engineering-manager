import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspacePathError(ValueError):
    """Raised when an operation tries to escape the project workspace."""


class WorkspaceTool:
    def __init__(self, root: Path | str = "workspace") -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list_files(self) -> list[str]:
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def read_file(self, relative_path: str) -> str:
        return self._resolve(relative_path).read_text(encoding="utf-8")

    def create_file(self, relative_path: str, content: str) -> None:
        path = self._resolve(relative_path)
        if path.exists():
            raise FileExistsError(f"Workspace file already exists: {relative_path}")
        self._write(path, relative_path, content)

    def update_file(self, relative_path: str, content: str) -> None:
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"Workspace file does not exist: {relative_path}")
        self._write(path, relative_path, content)

    def write_file(self, relative_path: str, content: str) -> None:
        path = self._resolve(relative_path)
        self._write(path, relative_path, content)

    def _resolve(self, relative_path: str) -> Path:
        requested = Path(relative_path)
        if requested.is_absolute():
            raise WorkspacePathError("Absolute paths are not allowed")
        candidate = (self.root / requested).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(f"Path escapes workspace: {relative_path}") from exc
        if candidate == self.root:
            raise WorkspacePathError("A file path is required")
        return candidate

    def _write(self, path: Path, relative_path: str, content: str) -> None:
        action = "modifying" if path.exists() else "creating"
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("WORKSPACE TOOL: %s %s", action, relative_path)
        path.write_text(content, encoding="utf-8")
