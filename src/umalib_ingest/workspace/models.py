"""TASK-013 工作区数据模型与规范 JSON（M02）。

所有持久 JSON 使用 UTF-8、固定键序的规范序列化；严格类型校验；拒绝未知
阶段与非整数 revision。原子替换只用于本任务自有元数据。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from umalib_ingest.workspace.errors import WorkspaceManifestError

MANIFEST_SCHEMA_VERSION = "1.0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_STAGES = ("PREPARED", "CANDIDATE_VERIFIED", "COMMITTED", "ABANDONED")
_OPERATION_KEYS = (
    "operation_id", "stage", "workspace_id", "item_id", "request_fingerprint",
    "request_choices", "parent_revision", "parent_head_sha256", "candidate_file_name",
    "plan_sha256", "preview_sha256", "result_sha256", "result", "error",
    "created_at", "updated_at",
)
_DELIVERY_KEYS = (
    "operation_id", "workspace_id", "item_id", "candidate_file_name",
    "candidate_sha256", "result_sha256", "article_name", "count_after",
    "created_at",
)
_MANIFEST_KEYS = (
    "schema_version", "workspace_id", "name", "mode", "base_source_id",
    "base_sha256", "revision", "head", "pending_operation_id", "created_at",
    "updated_at", "restored_from", "history_workspace_ids",
)
_CHOICES_KEYS = (
    "version", "item_id", "working_draft_sha256", "package_sha256", "updated_at",
    "note", "upload_time_seconds", "ordered_sources", "tag_choices",
    "content_mode", "formatted_body", "plain_loss_confirmed",
)


def utc_now() -> str:
    """Current UTC ISO-8601 timestamp with microseconds."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_hex(text: str) -> str:
    """Lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: dict[str, Any]) -> str:
    """Deterministic UTF-8 JSON with sorted keys and compact separators."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a dict."""
    return sha256_hex(canonical_json(value))


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def load_json_file(path: Any) -> dict[str, Any]:
    """Read and parse a JSON object file; raises with a clear message."""
    from pathlib import Path

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkspaceManifestError(f"无法读取 {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise WorkspaceManifestError(f"{path} 不是合法 JSON") from None
    if not isinstance(data, dict):
        raise WorkspaceManifestError(f"{path} 顶层必须是对象")
    return data


@dataclass(frozen=True)
class HeadBinding:
    """The verified current head: real source path, hash and revision."""

    operation_id: str | None
    candidate_file_name: str | None
    sha256: str | None
    revision: int

    @property
    def is_none(self) -> bool:
        return self.operation_id is None


@dataclass
class WorkspaceManifest:
    """Workspace metadata (v1); strictly validated on load."""

    schema_version: str = MANIFEST_SCHEMA_VERSION
    workspace_id: str = ""
    name: str = ""
    mode: str = "synthetic"
    base_source_id: str = "synthetic"
    base_sha256: str = ""
    revision: int = 0
    head: dict[str, Any] | None = None
    pending_operation_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    restored_from: str | None = None
    history_workspace_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in _MANIFEST_KEYS}

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def manifest_sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceManifest":
        unknown = set(data) - set(_MANIFEST_KEYS)
        if unknown:
            raise WorkspaceManifestError(f"workspace.json 含未知键: {sorted(unknown)}")
        if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise WorkspaceManifestError(
                f"workspace.json schema_version 不受支持: {data.get('schema_version')!r}"
            )
        manifest = cls()
        for key in _MANIFEST_KEYS:
            setattr(manifest, key, data.get(key))
        if not isinstance(manifest.workspace_id, str) or manifest.workspace_id == "":
            raise WorkspaceManifestError("workspace_id 缺失")
        if not isinstance(manifest.name, str):
            raise WorkspaceManifestError("name 必须是字符串")
        if manifest.mode not in ("synthetic", "real_copy"):
            raise WorkspaceManifestError(f"mode 非法: {manifest.mode!r}")
        if manifest.base_source_id not in ("synthetic", "main-copy"):
            raise WorkspaceManifestError(f"base_source_id 非法: {manifest.base_source_id!r}")
        if type(manifest.revision) is not int or manifest.revision < 0:
            raise WorkspaceManifestError("revision 必须是非负整数")
        if manifest.head is not None:
            head = manifest.head
            if not isinstance(head, dict):
                raise WorkspaceManifestError("head 必须是对象或 null")
            for key in ("operation_id", "candidate_file_name", "sha256"):
                value = head.get(key)
                if not isinstance(value, str) or value == "":
                    raise WorkspaceManifestError(f"head.{key} 必须是字符串")
            if not is_sha256(head["sha256"]):
                raise WorkspaceManifestError("head.sha256 格式非法")
        if manifest.pending_operation_id is not None and (
            not isinstance(manifest.pending_operation_id, str)
            or manifest.pending_operation_id == ""
        ):
            raise WorkspaceManifestError("pending_operation_id 格式非法")
        if manifest.restored_from is not None and not isinstance(manifest.restored_from, str):
            raise WorkspaceManifestError("restored_from 格式非法")
        if not isinstance(manifest.history_workspace_ids, list) or any(
            not isinstance(value, str) for value in manifest.history_workspace_ids
        ):
            raise WorkspaceManifestError("history_workspace_ids 必须是字符串数组")
        return manifest


