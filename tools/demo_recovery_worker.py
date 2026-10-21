"""TASK-013 M07：工作区写入恢复测试 worker（6 个真实 killpoint）。

由 demo_acceptance.py recovery 阶段或测试启动；在确定中断点写 READY marker 后
永久阻塞，等待父进程终止。只使用任务自有临时目录，不访问真实库/profile/网络。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

T0_SOURCE = "https://example.com/demo-recovery"


def _block(marker: Path) -> None:
    marker.write_text("READY", encoding="utf-8")
    while True:
        time.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -B tools/demo_recovery_worker.py")
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--killpoint", required=True,
                        choices=("k1", "k2", "k3", "k4", "k5", "k6"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    os.environ["UMALIB_DEMO_ROOT"] = str(work)
    marker = work / "ready.marker"

    from umalib_ingest.domain.models import SubmissionOrigin
    from umalib_ingest.inbox import InboxRepository
    from umalib_ingest.parser import parse_submission
    from umalib_ingest.review import ReviewFields, ReviewService, ReviewSources
    from umalib_ingest.web.writer_ui import WriterUiService
    from umalib_ingest.workspace.paths import WorkspacePaths
    from umalib_ingest.workspace.service import MaintenanceCoordinator, WorkspaceService
    from umalib_ingest.workspace.store import WorkspaceStore

    paths = WorkspacePaths(work)
    paths.ensure_root()
    store = WorkspaceStore(paths)
    service = WorkspaceService(paths, store, None, MaintenanceCoordinator())
    manifest = service.create("synthetic", "恢复测试")
    wsid = manifest.workspace_id
    repository = InboxRepository(paths.inbox_db(wsid))
    review = ReviewService(repository)

    text = "[收录]\n[标题] 恢复测试稿\n[作者] 演示作者\n[标签] 恢复标签\n\n恢复正文。"
    item = repository.import_draft(parse_submission(
        text, origin=SubmissionOrigin(platform="nga", thread_id="30099686",
                                      post_id="910001001", floor=None, canonical_url=None),
    ))
    review.save(item.id, ReviewFields(
        title="恢复测试稿", author="演示作者", translator=None, series=None,
        tags=["恢复标签"],
        sources=ReviewSources(nga=[], original_series=[], original=[T0_SOURCE],
                              translation_series=[], translation=[]),
        body_text="恢复正文。",
    ))
    repository.transition(item.id, "approved", "确认")

    writer = WriterUiService(repository, None, threading.RLock(), workspace_service=service)
    operation_id = str(uuid.uuid4())
    dto = {
        "candidate_file_name": f"candidate-{operation_id}.db",
        "choices": {
            "upload_time_seconds": 1788062400,
            "note": None,
            "ordered_sources": [T0_SOURCE],
            "tag_choices": [{"requested_kind": "tag", "requested_value": "恢复标签",
                             "existing_tag_id": None, "new_tag_type": 0}],
            "content_mode": "plain",
            "formatted_body": None,
            "plain_loss_confirmed": False,
        },
    }
    preview = writer.preview(item.id, dto, wsid)
    write_dto = {
        **dto,
        "expected_plan_sha256": preview["plan"]["plan_sha256"],
        "expected_preview_sha256": preview["preview"]["preview_sha256"],
        "expected_source_database_sha256": preview["plan"]["input"]["target_database_sha256"],
        "app_closed_confirmed": True,
        "operation_id": operation_id,
        "workspace_revision": 0,
    }

    from umalib_ingest.workspace.operations import OperationLog

    original_log_write = OperationLog.write
    original_cas = WorkspaceStore.compare_and_swap
    original_record_verified = WorkspaceService.record_verified
    original_commit = WorkspaceService.commit_operation

    def write_hook(self_log, record):
        result = original_log_write(self_log, record)
        if record.stage == "PREPARED":
            _block(marker)
        return result

    def committed_write_hook(self_log, record):
        if record.stage == "COMMITTED":
            _block(marker)  # HEAD 已推进，COMMITTED 未落盘
        return original_log_write(self_log, record)

    def cas_hook(self_store, *args, **kwargs):
        result = original_cas(self_store, *args, **kwargs)
        _block(marker)
        return result

    def verified_pre_hook(self_service, *args, **kwargs):
        _block(marker)

    def verified_post_hook(self_service, *args, **kwargs):
        result = original_record_verified(self_service, *args, **kwargs)
        _block(marker)
        return result

    def commit_pre_hook(self_service, *args, **kwargs):
        _block(marker)

    patches: list[Any] = []
    if args.killpoint == "k1":
        patches.append(mock.patch.object(OperationLog, "write", side_effect=write_hook,
                                         autospec=True))
    elif args.killpoint == "k2":
        patches.append(mock.patch.object(WorkspaceStore, "compare_and_swap",
                                         side_effect=cas_hook, autospec=True))
    elif args.killpoint == "k3":
        patches.append(mock.patch.object(WorkspaceService, "record_verified",
                                         side_effect=verified_pre_hook, autospec=True))
    elif args.killpoint == "k4":
        patches.append(mock.patch.object(WorkspaceService, "record_verified",
                                         side_effect=verified_post_hook, autospec=True))
    elif args.killpoint == "k5":
        patches.append(mock.patch.object(WorkspaceService, "commit_operation",
                                         side_effect=commit_pre_hook, autospec=True))
    elif args.killpoint == "k6":
        patches.append(mock.patch.object(OperationLog, "write",
                                         side_effect=committed_write_hook, autospec=True))
    for patch in patches:
        patch.start()
    writer.write(item.id, write_dto, wsid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
