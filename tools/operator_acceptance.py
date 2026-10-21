"""TASK-014 W15/W16：operator 端到端验收（只替换 transport/clock/故障注入）。"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _check(name: str, ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 字节级假 transport ----

class _Resp:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


class _Page:
    def __init__(self, tid, pages):
        self._tid = tid
        self._pages = pages

    def goto(self, url, wait_until=None):
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1])
        body = self._pages.get(page)
        if body is None:
            raise RuntimeError(f"no fake page {page}")
        return _Resp(url, body)

    def title(self):
        return "模拟帖子"


class _Session:
    def __init__(self, page):
        self.page = page

    def close(self):
        pass


def _floor(pid, content, posted_at="2026-08-10 10:00:00"):
    esc = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (f'<tr id="post1strow{pid}"><td><a id="pid{pid}Anchor"></a>'
            f'<span class="postdate" id="postdate{pid}">{posted_at}</span>'
            f'<span class="postcontent" id="postcontent{pid}">{esc}</span></td></tr>')


def _thread_html(tid, last_page, floors_by_page, page=None):
    parts = [f"<script>var __PAGE = {{0:'/read.php?tid={tid}',1:{last_page},2:1,3:20}};"
             "commonui.pageBtn(document.getElementById('pageBtnHere').parentNode,__PAGE,true)</script>"]
    if page is None:
        return ("<html><head><title>模拟帖子</title></head><body>" + "".join(parts)
                + "</body></html>").encode("utf-8")
    parts.append(f'<span class="posterinfo">第 {page} 页</span>')
    for pid, content, posted in floors_by_page.get(page, []):
        parts.append(_floor(pid, content, posted))
    return ("<html><head><title>模拟帖子</title></head><body>" + "".join(parts)
            + "</body></html>").encode("utf-8")


def make_session_factory(tid, last_page, floors_by_page):
    def factory():
        return _Session(_Page(tid, {
            page: _thread_html(tid, last_page, floors_by_page, page=page)
            for page in floors_by_page
        }))
    return factory


def _content_complete(title):
    return f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] 演示标签\n\n正文。"


def _content_formatted(title):
    return (f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] 演示标签\n\n"
            f"[b]{title}加粗正文[/b]\n普通一行。")


# --------------------------------------------------------------------------- #

def run_phase_launch(output: Path) -> dict[str, Any]:
    """Exercise the public launcher check and a real loopback HTTP lifetime."""
    from umalib_ingest.operator.app_factory import create_operator_app

    checks: list[dict[str, Any]] = []
    cmd_proc = subprocess.run(
        ["cmd.exe", "/d", "/c", str(PROJECT_ROOT / "启动录入助手.cmd"), "--check"],
        cwd=str(output), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
        env={key: value for key, value in __import__("os").environ.items()
             if key.upper() != "PYTHONPATH"},
    )
    checks.append(_check("chinese_cmd_from_foreign_cwd", cmd_proc.returncode == 0,
                         {"stdout": cmd_proc.stdout[-500:], "stderr": cmd_proc.stderr[-500:]}))
    proc = subprocess.run(
        [sys.executable, "-B", str(PROJECT_ROOT / "tools" / "launch_operator.py"),
         "--check"],
        cwd=str(output), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=30,
        env={key: value for key, value in __import__("os").environ.items()
             if key.upper() != "PYTHONPATH"},
    )
    checks.append(_check("launcher_check_from_foreign_cwd", proc.returncode == 0,
                         {"stdout": proc.stdout[-500:], "stderr": proc.stderr[-500:]}))
    ctx = create_operator_app(root=output / "launch-root", new_workspace=True,
                              nav_delay=0.0)
    thread = threading.Thread(target=ctx.serve_forever, daemon=True)
    thread.start()
    port = ctx.server.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/operator.html")
        response = conn.getresponse()
        html = response.read().decode("utf-8")
        conn.close()
        checks.append(_check("operator_page_served",
                             response.status == 200 and "大书库录入助手" in html))
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        body = json.dumps({"confirmed": True}).encode("utf-8")
        conn.request("POST", "/api/operator/shutdown", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()
        thread.join(timeout=5)
        checks.append(_check("http_shutdown_stops_server",
                             response.status == 200 and payload["data"]["shutdown"]
                             and not thread.is_alive()))
    finally:
        ctx.shutdown()
    summary = {"phase": "launch", "checks": checks,
               "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
    _write(output / "launch" / "summary.json", summary)
    return summary

def run_phase_threads_scan(output: Path) -> dict[str, Any]:
    from umalib_ingest.operator.app_factory import create_operator_app

    checks: list[dict[str, Any]] = []
    root = output / "threads-scan-root"
    tid = "12345678"
    floors = {
        1: [("91001001", _content_complete("稿一"), "2026-08-10 10:00:00"),
            ("91001002", "普通讨论", "2026-08-10 10:00:00")],
        2: [("91001003", _content_complete("稿二"), "2026-08-11 10:00:00")],
    }
    sf = make_session_factory(tid, 2, floors)
    ctx = create_operator_app(root=root, new_workspace=True, session_factory=sf,
                              nav_delay=0.0)
    try:
        svc = ctx.service

        def wait_run(run_id, timeout=20.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                record = svc.get_run(run_id)
                if record["status"] in ("succeeded", "partial", "cancelled", "failed",
                                        "interrupted"):
                    return record
                time.sleep(0.2)
            return svc.get_run(run_id)

        # 添加非默认帖子
        entry = svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid}", None, 0)
        checks.append(_check("add_nondefault_tid", entry["entry"]["tid"] == tid))
        # 重复添加不清进度
        svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid}", None, 1)
        idx = svc.list_threads()
        checks.append(_check("duplicate_no_reset", len(idx["entries"]) == 1))
        # 初始化（page 模式，从第 1 页开始，覆盖两页触发稿+讨论）
        run_id = svc.initialize_preview(tid, {"mode": "page", "page": 1, "date": None,
                                              "timezone": "UTC+08:00", "lower_bound_utc": None},
                                        0, True)
        run = wait_run(run_id)
        checks.append(_check("init_preview_succeeded", run["status"] == "succeeded",
                             run.get("error")))
        preview = run["result"]
        conf = svc.initialize_confirm(tid, run_id, preview["preview_sha256"], 0, True)
        checks.append(_check("init_confirmed", conf["initialized"]))
        # 扫描
        st = svc.thread_status(tid)
        scan_id = svc.scan(tid, False, True, st["state_sha256"])
        scan = wait_run(scan_id)
        checks.append(_check("scan_succeeded", scan["status"] == "succeeded",
                             scan.get("error")))
        checks.append(_check("scan_imported_triggered",
                             scan["result"]["triggered_count"] == 2
                             and scan["result"]["imported_count"] == 2,
                             scan["result"]))
        checks.append(_check("discussion_count", scan["result"]["discussion_count"] == 1))
        # 同页重扫不重复导入
        st = svc.thread_status(tid)
        scan2 = svc.scan(tid, False, True, st["state_sha256"])
        r2 = wait_run(scan2)
        checks.append(_check("rescan_no_duplicate", r2["result"]["imported_count"] == 0,
                             r2["result"]))
        # Inbox 有 2 条（触发稿）
        items = [s for s in ctx.repository.list_items()]
        checks.append(_check("inbox_two_items", len(items) == 2, len(items)))
        summary = {
            "phase": "threads-scan",
            "checks": checks,
            "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
        }
        _write(output / "threads-scan" / "summary.json", summary)
        return summary
    finally:
        ctx.shutdown()


def run_phase_review_writer(output: Path) -> dict[str, Any]:
    from umalib_ingest.domain.models import SubmissionOrigin
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources

    from umalib_ingest.operator.app_factory import create_operator_app

    checks: list[dict[str, Any]] = []
    root = output / "review-writer-root"
    ctx = create_operator_app(root=root, new_workspace=True)
    try:
        svc = ctx.service
        repository = ctx.repository
        review = ctx.review_service

        def add_approve(title, tag, body):
            item = repository.import_draft(parse_submission(
                f"[收录]\n[标题] {title}\n[作者] 演示作者\n[标签] {tag}\n\n{body}",
                origin=SubmissionOrigin(platform="nga", thread_id="12345678",
                                        post_id=str(uuid4()), floor=None, canonical_url=None)))
            review.save(item.id, ReviewFields(
                title=title, author="演示作者", translator=None, series=None, tags=[tag],
                sources=ReviewSources(nga=[], original_series=[],
                                      original=[f"https://example.com/{title}"],
                                      translation_series=[], translation=[]),
                body_text=body))
            repository.transition(item.id, "approved", "验收")
            return item.id

        from uuid import uuid4

        a = add_approve("累计稿一", "新标签", "正文一：内容不同。")
        b = add_approve("累计稿二", "新标签", "正文二：内容不同。")
        c = add_approve("累计稿三", "同名标签", "正文三：内容不同。")
        # choices
        for item_id, tag, title in ((a, "新标签", "累计稿一"), (b, "新标签", "累计稿二"),
                                     (c, "同名标签", "累计稿三")):
            svc._workspace.save_choices(svc.workspace_id, item_id, {
                "note": None, "upload_time_seconds": 1788062400,
                "ordered_sources": [f"https://example.com/{title}"],
                "tag_choices": ([{"requested_kind": "tag", "requested_value": "同名标签",
                                  "existing_tag_id": 2, "new_tag_type": None}]
                                if tag == "同名标签"
                                else ([{"requested_kind": "tag", "requested_value": "新标签",
                                        "existing_tag_id": None, "new_tag_type": 0}]
                                      if item_id == a else [])),
                "content_mode": "plain", "formatted_body": None,
                "plain_loss_confirmed": False,
            })
        # 三篇累计 writer preview+write（base 为 main-copy，count_after 为绝对文章数）
        import sqlite3

        base_db = ctx.workspace_service.resolve_head(ctx.service.workspace_id)[1].resolve()
        base_conn = sqlite3.connect(base_db.as_uri() + "?mode=ro", uri=True)
        try:
            base_articles = base_conn.execute("SELECT COUNT(*) FROM article").fetchone()[0]
        finally:
            base_conn.close()
        count = 0
        expected_articles = []
        for item_id, title in ((a, "累计稿一"), (b, "累计稿二"), (c, "累计稿三")):
            preview = svc.writer_preview(item_id, "candidate-x.db")
            plan = preview["plan"]
            pid = preview["preview_id"]
            result = svc.writer_write(item_id, pid, True)
            count = result["delivery"]["count_after"]
            article = plan["article"]
            plan_tags = plan.get("tags") or {}
            expected_articles.append({
                "title": title,
                "inserted_article_id": result["result"]["inserted"]["article_id"],
                "name": article["name"], "note": article.get("note"),
                "content": article["content"], "author": article["author"],
                "translator": article.get("translator"),
                "upload_time": article["upload_time"], "source": article["source"],
                "existing_tags": [
                    {"id": tag["id"], "name": tag["name"], "type": tag["type"]}
                    for tag in (plan_tags.get("existing") or [])],
                "created_tags": [
                    {"name": tag["name"], "type": tag["type"]}
                    for tag in (plan_tags.get("create") or [])],
            })
        checks.append(_check("three_articles_cumulative", count == base_articles + 3,
                             {"base": base_articles, "count_after": count}))
        view = svc.delivery_view_copy(result["delivery"]["operation_id"], True)
        checks.append(_check(
            "view_copy_separate_and_identical",
            Path(view["view_copy_path"]).is_file()
            and Path(view["view_copy_path"]) != Path(view["candidate_path"])
            and hashlib.sha256(Path(view["view_copy_path"]).read_bytes()).hexdigest()
            == view["sha256"],
            view,
        ))
        summary = {
            "phase": "review-writer",
            "checks": checks,
            "expectations": {
                "base_database": str(base_db),
                "candidate_database": str(ctx.workspace_service.resolve_head(
                    ctx.service.workspace_id)[1].resolve()),
                "articles": expected_articles,
            },
            "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
        }
        _write(output / "review-writer" / "summary.json", summary)
        return summary
    finally:
        ctx.shutdown()


def run_phase_real_copy(output: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    main = Path(r"D:\codex\13\umalib-test-data\main-copy.db")
    if not main.exists():
        return {"phase": "real-copy", "overall": "UNAVAILABLE", "reason": "main-copy 缺失"}
    from umalib_ingest.operator.app_factory import verify_source_path

    before = verify_source_path(main)
    sibling_names = sorted(p.name for p in main.parent.iterdir())
    exercise_root = output / "real-copy-exercise"
    exercise = run_phase_review_writer(exercise_root)
    checks.append(_check("three_writes_completed", exercise["overall"] == "PASS", exercise))
    manifests = list((exercise_root / "review-writer-root" / "workspaces").glob("*/workspace.json"))
    if len(manifests) != 1:
        raise RuntimeError(f"expected one workspace manifest, got {len(manifests)}")
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    head_name = manifest["head"]["candidate_file_name"]
    candidate = manifests[0].parent / "candidates" / head_name
    conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM article").fetchone()[0]
        titles = {row[0] for row in conn.execute(
            "SELECT name FROM article WHERE name IN (?,?,?)",
            ("累计稿一", "累计稿二", "累计稿三"))}
    finally:
        conn.close()
    base_conn = sqlite3.connect(main.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        base_count = base_conn.execute("SELECT COUNT(*) FROM article").fetchone()[0]
    finally:
        base_conn.close()
    checks.append(_check("candidate_integrity_and_plus_three",
                         integrity == "ok" and count == base_count + 3
                         and titles == {"累计稿一", "累计稿二", "累计稿三"},
                         {"integrity": integrity, "base": base_count, "candidate": count,
                          "titles": sorted(titles)}))
    after = verify_source_path(main)
    checks.append(_check("source_database_unchanged",
                         before["sha256"] == after["sha256"]
                         and sibling_names == sorted(p.name for p in main.parent.iterdir()),
                         before["sha256"]))
    # Q2：候选逐字段精确回读（标题/作者/译者/录入时间/来源/正文/注释/标签 type 与 tagged 关联）
    expectations = exercise.get("expectations") or {}
    expected_articles = expectations.get("articles") or []
    read_conn = sqlite3.connect(candidate.resolve().as_uri() + "?mode=ro", uri=True)
    read_base = sqlite3.connect(
        Path(expectations["base_database"]).resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True)
    try:
        field_ok = len(expected_articles) == 3
        field_detail = []
        tag_ok = len(expected_articles) == 3
        tag_detail = []
        for exp in expected_articles:
            rows = read_conn.execute(
                "SELECT id, name, note, content, author, translator, uploadTime, source "
                "FROM article WHERE name = ?", (exp["name"],)).fetchall()
            expected_row = (exp["inserted_article_id"], exp["name"], exp["note"],
                            exp["content"], exp["author"], exp["translator"],
                            exp["upload_time"], exp["source"])
            match = len(rows) == 1 and tuple(rows[0]) == expected_row
            field_ok = field_ok and match
            field_detail.append({"title": exp["title"], "match": match,
                                 "actual": [tuple(row) for row in rows]})
            if not match:
                tag_ok = False
                continue
            article_id = exp["inserted_article_id"]
            joined = read_conn.execute(
                "SELECT t.id, t.name, t.type FROM tagged g "
                "JOIN tag t ON t.id = g.tagId WHERE g.artId = ? ORDER BY t.id",
                (article_id,)).fetchall()
            expected_pairs = sorted(
                [(tag["name"], tag["type"]) for tag in exp.get("existing_tags", [])]
                + [(tag["name"], tag["type"]) for tag in exp.get("created_tags", [])])
            actual_pairs = sorted((row[1], row[2]) for row in joined)
            existing_ids_ok = all(
                any(row[0] == tag["id"] and row[1] == tag["name"] and row[2] == tag["type"]
                    for row in joined)
                for tag in exp.get("existing_tags", []))
            no_duplicate = len(joined) == len({row[0] for row in joined})
            ok = (len(joined) == len(expected_pairs) and actual_pairs == expected_pairs
                  and existing_ids_ok and no_duplicate)
            tag_ok = tag_ok and ok
            tag_detail.append({"title": exp["title"], "expected": expected_pairs,
                               "actual": actual_pairs, "existing_ids_ok": existing_ids_ok,
                               "no_duplicate": no_duplicate})
        checks.append(_check("candidate_article_fields_exact", field_ok, field_detail))
        checks.append(_check("candidate_tagged_exact", tag_ok, tag_detail))
        fk_rows = read_conn.execute("PRAGMA foreign_key_check").fetchall()
        fk_integrity = read_conn.execute("PRAGMA integrity_check").fetchone()[0]
        checks.append(_check("candidate_fk_and_integrity_ok",
                             not fk_rows and fk_integrity == "ok",
                             {"foreign_key_check": [tuple(row) for row in fk_rows],
                              "integrity": fk_integrity}))
        base_max_article = read_base.execute("SELECT MAX(id) FROM article").fetchone()[0]
        base_max_tag = read_base.execute("SELECT MAX(id) FROM tag").fetchone()[0]
        old_article_ok = read_base.execute(
            "SELECT id, name, note, content, author, translator, uploadTime, source "
            "FROM article ORDER BY id").fetchall() == read_conn.execute(
            "SELECT id, name, note, content, author, translator, uploadTime, source "
            "FROM article WHERE id <= ? ORDER BY id", (base_max_article,)).fetchall()
        old_tag_ok = read_base.execute(
            "SELECT id, name, type, cover, description FROM tag ORDER BY id").fetchall() \
            == read_conn.execute(
            "SELECT id, name, type, cover, description FROM tag WHERE id <= ? ORDER BY id",
            (base_max_tag,)).fetchall()
        old_tagged_ok = read_base.execute(
            "SELECT artId, tagId FROM tagged ORDER BY artId, tagId").fetchall() \
            == read_conn.execute(
            "SELECT artId, tagId FROM tagged WHERE artId <= ? ORDER BY artId, tagId",
            (base_max_article,)).fetchall()
        checks.append(_check("base_old_rows_unchanged",
                             old_article_ok and old_tag_ok and old_tagged_ok,
                             {"article": old_article_ok, "tag": old_tag_ok,
                              "tagged": old_tagged_ok}))
        base_schema = read_base.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        cand_schema = read_conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        target_tables = {"article", "tag", "tagged", "sqlite_sequence"}
        other_detail = {}
        other_counts_ok = True
        for (table,) in read_base.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            if table in target_tables:
                continue
            base_count = read_base.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            cand_count = read_conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            other_detail[table] = {"base": base_count, "candidate": cand_count}
            other_counts_ok = other_counts_ok and base_count == cand_count
        checks.append(_check("schema_and_non_target_tables_unchanged",
                             [tuple(row) for row in base_schema]
                             == [tuple(row) for row in cand_schema] and other_counts_ok,
                             other_detail))
    finally:
        read_conn.close()
        read_base.close()
    summary = {
        "phase": "real-copy",
        "checks": checks,
        "source": {"file_name": before["file_name"], "sha256": before["sha256"],
                   "size": before["size"]},
        "candidate": {"path": str(candidate), "sha256": hashlib.sha256(
            candidate.read_bytes()).hexdigest(), "article_count": count},
        "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL",
    }
    _write(output / "real-copy" / "summary.json", summary)
    return summary


def run_phase_recovery(output: Path) -> dict[str, Any]:
    """Checkpoint and restore a real operator workspace, then reopen it."""
    from umalib_ingest.operator.app_factory import create_operator_app

    checks: list[dict[str, Any]] = []
    root = output / "recovery-root"
    ctx = create_operator_app(root=root, new_workspace=True, nav_delay=0.0)
    original_id = ctx.service.workspace_id
    try:
        ctx.service.add_thread("https://bbs.nga.cn/read.php?tid=30099686", "目标帖", 0)
        imported = ctx.service.manual_import(_content_complete("恢复验证稿"))
        checkpoint = ctx.checkpoint_service.create(original_id, True)
        restored = ctx.checkpoint_service.restore(
            original_id, checkpoint["checkpoint_id"], True)
        restored_id = restored["new_workspace_id"]
    finally:
        ctx.shutdown()
    reopened = create_operator_app(root=root, nav_delay=0.0)
    try:
        checks.append(_check("restored_workspace_selected",
                             reopened.service.workspace_id == restored_id,
                             {"original": original_id, "restored": restored_id,
                              "opened": reopened.service.workspace_id}))
        threads = reopened.service.list_threads()
        checks.append(_check("thread_registry_restored",
                             len(threads["entries"]) == 1
                             and threads["entries"][0]["tid"] == "30099686"))
        item = reopened.repository.get(imported["id"])
        checks.append(_check("inbox_restored",
                             item is not None
                             and item.working_draft["metadata"]["title"] == "恢复验证稿"))
    finally:
        reopened.shutdown()
    summary = {"phase": "recovery", "checks": checks,
               "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
    _write(output / "recovery" / "summary.json", summary)
    return summary


def run_phase_ui(output: Path) -> dict[str, Any]:
    """C6：真实浏览器经产品 DOM 完成两帖三篇累计、检查点与 R06/R08 断言。"""
    from playwright.sync_api import sync_playwright

    from umalib_ingest.operator.app_factory import create_operator_app

    checks: list[dict[str, Any]] = []
    root = output / "ui-product-root"
    root.mkdir(parents=True, exist_ok=True)
    tid_a, tid_b = "12345678", "87654321"
    pages_a = {1: _thread_html(tid_a, 1, {
        1: [("91002001", _content_complete("界面稿一"), "2026-08-10 10:00:00"),
            ("91002002", _content_complete("界面稿二"), "2026-08-11 10:00:00")]}, page=1)}
    pages_b = {1: _thread_html(tid_b, 1, {
        1: [("91002003", _content_complete("界面稿三"), "2026-08-12 10:00:00"),
            ("91002004", _content_complete("界面旧稿"), "2026-07-01 10:00:00")]}, page=1)}

    class _MultiPage:
        def goto(self, url, wait_until=None):
            tid = url.split("tid=")[1].split("&")[0]
            table = pages_a if tid == tid_a else pages_b
            return _Resp(url, table[1])

        def title(self):
            return "模拟"

    class _MultiSession:
        def __init__(self):
            self.page = _MultiPage()

        def close(self):
            pass

    ctx = create_operator_app(root=root, new_workspace=True,
                              session_factory=lambda: _MultiSession(), nav_delay=0.0)
    server_thread = threading.Thread(target=ctx.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{ctx.server.server_address[1]}"
    try:
        svc = ctx.service
        main_copy = Path(r"D:\codex\13\umalib-test-data\main-copy.db")
        if main_copy.exists():
            verified = svc.verify_source(main_copy)
            svc.bind_source(verified["source_id"], "UI 验收工作区", True)
        svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid_a}", "A帖", 0)
        svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid_b}", "B帖", 1)

        shots = output / "ui" / "screenshots"
        shots.mkdir(parents=True, exist_ok=True)

        def ui_init(page, tid, mode, value=None):
            page.locator("#main-nav button", has_text="帖子与扫描").click()
            page.wait_for_selector("#thread-entries .thread-entry", timeout=15000)
            entry = page.locator("#thread-entries .thread-entry", has_text=tid)
            entry.click(position={"x": 8, "y": 8})
            page.wait_for_function(
                "tid => { const row = Array.from(document.querySelectorAll("
                "'#thread-entries .thread-entry')).find(r => r.textContent.indexOf(tid) >= 0);"
                " return !!(row && row.classList.contains('active')); }",
                arg=tid, timeout=15000)
            page.locator('input[name="start-mode"][value="' + mode + '"]').check()
            if mode == "page":
                page.locator("#start-page").fill(str(value))
            elif mode == "date":
                page.locator("#start-date").fill(value)
            page.locator("#confirm-start-btn").click()
            try:
                page.wait_for_function(
                    "() => document.querySelector('#start-msg').textContent.includes('预览完成')",
                    timeout=20000)
            except Exception:
                raise AssertionError("起点预览未完成：#start-msg=" + (
                    page.locator("#start-msg").text_content() or ""))
            page.locator("#initialize-btn").click()
            try:
                page.wait_for_function(
                    "() => { const t = document.querySelector('#scan-msg').textContent;"
                    " return t.includes('初始化') || t.indexOf('[') >= 0; }",
                    timeout=20000)
            except Exception:
                raise AssertionError("初始化未完成：#scan-msg=" + (
                    page.locator("#scan-msg").text_content() or ""))
            init_text = page.locator("#scan-msg").text_content() or ""
            assert "初始化" in init_text, "初始化失败：" + init_text

        def ui_scan(page):
            page.locator("#scan-btn").click()
            page.wait_for_function(
                "() => document.querySelector('#run-result-panel').className.indexOf('hidden') < 0",
                timeout=30000)

        def ui_write_article(page, title):
            page.locator("#main-nav button", has_text="待审稿件").click()
            page.wait_for_selector("#items-body tr", timeout=15000)
            page.locator("#items-body tr", has_text=title).click()
            page.wait_for_selector("#review-form [data-field=title]", timeout=15000)
            page.locator("#review-form [data-field=author]").fill("UI 演示作者")
            page.locator("#review-form [data-field=body_text]").fill("正文：" + title)
            page.locator("#review-form button", has_text="保存修改").click()
            page.wait_for_function(
                "() => document.querySelector('#status-diagnostic').textContent.includes('已保存')",
                timeout=15000)
            page.locator("#review-form button", has_text="批准稿件").click()
            page.wait_for_function(
                "() => document.querySelector('#status-diagnostic').textContent.includes('已批准')",
                timeout=15000)
            page.locator("#review-form input[type=datetime-local]").fill("2026-08-20T10:00")
            # 标签目录异步加载完成后：零匹配标签必须人工选类型；已有同名则自动复用
            page.wait_for_selector("#review-form .tag-decision select", timeout=30000)
            selects = page.locator("#review-form .tag-decision select")
            for i in range(selects.count()):
                select = selects.nth(i)
                if select.input_value() != "":
                    continue
                options = select.locator("option")
                has_existing = False
                for j in range(options.count()):
                    if (options.nth(j).get_attribute("value") or "").startswith("existing:"):
                        has_existing = True
                        break
                if not has_existing:
                    select.select_option("new:0")
                else:
                    for j in range(options.count()):
                        value = options.nth(j).get_attribute("value") or ""
                        if value.startswith("existing:"):
                            select.select_option(value)
                            break
            page.locator("#review-form button", has_text="保存人工决定").click()
            page.wait_for_function(
                "() => document.querySelector('#review-form').textContent.includes('请重新生成预览')",
                timeout=15000)
            page.locator("#review-form button", has_text="生成并查看预览").click()
            page.wait_for_function(
                "() => document.querySelector('#review-form').textContent.includes('最终写入预览')",
                timeout=30000)
            write_button = page.locator("#review-form button", has_text="确认生成候选数据库")
            if write_button.is_disabled():
                preview_text = page.locator("#review-form .writer-preview-inline").text_content() or ""
                raise AssertionError("预览存在阻断项，不能写入：" + preview_text[:500])
            page.locator("#review-form input[type=checkbox]").last.check()
            write_button.click()
            page.wait_for_function(
                "() => document.querySelector('#review-form').textContent.includes('候选已生成')",
                timeout=180000)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                bc = browser.new_context(viewport={"width": 1366, "height": 768})
                page = bc.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base, wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=20000)

                # R08：补查/中文/翻页
                page.locator("#main-nav button", has_text="待审稿件").click()
                page.wait_for_timeout(500)
                checks.append(_check("R08_rescan_controls",
                                     page.locator("#rescan-panel button").count() >= 1))
                checks.append(_check("R08_prev_next",
                                     page.locator("#filter-prev").count() == 1
                                     and page.locator("#filter-next").count() == 1))
                page.screenshot(path=str(shots / "01-home-1366.png"), full_page=True)

                # A 帖：页起点 → 扫描 → 两篇
                ui_init(page, tid_a, "page", 1)
                ui_scan(page)
                page.locator("#main-nav button", has_text="待审稿件").click()
                page.wait_for_selector("#items-body tr", timeout=15000)
                rows = page.locator("#items-body").text_content()
                checks.append(_check("R08_chinese_status",
                                     "待审" in rows and "candidate" not in rows,
                                     rows[:120]))
                ui_write_article(page, "界面稿一")
                ui_write_article(page, "界面稿二")

                # B 帖：日期起点（排除旧稿）→ 扫描 → 一篇
                page.locator("#main-nav button", has_text="帖子与扫描").click()
                ui_init(page, tid_b, "date", "2026-08-01")
                ui_scan(page)
                ui_write_article(page, "界面稿三")

                # 检查点
                page.locator("#main-nav button", has_text="交付与恢复").click()
                page.wait_for_selector("#deliveries", timeout=15000)
                page.locator("#checkpoint-create-btn").click()
                page.wait_for_timeout(2500)
                page.screenshot(path=str(shots / "02-delivery-1366.png"), full_page=True)

                deliveries = svc._workspace.list_deliveries(svc.workspace_id)
                checks.append(_check("three_cumulative",
                                     len(deliveries) == 3
                                     and deliveries[-1]["count_after"]
                                     >= deliveries[0]["count_after"] + 2,
                                     [d["count_after"] for d in deliveries]))
                checks.append(_check("date_excluded_old",
                                     not ctx.repository.list_items_page(
                                         thread_id=tid_b, query="界面旧稿")[1]))
                checks.append(_check("ui_no_page_errors", errors == [], errors))
                bc.close()
            finally:
                browser.close()
        summary = {"phase": "ui", "checks": checks,
                   "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
        _write(output / "ui" / "summary.json", summary)
        return summary
    finally:
        ctx.shutdown()


def run_phase_lifecycle(output: Path) -> dict[str, Any]:
    """D5/F004：三篇累计→检查点→退出→重开→恢复新工作区→继续扫描写第四篇。"""
    from uuid import uuid4

    from umalib_ingest.demo.samples import create_synthetic_base
    from umalib_ingest.domain.models import SubmissionOrigin
    from umalib_ingest.operator.app_factory import create_operator_app
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.review import ReviewFields, ReviewSources

    checks: list[dict[str, Any]] = []
    root = output / "lifecycle-root"
    src_dir = root / "source"
    src_dir.mkdir(parents=True, exist_ok=True)
    synthetic = src_dir / "synthetic-source.db"
    create_synthetic_base(synthetic)

    tid = "12345678"
    floor_list = [
        ("91003001", _content_complete("生命周期稿一"), "2026-08-10 10:00:00"),
        ("91003002", _content_complete("生命周期稿二"), "2026-08-11 10:00:00"),
        ("91003003", _content_complete("生命周期稿三"), "2026-08-12 10:00:00"),
    ]

    def factory():
        body = _thread_html(tid, 1, {1: list(floor_list)}, page=1)

        class _P:
            def goto(self, url, wait_until=None):
                return _Resp(url, body)

            def title(self):
                return "模拟"

        class _S:
            def __init__(self):
                self.page = _P()

            def close(self):
                pass

        return _S()

    def wait(svc, run_id, timeout=120.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            run = svc.get_run(run_id)
            if run["status"] in ("succeeded", "partial", "cancelled", "failed",
                                 "interrupted", "awaiting_login"):
                return run
            time.sleep(0.1)
        return svc.get_run(run_id)

    def open_app(new=False):
        return create_operator_app(root=root, new_workspace=new,
                                   session_factory=factory, nav_delay=0.0)

    def scan_thread(ctx):
        svc = ctx.service
        st = svc.thread_status(tid)
        run = wait(svc, svc.scan(tid, False, True, st["state_sha256"]))
        assert run["status"] == "succeeded", run.get("error")
        return run["result"]

    def write_current_items(ctx, titles):
        svc = ctx.service
        from umalib_ingest.umalib_writer import load_target_tag_catalog

        written = 0
        for summary in ctx.repository.list_items_page()[0]:
            item = ctx.repository.get(summary.id)
            title = (item.working_draft.get("metadata") or {}).get("title") or ""
            if title not in titles:
                continue
            if item.status == "approved":
                pass
            else:
                ctx.review_service.save(summary.id, ReviewFields(
                    title=title, author="生命周期作者", translator=None, series=None,
                    tags=["生命周期标签"],
                    sources=ReviewSources(nga=[], original_series=[],
                                          original=[f"https://example.com/{title}"],
                                          translation_series=[], translation=[]),
                    body_text=f"正文：{title}"))
                ctx.repository.transition(summary.id, "approved", "生命周期验收")
            # 按当前 HEAD 目录决定：唯一同名复用，否则人工选择新建
            _head, source = svc._workspace.resolve_head(svc.workspace_id)
            matches = [t for t in load_target_tag_catalog(source).tags
                       if t.name == "生命周期标签"]
            if len(matches) == 1:
                choice = {"requested_kind": "tag", "requested_value": "生命周期标签",
                          "existing_tag_id": matches[0].id, "new_tag_type": None}
            else:
                choice = {"requested_kind": "tag", "requested_value": "生命周期标签",
                          "existing_tag_id": None, "new_tag_type": 0}
            svc._workspace.save_choices(svc.workspace_id, summary.id, {
                "note": None, "upload_time_seconds": 1788062400,
                "ordered_sources": [f"https://example.com/{title}"],
                "tag_choices": [choice],
                "content_mode": "plain", "formatted_body": None,
                "plain_loss_confirmed": False,
            })
            preview = svc.writer_preview(summary.id, "candidate-x.db")
            svc.writer_write(summary.id, preview["preview_id"], True)
            written += 1
        return written

    ctx = open_app(new=True)
    try:
        svc = ctx.service
        verified = svc.verify_source(synthetic)
        svc.bind_source(verified["source_id"], "生命周期工作区", True)
        svc.add_thread(f"https://bbs.nga.cn/read.php?tid={tid}", "生命周期帖", 0)
        run_id = svc.initialize_preview(tid, {
            "mode": "page", "page": 1, "date": None,
            "timezone": "UTC+08:00", "lower_bound_utc": None}, 0, True)
        run = wait(svc, run_id)
        svc.initialize_confirm(tid, run_id, run["result"]["preview_sha256"], 0, True)
        scan_thread(ctx)
        written = write_current_items(ctx, ["生命周期稿一", "生命周期稿二", "生命周期稿三"])
        workspace_1 = svc.workspace_id
        deliveries_before = svc._workspace.list_deliveries(workspace_1)
        checks.append(_check("three_written", written == 3 and len(deliveries_before) == 3,
                             {"written": written, "deliveries": len(deliveries_before)}))
        checkpoint = svc.create_checkpoint(True)
    finally:
        ctx.shutdown()

    # 退出后重开：原工作区、交付与待审仍在
    ctx2 = open_app()
    try:
        checks.append(_check("reopen_same_workspace",
                             ctx2.service.workspace_id == workspace_1))
        deliveries = ctx2.service._workspace.list_deliveries(workspace_1)
        checks.append(_check("reopen_deliveries_kept", len(deliveries) == 3,
                             len(deliveries)))
        items = ctx2.repository.list_items_page()[0]
        checks.append(_check("reopen_items_kept", len(items) == 3, len(items)))
        restored = ctx2.service.restore_checkpoint(checkpoint["checkpoint_id"], True)
        new_id = restored["new_workspace_id"]
        launcher = ctx2.service._workspace.store().read_launcher()
        checks.append(_check("restore_new_uuid", new_id != workspace_1, new_id))
        checks.append(_check("launcher_points_to_new",
                             launcher.get("last_workspace_id") == new_id,
                             launcher.get("last_workspace_id")))
    finally:
        ctx2.shutdown()

    # 恢复后的新工作区：继续扫描并写第四篇
    ctx3 = open_app()
    try:
        svc = ctx3.service
        checks.append(_check("reopen_restored_workspace", svc.workspace_id == new_id,
                             svc.workspace_id))
        deliveries = svc._workspace.list_deliveries(new_id)
        checks.append(_check("restored_deliveries_kept", len(deliveries) == 3,
                             len(deliveries)))
        floor_list.append(("91003004", _content_complete("生命周期稿四"),
                           "2026-08-13 10:00:00"))
        result = scan_thread(ctx3)
        checks.append(_check("fourth_scanned", result["triggered_count"] == 1,
                             result))
        written = write_current_items(ctx3, ["生命周期稿四"])
        deliveries_after = svc._workspace.list_deliveries(new_id)
        checks.append(_check("fourth_written_cumulative",
                             written == 1 and len(deliveries_after) == 4,
                             {"written": written, "deliveries": len(deliveries_after)}))
        # 前三篇与来源链保留（抽查稿一，读取审核后的工作稿来源）
        first = next((ctx3.repository.get(s.id)
                      for s in ctx3.repository.list_items_page()[0]
                      if ((ctx3.repository.get(s.id).working_draft.get("metadata") or {})
                          .get("title") == "生命周期稿一")), None)
        fields = ctx3.review_service.load(first.id) if first is not None else None
        sources = list(fields.sources.original) if fields is not None else []
        checks.append(_check("first_three_sources_preserved",
                             sources == ["https://example.com/生命周期稿一"], sources))
    finally:
        ctx3.shutdown()

    summary = {"phase": "lifecycle", "checks": checks,
               "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
    _write(output / "lifecycle" / "summary.json", summary)
    return summary


def _spawn_operator_cmd(root: Path, scenario: Path, source: Path):
    """从非项目 cwd、无 PYTHONPATH 走中文 CMD 实际启动，返回 (proc, port)。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "UMALIB_OPERATOR_ROOT": str(root),
        "UMALIB_OPERATOR_TEST_TRANSPORT": "tools.acceptance_transport:make_factory",
        "UMALIB_OPERATOR_TEST_SCENARIO": str(scenario),
        "UMALIB_OPERATOR_TEST_SOURCE": str(source),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    cmd_path = PROJECT_ROOT / "启动录入助手.cmd"
    command = f'cmd /c ""{cmd_path}" --no-browser --port 0"'
    proc = subprocess.Popen(
        command,
        cwd=r"C:\Windows", env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    import re as _re

    deadline = time.monotonic() + 120
    captured: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        captured.append(line)
        match = _re.search(r"http://127\.0\.0\.1:(\d+)", line)
        if match:
            return proc, int(match.group(1))
    try:
        _kill_process_tree(proc)
    except Exception:
        pass
    raise AssertionError("CMD 未启动到页面: " + "".join(captured[-6:]))


def _kill_process_tree(proc) -> None:
    """Kill a spawned CMD and its python server children; never leave listeners."""
    if proc is None or proc.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _http(port: int, method: str, path: str, body: dict | None = None):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path,
                 body=json.dumps(body).encode("utf-8") if body is not None else None,
                 headers=headers)
    res = conn.getresponse()
    data = res.read().decode("utf-8")
    conn.close()
    return res.status, (json.loads(data) if data else {})


