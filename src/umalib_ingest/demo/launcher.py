"""TASK-013 启动器：工作区创建/恢复 + 本地服务 + 可选浏览器（M01/M11）。"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from umalib_ingest.local_paths import nga_profile_dir, private_state_root, source_database_candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m umalib_ingest.demo",
        description="本地大书库录入 Demo（synthetic / real_copy 工作区）",
    )
    parser.add_argument("--mode", choices=("synthetic", "real_copy"),
                        default="synthetic",
                        help="工作区模式：synthetic 使用内置合成数据；real_copy 只读绑定 main-copy.db")
    parser.add_argument("--workspace", default=None,
                        help="打开指定工作区 UUID（与 --new-workspace 互斥）")
    parser.add_argument("--new-workspace", action="store_true",
                        help="新建一个工作区")
    parser.add_argument("--name", default=None, help="新工作区名称")
    parser.add_argument("--no-browser", action="store_true",
                        help="只启动服务，不打开浏览器")
    parser.add_argument("--port", type=int, default=0,
                        help="监听端口（0 表示随机空闲端口）")
    parser.add_argument("--enable-nga", action="store_true",
                        help="real_copy 模式下启用真实 NGA 读取（需现场授权）")
    parser.add_argument("--check", action="store_true",
                        help="只检查环境并退出，不创建工作区/服务")
    return parser


def check_environment() -> dict[str, Any]:
    """Read-only environment check; never creates a workspace or browser."""
    import platform

    report = {
        "python": platform.python_version(),
        "umalib_ingest": "OK",
        "demo_root": str(default_root()),
        "playwright": "OK",
    }
    try:
        import umalib_ingest.demo  # noqa: F401
    except Exception as exc:
        report["umalib_ingest"] = f"BLOCKED: {exc}"
    import importlib.util

    if importlib.util.find_spec("playwright") is None:
        report["playwright"] = "unavailable（离线审核不受影响）"
    return report


def default_root() -> Path:
    import os

    env = os.environ.get("UMALIB_DEMO_ROOT")
    if env:
        return Path(env)
    return private_state_root() / "demo-user"


def real_copy_source() -> Path:
    """The fixed read-only main-copy source for real_copy workspaces."""
    return source_database_candidate()


def _resolve_workspace_id(args: argparse.Namespace, store, mode: str) -> str:
    """Choose continue/new per CLI args and launcher memory (no secrets)."""
    from umalib_ingest.workspace.errors import WorkspaceNotFoundError

    if args.workspace and args.new_workspace:
        print("错误: --workspace 与 --new-workspace 互斥", file=sys.stderr)
        raise SystemExit(2)
    if args.workspace:
        try:
            store.load(args.workspace)
        except WorkspaceNotFoundError:
            print(f"错误: 工作区不存在: {args.workspace}", file=sys.stderr)
            raise SystemExit(2)
        return args.workspace
    if args.new_workspace:
        return ""
    remembered = store.last_workspace(mode)
    if remembered:
        try:
            store.load(remembered)
            return remembered
        except WorkspaceNotFoundError:
            pass
    return ""


def launch_demo(args: argparse.Namespace) -> int:
    """Create/recover the workspace, start the server, optionally open a browser."""
    from umalib_ingest.handoff import ApprovedPackageService
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.review import ReviewService
    from umalib_ingest.web.app import create_server
    from umalib_ingest.web.demo_ui import DemoUiService
    from umalib_ingest.web.writer_ui import WriterUiService
    from umalib_ingest.workspace.checkpoints import CheckpointService
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import InstanceLock, WorkspaceStore

    if args.mode == "synthetic" and args.enable_nga:
        print("错误: synthetic 模式不允许 --enable-nga", file=sys.stderr)
        return 2
    root = default_root()
    paths = WorkspacePaths(root)
    paths.ensure_root()
    store = WorkspaceStore(paths)
    workspace_id = _resolve_workspace_id(args, store, args.mode)
    coordinator = MaintenanceCoordinator()
    service = WorkspaceService(
        paths, store,
        real_copy_source() if args.mode == "real_copy" else None,
        coordinator,
    )
    if workspace_id == "":
        manifest = service.create(args.mode, args.name or
                                  ("演示工作区" if args.mode == "synthetic" else "实际副本工作区"))
        workspace_id = manifest.workspace_id
        store.remember_last(args.mode, workspace_id)
    else:
        manifest = store.load(workspace_id)
    lock = InstanceLock(paths.lock_file(workspace_id))
    lock.acquire()
    try:
        repository = InboxRepository(paths.inbox_db(workspace_id))
        writer_service = WriterUiService(
            repository, None, threading.RLock(), workspace_service=service,
        )
        nga_setup = None
        if args.mode == "real_copy" and args.enable_nga:
            from umalib_ingest.demo.nga_setup import NgaSetup

            nga_setup = NgaSetup(
                workspace_id,
                state_file=paths.watcher_state(workspace_id),
                snapshot_dir=paths.watcher_snapshots(workspace_id),
                profile_dir=nga_profile_dir(),
                inbox_db=paths.inbox_db(workspace_id),
            )
        from umalib_ingest.demo.simulated_scan import run_simulated_scan

        demo_service = DemoUiService(
            service, workspace_id, repository,
            ReviewService(repository), writer_service,
            nga_setup=nga_setup,
            simulated_scan_fn=run_simulated_scan,
        )
        checkpoints = CheckpointService(paths, store, coordinator, None)
        demo_service.set_checkpoints(checkpoints)
        try:
            server = create_server(repository, port=args.port)
        except OSError as exc:
            print(f"错误: 无法监听端口 {args.port}（可能已被占用）: {exc}",
                  file=sys.stderr)
            return 2
        from umalib_ingest.web.app import bind_demo_service

        bind_demo_service(server, demo_service)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"大书库录入助手已启动: {url}", flush=True)
        print(f"工作区: {manifest.name}（{manifest.mode}） revision={manifest.revision}",
              flush=True)
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("已停止", flush=True)
        finally:
            server.server_close()
    finally:
        lock.release()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.check:
        for key, value in check_environment().items():
            print(f"  {key}: {value}")
        return 0
    return launch_demo(args)
