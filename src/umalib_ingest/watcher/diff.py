"""Pure floor diff, trigger and NGA origin helpers (TASK-011)."""

from __future__ import annotations

import re
from typing import Any

from umalib_ingest.domain.models import SubmissionDraft, SubmissionOrigin
from umalib_ingest.nga_access.parsing import FloorSnapshot
from umalib_ingest.watcher.state import KnownFloor, sha256_bytes

_TRIGGER_LINE_RE = re.compile(r"^\s*\[收录\]\s*$")


def floor_content_sha256(floor: FloorSnapshot) -> str:
    """Stable content hash over the raw UBB fragment bytes (dynamic shell excluded)."""
    return sha256_bytes(floor.content_raw_fragment.encode("utf-8"))


def has_trigger(content_text: str) -> bool:
    """True when the normalized text contains a standalone [收录] line."""
    return any(_TRIGGER_LINE_RE.match(line) for line in content_text.splitlines())


def canonical_floor_url(tid: int, pid: str) -> str:
    """Standard NGA floor URL; never includes &to=1."""
    if type(tid) is not int or tid <= 0:
        raise ValueError("tid 必须是正整数")
    if not str(pid).isdecimal() or str(pid) == "0":
        raise ValueError("pid 必须是正整数十进制字符串")
    return f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"


def build_floor_origin(tid: int, pid: str, floor_number: int | None) -> SubmissionOrigin:
    """Standard NGA floor origin for the existing Parser."""
    return SubmissionOrigin(
        platform="nga",
        thread_id=str(tid),
        post_id=pid,
        floor=floor_number,
        canonical_url=canonical_floor_url(tid, pid),
    )


def adapt_draft_with_floor(draft: SubmissionDraft, floor: FloorSnapshot, tid: int) -> None:
    """Keep Parser metadata/body/diagnostics, preserve raw UBB and NGA source.

    Replaces content.raw_text with the floor's original UBB fragment and
    appends the canonical floor URL to metadata.sources.nga. Parser results
    (metadata, body_text, diagnostics) are not recomputed.
    """
    draft.content.raw_text = floor.content_raw_fragment
    nga = draft.metadata.sources.nga
    url = canonical_floor_url(tid, floor.pid)
    if url not in nga:
        nga.append(url)


def floor_to_known(
    floor: FloorSnapshot,
    *,
    last_seen_page: int,
    content_sha256: str | None = None,
    trigger_found: bool | None = None,
) -> KnownFloor:
    """Build a KnownFloor from a floor snapshot (baseline/diff use)."""
    return KnownFloor(
        pid=floor.pid,
        content_sha256=content_sha256 or floor_content_sha256(floor),
        last_seen_page=last_seen_page,
        trigger_found=(
            trigger_found if trigger_found is not None else has_trigger(floor.content_text)
        ),
        inbox_item_id=None,
        imported_content_sha256=None,
        changed_after_import=False,
    )


def summary_payload(result: Any) -> dict[str, Any]:
    """Minimal last_result_summary payload (no content/authors/pids)."""
    return {
        "mode": result.mode,
        "previous_last_page": result.previous_last_page,
        "current_last_page": result.current_last_page,
        "scanned_pages": list(result.scanned_pages),
        "new_floor_count": result.new_floor_count,
        "unchanged_floor_count": result.unchanged_floor_count,
        "changed_floor_count": result.changed_floor_count,
        "discussion_count": result.discussion_count,
        "triggered_count": result.triggered_count,
        "imported_item_count": len(result.imported_items),
        "changed_after_import_count": len(result.changed_after_import),
        "dry_run": result.dry_run,
    }
