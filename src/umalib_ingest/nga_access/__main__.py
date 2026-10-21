"""NGA authenticated-read PoC CLI (TASK-010).

--help and inspect-snapshot never import Playwright and never touch the
network. probe is the only network-capable command and it is strictly
bounded to the fixed target thread.

Console entry points (nga-probe / nga-inspect-snapshot) map directly to
probe_main / inspect_snapshot_main, which parse their own arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from umalib_ingest.nga_access import snapshot
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.urls import MAX_PAGES, MIN_NAVIGATION_DELAY_SECONDS
from umalib_ingest.local_paths import nga_profile_dir, nga_snapshot_dir

_DEFAULT_THREAD_URL = "https://bbs.nga.cn/read.php?tid=30099686"
_DEFAULT_PROFILE_DIR = str(nga_profile_dir())
_DEFAULT_SNAPSHOT_DIR = str(nga_snapshot_dir())


def _build_probe_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nga-probe",
        description="NGA 登录态只读访问 PoC（固定目标帖、低频、无写操作）",
    )
    parser.add_argument("--thread-url", default=_DEFAULT_THREAD_URL, help="目标帖 URL")
    parser.add_argument("--profile-dir", default=_DEFAULT_PROFILE_DIR, help="独立 profile 目录")
    parser.add_argument("--snapshot-dir", default=_DEFAULT_SNAPSHOT_DIR, help="快照输出目录")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=2,
        choices=[1, 2],
        help=f"最多读取页数（默认 2，上限 {MAX_PAGES}）",
    )
    parser.add_argument(
        "--navigation-delay-seconds",
        type=float,
        default=MIN_NAVIGATION_DELAY_SECONDS,
        help=f"两次帖子导航最小间隔秒数（默认 {MIN_NAVIGATION_DELAY_SECONDS}）",
    )
    parser.add_argument("--no-json-probe", action="store_true", help="禁止可选 JSON 观察")
    parser.add_argument(
        "--login-wait-file",
        default=None,
        help="非交互启动时等待该文件出现作为登录确认信号（内容不读取）",
    )
    return parser


def _build_inspect_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nga-inspect-snapshot",
        description="完全离线检查 NGA PoC 快照目录",
    )
    parser.add_argument("--snapshot-dir", default=_DEFAULT_SNAPSHOT_DIR, help="快照目录")
    return parser


def inspect_snapshot_main(argv: list[str] | None = None) -> int:
    """Fully offline snapshot inspection; never imports Playwright."""
    args = _build_inspect_parser().parse_args(argv)
    snapshot_dir = Path(args.snapshot_dir)
    if not snapshot_dir.exists() or not snapshot_dir.is_dir():
        print(f"错误: 快照目录不存在: {snapshot_dir}", file=sys.stderr)
        return 1
    manifest = snapshot_dir / "probe-result.json"
    if not manifest.exists():
        print("错误: 未找到 probe-result.json", file=sys.stderr)
        return 1
    data = json.loads(manifest.read_text(encoding="utf-8"))
    print(f"tid: {data.get('tid')}")
    for page in data.get("pages", []):
        print(
            f"  page-{page.get('page_label')}: status={page.get('status')} "
            f"body_sha256={page.get('body_sha256')} "
            f"floors={page.get('floor_count')} pids={page.get('unique_pid_count')} "
            f"encoding={page.get('encoding')} "
            f"snapshot={page.get('snapshot_status')} "
            f"diagnostics={','.join(page.get('diagnostics', []))}"
        )
    print(f"json_observation: {data.get('json_observation')}")
    print(f"diagnostics: {','.join(data.get('diagnostics', []))}")
    return 0


def probe_main(argv: list[str] | None = None) -> int:
    """Run the authenticated read probe (console entry point)."""
    parser = _build_probe_parser()
    args = parser.parse_args(argv)
    if args.navigation_delay_seconds < MIN_NAVIGATION_DELAY_SECONDS:
        print(
            f"错误: navigation-delay-seconds 不得低于 {MIN_NAVIGATION_DELAY_SECONDS}",
            file=sys.stderr,
        )
        return 2
    from umalib_ingest.nga_access.probe import run_probe

    try:
        result = run_probe(
            args.thread_url,
            args.profile_dir,
            args.snapshot_dir,
            max_pages=args.max_pages,
            navigation_delay_seconds=args.navigation_delay_seconds,
            json_probe=not args.no_json_probe,
            login_wait_file=args.login_wait_file,
        )
    except NgaAccessError as exc:
        print(f"错误[{exc.code}]: {exc.message}", file=sys.stderr)
        return 1
    except Exception:
        print("错误: 探测失败（详细信息请查看安全日志）", file=sys.stderr)
        return 1
    for page in result.pages:
        print(
            f"page-{page.page_label}: status={page.status} "
            f"body_sha256={page.body_sha256} floors={page.floor_count} "
            f"pids={page.unique_pid_count} snapshot={page.snapshot_status}"
        )
    print(f"json_observation: {result.json_observation}")
    print(f"diagnostics: {','.join(result.diagnostics)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch by first positional argument (probe / inspect-snapshot)."""
    if argv is None:
        argv = sys.argv[1:]
    if argv in (["--help"], ["-h"]):
        print("用法: python -m umalib_ingest.nga_access {probe|inspect-snapshot} [选项]")
        print("子命令:")
        print("  probe              有头浏览器人工登录态读取（固定目标帖、低频、无写操作）")
        print("  inspect-snapshot   完全离线检查快照目录")
        print("每个子命令可用 --help 查看详细选项。")
        return 0
    if argv and argv[0] == "probe":
        return probe_main(argv[1:])
    if argv and argv[0] == "inspect-snapshot":
        return inspect_snapshot_main(argv[1:])
    print("用法: python -m umalib_ingest.nga_access {probe|inspect-snapshot} [选项]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
