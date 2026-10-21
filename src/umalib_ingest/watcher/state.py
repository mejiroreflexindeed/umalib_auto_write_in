"""Watcher state v1: strict, atomic, lock-protected (TASK-011)."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.nga_access.paths import ensure_distinct, validate_profile_dir, validate_snapshot_dir
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.local_paths import private_state_root

PRIVATE_STATE_ROOT = private_state_root()
DEFAULT_TID = 30099686
SCHEMA_VERSION = "1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_SUMMARY_KEYS = (
    "mode",
    "previous_last_page",
    "current_last_page",
    "scanned_pages",
    "new_floor_count",
    "unchanged_floor_count",
    "changed_floor_count",
    "discussion_count",
    "triggered_count",
    "imported_item_count",
    "changed_after_import_count",
    "dry_run",
)
_SUMMARY_COUNT_KEYS = (
    "new_floor_count",
    "unchanged_floor_count",
    "changed_floor_count",
    "discussion_count",
    "triggered_count",
    "imported_item_count",
    "changed_after_import_count",
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stable_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fail(message: str) -> NgaAccessError:
    return NgaAccessError("watcher_state_invalid", message)


def _invalid(value: object, field: str, expected: str) -> None:
    raise _fail(f"state 字段 {field} 非法：期望 {expected}")


def validate_state_path(path: str | Path) -> Path:
    """State file must live under private-state and not be a symlink."""
    target = Path(path)
    if not target.is_absolute():
        raise _fail("state 文件必须是绝对路径")
    if not target.is_relative_to(PRIVATE_STATE_ROOT):
        raise _fail(f"state 文件必须位于独立数据目录 {PRIVATE_STATE_ROOT} 下")
    if target.is_symlink():
        raise _fail("state 文件不得是符号链接")
    if str(target).endswith((".lock", ".tmp")):
        raise _fail("state 文件名不得以 .lock/.tmp 结尾")
    return target


@dataclass(frozen=True)
class WatcherConfig:
    """Fixed single-thread configuration."""

    tid: int = DEFAULT_TID
    canonical_thread_url: str = f"https://bbs.nga.cn/read.php?tid={DEFAULT_TID}"
    state_file: Path = PRIVATE_STATE_ROOT / "nga-watcher-state.json"
    profile_dir: Path | None = None
    snapshot_dir: Path | None = None
    inbox_db: Path | None = None
    navigation_delay_seconds: float = 3.0
    max_scan_pages: int = 12
    hard_max_scan_pages: int = 25

    def __post_init__(self) -> None:
        if self.tid != DEFAULT_TID:
            raise _fail(f"V1 只允许固定 tid={DEFAULT_TID}")
        if self.navigation_delay_seconds < 3.0:
            raise _fail("navigation_delay_seconds 不得低于 3")
        if not 1 <= self.max_scan_pages <= self.hard_max_scan_pages:
            raise _fail("max_scan_pages 必须在 1..hard_max_scan_pages")
        object.__setattr__(self, "state_file", validate_state_path(self.state_file))
        if self.snapshot_dir is not None:
            object.__setattr__(
                self, "snapshot_dir", validate_snapshot_dir(self.snapshot_dir)
            )
        if self.profile_dir is not None:
            object.__setattr__(
                self, "profile_dir", validate_profile_dir(self.profile_dir)
            )
            if self.snapshot_dir is not None:
                ensure_distinct(Path(self.profile_dir), Path(self.snapshot_dir))


@dataclass(frozen=True)
class KnownFloor:
    """One floor's watch evidence; never holds content/author/title."""

    pid: str
    content_sha256: str
    last_seen_page: int
    trigger_found: bool
    inbox_item_id: int | None
    imported_content_sha256: str | None
    changed_after_import: bool


