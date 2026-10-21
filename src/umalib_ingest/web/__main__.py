"""CLI entry point for the local review UI (TASK-004, extended by TASK-009)."""

from __future__ import annotations

import argparse
import sys

from umalib_ingest.web import run


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m umalib_ingest.web",
        description="本地投稿审核台（只监听 127.0.0.1）",
    )
    parser.add_argument("--db", required=True, help="Inbox SQLite 路径（必填）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口 0-65535（默认 8765）")
    parser.add_argument(
        "--init",
        action="store_true",
        help="数据库路径不存在时创建并初始化 Inbox",
    )
    parser.add_argument(
        "--umalib-db",
        default=None,
        help="启用 Writer：UmaLib 源数据库副本路径（只读，必须与 --candidate-dir 同时提供）",
    )
    parser.add_argument(
        "--candidate-dir",
        default=None,
        help="启用 Writer：候选输出目录（必须已存在，必须与 --umalib-db 同时提供）",
    )
    parser.add_argument(
        "--watcher-state",
        default=None,
        help="启用 Watcher：state 文件路径（必须与 --watcher-profile/--watcher-snapshot-dir 同时提供）",
    )
    parser.add_argument(
        "--watcher-profile",
        default=None,
        help="启用 Watcher：独立登录 profile 目录",
    )
    parser.add_argument(
        "--watcher-snapshot-dir",
        default=None,
        help="启用 Watcher：快照输出目录",
    )
    parser.add_argument(
        "--watcher-login-timeout-seconds",
        type=float,
        default=300.0,
        help="Watcher 登录确认等待超时秒数（默认 300）",
    )
    parser.add_argument(
        "--watcher-max-scan-pages",
        type=int,
        default=12,
        help="Watcher 单次最大扫描页数（1..25，默认 12）",
    )
    args = parser.parse_args()
    if args.port < 0 or args.port > 65535:
        print(f"错误: 非法端口: {args.port}（允许 0-65535）", file=sys.stderr)
        raise SystemExit(2)
    run(
        db_path=args.db,
        port=args.port,
        initialize=args.init,
        umalib_db=args.umalib_db,
        candidate_dir=args.candidate_dir,
        watcher_state=args.watcher_state,
        watcher_profile=args.watcher_profile,
        watcher_snapshot_dir=args.watcher_snapshot_dir,
        watcher_login_timeout_seconds=args.watcher_login_timeout_seconds,
        watcher_max_scan_pages=args.watcher_max_scan_pages,
    )


if __name__ == "__main__":
    main()
