"""Watcher CLI: baseline-from-snapshot / scan / status (TASK-011)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.local_paths import nga_profile_dir, nga_snapshot_dir, nga_state_file
from umalib_ingest.watcher.result import WatcherRunResult
from umalib_ingest.watcher.state import (
    WatcherConfig,
    load_state,
    state_file_sha256,
)

_DEFAULT_STATE_FILE = str(nga_state_file())
_DEFAULT_SNAPSHOT_DIR = str(nga_snapshot_dir())
_DEFAULT_PROFILE_DIR = str(nga_profile_dir())


def _base_parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=description)


def _build_baseline_parser() -> argparse.ArgumentParser:
    parser = _base_parser(
        "nga-watch baseline-from-snapshot",
        "从现有离线快照建立 Watcher 首次基线（不联网、不导入历史）",
    )
    parser.add_argument("--state-file", default=_DEFAULT_STATE_FILE, help="state 文件路径")
    parser.add_argument("--snapshot-dir", default=_DEFAULT_SNAPSHOT_DIR, help="快照目录")
    return parser


def _build_status_parser() -> argparse.ArgumentParser:
    parser = _base_parser("nga-watch status", "只读查看 Watcher 状态摘要")
    parser.add_argument("--state-file", default=_DEFAULT_STATE_FILE, help="state 文件路径")
    return parser


def _build_scan_parser() -> argparse.ArgumentParser:
    parser = _base_parser("nga-watch scan", "扫描固定目标帖新页并导入 Inbox（在线）")
    parser.add_argument("--state-file", default=_DEFAULT_STATE_FILE, help="state 文件路径")
    parser.add_argument(
        "--inbox-db", required=True, help="Inbox SQLite 路径（必填；不存在则初始化）"
    )
    parser.add_argument("--profile-dir", default=_DEFAULT_PROFILE_DIR, help="独立登录 profile 目录")
    parser.add_argument("--snapshot-dir", default=_DEFAULT_SNAPSHOT_DIR, help="快照输出目录")
    parser.add_argument("--navigation-delay-seconds", type=float, default=3.0, help="导航间隔（≥3）")
    parser.add_argument("--max-scan-pages", type=int, default=12, help="单次最大扫描页数（1..25）")
    parser.add_argument(
        "--login-wait-file",
        default=None,
        help="非交互登录确认信号文件（不读取内容）",
    )
    parser.add_argument(
        "--login-timeout-seconds",
        type=float,
        default=300.0,
        help="登录确认等待超时秒数（默认 300）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只读页面并返回计划，不写 Inbox/state")
    return parser


def baseline_from_snapshot_main(argv: list[str] | None = None) -> int:
    """Create the offline first baseline; never imports Playwright/network."""
    args = _build_baseline_parser().parse_args(argv)
    from umalib_ingest.watcher.baseline import baseline_from_snapshot
    from umalib_ingest.watcher.scan import _utc_clock

    config = WatcherConfig(state_file=args.state_file, snapshot_dir=args.snapshot_dir)
    try:
        result = baseline_from_snapshot(config, clock=_utc_clock)
    except NgaAccessError as exc:
        print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
        return _exit_code(exc)
    _print_result(result)
    return 0


def status_main(argv: list[str] | None = None) -> int:
    """Read-only status; no network, no Inbox, no Playwright."""
    args = _build_status_parser().parse_args(argv)
    try:
        state = load_state(Path(args.state_file))
        sha = state_file_sha256(Path(args.state_file))
    except NgaAccessError as exc:
        print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
        return _exit_code(exc)
    trigger_count = sum(1 for f in state.known_floors.values() if f.trigger_found)
    import_count = sum(1 for f in state.known_floors.values() if f.inbox_item_id is not None)
    changed_count = sum(1 for f in state.known_floors.values() if f.changed_after_import)
    print(f"tid: {state.tid}")
    print(f"last_page: {state.last_page}")
    print(f"known_floors: {len(state.known_floors)}")
    print(f"trigger_found: {trigger_count}")
    print(f"imported: {import_count}")
    print(f"changed_after_import: {changed_count}")
    print(f"baseline_created_at: {state.baseline_created_at}")
    print(f"last_successful_scan_at: {state.last_successful_scan_at or '（无）'}")
    print(f"state_sha256: {sha}")
    return 0


def scan_main(argv: list[str] | None = None) -> int:
    """Run the scan (online capability; tests use fake sessions)."""
    args = _build_scan_parser().parse_args(argv)
    if args.login_timeout_seconds <= 0:
        print("错误: login-timeout-seconds 必须是正数", file=sys.stderr)
        return 2
    config = WatcherConfig(
        state_file=args.state_file,
        profile_dir=args.profile_dir,
        snapshot_dir=args.snapshot_dir,
        inbox_db=args.inbox_db,
        navigation_delay_seconds=args.navigation_delay_seconds,
        max_scan_pages=args.max_scan_pages,
    )
    from umalib_ingest.watcher.scan import run_scan

    try:
        result = run_scan(
            config,
            dry_run=args.dry_run,
            login_wait_file=args.login_wait_file,
            login_wait_timeout_seconds=args.login_timeout_seconds,
        )
    except NgaAccessError as exc:
        print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
        return _exit_code(exc)
    except Exception:
        print("错误: scan 失败（详细信息请查看安全日志）", file=sys.stderr)
        return 1
    _print_result(result)
    return 0


def _exit_code(exc: NgaAccessError) -> int:
    if exc.code in ("login_required", "login_not_confirmed", "manual_login_required"):
        return 2
    if exc.code == "watcher_inbox_required":
        return 2
    if exc.code in (
        "watcher_state_invalid",
        "watcher_state_changed",
        "watcher_locked",
        "watcher_state_exists",
        "baseline_snapshot_invalid",
        "thread_page_regressed",
        "scan_range_too_large",
        "page_fetch_failed",
        "duplicate_pid_across_pages",
        "state_import_recovery_ambiguous",
        "watcher_state_missing",
    ):
        return 3
    return 1


def _print_result(result: WatcherRunResult) -> None:
    print(f"mode: {result.mode}")
    print(f"tid: {result.tid}")
    print(f"pages: {result.previous_last_page or '-'} -> {result.current_last_page or '-'}")
    print(f"scanned_pages: {','.join(str(p) for p in result.scanned_pages) or '-'}")
    print(
        f"floors: new={result.new_floor_count} unchanged={result.unchanged_floor_count} "
        f"changed={result.changed_floor_count}"
    )
    print(f"discussion: {result.discussion_count}")
    print(f"triggered: {result.triggered_count}")
    print(f"imported: {','.join(f'#{i.item_id}({i.status})' for i in result.imported_items) or '-'}")
    print(
        "changed_after_import: "
        + (",".join(str(i) for i in result.changed_after_import) or "-")
    )
    print(f"state_before_sha256: {result.state_before_sha256 or '-'}")
    print(f"state_after_sha256: {result.state_after_sha256 or '-'}")
    print(f"dry_run: {result.dry_run}")
    print(f"diagnostics: {','.join(result.diagnostics) or '-'}")


def main(argv: list[str] | None = None) -> int:
    """Dispatch by subcommand; console entry point 'nga-watch'."""
    if argv is None:
        argv = sys.argv[1:]
    if argv in (["--help"], ["-h"]):
        print("用法: nga-watch {baseline-from-snapshot|scan|status} [选项]")
        print("子命令 --help 查看详细选项。")
        return 0
    if argv and argv[0] == "baseline-from-snapshot":
        return baseline_from_snapshot_main(argv[1:])
    if argv and argv[0] == "scan":
        return scan_main(argv[1:])
    if argv and argv[0] == "status":
        return status_main(argv[1:])
    print("用法: nga-watch {baseline-from-snapshot|scan|status} [选项]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