@dataclass(frozen=True)
class ChoicesFile:
    """Human decisions bound to one working draft fingerprint (v1)."""

    version: int = 1
    item_id: int = 0
    working_draft_sha256: str = ""
    package_sha256: str | None = None
    updated_at: str = ""
    note: str | None = None
    upload_time_seconds: int = 0
    ordered_sources: tuple[str, ...] = ()
    tag_choices: tuple[dict[str, Any], ...] = ()
    content_mode: str = "plain"
    formatted_body: str | None = None
    plain_loss_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "item_id": self.item_id,
            "working_draft_sha256": self.working_draft_sha256,
            "package_sha256": self.package_sha256,
            "updated_at": self.updated_at,
            "note": self.note,
            "upload_time_seconds": self.upload_time_seconds,
            "ordered_sources": list(self.ordered_sources),
            "tag_choices": list(self.tag_choices),
            "content_mode": self.content_mode,
            "formatted_body": self.formatted_body,
            "plain_loss_confirmed": self.plain_loss_confirmed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChoicesFile":
        unknown = set(data) - set(_CHOICES_KEYS)
        if unknown:
            raise WorkspaceManifestError(f"choices 含未知键: {sorted(unknown)}")
        if data.get("version") != 1:
            raise WorkspaceManifestError("choices version 不受支持")
        item_id = data.get("item_id")
        if type(item_id) is not int or item_id < 1:
            raise WorkspaceManifestError("choices.item_id 必须是正整数")
        sha = data.get("working_draft_sha256")
        if not is_sha256(sha):
            raise WorkspaceManifestError("choices.working_draft_sha256 格式非法")
        pkg = data.get("package_sha256")
        if pkg is not None and not is_sha256(pkg):
            raise WorkspaceManifestError("choices.package_sha256 格式非法")
        mode = data.get("content_mode")
        if mode not in ("plain", "nga_ubb"):
            raise WorkspaceManifestError(f"choices.content_mode 非法: {mode!r}")
        sources = data.get("ordered_sources")
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise WorkspaceManifestError("choices.ordered_sources 必须是字符串数组")
        tags = data.get("tag_choices")
        if not isinstance(tags, list) or any(not isinstance(t, dict) for t in tags):
            raise WorkspaceManifestError("choices.tag_choices 必须是对象数组")
        upload = data.get("upload_time_seconds")
        if type(upload) is not int or upload < 0:
            raise WorkspaceManifestError("choices.upload_time_seconds 必须是非负整数")
        confirmed = data.get("plain_loss_confirmed")
        if type(confirmed) is not bool:
            raise WorkspaceManifestError("plain_loss_confirmed 必须是布尔值")
        return cls(
            item_id=item_id,
            working_draft_sha256=sha,
            package_sha256=pkg,
            updated_at=str(data.get("updated_at", "")),
            note=data.get("note"),
            upload_time_seconds=upload,
            ordered_sources=tuple(sources),
            tag_choices=tuple(tags),
            content_mode=mode,
            formatted_body=data.get("formatted_body"),
            plain_loss_confirmed=confirmed,
        )


@dataclass(frozen=True)
class OperationRecord:
    """One candidate-generation operation record (audit + recovery)."""

    operation_id: str
    stage: str
    workspace_id: str
    item_id: int
    request_fingerprint: str
    request_choices: dict[str, Any] | None
    parent_revision: int
    parent_head_sha256: str | None
    candidate_file_name: str
    plan_sha256: str
    preview_sha256: str
    result_sha256: str | None
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "stage": self.stage,
            "workspace_id": self.workspace_id,
            "item_id": self.item_id,
            "request_fingerprint": self.request_fingerprint,
            "request_choices": self.request_choices,
            "parent_revision": self.parent_revision,
            "parent_head_sha256": self.parent_head_sha256,
            "candidate_file_name": self.candidate_file_name,
            "plan_sha256": self.plan_sha256,
            "preview_sha256": self.preview_sha256,
            "result_sha256": self.result_sha256,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OperationRecord":
        unknown = set(data) - set(_OPERATION_KEYS)
        if unknown:
            raise WorkspaceManifestError(f"operation 含未知键: {sorted(unknown)}")
        stage = data.get("stage")
        if stage not in _OPERATION_STAGES:
            raise WorkspaceManifestError(f"operation stage 非法: {stage!r}")
        item_id = data.get("item_id")
        if type(item_id) is not int or item_id < 1:
            raise WorkspaceManifestError("operation.item_id 必须是正整数")
        for key in ("operation_id", "workspace_id", "request_fingerprint", "candidate_file_name"):
            value = data.get(key)
            if not isinstance(value, str) or value == "":
                raise WorkspaceManifestError(f"operation.{key} 缺失")
        return cls(
            operation_id=data["operation_id"],
            stage=stage,
            workspace_id=data["workspace_id"],
            item_id=item_id,
            request_fingerprint=data["request_fingerprint"],
            request_choices=data.get("request_choices"),
            parent_revision=int(data.get("parent_revision", 0)),
            parent_head_sha256=data.get("parent_head_sha256"),
            candidate_file_name=data["candidate_file_name"],
            plan_sha256=str(data.get("plan_sha256", "")),
            preview_sha256=str(data.get("preview_sha256", "")),
            result_sha256=data.get("result_sha256"),
            result=data.get("result"),
            error=data.get("error"),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass(frozen=True)
class DeliveryRecord:
    """A persisted, verified delivery entry (not JS memory)."""

    operation_id: str
    workspace_id: str
    item_id: int
    candidate_file_name: str
    candidate_sha256: str
    result_sha256: str
    article_name: str
    count_after: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliveryRecord":
        unknown = set(data) - set(_DELIVERY_KEYS)
        if unknown:
            raise WorkspaceManifestError(f"delivery 含未知键: {sorted(unknown)}")
        return cls(**{key: data.get(key) for key in _DELIVERY_KEYS})
