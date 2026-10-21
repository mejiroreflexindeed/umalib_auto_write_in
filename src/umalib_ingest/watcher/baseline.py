"""Offline first baseline from existing TASK-010 snapshots (TASK-011)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.nga_access import codec, parsing
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.paths import validate_snapshot_dir
from umalib_ingest.watcher import diff
from umalib_ingest.watcher.result import WatcherRunResult
from umalib_ingest.watcher.state import (
    DEFAULT_TID,
    KnownFloor,
    WatcherConfig,
    WatcherState,
    atomic_write_json,
    stable_json,
    sha256_bytes,
    state_file_sha256,
    validate_state_path,
)


def _baseline_invalid(message: str) -> NgaAccessError:
    return NgaAccessError("baseline_snapshot_invalid", message)


def load_last_page_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Read probe-result.json and the last page's meta/body (read-only)."""
    manifest_path = snapshot_dir / "probe-result.json"
    if not manifest_path.exists():
        raise _baseline_invalid("probe-result.json 不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise _baseline_invalid("probe-result.json 顶层不是对象")
    if manifest.get("tid") != DEFAULT_TID:
        raise _baseline_invalid("manifest tid 与固定目标不一致")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        raise _baseline_invalid("manifest 缺少 pages")
    last_entry = max(
        (p for p in pages if isinstance(p, dict) and str(p.get("page_label", "")).isdecimal()),
        key=lambda p: int(p["page_label"]),
        default=None,
    )
    if last_entry is None:
        raise _baseline_invalid("manifest 缺少数字页码的页面条目")
    label = str(last_entry["page_label"])
    if last_entry.get("last_page") != 173:
        raise _baseline_invalid(f"最后一页 last_page 应为 173，实际 {last_entry.get('last_page')}")
    if last_entry.get("status") != 200:
        raise _baseline_invalid(f"最后一页 status 应为 200，实际 {last_entry.get('status')}")
    entry_diags = last_entry.get("diagnostics") or []
    if any(d in ("thread_forbidden", "thread_not_found", "captcha_or_challenge",
                 "manual_login_required", "response_not_captured",
                 "floor_structure_unrecognized", "unexpected_redirect") for d in entry_diags):
        raise _baseline_invalid(f"最后一页存在阻断诊断: {sorted(set(entry_diags))}")

    thread_dir = snapshot_dir / f"tid-{DEFAULT_TID}"
    meta_path = thread_dir / f"page-{label}.meta.json"
    body_path = thread_dir / f"page-{label}.response.bin"
    if not meta_path.exists() or not body_path.exists():
        raise _baseline_invalid(f"最后一页快照文件缺失（page-{label}）")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    body = body_path.read_bytes()
    body_sha = sha256_bytes(body)
    if meta.get("body_sha256") != body_sha:
        raise _baseline_invalid("meta body_sha256 与实际字节不符")
    if last_entry.get("body_sha256") != body_sha:
        raise _baseline_invalid("manifest body_sha256 与实际字节不符")
    return {
        "manifest": manifest,
        "page_label": label,
        "meta": meta,
        "body": body,
        "body_sha256": body_sha,
    }


def build_baseline_state(
    snapshot_dir: Path,
    *,
    clock: Callable[[], str],
) -> tuple[WatcherState, dict[str, Any]]:
    """Parse the last page and build the baseline state; no Parser/Inbox."""
    evidence = load_last_page_snapshot(snapshot_dir)
    body = evidence["body"]
    content_type = evidence["meta"].get("content_type", "")
    decode = codec.decode_response(body, content_type)
    if decode.text is None:
        raise _baseline_invalid("最后一页字节无法确定性解码")
    thread = parsing.parse_html_thread(decode.text, DEFAULT_TID)
    if "floor_structure_unrecognized" in thread.diagnostics:
        raise _baseline_invalid("最后一页楼层结构无法识别")
    pids = [floor.pid for floor in thread.floors]
    if len(set(pids)) != len(pids):
        raise _baseline_invalid("最后一页存在重复 PID")

    diagnostics: list[str] = []
    if len(pids) != 20:
        diagnostics.append("baseline_floor_count_unexpected")
    known_floors: dict[str, KnownFloor] = {}
    for floor in thread.floors:
        known_floors[floor.pid] = diff.floor_to_known(
            floor, last_seen_page=thread.last_page or 173
        )
    state = WatcherState(
        schema_version="1",
        tid=DEFAULT_TID,
        canonical_thread_url=f"https://bbs.nga.cn/read.php?tid={DEFAULT_TID}",
        last_page=thread.last_page or 173,
        known_floors=known_floors,
        baseline_created_at=clock(),
        last_successful_scan_at=None,
        last_result_summary=None,
    )
    return state, {"diagnostics": diagnostics, "body_sha256": evidence["body_sha256"]}


def baseline_from_snapshot(
    config: WatcherConfig,
    *,
    clock: Callable[[], str],
) -> WatcherRunResult:
    """Create the offline first baseline; refuses to overwrite existing state."""
    state_file = validate_state_path(config.state_file)
    if state_file.exists():
        raise NgaAccessError("watcher_state_exists", "state 已存在，禁止覆盖")
    if config.snapshot_dir is None:
        raise _baseline_invalid("缺少 snapshot-dir")
    snapshot_dir = validate_snapshot_dir(config.snapshot_dir)
    state, evidence = build_baseline_state(snapshot_dir, clock=clock)
    state_sha = atomic_write_json(state_file, state.to_dict())
    result = WatcherRunResult(
        mode="baseline",
        tid=state.tid,
        previous_last_page=None,
        current_last_page=state.last_page,
        scanned_pages=(),
        new_floor_count=len(state.known_floors),
        state_before_sha256=None,
        state_after_sha256=state_sha,
        dry_run=False,
        diagnostics=tuple(evidence["diagnostics"]),
    )
    return result


def state_canonical_sha(state: WatcherState) -> str:
    return sha256_bytes(stable_json(state.to_dict()).encode("utf-8"))
