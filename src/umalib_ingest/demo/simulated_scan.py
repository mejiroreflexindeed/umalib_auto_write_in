"""TASK-013 M09：离线模拟扫描（synthetic 工作区专用）。

使用字节级假 transport：真实 fetch_thread_page 的编码/HTML 解析/楼层解析链 +
真实 run_scan + 真实 Parser/Inbox；只替代浏览器 session 与网络响应。
不写 canonical watcher state；页面长期显示"离线模拟，不代表已连接 NGA"。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from umalib_ingest.demo import samples


class _FakeResponse:
    def __init__(self, url: str, status: int, headers: dict[str, str], body: bytes) -> None:
        self.url = url
        self.status = status
        self.headers = headers
        self._body = body

    def body(self) -> bytes:
        return self._body


class _FakePage:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses

    def goto(self, url: str, wait_until: str | None = None) -> _FakeResponse:
        response = self._responses.get(url)
        if response is None:
            raise RuntimeError(f"模拟 transport 无该 URL 响应: {url}")
        return response

    def title(self) -> str:
        return "模拟帖子页面"


class _FakeSession:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def close(self) -> None:
        pass


def build_thread_html(floors: tuple[dict[str, Any], ...], last_page: int) -> bytes:
    """Build a deterministic NGA-like thread HTML with the given floors."""
    floor_parts: list[str] = []
    for floor in floors:
        escaped = floor["content"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        floor_parts.append(
            f'<tr id="post1strow{floor["pid"]}">'
            f'<td><a id="pid{floor["pid"]}Anchor"></a>'
            f'<span class="postcontent" id="postcontent{floor["pid"]}">{escaped}</span></td></tr>'
        )
    html = (
        "<html><head><title>模拟帖子</title></head><body>"
        + "<script>var __PAGE = {0:'/read.php?tid=30099686',1:"
        + str(last_page)
        + ",2:1,3:20};commonui.pageBtn(document.getElementById('pageBtnHere').parentNode,__PAGE,true)</script>"
        + "".join(floor_parts)
        + "</body></html>"
    )
    return html.encode("utf-8")


def run_simulated_scan(
    state_file: Path,
    inbox_db: Path,
    snapshot_dir: Path,
    scenario: dict[str, Any],
    profile_dir: Path,
) -> dict[str, Any]:
    """Run one real run_scan over byte-level fake responses (synthetic only)."""
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import WatcherConfig

    scenario_id = scenario["scenario_id"]
    floors = scenario["floors"]
    html = build_thread_html(floors, last_page=173)
    page_one_url = f"https://bbs.nga.cn/read.php?tid=30099686"
    page_last_url = f"https://bbs.nga.cn/read.php?tid=30099686&page=173"
    responses = {
        page_one_url: _FakeResponse(
            page_one_url, 200,
            {"content-type": "text/html; charset=utf-8"}, html,
        ),
        page_last_url: _FakeResponse(
            page_last_url, 200,
            {"content-type": "text/html; charset=utf-8"}, html,
        ),
    }
    session = _FakeSession(_FakePage(responses))
    config = WatcherConfig(
        state_file=state_file,
        profile_dir=profile_dir,
        snapshot_dir=snapshot_dir,
        inbox_db=inbox_db,
        navigation_delay_seconds=3.0,
        max_scan_pages=12,
    )

    def session_factory():
        return session

    from umalib_ingest.nga_access.fetch import fetch_thread_page

    def fetch_fn(session_arg, tid, page, snap):
        return fetch_thread_page(session_arg, tid, page, snap)

    result = run_scan(
        config,
        dry_run=False,
        session_factory=session_factory,
        fetch_fn=fetch_fn,
        sleep=lambda seconds: None,
        should_cancel=lambda: False,
    )
    return {
        "scenario_id": scenario_id,
        "mode": result.mode,
        "new_floor_count": result.new_floor_count,
        "unchanged_floor_count": result.unchanged_floor_count,
        "discussion_count": result.discussion_count,
        "triggered_count": result.triggered_count,
        "imported_count": len(result.imported_items),
        "changed_after_import": list(result.changed_after_import),
        "diagnostics": list(result.diagnostics),
        "scanned_pages": list(result.scanned_pages),
        "simulated": True,
    }
