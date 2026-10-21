"""Offline tests for the NGA authenticated-read PoC (TASK-010).

All tests run without Playwright, Edge or network. The browser adapter is
exercised with a fake playwright module injected into sys.modules.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from umalib_ingest.nga_access import codec, parsing, snapshot, urls
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.paths import (
    ensure_distinct,
    validate_profile_dir,
    validate_snapshot_dir,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "nga_access"
PRIVATE_STATE = Path(r"D:\codex\13\umalib-private-state")


class UrlTests(unittest.TestCase):
    def test_valid_canonical(self) -> None:
        self.assertEqual(
            urls.canonical_thread_url(30099686, 1),
            "https://bbs.nga.cn/read.php?tid=30099686",
        )
        self.assertEqual(
            urls.canonical_thread_url(30099686, 2),
            "https://bbs.nga.cn/read.php?tid=30099686&page=2",
        )

    def test_parse_accepts_valid(self) -> None:
        self.assertEqual(
            urls.parse_thread_url("https://bbs.nga.cn/read.php?tid=30099686"),
            (30099686, 1),
        )
        self.assertEqual(
            urls.parse_thread_url("https://BBS.NGA.CN/read.php?tid=30099686&page=2"),
            (30099686, 2),
        )

    def test_parse_rejects_invalid(self) -> None:
        bad_urls = [
            "http://bbs.nga.cn/read.php?tid=30099686",
            "https://bbs.nga.cn.evil.com/read.php?tid=30099686",
            "https://nga.cn/read.php?tid=30099686",
            "https://bbs.nga.cn/other.php?tid=30099686",
            "https://bbs.nga.cn/read.php?tid=30099686&extra=1",
            "https://bbs.nga.cn/read.php?tid=30099686&tid=1",
            "https://bbs.nga.cn/read.php?tid=abc",
            "https://bbs.nga.cn/read.php?tid=",
            "https://bbs.nga.cn/read.php?tid=30099686&page=0",
            "https://bbs.nga.cn/read.php?tid=30099686&page=1&page=2",
            "https://user:pw@bbs.nga.cn/read.php?tid=30099686",
            "https://bbs.nga.cn:8443/read.php?tid=30099686",
            "https://bbs.nga.cn/read.php?tid=30099686#frag",
            "https://bbs.nga.cn/read.php?tid=30099686%2F1",
            "https://bbs.nga.cn/read.php?tid=30099686\n",
            "https://bbs.nga.cn/read.php?tid=30099686\x00",
            "",
        ]
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(NgaAccessError) as caught:
                    urls.parse_thread_url(url)
                self.assertEqual(caught.exception.code, "invalid_thread_url")

    def test_page_boundary_and_max(self) -> None:
        self.assertEqual(urls.MAX_PAGES, 2)
        with self.assertRaises(NgaAccessError):
            urls.canonical_thread_url(1, 0)
        with self.assertRaises(NgaAccessError):
            urls.canonical_thread_url(0, 1)

    def test_online_tid_fixed(self) -> None:
        urls.check_online_tid(30099686)
        with self.assertRaises(NgaAccessError):
            urls.check_online_tid(1)

    def test_validate_final_url_accepts_canonical(self) -> None:
        self.assertEqual(
            urls.validate_final_url(
                "https://bbs.nga.cn/read.php?tid=30099686", 30099686
            ),
            (30099686, 1),
        )
        self.assertEqual(
            urls.validate_final_url(
                "https://bbs.nga.cn/read.php?tid=30099686&page=173", 30099686
            ),
            (30099686, 173),
        )

    def test_validate_final_url_rejects_redirects(self) -> None:
        for url in (
            "https://bbs.nga.cn/read.php?tid=1",
            "https://evil.example.com/read.php?tid=30099686",
            "https://bbs.nga.cn/login.php",
            "https://bbs.nga.cn/read.php",
            "http://bbs.nga.cn/read.php?tid=30099686",
        ):
            with self.subTest(url=url):
                with self.assertRaises(NgaAccessError) as caught:
                    urls.validate_final_url(url, 30099686)
                self.assertEqual(caught.exception.code, "unexpected_redirect")


class PathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_profile_must_be_in_private_state(self) -> None:
        with self.assertRaises(NgaAccessError) as caught:
            validate_profile_dir(str(self.tmp / "profile"))
        self.assertEqual(caught.exception.code, "unsafe_profile_path")
        with self.assertRaises(NgaAccessError):
            validate_profile_dir("relative/profile")
        with self.assertRaises(NgaAccessError):
            validate_profile_dir("~/profile")
        with self.assertRaises(NgaAccessError):
            validate_profile_dir(str(Path.home() / "profile"))
        with self.assertRaises(NgaAccessError):
            validate_profile_dir(str(PROJECT_ROOT / "profile"))
        with self.assertRaises(NgaAccessError):
            validate_profile_dir("D:\\")

    def test_profile_edge_default_rejected(self) -> None:
        edge_default = Path(os.environ.get("LOCALAPPDATA", "C:\\")) / "Microsoft" / "Edge" / "User Data"
        with self.assertRaises(NgaAccessError):
            validate_profile_dir(str(edge_default))

    def test_valid_profile_and_snapshot(self) -> None:
        profile = validate_profile_dir(str(PRIVATE_STATE / "nga-profile"))
        snap = validate_snapshot_dir(str(PRIVATE_STATE / "nga-snapshots"))
        self.assertEqual(profile, PRIVATE_STATE / "nga-profile")
        self.assertEqual(snap, PRIVATE_STATE / "nga-snapshots")
        with self.assertRaises(NgaAccessError):
            ensure_distinct(profile, profile)
        ensure_distinct(profile, snap)

    def test_snapshot_safety(self) -> None:
        with self.assertRaises(NgaAccessError) as caught:
            validate_snapshot_dir(str(self.tmp / "snaps"))
        self.assertEqual(caught.exception.code, "unsafe_snapshot_path")


class CodecTests(unittest.TestCase):
    def test_content_type_charset_priority(self) -> None:
        body = "第一行".encode("gb18030")
        result = codec.decode_response(body, "text/html; charset=gbk")
        self.assertEqual(result.encoding, "gb18030")
        self.assertEqual(result.text, "第一行")
        self.assertIsNone(result.diagnostic)

    def test_meta_charset_fallback(self) -> None:
        body = '<meta charset="utf-8"><title>标题</title>'.encode("utf-8")
        result = codec.decode_response(body, "text/html")
        self.assertEqual(result.encoding, "utf-8")
        self.assertIn("标题", result.text)

    def test_gb18030_and_utf8_branches(self) -> None:
        gbk_body = "中文内容".encode("gb18030")
        result = codec.decode_response(gbk_body, None)
        self.assertEqual(result.encoding, "gb18030")
        utf8_body = "中文内容".encode("utf-8")
        result = codec.decode_response(utf8_body, None)
        self.assertEqual(result.encoding, "utf-8")

    def test_encoding_uncertain_no_loss(self) -> None:
        body = b"\xff\xfe\x00garbage\x81"
        result = codec.decode_response(body, "text/html")
        self.assertIsNone(result.text)
        self.assertEqual(result.diagnostic, "encoding_uncertain")

    def test_bad_charset_falls_through(self) -> None:
        body = "文本".encode("utf-8")
        result = codec.decode_response(body, "text/html; charset=bogus-encoding")
        self.assertEqual(result.encoding, "utf-8")


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.snap_dir = self.tmp / "snaps"
        self.snap_dir.mkdir()

    def _capture(self, label="1", body=b"<html>ok</html>", status=200):
        return snapshot.Capture(
            tid=30099686,
            page_label=label,
            status=status,
            final_url="https://bbs.nga.cn/read.php?tid=30099686",
            content_type="text/html; charset=utf-8",
            body_bytes=body,
            captured_at_utc="2026-08-31T00:00:00+00:00",
            diagnostics=(),
        )

    def test_write_and_unchanged(self) -> None:
        body = b"<html>same</html>"
        first = snapshot.write_response_snapshot(
            self.snap_dir, self._capture(body=body), "<html>same</html>"
        )
        self.assertEqual(first.status, "written")
        second = snapshot.write_response_snapshot(
            self.snap_dir, self._capture(body=body), "<html>same</html>"
        )
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(second.body_sha256, first.body_sha256)
        thread_dir = self.snap_dir / "tid-30099686"
        files = sorted(p.name for p in thread_dir.iterdir())
        self.assertIn("page-1.response.bin", files)
        self.assertIn("page-1.decoded.html", files)
        self.assertIn("page-1.meta.json", files)

    def test_conflict_group_uses_same_hash_suffix(self) -> None:
        body_a = b"<html>AAAA</html>"
        body_b = b"<html>BBBB</html>"
        first = snapshot.write_response_snapshot(
            self.snap_dir, self._capture(body=body_a), "<html>AAAA</html>"
        )
        self.assertEqual(first.status, "written")
        outcome = snapshot.write_response_snapshot(
            self.snap_dir, self._capture(body=body_b), "<html>BBBB</html>"
        )
        self.assertEqual(outcome.status, "conflict")
        thread_dir = self.snap_dir / "tid-30099686"
        suffix = outcome.body_sha256[:8]
        # 冲突版本：response、decoded、meta 必须使用相同 hash 后缀成组。
        self.assertTrue((thread_dir / f"page-1.{suffix}.response.bin").exists())
        self.assertTrue((thread_dir / f"page-1.{suffix}.decoded.html").exists())
        self.assertTrue((thread_dir / f"page-1.{suffix}.meta.json").exists())
        # 旧版本不被覆盖。
        self.assertEqual(
            (thread_dir / "page-1.response.bin").read_bytes(), body_a
        )
        self.assertEqual(
            (thread_dir / "page-1.decoded.html").read_text("utf-8"), "<html>AAAA</html>"
        )
        conflict_meta = json.loads(
            (thread_dir / f"page-1.{suffix}.meta.json").read_text("utf-8")
        )
        self.assertEqual(conflict_meta["files"]["body"], f"page-1.{suffix}.response.bin")
        self.assertEqual(
            conflict_meta["files"]["decoded"], f"page-1.{suffix}.decoded.html"
        )
        self.assertEqual(conflict_meta["files"]["meta"], f"page-1.{suffix}.meta.json")

    def test_meta_has_no_secrets_or_content(self) -> None:
        body = b"<html>SECRET CONTENT</html>"
        snapshot.write_response_snapshot(self.snap_dir, self._capture(body=body), None)
        meta = json.loads(
            (self.snap_dir / "tid-30099686" / "page-1.meta.json").read_text("utf-8")
        )
        raw = json.dumps(meta, ensure_ascii=False)
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("cookie", raw.lower())
        self.assertNotIn("author", raw.lower())
        self.assertNotIn(str(PROJECT_ROOT).replace("\\", "/"), raw)

    def test_atomic_write_failure_leaves_no_half_file(self) -> None:
        body = b"<html>atomic</html>"
        thread_dir = self.snap_dir / "tid-30099686"
        thread_dir.mkdir()
        target = thread_dir / "page-1.response.bin"
        import umalib_ingest.nga_access.snapshot as snapshot_module

        original = snapshot_module.os.replace
        snapshot_module.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
        try:
            with self.assertRaises(OSError):
                snapshot._atomic_write_bytes(target, body)
        finally:
            snapshot_module.os.replace = original
        self.assertFalse(target.exists())
        leftovers = [p for p in thread_dir.iterdir() if p.name.startswith(".nga-tmp-")]
        self.assertEqual(leftovers, [])

    def test_diagnostic_meta_written_without_content_files(self) -> None:
        snapshot.write_diagnostic_meta(
            self.snap_dir, 30099686, "1", {"status": 403, "diagnostics": ["thread_forbidden"]}
        )
        diag = self.snap_dir / "tid-30099686" / "page-1.diag.json"
        self.assertTrue(diag.exists())
        data = json.loads(diag.read_text("utf-8"))
        self.assertEqual(data["status"], 403)
        self.assertFalse((self.snap_dir / "tid-30099686" / "page-1.response.bin").exists())

    def test_manifest_no_content(self) -> None:
        payload = {
            "tid": 30099686,
            "pages": [{"body_sha256": "ab", "status": 200, "diagnostics": []}],
        }
        path = snapshot.write_probe_manifest(self.snap_dir, payload)
        self.assertTrue(path.exists())
        raw = path.read_text("utf-8")
        self.assertNotIn("SECRET", raw)


class ParsingTests(unittest.TestCase):
    """Parsing against the sanitized fixture mirroring the real logged-in
    response structure (tr#post1strowN / a#pid{PID}Anchor / postdateN /
    postcontentN UBB / __PAGE pager)."""

    def setUp(self) -> None:
        self.fixture = (FIXTURES_DIR / "sanitized_thread.html").read_text("utf-8")

    def test_parse_sanitized_fixture(self) -> None:
        thread = parsing.parse_html_thread(self.fixture, 30099686)
        self.assertTrue(thread.title_present)
        self.assertEqual(thread.current_page, 1)
        self.assertEqual(thread.last_page, 173)
        self.assertEqual([f.pid for f in thread.floors], ["111111111", "222222222", "333333333"])
        self.assertNotIn("floor_structure_unrecognized", thread.diagnostics)
        self.assertNotIn("pagination_uncertain", thread.diagnostics)
        first = thread.floors[0]
        self.assertTrue(first.author_present)
        self.assertIsNotNone(first.posted_at_raw)
        self.assertIn("[术语]注释", first.content_raw_fragment)
        self.assertEqual(first.content_format, "ubbcode")
        self.assertIsNone(first.floor_number)

    def test_topic_row_pid0_excluded(self) -> None:
        thread = parsing.parse_html_thread(self.fixture, 30099686)
        pids = [f.pid for f in thread.floors]
        self.assertNotIn("0", pids)

    def test_annotations_colors_links_preserved_in_raw(self) -> None:
        thread = parsing.parse_html_thread(self.fixture, 30099686)
        first = thread.floors[0]
        self.assertIn("[术语]注释", first.content_raw_fragment)
        self.assertIn("[color=#C00000]", first.content_raw_fragment)
        second = thread.floors[1]
        self.assertIn("[url=https://bbs.nga.cn/read.php?tid=99999999]", second.content_raw_fragment)
        self.assertIn("[quote]", second.content_raw_fragment)
        self.assertIn("站内链接", second.content_text)
        self.assertNotIn("[quote]", second.content_text)
        self.assertNotIn("[color=", first.content_text)
        self.assertIn("[术语]注释", first.content_text)

    def test_emoticons_stripped_in_text(self) -> None:
        thread = parsing.parse_html_thread(self.fixture, 30099686)
        third = thread.floors[2]
        self.assertIn("[s:pg:拒绝]", third.content_raw_fragment)
        self.assertNotIn("[s:pg:", third.content_text)

    def test_duplicate_pid_blocker(self) -> None:
        duplicated = self.fixture.replace(
            'id=\'pid222222222Anchor\'', 'id=\'pid111111111Anchor\''
        )
        with self.assertRaises(NgaAccessError) as caught:
            parsing.parse_html_thread(duplicated, 30099686)
        self.assertEqual(caught.exception.code, "duplicate_floor_pid")

    def test_unrecognized_structure_diagnostic(self) -> None:
        thread = parsing.parse_html_thread("<html><body>无楼层</body></html>", 1)
        self.assertIn("floor_structure_unrecognized", thread.diagnostics)
        self.assertEqual(thread.floors, ())

    def test_pagination_uncertain_without_pager(self) -> None:
        page = (
            "<html><body>"
            "<table class='forumbox postbox'>"
            "<tr id='post1strow1'><td class='c2' id='postcontainer1'>"
            "<a id='pid999999999Anchor'></a>"
            "<div class='postInfo' id='postInfo1'><span id='postdate1'>2026-08-01 10:00</span></div>"
            "<span id='postcontentandsubject1'><span id='postcontent1' class='postcontent ubbcode'>正文</span></span>"
            "</td></tr></table></body></html>"
        )
        thread = parsing.parse_html_thread(page, 1)
        self.assertIn("pagination_uncertain", thread.diagnostics)
        self.assertIsNone(thread.last_page)
        self.assertEqual(len(thread.floors), 1)

    def test_edited_at_absent_when_no_evidence(self) -> None:
        thread = parsing.parse_html_thread(self.fixture, 30099686)
        self.assertTrue(all(f.edited_at_raw is None for f in thread.floors))

    def test_json_parser_experimental(self) -> None:
        fixture = json.loads(
            (FIXTURES_DIR / "sanitized_thread.json").read_text("utf-8")
        )
        thread = parsing.parse_json_thread(fixture, 30099686)
        self.assertEqual(len(thread.floors), 2)
        self.assertEqual(thread.floors[0].pid, "888000001")
        self.assertEqual(thread.floors[0].floor_number, 1)
        self.assertEqual(thread.floors[1].floor_number, 2)
        self.assertTrue(thread.floors[0].author_present)
        self.assertEqual(thread.floors[0].posted_at_raw, "2026-08-01 10:05")
        self.assertEqual(thread.current_page, 1)
        self.assertIn("json_observation_experimental", thread.diagnostics)
        self.assertIn("[术语]注释", thread.floors[0].content_raw_fragment)
        self.assertNotIn("[b]", thread.floors[0].content_text)
        with self.assertRaises(NgaAccessError) as caught:
            parsing.parse_json_thread({"data": {"__R": [{"pid": 1}, {"pid": 1}]}}, 1)
        self.assertEqual(caught.exception.code, "duplicate_floor_pid")
        unknown = parsing.parse_json_thread({"data": {}}, 1)
        self.assertIn("floor_structure_unrecognized", unknown.diagnostics)
        unknown2 = parsing.parse_json_thread({"unknown": True}, 1)
        self.assertIn("floor_structure_unrecognized", unknown2.diagnostics)


class BrowserAdapterTests(unittest.TestCase):
    """Fake-playwright assertions on the adapter contract."""

    def _patch_import(self, manager_factory):
        """Patch the adapter's lazy import to return a fake manager factory."""
        import umalib_ingest.nga_access.browser as browser_module

        patcher = mock.patch.object(
            browser_module, "_import_sync_playwright", return_value=manager_factory
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return browser_module

    def test_adapter_uses_msedge_headless_false_explicit_profile(self) -> None:
        captured = {}

        class FakeContext:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.pages = []
                self.routes = []

            def new_page(self):
                return object()

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

            def close(self):
                pass

        class FakeChromium:
            def launch_persistent_context(self, **kwargs):
                return FakeContext(**kwargs)

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FakeManager:
            def start(self):
                return FakePlaywright()

        browser = self._patch_import(lambda: FakeManager())
        profile = PRIVATE_STATE / "nga-profile"
        session = browser.launch_probe_session(profile)
        self.assertEqual(captured["kwargs"]["channel"], "msedge")
        self.assertFalse(captured["kwargs"]["headless"])
        self.assertEqual(captured["kwargs"]["user_data_dir"], str(profile))
        session.close()

    def test_adapter_no_sensitive_calls(self) -> None:
        captured = {}

        class FakeContext:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.pages = []
                self.routes = []

            def route(self, pattern, handler):
                self.routes.append((pattern, handler))

            def close(self):
                pass

        class FakeChromium:
            def launch_persistent_context(self, **kwargs):
                return FakeContext(**kwargs)

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FakeManager:
            def start(self):
                return FakePlaywright()

        browser = self._patch_import(lambda: FakeManager())
        session = browser.launch_probe_session(PRIVATE_STATE / "nga-profile")
        self.assertNotIn("storage_state", captured["kwargs"])
        self.assertNotIn("proxy", captured["kwargs"])
        self.assertNotIn("ignore_https_errors", captured["kwargs"])
        self.assertNotIn("args", captured["kwargs"])
        self.assertNotIn("extensions", captured["kwargs"])
        session.close()

    def test_launch_failure_closes_playwright(self) -> None:
        import umalib_ingest.nga_access.browser as browser_module

        class FakeChromium:
            def launch_persistent_context(self, **kwargs):
                raise RuntimeError("Executable doesn't exist at .../msedge.exe")

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FakeManager:
            def start(self):
                return FakePlaywright()

        patcher = mock.patch.object(
            browser_module, "_import_sync_playwright", return_value=lambda: FakeManager()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(NgaAccessError) as caught:
            browser_module.launch_probe_session(PRIVATE_STATE / "nga-profile")
        self.assertEqual(caught.exception.code, "edge_channel_unavailable")

    def test_not_installed_error(self) -> None:
        import umalib_ingest.nga_access.browser as browser_module

        patcher = mock.patch.object(
            browser_module, "_import_sync_playwright", side_effect=ImportError()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(NgaAccessError) as caught:
            browser_module.launch_probe_session(PRIVATE_STATE / "nga-profile")
        self.assertEqual(caught.exception.code, "playwright_not_installed")


class CliOfflineTests(unittest.TestCase):
    def test_help_does_not_import_playwright(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.nga_access", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src")),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe", result.stdout)
        self.assertIn("inspect-snapshot", result.stdout)

    def test_inspect_snapshot_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = Path(tmp)
            manifest = {
                "tid": 30099686,
                "pages": [
                    {
                        "page_label": "1",
                        "status": 200,
                        "body_sha256": "ab" * 32,
                        "floor_count": 3,
                        "unique_pid_count": 3,
                        "encoding": "utf-8",
                        "snapshot_status": "written",
                        "diagnostics": [],
                    }
                ],
                "diagnostics": [],
            }
            (snap_dir / "probe-result.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    sys.executable, "-B", "-m", "umalib_ingest.nga_access",
                    "inspect-snapshot", "--snapshot-dir", str(snap_dir),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src")),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("tid: 30099686", result.stdout)
            self.assertIn("floors=3", result.stdout)


class ProbeOfflineTests(unittest.TestCase):
    """run_probe with a fake playwright session; no network, no Edge."""

    def setUp(self) -> None:
        PRIVATE_STATE.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE), prefix="nga-test-")
        self.addCleanup(self._tmp.cleanup)
        self.snap_dir = Path(self._tmp.name) / "snaps"

    def _install_fake_playwright(self, responses):
        """Patch the adapter import with fake playwright returning staged responses."""
        import umalib_ingest.nga_access.browser as browser_module

        class FakeResponse:
            def __init__(self, status, url, content_type, body):
                self._status = status
                self._url = url
                self.headers = {"content-type": content_type}
                self._body = body

            @property
            def status(self):
                return self._status

            @property
            def url(self):
                return self._url

            def body(self):
                return self._body

        class FakePage:
            wait_calls = 0
            last_goto_url = None

            def __init__(self):
                self.goto_count = 0
                self.title_value = "标题"

            def goto(self, url, wait_until=None):
                self.goto_count += 1
                FakePage.last_goto_url = url
                response = responses.pop(0)
                return FakeResponse(
                    response[0], response[1] or url, response[2], response[3]
                )

            def title(self):
                return self.title_value

            def wait_for_timeout(self, ms):
                FakePage.wait_calls += 1

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]
                self.closed = False

            def route(self, pattern, handler):
                self.routes = getattr(self, "routes", [])
                self.routes.append((pattern, handler))

            def close(self):
                self.closed = True

        class FakeChromium:
            def launch_persistent_context(self, **kwargs):
                return FakeContext()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FakeManager:
            def start(self):
                return FakePlaywright()

        patcher = mock.patch.object(
            browser_module, "_import_sync_playwright", return_value=lambda: FakeManager()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        FakePage.wait_calls = 0
        FakePage.last_goto_url = None
        return FakePage

    def test_probe_forbidden_diagnostic(self) -> None:
        from umalib_ingest.nga_access import probe

        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
                (403, "https://bbs.nga.cn/read.php?tid=30099686", "text/html", b"<html>403</html>"),
            ]
        )
        result = probe.run_probe(
            "https://bbs.nga.cn/read.php?tid=30099686",
            PRIVATE_STATE / "nga-profile",
            self.snap_dir,
            max_pages=1,
            confirm_login=lambda: None,
            json_probe=False,
        )
        self.assertEqual(result.pages[0].status, 403)
        self.assertIn("thread_forbidden", result.pages[0].diagnostics)

    def test_probe_success_path_with_fixture(self) -> None:
        from umalib_ingest.nga_access import probe

        body = (FIXTURES_DIR / "sanitized_thread.html").read_bytes()
        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
                (200, "https://bbs.nga.cn/read.php?tid=30099686", "text/html; charset=utf-8", body),
            ]
        )
        result = probe.run_probe(
            "https://bbs.nga.cn/read.php?tid=30099686",
            PRIVATE_STATE / "nga-profile",
            self.snap_dir,
            max_pages=1,
            confirm_login=lambda: None,
            json_probe=False,
        )
        page_result = result.pages[0]
        self.assertEqual(page_result.status, 200)
        self.assertEqual(page_result.floor_count, 3)
        self.assertEqual(page_result.unique_pid_count, 3)
        self.assertEqual(page_result.snapshot_status, "written")
        self.assertTrue((self.snap_dir / "tid-30099686" / "page-1.response.bin").exists())
        self.assertTrue((self.snap_dir / "tid-30099686" / "page-1.decoded.html").exists())
        self.assertTrue((self.snap_dir / "probe-result.json").exists())
        manifest = json.loads((self.snap_dir / "probe-result.json").read_text("utf-8"))
        raw = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn("合成作者", raw)
        self.assertNotIn("这是第一楼", raw)


    def test_probe_redirect_aborts_before_saving(self) -> None:
        from umalib_ingest.nga_access import probe

        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
                (200, "https://evil.example.com/phish", "text/html", b"<html>redirected</html>"),
            ]
        )
        with self.assertRaises(NgaAccessError) as caught:
            probe.run_probe(
                "https://bbs.nga.cn/read.php?tid=30099686",
                PRIVATE_STATE / "nga-profile",
                self.snap_dir,
                max_pages=1,
                confirm_login=lambda: None,
                json_probe=False,
            )
        self.assertEqual(caught.exception.code, "unexpected_redirect")
        self.assertFalse((self.snap_dir / "tid-30099686").exists())

    def test_probe_unrecognized_structure_is_diagnostic_only(self) -> None:
        from umalib_ingest.nga_access import probe

        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
                (200, "https://bbs.nga.cn/read.php?tid=30099686", "text/html; charset=utf-8",
                 b"<html><body>no floor structure here</body></html>"),
            ]
        )
        result = probe.run_probe(
            "https://bbs.nga.cn/read.php?tid=30099686",
            PRIVATE_STATE / "nga-profile",
            self.snap_dir,
            max_pages=1,
            confirm_login=lambda: None,
            json_probe=False,
        )
        page = result.pages[0]
        self.assertIn("floor_structure_unrecognized", page.diagnostics)
        self.assertEqual(page.snapshot_status, "skipped")
        thread_dir = self.snap_dir / "tid-30099686"
        self.assertTrue((thread_dir / "page-1.diag.json").exists())
        self.assertFalse((thread_dir / "page-1.response.bin").exists())
        self.assertFalse((thread_dir / "page-1.decoded.html").exists())

    def test_probe_json_observation_waits_before_navigation(self) -> None:
        from umalib_ingest.nga_access import probe

        fixture_body = (FIXTURES_DIR / "sanitized_thread.html").read_bytes()
        fake_page_class = self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
                (200, "https://bbs.nga.cn/read.php?tid=30099686", "text/html; charset=utf-8",
                 fixture_body),
                (200, "https://bbs.nga.cn/read.php?tid=30099686&__output=11",
                 "text/json;charset=UTF-8", b'{"data":{},"time":1}'),
            ]
        )
        result = probe.run_probe(
            "https://bbs.nga.cn/read.php?tid=30099686",
            PRIVATE_STATE / "nga-profile",
            self.snap_dir,
            max_pages=1,
            confirm_login=lambda: None,
            json_probe=True,
        )
        self.assertEqual(result.json_observation["status"], 200)
        self.assertEqual(result.json_observation["top_shape"], "object")
        self.assertGreaterEqual(fake_page_class.wait_calls, 1)
        self.assertIn("__output=11", fake_page_class.last_goto_url)
        from umalib_ingest.nga_access import probe

        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/", "text/html", b"<html>home</html>"),
            ]
        )
        with self.assertRaises(NgaAccessError) as caught:
            probe.run_probe(
                "https://bbs.nga.cn/read.php?tid=30099686",
                PRIVATE_STATE / "nga-profile",
                self.snap_dir,
                max_pages=1,
                json_probe=False,
                login_wait_file=self.snap_dir / "never-created",
                login_wait_timeout_seconds=0.5,
            )
        self.assertEqual(caught.exception.code, "login_not_confirmed")


