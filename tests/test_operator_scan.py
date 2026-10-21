"""Behavior regressions for TASK-014 initialization and recurring scans."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from umalib_ingest.operator.app_factory import create_operator_app


def _floor(pid: str, content: str, posted: str) -> str:
    import html

    return (
        f'<tr id="post1strow{pid}"><a id="pid{pid}Anchor"></a>'
        f'<a id="postauthor{pid}">作者</a>'
        f'<span id="postdate{pid}">{html.escape(posted)}</span>'
        f'<span id="postcontent{pid}">{html.escape(content)}</span></tr>'
    )


def _html(tid: str, last_page: int, floors: list[tuple[str, str, str]]) -> bytes:
    pager = (
        f"<script>var __PAGE={{0:'/read.php?tid={tid}',1:{last_page},2:1,3:20}};"
        "commonui.pageBtn(document.getElementById('p').parentNode,__PAGE,true)</script>"
    )
    return ("<html><head><title>测试帖</title></head><body>" + pager
            + "".join(_floor(*entry) for entry in floors) + "</body></html>").encode()


class _Response:
    status = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def __init__(self, url: str, body: bytes) -> None:
        self.url = url
        self._body = body

    def body(self) -> bytes:
        return self._body


class _Page:
    def __init__(self, scenario: dict) -> None:
        self._scenario = scenario

    def goto(self, url: str, wait_until=None):
        page = int(url.split("&page=", 1)[1]) if "&page=" in url else 1
        floors = self._scenario["floors"].get(page, [])
        return _Response(url, _html(self._scenario["tid"],
                                    self._scenario["last_page"], floors))

    def title(self) -> str:
        return "测试帖"


class _Session:
    def __init__(self, scenario: dict) -> None:
        self.page = _Page(scenario)

    def close(self) -> None:
        return None


class OperatorScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="op-scan-")
        self.scenario = {
            "tid": "12345678", "last_page": 1,
            "floors": {1: [("80000001", "普通基线讨论", "2026-07-01 10:00:00")]},
        }
        self.ctx = create_operator_app(
            root=Path(self.temp.name), new_workspace=True,
            session_factory=lambda: _Session(self.scenario), nav_delay=0.0,
        )
        self.service = self.ctx.service
        self.service.add_thread(
            "https://bbs.nga.cn/read.php?tid=12345678", None, 0)

    def tearDown(self) -> None:
        self.ctx.shutdown()
        self.temp.cleanup()

    def _wait(self, run_id: str) -> dict:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = self.service.get_run(run_id)
            if record["status"] in (
                    "succeeded", "partial", "failed", "cancelled", "interrupted"):
                return record
            time.sleep(0.02)
        self.fail(f"run did not finish: {self.service.get_run(run_id)}")

    def _initialize(self, start: dict) -> dict:
        run_id = self.service.initialize_preview("12345678", start, 0, True)
        preview_run = self._wait(run_id)
        self.assertEqual(preview_run["status"], "succeeded", preview_run)
        result = preview_run["result"]
        self.service.initialize_confirm(
            "12345678", run_id, result["preview_sha256"], 0, True)
        return result

    def test_requested_page_is_used_during_preview(self) -> None:
        self.scenario["last_page"] = 2
        run_id = self.service.initialize_preview(
            "12345678",
            {"mode": "page", "page": 3, "date": None,
             "timezone": "UTC+08:00", "lower_bound_utc": None},
            0, True,
        )
        result = self._wait(run_id)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("超过尾页", result["error"]["message"])

    def test_date_start_is_applied_to_first_scan(self) -> None:
        self.scenario["floors"] = {1: [
            ("90000001", "[收录]\n[标题] 早稿\n[作者] 甲\n[标签] 标签\n\n正文",
             "2026-07-31 23:59:59"),
            ("90000002", "[收录]\n[标题] 当日稿\n[作者] 乙\n[标签] 标签\n\n正文",
             "2026-08-01 00:00:00"),
        ]}
        self._initialize({
            "mode": "date", "page": None, "date": "2026-08-01",
            "timezone": "UTC+08:00", "lower_bound_utc": "2026-07-31T16:00:00+00:00",
        })
        status = self.service.thread_status("12345678")
        scan_id = self.service.scan("12345678", False, True, status["state_sha256"])
        scan = self._wait(scan_id)
        self.assertEqual(scan["status"], "succeeded", scan)
        self.assertEqual(scan["result"]["date_excluded_count"], 1)
        self.assertEqual(scan["result"]["imported_count"], 1)

    def test_recurring_scan_discovers_a_new_tail_page(self) -> None:
        self._initialize({
            "mode": "now", "page": None, "date": None,
            "timezone": "UTC+08:00", "lower_bound_utc": None,
        })
        self.scenario["last_page"] = 2
        self.scenario["floors"][2] = [(
            "90000003", "[收录]\n[标题] 新页稿\n[作者] 丙\n[标签] 标签\n\n正文",
            "2026-09-04 10:00:00",
        )]
        status = self.service.thread_status("12345678")
        scan_id = self.service.scan("12345678", False, True, status["state_sha256"])
        scan = self._wait(scan_id)
        self.assertEqual(scan["status"], "succeeded", scan)
        self.assertEqual(scan["result"]["imported_count"], 1)
        after = self.service.thread_status("12345678")
        self.assertEqual(after["state"]["observed_last_page"], 2)

    def test_edited_submission_creates_a_new_review_item(self) -> None:
        self.scenario["floors"] = {1: [(
            "90000004", "[收录]\n[标题] 初稿\n[作者] 丁\n[标签] 标签\n\n第一版",
            "2026-09-04 10:00:00",
        )]}
        self._initialize({
            "mode": "page", "page": 1, "date": None,
            "timezone": "UTC+08:00", "lower_bound_utc": None,
        })
        status = self.service.thread_status("12345678")
        first_id = self.service.scan("12345678", False, True, status["state_sha256"])
        self.assertEqual(self._wait(first_id)["result"]["imported_count"], 1)
        self.scenario["floors"][1] = [(
            "90000004", "[收录]\n[标题] 修订稿\n[作者] 丁\n[标签] 标签\n\n第二版",
            "2026-09-04 10:00:00",
        )]
        status = self.service.thread_status("12345678")
        second_id = self.service.scan("12345678", False, True, status["state_sha256"])
        second = self._wait(second_id)
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(second["result"]["updated_count"], 1)
        self.assertEqual(second["result"]["imported_count"], 1)
        items, total = self.ctx.repository.list_items_page(thread_id="12345678")
        self.assertEqual(total, 2)
        self.assertEqual(items[0].working_draft["metadata"]["title"], "修订稿")

    def test_unknown_floor_date_is_visible_on_imported_draft(self) -> None:
        self.scenario["floors"] = {1: [(
            "90000005", "[收录]\n[标题] 时间未知\n[作者] 戊\n[标签] 标签\n\n正文",
            "刚刚",
        )]}
        self._initialize({
            "mode": "date", "page": None, "date": "2026-08-01",
            "timezone": "UTC+08:00", "lower_bound_utc": "2026-07-31T16:00:00+00:00",
        })
        status = self.service.thread_status("12345678")
        run_id = self.service.scan("12345678", False, True, status["state_sha256"])
        result = self._wait(run_id)
        self.assertEqual(result["result"]["date_unresolved_count"], 1)
        items, _ = self.ctx.repository.list_items_page(thread_id="12345678")
        warning_codes = [w["code"] for w in items[0].working_draft["diagnostics"]["warnings"]]
        self.assertIn("posted_at_unresolved", warning_codes)


if __name__ == "__main__":
    unittest.main()
