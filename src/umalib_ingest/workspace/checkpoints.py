"""TASK-013 检查点服务（M10）。

只处理本工具生成的检查点目录：明确清单 + 逐文件 SHA；SQLite backup 生成一致
Inbox 副本；恢复时校验后新建 workspace_uuid，不覆盖原工作区；原操作/包/choices
字节不改；restore-provenance.json 记录来源清单与历史 hash。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from umalib_ingest.workspace.errors import (
    CheckpointError,
    WorkspaceBusyError,
    WorkspaceManifestError,
)
from umalib_ingest.workspace.models import (
    WorkspaceManifest,
    canonical_sha256,
    utc_now,
)
from umalib_ingest.workspace.paths import WorkspacePaths
from umalib_ingest.workspace.store import InstanceLock, WorkspaceStore, atomic_write_json

CHECKPOINT_SCHEMA_VERSION = "1.0"
_SNAPSHOT_EXCLUDED_NOTE = "snapshots 未包含；恢复后 Watcher 默认禁用，需重新配置后再扫描"

_COPY_SOURCES = (
    ("workspace.json", True),
    ("restore-provenance.json", False),
    ("inbox.db", True),
    ("base.db", False),
    ("watcher/state.json", False),
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CheckpointService:
    """Create/restore consistent checkpoint directories."""

    def __init__(
        self,
        paths: WorkspacePaths,
        store: WorkspaceStore,
        coordinator,
        repository_factory,
    ) -> None:
        self._paths = paths
        self._store = store
        self._coordinator = coordinator
        self._repository_factory = repository_factory

    def create(self, workspace_id: str, confirmed: bool) -> dict[str, Any]:
        """Create a checkpoint directory while maintenance holds new writes out."""
        if type(confirmed) is not bool or confirmed is not True:
            raise CheckpointError("创建检查点必须显式确认")
        manifest = self._store.load(workspace_id)
        if manifest.pending_operation_id is not None:
            raise CheckpointError("存在待恢复操作，先处理再备份")
        checkpoint_id = str(uuid.uuid4()).lower()
        if not self._coordinator.enter_maintenance():
            raise WorkspaceBusyError("已有检查点在进行")
        try:
            target_dir = self._paths.checkpoints_dir(workspace_id) / checkpoint_id
            staging = target_dir.with_name(f".{checkpoint_id}-staging")
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            try:
                manifest_map = self._copy_sources(workspace_id, staging)
                self._copy_choices_and_operations(workspace_id, staging, manifest_map)
                self._copy_operator_state(workspace_id, staging, manifest_map)
                self._copy_referenced_candidates(workspace_id, staging, manifest_map)
                self._write_checkpoint_json(
                    staging, checkpoint_id, workspace_id, manifest, manifest_map,
                )
                manifest_map["checkpoint.json"] = sha256_file(staging / "checkpoint.json")
                target_dir.mkdir()
                # 整体发布：逐个移动，失败只清理 staging
                for entry in sorted(staging.iterdir()):
                    shutil.move(str(entry), str(target_dir / entry.name))
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        finally:
            self._coordinator.leave_maintenance()
        return {
            "checkpoint_id": checkpoint_id,
            "revision": manifest.revision,
            "manifest_sha256": manifest_map["workspace.json"],
        }

    def _copy_sources(self, workspace_id: str, staging: Path) -> dict[str, str]:
        file_map: dict[str, str] = {}
        for rel, required in _COPY_SOURCES:
            source = self._paths.sub_path(workspace_id, *rel.split("/"))
            if not source.exists():
                if required:
                    raise CheckpointError(f"检查点缺少必需文件: {rel}")
                continue
            destination = staging / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            if rel == "inbox.db":
                self._backup_inbox(source, destination)
            else:
                shutil.copyfile(source, destination)
            file_map[rel] = sha256_file(destination)
        return file_map

    @staticmethod
    def _backup_inbox(source: Path, destination: Path) -> None:
        """Use the SQLite backup API for a consistent Inbox copy."""
        source_conn = sqlite3.connect(
            source.resolve().as_uri() + "?mode=ro", uri=True
        )
        try:
            destination_conn = sqlite3.connect(str(destination))
            try:
                source_conn.backup(destination_conn)
            finally:
                destination_conn.close()
        finally:
            source_conn.close()

    def _copy_choices_and_operations(
        self, workspace_id: str, staging: Path, file_map: dict[str, str]
    ) -> None:
        for sub, name in (("choices", "choices"), ("operations", "operations"),
                          ("deliveries", "deliveries")):
            source_dir = self._paths.sub_path(workspace_id, sub)
            if not source_dir.exists():
                continue
            destination_dir = staging / sub
            destination_dir.mkdir(parents=True, exist_ok=True)
            for entry in sorted(source_dir.iterdir()):
                if entry.is_file() and entry.suffix == ".json":
                    target = destination_dir / entry.name
                    shutil.copyfile(entry, target)
                    file_map[f"{sub}/{entry.name}"] = sha256_file(target)

    def _copy_operator_state(
        self, workspace_id: str, staging: Path, file_map: dict[str, str]
    ) -> None:
        """Include operator source/thread progress, excluding bulky HTML snapshots."""
        workspace_dir = self._paths.workspace_dir(workspace_id)
        source_binding = workspace_dir / "source-binding.json"
        if source_binding.is_file():
            target = staging / "source-binding.json"
            shutil.copyfile(source_binding, target)
            file_map["source-binding.json"] = sha256_file(target)
        threads = workspace_dir / "threads"
        if not threads.is_dir():
            return
        for source in sorted(threads.rglob("*.json")):
            relative = source.relative_to(workspace_dir)
            if "snapshots" in relative.parts:
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            file_map[relative.as_posix()] = sha256_file(target)

    def _copy_referenced_candidates(
        self, workspace_id: str, staging: Path, file_map: dict[str, str]
    ) -> None:
        """复制所有被日志/交付/HEAD 引用的候选文件（不做去重优化）。"""
        referenced: set[str] = set()
        manifest = self._store.load(workspace_id)
        if manifest.head:
            referenced.add(manifest.head["candidate_file_name"])
        for sub in ("operations", "deliveries"):
            source_dir = self._paths.sub_path(workspace_id, sub)
            if not source_dir.exists():
                continue
            for entry in sorted(source_dir.iterdir()):
                if not entry.is_file() or entry.suffix != ".json":
                    continue
                try:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                name = data.get("candidate_file_name")
                if isinstance(name, str) and name:
                    referenced.add(name)
        candidates_dir = self._paths.candidates_dir(workspace_id)
        destination_dir = staging / "candidates"
        for name in sorted(referenced):
            source = candidates_dir / name
            if not source.exists():
                raise CheckpointError(f"检查点引用候选缺失: {name}")
            destination_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination_dir / name)
            file_map[f"candidates/{name}"] = sha256_file(destination_dir / name)

    def _write_checkpoint_json(
        self,
        staging: Path,
        checkpoint_id: str,
        workspace_id: str,
        manifest: WorkspaceManifest,
        file_map: dict[str, str],
    ) -> None:
        payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "checkpoint_id": checkpoint_id,
            "source_workspace_id": workspace_id,
            "revision": manifest.revision,
            "created_at": utc_now(),
            "files": file_map,
            "snapshots_note": _SNAPSHOT_EXCLUDED_NOTE,
        }
        atomic_write_json(staging / "checkpoint.json", payload)

    # ---- restore ----

    def restore(self, workspace_id: str, checkpoint_id: str, confirmed: bool) -> dict[str, Any]:
        """Restore into a NEW workspace uuid; the original is never modified."""
        if type(confirmed) is not bool or confirmed is not True:
            raise CheckpointError("恢复必须显式确认")
        manifest = self._store.load(workspace_id)
        checkpoint_dir = self._paths.checkpoints_dir(workspace_id) / checkpoint_id
        if not checkpoint_dir.is_dir():
            raise CheckpointError("检查点不存在")
        payload = json.loads((checkpoint_dir / "checkpoint.json").read_text(encoding="utf-8"))
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError("检查点 schema 版本不受支持")
        files: dict[str, str] = payload.get("files") or {}
        # 校验全部清单文件
        for rel, want in files.items():
            path = checkpoint_dir / rel
            if not path.exists() or sha256_file(path) != want:
                raise CheckpointError(f"检查点文件校验失败: {rel}")
        new_id = str(uuid.uuid4()).lower()
        new_dir = self._paths.workspace_dir(new_id)
        new_dir.mkdir(parents=True, exist_ok=False)
        try:
            self._restore_into(workspace_id, new_id, checkpoint_dir, payload)
        except BaseException:
            shutil.rmtree(new_dir, ignore_errors=True)
            raise
        self._store.remember_last(manifest.mode, new_id)
        return {"new_workspace_id": new_id, "restored_from": f"checkpoint:{checkpoint_id}"}

    def _restore_into(
        self, source_id: str, new_id: str, checkpoint_dir: Path, payload: dict[str, Any]
    ) -> None:
        new_dir = self._paths.workspace_dir(new_id)
        source_manifest = json.loads(
            (checkpoint_dir / "workspace.json").read_text(encoding="utf-8")
        )
        restored_manifest = WorkspaceManifest.from_dict(source_manifest)
        restored_manifest.workspace_id = new_id
        restored_manifest.restored_from = f"checkpoint:{payload['checkpoint_id']}"
        history = list(restored_manifest.history_workspace_ids or [])
        for candidate in (source_id, *history):
            if candidate not in history:
                history.append(candidate)
        restored_manifest.history_workspace_ids = history
        restored_manifest.updated_at = utc_now()
        # 复制清单文件（原字节不改）
        for rel in ("inbox.db", "base.db", "watcher/state.json"):
            source = checkpoint_dir / rel
            if source.exists():
                target = new_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        for sub in ("choices", "operations", "deliveries", "candidates"):
            source = checkpoint_dir / sub
            if not source.exists():
                continue
            target = new_dir / sub
            shutil.copytree(source, target)
        # Operator-only files are optional for older checkpoints.  Every file
        # copied here was already verified against checkpoint.json above.
        operator_files = payload.get("files") or {}
        for relative in sorted(operator_files):
            if relative != "source-binding.json" and not relative.startswith("threads/"):
                continue
            source = checkpoint_dir / relative
            target = new_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        # provenance
        provenance = {
            "schema_version": "1.0",
            "restored_from": restored_manifest.restored_from,
            "history_workspace_ids": history,
            "files": payload.get("files") or {},
        }
        atomic_write_json(new_dir / "restore-provenance.json", provenance)
        atomic_write_json(new_dir / "workspace.json", restored_manifest.to_dict())
        # Inbox 恢复后校验可打开（只读）
        self._verify_inbox_readable(new_id)

    def _verify_inbox_readable(self, workspace_id: str) -> None:
        from umalib_ingest.inbox import InboxRepository

        inbox_path = self._paths.inbox_db(workspace_id)
        repository = InboxRepository(inbox_path)
        repository.list_items()
