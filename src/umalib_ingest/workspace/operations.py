"""TASK-013 工作区操作日志与交付记录（M06）。

操作记录保存恢复所需的批准包、计划、预览和 Writer result 的规范内容及 hash；
规范请求指纹覆盖全部人工选择与绑定；记录留在私有工作区，不进公开源码包。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from umalib_ingest.workspace.errors import WorkspaceManifestError
from umalib_ingest.workspace.models import (
    DeliveryRecord,
    OperationRecord,
    canonical_json,
    canonical_sha256,
    load_json_file,
)
from umalib_ingest.workspace.store import atomic_write_json

_STAGES = ("PREPARED", "CANDIDATE_VERIFIED", "COMMITTED", "ABANDONED")


def build_request_fingerprint(
    workspace_id: str,
    item_id: int,
    package_sha256: str,
    parent_revision: int,
    parent_head_sha256: str | None,
    choices: dict[str, Any],
    plan_sha256: str,
    preview_sha256: str,
    candidate_file_name: str,
) -> str:
    """Canonical fingerprint of one write request (all human decisions)."""
    return canonical_sha256({
        "workspace_id": workspace_id,
        "item_id": item_id,
        "package_sha256": package_sha256,
        "parent_revision": parent_revision,
        "parent_head_sha256": parent_head_sha256,
        "choices": choices,
        "plan_sha256": plan_sha256,
        "preview_sha256": preview_sha256,
        "candidate_file_name": candidate_file_name,
    })


class OperationLog:
    """Persistent operation records under operations/<uuid>.json."""

    def __init__(self, operations_dir: Path) -> None:
        self._dir = operations_dir

    def path(self, operation_id: str) -> Path:
        return self._dir / f"{operation_id}.json"

    def list_records(self) -> list[OperationRecord]:
        if not self._dir.exists():
            return []
        records: list[OperationRecord] = []
        for entry in sorted(self._dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                records.append(OperationRecord.from_dict(load_json_file(entry)))
            except WorkspaceManifestError:
                continue
        return records

    def get(self, operation_id: str) -> OperationRecord | None:
        path = self.path(operation_id)
        if not path.exists():
            return None
        try:
            return OperationRecord.from_dict(load_json_file(path))
        except WorkspaceManifestError:
            return None

    def write(self, record: OperationRecord) -> None:
        """Atomic upsert (only for records this process created/owns)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        if self.path(record.operation_id).exists() and record.stage == "PREPARED":
            raise WorkspaceManifestError("PREPARED 记录已存在，不能覆盖")
        atomic_write_json(self.path(record.operation_id), record.to_dict())


class DeliveryLog:
    """Persisted verified deliveries under deliveries/<uuid>.json."""

    def __init__(self, deliveries_dir: Path) -> None:
        self._dir = deliveries_dir

    def list_records(self) -> list[DeliveryRecord]:
        if not self._dir.exists():
            return []
        records: list[DeliveryRecord] = []
        for entry in sorted(self._dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".json":
                continue
            try:
                records.append(DeliveryRecord.from_dict(load_json_file(entry)))
            except WorkspaceManifestError:
                continue
        records.sort(key=lambda record: record.created_at)
        return records

    def write(self, record: DeliveryRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{record.operation_id}.json"
        if path.exists():
            raise WorkspaceManifestError("交付记录已存在，不能覆盖")
        atomic_write_json(path, record.to_dict())