def install_page_guards(page, errors: list, blocked: list) -> None:
    """Q1：每个 context/page 都安装页面异常与外部请求阻断检查。"""
    page.on("pageerror", lambda e: errors.append(str(e)))

    def route_block(route):
        if not route.request.url.startswith("http://127.0.0.1"):
            blocked.append(route.request.url)
            route.abort()
        else:
            route.continue_()

    page.context.route("**/*", route_block)


def _journey_helpers(page, base: str):
    """Q1 DOM 步骤（复用与 ui phase 相同的产品选择器）。"""

    def wait(pattern, timeout=30000):
        page.wait_for_function(pattern, timeout=timeout)

    def add_thread(tid, name):
        page.locator("#main-nav button", has_text="帖子与扫描").click()
        page.wait_for_selector("#thread-url", state="visible", timeout=15000)
        page.locator("#thread-url").fill(f"https://bbs.nga.cn/read.php?tid={tid}")
        page.locator("#thread-name").fill(name)
        page.locator("#add-thread-btn").click()
        try:
            page.wait_for_selector(f"#thread-entries .thread-entry:has-text('{tid}')",
                                   timeout=20000)
        except Exception:
            hint = (page.locator("#thread-msg").text_content() or "").strip()
            diag = (page.locator("#status-diagnostic").text_content() or "").strip()
            listing = page.locator("#thread-entries").inner_html()
            raise AssertionError(
                f"添加帖子失败: thread-msg={hint!r} diag={diag!r} entries_html={listing[:300]!r}"
            ) from None

    def init_thread(tid, mode, value=None):
        page.locator("#main-nav button", has_text="帖子与扫描").click()
        page.wait_for_selector("#thread-entries .thread-entry", timeout=15000)
        page.locator("#thread-entries .thread-entry", has_text=tid).click(
            position={"x": 8, "y": 8})
        wait("() => { const row = Array.from(document.querySelectorAll("
             "'#thread-entries .thread-entry'))"
             f".find(r => r.textContent.indexOf('{tid}') >= 0);"
             " return !!(row && row.classList.contains('active')); }", timeout=15000)
        page.locator(f'input[name="start-mode"][value="{mode}"]').check()
        if mode == "page":
            page.locator("#start-page").fill(str(value))
        elif mode == "date":
            page.locator("#start-date").fill(value)
        page.locator("#confirm-start-btn").click()
        wait("() => document.querySelector('#start-msg').textContent.includes('预览完成')",
             timeout=30000)
        page.locator("#initialize-btn").click()
        wait("() => document.querySelector('#scan-msg').textContent.includes('初始化')",
             timeout=30000)

    def scan_thread():
        page.locator("#scan-btn").click()
        try:
            wait("() => document.querySelector('#run-result-panel').className.indexOf('hidden') < 0",
                 timeout=60000)
        except Exception:
            raise AssertionError(
                "扫描结果未出现: scan-msg="
                + (page.locator("#scan-msg").text_content() or "").strip()
                + " | run-result="
                + (page.locator("#run-result").text_content() or "").strip()[:200]
                + " | diag="
                + (page.locator("#status-diagnostic").text_content() or "").strip()[:200]
            ) from None
        page.wait_for_timeout(300)

    def open_item(title):
        page.locator("#main-nav button", has_text="待审稿件").click()
        page.wait_for_selector("#items-body tr", timeout=20000)
        page.locator("#items-body tr", has_text=title).first.click()
        page.wait_for_selector("#review-form [data-field=title]", timeout=20000)

    def save_choices(title, *, note=None, upload_local=None, extract_format=False,
                     extra_original=None, write=True, expect_choices: list | None = None):
        """保存审核字段与人工决定；可保留未导出的 A068 稿（write=False）。"""
        open_item(title)
        page.locator("#review-form [data-field=author]").fill("旅程作者")
        page.locator("#review-form [data-field=body_text]").fill("正文：" + title)
        if upload_local is not None:
            page.locator("#review-form input[type=datetime-local]").fill(upload_local)
        if note is not None:
            page.locator("#review-form label", has_text="备注").locator(
                "input").fill(note)
        if extra_original:
            details = page.locator("#review-form details.sources-block")
            if details.count():
                details.first.evaluate("d => { d.open = true; }")
            areas = page.locator("#review-form .sources-block textarea")
            areas.nth(2).fill("\n".join(extra_original))
        if extract_format:
            page.locator("#review-form button", has_text="从原始楼层提取基础格式").click()
            wait("() => document.querySelector('#review-form').textContent"
                 ".includes('已从原始楼层提取基础 UBB')", timeout=30000)
        # 审核字段先于批准保存（批准后审核字段锁定，修改需先退回待审）
        save_first = page.locator("#review-form button", has_text="保存修改")
        if save_first.is_enabled():
            save_first.click()
            try:
                wait("() => document.querySelector('#status-diagnostic')"
                     ".textContent.includes('已保存')")
            except Exception:
                diag = (page.locator("#status-diagnostic").text_content() or "").strip()
                msgs = " | ".join(page.locator("#review-form .msg").all_text_contents())
                raise AssertionError(
                    f"保存审核字段未完成: diag={diag!r} msgs={msgs!r}") from None
        page.locator("#review-form button", has_text="批准稿件").click()
        wait("() => document.querySelector('#status-diagnostic').textContent.includes('已批准')")
        page.wait_for_selector("#review-form .tag-decision select", timeout=30000)
        selects = page.locator("#review-form .tag-decision select")
        chosen_tags = []
        for i in range(selects.count()):
            select = selects.nth(i)
            if select.input_value() != "":
                chosen_tags.append(select.input_value())
                continue
            options = select.locator("option")
            chosen = None
            for j in range(options.count()):
                value = options.nth(j).get_attribute("value") or ""
                if value.startswith("existing:"):
                    chosen = value
                    break
            chosen = chosen or "new:0"
            select.select_option(chosen)
            chosen_tags.append(chosen)
        page.locator("#review-form button", has_text="保存人工决定").click()
        try:
            wait("() => document.querySelector('#review-form').textContent"
                 ".includes('请重新生成预览')")
        except Exception:
            diag = (page.locator("#status-diagnostic").text_content() or "").strip()
            msgs = " | ".join(page.locator("#review-form .msg").all_text_contents())
            raise AssertionError(
                f"保存人工决定未完成: diag={diag!r} msgs={msgs!r}") from None
        if expect_choices is not None:
            expect_choices.extend(chosen_tags)
        if not write:
            return
        page.locator("#review-form button", has_text="生成并查看预览").click()
        wait("() => document.querySelector('#review-form').textContent.includes('最终写入预览')",
             timeout=60000)
        write_button = page.locator("#review-form button", has_text="确认生成候选数据库")
        if write_button.is_disabled():
            detail = page.locator("#review-form .writer-preview-inline").text_content() or ""
            raise AssertionError("写入被阻断：" + detail[:300])
        page.locator("#review-form input[type=checkbox]").last.check()
        write_button.click()
        wait("() => document.querySelector('#review-form').textContent.includes('候选已生成')",
             timeout=180000)
        page.wait_for_timeout(300)

    def write_article(title):
        save_choices(title, upload_local="2026-08-20T10:00")

    def read_choices_dom():
        """读取当前审核稿的人工决定可见值（A068 回填比对）。"""
        page.wait_for_selector("#review-form .tag-decision select", timeout=30000)
        return page.evaluate(
            "() => {"
            " const form = document.querySelector('#review-form');"
            " const noteLabel = Array.from(form.querySelectorAll('label'))"
            "   .find(l => (l.textContent || '').indexOf('备注') >= 0);"
            " const mode = form.querySelector('select:has(option[value=nga_ubb])');"
            " const formatted = form.querySelector('textarea[placeholder*=格式正文]');"
            " const areas = form.querySelectorAll('.sources-block textarea');"
            " const tags = Array.from(form.querySelectorAll('.tag-decision select'))"
            "   .map(s => s.value);"
            " return {"
            "   note: noteLabel ? noteLabel.querySelector('input').value : null,"
            "   upload: form.querySelector('input[type=datetime-local]').value,"
            "   content_mode: mode ? mode.value : null,"
            "   formatted: formatted ? formatted.value : null,"
            "   categories: Array.from(areas).map(a => a.value),"
            "   tags: tags,"
            " }; }"
        )

    def exit_app():
        page.locator("#main-nav button", has_text="设置与帮助").click()
        page.wait_for_timeout(300)
        page.locator("#shutdown-btn").click()
        page.wait_for_timeout(1500)

    return (add_thread, init_thread, scan_thread, write_article, exit_app,
            save_choices, read_choices_dom, open_item)


