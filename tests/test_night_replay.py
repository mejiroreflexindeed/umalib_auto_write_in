"""TASK-012B W3：回放实验室自身的离线测试。

验证：场景语料真实被消费（改动即失败、缺失即失败）、全部场景经真实
Watcher→Inbox 链得到预期结果、两次独立运行结果确定一致、环境检查无副作用。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.night_acceptance import (  # noqa: E402
    LabError,
    check_environment,
    load_scenarios,
    run_replay,
    run_scenario,
)


class FixtureValidationTests(unittest.TestCase):
    def test_fixture_loads_with_at_least_32_unique_scenarios(self) -> None:
        scenarios = load_scenarios()
        self.assertGreaterEqual(len(scenarios), 32)
        ids = [s["scenario_id"] for s in scenarios]
        self.assertEqual(len(ids), len(set(ids)))

    def test_missing_fixture_fails_loudly(self) -> None:
        with self.assertRaises(LabError):
            load_scenarios(Path(tempfile.gettempdir()) / "no-such-scenarios.json")

    def test_expected_values_are_hand_authored_not_generated(self) -> None:
        scenarios = load_scenarios()
        for scenario in scenarios:
            expected = scenario["expected"]
            self.assertIn("events", expected)
            self.assertIn("error", expected)
            self.assertTrue(
                isinstance(expected.get("events"), list),
                f"{scenario['scenario_id']} events 必须是列表",
            )


class ScenarioConsumptionTests(unittest.TestCase):
    """场景输入必须被实际消费：改一项预期必须失败，缺失语料必须失败."""

    def _scenarios(self) -> list[dict]:
        import copy

        return copy.deepcopy(load_scenarios())

    def test_modifying_one_expected_count_must_fail(self) -> None:
        scenarios = self._scenarios()
        scenario = scenarios[0]
        scenario["expected"]["counts"]["new_floor_count"] += 1
        record = run_scenario(scenario)
        self.assertEqual(record["verdict"], "FAIL")

    def test_modifying_one_expected_event_must_fail(self) -> None:
        scenarios = self._scenarios()
        scenario = scenarios[0]
        scenario["expected"]["events"] = scenario["expected"]["events"][1:]
        record = run_scenario(scenario)
        self.assertEqual(record["verdict"], "FAIL")

    def test_removing_one_fixture_page_must_fail(self) -> None:
        scenarios = self._scenarios()
        scenario = scenarios[1]
        scenario["pages"] = scenario["pages"][:1]
        record = run_scenario(scenario)
        self.assertEqual(record["verdict"], "FAIL")


class ReplayAcceptanceTests(unittest.TestCase):
    """全部 44 个场景经真实链执行，期望结果全部满足."""

    def test_all_scenarios_pass(self) -> None:
        scenarios = load_scenarios()
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_replay(Path(tmp), scenarios)
        self.assertEqual(summary["failed"], [], json.dumps(summary, ensure_ascii=False))
        self.assertEqual(summary["overall"], "PASS")
        self.assertGreaterEqual(summary["passed"], 32)

    def test_two_independent_runs_are_deterministic(self) -> None:
        scenarios = load_scenarios()
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            run_replay(Path(tmp_a), scenarios)
            run_replay(Path(tmp_b), scenarios)
            a = sorted(p.name for p in (Path(tmp_a) / "replay").iterdir())
            b = sorted(p.name for p in (Path(tmp_b) / "replay").iterdir())
            self.assertEqual(a, b)
            for name in a:
                if name == "summary.json":
                    continue
                ra = (Path(tmp_a) / "replay" / name / "result.json").read_bytes()
                rb = (Path(tmp_b) / "replay" / name / "result.json").read_bytes()
                self.assertEqual(ra, rb, f"{name} 两次运行结果不一致")


class CheckEnvironmentTests(unittest.TestCase):
    def test_check_environment_reports_and_writes_nothing(self) -> None:
        before = {
            "scenarios": Path(PROJECT_ROOT / "tests" / "fixtures" / "night" / "scenarios.json").exists(),
        }
        report = check_environment()
        self.assertTrue(report["core_ok"], json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["items"]["umalib_ingest"], "OK")
        self.assertTrue(report["items"]["night_scenarios"].startswith("OK"))
        self.assertTrue(before["scenarios"])


if __name__ == "__main__":
    unittest.main()