class SubresourceGuardTests(unittest.TestCase):
    def test_image_media_font_always_blocked(self) -> None:
        from umalib_ingest.nga_access.browser import should_block_request

        for resource_type in ("image", "media", "font"):
            self.assertTrue(
                should_block_request("https://bbs.nga.cn/read.php?tid=1", resource_type),
                resource_type,
            )

    def test_document_navigations_not_blocked(self) -> None:
        from umalib_ingest.nga_access.browser import should_block_request

        self.assertFalse(
            should_block_request("https://bbs.nga.cn/read.php?tid=1", "document")
        )

    def test_nga_hosts_allowed_for_scripts_xhr(self) -> None:
        from umalib_ingest.nga_access.browser import should_block_request

        for url in (
            "https://bbs.nga.cn/js/main.js",
            "https://www.nga.cn/js/a.js",
            "https://img.nga.178.com/attachments/foo.js",
            "https://static.nga.cn/css/x.css",
        ):
            self.assertFalse(should_block_request(url, "script"), url)
        self.assertFalse(
            should_block_request("https://bbs.nga.cn/read.php?tid=1", "xhr")
        )

    def test_non_nga_hosts_blocked(self) -> None:
        from umalib_ingest.nga_access.browser import should_block_request

        for url in (
            "https://evil.example.com/steal.js",
            "https://bbs.nga.cn.evil.com/x.js",
            "https://cdn.jsdelivr.net/x.js",
            "https://example.com/img/1.png",
        ):
            self.assertTrue(should_block_request(url, "script"), url)

    def test_route_handler_installed_and_applied(self) -> None:
        from umalib_ingest.nga_access.browser import install_subresource_guard

        decisions = []

        class FakeRequest:
            def __init__(self, url, resource_type):
                self.url = url
                self.resource_type = resource_type

        class FakeRoute:
            def __init__(self, request):
                self.request = request
                self.action = None

            def abort(self):
                self.action = "abort"

            def continue_(self):
                self.action = "continue"

        class FakeContext:
            def __init__(self):
                self.handler = None

            def route(self, pattern, handler):
                self.handler = handler

        context = FakeContext()
        install_subresource_guard(context)
        self.assertIsNotNone(context.handler)
        cases = [
            (FakeRequest("https://img.nga.178.com/1.jpg", "image"), "abort"),
            (FakeRequest("https://evil.example.com/x.js", "script"), "abort"),
            (FakeRequest("https://bbs.nga.cn/js/main.js", "script"), "continue"),
            (FakeRequest("https://bbs.nga.cn/read.php?tid=1", "document"), "continue"),
        ]
        for request, expected in cases:
            route = FakeRoute(request)
            context.handler(route)
            self.assertEqual(route.action, expected, request.url)