@dataclass(frozen=True)
class WatcherState:
    """Watcher state v1."""

    schema_version: str
    tid: int
    canonical_thread_url: str
    last_page: int
    known_floors: dict[str, KnownFloor]
    baseline_created_at: str
    last_successful_scan_at: str | None
    last_result_summary: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tid": self.tid,
            "canonical_thread_url": self.canonical_thread_url,
            "last_page": self.last_page,
            "known_floors": {
                pid: {
                    "pid": floor.pid,
                    "content_sha256": floor.content_sha256,
                    "last_seen_page": floor.last_seen_page,
                    "trigger_found": floor.trigger_found,
                    "inbox_item_id": floor.inbox_item_id,
                    "imported_content_sha256": floor.imported_content_sha256,
                    "changed_after_import": floor.changed_after_import,
                }
                for pid, floor in sorted(self.known_floors.items())
            },
            "baseline_created_at": self.baseline_created_at,
            "last_successful_scan_at": self.last_successful_scan_at,
            "last_result_summary": self.last_result_summary,
        }

    def to_json(self) -> str:
        return stable_json(self.to_dict())

    def sha256(self) -> str:
        return sha256_bytes(self.to_json().encode("utf-8"))


def _require_str_dict(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(value, field, "对象")
    return value


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or value == "":
        _invalid(value, field, "非空字符串")
    return value


def _require_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _invalid(value, field, "布尔值")
    return value


def _require_utc_iso(value: object, field: str) -> str:
    """Require a UTC ISO 8601 timestamp with explicit +00:00 offset."""
    if not isinstance(value, str):
        _invalid(value, field, "UTC ISO 8601 字符串")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        _invalid(value, field, "UTC ISO 8601 字符串")
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        _invalid(value, field, "UTC ISO 8601（+00:00）")
    return value


def _require_summary(value: object) -> dict[str, Any] | None:
    """Require the fixed-key, strictly typed last_result_summary (or null)."""
    if value is None:
        return None
    if not isinstance(value, dict):
        _invalid(value, "last_result_summary", "对象或 null")
    unknown = set(value) - set(_SUMMARY_KEYS)
    if unknown:
        raise _fail(f"last_result_summary 含未知键: {sorted(unknown)}")
    missing = set(_SUMMARY_KEYS) - set(value)
    if missing:
        raise _fail(f"last_result_summary 缺少键: {sorted(missing)}")
    if value["mode"] not in ("baseline", "scan"):
        _invalid(value["mode"], "last_result_summary.mode", "baseline 或 scan")
    for key in ("previous_last_page", "current_last_page"):
        page = value[key]
        if page is not None and (type(page) is not int or page <= 0):
            _invalid(page, f"last_result_summary.{key}", "正整数或 null")
    pages = value["scanned_pages"]
    if not isinstance(pages, list) or any(type(p) is not int or p <= 0 for p in pages):
        _invalid(pages, "last_result_summary.scanned_pages", "正整数数组")
    for key in _SUMMARY_COUNT_KEYS:
        count = value[key]
        if type(count) is not int or count < 0:
            _invalid(count, f"last_result_summary.{key}", "非负整数")
    if type(value["dry_run"]) is not bool:
        _invalid(value["dry_run"], "last_result_summary.dry_run", "布尔值")
    return value


def _require_pos_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        _invalid(value, field, "正整数")
    return value


def _require_optional_pos_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _require_pos_int(value, field)


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        _invalid(value, field, "64 位小写十六进制 SHA-256")
    return value


def _require_pid(value: object, field: str) -> str:
    text = _require_str(value, field)
    if not text.isdecimal() or text == "0":
        _invalid(value, field, "十进制正整数 PID 字符串")
    return text


def known_floor_from_dict(data: dict[str, Any]) -> KnownFloor:
    allowed = {
        "pid",
        "content_sha256",
        "last_seen_page",
        "trigger_found",
        "inbox_item_id",
        "imported_content_sha256",
        "changed_after_import",
    }
    unknown = set(data) - allowed
    if unknown:
        raise _fail(f"KnownFloor 含未知键: {sorted(unknown)}")
    imported = data.get("imported_content_sha256")
    return KnownFloor(
        pid=_require_pid(data.get("pid"), "pid"),
        content_sha256=_require_sha256(data.get("content_sha256"), "content_sha256"),
        last_seen_page=_require_pos_int(data.get("last_seen_page"), "last_seen_page"),
        trigger_found=_require_bool(data.get("trigger_found"), "trigger_found"),
        inbox_item_id=_require_optional_pos_int(data.get("inbox_item_id"), "inbox_item_id"),
        imported_content_sha256=(
            _require_sha256(imported, "imported_content_sha256")
            if imported is not None
            else None
        ),
        changed_after_import=_require_bool(
            data.get("changed_after_import"), "changed_after_import"
        ),
    )


def state_from_dict(data: dict[str, Any]) -> WatcherState:
    allowed = {
        "schema_version",
        "tid",
        "canonical_thread_url",
        "last_page",
        "known_floors",
        "baseline_created_at",
        "last_successful_scan_at",
        "last_result_summary",
    }
    unknown = set(data) - allowed
    if unknown:
        raise _fail(f"state 含未知键: {sorted(unknown)}")
    schema_version = _require_str(data.get("schema_version"), "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise _fail(f"不支持的 schema_version: {schema_version}")
    tid = _require_pos_int(data.get("tid"), "tid")
    if tid != DEFAULT_TID:
        raise _fail(f"tid 必须为 {DEFAULT_TID}")
    canonical = _require_str(data.get("canonical_thread_url"), "canonical_thread_url")
    if canonical != f"https://bbs.nga.cn/read.php?tid={DEFAULT_TID}":
        raise _fail("canonical_thread_url 与固定目标不一致")
    last_page = _require_pos_int(data.get("last_page"), "last_page")
    floors_raw = _require_str_dict(data.get("known_floors"), "known_floors")
    known_floors: dict[str, KnownFloor] = {}
    for pid, raw in floors_raw.items():
        if pid != _require_pid(pid, "known_floors 键"):
            raise _fail("known_floors 键必须是合法 PID")
        floor = known_floor_from_dict(_require_str_dict(raw, f"known_floors.{pid}"))
        if floor.pid != pid:
            raise _fail(f"known_floors.{pid}.pid 与键不一致")
        known_floors[pid] = floor
    baseline_at = _require_utc_iso(data.get("baseline_created_at"), "baseline_created_at")
    last_scan = data.get("last_successful_scan_at")
    if last_scan is not None:
        last_scan = _require_utc_iso(last_scan, "last_successful_scan_at")
    summary = _require_summary(data.get("last_result_summary"))
    return WatcherState(
        schema_version=schema_version,
        tid=tid,
        canonical_thread_url=canonical,
        last_page=last_page,
        known_floors=known_floors,
        baseline_created_at=baseline_at,
        last_successful_scan_at=last_scan,
        last_result_summary=summary,
    )


def atomic_write_json(path: Path, payload: dict[str, Any], *, clock: Callable[[], str] | None = None) -> str:
    """Atomically write JSON to path via same-dir temp + fsync + os.replace."""
    body = stable_json(payload).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".nga-watcher-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return sha256_bytes(body)


class StateLock:
    """Exclusive same-directory .lock for one scan run; released in finally."""

    def __init__(self, state_file: Path) -> None:
        self._lock_path = Path(str(state_file) + ".lock")
        self._acquired = False

    def __enter__(self) -> "StateLock":
        try:
            descriptor = os.open(
                str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
        except FileExistsError:
            raise NgaAccessError(
                "watcher_locked", "另一个 scan 正在运行（state .lock 已存在）"
            ) from None
        with os.fdopen(descriptor, "w") as handle:
            handle.write("")
        self._acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            os.unlink(str(self._lock_path))
        except OSError:
            pass


def load_state(state_file: Path) -> WatcherState:
    """Load and strictly validate an existing state file."""
    return load_state_snapshot(state_file)[0]


def load_state_snapshot(state_file: Path) -> tuple[WatcherState, str]:
    """Parse and hash the same bytes, never a second read of a changing file."""
    try:
        raw = state_file.read_bytes()
    except FileNotFoundError:
        raise NgaAccessError("watcher_state_missing", "state 文件不存在，请先建立基线") from None
    except OSError:
        raise NgaAccessError("watcher_state_unreadable", "无法读取 state 文件，请检查路径和权限") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise _fail("state 文件不是合法 JSON") from None
    if not isinstance(data, dict):
        raise _fail("state 顶层必须是对象")
    return state_from_dict(data), sha256_bytes(raw)


def state_file_sha256(state_file: Path) -> str:
    return sha256_bytes(state_file.read_bytes())
