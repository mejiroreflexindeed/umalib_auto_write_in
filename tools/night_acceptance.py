"""TASK-012B W3/W4/W5：离线回放实验室与发布候选验收工具（night_acceptance）。

S 系列替代已解析页面，测试真实 Watcher/Inbox；H 系列只替代 HTTP 响应/session，
测试真实 fetch/解码/HTML/Watcher/Inbox。所有输入均为合成数据，无真实 NGA 访问。
证据写入 --output；临时 state/Inbox 位于私有目录下的独立随机子目录，结束时清理。

用法：
  python -B tools/night_acceptance.py --check-environment
  python -B tools/night_acceptance.py --phase replay --output <新证据根>
  python -B tools/night_acceptance.py --phase recovery --output <证据根>
  python -B tools/night_acceptance.py --phase ui --output <证据根>
  python -B tools/night_acceptance.py --phase all --output <新证据根>
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "night" / "scenarios.json"
T0 = "2026-09-02T00:00:00.000000+00:00"

_EXIT_OK = 0
_EXIT_FAIL = 1
_EXIT_BLOCKED = 2


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _log(message: str) -> None:
    print(message, flush=True)


class LabError(Exception):
    """Lab-internal configuration/fixture error (not a scenario failure)."""


class LabUnavailable(LabError):
    """Required test environment is missing; never a successful phase."""


# --------------------------------------------------------------------------- #
# check-environment：只读，不写库、不启浏览器、不联网
# --------------------------------------------------------------------------- #

def check_environment() -> dict[str, Any]:
    import importlib.util
    import platform
    import shutil

    report: dict[str, Any] = {
        "checked_at_utc": _utc_now(),
        "python_version": platform.python_version(),
        "project_root": str(PROJECT_ROOT),
        "items": {},
    }
    try:
        import umalib_ingest  # noqa: F401

        report["items"]["umalib_ingest"] = "OK"
    except Exception as exc:  # pragma: no cover - environment dependent
        report["items"]["umalib_ingest"] = f"BLOCKED: {type(exc).__name__}: {exc}"

    try:
        scenarios = load_scenarios()
        from tools.night_responses import load_responses
        load_responses()
        report["items"]["night_scenarios"] = f"OK ({len(scenarios)} scenarios)"
    except Exception as exc:  # pragma: no cover
        report["items"]["night_scenarios"] = f"BLOCKED: {type(exc).__name__}: {exc}"

    report["items"]["playwright"] = (
        "OK" if importlib.util.find_spec("playwright") is not None else "unavailable"
    )
    edge = shutil.which("msedge")
    if edge is None:
        for candidate in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ):
            if Path(candidate).exists():
                edge = candidate
                break
    report["items"]["msedge"] = edge if edge else "unavailable"
    report["items"]["night_lab"] = "OK"
    report["core_ok"] = (
        sys.version_info >= (3, 10)
        and report["items"]["umalib_ingest"] == "OK"
        and report["items"]["night_scenarios"].startswith("OK")
    )
    return report


# --------------------------------------------------------------------------- #
# 场景加载与校验
# --------------------------------------------------------------------------- #

_REQUIRED_KEYS = ("scenario_id", "category", "purpose", "basis", "state", "pages", "expected")


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or SCENARIOS_PATH
    if not target.exists():
        raise LabError(f"缺少场景语料: {target}")
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise LabError(f"场景语料不是合法 JSON: {exc}") from None
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list) or not scenarios:
        raise LabError("场景语料缺少非空 scenarios 列表")
    ids: set[str] = set()
    for scenario in scenarios:
        import re
        if not isinstance(scenario, dict) or not re.fullmatch(r"S\d{2,3}", str(scenario.get("scenario_id", ""))):
            raise LabError("场景必须是对象且 ID 必须为 S 加两到三位数字")
        missing = [key for key in _REQUIRED_KEYS if key not in scenario]
        if missing:
            raise LabError(
                f"场景 {scenario.get('scenario_id', '?')} 缺少键: {sorted(missing)}"
            )
        if scenario["scenario_id"] in ids:
            raise LabError(f"场景 ID 重复: {scenario['scenario_id']}")
        ids.add(scenario["scenario_id"])
    if len(scenarios) < 32:
        raise LabError(f"场景数量不足 32: {len(scenarios)}")
    return scenarios


# --------------------------------------------------------------------------- #
# 场景执行（回放）
# --------------------------------------------------------------------------- #

def _floor_of(spec: dict[str, Any]):
    from umalib_ingest.nga_access.parsing import FloorSnapshot

    return FloorSnapshot(
        pid=spec["pid"],
        floor_number=None,
        author_present=True,
        posted_at_raw="2026-09-02 10:00",
        edited_at_raw=None,
        content_format="ubbcode",
        content_text=spec.get("content_text", spec["content"]),
        content_raw_fragment=spec["content"],
    )


def _page_of(spec: dict[str, Any], page: int):
    from umalib_ingest.nga_access.fetch import FetchedThreadPage

    floors = tuple(_floor_of(f) for f in spec.get("floors", []))
    return FetchedThreadPage(
        tid=30099686,
        page=page,
        last_page=spec.get("last_page"),
        status=spec.get("status", 200),
        final_url=f"https://bbs.nga.cn/read.php?tid=30099686&page={page}",
        content_type="text/html; charset=utf-8",
        encoding="utf-8",
        body_sha256="ab" * 32,
        body_bytes=0,
        snapshot_outcome="written",
        floors=floors,
        diagnostics=tuple(spec.get("diagnostics", [])),
    )


def _initial_state_dict(scenario: dict[str, Any]) -> dict[str, Any]:
    from umalib_ingest.watcher.diff import floor_content_sha256
    from umalib_ingest.nga_access.parsing import FloorSnapshot

    known: dict[str, Any] = {}
    for pid, spec in scenario["state"].get("known_floors", {}).items():
        sha = floor_content_sha256(
            FloorSnapshot(
                pid=pid, floor_number=None, author_present=True,
                posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
                content_format="ubbcode", content_text=spec["content"],
                content_raw_fragment=spec["content"],
            )
        )
        known[pid] = {
            "pid": pid,
            "content_sha256": sha,
            "last_seen_page": scenario["state"]["last_page"],
            "trigger_found": bool(spec.get("trigger_found")),
            "inbox_item_id": spec.get("inbox_item_id"),
            "imported_content_sha256": sha if spec.get("imported") else None,
            "changed_after_import": bool(spec.get("changed_after_import")),
        }
    return {
        "schema_version": "1",
        "tid": 30099686,
        "canonical_thread_url": "https://bbs.nga.cn/read.php?tid=30099686",
        "last_page": scenario["state"]["last_page"],
        "known_floors": known,
        "baseline_created_at": T0,
        "last_successful_scan_at": None,
        "last_result_summary": None,
    }


class _FakeSession:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def run_scenario(
    scenario: dict[str, Any], work_root: Path | None = None
) -> dict[str, Any]:
    """Own and clean a synthetic workspace even on early setup failures."""
    import tempfile
    from umalib_ingest.watcher.state import PRIVATE_STATE_ROOT
    if work_root is not None:
        return _run_scenario(scenario, work_root)
    with tempfile.TemporaryDirectory(dir=PRIVATE_STATE_ROOT, prefix="night-lab-") as tmp:
        return _run_scenario(scenario, Path(tmp))


def _run_scenario(scenario: dict[str, Any], work_root: Path) -> dict[str, Any]:
    """Semantic replay: parsed pages are injected; Watcher/Inbox are real."""

    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher import scan as scan_module
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import (
        PRIVATE_STATE_ROOT,
        WatcherConfig,
        atomic_write_json,
        load_state,
    )

    scenario_id = scenario["scenario_id"]
    work_root.mkdir(parents=True, exist_ok=True)
    state_file = work_root / "watcher-state.json"
    inbox_db = work_root / "inbox.db"
    snapshot_dir = work_root / "snaps"
    profile_dir = work_root / "profile"
    atomic_write_json(state_file, _initial_state_dict(scenario))

    events: list[str] = []
    cancel_state: dict[str, Any] = {"after_fetch": 0, "during_sleep": 0, "flag": False}
    cancel_spec = scenario.get("cancel") or {}
    cancel_state["after_fetch"] = int(cancel_spec.get("after_fetch", 0))
    cancel_state["during_sleep"] = int(cancel_spec.get("during_sleep", 0))

    page_call_counts: dict[int, int] = {}
    pages = {entry["page"]: entry for entry in scenario["pages"]}
    fetch_error = {entry["page"]: entry.get("fetch_error") for entry in scenario["pages"]}

    session = _FakeSession()

    def session_factory():
        if scenario.get("session_error"):
            raise NgaAccessError(
                scenario["session_error"]["code"], "simulated session failure"
            )
        return session

    def fetch_fn(session_arg, tid, page, snapshot_dir_arg):
        page_call_counts[page] = page_call_counts.get(page, 0) + 1
        if fetch_error.get(page):
            raise NgaAccessError(fetch_error[page]["code"], "simulated fetch error")
        entry = pages[page]
        if page_call_counts[page] > 1 and "retry" in entry:
            spec = dict(entry)
            spec.update(entry["retry"])
        else:
            spec = entry
        events.append(f"fetch:{page}")
        return _page_of(spec, page)

    def sleep(seconds):
        events.append(f"sleep:{seconds}")
        if cancel_state["during_sleep"]:
            cancel_state["during_sleep"] -= 1
            cancel_state["flag"] = True

    def should_cancel():
        if cancel_state["after_fetch"] and len(
            [e for e in events if e.startswith("fetch:")]
        ) >= cancel_state["after_fetch"]:
            cancel_state["flag"] = True
        return cancel_state["flag"]

    def confirm_login():
        events.append("confirm")

    if scenario.get("pre_lock"):
        (work_root / "watcher-state.json.lock").write_text("", encoding="utf-8")

    config = WatcherConfig(
        state_file=state_file,
        profile_dir=profile_dir,
        snapshot_dir=snapshot_dir,
        inbox_db=inbox_db,
        navigation_delay_seconds=float(scenario.get("navigation_delay_seconds", 3.0)),
        max_scan_pages=int(scenario.get("max_scan_pages", 12)),
    )

    original_import = scan_module._import_floor

    def wrapped_import(repo, tid, floor, diagnostics):
        events.append(f"import:{floor.pid}")
        return original_import(repo, tid, floor, diagnostics)

    error_code: str | None = None
    result = None
    patches = [
        mock.patch.object(scan_module, "_import_floor", wrapped_import),
    ]
    if scenario.get("inject", {}).get("atomic_write_fails"):
        patches.append(
            mock.patch.object(
                scan_module, "atomic_write_json",
                side_effect=OSError("simulated atomic write failure"),
            )
        )
    try:
        for patch in patches:
            patch.start()
        kwargs: dict[str, Any] = {
            "session_factory": session_factory,
            "fetch_fn": fetch_fn,
            "sleep": sleep,
            "clock": lambda: T0,
            "should_cancel": should_cancel,
        }
        if scenario.get("confirm"):
            kwargs["confirm_login"] = confirm_login
        if scenario.get("login_wait_timeout"):
            kwargs["login_wait_file"] = work_root / "never-created-wait-file"
            kwargs["login_wait_timeout_seconds"] = float(scenario["login_wait_timeout"])
        if scenario.get("expected_state_sha256"):
            kwargs["expected_state_sha256"] = scenario["expected_state_sha256"]
        result = run_scan(config, dry_run=False, **kwargs)
    except NgaAccessError as exc:
        error_code = exc.code
    except OSError as exc:
        error_code = "OSError"
    except Exception as exc:  # unexpected: record type
        error_code = type(exc).__name__
    finally:
        for patch in reversed(patches):
            patch.stop()

    all_events = events
    checks: list[dict[str, Any]] = []
    expected = scenario["expected"]

    def check(name: str, ok: bool, detail: Any) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("events", all_events == expected["events"], {"expected": expected["events"], "got": all_events})
    check("error", error_code == expected["error"], {"expected": expected["error"], "got": error_code})
    check("session_close", session.close_count == expected.get("session_close", 1),
          {"expected": expected.get("session_close", 1), "got": session.close_count})

    if result is not None and expected.get("counts") is not None:
        counts = {
            "new_floor_count": result.new_floor_count,
            "unchanged_floor_count": result.unchanged_floor_count,
            "changed_floor_count": result.changed_floor_count,
            "discussion_count": result.discussion_count,
            "triggered_count": result.triggered_count,
            "imported_count": len(result.imported_items),
        }
        check("counts", counts == expected["counts"],
              {"expected": expected["counts"], "got": counts})
    elif expected.get("counts") is None:
        check("counts", result is None, {"expected_error_path": error_code})

    if expected.get("changed_after_import") is not None:
        got_changed = list(result.changed_after_import) if result is not None else []
        check("changed_after_import", got_changed == expected["changed_after_import"],
              {"expected": expected["changed_after_import"], "got": got_changed})

    if expected.get("diagnostics_contains"):
        got_diag = list(result.diagnostics) if result is not None else []
        check("diagnostics_contains",
              all(code in got_diag for code in expected["diagnostics_contains"]),
              {"expected": expected["diagnostics_contains"], "got": got_diag})

    # Inbox 校验（dry-run 之外的场景）：按 pid 匹配 status
    inbox_check: list[dict[str, Any]] = []
    if inbox_db.exists():
        repo = InboxRepository(inbox_db)
        repo.initialize()
        items = []
        for summary in repo.list_items():
            item = repo.get(summary.id)
            if item is None:
                continue
            origin = (item.original_draft.get("origin") or {})
            items.append({"pid": origin.get("post_id"), "status": item.status,
                          "draft": item.original_draft})
        expected_items = expected.get("inbox_items") or []
        got_summary = [{"pid": i["pid"], "status": i["status"]} for i in items]
        check("inbox_items", got_summary == expected_items,
              {"expected": expected_items, "got": got_summary})
        if expected.get("draft_contains"):
            draft_blob = json.dumps([i["draft"] for i in items], ensure_ascii=False)
            check("draft_contains",
                  all(token in draft_blob for token in expected["draft_contains"]),
                  {"expected": expected["draft_contains"]})
        inbox_check = items
    elif expected.get("inbox_items"):
        check("inbox_items", False, {"expected": expected["inbox_items"], "got": "inbox not created"})
    else:
        check("inbox_items", True, {"expected": [], "got": []})

    # state 校验
    state_check = {}
    if state_file.exists():
        try:
            state = load_state(state_file)
            state_check = {
                "last_page": state.last_page,
                "floors": {
                    pid: {
                        "trigger_found": f.trigger_found,
                        "inbox_item_id": f.inbox_item_id,
                        "changed_after_import": f.changed_after_import,
                        "imported_matches_content": (
                            f.imported_content_sha256 is not None
                            and f.imported_content_sha256 == f.content_sha256
                        ),
                    }
                    for pid, f in state.known_floors.items()
                },
            }
        except Exception as exc:
            state_check = {"load_error": f"{type(exc).__name__}: {exc}"}
    check("last_page_after",
          state_check.get("last_page") == expected.get("last_page_after"),
          {"expected": expected.get("last_page_after"), "got": state_check.get("last_page")})
    expected_floors = expected.get("state_floors") or {}
    got_floors = state_check.get("floors", {})
    floors_ok = set(got_floors) == set(expected_floors)
    floor_detail: dict[str, Any] = {}
    for pid, exp in expected_floors.items():
        got = got_floors.get(pid, {})
        detail = {}
        detail["trigger_found"] = got.get("trigger_found") == exp.get("trigger_found")
        exp_item = exp.get("inbox_item_id")
        if exp_item == "set":
            detail["inbox_item_id"] = got.get("inbox_item_id") is not None
        else:
            detail["inbox_item_id"] = got.get("inbox_item_id") == exp_item
        detail["changed_after_import"] = got.get("changed_after_import") == exp.get(
            "changed_after_import", False
        )
        if exp.get("imported_content_sha256") == "same":
            detail["imported_content_sha256"] = got.get("imported_matches_content") is True
        floors_ok = floors_ok and all(detail.values())
        floor_detail[pid] = detail
    check("state_floors", floors_ok, {"expected": expected_floors, "detail": floor_detail})

    verdict = all(c["ok"] for c in checks)
    record = {
        "scenario_id": scenario_id,
        "category": scenario["category"],
        "purpose": scenario["purpose"],
        "basis": scenario["basis"],
        "verdict": "PASS" if verdict else "FAIL",
        "error_code": error_code,
        "events": all_events,
        "checks": checks,
    }
    return record


def _prepare_output_root(output: Path) -> None:
    if output.exists():
        if any(output.iterdir()):
            raise LabError(f"证据根已存在且非空（不覆盖）: {output}")
    else:
        output.mkdir(parents=True)


def run_replay(output: Path, scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    from tools.night_responses import load_responses, run_response
    responses = load_responses()
    phase_root = output / "replay"
    _prepare_output_root(phase_root)
    records: list[dict[str, Any]] = []
    def response_job(case):
        return lambda: run_response(case, responses["bodies"])

    jobs = [(scenario, lambda s=scenario: run_scenario(s)) for scenario in scenarios]
    jobs.extend(({"scenario_id": case["id"], "category": "response-chain",
                  "purpose": case["purpose"], "basis": "synthetic HTTP"}, response_job(case))
                for case in responses["scenarios"])
    for scenario, job in jobs:
        scenario_id = scenario["scenario_id"]
        scenario_root = phase_root / scenario_id
        try:
            record = job()
        except Exception as exc:
            record = {**scenario, "verdict": "FAIL", "error_code": type(exc).__name__,
                      "events": [], "checks": [{"check": "scenario_execution", "ok": False,
                                                "detail": type(exc).__name__}]}
        verdict = record["verdict"]
        scenario_root.mkdir(parents=True, exist_ok=True)
        (scenario_root / "result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        lines = [f"scenario: {scenario_id} [{verdict}]", f"category: {record['category']}",
                 f"purpose: {record['purpose']}", f"basis: {record['basis']}",
                 f"error_code: {record['error_code']}", f"events: {record['events']}"]
        for check in record["checks"]:
            lines.append(f"  - {check['check']}: {'OK' if check['ok'] else 'FAIL'} {json.dumps(check['detail'], ensure_ascii=False)}")
        (scenario_root / "summary.txt").write_text("\n".join(lines), encoding="utf-8")
        records.append(record)
        _log(f"[replay] {scenario_id}: {verdict}")

    passed = sum(1 for r in records if r["verdict"] == "PASS")
    failed = [r["scenario_id"] for r in records if r["verdict"] != "PASS"]
    summary = {
        "phase": "replay",
        "total": len(records),
        "passed": passed,
        "semantic_cases": len(scenarios),
        "response_cases": len(responses["scenarios"]),
        "failed": failed,
        "overall": "PASS" if not failed else "FAIL",
    }
    (phase_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _cleanup_work(work_root: Path) -> None:
    import shutil

    if work_root.exists():
        shutil.rmtree(work_root, ignore_errors=True)


def run_recovery(output: Path) -> dict[str, Any]:  # W4 实现
    from tools.night_recovery import run_recovery as impl  # type: ignore[import-not-found]

    return impl(output)


def run_ui(output: Path) -> dict[str, Any]:  # W5 实现
    from tools.night_ui import run_ui as impl  # type: ignore[import-not-found]

    return impl(output)


def run_all(output: Path, scenarios_path: Path | None = None) -> dict[str, Any]:
    """Run independent phases and retain failures, including unavailable tools."""
    scenarios = load_scenarios(scenarios_path)
    parts = {}
    for phase, fn in (("replay", lambda p: run_replay(p, scenarios)),
                      ("recovery", run_recovery), ("ui", run_ui)):
        try:
            parts[phase] = fn(output)
        except LabUnavailable as exc:
            parts[phase] = {"phase": phase, "overall": "UNAVAILABLE",
                            "reason": str(exc)}
        except ModuleNotFoundError as exc:
            parts[phase] = {"phase": phase,
                "overall": "UNAVAILABLE" if exc.name and exc.name.split('.')[0] == "playwright" else "FAIL",
                "reason": "missing dependency" if exc.name == "playwright" else "internal import error"}
        except Exception as exc:
            parts[phase] = {"phase": phase, "overall": "FAIL", "reason": type(exc).__name__}
    overall = all(p.get("overall") == "PASS" for p in parts.values())
    (output / "all-summary.json").write_text(
        json.dumps({"parts": parts, "overall": "PASS" if overall else "FAIL"},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return parts


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B tools/night_acceptance.py",
        description="TASK-012B 离线回放实验室（只替代网络/浏览器/时钟）",
    )
    parser.add_argument("--phase", choices=("replay", "recovery", "ui", "all"))
    parser.add_argument("--check-environment", action="store_true",
                        help="只读环境检查（不写库、不启浏览器、不联网）")
    parser.add_argument("--output", type=Path, default=None,
                        help="证据根目录（新目录或空目录；已存在结果不覆盖）")
    parser.add_argument("--scenarios", type=Path, default=None,
                        help="场景语料路径（默认 tests/fixtures/night/scenarios.json）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check_environment and args.phase is None:
        report = check_environment()
        for key, value in report["items"].items():
            _log(f"  {key}: {value}")
        _log("环境检查: " + ("OK" if report["core_ok"] else "BLOCKED"))
        return _EXIT_OK if report["core_ok"] else _EXIT_BLOCKED
    if args.check_environment and args.phase is not None:
        _log("错误: --check-environment 与 --phase 不能同时使用")
        return _EXIT_BLOCKED
    if args.phase is None:
        _log("错误: 需要 --check-environment 或 --phase replay/recovery/ui/all")
        return _EXIT_BLOCKED

    if args.phase in ("replay", "recovery", "ui", "all") and args.output is None:
        _log("错误: --phase replay/recovery/ui/all 必须提供 --output 证据根")
        return _EXIT_BLOCKED
    output: Path = args.output  # type: ignore[assignment]
    try:
        if args.phase == "all":
            _prepare_output_root(output)
        else:
            # Each phase still refuses to overwrite its own evidence directory.
            output.mkdir(parents=True, exist_ok=True)
    except LabError as exc:
        _log(f"错误: {exc}")
        return _EXIT_BLOCKED

    try:
        if args.phase == "replay":
            summary = run_replay(output, load_scenarios(args.scenarios))
            _log(f"replay: {summary['passed']}/{summary['total']} PASS, overall={summary['overall']}")
            return _EXIT_OK if summary["overall"] == "PASS" else _EXIT_FAIL
        if args.phase == "all":
            parts = run_all(output, args.scenarios)
            for name, part in parts.items():
                _log(f"{name}: {part.get('overall', part)}")
            return _EXIT_OK if all(
                p.get("overall") == "PASS" for p in parts.values()
            ) else _EXIT_FAIL
        if args.phase == "recovery":
            summary = run_recovery(output)
            _log(f"recovery: overall={summary.get('overall')}")
            return _EXIT_OK if summary.get("overall") == "PASS" else _EXIT_FAIL
        if args.phase == "ui":
            summary = run_ui(output)
            _log(f"ui: overall={summary.get('overall')}")
            return _EXIT_OK if summary.get("overall") == "PASS" else _EXIT_FAIL
    except LabError as exc:
        _log(f"错误: {exc}")
        return _EXIT_BLOCKED
    except Exception as exc:
        _log(f"内部错误: {type(exc).__name__}: {exc}")
        return _EXIT_FAIL
    return _EXIT_BLOCKED


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    raise SystemExit(main())
