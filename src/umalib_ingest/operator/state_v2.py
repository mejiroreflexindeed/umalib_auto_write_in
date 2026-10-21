"""TASK-014 W04/W06：Watcher state v2 严格读写 + v1 只读适配 + 楼层时间解析。

v2 顶层固定（3.3.4）：frontier_page/observed_last_page/next_scan_page/known_floors/
date_exclusions/…/active_rescan。v1 适配器读旧 state 映射，原文件不改。
"""

from __future__ import annotations

import datetime
import html as html_module
import json
import re
from dataclasses import dataclass, field
from typing import Any

from umalib_ingest.operator.models import (
    JsonValidationError,
    canonical_json,
    is_sha256,
    sha256_hex,
    utc_now,
    validate_tid,
)

_TZ_OFFSET = datetime.timedelta(hours=8)
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_SEC_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_DATETIME_MIN_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def parse_nga_posted_at(raw: str) -> datetime.datetime | None:
    """严格解析楼层原始发表时间文本，按 UTC+08:00 转 UTC；未知格式返回 None。

    只接受 %Y-%m-%d %H:%M:%S、%Y-%m-%d %H:%M、%Y-%m-%d 三种完整匹配；先
    HTML-unescape + strip；不做模糊解析、不用系统 locale。
    """
    if not isinstance(raw, str):
        return None
    text = html_module.unescape(raw).strip()
    local_naive = None
    if _DATETIME_SEC_RE.fullmatch(text):
        local_naive = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    elif _DATETIME_MIN_RE.fullmatch(text):
        local_naive = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
    elif _DATE_ONLY_RE.fullmatch(text):
        local_naive = datetime.datetime.strptime(text, "%Y-%m-%d")
    else:
        return None
    aware = local_naive.replace(tzinfo=datetime.timezone(_TZ_OFFSET))
    return aware.astimezone(datetime.timezone.utc)


@dataclass(frozen=True)
class DateExclusion:
    pid: str
    content_sha256: str
    posted_at_raw: str
    posted_at_utc: str
    last_seen_page: int
    excluded_lower_bound_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid, "content_sha256": self.content_sha256,
            "posted_at_raw": self.posted_at_raw, "posted_at_utc": self.posted_at_utc,
            "last_seen_page": self.last_seen_page,
            "excluded_lower_bound_utc": self.excluded_lower_bound_utc,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DateExclusion":
        for key in ("pid", "content_sha256", "posted_at_raw", "posted_at_utc",
                    "excluded_lower_bound_utc"):
            if not isinstance(data.get(key), str) or data[key] == "":
                raise JsonValidationError(f"date_exclusions.{key} 非法")
        if type(data.get("last_seen_page")) is not int:
            raise JsonValidationError("date_exclusions.last_seen_page 非法")
        return cls(**data)


@dataclass(frozen=True)
class ActiveRescan:
    rescan_id: str
    kind: str  # pages | date
    from_page: int | None
    to_page: int | None
    lower_bound_utc: str | None
    next_page: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rescan_id": self.rescan_id, "kind": self.kind,
            "from_page": self.from_page, "to_page": self.to_page,
            "lower_bound_utc": self.lower_bound_utc, "next_page": self.next_page,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveRescan":
        if data.get("kind") not in ("pages", "date"):
            raise JsonValidationError("active_rescan.kind 非法")
        return cls(**data)


