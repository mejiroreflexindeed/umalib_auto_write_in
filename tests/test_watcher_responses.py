"""Response-level regression; not an online NGA acceptance."""
import copy
import tempfile
import unittest
from pathlib import Path
from tools.night_responses import load_responses, run_response
from tools.night_acceptance import LabError


class ResponseChainTests(unittest.TestCase):
    def test_all_response_cases(self):
        data = load_responses()
        for case in data["scenarios"]:
            with self.subTest(case=case["id"]):
                record = run_response(case, data["bodies"])
                self.assertEqual(record["verdict"], "PASS", record)

    def test_fixture_is_consumed(self):
        data = copy.deepcopy(load_responses())
        data["bodies"]["submission"] = "<p>结构已变更</p>"
        record = run_response(data["scenarios"][0], data["bodies"])
        self.assertEqual(record["verdict"], "FAIL")

    def test_missing_fixture_blocks(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(LabError):
            load_responses(Path(tmp) / "missing.json")