def run_phase_ui_journey(output: Path) -> dict[str, Any]:
    """Q1：中文 CMD 实际入口 + 两帖三篇 + 检查点 + 重开恢复第四篇（真实 DOM/HTTP）。"""
    import subprocess as _subprocess

    from playwright.sync_api import sync_playwright

    from umalib_ingest.demo.samples import create_synthetic_base

    checks: list[dict[str, Any]] = []
    root = output / "ui-journey-root"
    (root / "source").mkdir(parents=True, exist_ok=True)
    source = root / "source" / "synthetic-source.db"
    create_synthetic_base(source)
    scenario_path = root / "scenario.json"
    tid_a, tid_b = "12345678", "87654321"
    scenario = {
        tid_a: {"last_page": 1, "floors": {"1": [
            ["91004001", _content_complete("旅程稿一"), "2026-08-10 10:00:00"],
            ["91004002", _content_complete("旅程稿二"), "2026-08-11 10:00:00"],
            ["91004006", _content_formatted("旅程待定稿"), "2026-08-14 10:00:00"]]}},
        tid_b: {"last_page": 1, "floors": {"1": [
            ["91004003", _content_complete("旅程稿三"), "2026-08-12 10:00:00"],
            ["91004004", _content_complete("旅程旧稿"), "2026-07-01 10:00:00"]]}},
    }
    scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    shots = output / "ui-journey" / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)

    procs: list = []
    errors: list[str] = []
    blocked: list[str] = []
    entries: list[dict[str, Any]] = []
    from datetime import datetime, timedelta, timezone as _timezone

    _shanghai = _timezone(timedelta(hours=8))
    expected_upload_day = int(datetime(2026, 8, 20, 10, 0,
                                       tzinfo=_shanghai).timestamp())
    expected_pending_upload = int(datetime(2026, 8, 21, 9, 30,
                                           tzinfo=_shanghai).timestamp())
    try:
        proc, port = _spawn_operator_cmd(root, scenario_path, source)
        procs.append(proc)
        entries.append({"stage": "first", "pid": proc.pid, "port": port})
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_context(viewport={"width": 1366, "height": 768}).new_page()
                install_page_guards(page, errors, blocked)
                page.goto(base, wait_until="load")
                page.wait_for_selector("#main-nav", timeout=20000)
                # 副本向导（DOM）
                page.wait_for_selector("#source-wizard:not(.hidden)", timeout=20000)
                page.screenshot(path=str(shots / "01-wizard-1366.png"), full_page=True)
                with page.expect_navigation(wait_until="load", timeout=60000):
                    page.locator("#confirm-source-btn").click()
                page.wait_for_function(
                    "() => { const w = document.querySelector('#source-wizard');"
                    " const s = document.querySelector('#ws-summary');"
                    " return w && w.classList.contains('hidden') && s && s.textContent.length > 0; }",
                    timeout=30000)
                page.wait_for_timeout(500)
                state_dump = page.evaluate(
                    "() => ({url: location.href,"
                    " threads_page: document.querySelector('#threads-page') && document.querySelector('#threads-page').className,"
                    " add_panel: document.querySelector('#add-thread-panel') && document.querySelector('#add-thread-panel').className,"
                    " url_visible: !!(document.querySelector('#thread-url') && document.querySelector('#thread-url').offsetParent)})"
                )
                print("[ui-journey] state after wizard:", json.dumps(state_dump, ensure_ascii=False),
                      flush=True)
                checks.append(_check("wizard_bound_via_dom", True))
                ws_first = _http(port, "GET", "/api/operator/status")[1]["data"]["workspace_id"]
                (add_thread, init_thread, scan_thread, write_article, exit_app,
                 save_choices, read_choices_dom, open_item) = _journey_helpers(page, base)
                # 帖 A：page 起点 → 两篇
                add_thread(tid_a, "旅程A帖")
                init_thread(tid_a, "page", 1)
                scan_thread()
                page.screenshot(path=str(shots / "02-home-1366.png"), full_page=True)
                write_article("旅程稿一")
                write_article("旅程稿二")
                # 帖 B：date 起点（排除旧稿）→ 第三篇
                add_thread(tid_b, "旅程B帖")
                init_thread(tid_b, "date", "2026-08-01")
                scan_thread()
                write_article("旅程稿三")
                page.screenshot(path=str(shots / "03-review-1366.png"), full_page=True)
                # A068：保留未导出的多来源/非默认 choices 稿，供重开与恢复后回填比对
                a068_tags: list[str] = []
                pending_sources = ["https://example.com/旅程-原作一",
                                   "https://example.com/旅程-原作二"]
                save_choices("旅程待定稿", note="旅程备注-A068",
                             upload_local="2026-08-21T09:30", extract_format=True,
                             extra_original=pending_sources, write=False,
                             expect_choices=a068_tags)
                dom_expected = read_choices_dom()
                checks.append(_check("a068_expected_captured",
                                     dom_expected.get("note") == "旅程备注-A068"
                                     and dom_expected.get("content_mode") == "nga_ubb"
                                     and "加粗正文" in (dom_expected.get("formatted") or ""),
                                     dom_expected))
                checks.append(_check("a068_pending_choices_saved",
                                     bool(a068_tags), a068_tags))
                # 检查点（DOM）
                page.locator("#main-nav button", has_text="交付与恢复").click()
                page.wait_for_timeout(800)
                page.locator("#checkpoint-create-btn").click()
                wait_ck = False
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    text = page.locator("#checkpoints").text_content() or ""
                    if "revision=" in text:
                        wait_ck = True
                        break
                    page.wait_for_timeout(400)
                page.screenshot(path=str(shots / "04-delivery-1366.png"), full_page=True)
                checks.append(_check("checkpoint_created_dom", wait_ck))
                import hashlib as _hashlib

                def _region_fingerprint(path: Path) -> dict:
                    fingerprint = {}
                    if path.exists():
                        for child in sorted(path.rglob("*")):
                            if not child.is_file():
                                continue
                            if child.name.endswith((".lock", "-wal", "-shm")):
                                continue
                            key = str(child.relative_to(path))
                            try:
                                fingerprint[key] = _hashlib.sha256(
                                    child.read_bytes()).hexdigest()
                            except OSError:
                                stat = child.stat()
                                fingerprint[key] = (f"unreadable:{stat.st_size}:"
                                                    f"{int(stat.st_mtime_ns)}")
                    return fingerprint

                old_region_before = _region_fingerprint(root / "workspaces" / ws_first)
                # 正常退出并确认进程结束
                exit_app()
                try:
                    proc.wait(timeout=30)
                    exited = proc.poll() is not None
                except _subprocess.TimeoutExpired:
                    exited = False
                checks.append(_check("normal_exit_process_ended", exited))
                if not exited:
                    _kill_process_tree(proc)
            finally:
                browser.close()
        _kill_process_tree(proc)

        # 重开：同入口恢复原工作区
        proc2, port2 = _spawn_operator_cmd(root, scenario_path, source)
        procs.append(proc2)
        status2 = _http(port2, "GET", "/api/operator/status")[1]["data"]
        checks.append(_check("reopen_same_workspace",
                             status2["workspace_id"] == ws_first,
                             {"reopened": status2["workspace_id"], "first": ws_first}))
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_context(viewport={"width": 1366, "height": 768}).new_page()
                install_page_guards(page, errors, blocked)
                entries.append({"stage": "reopen", "pid": proc2.pid, "port": port2})
                page.goto(f"http://127.0.0.1:{port2}", wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=30000)
                (_, _, _, _, exit_app2, _,
                 read_choices_dom2, open_item2) = _journey_helpers(page, "")
                # A068：普通新进程重开后即比对（不是恢复后才开始）
                open_item2("旅程待定稿")
                dom_reopen = read_choices_dom2()
                checks.append(_check("a068_dom_after_reopen_matches",
                                     dom_reopen == dom_expected,
                                     {"expected": dom_expected, "actual": dom_reopen}))
                # 恢复（DOM，确认对话框）
                page.locator("#main-nav button", has_text="交付与恢复").click()
                page.wait_for_timeout(800)
                page.on("dialog", lambda d: d.accept())
                with page.expect_response(
                        lambda r: r.request.method == "POST" and "/restore" in r.url,
                        timeout=240000) as restore_info:
                    page.locator("#checkpoints button",
                                 has_text="恢复为新的工作区").first.click()
                restore_payload = (restore_info.value.json() or {}).get("data") or {}
                restored_id = restore_payload.get("new_workspace_id")
                checks.append(_check("restore_response_captured",
                                     bool(restored_id) and restored_id != ws_first,
                                     {"restored": restored_id, "first": ws_first}))
                wait_restore = False
                deadline = time.monotonic() + 180
                while time.monotonic() < deadline:
                    text = page.locator("#status-diagnostic").text_content() or ""
                    if "已恢复为新工作区" in text:
                        wait_restore = True
                        break
                    page.wait_for_timeout(500)
                checks.append(_check("restore_via_dom", wait_restore))
                exit_app2()
                try:
                    proc2.wait(timeout=30)
                except _subprocess.TimeoutExpired:
                    pass
            finally:
                browser.close()
        _kill_process_tree(proc2)

        # 追加第四篇楼层后再次经入口启动（应进入恢复后的新工作区）
        scenario[tid_a]["floors"]["1"].append(
            ["91004005", _content_complete("旅程稿四"), "2026-08-13 10:00:00"])
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
        proc3, port3 = _spawn_operator_cmd(root, scenario_path, source)
        procs.append(proc3)
        entries.append({"stage": "restored", "pid": proc3.pid, "port": port3})
        status3 = _http(port3, "GET", "/api/operator/status")[1]["data"]
        checks.append(_check("restored_workspace_loaded_via_cmd",
                             status3["workspace_id"] == restored_id
                             and restored_id != ws_first,
                             {"restored_from_response": restored_id,
                              "third_start": status3["workspace_id"],
                              "first": ws_first}))
        deliveries = _http(port3, "GET", "/api/operator/deliveries")[1]["data"]["deliveries"]
        checks.append(_check("restored_deliveries_preserved", len(deliveries) == 3,
                             {"ws": status3["workspace_id"], "deliveries": len(deliveries)}))
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_context(viewport={"width": 1366, "height": 768}).new_page()
                install_page_guards(page, errors, blocked)
                page.goto(f"http://127.0.0.1:{port3}", wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=30000)
                (add_thread, init_thread, scan_thread, write_article, _,
                 _, read_choices_dom, open_item) = _journey_helpers(page, "")
                # 恢复后继续扫描（帖 A 的游标/配置来自检查点）
                page.locator("#main-nav button", has_text="帖子与扫描").click()
                page.wait_for_selector("#thread-entries .thread-entry", timeout=15000)
                page.locator("#thread-entries .thread-entry", has_text=tid_a).click(
                    position={"x": 8, "y": 8})
                scan_thread()
                write_article("旅程稿四")
                # A068：恢复新区后逐项比对（与保存时预期、普通重开值三方一致）
                open_item("旅程待定稿")
                dom_after_restore = read_choices_dom()
                checks.append(_check("a068_dom_after_restore_matches",
                                     dom_after_restore == dom_expected
                                     == dom_reopen,
                                     {"expected": dom_expected,
                                      "reopen": dom_reopen,
                                      "restored": dom_after_restore}))
                items3 = _http(port3, "GET",
                               "/api/operator/items?page=1&page_size=50")[1]["data"]["items"]
                pending_summary = next((row for row in items3
                                        if row.get("title") == "旅程待定稿"), None)
                checks.append(_check("a068_pending_item_present",
                                     pending_summary is not None))
                pending_detail = _http(
                    port3, "GET",
                    f"/api/operator/items/{pending_summary['id']}")[1]["data"]
                pending_choices = pending_detail.get("choices") or {}
                expected_nga = (f"https://bbs.nga.cn/read.php?tid={tid_a}"
                                "&pid=91004006")
                expected_order = [expected_nga] + pending_sources
                checks.append(_check(
                    "a068_choices_after_restore_http",
                    pending_detail.get("status") != "exported"
                    and pending_choices.get("note") == "旅程备注-A068"
                    and pending_choices.get("upload_time_seconds")
                    == expected_pending_upload
                    and pending_choices.get("content_mode") == "nga_ubb"
                    and "加粗正文" in (pending_choices.get("formatted_body") or "")
                    and list(pending_choices.get("ordered_sources") or [])
                    == expected_order,
                    {"note": pending_choices.get("note"),
                     "upload": pending_choices.get("upload_time_seconds"),
                     "mode": pending_choices.get("content_mode"),
                     "ordered_sources": pending_choices.get("ordered_sources"),
                     "status": pending_detail.get("status")}))
                open_item("旅程稿一")
                open_item("旅程待定稿")
                dom_second = read_choices_dom()
                page.reload(wait_until="load")
                page.wait_for_selector("#thread-entries .thread-entry", timeout=30000)
                open_item("旅程待定稿")
                dom_third = read_choices_dom()
                checks.append(_check(
                    "a068_dom_backfill_stable",
                    dom_expected == dom_after_restore == dom_second == dom_third,
                    {"expected": dom_expected, "restored": dom_after_restore,
                     "switch_back": dom_second, "reload": dom_third}))
                # 1024×768 截图（首页/审核）
                ctx2 = browser.new_context(viewport={"width": 1024, "height": 768})
                page2 = ctx2.new_page()
                install_page_guards(page2, errors, blocked)
                page2.goto(f"http://127.0.0.1:{port3}", wait_until="load")
                page2.wait_for_selector("#thread-entries .thread-entry", timeout=30000)
                page2.screenshot(path=str(shots / "05-home-1024.png"), full_page=True)
                page2.locator("#main-nav button", has_text="待审稿件").click()
                page2.wait_for_selector("#items-body tr", timeout=20000)
                page2.screenshot(path=str(shots / "06-review-list-1024.png"), full_page=True)
                page2.locator("#items-body tr").first.click()
                page2.wait_for_selector("#review-form [data-field=title]", timeout=20000)
                page2.screenshot(path=str(shots / "07-review-form-1024.png"), full_page=True)
                page2.locator("#main-nav button", has_text="交付与恢复").click()
                page2.wait_for_timeout(800)
                page2.screenshot(path=str(shots / "08-delivery-1024.png"), full_page=True)
                overflow = page2.evaluate(
                    "() => document.documentElement.scrollWidth > window.innerWidth + 1")
                checks.append(_check("no_horizontal_overflow_1024", not overflow))
                ctx2.close()
                checks.append(_check("no_page_errors", errors == [], errors[:2]))
                checks.append(_check("no_external_requests", blocked == [], blocked[:2]))
            finally:
                browser.close()
        # 精确断言：四篇累计 + 逐篇最终字段/来源/标签 + 旧区不变 + 日期游标保留
        deliveries = _http(port3, "GET", "/api/operator/deliveries")[1]["data"]["deliveries"]
        checks.append(_check("four_cumulative", len(deliveries) == 4,
                             [d["count_after"] for d in deliveries]))
        latest = deliveries[-1]
        candidate_path = (root / "workspaces" / restored_id / "candidates"
                          / latest["candidate_file_name"])
        import sqlite3 as _sqlite3

        expected_articles = {
            "旅程稿一": (tid_a, "91004001"),
            "旅程稿二": (tid_a, "91004002"),
            "旅程稿三": (tid_b, "91004003"),
            "旅程稿四": (tid_a, "91004005"),
        }
        base_conn = _sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            base_tag = base_conn.execute(
                "SELECT type FROM tag WHERE name = ?", ("演示标签",)).fetchone()
        finally:
            base_conn.close()
        expected_tag_type = base_tag[0] if base_tag else 0
        conn = _sqlite3.connect(candidate_path.resolve().as_uri() + "?mode=ro",
                                uri=True)
        try:
            per_article = []
            exact_ok = True
            for title, (tid, pid) in expected_articles.items():
                rows = conn.execute(
                    "SELECT id, name, note, content, author, translator, uploadTime,"
                    " source FROM article WHERE name = ?", (title,)).fetchall()
                expected_source = f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"
                expected_row = (title, None, f"<p>正文：{title}</p>", "旅程作者",
                                None, expected_upload_day, expected_source)
                match = len(rows) == 1 and tuple(rows[0][1:]) == expected_row
                tag_rows = []
                if len(rows) == 1:
                    tag_rows = conn.execute(
                        "SELECT t.name, t.type FROM tagged g JOIN tag t ON t.id = g.tagId"
                        " WHERE g.artId = ?", (rows[0][0],)).fetchall()
                tag_ok = tag_rows == [("演示标签", expected_tag_type)]
                exact_ok = exact_ok and match and tag_ok
                per_article.append({"title": title, "match": match,
                                    "tag_ok": tag_ok,
                                    "actual": list(rows[0]) if rows else None})
            checks.append(_check("all_four_sources_exact", exact_ok, per_article))
        finally:
            conn.close()
        old_region_after = _region_fingerprint(root / "workspaces" / ws_first)
        checks.append(_check("old_region_unchanged_after_fourth",
                             old_region_after == old_region_before,
                             {"changed": sorted(set(old_region_after.items())
                                                ^ set(old_region_before.items()))[:4]}))
        checks.append(_check("entry_process_records",
                             len(entries) == 3
                             and all(entry.get("pid") and entry.get("port")
                                     for entry in entries),
                             entries))
        thread_b = _http(port3, "GET", f"/api/operator/threads/{tid_b}/status")[1]["data"]
        checks.append(_check("date_start_cursor_preserved",
                             thread_b["start"]["mode"] == "date"
                             and thread_b["start"]["date"] == "2026-08-01",
                             thread_b["start"]))
        page_end_proc = [pr for pr in procs if pr.poll() is None]
        checks.append(_check("journey_process_running", len(page_end_proc) == 1))
    finally:
        for proc in procs:
            _kill_process_tree(proc)
    exited_after_cleanup = all(pr.poll() is not None for pr in procs)
    checks.append(_check("entry_processes_exited_after_cleanup",
                         exited_after_cleanup,
                         [pr.pid for pr in procs if pr.poll() is None]))

    summary = {"phase": "ui-journey", "checks": checks,
               "overall": "PASS" if all(c["ok"] for c in checks) else "FAIL"}
    _write(output / "ui-journey" / "summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -B tools/operator_acceptance.py")
    parser.add_argument("--phase", required=True,
                        choices=("launch", "threads", "scan", "review", "writer",
                                 "recovery", "real-copy", "ui", "lifecycle", "ui-journey", "all"))
    parser.add_argument("--output", required=True, type=Path,
                        help="新证据目录（已存在且非空则拒绝）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        print(f"错误: 证据目录已存在且非空: {args.output}", file=sys.stderr)
        return 2
    args.output.mkdir(parents=True, exist_ok=True)
    phases = {
        "launch": run_phase_launch,
        "threads": lambda o: run_phase_threads_scan(o),
        "scan": lambda o: run_phase_threads_scan(o),
        "review": lambda o: run_phase_review_writer(o),
        "writer": lambda o: run_phase_review_writer(o),
        "recovery": run_phase_recovery,
        "real-copy": run_phase_real_copy,
        "ui": run_phase_ui,
        "lifecycle": run_phase_lifecycle,
        "ui-journey": run_phase_ui_journey,
    }
    selected = list(phases) if args.phase == "all" else [args.phase]
    results = {}
    failed = False
    for phase in selected:
        print(f"[operator_acceptance] {phase} …", flush=True)
        try:
            results[phase] = phases[phase](args.output)
        except Exception as exc:
            import traceback

            traceback.print_exc()
            results[phase] = {"phase": phase, "overall": "FAIL",
                              "reason": f"{type(exc).__name__}: {exc}"}
        print(f"[operator_acceptance] {phase}: {results[phase].get('overall')}", flush=True)
        if results[phase].get("overall") != "PASS":
            failed = True
    _write(args.output / "all-summary.json", {"phases": results,
                                              "overall": "FAIL" if failed else "PASS"})
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
