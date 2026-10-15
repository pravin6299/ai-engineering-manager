import ast
import hashlib
import json
from pathlib import Path

from app.tools.workspace import WorkspacePathError, WorkspaceTool

MAX_FILES = 200
MAX_FILE_BYTES = 64 * 1024
MAX_CONTENT_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
SENSITIVE_NAMES = {"secret", "secrets", "credential", "credentials", "private_key"}


class ProjectContextError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _has_embedded_secret(content: str) -> bool:
    if "-----BEGIN " in content and "PRIVATE KEY-----" in content:
        return True
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            names = [node.target.id] if isinstance(node.target, ast.Name) else []
            value = node.value
        else:
            continue
        if value is not None:
            literals = [
                item.value for item in ast.walk(value)
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if literals and any(
                term in name.lower()
                for name in names
                for term in ("secret", "password", "api_key", "token", "credential")
            ):
                return True
            if any("://" in literal and "@" in literal for literal in literals):
                return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and any(term in key.value.lower() for term in ("secret", "password", "api_key", "token", "credential"))
                    and any(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in ast.walk(value))
                ):
                    return True
        if isinstance(node, ast.keyword) and node.arg:
            if any(term in node.arg.lower() for term in ("secret", "password", "api_key", "credential")):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    return True
    return False


class BackendProjectContext:
    def __init__(self, workspace: WorkspaceTool) -> None:
        self.workspace = workspace
        self.app_root = workspace.root / "backend" / "app"

    def _source_path(self, relative_path: str) -> Path:
        parts = relative_path.split("/")
        if (
            len(parts) < 3
            or parts[:2] != ["backend", "app"]
            or not relative_path.endswith(".py")
            or "\\" in relative_path
            or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
            or any(part in {"__pycache__", "venv", "env"} for part in parts)
            or Path(parts[-1]).stem.lower() in SENSITIVE_NAMES
        ):
            raise ProjectContextError("Unauthorized backend source path")
        candidate = self.workspace.root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ProjectContextError("Symlinked source paths are not allowed")
        try:
            resolved = self.workspace._resolve(relative_path)
            resolved.relative_to(self.app_root)
        except (WorkspacePathError, ValueError) as exc:
            raise ProjectContextError("Unauthorized backend source path") from exc
        return resolved

    def get(self, requested_files: list[str] | None = None) -> dict:
        requested = requested_files or []
        if len(requested) > MAX_FILES:
            raise ProjectContextError("Too many requested files", 413)
        for path in requested:
            self._source_path(path)

        if not self.app_root.is_dir() or self.app_root.is_symlink():
            raise ProjectContextError("Backend application directory not found", 404)
        source_paths = sorted(
            path.relative_to(self.workspace.root).as_posix()
            for path in self.app_root.rglob("*.py")
            if "__pycache__" not in path.parts
            and not path.name.startswith(".")
            and path.stem.lower() not in SENSITIVE_NAMES
        )
        if len(source_paths) > MAX_FILES:
            raise ProjectContextError("Too many backend source files", 413)
        available = set(source_paths)
        if any(path not in available for path in requested):
            raise ProjectContextError("Requested source file not found", 404)

        metadata = []
        contents = {}
        for relative_path in source_paths:
            path = self._source_path(relative_path)
            if not path.is_file():
                raise ProjectContextError("Backend source file not found", 404)
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ProjectContextError("Backend source file exceeds size limit", 413)
            data = path.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise ProjectContextError("Backend source file exceeds size limit", 413)
            metadata.append({"path": relative_path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
            if relative_path in requested:
                if len(data) > MAX_CONTENT_BYTES:
                    raise ProjectContextError("Requested file content exceeds size limit", 413)
                try:
                    content = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ProjectContextError("Requested source is not UTF-8") from exc
                if _has_embedded_secret(content):
                    raise ProjectContextError("Requested source may contain credentials")
                contents[relative_path] = content

        result = {
            "project_root": str(self.workspace.root / "backend"),
            "canonical_app": "backend.app.main",
            "existing_files": source_paths,
            "file_metadata": metadata,
            "file_contents": contents,
            "architecture_rules": [
                "Import backend application code using backend.app.*.",
                "Use Base and database configuration from backend.app.database.",
                "Reuse models from backend.app.models; do not define a second User or Base.",
            ],
        }
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ProjectContextError("Project context exceeds response size limit", 413)
        return result