@dataclass(frozen=True)
class WatcherStateV2:
    schema_version: str
    tid: str
    canonical_thread_url: str
    frontier_page: int
    observed_last_page: int | None
    next_scan_page: int
    known_floors: dict[str, Any]
    date_exclusions: dict[str, DateExclusion]
    baseline_created_at: str
    last_successful_scan_at: str | None
    last_result_summary: dict[str, Any] | None
    active_rescan: ActiveRescan | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "tid": self.tid,
            "canonical_thread_url": self.canonical_thread_url,
            "frontier_page": self.frontier_page,
            "observed_last_page": self.observed_last_page,
            "next_scan_page": self.next_scan_page,
            "known_floors": self.known_floors,
            "date_exclusions": {pid: e.to_dict() for pid, e in self.date_exclusions.items()},
            "baseline_created_at": self.baseline_created_at,
            "last_successful_scan_at": self.last_successful_scan_at,
            "last_result_summary": self.last_result_summary,
            "active_rescan": self.active_rescan.to_dict() if self.active_rescan else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatcherStateV2":
        allowed = {"schema_version", "tid", "canonical_thread_url", "frontier_page",
                   "observed_last_page", "next_scan_page", "known_floors",
                   "date_exclusions", "baseline_created_at", "last_successful_scan_at",
                   "last_result_summary", "active_rescan"}
        unknown = set(data) - allowed
        if unknown:
            raise JsonValidationError(f"state.json 含未知键: {sorted(unknown)}")
        if data.get("schema_version") != "2":
            raise JsonValidationError(f"state schema_version 不受支持: {data.get('schema_version')!r}")
        validate_tid(data.get("tid"))
        frontier = data.get("frontier_page")
        if type(frontier) is not int or frontier < 0:
            raise JsonValidationError("frontier_page 非法")
        observed = data.get("observed_last_page")
        if observed is not None and (type(observed) is not int or observed < 1):
            raise JsonValidationError("observed_last_page 非法")
        next_page = data.get("next_scan_page")
        if type(next_page) is not int or next_page < 1:
            raise JsonValidationError("next_scan_page 非法")
        if frontier == 0:
            if next_page != 1:
                raise JsonValidationError("frontier=0 时 next_scan_page 必须为 1")
        elif next_page not in (frontier + 1, frontier):
            # 唯一例外：追上活跃尾页后允许 next==frontier 以便重查
            raise JsonValidationError("next_scan_page 与 frontier_page 关系非法")
        exclusions = data.get("date_exclusions")
        if not isinstance(exclusions, dict):
            raise JsonValidationError("date_exclusions 非法")
        rescan = data.get("active_rescan")
        return cls(
            schema_version="2", tid=data["tid"],
            canonical_thread_url=data.get("canonical_thread_url", ""),
            frontier_page=frontier, observed_last_page=observed, next_scan_page=next_page,
            known_floors=data.get("known_floors") or {},
            date_exclusions={pid: DateExclusion.from_dict(e) for pid, e in exclusions.items()},
            baseline_created_at=str(data.get("baseline_created_at", "")),
            last_successful_scan_at=data.get("last_successful_scan_at"),
            last_result_summary=data.get("last_result_summary"),
            active_rescan=ActiveRescan.from_dict(rescan) if rescan else None,
        )


def from_v1_state(v1: dict[str, Any]) -> WatcherStateV2:
    """只读适配旧 v1 state：映射 last_page -> frontier/observed/next，不改原文件。"""
    if not isinstance(v1, dict) or v1.get("schema_version") != "1":
        raise JsonValidationError("不是合法的 v1 state")
    last_page = v1.get("last_page")
    if type(last_page) is not int or last_page < 1:
        raise JsonValidationError("v1 last_page 非法")
    tid = v1.get("tid")
    validate_tid(str(tid))
    known = v1.get("known_floors") or {}
    for pid, floor in known.items():
        if "pid" not in floor:
            floor = dict(floor)
            floor["pid"] = pid
            known[pid] = floor
    return WatcherStateV2(
        schema_version="2", tid=str(tid),
        canonical_thread_url=v1.get("canonical_thread_url", ""),
        frontier_page=last_page, observed_last_page=last_page,
        next_scan_page=last_page, known_floors=known, date_exclusions={},
        baseline_created_at=str(v1.get("baseline_created_at", "")),
        last_successful_scan_at=v1.get("last_successful_scan_at"),
        last_result_summary=v1.get("last_result_summary"),
        active_rescan=None,
    )


def read_state_file(path: Any) -> WatcherStateV2:
    """读取 threads/<tid>/state.json；v1 旧文件只读适配，v2 严格校验。"""
    from pathlib import Path

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise JsonValidationError("state.json 损坏")
    if not isinstance(data, dict):
        raise JsonValidationError("state.json 顶层必须是对象")
    version = data.get("schema_version")
    if version == "2":
        return WatcherStateV2.from_dict(data)
    if version == "1":
        return from_v1_state(data)
    raise JsonValidationError(f"state schema_version 不受支持: {version!r}")
