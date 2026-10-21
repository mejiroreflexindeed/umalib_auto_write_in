"""TASK-015 Q2：矩阵生成器证据校验（缺证据/失败结果各一小验证）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.build_acceptance_matrix import build  # noqa: E402


def _feature_map(path: Path, ids: list[str]) -> Path:
    data = {"schema_version": "1", "features": [
        {"id": i, "requirement": "req " + i, "dev_package": "D1"} for i in ids]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _map_file(path: Path, ids: list[str], evidence_file: str, needle: str) -> Path:
    data = {"schema_version": "1", "groups": [{
        "ids": ids, "status": "PASS", "test": "t", "command": "c",
        "evidence": [{"kind": "unittest_log", "file": evidence_file,
                      "must_contain": [needle]}],
    }]}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class MatrixEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="matrix-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_evidence_is_not_pass(self) -> None:
        fmap = _feature_map(self.root / "fm.json", ["A001"])
        mmap = _map_file(self.root / "map.json", ["A001"], "logs/missing.log", "x ... ok")
        matrix = build(fmap, self.root, mmap)
        item = next(i for i in matrix["items"] if i["id"] == "A001")
        self.assertEqual(item["status"], "BLOCKED")
        self.assertNotEqual(item["status"], "PASS")

    def test_failed_result_is_fail(self) -> None:
        (self.root / "logs").mkdir()
        (self.root / "logs" / "t.log").write_text(
            "test_x ... FAIL\nFAILED (failures=1)\n", encoding="utf-8")
        fmap = _feature_map(self.root / "fm.json", ["A002"])
        mmap = _map_file(self.root / "map.json", ["A002"], "logs/t.log", "test_x ... ok")
        item = next(i for i in build(fmap, self.root, mmap)["items"] if i["id"] == "A002")
        self.assertEqual(item["status"], "FAIL")

    def test_passing_log_is_pass(self) -> None:
        (self.root / "logs").mkdir()
        (self.root / "logs" / "ok.log").write_text(
            "test_y (m.T) ... ok\n\nRan 1 tests\n\nOK\n", encoding="utf-8")
        fmap = _feature_map(self.root / "fm.json", ["A003"])
        mmap = _map_file(self.root / "map.json", ["A003"], "logs/ok.log",
                         "test_y (m.T) ... ok")
        item = next(i for i in build(fmap, self.root, mmap)["items"] if i["id"] == "A003")
        self.assertEqual(item["status"], "PASS")

    def test_unmapped_defaults_partial(self) -> None:
        fmap = _feature_map(self.root / "fm.json", ["A099"])
        mmap = _map_file(self.root / "map.json", [], "", "")
        item = next(i for i in build(fmap, self.root, mmap)["items"] if i["id"] == "A099")
        self.assertEqual(item["status"], "PARTIAL")


if __name__ == "__main__":
    unittest.main()
