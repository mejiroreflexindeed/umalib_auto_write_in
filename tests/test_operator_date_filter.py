"""TASK-014 W06/W07 起始扫描日期测试（A101-A112 核心）。"""

from __future__ import annotations

import datetime
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.operator.models import ThreadConfig, ThreadStart, utc_now
from umalib_ingest.operator.scan_service import Transport, run_batch_scan
from umalib_ingest.operator.state_v2 import WatcherStateV2, parse_nga_posted_at


class _Resp:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


class _Page:
    def __init__(self, tid, pages):
        self._pages = pages

    def goto(self, url, wait_until=None):
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1])
        body = self._pages.get(page)
        if body is None:
            raise RuntimeError(f"no page {page}")
        return _Resp(url, body)

    def title(self):
        return "模拟帖子"


class _Session:
    def __init__(self, page):
        self.page = page

    def close(self):
        pass


def _floor(pid, content, posted):
    esc = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<tr id="post1strow{pid}"><td><a id="pid{pid}Anchor"></a>'
            f'<span class="postdate" id="postdate{pid}">{posted}</span>'
            f'<span class="postcontent" id="postcontent{pid}">{esc}</span></td></tr>')


def _html(tid, last_page, page, floors):
    head = (f"<script>var __PAGE = {{0:'/read.php?tid={tid}',1:{last_page},2:1,3:20}};"
            "commonui.pageBtn(document.getElementById('pageBtnHere').parentNode,__PAGE,true)</script>")
    return (f"<html><head><title>模拟</title></head><body>{head}"
            + "".join(_floor(p, c, d) for p, c, d in floors)
            + "</body></html>").encode("utf-8")


def make_state(observed_last_page=1):
    return WatcherStateV2(
        schema_version="2", tid="30099686",
        canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
        frontier_page=0, observed_last_page=observed_last_page, next_scan_page=1,
        known_floors={}, date_exclusions={},
        baseline_created_at=utc_now(), last_successful_scan_at=None,
        last_result_summary=None, active_rescan=None,
    )


def make_transport(tid, floors_by_page, last_page, delay=0.0):
    pages = {page: _html(tid, last_page, page, floors_by_page.get(page, []))
             for page in floors_by_page}
    return Transport(lambda: _Session(_Page(tid, pages)),
                     Path(tempfile.mkdtemp(prefix="op-date-")), tid, delay=delay)


class _Ctrl:
    def __init__(self):
        self.cancelled = False

    def check_cancel(self):
        if self.cancelled:
            raise RuntimeError("cancelled")

    def update_progress(self, **kw):
        pass


class DateParseTests(unittest.TestCase):
    def test_boundaries_and_edge_cases(self) -> None:
        # D 日 00:00:00 含；前一秒排除（UTC 换算）
        self.assertEqual(parse_nga_posted_at("2026-08-01 00:00:00").isoformat(),
                         "2026-07-31T16:00:00+00:00")
        self.assertEqual(parse_nga_posted_at("2026-07-31 23:59:59").isoformat(),
                         "2026-07-31T15:59:59+00:00")
        # 闰日
        self.assertIsNotNone(parse_nga_posted_at("2024-02-29 10:00:00"))
        # 未知格式返回 None（不做模糊解析）
        self.assertIsNone(parse_nga_posted_at("2026/08/01"))
        self.assertIsNone(parse_nga_posted_at("发表于昨天"))
        # HTML entity 与空白
        self.assertIsNotNone(parse_nga_posted_at("  2026-08-01 10:00:00  "))
        self.assertIsNotNone(parse_nga_posted_at("2026-08-01&#32;10:00:00".replace("&#32;", " ")))


class DateFilterScanTests(unittest.TestCase):
    def test_date_filter_excludes_before_bound_and_keeps_unknown(self) -> None:
        # 三个楼层：早于下限、晚于下限、时间未知
        tid = "30099686"
        floors = {
            1: [
                ("91001001", "[收录]\n[标题] 早稿\n[作者] 甲\n[标签] 标签\n\n早。",
                 "2026-07-30 10:00:00"),
                ("91001002", "[收录]\n[标题] 晚稿\n[作者] 甲\n[标签] 标签\n\n晚。",
                 "2026-08-02 10:00:00"),
                ("91001003", "[收录]\n[标题] 未知时间稿\n[作者] 甲\n[标签] 标签\n\n未知。",
                 "（时间未知）"),
            ],
        }
        transport = make_transport(tid, floors, last_page=1)
        state = make_state(observed_last_page=1)
        lower = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone(
            datetime.timedelta(hours=8))).astimezone(datetime.timezone.utc)
        config = ThreadConfig(
            schema_version="1", revision=0, tid=tid,
            canonical_url=f"https://bbs.nga.cn/read.php?tid={tid}",
            name="帖", enabled=True,
            start=ThreadStart(mode="date", page=None, date="2026-08-01",
                              timezone="UTC+08:00",
                              lower_bound_utc="2026-07-31T16:00:00+00:00"),
            start_confirmed=True, initialized=True,
            created_at=utc_now(), updated_at=utc_now(),
        )
        import tempfile as tf

        with tempfile.TemporaryDirectory() as tmp:
            new_state, counters = run_batch_scan(
                state, config, transport, Path(tmp) / "inbox.db",
                dry_run=False, controller=_Ctrl(), date_lower_bound=lower,
            )
        self.assertEqual(counters["date_excluded_count"], 1)
        self.assertEqual(counters["date_unresolved_count"], 1)
        self.assertEqual(counters["triggered_count"], 2)  # 晚稿 + 未知时间稿
        self.assertEqual(counters["imported_count"], 2)
        # 早稿进入 date_exclusions，未导入
        self.assertIn("91001001", new_state.date_exclusions)
        self.assertNotIn("91001002", new_state.date_exclusions)
        self.assertNotIn("91001003", new_state.date_exclusions)

    def test_discussion_not_entered_even_with_unknown_time(self) -> None:
        tid = "30099686"
        floors = {1: [("91001009", "普通讨论", "（时间未知）")]}
        transport = make_transport(tid, floors, last_page=1)
        state = make_state(observed_last_page=1)
        lower = datetime.datetime(2026, 8, 1, tzinfo=datetime.timezone(
            datetime.timedelta(hours=8))).astimezone(datetime.timezone.utc)
        config = ThreadConfig(
            schema_version="1", revision=0, tid=tid,
            canonical_url=f"https://bbs.nga.cn/read.php?tid={tid}",
            name="帖", enabled=True,
            start=ThreadStart(mode="date", page=None, date="2026-08-01",
                              timezone="UTC+08:00",
                              lower_bound_utc="2026-07-31T16:00:00+00:00"),
            start_confirmed=True, initialized=True,
            created_at=utc_now(), updated_at=utc_now(),
        )
        import tempfile as tf

        with tempfile.TemporaryDirectory() as tmp:
            new_state, counters = run_batch_scan(
                state, config, transport, Path(tmp) / "inbox.db",
                dry_run=False, controller=_Ctrl(), date_lower_bound=lower,
            )
        self.assertEqual(counters["discussion_count"], 1)
        self.assertEqual(counters["triggered_count"], 0)
        self.assertNotIn("91001009", new_state.date_exclusions)


if __name__ == "__main__":
    unittest.main()
