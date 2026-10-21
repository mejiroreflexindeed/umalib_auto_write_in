"""TASK-012B R1/R2：参考状态机、种子序列对比与有界生命周期/规模测量。

R1：独立最小参考模型（只按 docs/11 契约书写，不从实现复制状态判断）生成
固定种子的事件序列，与真实 Watcher→Inbox 链逐步比较；保存种子与最小失败序列。
R2：有界生命周期（≤100 次本地 server 启停）与 25 页假数据规模测量。
全部离线：fake fetch/session，临时目录自动清理。
"""

from __future__ import annotations

import json
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

TID = 30099686
T0 = "2026-09-02T00:00:00.000000+00:00"

COMPLETE = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n正文甲。"
COMPLETE2 = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n正文甲改。"
INCOMPLETE = "[收录]\n[标题] 缺作者\n[标签] 标签甲\n\n正文。"
DISCUSSION = "普通讨论内容"

KINDS = ("discussion", "complete", "incomplete", "edit_complete", "remove_trigger")

SEED = 20260902
MAX_SEQUENCES = 500


def _tmp(prefix: str) -> Path:
    from umalib_ingest.watcher.state import PRIVATE_STATE_ROOT

    return Path(tempfile.mkdtemp(dir=str(PRIVATE_STATE_ROOT), prefix=prefix))


# --------------------------------------------------------------------------- #
# R1 参考模型（独立书写，只依据契约文档）
# --------------------------------------------------------------------------- #

def reference_step(state: dict[str, dict], floors: list[dict]) -> dict[str, Any]:
    """Independent reference model for one scan step.

    state: pid -> {imported(bool), sha, status}
    floors: latest floor list (pid, content)
    Returns expected counts/inbox additions/changed list for THIS step.
    """
    counts = {"new": 0, "unchanged": 0, "changed": 0, "discussion": 0,
              "triggered": 0, "imported": 0}
    inbox_add: list[str] = []
    changed_after_import: list[str] = []
    by_pid = {f["pid"]: f for f in floors}
    for pid, floor in by_pid.items():
        content = floor["content"]
        lines = content.splitlines()
        has_trigger = bool(lines) and lines[0].strip() == "[收录]"
        status = (
            "incomplete" if content == INCOMPLETE
            else "candidate" if has_trigger else None
        )
        prev = state.get(pid)
        if prev is None:
            counts["new"] += 1
            if has_trigger:
                counts["triggered"] += 1
                counts["imported"] += 1
                inbox_add.append(pid)
                state[pid] = {"imported": True, "sha": content, "status": status}
            else:
                counts["discussion"] += 1
                state[pid] = {"imported": False, "sha": content, "status": None}
            continue
        if prev["sha"] == content:
            counts["unchanged"] += 1
            continue
        counts["changed"] += 1
        if prev["imported"]:
            changed_after_import.append(pid)
            state[pid] = {"imported": True, "sha": content, "status": prev["status"]}
            continue
        if has_trigger:
            counts["triggered"] += 1
            counts["imported"] += 1
            inbox_add.append(pid)
            state[pid] = {"imported": True, "sha": content, "status": status}
        else:
            counts["discussion"] += 1
            state[pid] = {"imported": False, "sha": content, "status": None}
    return {"counts": counts, "inbox_add": inbox_add,
            "changed_after_import": changed_after_import}


def generate_sequences(seed: int = SEED, count: int = MAX_SEQUENCES) -> list[list[tuple[int, str]]]:
    """Deterministic sequences: (pid, kind) events; pid reused means the floor evolves."""
    rng = random.Random(seed)
    sequences: list[list[tuple[int, str]]] = []
    for _ in range(count):
        length = rng.randint(1, 6)
        used_pids: list[int] = []
        events: list[tuple[int, str]] = []
        for _ in range(length):
            if used_pids and rng.random() < 0.5:
                pid = rng.choice(used_pids)
            else:
                pid = rng.randint(910010000, 910010999)
                used_pids.append(pid)
            kind = rng.choice(KINDS)
            events.append((pid, kind))
        sequences.append(events)
    return sequences


def content_for_kind(kind: str, previous: str | None = None) -> str:
    if kind == "discussion":
        return DISCUSSION
    if kind == "complete":
        return COMPLETE
    if kind == "incomplete":
        return INCOMPLETE
    if kind == "edit_complete":
        return COMPLETE2 if previous == COMPLETE else COMPLETE
    if kind == "remove_trigger":
        return DISCUSSION
    raise ValueError(kind)


