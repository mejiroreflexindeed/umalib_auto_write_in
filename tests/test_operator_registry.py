"""TASK-014 W03 帖子注册表测试（A011-A018 核心）。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.operator.paths import OperatorPaths
from umalib_ingest.operator.thread_registry import ConflictError, ThreadRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="op-registry-")
        self.paths = OperatorPaths(Path(self.tmp.name))
        self.paths.ensure_root()
        self.wsid = "11111111-1111-4111-8111-111111111111"
        self.registry = ThreadRegistry(self.paths, self.wsid)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_and_read_thread(self) -> None:
        entry = self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686",
                                         None, 0)
        self.assertEqual(entry.tid, "30099686")
        index = self.registry.read_index()
        self.assertEqual(index.revision, 1)
        self.assertEqual(index.active_tid, "30099686")
        self.assertEqual(index.entries[0].tid, "30099686")
        config = self.registry.read_config("30099686")
        self.assertEqual(config.start.mode, "now")
        self.assertFalse(config.start_confirmed)

    def test_duplicate_tid_no_reset(self) -> None:
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686", None, 0)
        entry2 = self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686&page=2",
                                          None, 1)
        self.assertEqual(entry2.tid, "30099686")
        index = self.registry.read_index()
        self.assertEqual(len(index.entries), 1)  # 不重复建
        self.assertEqual(index.revision, 1)  # 不清进度

    def test_two_threads_isolated(self) -> None:
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686", None, 0)
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=12345678", None, 1)
        index = self.registry.read_index()
        self.assertEqual({e.tid for e in index.entries}, {"30099686", "12345678"})
        self.assertTrue(self.paths.config_json(self.wsid, "30099686").exists())
        self.assertTrue(self.paths.config_json(self.wsid, "12345678").exists())

    def test_invalid_url_rejected(self) -> None:
        for url in ("https://evil.com/read.php?tid=1", "https://bbs.nga.cn/read.php?tid=0",
                    "https://x@bbs.nga.cn/read.php?tid=1"):
            with self.assertRaises(ValueError):
                self.registry.add_thread(url, None, 0)

    def test_cas_conflict(self) -> None:
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686", None, 0)
        config = self.registry.read_config("30099686")
        with self.assertRaises(ConflictError):
            self.registry.cas_config("30099686", 999, config)

    def test_patch_rename_disable_make_active(self) -> None:
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686", None, 0)
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=12345678", None, 1)
        index = self.registry.patch_thread(
            "12345678", 2, name="新名字", enabled=False, make_active=True)
        self.assertEqual(index.active_tid, "12345678")
        entry = next(e for e in index.entries if e.tid == "12345678")
        self.assertEqual(entry.name, "新名字")
        self.assertFalse(entry.enabled)
        config = self.registry.read_config("12345678")
        self.assertEqual(config.name, "新名字")
        self.assertFalse(config.enabled)
        self.assertEqual(config.revision, 1)

    def test_cas_success_updates_revision(self) -> None:
        self.registry.add_thread("https://bbs.nga.cn/read.php?tid=30099686", None, 0)
        config = self.registry.read_config("30099686")
        new = type(config).from_dict({**config.to_dict(), "revision": 1,
                                      "start_confirmed": True})
        result = self.registry.cas_config("30099686", 0, new)
        self.assertEqual(result.revision, 1)
        self.assertTrue(result.start_confirmed)


if __name__ == "__main__":
    unittest.main()