class EntryPointTests(unittest.TestCase):
    def test_module_help_commands_available(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.nga_access", "probe", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src")),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--thread-url", result.stdout)
        self.assertIn("--login-wait-file", result.stdout)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "umalib_ingest.nga_access", "inspect-snapshot", "--help"],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT / "src")),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--snapshot-dir", result.stdout)

    def test_console_entry_points_resolve(self) -> None:
        import importlib

        pyproject = (PROJECT_ROOT / "pyproject.toml").read_text("utf-8")
        self.assertIn('nga-probe = "umalib_ingest.nga_access.__main__:probe_main"', pyproject)
        self.assertIn(
            'nga-inspect-snapshot = "umalib_ingest.nga_access.__main__:inspect_snapshot_main"',
            pyproject,
        )
        module = importlib.import_module("umalib_ingest.nga_access.__main__")
        self.assertTrue(callable(module.probe_main))
        self.assertTrue(callable(module.inspect_snapshot_main))
        for entry in (module.probe_main, module.inspect_snapshot_main):
            with self.assertRaises(SystemExit) as caught:
                entry(["--help"])
            self.assertEqual(caught.exception.code, 0)


class FetchApiTests(unittest.TestCase):
    """Tests for the extracted public fetch_thread_page API (TASK-011)."""

    def setUp(self) -> None:
        PRIVATE_STATE.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(PRIVATE_STATE), prefix="nga-fetch-")
        self.addCleanup(self._tmp.cleanup)
        self.snap_dir = Path(self._tmp.name) / "snaps"

    def _install_fake_playwright(self, responses):
        import umalib_ingest.nga_access.browser as browser_module
        import types as _types

        class FakeResponse:
            def __init__(self, status, url, content_type, body):
                self._status = status
                self._url = url
                self.headers = {"content-type": content_type}
                self._body = body

            @property
            def status(self):
                return self._status

            @property
            def url(self):
                return self._url

            def body(self):
                return self._body

        class FakePage:
            def __init__(self):
                self.title_value = "标题"

            def goto(self, url, wait_until=None):
                response = responses.pop(0)
                return FakeResponse(
                    response[0], response[1] or url, response[2], response[3]
                )

            def title(self):
                return self.title_value

            def wait_for_timeout(self, ms):
                pass

        class FakeContext:
            def __init__(self):
                self.pages = [FakePage()]

            def route(self, pattern, handler):
                pass

            def close(self):
                pass

        class FakeChromium:
            def launch_persistent_context(self, **kwargs):
                return FakeContext()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class FakeManager:
            def start(self):
                return FakePlaywright()

        patcher = mock.patch.object(
            browser_module, "_import_sync_playwright", return_value=lambda: FakeManager()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fetch_returns_parsed_floors(self) -> None:
        from umalib_ingest.nga_access.browser import launch_probe_session
        from umalib_ingest.nga_access.fetch import fetch_thread_page

        body = (FIXTURES_DIR / "sanitized_thread.html").read_bytes()
        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/read.php?tid=30099686",
                 "text/html; charset=utf-8", body),
            ]
        )
        session = launch_probe_session(PRIVATE_STATE / "nga-profile")
        fetched = fetch_thread_page(session, 30099686, 1, self.snap_dir)
        self.assertEqual(fetched.page, 1)
        self.assertEqual(fetched.last_page, 173)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(len(fetched.floors), 3)
        self.assertEqual(fetched.floors[0].pid, "111111111")
        self.assertEqual(fetched.snapshot_outcome, "written")
        self.assertEqual(fetched.diagnostics, ())
        session.close()

    def test_fetch_unrecognized_is_diagnostic_only(self) -> None:
        from umalib_ingest.nga_access.browser import launch_probe_session
        from umalib_ingest.nga_access.fetch import fetch_thread_page

        self._install_fake_playwright(
            [
                (200, "https://bbs.nga.cn/read.php?tid=30099686",
                 "text/html; charset=utf-8", b"<html><body>nothing</body></html>"),
            ]
        )
        session = launch_probe_session(PRIVATE_STATE / "nga-profile")
        fetched = fetch_thread_page(session, 30099686, 1, self.snap_dir)
        self.assertIn("floor_structure_unrecognized", fetched.diagnostics)
        self.assertEqual(fetched.snapshot_outcome, "skipped")
        self.assertEqual(fetched.floors, ())
        self.assertFalse((self.snap_dir / "tid-30099686" / "page-1.response.bin").exists())
        session.close()

    def test_fetch_redirect_raises_before_save(self) -> None:
        from umalib_ingest.nga_access.browser import launch_probe_session
        from umalib_ingest.nga_access.fetch import fetch_thread_page

        self._install_fake_playwright(
            [
                (200, "https://evil.example.com/x", "text/html", b"<html>x</html>"),
            ]
        )
        session = launch_probe_session(PRIVATE_STATE / "nga-profile")
        with self.assertRaises(NgaAccessError) as caught:
            fetch_thread_page(session, 30099686, 1, self.snap_dir)
        self.assertEqual(caught.exception.code, "unexpected_redirect")
        self.assertFalse((self.snap_dir / "tid-30099686").exists())
        session.close()


if __name__ == "__main__":
    unittest.main()
