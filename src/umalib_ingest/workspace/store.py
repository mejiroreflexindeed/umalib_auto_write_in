"""TASK-013 工作区存储：创建/加载/CAS、原子写入与系统文件锁（M02）。

- 原子写入：同目录临时文件 + flush/fsync + os.replace；失败保留旧版本。
- 排他锁：Windows 使用 msvcrt.locking（阻塞式系统锁），POSIX 使用 fcntl.flock；
  锁文件不删除来“修复占用”，进程退出后由操作系统释放。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from umalib_ingest.workspace.errors import (
    WorkspaceConflictError,
    WorkspaceLockedError,
    WorkspaceManifestError,
    WorkspaceNotFoundError,
)
from umalib_ingest.workspace.models import (
    WorkspaceManifest,
    canonical_json,
    utc_now,
)
from umalib_ingest.workspace.paths import WorkspacePaths, validate_workspace_id


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace a JSON file; the old file survives failures."""
    payload = canonical_json(data).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".ws-", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


class InstanceLock:
    """A system file lock for one workspace; released by OS on process exit."""

    _registry_lock = threading.RLock()
    _held: dict[str, tuple[Any, int]] = {}

    def __init__(self, lock_path: Path, *, allow_reentrant: bool = False) -> None:
        self._lock_path = lock_path
        self._handle: Any = None
        self._key = str(lock_path.resolve())
        self._allow_reentrant = allow_reentrant

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        # WorkspaceService CAS operations acquire the same lock while the
        # launcher keeps its lifetime lock.  Share the already-held OS handle
        # inside this process, while separate processes still contend on the
        # real file lock.
        if self._allow_reentrant:
            with self._registry_lock:
                held = self._held.get(self._key)
                if held is not None:
                    handle, count = held
                    self._held[self._key] = (handle, count + 1)
                    self._handle = handle
                    return
        handle = open(self._lock_path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            handle.close()
            raise WorkspaceLockedError(self._lock_path.name) from None
        with self._registry_lock:
            self._held[self._key] = (handle, 1)
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        with self._registry_lock:
            held = self._held.get(self._key)
            if self._allow_reentrant and held is not None and held[0] is self._handle and held[1] > 1:
                self._held[self._key] = (held[0], held[1] - 1)
                self._handle = None
                return
            self._held.pop(self._key, None)
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "InstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class WorkspaceStore:
    """Create/load/update workspace manifests inside a demo root."""

    def __init__(self, paths: WorkspacePaths) -> None:
        self._paths = paths

    def create(self, mode: str, name: str) -> WorkspaceManifest:
        """Create a new workspace directory, manifest and Inbox schema.

        The Inbox itself is initialized by the caller (service layer) so this
        store stays schema-free.
        """
        workspace_id = str(uuid.uuid4()).lower()
        directory = self._paths.workspace_dir(workspace_id)
        directory.mkdir(parents=True, exist_ok=False)
        now = utc_now()
        manifest = WorkspaceManifest(
            workspace_id=workspace_id,
            name=name or "我的录入工作区",
            mode=mode,
            base_source_id="synthetic" if mode == "synthetic" else "main-copy",
            base_sha256="",
            revision=0,
            head=None,
            pending_operation_id=None,
            created_at=now,
            updated_at=now,
        )
        self._write_manifest(manifest)
        return manifest

    def load(self, workspace_id: str) -> WorkspaceManifest:
        """Load and strictly validate the manifest."""
        validate_workspace_id(workspace_id)
        manifest_path = self._paths.manifest_path(workspace_id)
        if not manifest_path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkspaceManifestError(f"workspace.json 损坏: {manifest_path}") from None
        manifest = WorkspaceManifest.from_dict(data)
        if manifest.workspace_id != workspace_id:
            raise WorkspaceManifestError("workspace.json 的 workspace_id 与路径不符")
        return manifest

    def load_bytes(self, workspace_id: str) -> bytes:
        """Return the exact manifest bytes currently on disk (single read)."""
        validate_workspace_id(workspace_id)
        manifest_path = self._paths.manifest_path(workspace_id)
        if not manifest_path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        return manifest_path.read_bytes()

    def compare_and_swap(
        self,
        workspace_id: str,
        expected_revision: int,
        expected_manifest_sha256: str,
        new_manifest: WorkspaceManifest,
    ) -> WorkspaceManifest:
        """Atomically write the new manifest iff bytes match the expectation.

        The same bytes are hashed and compared; no second read.
        """
        from umalib_ingest.workspace.models import canonical_sha256, canonical_json

        manifest_path = self._paths.manifest_path(workspace_id)
        if not manifest_path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        with InstanceLock(self._paths.lock_file(workspace_id), allow_reentrant=True):
            current_bytes = manifest_path.read_bytes()
            current = WorkspaceManifest.from_dict(json.loads(current_bytes.decode("utf-8")))
            actual_sha = canonical_sha256(
                {key: getattr(current, key) for key in (
                    "schema_version", "workspace_id", "name", "mode", "base_source_id",
                    "base_sha256", "revision", "head", "pending_operation_id",
                    "created_at", "updated_at", "restored_from", "history_workspace_ids",
                )}
            )
            if actual_sha != expected_manifest_sha256:
                raise WorkspaceConflictError("工作区清单已变化，请刷新后重试")
            if current.revision != expected_revision:
                raise WorkspaceConflictError("工作区 revision 已变化，请刷新后重试")
            payload = new_manifest.to_json().encode("utf-8")
            fd, temp_name = tempfile.mkstemp(
                dir=str(manifest_path.parent), prefix=".ws-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, str(manifest_path))
            except BaseException:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
                raise
        return new_manifest

    def _write_manifest(self, manifest: WorkspaceManifest) -> None:
        atomic_write_json(self._paths.manifest_path(manifest.workspace_id), manifest.to_dict())

    # ---- launcher.json ----

    def read_launcher(self) -> dict[str, Any]:
        """Return launcher.json or an empty structure; never contains secrets."""
        path = self._paths.launcher_json
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise WorkspaceManifestError("launcher.json 损坏") from None
        if not isinstance(data, dict):
            raise WorkspaceManifestError("launcher.json 顶层必须是对象")
        return data

    def write_launcher(self, data: dict[str, Any]) -> None:
        self._paths.ensure_root()
        atomic_write_json(self._paths.launcher_json, data)

    def remember_last(self, mode: str, workspace_id: str) -> None:
        """Update last_workspace_by_mode without clobbering the other mode."""
        data = self.read_launcher()
        by_mode = data.get("last_workspace_by_mode")
        if not isinstance(by_mode, dict):
            by_mode = {}
        by_mode[mode] = workspace_id
        data["last_workspace_by_mode"] = by_mode
        self.write_launcher(data)

    def last_workspace(self, mode: str) -> str | None:
        """Return the last workspace id for a mode or None."""
        data = self.read_launcher()
        by_mode = data.get("last_workspace_by_mode")
        if not isinstance(by_mode, dict):
            return None
        value = by_mode.get(mode)
        return value if isinstance(value, str) and value else None