def run_reference_sequences(output: Path) -> dict[str, Any]:
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.nga_access.fetch import FetchedThreadPage
    from umalib_ingest.nga_access.parsing import FloorSnapshot
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import (
        WatcherConfig,
        WatcherState,
        atomic_write_json,
        load_state,
    )

    sequences = generate_sequences()
    failures: list[dict[str, Any]] = []
    out_of_scope: list[str] = []
    mismatches = 0

    def floor(pid: int, content: str) -> FloorSnapshot:
        return FloorSnapshot(
            pid=str(pid), floor_number=None, author_present=True,
            posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
            content_format="ubbcode", content_text=content, content_raw_fragment=content,
        )

    def page(floors: list[tuple[int, str]]) -> FetchedThreadPage:
        return FetchedThreadPage(
            tid=TID, page=173, last_page=173, status=200,
            final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page=173",
            content_type="text/html; charset=utf-8", encoding="utf-8",
            body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
            floors=tuple(floor(pid, content) for pid, content in floors),
            diagnostics=(),
        )

    class FakeSession:
        def close(self):
            pass

    for index, sequence in enumerate(sequences):
        work = _tmp(f"night-r1-{index}-")
        try:
            state_file = work / "watcher-state.json"
            inbox_db = work / "inbox.db"
            initial = WatcherState(
                schema_version="1", tid=TID,
                canonical_thread_url=f"https://bbs.nga.cn/read.php?tid={TID}",
                last_page=173, known_floors={}, baseline_created_at=T0,
                last_successful_scan_at=None, last_result_summary=None,
            )
            atomic_write_json(state_file, initial.to_dict())
            config = WatcherConfig(
                state_file=state_file, profile_dir=work / "profile",
                snapshot_dir=work / "snaps", inbox_db=inbox_db,
                navigation_delay_seconds=3.0, max_scan_pages=12,
            )
            ref_state: dict[str, dict] = {}
            latest: dict[int, str] = {}
            mismatch = False
            for step_index, (pid, kind) in enumerate(sequence):
                previous = latest.get(pid)
                latest[pid] = content_for_kind(kind, previous)
                step_floors = [{"pid": str(p), "content": c}
                               for p, c in sorted(latest.items())]
                expected = reference_step(ref_state, step_floors)

                current = page([(int(f["pid"]), f["content"]) for f in step_floors])
                result = run_scan(
                    config, dry_run=False, session_factory=FakeSession,
                    fetch_fn=lambda s, t, p, d, _c=current: _c,
                    sleep=lambda s: None, clock=lambda: T0,
                )
                got = {
                    "new": result.new_floor_count,
                    "unchanged": result.unchanged_floor_count,
                    "changed": result.changed_floor_count,
                    "discussion": result.discussion_count,
                    "triggered": result.triggered_count,
                    "imported": len(result.imported_items),
                }
                got_changed = list(result.changed_after_import)
                expected_changed = expected["changed_after_import"]
                if got != expected["counts"] or len(got_changed) != len(expected_changed):
                    mismatch = True
                    mismatches += 1
                    failures.append({
                        "sequence": [(p, k) for p, k in sequence],
                        "step": step_index,
                        "expected_counts": expected["counts"],
                        "got_counts": got,
                        "expected_changed": expected_changed,
                        "got_changed": got_changed,
                    })
                    break
            if not mismatch:
                repo = InboxRepository(inbox_db)
                repo.initialize()
                inbox_items = [
                    {"pid": (it.original_draft.get("origin") or {}).get("post_id"),
                     "status": it.status}
                    for it in (repo.get(s.id) for s in repo.list_items()) if it is not None
                ]
                expected_inbox = [
                    {"pid": str(pid), "status": ref_state[str(pid)]["status"]}
                    for pid in sorted(latest)
                    if ref_state[str(pid)].get("imported")
                ]
                if sorted((i["pid"], i["status"]) for i in inbox_items) != \
                        sorted((i["pid"], i["status"]) for i in expected_inbox):
                    mismatches += 1
                    failures.append({
                        "sequence": [(p, k) for p, k in sequence],
                        "step": "final-inbox",
                        "expected": expected_inbox,
                        "got": inbox_items,
                    })
        except NgaAccessError as exc:
            out_of_scope.append({"sequence": [(p, k) for p, k in sequence], "error": exc.code})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    verdict = "PASS" if mismatches == 0 and not out_of_scope else "FAIL"
    summary = {
        "phase": "r1-reference-model",
        "seed": SEED,
        "generated": len(sequences),
        "mismatches": mismatches,
        "minimal_failures": failures[:10],
        "out_of_scope": out_of_scope[:10],
        "out_of_scope_count": len(out_of_scope),
        "overall": verdict,
    }
    (output / "r1-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


# --------------------------------------------------------------------------- #
# R2 有界生命周期与规模测量
# --------------------------------------------------------------------------- #

def run_lifecycle(output: Path, cycles: int = 100) -> dict[str, Any]:
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.web import create_server

    work = _tmp("night-r2-lifecycle-")
    inbox_db = work / "inbox.db"
    InboxRepository(inbox_db).initialize()
    baseline_threads = threading.active_count()
    residues = 0
    failures: list[dict[str, Any]] = []
    durations: list[float] = []
    for cycle in range(cycles):
        try:
            started = time.monotonic()
            server = create_server(InboxRepository(inbox_db), port=0)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            probe = socket.create_connection(("127.0.0.1", port), timeout=5)
            probe.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
            durations.append(time.monotonic() - started)
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                sock.close()
                residues += 1
                failures.append({"cycle": cycle, "issue": "port still open"})
            except OSError:
                pass
            if thread.is_alive():
                residues += 1
                failures.append({"cycle": cycle, "issue": "thread still alive"})
        except Exception as exc:
            residues += 1
            failures.append({"cycle": cycle, "issue": f"{type(exc).__name__}: {exc}"})
    threads_after = threading.active_count()
    shutil.rmtree(work, ignore_errors=True)
    summary = {
        "phase": "r2-lifecycle",
        "cycles": cycles,
        "residues": residues,
        "failures": failures[:10],
        "threads_baseline": baseline_threads,
        "threads_after": threads_after,
        "avg_cycle_ms": round(sum(durations) / max(len(durations), 1) * 1000, 2),
        "overall": "PASS" if residues == 0 and threads_after <= baseline_threads + 1 else "FAIL",
    }
    (output / "r2-lifecycle-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def run_scale25(output: Path) -> dict[str, Any]:
    from umalib_ingest.nga_access.fetch import FetchedThreadPage
    from umalib_ingest.nga_access.parsing import FloorSnapshot
    from umalib_ingest.watcher.scan import run_scan
    from umalib_ingest.watcher.state import (
        WatcherConfig,
        WatcherState,
        atomic_write_json,
    )

    work = _tmp("night-r2-scale25-")
    state_file = work / "watcher-state.json"
    inbox_db = work / "inbox.db"
    initial = WatcherState(
        schema_version="1", tid=TID,
        canonical_thread_url=f"https://bbs.nga.cn/read.php?tid={TID}",
        last_page=1, known_floors={}, baseline_created_at=T0,
        last_successful_scan_at=None, last_result_summary=None,
    )
    atomic_write_json(state_file, initial.to_dict())
    config = WatcherConfig(
        state_file=state_file, profile_dir=work / "profile",
        snapshot_dir=work / "snaps", inbox_db=inbox_db,
        navigation_delay_seconds=3.0, max_scan_pages=25,
    )
    pages: dict[int, FetchedThreadPage] = {}
    for page_number in range(1, 26):
        floors = ()
        if page_number == 25:
            floors = tuple(
                FloorSnapshot(
                    pid=f"910025{index:03d}", floor_number=None, author_present=True,
                    posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
                    content_format="ubbcode", content_text=DISCUSSION,
                    content_raw_fragment=DISCUSSION,
                )
                for index in range(10)
            )
        pages[page_number] = FetchedThreadPage(
            tid=TID, page=page_number, last_page=25, status=200,
            final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page={page_number}",
            content_type="text/html; charset=utf-8", encoding="utf-8",
            body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
            floors=floors, diagnostics=(),
        )

    class FakeSession:
        def close(self):
            pass

    sleep_calls: list[float] = []
    started = time.monotonic()
    result = run_scan(
        config, dry_run=True, session_factory=FakeSession,
        fetch_fn=lambda s, t, p, d: pages[p],
        sleep=sleep_calls.append, clock=lambda: T0,
    )
    duration = time.monotonic() - started
    shutil.rmtree(work, ignore_errors=True)
    summary = {
        "phase": "r2-scale25",
        "pages": 25,
        "duration_seconds": round(duration, 3),
        "sleep_calls": len(sleep_calls),
        "scanned_pages": list(result.scanned_pages),
        "new_floor_count": result.new_floor_count,
        "discussion_count": result.discussion_count,
        "overall": "PASS" if len(result.scanned_pages) == 25
                   and len(sleep_calls) == 24 else "FAIL",
    }
    (output / "r2-scale25-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else _tmp("night-r-run-")
    if out.exists() and any(out.iterdir()):
        raise SystemExit("证据目录非空，不覆盖")
    out.mkdir(parents=True, exist_ok=True)
    r1 = run_reference_sequences(out)
    r2a = run_lifecycle(out)
    r2b = run_scale25(out)
    print("r1:", r1["overall"], r1["mismatches"], "mismatches /", r1["generated"], "sequences")
    print("r2-lifecycle:", r2a["overall"], "residues=", r2a["residues"])
    print("r2-scale25:", r2b["overall"], r2b["duration_seconds"], "s")
    raise SystemExit(0 if all(s["overall"] == "PASS" for s in (r1, r2a, r2b)) else 1)
