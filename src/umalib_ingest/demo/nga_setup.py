"""TASK-013 M09：真实 NGA "从现在开始"基线预览与确认（第 9.3 节）。

复用 launch_probe_session / fetch_thread_page / 现有解析与 state 序列化；
不硬编码 173、不导入/复制 canonical state；profile 由启动器固定，HTTP 不能
指定任意路径。真正连网受 M14 授权限制；本地功能经假 transport 完整测试。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umalib_ingest.demo.simulated_scan import build_thread_html


@dataclass(frozen=True)
class NgaBaselinePreview:
    """In-process preview of a 'start from now' baseline; expires with process."""

    preview_id: str
    workspace_id: str
    observed_bytes_sha256: str
    last_page: int
    floor_pids: tuple[str, ...]
    floor_hashes: tuple[tuple[str, str], ...]
    pages_read: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "preview_id": self.preview_id,
            "workspace_id": self.workspace_id,
            "observed_bytes_sha256": self.observed_bytes_sha256,
            "last_page": self.last_page,
            "floor_pids": list(self.floor_pids),
            "floor_hashes": [list(item) for item in self.floor_hashes],
            "pages_read": list(self.pages_read),
        }


class NgaSetup:
    """Baseline preview/confirm lifecycle bound to one workspace."""

    def __init__(self, workspace_id: str, state_file: Path, snapshot_dir: Path,
                 profile_dir: Path, inbox_db: Path) -> None:
        self._workspace_id = workspace_id
        self._state_file = state_file
        self._snapshot_dir = snapshot_dir
        self._profile_dir = profile_dir
        self._inbox_db = inbox_db
        self._preview: NgaBaselinePreview | None = None
        self._lock = threading.Lock()

    def build_preview(self, session_factory=None, clock=None) -> NgaBaselinePreview:
        """Read page 1 + current last page and build the initialization suggestion.

        session_factory returns a browser session (real launch or fake transport
        in tests). Never writes state/Inbox.
        """
        import uuid

        from umalib_ingest.nga_access.fetch import fetch_thread_page

        with self._lock:
            pages: list[Any] = []
            floor_hashes: list[tuple[str, str]] = []
            seen_pids: set[str] = set()
            if session_factory is None:
                from umalib_ingest.nga_access.browser import launch_probe_session

                def session_factory():
                    return launch_probe_session(self._profile_dir)

            session = session_factory()
            try:
                from umalib_ingest.watcher.diff import floor_content_sha256

                first = fetch_thread_page(session, 30099686, 1, self._snapshot_dir)
                pages.append(first)
                for floor in first.floors:
                    if floor.pid not in seen_pids:
                        seen_pids.add(floor.pid)
                        floor_hashes.append((floor.pid, floor_content_sha256(floor)))
                if first.last_page is None:
                    raise RuntimeError("无法确定当前最后一页")
                if first.last_page != 1:
                    last = fetch_thread_page(session, 30099686, first.last_page,
                                             self._snapshot_dir)
                    pages.append(last)
                    for floor in last.floors:
                        if floor.pid not in seen_pids:
                            seen_pids.add(floor.pid)
                            floor_hashes.append((floor.pid, floor_content_sha256(floor)))
            finally:
                session.close()
            digest = hashlib.sha256()
            for page in pages:
                digest.update(f"{page.page}:{page.last_page}".encode("utf-8"))
            for pid, sha in sorted(floor_hashes):
                digest.update(f"{pid}:{sha}".encode("utf-8"))
            preview = NgaBaselinePreview(
                preview_id=str(uuid.uuid4()),
                workspace_id=self._workspace_id,
                observed_bytes_sha256=digest.hexdigest(),
                last_page=pages[-1].last_page or 0,
                floor_pids=tuple(sorted(pid for pid, _ in floor_hashes)),
                floor_hashes=tuple(sorted(floor_hashes)),
                pages_read=tuple(page.page for page in pages),
            )
            self._preview = preview
            return preview

    def confirm(self, preview_id: str, observed_sha256: str, confirmed: bool,
                clock=None) -> dict[str, Any]:
        """Write a NEW 'start from now' state; never touches existing state/Inbox."""
        if type(confirmed) is not bool or confirmed is not True:
            raise ValueError("确认必须显式为 true")
        with self._lock:
            preview = self._preview
            if preview is None:
                raise ValueError("没有可确认的预览")
            if preview.preview_id != preview_id:
                raise ValueError("预览已失效，请重新预览")
            if preview.observed_bytes_sha256 != observed_sha256:
                raise ValueError("观测字节 hash 不一致")
            if self._state_file.exists():
                raise ValueError("state 已存在，不能覆盖（请先手工处理）")
            from umalib_ingest.watcher.state import (
                KnownFloor,
                WatcherState,
                atomic_write_json,
            )

            known: dict[str, KnownFloor] = {
                pid: KnownFloor(
                    pid=pid, content_sha256=sha, last_seen_page=preview.last_page,
                    trigger_found=False, inbox_item_id=None,
                    imported_content_sha256=None, changed_after_import=False,
                )
                for pid, sha in preview.floor_hashes
            }
            state = WatcherState(
                schema_version="1", tid=30099686,
                canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
                last_page=preview.last_page, known_floors=known,
                baseline_created_at=clock() if clock else "2026-09-03T00:00:00+00:00",
                last_successful_scan_at=None, last_result_summary=None,
            )
            atomic_write_json(self._state_file, state.to_dict())
            return {"state_written": True, "last_page": preview.last_page,
                    "floor_count": len(known)}
