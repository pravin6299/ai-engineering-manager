import fcntl
import hashlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

from app.models.write_files import WriteFile
from app.tools.workspace import WorkspacePathError, WorkspaceTool

_process_lock = threading.Lock()
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
MAX_FILE_BYTES = 256 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024


class WriteRejection(Exception):
    def __init__(
        self, reason: str, paths: list[str], status_code: int = 400,
        files_written: list[str] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.paths = paths
        self.status_code = status_code
        self.files_written = files_written or []


class SafeFileWriter:
    def __init__(self, workspace: WorkspaceTool) -> None:
        self.workspace = workspace

    def _path(self, relative_path: str, agent: str) -> Path:
        parts = relative_path.split("/")
        if (
            len(parts) < 2
            or parts[0] != agent
            or "\\" in relative_path
            or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
            or any(part in {"node_modules", "__pycache__", "venv", "env"} for part in parts)
        ):
            raise WriteRejection("invalid_path", [relative_path])
        if agent == "backend" and not (
            (len(parts) >= 3 and parts[1] == "app" and relative_path.endswith(".py"))
            or (len(parts) == 3 and parts[1] == "tests" and parts[2].startswith("test_") and parts[2].endswith(".py"))
            or relative_path == "backend/requirements.txt"
        ):
            raise WriteRejection("unsafe_file_type", [relative_path])
        candidate = self.workspace.root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise WriteRejection("invalid_path", [relative_path])
        try:
            resolved = self.workspace._resolve(relative_path)
            resolved.relative_to(self.workspace.root / agent)
        except (WorkspacePathError, ValueError) as exc:
            raise WriteRejection("invalid_path", [relative_path]) from exc
        if resolved.is_dir():
            raise WriteRejection("invalid_path", [relative_path])
        return resolved

    def _validate(self, files: list[WriteFile], agent: str, trusted_update: bool) -> list[tuple[WriteFile, Path, bool]]:
        prepared = []
        seen = set()
        batch_size = 0
        for file in files:
            content_size = len(file.content.encode("utf-8"))
            batch_size += content_size
            if content_size > MAX_FILE_BYTES or batch_size > MAX_BATCH_BYTES:
                raise WriteRejection("file_too_large", [file.path], 413)
            path = self._path(file.path, agent)
            if path in seen:
                raise WriteRejection("duplicate_path", [file.path])
            seen.add(path)
            exists = path.exists()
            if exists and not stat.S_ISREG(path.stat().st_mode):
                raise WriteRejection("invalid_path", [file.path])
            if exists:
                if not file.allow_modify or not trusted_update:
                    raise WriteRejection("existing_file_without_authorization", [file.path])
                if file.expected_sha256 is None:
                    raise WriteRejection("missing_expected_hash", [file.path])
                if not _SHA256.fullmatch(file.expected_sha256):
                    raise WriteRejection("invalid_expected_hash", [file.path])
                if hashlib.sha256(path.read_bytes()).hexdigest() != file.expected_sha256:
                    raise WriteRejection("hash_mismatch", [file.path], 409)
            elif file.expected_sha256 is not None:
                raise WriteRejection("unexpected_expected_hash", [file.path])
            prepared.append((file, path, exists))
        return prepared

    @staticmethod
    def _stage(path: Path, data: bytes, mode: int | None = None) -> Path:
        fd, name = tempfile.mkstemp(prefix=".write-files-", dir=path.parent)
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                os.chmod(staged, mode)
            return staged
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    def write(self, files: list[WriteFile], agent: str, trusted_update: bool) -> list[str]:
        with _process_lock:
            lock_fd = os.open(
                self.workspace.root / ".write-files.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                return self._write_locked(files, agent, trusted_update)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)

    def _write_locked(self, files: list[WriteFile], agent: str, trusted_update: bool) -> list[str]:
        prepared = self._validate(files, agent, trusted_update)
        staged: list[tuple[WriteFile, Path, bool, Path, Path | None]] = []
        committed: list[tuple[Path, bool, Path | None, int]] = []
        rollback_failed = False
        try:
            for file, path, exists in prepared:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._path(file.path, agent)
                mode = stat.S_IMODE(path.stat().st_mode) if exists else None
                stage = self._stage(path, file.content.encode("utf-8"), mode)
                try:
                    backup = self._stage(path, path.read_bytes(), mode) if exists else None
                except BaseException:
                    stage.unlink(missing_ok=True)
                    raise
                staged.append((file, path, exists, stage, backup))
            for file, path, exists, stage, backup in staged:
                # Recheck after staging, while all cooperating requests hold the lock.
                self._validate([file], agent, trusted_update)
                inode = stage.stat().st_ino
                if exists:
                    os.replace(stage, path)
                else:
                    os.link(stage, path, follow_symlinks=False)
                committed.append((path, exists, backup, inode))
        except (OSError, WriteRejection) as exc:
            for path, existed, backup, inode in reversed(committed):
                try:
                    if existed and backup is not None:
                        os.replace(backup, path)
                    elif path.exists() and path.stat().st_ino == inode:
                        path.unlink()
                except OSError:
                    rollback_failed = True
            if rollback_failed:
                surviving = [
                    path.relative_to(self.workspace.root).as_posix()
                    for path, _, _, inode in committed
                    if path.exists() and path.stat().st_ino == inode
                ]
                raise WriteRejection(
                    "rollback_failed", [file.path for file in files], 500, surviving
                ) from exc
            if isinstance(exc, WriteRejection):
                raise
            raise WriteRejection("write_failed", [file.path for file in files], 500) from exc
        finally:
            for _, _, _, stage, backup in staged:
                stage.unlink(missing_ok=True)
                if backup is not None and not rollback_failed:
                    backup.unlink(missing_ok=True)
        return [file.path for file in files]
