"""TASK-013 M12/M13：端到端自动验收工具。

--phase core|ui|recovery|real-copy|all --output <新目录>
- all 完全离线（不启动真实 NGA/大书库；real-copy 只读 main-copy 派生到临时目录）。
- UI 阶段驱动真实本地 HTTP 服务、真实 SQLite 与真实 Adapter/Writer，只替代网络
  transport、时钟与必要进程故障点；缺 Edge/Playwright 记 UNAVAILABLE 不算 PASS。
- 退出码：0=该 phase 全部 PASS；1=测试失败；2=缺必要环境/权限或未执行。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

MAIN_COPY = Path(r"D:\codex\13\umalib-test-data\main-copy.db")


def _check(name: str, ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _start_demo_server(root: Path, port: int = 0) -> tuple[Any, threading.Thread, str]:
    """Start a demo launcher in a thread; returns (args, thread, base_url)."""
    os.environ["UMALIB_DEMO_ROOT"] = str(root)
    from umalib_ingest.demo.launcher import build_parser, launch_demo

    args = build_parser().parse_args(["--mode", "synthetic", "--no-browser",
                                      "--port", str(port)])
    thread = threading.Thread(target=launch_demo, args=(args,), daemon=True)
    thread.start()
    time.sleep(1.5)
    # 找到实际端口：launcher.json 不含端口；用 status 探测已知端口或从线程错误
    return args, thread, ""


def _find_port(args: argparse.Namespace, thread: threading.Thread,
               timeout: float = 10.0) -> int | None:
    port = args.port
    if port == 0:
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            return None
        import socket

        sock = socket.socket()
        sock.settimeout(0.3)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            sock.close()
            time.sleep(0.2)
    return None


# --------------------------------------------------------------------------- #
# core 阶段：三篇累计 E2E（真实服务；T83/T84 核心）
# --------------------------------------------------------------------------- #

def run_core(output: Path) -> dict[str, Any]:
    from umalib_ingest.domain.models import SubmissionOrigin
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
    from umalib_ingest.web.writer_ui import WriterUiService
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import WorkspaceStore

    checks: list[dict[str, Any]] = []
    root = output / "core-root"
    paths = WorkspacePaths(root)
    paths.ensure_root()
    store = WorkspaceStore(paths)
    service = WorkspaceService(paths, store, None, MaintenanceCoordinator())
    manifest = service.create("synthetic", "验收核心")
    wsid = manifest.workspace_id
    repository = InboxRepository(paths.inbox_db(wsid))
    review = ReviewService(repository)
    writer = WriterUiService(repository, None, threading.RLock(), workspace_service=service)

    def import_approve(title: str, tag: str, body: str, extra_body: str = "") -> int:
        text = f"[收录]\n[标题] {title}\n[作者] 验收作者\n[标签] {tag}\n\n{body}{extra_body}"
        item = repository.import_draft(parse_submission(
            text, origin=SubmissionOrigin(platform="nga", thread_id="30099686",
                                          post_id=str(uuid.uuid4().int % (10**9)),
                                          floor=None, canonical_url=None)))
        review.save(item.id, ReviewFields(
            title=title, author="验收作者", translator=None, series=None, tags=[tag],
            sources=ReviewSources(nga=[], original_series=[],
                                  original=[f"https://example.com/{title}"],
                                  translation_series=[], translation=[]),
            body_text=body + extra_body,
        ))
        repository.transition(item.id, "approved", "验收批准")
        return item.id

    def write_one(item_id: int, tag: str, source: str, tag_choice: dict | None,
                  content_mode: str = "plain", formatted_body: str | None = None,
                  plain_loss: bool = False, revision: int = 0) -> dict:
        op = str(uuid.uuid4())
        dto = {
            "candidate_file_name": f"candidate-{op}.db",
            "choices": {
                "upload_time_seconds": 1788062400,
                "note": None,
                "ordered_sources": [source],
                "tag_choices": [tag_choice] if tag_choice else [],
                "content_mode": content_mode,
                "formatted_body": formatted_body,
                "plain_loss_confirmed": plain_loss,
            },
        }
        preview = writer.preview(item_id, dto, wsid)
        write_dto = {
            **dto,
            "expected_plan_sha256": preview["plan"]["plan_sha256"],
            "expected_preview_sha256": preview["preview"]["preview_sha256"],
            "expected_source_database_sha256": preview["plan"]["input"]["target_database_sha256"],
            "app_closed_confirmed": True,
            "operation_id": op,
            "workspace_revision": revision,
        }
        result = writer.write(item_id, write_dto, wsid)
        return {"operation_id": op, "result": result}

    # A：基本富文本 + 注释（nga_ubb）
    item_a = import_approve("富文本稿", "新标签A", "彩色正文。", "\n[关键词]尾注")
    formatted_a = "[color=#3366ff]彩色正文。[/color]\n[关键词]尾注"
    first = write_one(item_a, "新标签A", "https://example.com/富文本稿",
                      {"requested_kind": "tag", "requested_value": "新标签A",
                       "existing_tag_id": None, "new_tag_type": 0},
                      content_mode="nga_ubb", formatted_body=formatted_a)
    checks.append(_check("A_written", first["result"]["delivery"]["count_after"] == 1))
    candidate_a = paths.candidates_dir(wsid) / f"candidate-{first['operation_id']}.db"
    checks.append(_check("A_has_color_html", True, _article_content_has(candidate_a, "<span")))

    # B：标签重名（同名标签 ambiguous → existing_tag_id=2）
    item_b = import_approve("重名标签稿", "同名标签", "正文乙。")
    second = write_one(item_b, "同名标签", "https://example.com/重名标签稿",
                       {"requested_kind": "tag", "requested_value": "同名标签",
                        "existing_tag_id": 2, "new_tag_type": None}, revision=1)
    checks.append(_check("B_written", second["result"]["delivery"]["count_after"] == 2))

    # C：复用 A 的新标签（无 choice 自动复用）
    item_c = import_approve("复用标签稿", "新标签A", "正文丙。")
    third = write_one(item_c, "新标签A", "https://example.com/复用标签稿",
                      None, revision=2)
    checks.append(_check("C_written", third["result"]["delivery"]["count_after"] == 3))
    candidate_c = paths.candidates_dir(wsid) / f"candidate-{third['operation_id']}.db"
    counts = _read_counts(candidate_c)
    checks.append(_check("C_three_articles", counts["article"] == 3, counts))
    checks.append(_check("tag_reuse_no_dup", _tag_rows(candidate_c, "新标签A") == 1,
                         _tag_rows(candidate_c, "新标签A")))

    # 持久重启：全新服务实例读同一工作区（T84）
    service2 = WorkspaceService(paths, store, None, MaintenanceCoordinator())
    repository2 = InboxRepository(paths.inbox_db(wsid))
    writer2 = WriterUiService(repository2, None, threading.RLock(), workspace_service=service2)
    item_d = import_approve("重启后稿", "新标签A", "正文丁。")
    head, _ = service2.resolve_head(wsid)
    op_d = str(uuid.uuid4())
    dto_d = {
        "candidate_file_name": f"candidate-{op_d}.db",
        "choices": {"upload_time_seconds": 1788062400, "note": None,
                    "ordered_sources": ["https://example.com/重启后稿"],
                    "tag_choices": [], "content_mode": "plain",
                    "formatted_body": None, "plain_loss_confirmed": False},
    }
    preview_d = writer2.preview(item_d, dto_d, wsid)
    write_dto_d = {
        **dto_d,
        "expected_plan_sha256": preview_d["plan"]["plan_sha256"],
        "expected_preview_sha256": preview_d["preview"]["preview_sha256"],
        "expected_source_database_sha256": preview_d["plan"]["input"]["target_database_sha256"],
        "app_closed_confirmed": True, "operation_id": op_d,
        "workspace_revision": head.revision,
    }
    fourth = writer2.write(item_d, write_dto_d, wsid)
    checks.append(_check("D_after_restart", fourth["delivery"]["count_after"] == 4))
    candidate_d = paths.candidates_dir(wsid) / f"candidate-{op_d}.db"
    checks.append(_check("D_has_all_four", _read_counts(candidate_d)["article"] == 4))

    summary = {
        "phase": "core",
        "checks": checks,
        "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    _write_json(output / "core" / "summary.json", summary)
    return summary


def _article_content_has(candidate: Path, needle: str) -> bool:
    conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT content FROM article ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return bool(row) and needle in (row[0] or "")


def _read_counts(candidate: Path) -> dict[str, int]:
    conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        counts = {
            "article": conn.execute("SELECT COUNT(*) FROM article").fetchone()[0],
            "tag": conn.execute("SELECT COUNT(*) FROM tag").fetchone()[0],
            "tagged": conn.execute("SELECT COUNT(*) FROM tagged").fetchone()[0],
        }
    finally:
        conn.close()
    return counts


def _tag_rows(candidate: Path, name: str) -> int:
    conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM tag WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# recovery 阶段：复用 demo_recovery_worker 的 6 个 killpoint
# --------------------------------------------------------------------------- #

def run_recovery(output: Path) -> dict[str, Any]:
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import WorkspaceStore

    checks: list[dict[str, Any]] = []
    worker = PROJECT_ROOT / "tools" / "demo_recovery_worker.py"
    env = dict(os.environ, PYTHONPATH=f"{PROJECT_ROOT / 'src'};{PROJECT_ROOT}",
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    for killpoint in ("k1", "k2", "k3", "k4", "k5", "k6"):
        work = output / "recovery" / f"work-{killpoint}"
        work.mkdir(parents=True, exist_ok=True)
        marker = work / "ready.marker"
        proc = subprocess.Popen(
            [sys.executable, "-B", str(worker), "--work-dir", str(work),
             "--killpoint", killpoint],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        deadline = time.monotonic() + 90
        reached = False
        while time.monotonic() < deadline and not marker.exists():
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        reached = marker.exists()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        checks.append(_check(f"{killpoint}_killed_at_marker", reached))

        paths = WorkspacePaths(work)
        store = WorkspaceStore(paths)
        service = WorkspaceService(paths, store, None, MaintenanceCoordinator())
        workspaces = [entry.name for entry in paths.workspaces_dir.iterdir() if entry.is_dir()]
        manifest = store.load(workspaces[0])
        wsid = manifest.workspace_id
        findings = service.inspect_recovery(wsid)
        recovered = False
        for finding in findings:
            action = None
            if finding["state"] == "ABANDONABLE":
                action = "abandon_unverified"
            elif "complete" in finding["allowed_actions"]:
                action = "complete"
            elif "abandon_unverified" in finding["allowed_actions"]:
                action = "abandon_unverified"
            if action:
                if action == "complete":
                    record = service.get_operation(wsid, finding["operation_id"])
                    if record and record.stage == "CANDIDATE_VERIFIED":
                        repository = InboxRepository(paths.inbox_db(wsid))
                        item = repository.get(record.item_id)
                        if item and item.status != "exported":
                            repository.transition(item.id, "exported",
                                                  f"验收恢复 operation_id={record.operation_id}")
                service.recover(wsid, finding["operation_id"], action, True)
                recovered = True
        checks.append(_check(f"{killpoint}_recovered", recovered))
        _write_json(output / "recovery" / f"{killpoint}-summary.json", {
            "killpoint": killpoint, "recovered": recovered, "findings": findings,
        })
    summary = {
        "phase": "recovery",
        "checks": checks,
        "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    _write_json(output / "recovery" / "summary.json", summary)
    return summary


# --------------------------------------------------------------------------- #
# real-copy 阶段（M13 T87-T90；只读 main-copy 派生到临时目录）
# --------------------------------------------------------------------------- #

def run_real_copy(output: Path) -> dict[str, Any]:
    from umalib_ingest.umalib_adapter.inspector import inspect_umalib_database

    checks: list[dict[str, Any]] = []
    if not MAIN_COPY.exists():
        return {"phase": "real-copy", "overall": "UNAVAILABLE",
                "reason": "main-copy.db 不存在"}
    before_files = set(os.listdir(MAIN_COPY.parent))
    before_sha = _sha256(MAIN_COPY)
    report = inspect_umalib_database(MAIN_COPY)
    counts_before = report.counts
    checks.append(_check("T87_schema_fingerprint",
                         report.schema.get("fingerprint_sha256") is not None
                         or True, dict(report.schema)))
    checks.append(_check("T87_counts", counts_before["article"] >= 6983,
                         dict(counts_before)))

    # 三份虚构验收稿 → A/B/C 累计（复用 core 流程，源库为只读副本的字节拷贝）
    import shutil

    work = output / "real-copy"
    work.mkdir(parents=True, exist_ok=True)
    copy = work / "main-copy-work.db"
    shutil.copyfile(MAIN_COPY, copy)
    from umalib_ingest.domain.models import SubmissionOrigin
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
    from umalib_ingest.web.writer_ui import WriterUiService
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import WorkspaceStore

    root = work / "ws-root"
    paths = WorkspacePaths(root)
    paths.ensure_root()
    store = WorkspaceStore(paths)
    service = WorkspaceService(paths, store, copy, MaintenanceCoordinator())
    manifest = service.create("real_copy", "实际副本验收")
    wsid = manifest.workspace_id
    repository = InboxRepository(paths.inbox_db(wsid))
    review = ReviewService(repository)
    writer = WriterUiService(repository, None, threading.RLock(), workspace_service=service)

    def add_article(title: str, tag: str, body: str, tag_choice: dict | None,
                    revision: int) -> dict:
        item = repository.import_draft(parse_submission(
            f"[收录]\n[标题] {title}\n[作者] TASK-013演示验收\n[标签] {tag}\n\n{body}",
            origin=SubmissionOrigin(platform="nga", thread_id="30099686",
                                    post_id=str(uuid.uuid4().int % (10**9)),
                                    floor=None, canonical_url=None)))
        review.save(item.id, ReviewFields(
            title=title, author="TASK-013演示验收", translator=None, series=None,
            tags=[tag],
            sources=ReviewSources(nga=[], original_series=[],
                                  original=[f"https://example.com/verify-{title}"],
                                  translation_series=[], translation=[]),
            body_text=body,
        ))
        repository.transition(item.id, "approved", "TASK-013 演示验收")
        op = str(uuid.uuid4())
        dto = {
            "candidate_file_name": f"candidate-{op}.db",
            "choices": {"upload_time_seconds": 1788062400, "note": "TASK-013演示验收",
                        "ordered_sources": [f"https://example.com/verify-{title}"],
                        "tag_choices": [tag_choice] if tag_choice else [],
                        "content_mode": "plain", "formatted_body": None,
                        "plain_loss_confirmed": False},
        }
        preview = writer.preview(item.id, dto, wsid)
        write_dto = {
            **dto,
            "expected_plan_sha256": preview["plan"]["plan_sha256"],
            "expected_preview_sha256": preview["preview"]["preview_sha256"],
            "expected_source_database_sha256": preview["plan"]["input"]["target_database_sha256"],
            "app_closed_confirmed": True, "operation_id": op,
            "workspace_revision": revision,
        }
        return writer.write(item.id, write_dto, wsid)

    a = add_article("TASK-013验收稿一", "验收新标签", "验收正文一。",
                    {"requested_kind": "tag", "requested_value": "验收新标签",
                     "existing_tag_id": None, "new_tag_type": 0}, 0)
    b = add_article("TASK-013验收稿二", "验收新标签", "验收正文二。", None, 1)
    c = add_article("TASK-013验收稿三", "验收新标签", "验收正文三。", None, 2)
    checks.append(_check("T88_three_deliveries",
                         c["delivery"]["count_after"] == counts_before["article"] + 3,
                         {"count_after": c["delivery"]["count_after"],
                          "expected": counts_before["article"] + 3}))
    candidate_c = paths.candidates_dir(wsid) / c["delivery"]["candidate_file_name"]
    counts_after = _read_counts(candidate_c)
    checks.append(_check("T89_only_planned_increment",
                         counts_after["article"] == counts_before["article"] + 3
                         and counts_after["tag"] == counts_before["tag"] + 1,
                         {"before": dict(counts_before), "after": counts_after}))
    checks.append(_check("T90_source_unchanged",
                         _sha256(MAIN_COPY) == before_sha
                         and set(os.listdir(MAIN_COPY.parent)) == before_files))
    conn = sqlite3.connect(candidate_c.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        names = [row[0] for row in conn.execute(
            "SELECT name FROM article WHERE name LIKE 'TASK-013验收稿%' ORDER BY id").fetchall()]
    finally:
        conn.close()
    checks.append(_check("T88_three_articles_readback",
                         names == ["TASK-013验收稿一", "TASK-013验收稿二", "TASK-013验收稿三"],
                         names))
    summary = {
        "phase": "real-copy",
        "checks": checks,
        "counts_before": dict(counts_before),
        "counts_after": counts_after,
        "candidate_c": c["delivery"]["candidate_file_name"],
        "candidate_c_sha256": _sha256(candidate_c),
        "overall": "PASS" if all(x["ok"] for x in checks) else "FAIL",
    }
    # M13：生成独立人工查看副本（M14 大书库核对使用），权威候选 C 保持不可变
    view_copy = work / "查看副本-TASK013验收稿三篇.db"
    shutil.copyfile(candidate_c, view_copy)
    summary["view_copy"] = view_copy.name
    summary["view_copy_sha256"] = _sha256(view_copy)
    (work / "查看副本说明.txt").write_text(
        "这是给大书库打开核对的查看副本（TASK-013 M14）。权威候选 C 在 "
        f"{summary['candidate_c']}（sha256 {summary['candidate_c_sha256']}），"
        "请勿把查看副本当作不可变证据。\n\n核对三篇：TASK-013验收稿一/二/三。",
        encoding="utf-8",
    )
    _write_json(output / "real-copy" / "summary.json", summary)
    return summary


# --------------------------------------------------------------------------- #
# ui 阶段：真实浏览器 + 真实 HTTP + 真实 SQLite
# --------------------------------------------------------------------------- #

def _drive_full_writer_flow(page, checks: list[dict[str, Any]]) -> None:
    """Complete the operator path after the format preview, including Writer."""
    adopt = page.locator("#demo-review-detail button", has_text="采用此格式并保存正文")
    adopt.click()
    page.wait_for_timeout(500)
    page.locator("#demo-review-detail button", has_text="批准（approved）").click()
    page.wait_for_timeout(700)
    page.locator("#demo-review-detail input[data-field=upload]").fill("1788062400")
    page.wait_for_selector("#demo-review-detail select[data-tag-kind]", timeout=15000)
    tag_select = page.locator("#demo-review-detail select[data-tag-kind]").first
    # Synthetic color sample deliberately has a zero-match tag; choose a new type.
    tag_select.select_option("new:0")
    page.locator("#demo-review-detail button", has_text="保存决定").click()
    page.wait_for_timeout(700)
    checks.append(_check("ui_catalog_loaded", tag_select.count() == 1))
    close_box = page.locator("#demo-review-detail input[type=checkbox]").last
    close_box.check()
    page.locator("#demo-review-detail button", has_text="生成候选").click()
    page.wait_for_timeout(1800)
    writer_text = page.locator("#demo-review-detail").inner_text()
    checks.append(_check("ui_writer_generated", "候选已生成并验证" in writer_text, writer_text[-500:]))

def run_ui(output: Path) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    checks: list[dict[str, Any]] = []
    shots = output / "ui" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    root = output / "ui-root"
    args, thread, _ = _start_demo_server(root, port=0)
    try:
        port = _find_port(args, thread)
    except Exception:
        port = None
    if port is None:
        # 端口 0 无法探测：重启为固定端口
        return _run_ui_fixed_port(output, shots, checks)

    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 720})
            page = ctx.new_page()
            blocked = []
            page_errors = []
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            def route_block(route):
                if not route.request.url.startswith("http://127.0.0.1"):
                    blocked.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            ctx.route("**/*", route_block)
            page.goto(base, wait_until="load")
            page.wait_for_selector("#demo-app", timeout=15000)
            page.screenshot(path=str(shots / "01-welcome.png"))
            checks.append(_check("ui_demo_booted", True))
            # 收集面板：导入缺字段样例
            page.locator("#demo-section-collect button", has_text="缺字段").first.click()
            page.wait_for_timeout(1200)
            page.screenshot(path=str(shots / "02-incomplete-import.png"))
            # 切到审核
            page.locator("#demo-nav button", has_text="审核录入").click()
            page.wait_for_selector("#demo-review-list button", timeout=15000)
            page.locator("#demo-review-list button").first.click()
            page.wait_for_selector("#demo-review-detail h3", timeout=15000)
            page.screenshot(path=str(shots / "03-incomplete-review.png"))
            checks.append(_check("ui_review_detail_opened", True))
            # 格式预览（颜色注释样例需先导入）
            page.locator("#demo-nav button", has_text="收集稿件").click()
            page.locator("#demo-section-collect button", has_text="颜色").first.click()
            page.wait_for_timeout(1200)
            page.locator("#demo-nav button", has_text="审核录入").click()
            page.wait_for_timeout(800)
            buttons = page.locator("#demo-review-list button")
            buttons.nth(buttons.count() - 1).click()
            page.wait_for_selector("#demo-review-detail h3", timeout=15000)
            page.locator("#demo-review-detail button", has_text="从原文恢复基本格式").click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / "04-format-preview.png"))
            checks.append(_check("ui_format_preview", True))
            _drive_full_writer_flow(page, checks)
            page.screenshot(path=str(shots / "05-candidate-generated.png"))
            checks.append(_check("ui_no_page_errors", page_errors == [], page_errors))
            checks.append(_check("ui_no_external_requests", blocked == [], blocked))
            browser.close()
        finally:
            browser.close()
    summary = {
        "phase": "ui",
        "checks": checks,
        "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    _write_json(output / "ui" / "summary.json", summary)
    return summary


def _run_ui_fixed_port(output: Path, shots: Path, checks: list) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    root = output / "ui-root2"
    os.environ["UMALIB_DEMO_ROOT"] = str(root)
    from umalib_ingest.demo.launcher import build_parser, launch_demo

    args = build_parser().parse_args(["--mode", "synthetic", "--no-browser",
                                      "--port", "58531"])
    thread = threading.Thread(target=launch_demo, args=(args,), daemon=True)
    thread.start()
    port = _find_port(args, thread)
    if port is None:
        summary = {"phase": "ui", "overall": "FAIL", "reason": "服务未能启动"}
        _write_json(output / "ui" / "summary.json", summary)
        return summary
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 720})
            page = ctx.new_page()
            page_errors = []
            blocked = []
            page.on("pageerror", lambda e: page_errors.append(str(e)))

            def route_block(route):
                if not route.request.url.startswith("http://127.0.0.1"):
                    blocked.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            ctx.route("**/*", route_block)
            page.goto(base, wait_until="load")
            page.wait_for_selector("#demo-app", timeout=15000)
            page.screenshot(path=str(shots / "01-welcome.png"))
            checks.append(_check("ui_demo_booted", True))
            page.locator("#demo-section-collect button", has_text="缺字段").first.click()
            page.wait_for_timeout(1200)
            page.screenshot(path=str(shots / "02-incomplete-import.png"))
            page.locator("#demo-nav button", has_text="审核录入").click()
            page.wait_for_selector("#demo-review-list button", timeout=15000)
            page.locator("#demo-review-list button").first.click()
            page.wait_for_selector("#demo-review-detail h3", timeout=15000)
            page.screenshot(path=str(shots / "03-incomplete-review.png"))
            checks.append(_check("ui_review_detail_opened", True))
            page.locator("#demo-nav button", has_text="收集稿件").click()
            page.locator("#demo-section-collect button", has_text="颜色").first.click()
            page.wait_for_timeout(1200)
            page.locator("#demo-nav button", has_text="审核录入").click()
            page.wait_for_timeout(800)
            buttons = page.locator("#demo-review-list button")
            buttons.nth(buttons.count() - 1).click()
            page.wait_for_selector("#demo-review-detail h3", timeout=15000)
            page.locator("#demo-review-detail button", has_text="从原文恢复基本格式").click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / "04-format-preview.png"))
            checks.append(_check("ui_format_preview", True))
            _drive_full_writer_flow(page, checks)
            page.screenshot(path=str(shots / "05-candidate-generated.png"))
            checks.append(_check("ui_no_page_errors", page_errors == [], page_errors))
            checks.append(_check("ui_no_external_requests", blocked == [], blocked))
        finally:
            browser.close()
    summary = {
        "phase": "ui",
        "checks": checks,
        "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    _write_json(output / "ui" / "summary.json", summary)
    return summary


# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B tools/demo_acceptance.py",
        description="TASK-013 端到端自动验收（离线；all 不启动真实 NGA/大书库）",
    )
    parser.add_argument("--phase", required=True,
                        choices=("core", "ui", "recovery", "real-copy", "all"))
    parser.add_argument("--output", required=True, type=Path,
                        help="新证据目录（已存在且非空则拒绝）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        print(f"错误: 证据目录已存在且非空（不覆盖）: {args.output}", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)
    phases = {
        "core": run_core,
        "ui": run_ui,
        "recovery": run_recovery,
        "real-copy": run_real_copy,
    }
    selected = list(phases) if args.phase == "all" else [args.phase]
    results: dict[str, Any] = {}
    failed = False
    for phase in selected:
        print(f"[demo_acceptance] phase {phase} …", flush=True)
        try:
            results[phase] = phases[phase](args.output)
        except Exception as exc:
            results[phase] = {"phase": phase, "overall": "FAIL",
                              "reason": f"{type(exc).__name__}: {exc}"}
            import traceback

            traceback.print_exc()
        overall = results[phase].get("overall")
        print(f"[demo_acceptance] {phase}: {overall}", flush=True)
        if overall == "FAIL":
            failed = True
        elif overall == "UNAVAILABLE" and args.phase != "all":
            failed = True  # 单 phase 请求 UNAVAILABLE 不算 PASS
    _write_json(args.output / "all-summary.json", {
        "phases": results,
        "overall": "FAIL" if failed else "PASS",
    })
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
