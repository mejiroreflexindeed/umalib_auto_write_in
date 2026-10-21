"""TASK-014 W03/W04：operator 数据模型与严格 JSON（3.3.4 冻结形状）。

所有持久 JSON：ensure_ascii=False、sort_keys=True、紧凑分隔符、UTF-8；
严格拒绝未知顶层键与未知 schema version；时间 UTC ISO 带 +00:00。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TID_RE = re.compile(r"^[1-9][0-9]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TZ = "UTC+08:00"
_UTC_OFFSET = datetime.timedelta(hours=8)


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def validate_tid(value: Any) -> str:
    if not isinstance(value, str) or _TID_RE.fullmatch(value) is None:
        raise ValueError(f"非法 tid: {value!r}")
    return value


def utc_to_shanghai_lower_bound(date_text: str) -> str:
    """YYYY-MM-DD -> 该日 00:00:00+08:00 -> UTC ISO lower bound."""
    if _DATE_RE.fullmatch(date_text) is None:
        raise ValueError(f"非法日期: {date_text!r}")
    local = datetime.datetime.strptime(date_text, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone(_UTC_OFFSET))
    return local.astimezone(datetime.timezone.utc).isoformat()


class JsonValidationError(ValueError):
    pass


def _require_keys(data: dict, allowed: set, label: str) -> None:
    if not isinstance(data, dict):
        raise JsonValidationError(f"{label} 必须是对象")
    unknown = set(data) - allowed
    if unknown:
        raise JsonValidationError(f"{label} 含未知键: {sorted(unknown)}")


@dataclass(frozen=True)
class SourceBinding:
    """workspaces/<id>/source-binding.json（3.3.4 固定）。"""

    schema_version: str
    source_id: str
    path: str
    file_name: str
    sha256: str
    size: int
    bound_at: str
    schema_fingerprint_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "path": self.path,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size": self.size,
            "bound_at": self.bound_at,
            "schema_fingerprint_sha256": self.schema_fingerprint_sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceBinding":
        _require_keys(data, {"schema_version", "source_id", "path", "file_name",
                             "sha256", "size", "bound_at", "schema_fingerprint_sha256"},
                      "source-binding")
        if data.get("schema_version") != "1":
            raise JsonValidationError("source-binding schema_version 不受支持")
        for key in ("source_id", "path", "file_name", "bound_at"):
            if not isinstance(data.get(key), str) or data[key] == "":
                raise JsonValidationError(f"source-binding.{key} 非法")
        for key in ("sha256", "schema_fingerprint_sha256"):
            if not is_sha256(data.get(key)):
                raise JsonValidationError(f"source-binding.{key} 非法")
        if not isinstance(data.get("size"), int) or data["size"] < 1:
            raise JsonValidationError("source-binding.size 非法")
        return cls(**data)


@dataclass(frozen=True)
class ThreadEntry:
    """threads/index.json entries 元素（3.3.4 固定）。"""

    tid: str
    name: str
    enabled: bool
    canonical_url: str
    config_revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tid": self.tid, "name": self.name, "enabled": self.enabled,
            "canonical_url": self.canonical_url, "config_revision": self.config_revision,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadEntry":
        _require_keys(data, {"tid", "name", "enabled", "canonical_url",
                             "config_revision", "created_at", "updated_at"}, "thread entry")
        validate_tid(data.get("tid"))
        if not isinstance(data.get("name"), str) or not isinstance(data.get("canonical_url"), str):
            raise JsonValidationError("thread entry 字段类型非法")
        if type(data.get("enabled")) is not bool:
            raise JsonValidationError("thread entry.enabled 非法")
        if type(data.get("config_revision")) is not int or data["config_revision"] < 0:
            raise JsonValidationError("thread entry.config_revision 非法")
        return cls(**data)


@dataclass(frozen=True)
class ThreadIndex:
    """threads/index.json（3.3.4 固定）。"""

    schema_version: str
    revision: int
    active_tid: str | None
    entries: tuple[ThreadEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "active_tid": self.active_tid,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadIndex":
        _require_keys(data, {"schema_version", "revision", "active_tid", "entries"},
                      "threads/index")
        if data.get("schema_version") != "1":
            raise JsonValidationError("threads/index schema_version 不受支持")
        if type(data.get("revision")) is not int or data["revision"] < 0:
            raise JsonValidationError("index.revision 非法")
        active = data.get("active_tid")
        if active is not None:
            validate_tid(active)
        if not isinstance(data.get("entries"), list):
            raise JsonValidationError("index.entries 非法")
        entries = tuple(ThreadEntry.from_dict(e) for e in data["entries"])
        tids = [e.tid for e in entries]
        if len(tids) != len(set(tids)):
            raise JsonValidationError("index.entries 存在重复 tid")
        if active is not None and active not in tids:
            raise JsonValidationError("index.active_tid 不在 entries 中")
        entries = tuple(sorted(entries, key=lambda e: (e.created_at, e.tid)))
        return cls(schema_version=data["schema_version"], revision=data["revision"],
                   active_tid=active, entries=entries)


@dataclass(frozen=True)
class ThreadStart:
    """threads/<tid>/config.json start（三形状之一）。"""

    mode: str  # now | page | date
    page: int | None
    date: str | None
    timezone: str
    lower_bound_utc: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "page": self.page, "date": self.date,
            "timezone": self.timezone, "lower_bound_utc": self.lower_bound_utc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadStart":
        _require_keys(data, {"mode", "page", "date", "timezone", "lower_bound_utc"},
                      "config.start")
        mode = data.get("mode")
        if mode not in ("now", "page", "date"):
            raise JsonValidationError("config.start.mode 非法")
        tz = data.get("timezone")
        if tz != _TZ:
            raise JsonValidationError("config.start.timezone 必须为 UTC+08:00")
        page = data.get("page")
        date = data.get("date")
        lower = data.get("lower_bound_utc")
        if mode == "now":
            if page is not None or date is not None or lower is not None:
                raise JsonValidationError("now 模式其余键必须为 null")
        elif mode == "page":
            if type(page) is not int or page < 1 or date is not None or lower is not None:
                raise JsonValidationError("page 模式字段非法")
        else:
            if page is not None or not isinstance(date, str) or _DATE_RE.fullmatch(date) is None:
                raise JsonValidationError("date 模式字段非法")
            expected = utc_to_shanghai_lower_bound(date)
            if lower != expected:
                raise JsonValidationError("date.lower_bound_utc 与后端重算不一致")
        return cls(mode=mode, page=page, date=date, timezone=tz, lower_bound_utc=lower)


@dataclass(frozen=True)
class ThreadConfig:
    """threads/<tid>/config.json（3.3.4 固定）。"""

    schema_version: str
    revision: int
    tid: str
    canonical_url: str
    name: str
    enabled: bool
    start: ThreadStart
    start_confirmed: bool
    initialized: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "tid": self.tid,
            "canonical_url": self.canonical_url,
            "name": self.name,
            "enabled": self.enabled,
            "start": self.start.to_dict(),
            "start_confirmed": self.start_confirmed,
            "initialized": self.initialized,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThreadConfig":
        _require_keys(data, {"schema_version", "revision", "tid", "canonical_url",
                             "name", "enabled", "start", "start_confirmed",
                             "initialized", "created_at", "updated_at"}, "config")
        if data.get("schema_version") != "1":
            raise JsonValidationError("config schema_version 不受支持")
        validate_tid(data.get("tid"))
        if type(data.get("revision")) is not int or data["revision"] < 0:
            raise JsonValidationError("config.revision 非法")
        if not isinstance(data.get("canonical_url"), str) or not isinstance(data.get("name"), str):
            raise JsonValidationError("config 字段类型非法")
        if type(data.get("enabled")) is not bool or type(data.get("start_confirmed")) is not bool \
                or type(data.get("initialized")) is not bool:
            raise JsonValidationError("config 布尔字段非法")
        start = ThreadStart.from_dict(data["start"])
        return cls(schema_version=data["schema_version"], revision=data["revision"],
                   tid=data["tid"], canonical_url=data["canonical_url"],
                   name=data["name"], enabled=data["enabled"], start=start,
                   start_confirmed=data["start_confirmed"], initialized=data["initialized"],
                   created_at=data["created_at"], updated_at=data["updated_at"])


@dataclass(frozen=True)
class RunRecord:
    """threads/<tid>/runs/<uuid>.json（3.3.4 固定）。"""

    schema_version: str
    run_id: str
    workspace_id: str
    tid: str
    kind: str  # login | initialize_preview | scan | rescan
    status: str  # queued/running/awaiting_login/cancelling/succeeded/partial/cancelled/failed/interrupted
    request: dict[str, Any]
    progress: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: str
    updated_at: str

    _KINDS = ("login", "initialize_preview", "scan", "rescan")
    _STATUSES = ("queued", "running", "awaiting_login", "cancelling", "succeeded",
                 "partial", "cancelled", "failed", "interrupted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "workspace_id": self.workspace_id, "tid": self.tid, "kind": self.kind,
            "status": self.status, "request": self.request, "progress": self.progress,
            "result": self.result, "error": self.error,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        _require_keys(data, {"schema_version", "run_id", "workspace_id", "tid",
                             "kind", "status", "request", "progress", "result",
                             "error", "created_at", "updated_at"}, "run")
        if data.get("schema_version") != "1":
            raise JsonValidationError("run schema_version 不受支持")
        if data.get("kind") not in cls._KINDS or data.get("status") not in cls._STATUSES:
            raise JsonValidationError("run kind/status 非法")
        validate_tid(data.get("tid"))
        return cls(**data)
