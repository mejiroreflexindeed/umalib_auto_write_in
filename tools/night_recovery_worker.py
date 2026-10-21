"""TASK-012B W4：可终止的恢复测试 worker（只属于本次测试的登记子进程）。

由 tools/night_recovery.py 或 test_watcher_recovery.py 启动；按 --mode 在确定
中断点写入 READY marker 后永久阻塞，等待父进程终止。不使用网络、不访问 profile、
不修改任何现有数据库；state/inbox 全部位于父进程指定的临时工作区。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from umalib_ingest.nga_access.errors import NgaAccessError  # noqa: E402
from umalib_ingest.nga_access.fetch import FetchedThreadPage  # noqa: E402
from umalib_ingest.nga_access.parsing import FloorSnapshot  # noqa: E402
from umalib_ingest.watcher import scan as scan_module  # noqa: E402
from umalib_ingest.watcher.scan import run_scan  # noqa: E402
from umalib_ingest.watcher.state import WatcherConfig  # noqa: E402

TID = 30099686
CONTENT = "[收录]\n[标题] 标题甲\n[作者] 作者甲\n[标签] 标签甲\n\n正文甲。"


class _FakeSession:
    def close(self):
        pass


def _page(page: int, last_page: int, *, login: bool = False,
          floors: tuple = ()) -> FetchedThreadPage:
    return FetchedThreadPage(
        tid=TID, page=page, last_page=last_page, status=200,
        final_url=f"https://bbs.nga.cn/read.php?tid={TID}&page={page}",
        content_type="text/html; charset=utf-8", encoding="utf-8",
        body_sha256="ab" * 32, body_bytes=0, snapshot_outcome="written",
        floors=floors,
        diagnostics=("manual_login_required",) if login else (),
    )


def _floor(pid: str, content: str) -> FloorSnapshot:
    return FloorSnapshot(
        pid=pid, floor_number=None, author_present=True,
        posted_at_raw="2026-09-02 10:00", edited_at_raw=None,
        content_format="ubbcode", content_text=content, content_raw_fragment=content,
    )


def _block(marker: Path) -> None:
    marker.write_text("READY", encoding="utf-8")
    while True:
        time.sleep(1)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -B tools/night_recovery_worker.py")
    parser.add_argument("--mode", choices=("point2", "point3", "point9", "point10"), required=True)
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--inbox-db", required=True)
    parser.add_argument("--work-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    marker = work / "ready.marker"
    config = WatcherConfig(
        state_file=Path(args.state_file),
        profile_dir=work / "profile",
        snapshot_dir=work / "snaps",
        inbox_db=Path(args.inbox_db),
        navigation_delay_seconds=3.0,
        max_scan_pages=12,
    )
    good_page = _page(173, 173, floors=(
        _floor("910009001", CONTENT), _floor("910009002", CONTENT),
    ))

    if args.mode == "point2":
        def fetch_fn(session, tid, page, snapshot_dir):
            if page == 1:
                return _page(1, 173)
            _block(marker)
            raise NgaAccessError("page_fetch_failed", "unreachable")

        run_scan(config, dry_run=False, session_factory=_FakeSession,
                 fetch_fn=fetch_fn, sleep=lambda s: None)
        return 0

    if args.mode == "point3":
        def fetch_fn(session, tid, page, snapshot_dir):
            return _page(1, 173, login=True)

        def confirm_login():
            _block(marker)

        run_scan(config, dry_run=False, session_factory=_FakeSession,
                 fetch_fn=fetch_fn, sleep=lambda s: None, confirm_login=confirm_login)
        return 0

    if args.mode in ("point9", "point10"):
        def fetch_fn(session, tid, page, snapshot_dir):
            if page == 1:
                return _page(1, 173)
            return good_page

        original_import = scan_module._import_floor
        count = {"n": 0}

        def wrapped_import(repo, tid, floor, diagnostics):
            item_id, status = original_import(repo, tid, floor, diagnostics)
            count["n"] += 1
            if count["n"] == (1 if args.mode == "point9" else 2):
                _block(marker)
            return item_id, status

        with mock.patch.object(scan_module, "_import_floor", wrapped_import):
            run_scan(config, dry_run=False, session_factory=_FakeSession,
                     fetch_fn=fetch_fn, sleep=lambda s: None)
        return 0

    print(f"错误: 未知 mode {args.mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
