"""Synthetic HTTP bytes -> real fetch/codec/HTML/Watcher/Parser/Inbox.

No real browser, credentials, network or existing database is accessed.
Expectations live in responses.json, not in the implementation under test.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

from tools.night_acceptance import LabError, PROJECT_ROOT, T0

FIXTURE = PROJECT_ROOT / "tests/fixtures/night/responses.json"


def load_responses(path: Path | None = None) -> dict[str, Any]:
    """Load hand-authored response fixtures; reject missing/malformed inputs."""
    try:
        data = json.loads((path or FIXTURE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LabError(f"响应语料不可用: {type(exc).__name__}") from None
    if not isinstance(data, dict) or not isinstance(data.get("bodies"), dict):
        raise LabError("响应语料缺少 bodies")
    cases = data.get("scenarios")
    if not isinstance(cases, list) or not cases:
        raise LabError("响应语料缺少 scenarios")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or not re.fullmatch(r"H\d{2,3}", str(case.get("id", ""))):
            raise LabError("响应场景 ID 非法")
        if case["id"] in ids or not case.get("responses") or not isinstance(case.get("expected"), dict):
            raise LabError("响应场景重复或不完整")
        ids.add(case["id"])
    return data


def run_response(case: dict[str, Any], bodies: dict[str, str]) -> dict[str, Any]:
    """Run a byte-response fixture through the real chain in an owned temp dir."""
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.nga_access import fetch
    from umalib_ingest.watcher import scan
    from umalib_ingest.watcher.state import PRIVATE_STATE_ROOT, WatcherConfig, atomic_write_json

    events = []
    fetched = []
    close_count = 0
    specs = iter(case["responses"])

    class Response:
        def __init__(self, spec, url):
            self.url = spec.get("url", url)
            self.status = spec.get("status", 200)
            self.headers = {"content-type": "text/html; charset=" + spec.get("charset", "utf-8")}
            self._body = (bytes.fromhex(spec["hex"]) if "hex" in spec else
                          bodies[spec["body"]].encode(spec.get("encoding", "utf-8")))

        def body(self):
            return self._body

    class Page:
        def goto(self, url, **kwargs):
            events.append("fetch:1")
            return Response(next(specs), url)

        def title(self):
            return "离线响应测试"

    class Session:
        page = Page()

        def close(self):
            nonlocal close_count
            close_count += 1

    original_import = scan._import_floor

    def do_import(repo, tid, floor, diagnostics):
        events.append(f"import:{floor.pid}")
        return original_import(repo, tid, floor, diagnostics)

    def do_fetch(*args):
        result = fetch.fetch_thread_page(*args)
        fetched.append(result)
        return result

    with tempfile.TemporaryDirectory(dir=PRIVATE_STATE_ROOT, prefix="night-response-") as tmp:
        root = Path(tmp)
        state_file = root / "state.json"
        initial = {"schema_version": "1", "tid": 30099686,
                   "canonical_thread_url": "https://bbs.nga.cn/read.php?tid=30099686",
                   "last_page": 1, "known_floors": {}, "baseline_created_at": T0,
                   "last_successful_scan_at": None, "last_result_summary": None}
        atomic_write_json(state_file, initial)
        before = state_file.read_bytes()
        config = WatcherConfig(state_file=state_file, profile_dir=root / "profile",
                               snapshot_dir=root / "snaps", inbox_db=root / "inbox.db",
                               navigation_delay_seconds=3.0, max_scan_pages=12)
        error = None
        try:
            with mock.patch.object(scan, "_import_floor", do_import):
                scan.run_scan(config, dry_run=False, session_factory=Session, fetch_fn=do_fetch,
                              sleep=lambda seconds: events.append(f"sleep:{seconds}"),
                              confirm_login=lambda: events.append("confirm"), clock=lambda: T0)
        except NgaAccessError as exc:
            error = exc.code
        expected = case["expected"]
        repo = InboxRepository(config.inbox_db)
        items = [repo.get(item.id) for item in repo.list_items()] if config.inbox_db.exists() else []
        drafts = [item.original_draft for item in items]
        checks = []

        def check(name, got, wanted):
            checks.append({"check": name, "ok": got == wanted,
                           "detail": {"got": got, "expected": wanted}})

        check("error", error, expected["error"])
        check("events", events, expected["events"])
        check("session_close", close_count, 1)
        check("statuses", [item.status for item in items],
              [expected["status"]] if expected["status"] else [])
        if error:
            check("state_unchanged", state_file.read_bytes() == before, True)
        files = list((root / "snaps").rglob("*"))
        check("body_snapshots", any(p.name.endswith((".bin", ".html")) for p in files),
              expected["body_snapshots"])
        if expected.get("diagnostic"):
            check("diagnostic", any(expected["diagnostic"] in f.diagnostics for f in fetched), True)
        for field, key in (("raw_text", "raw_contains"), ("body_text", "body_contains")):
            for token in expected.get(key, []):
                check(f"{key}:{token}", any(token in d["content"][field] for d in drafts), True)
        return {"scenario_id": case["id"], "category": "response-chain",
                "purpose": case["purpose"], "basis": "hand-authored synthetic HTTP responses",
                "error_code": error, "events": events, "checks": checks,
                "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
