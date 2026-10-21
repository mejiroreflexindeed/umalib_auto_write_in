"""TASK-013 M05 来源规范化纯函数测试（T33-T37）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.review.source_urls import normalize_source_url


class SourceUrlTests(unittest.TestCase):
    def test_tid_pid_normalized_and_to_page_removed(self) -> None:
        result = normalize_source_url(
            "https://bbs.nga.cn/read.php?tid=30099686&pid=123&to=1&page=2"
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.normalized,
                         "https://bbs.nga.cn/read.php?tid=30099686&pid=123")
        self.assertTrue(result.changed)

    def test_http_upgraded_to_https(self) -> None:
        result = normalize_source_url("http://bbs.nga.cn/read.php?tid=30099686&pid=1")
        self.assertEqual(result.normalized,
                         "https://bbs.nga.cn/read.php?tid=30099686&pid=1")

    def test_tid_only_kept_with_warning(self) -> None:
        result = normalize_source_url("https://bbs.nga.cn/read.php?tid=30099686")
        self.assertTrue(result.ready)
        self.assertEqual(result.normalized,
                         "https://bbs.nga.cn/read.php?tid=30099686")
        self.assertIn("url_tid_only_no_floor", result.warnings)

    def test_pid_without_tid_not_invented(self) -> None:
        result = normalize_source_url("https://bbs.nga.cn/read.php?pid=123")
        self.assertIn("url_pid_without_tid", result.blockers)
        self.assertFalse(result.ready)

    def test_conflicting_duplicate_params_blocked(self) -> None:
        result = normalize_source_url(
            "https://bbs.nga.cn/read.php?tid=1&tid=2&pid=1"
        )
        self.assertIn("url_tid_conflict", result.blockers)

    def test_invalid_ids_blocked(self) -> None:
        for url in (
            "https://bbs.nga.cn/read.php?tid=0&pid=1",
            "https://bbs.nga.cn/read.php?tid=abc",
            "https://bbs.nga.cn/read.php?tid=-1",
        ):
            result = normalize_source_url(url)
            self.assertFalse(result.ready, url)

    def test_userinfo_and_port_blocked(self) -> None:
        self.assertIn("url_userinfo_forbidden",
                      normalize_source_url("https://x@bbs.nga.cn/read.php?tid=1").blockers)
        self.assertIn("url_nondefault_port",
                      normalize_source_url("https://bbs.nga.cn:8443/read.php?tid=1").blockers)

    def test_other_external_links_kept_untouched(self) -> None:
        for url in (
            "https://www.pixiv.net/artworks/1",
            "https://example.com/x/y?z=1",
        ):
            result = normalize_source_url(url)
            self.assertTrue(result.ready, url)
            self.assertEqual(result.normalized, url)
            self.assertFalse(result.changed)

    def test_whitespace_blocked_not_truncated(self) -> None:
        result = normalize_source_url("https://bbs.nga.cn/read.php?tid=1 bad")
        self.assertIn("url_contains_whitespace", result.blockers)
        self.assertIsNone(result.normalized)


if __name__ == "__main__":
    unittest.main()
