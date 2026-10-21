"""Atomic response snapshot writing and manifest (TASK-010)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umalib_ingest.nga_access.errors import NgaAccessError


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of bytes."""
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Write bytes atomically via a temp file in the same directory."""
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".nga-tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _atomic_write_text(target: Path, text: str) -> None:
    _atomic_write_bytes(target, text.encode("utf-8"))


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(
        target, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    )


@dataclass(frozen=True)
class Capture:
    """The minimal evidence of one top-level document response."""

    tid: int
    page_label: str  # "1" or "last"
    status: int
    final_url: str
    content_type: str
    body_bytes: bytes
    captured_at_utc: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotOutcome:
    """What was written for one page capture."""

    status: str  # "written" | "unchanged" | "conflict" | "skipped"
    body_sha256: str
    meta_path: str
    body_path: str | None
    decoded_path: str | None
    diagnostics: tuple[str, ...]


def write_response_snapshot(
    snapshot_dir: Path,
    capture: Capture,
    decoded_text: str | None,
) -> SnapshotOutcome:
    """Persist one page capture atomically as a versioned file group.

    A capture is stored under one base name: `page-{label}` on first write,
    and `page-{label}.{body_sha8}` for every differing version. The response,
    decoded text and meta of one version always share the same base name and
    are never overwritten.
    """
    thread_dir = snapshot_dir / f"tid-{capture.tid}"
    thread_dir.mkdir(parents=True, exist_ok=True)
    label = capture.page_label
    body_sha = sha256_bytes(capture.body_bytes)

    primary_body = thread_dir / f"page-{label}.response.bin"
    group_base: str | None = None
    outcome_status = "written"
    if primary_body.exists():
        if sha256_bytes(primary_body.read_bytes()) == body_sha:
            outcome_status = "unchanged"
            group_base = f"page-{label}"
        else:
            group_base = f"page-{label}.{body_sha[:8]}"
            candidate = thread_dir / f"{group_base}.response.bin"
            if candidate.exists():
                if sha256_bytes(candidate.read_bytes()) == body_sha:
                    outcome_status = "unchanged"
                else:
                    raise NgaAccessError(
                        "snapshot_conflict", "目标响应存在多个不同版本且冲突名被占用"
                    )
            else:
                outcome_status = "conflict"
    else:
        group_base = f"page-{label}"

    if group_base is None:
        raise NgaAccessError("snapshot_conflict", "无法确定快照版本名")

    body_path = thread_dir / f"{group_base}.response.bin"
    decoded_path = thread_dir / f"{group_base}.decoded.html"
    meta_path = thread_dir / f"{group_base}.meta.json"

    if not body_path.exists():
        _atomic_write_bytes(body_path, capture.body_bytes)
    if decoded_text is not None and not decoded_path.exists():
        _atomic_write_text(decoded_path, decoded_text)
    if not meta_path.exists():
        meta = {
            "tid": capture.tid,
            "page_label": label,
            "group": group_base,
            "status": capture.status,
            "final_url": capture.final_url,
            "content_type": capture.content_type,
            "captured_at_utc": capture.captured_at_utc,
            "body_bytes": len(capture.body_bytes),
            "body_sha256": body_sha,
            "decoded_encoding": "utf-8" if decoded_text is not None else None,
            "files": {
                "body": f"{group_base}.response.bin",
                "decoded": f"{group_base}.decoded.html" if decoded_text is not None else None,
                "meta": f"{group_base}.meta.json",
            },
            "diagnostics": list(capture.diagnostics),
        }
        _atomic_write_json(meta_path, meta)
    return SnapshotOutcome(
        status=outcome_status,
        body_sha256=body_sha,
        meta_path=str(meta_path.relative_to(snapshot_dir)),
        body_path=str(body_path.relative_to(snapshot_dir)),
        decoded_path=str(decoded_path.relative_to(snapshot_dir)) if decoded_text is not None else None,
        diagnostics=capture.diagnostics,
    )


def write_diagnostic_meta(
    snapshot_dir: Path, tid: int, page_label: str, payload: dict[str, Any]
) -> Path:
    """Persist content-free diagnostic metadata without touching content files."""
    thread_dir = snapshot_dir / f"tid-{tid}"
    thread_dir.mkdir(parents=True, exist_ok=True)
    target = thread_dir / f"page-{page_label}.diag.json"
    _atomic_write_json(target, payload)
    return target


def write_probe_manifest(snapshot_dir: Path, payload: dict[str, Any]) -> Path:
    """Atomically write the probe-result manifest (latest run summary only)."""
    target = snapshot_dir / "probe-result.json"
    _atomic_write_json(target, payload)
    return target
