"""TASK-012B W4：故障与重启恢复矩阵（离线、确定注入、真实子进程终止）。

中断点覆盖（映射见 run_recovery 输出 index.json）：
1 session 创建前、2 第一页返回后、3 等待登录中、4 login-confirm 与 cancel 竞态、
5 导航 sleep 中、6 下一页失败、7 diff 结束后、8 首次 import 前、
9 首个 import 已提交后、10 所有 import 后 state 写入前、11 原子替换失败、
12 请求客户端断开/server 关闭。

只终止本次登记的子进程（Popen 对象），不按进程名杀用户应用。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tools.night_acceptance import LabError  # noqa: E402

TID = 30099686
CONTENT = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n正文甲。"
T0 = "2026-09-02T00:00:00.000000+00:00"


def _check(name: str, ok: bool, detail: Any) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _make_temp_work(prefix: str) -> Path:
    from umalib_ingest.watcher.state import PRIVATE_STATE_ROOT

    return Path(tempfile.mkdtemp(dir=str(PRIVATE_STATE_ROOT), prefix=prefix))


def _write_initial_state(state_file: Path) -> bytes:
    from umalib_ingest.watcher.state import WatcherState, atomic_write_json

    state = WatcherState(
        schema_version="1",
        tid=TID,
        canonical_thread_url=f"https://bbs.nga.cn/read.php?tid={TID}",
        last_page=173,
        known_floors={},
        baseline_created_at=T0,
        last_successful_scan_at=None,
        last_result_summary=None,
    )
    atomic_write_json(state_file, state.to_dict())
    return state_file.read_bytes()


def _normal_scan(state_file: Path, inbox_db: Path, work: Path, *,
                 fetch_calls: list[int] | None = None):
    from umalib_ingest.nga_access.fetch import FetchedThreadPage
    from umalib_ingest.nga_access.parsing import FloorSnapshot
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import WatcherConfig

    def floor(pid, content):
        return FloorSnapshot(
            pid=pid, floor_number=None, author_present=True,
            posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
            content_format="ubbcode", content_text=content, content_raw_fragment=content,
        )

    good = FetchedThreadPage(
        tid=TID, page=173, last_page=173, status=200,
        final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page=173",
        content_type="text/html; charset=utf-8", encoding="utf-8",
        body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
        floors=(floor("910009001", CONTENT), floor("910009002", CONTENT)),
        diagnostics=(),
    )

    class FakeSession:
        def close(self):
            pass

    def fetch_fn(session, tid, page, snapshot_dir):
        if fetch_calls is not None:
            fetch_calls.append(page)
        if page == 1:
            return FetchedThreadPage(
                tid=TID, page=1, last_page=173, status=200,
                final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page=1",
                content_type="text/html; charset=utf-8", encoding="utf-8",
                body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
                floors=(), diagnostics=(),
            )
        return good

    config = WatcherConfig(
        state_file=state_file, profile_dir=work / "profile",
        snapshot_dir=work / "snaps", inbox_db=inbox_db,
        navigation_delay_seconds=3.0, max_scan_pages=12,
    )
    return run_scan(config, dry_run=False, session_factory=FakeSession,
                    fetch_fn=fetch_fn, sleep=lambda s: None, clock=lambda: T0)


def _inbox_summary(inbox_db: Path) -> list[dict[str, Any]]:
    from umalib_ingest.inbox import InboxRepository

    repo = InboxRepository(inbox_db)
    repo.initialize()
    items = []
    for summary in repo.list_items():
        item = repo.get(summary.id)
        if item is None:
            continue
        origin = item.original_draft.get("origin") or {}
        items.append({"item_id": item.id, "pid": origin.get("post_id"),
                      "status": item.status})
    return items


def check_close_failure_not_masked() -> dict[str, Any]:
    """session.close 自身失败不得掩盖原始 page_fetch_failed."""
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import WatcherConfig

    work = _make_temp_work("night-rec-close-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "inbox.db"
    _write_initial_state(state_file)

    class BrokenSession:
        def close(self):
            raise RuntimeError("close exploded")

    def fetch_fn(session, tid, page, snapshot_dir):
        raise NgaAccessError("page_fetch_failed", "simulated")

    config = WatcherConfig(
        state_file=state_file, profile_dir=work / "profile",
        snapshot_dir=work / "snaps", inbox_db=inbox_db,
        navigation_delay_seconds=3.0, max_scan_pages=12,
    )
    caught = None
    try:
        run_scan(config, dry_run=False, session_factory=BrokenSession,
                 fetch_fn=fetch_fn, sleep=lambda s: None)
    except NgaAccessError as exc:
        caught = exc.code
    except Exception as exc:
        caught = type(exc).__name__
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    return {
        "point": "close-failure",
        "checks": [_check("original_error_kept", caught == "page_fetch_failed", caught)],
        "verdict": "PASS" if caught == "page_fetch_failed" else "FAIL",
    }


def check_schema_mismatch_blocks_before_network() -> dict[str, Any]:
    """user_version 不支持的假库必须在 session/网络之前阻断."""
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import WatcherConfig

    work = _make_temp_work("night-rec-schema-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "fake.db"
    _write_initial_state(state_file)
    conn = sqlite3.connect(str(inbox_db))
    conn.execute("PRAGMA user_version = 99")
    conn.close()

    fetch_calls: list[int] = []
    caught = None

    class FakeSession:
        def close(self):
            pass

    config = WatcherConfig(
        state_file=state_file, profile_dir=work / "profile",
        snapshot_dir=work / "snaps", inbox_db=inbox_db,
        navigation_delay_seconds=3.0, max_scan_pages=12,
    )
    try:
        run_scan(config, dry_run=False, session_factory=FakeSession,
                 fetch_fn=lambda s, t, p, d: fetch_calls.append(p) or None,
                 sleep=lambda s: None)
    except Exception as exc:
        caught = type(exc).__name__
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    checks = [
        _check("schema_mismatch_raised", caught == "UnsupportedInboxSchemaError", caught),
        _check("no_network", fetch_calls == [], fetch_calls),
    ]
    return {
        "point": "schema-mismatch",
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def check_two_scans_lock_only_one_proceeds() -> dict[str, Any]:
    """两个 scan 争同一 state：后者 watcher_locked 且未联网；释放后成功."""
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.state import StateLock

    work = _make_temp_work("night-rec-lock-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "inbox.db"
    _write_initial_state(state_file)
    loser_calls: list[int] = []
    winner_calls: list[int] = []
    caught: dict[str, Any] = {}

    lock = StateLock(state_file)
    lock.__enter__()

    def run_in_thread():
        try:
            _normal_scan(state_file, inbox_db, work, fetch_calls=loser_calls)
            caught["code"] = None
        except NgaAccessError as exc:
            caught["code"] = exc.code

    thread = threading.Thread(target=run_in_thread)
    thread.start()
    thread.join(timeout=30)
    lock.release()
    second = _normal_scan(state_file, inbox_db, work, fetch_calls=winner_calls)
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    checks = [
        _check("second_scan_locked", caught.get("code") == "watcher_locked", caught),
        _check("loser_no_network", loser_calls == [], loser_calls),
        _check("after_release_success", second.mode == "scan"
               and len(second.imported_items) == 2, len(second.imported_items)),
    ]
    return {
        "point": "two-scans-lock",
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def check_cancel_after_diff_before_first_import() -> dict[str, Any]:
    """最后一页返回后取消：diff 结束/首次 import 前可见，无任何导入."""
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.nga_access.fetch import FetchedThreadPage
    from umalib_ingest.nga_access.parsing import FloorSnapshot
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import WatcherConfig

    work = _make_temp_work("night-rec-afterdiff-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "inbox.db"
    state_before = _write_initial_state(state_file)
    flag = {"value": False}
    calls: list[int] = []

    def floor(pid, content):
        return FloorSnapshot(
            pid=pid, floor_number=None, author_present=True,
            posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
            content_format="ubbcode", content_text=content, content_raw_fragment=content,
        )

    good = FetchedThreadPage(
        tid=TID, page=173, last_page=173, status=200,
        final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page=173",
        content_type="text/html; charset=utf-8", encoding="utf-8",
        body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
        floors=(floor("910009001", CONTENT),), diagnostics=(),
    )

    class FakeSession:
        def close(self):
            pass

    def fetch_fn(session, tid, page, snapshot_dir):
        calls.append(page)
        if page == 173:
            flag["value"] = True
        if page == 1:
            return FetchedThreadPage(
                tid=TID, page=1, last_page=173, status=200,
                final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page=1",
                content_type="text/html; charset=utf-8", encoding="utf-8",
                body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
                floors=(), diagnostics=(),
            )
        return good

    config = WatcherConfig(
        state_file=state_file, profile_dir=work / "profile",
        snapshot_dir=work / "snaps", inbox_db=inbox_db,
        navigation_delay_seconds=3.0, max_scan_pages=12,
    )
    caught = None
    try:
        run_scan(config, dry_run=False, session_factory=FakeSession,
                 fetch_fn=fetch_fn, sleep=lambda s: None,
                 should_cancel=lambda: flag["value"])
    except NgaAccessError as exc:
        caught = exc.code
    inbox_items = _inbox_summary(inbox_db)
    state_unchanged = state_file.read_bytes() == state_before
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    checks = [
        _check("cancelled", caught == "watcher_shutdown", caught),
        _check("no_import", inbox_items == [], inbox_items),
        _check("state_unchanged", state_unchanged, None),
    ]
    return {
        "point": "cancel-after-diff",
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def _kill_matrix(mode: str) -> dict[str, Any]:
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.state import load_state

    work = _make_temp_work(f"night-rec-kill-{mode}-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "inbox.db"
    state_before = _write_initial_state(state_file)
    worker = PROJECT_ROOT / "tools" / "night_recovery_worker.py"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    marker = work / "ready.marker"
    proc = subprocess.Popen(
        [sys.executable, "-B", str(worker), "--mode", mode,
         "--state-file", str(state_file), "--inbox-db", str(inbox_db),
         "--work-dir", str(work)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not marker.exists():
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    marker_seen = marker.exists()
    killed = False
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=15)
        killed = True

    lock_path = Path(str(state_file) + ".lock")
    lock_residue = lock_path.exists()

    pre_inbox = _inbox_summary(inbox_db) if inbox_db.exists() else []
    state_unchanged = state_file.read_bytes() == state_before

    restart_blocked = None
    if lock_residue:
        try:
            _normal_scan(state_file, inbox_db, work)
        except NgaAccessError as exc:
            restart_blocked = exc.code
        lock_path.unlink()

    restart = _normal_scan(state_file, inbox_db, work)
    post_inbox = _inbox_summary(inbox_db)
    state = load_state(state_file)
    bound = {
        pid: floor.inbox_item_id for pid, floor in state.known_floors.items()
    }

    expected_pre_inbox = {"point9": 1, "point10": 2}.get(mode, 0)
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    checks = [
        _check("marker_seen", marker_seen, None),
        _check("child_killed", killed, None),
        _check("state_unchanged_after_kill", state_unchanged, None),
        _check("pre_restart_inbox",
               len(pre_inbox) == expected_pre_inbox,
               {"expected": expected_pre_inbox, "got": len(pre_inbox)}),
        _check("lock_residue", lock_residue, None),
        _check("restart_blocked_by_lock",
               restart_blocked == "watcher_locked" if lock_residue else True,
               restart_blocked),
        _check("restart_success",
               len(restart.imported_items) == 2 and len(post_inbox) == 2,
               {"imported": len(restart.imported_items), "inbox": len(post_inbox)}),
        _check("state_bound_to_items",
               all(v is not None for v in bound.values()), bound),
    ]
    if mode in ("point9", "point10"):
        pre_ids = sorted(i["item_id"] for i in pre_inbox)
        post_ids = sorted(i["item_id"] for i in post_inbox)
        checks.append(_check("item_ids_reused", set(pre_ids).issubset(post_ids)
                             and len(pre_ids) == expected_pre_inbox,
                             {"pre": pre_ids, "post": post_ids}))
    return {
        "point": f"kill-{mode}",
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def check_client_disconnect_server_survives() -> dict[str, Any]:
    """请求客户端中途断开：服务器继续服务，随后可正常关闭."""
    import http.client
    import socket

    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.web import create_server

    work = _make_temp_work("night-rec-disconnect-")
    inbox_db = work / "inbox.db"
    repo = InboxRepository(inbox_db)
    repo.initialize()
    server = create_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.sendall(
            b"POST /api/watcher/scan HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100000\r\n\r\n"
            b'{"dry_run": tru'
        )
        sock.close()
        time.sleep(0.5)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/api/watcher/config")
        res = conn.getresponse()
        res.read()
        conn.close()
        alive = res.status in (200, 409)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    checks = [
        _check("server_alive_after_disconnect", alive, None),
        _check("thread_ended", not thread.is_alive(), None),
    ]
    return {
        "point": "client-disconnect",
        "checks": checks,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }


def run_recovery(output: Path) -> dict[str, Any]:
    phase_root = output / "recovery"
    if phase_root.exists() and any(phase_root.iterdir()):
        raise LabError(f"recovery 证据目录已存在且非空（不覆盖）: {phase_root}")
    phase_root.mkdir(parents=True, exist_ok=True)

    records = [
        check_close_failure_not_masked(),
        check_schema_mismatch_blocks_before_network(),
        check_two_scans_lock_only_one_proceeds(),
        check_cancel_after_diff_before_first_import(),
        _kill_matrix("point2"),
        _kill_matrix("point3"),
        _kill_matrix("point9"),
        _kill_matrix("point10"),
        check_client_disconnect_server_survives(),
    ]
    for record in records:
        (phase_root / f"{record['point']}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    mapping = {
        "1-session-creation": "corpus S42 + schema-mismatch check",
        "2-after-first-page": "kill-point2",
        "3-login-waiting": "kill-point3",
        "4-login-confirm-vs-cancel": "acceptance test_confirm_then_shutdown_aborts_without_success",
        "5-during-navigation-sleep": "corpus S41",
        "6-next-page-failure": "corpus S43",
        "7-after-diff": "cancel-after-diff",
        "8-before-first-import": "cancel-after-diff",
        "9-after-first-import": "kill-point9",
        "10-before-state-write": "kill-point10（两稿全部 import 后阻塞）",
        "11-atomic-replace-failure": "corpus S44",
        "12-client-disconnect-shutdown": "client-disconnect",
    }
    overall = "PASS" if all(r["verdict"] == "PASS" for r in records) else "FAIL"
    summary = {"phase": "recovery", "overall": overall, "records": records, "point_mapping": mapping}
    (phase_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary
