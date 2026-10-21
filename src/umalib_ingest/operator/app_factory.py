"""TASK-014 W01/W02：应用装配工厂与源库校验/选择。"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umalib_ingest.local_paths import nga_profile_dir, source_database_candidate


def verify_source_path(path: Path) -> dict[str, Any]:
    """校验一个本地 .db/.sqlite 源副本；只读，不创建文件。"""
    from umalib_ingest.umalib_writer import load_target_tag_catalog
    from umalib_ingest.umalib_writer.file_state import validate_source_path

    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_absolute():
        raise ValueError("源路径必须是绝对路径")
    if path.suffix.lower() not in (".db", ".sqlite"):
        raise ValueError("源文件必须以 .db 或 .sqlite 结尾")
    validate_source_path(path)
    # 拒绝活动 WAL/SHM
    for suffix in ("-wal", "-shm"):
        if Path(str(path) + suffix).exists():
            raise ValueError("源库存在活动的 WAL/SHM 日志，请先关闭大书库并使用完整副本")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog = load_target_tag_catalog(path)
    return {
        "source_id": "sha256:" + sha,
        "path": str(path.resolve()),
        "file_name": path.name,
        "sha256": sha,
        "size": path.stat().st_size,
        "schema_fingerprint_sha256": catalog.schema_fingerprint_sha256,
    }


def detect_main_copy() -> Path | None:
    candidate = source_database_candidate()
    if candidate.exists():
        try:
            verify_source_path(candidate)
            return candidate
        except Exception:
            return None
    return None


@dataclass
class OperatorContext:
    server: Any
    service: Any
    repository: Any
    review_service: Any
    writer_service: Any
    workspace_service: Any
    checkpoint_service: Any
    shutdown: Any

    _serving: bool = False

    def serve_forever(self) -> None:
        self._serving = True
        try:
            self.server.serve_forever()
        finally:
            self._serving = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def create_operator_app(
    *,
    root: Path,
    workspace_id: str | None = None,
    new_workspace: bool = False,
    name: str | None = None,
    port: int = 0,
    session_factory=None,
    nav_delay: float = 3.0,
) -> OperatorContext:
    """一次性装配并返回 OperatorContext；产品必须从此入口进入。"""
    if session_factory is None:
        # 仅验收使用的受限测试开关：默认关闭；设置后仍共用本装配路径。
        test_transport = os.environ.get("UMALIB_OPERATOR_TEST_TRANSPORT")
        if test_transport:
            import importlib

            module_name, _, attr = test_transport.partition(":")
            session_factory = getattr(importlib.import_module(module_name), attr)()
    from umalib_ingest.handoff import ApprovedPackageService
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.review import ReviewService
    from umalib_ingest.web.writer_ui import WriterUiService
    from umalib_ingest.workspace.checkpoints import CheckpointService
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import InstanceLock, WorkspaceStore

    from umalib_ingest.operator.paths import OperatorPaths
    from umalib_ingest.operator.service import OperatorService

    op_paths = OperatorPaths(root)
    op_paths.ensure_root()
    ws_paths = WorkspacePaths(root)
    ws_store = WorkspaceStore(ws_paths)
    coordinator = MaintenanceCoordinator()
    launcher = ws_store.read_launcher()
    last_id = ws_store.last_workspace("real_copy")
    if last_id is None and isinstance(launcher, dict):
        last_id = launcher.get("last_workspace_id")

    if new_workspace:
        chosen_id = None
    elif workspace_id:
        chosen_id = workspace_id
    elif isinstance(last_id, str) and last_id:
        chosen_id = last_id
    else:
        chosen_id = None

    if chosen_id is None:
        temp_ws = WorkspaceService(ws_paths, ws_store, None, coordinator)
        manifest = temp_ws.create("real_copy", name or "录入工作区")
        chosen_id = manifest.workspace_id
        ws_store.write_launcher({**launcher, "last_workspace_id": chosen_id,
                                 "last_workspace_by_mode": {
                                     **(launcher.get("last_workspace_by_mode") or {}),
                                     "real_copy": chosen_id,
                                 },
                                 "schema_version": "1", "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
    else:
        manifest = ws_store.load(chosen_id)

    # 源库：优先 source-binding.json，其次默认 main-copy（仅当存在且校验通过）
    binding_path = op_paths.source_binding(chosen_id)
    source_db = None
    if binding_path.exists():
        import json

        from umalib_ingest.operator.models import SourceBinding

        binding = SourceBinding.from_dict(json.loads(binding_path.read_text(encoding="utf-8")))
        source_db = Path(binding.path)
    else:
        detected = detect_main_copy()
        # 受限验收开关：仅当显式设置时用指定合成副本作为"检测到的"源（默认关闭）
        test_source = os.environ.get("UMALIB_OPERATOR_TEST_SOURCE")
        if test_source:
            candidate = Path(test_source)
            if candidate.exists():
                detected = candidate
        if detected is not None:
            source_db = detected

    # Acquire before restoring runs or starting workers in this workspace.
    lock = InstanceLock(ws_paths.lock_file(chosen_id))
    lock.acquire()
    service = None
    try:
        workspace_service = WorkspaceService(ws_paths, ws_store, source_db, coordinator)
        repository = InboxRepository(op_paths.inbox_db(chosen_id))
        review_service = ReviewService(repository)
        writer_service = WriterUiService(repository, None, threading.RLock(),
                                         workspace_service=workspace_service)
        checkpoint_service = CheckpointService(ws_paths, ws_store, coordinator, None)
        service = OperatorService(
            op_paths, chosen_id, workspace_service, repository, review_service,
            writer_service, checkpoint_service, coordinator,
            profile_dir=nga_profile_dir(),
            source_verifier=verify_source_path,
            session_factory=session_factory,
            nav_delay=nav_delay,
        )
        if source_db is not None and not binding_path.exists():
            service.register_detected_source(source_db)

        from umalib_ingest.web.operator_ui import build_operator_server

        server = build_operator_server(service, repository, review_service,
                                       writer_service, workspace_service,
                                       checkpoint_service, port=port)
    except BaseException:
        try:
            if service is not None:
                service.shutdown(True)
        finally:
            lock.release()
        raise

    shutdown_guard = threading.Lock()
    shutdown_done = [False]

    def shutdown() -> None:
        with shutdown_guard:
            if shutdown_done[0]:
                return
            shutdown_done[0] = True
        service.shutdown(True)
        if context_ref[0]._serving:
            server.shutdown()
        server.server_close()
        lock.release()

    context_ref = [None]
    context = OperatorContext(
        server=server, service=service, repository=repository,
        review_service=review_service, writer_service=writer_service,
        workspace_service=workspace_service, checkpoint_service=checkpoint_service,
        shutdown=shutdown,
    )
    context_ref[0] = context
    return context
