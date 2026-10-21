"""TASK-013 M08/M09：Demo API 服务层。

只编排现有公共 API（Parser/Inbox/Review/Workspace/Writer/Watcher/格式化/
来源规范化）；不复制 SQL 或哈希算法。所有错误沿用现有稳定错误结构。
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from umalib_ingest.web.writer_ui import WriterUiError
from umalib_ingest.workspace.errors import WorkspaceError


class DemoUiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _bad(message: str) -> DemoUiError:
    return DemoUiError(400, "demo_invalid_request", message)


class DemoUiService:
    """Guided demo flow over one workspace + the real services."""

    def __init__(
        self,
        workspace_service,
        workspace_id: str,
        repository,
        review_service,
        writer_service: WriterUiService,
        nga_setup=None,
        simulated_scan_fn=None,
    ) -> None:
        self._workspace = workspace_service
        self._workspace_id = workspace_id
        self._repository = repository
        self._review = review_service
        self._writer = writer_service
        self._nga = nga_setup
        self._simulated_scan = simulated_scan_fn
        self._write_lock = threading.RLock()

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def writer_service(self):
        """Expose the workspace-aware writer for the HTTP handler binding."""
        return self._writer

    # ---- status / samples ----

    def status(self) -> dict[str, Any]:
        from umalib_ingest.workspace.service import file_sha256

        manifest = self._workspace.store().load(self._workspace_id)
        head, source = self._workspace.resolve_head(self._workspace_id)
        deliveries = self._workspace.list_deliveries(self._workspace_id)
        findings = self._workspace.inspect_recovery(self._workspace_id)
        latest = next((d for d in deliveries if d.get("latest")), None)
        return {
            "workspace_id": self._workspace_id,
            "name": manifest.name,
            "mode": manifest.mode,
            "revision": manifest.revision,
            "head": None if head.is_none else {
                "operation_id": head.operation_id,
                "candidate_file_name": head.candidate_file_name,
                "sha256": head.sha256,
            },
            "source_sha256": file_sha256(source),
            "latest_delivery": latest,
            "delivery_count": len(deliveries),
            "recovery_findings": findings,
            "simulated_scan_only": manifest.mode == "synthetic",
            "nga_enabled": self._nga is not None,
            "maintenance": False,
        }

    def samples(self) -> dict[str, Any]:
        from umalib_ingest.demo.samples import SAMPLES

        return {"samples": [{
            "sample_id": s["sample_id"], "title": s["title"],
            "description": s["description"], "expected_status": s["expected_status"],
            "next_step": s["next_step"],
        } for s in SAMPLES]}

    def import_sample(self, sample_id: str) -> dict[str, Any]:
        from umalib_ingest.demo.samples import sample_by_id
        from umalib_ingest.domain.models import SubmissionOrigin
        from umalib_ingest.parser import parse_submission

        sample = sample_by_id(sample_id)
        # Synthetic samples model NGA submissions so they can traverse the
        # same approved-package/Writer contract as real imports.  The source
        # URL remains a fictional example URL; only the transport platform is
        # marked as NGA for contract compatibility.
        origin = SubmissionOrigin(
            platform="nga", thread_id="demo", post_id=sample_id, floor=None,
            canonical_url=sample.get("source"),
        )
        draft = parse_submission(sample["raw_text"], origin=origin)
        item = self._repository.import_draft(draft)
        return {"id": item.id, "sample_id": sample_id, "status": item.status}

    def import_manual(self, raw_text: str) -> dict[str, Any]:
        from umalib_ingest.parser import parse_submission

        if not isinstance(raw_text, str) or raw_text.strip() == "":
            raise _bad("raw_text 必须是非空字符串")
        item = self._repository.import_draft(parse_submission(raw_text))
        return {"id": item.id, "reused": False, "status": item.status}

    # ---- format / source preview ----

    def format_preview(self, raw_text: str, formatted_body: str | None = None) -> dict[str, Any]:
        from umalib_ingest.formatting.extract import extract_body_for_presentation
        from umalib_ingest.formatting.quill import render_quill_html, validate_quill_subset
        from umalib_ingest.formatting.ubb import parse_basic_ubb

        if formatted_body is None or formatted_body.strip() == "":
            extraction = extract_body_for_presentation(raw_text)
            if not extraction.ready:
                return {"mode": extraction.mode, "document": None,
                        "plain_text": None,
                        "diagnostics": [d.__dict__ for d in extraction.diagnostics]}
            result = parse_basic_ubb(extraction.body)
            return {
                "mode": "auto",
                "source_body": extraction.body,
                "document": None,
                "html": render_quill_html(result.document) if result.ready else None,
                "plain_text": result.plain_text,
                "diagnostics": [d.__dict__ for d in result.diagnostics],
            }
        result = parse_basic_ubb(formatted_body)
        return {
            "mode": "manual",
            "source_body": None,
            "html": render_quill_html(result.document) if result.ready else None,
            "plain_text": result.plain_text,
            "diagnostics": [d.__dict__ for d in result.diagnostics],
        }

    def source_preview(self, urls: list[str]) -> dict[str, Any]:
        from umalib_ingest.review.source_urls import normalize_source_url

        if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
            raise _bad("urls 必须是字符串数组")
        return {"results": [{
            "original": u,
            "normalized": r.normalized,
            "changed": r.changed,
            "warnings": list(r.warnings),
            "blockers": list(r.blockers),
        } for u in urls for r in [normalize_source_url(u)]]}

    def catalog(self) -> dict[str, Any]:
        """Load the tag catalog for the current workspace HEAD, read-only."""
        from umalib_ingest.umalib_writer import load_target_tag_catalog

        try:
            _head, source = self._workspace.resolve_head(self._workspace_id)
            catalog = load_target_tag_catalog(source)
        except Exception as exc:
            raise DemoUiError(409, "catalog_not_ready", "当前候选库标签目录不可用，请刷新后重试") from exc
        return {
            "database_sha256": catalog.database_sha256,
            "schema_fingerprint_sha256": catalog.schema_fingerprint_sha256,
            "tags": [
                {"id": tag.id, "name": tag.name, "type": tag.type,
                 "usage_count": tag.usage_count}
                for tag in catalog.tags
            ],
        }

    def apply_sources(self, item_id: int, urls: list[str]) -> dict[str, Any]:
        from umalib_ingest.review.source_urls import normalize_source_url
        from umalib_ingest.workspace.service import canonical_sha256

        item = self._repository.get(item_id)
        if item is None:
            raise DemoUiError(404, "item_not_found", "记录不存在")
        normalized: list[str] = []
        for url in urls:
            result = normalize_source_url(url)
            if not result.ready:
                raise _bad(f"来源无法规范化: {url}")
            normalized.append(result.normalized or url)
        fields = self._current_fields(item)
        fields["sources"]["original"] = normalized
        self._review.save(item_id, self._fields(fields))
        return {"item_id": item_id, "sources": normalized}

    # ---- choices ----

    def get_choices(self, item_id: int) -> dict[str, Any]:
        value = self._workspace.load_choices(self._workspace_id, item_id)
        return {"choices": value}

    def put_choices(self, item_id: int, dto: dict[str, Any]) -> dict[str, Any]:
        from umalib_ingest.workspace.models import ChoicesFile, utc_now
        from umalib_ingest.workspace.service import canonical_sha256

        allowed = {"note", "upload_time_seconds", "ordered_sources", "tag_choices",
                   "content_mode", "formatted_body", "plain_loss_confirmed"}
        if not isinstance(dto, dict) or set(dto) != allowed:
            raise _bad("choices 字段不完整或包含未知键")
        item = self._repository.get(item_id)
        if item is None:
            raise DemoUiError(404, "item_not_found", "记录不存在")
        choices = ChoicesFile.from_dict({
            **dto, "version": 1, "item_id": item_id,
            "working_draft_sha256": canonical_sha256(item.working_draft),
            "package_sha256": None, "updated_at": utc_now(),
        })
        saved = self._workspace.save_choices(
            self._workspace_id, item_id,
            {key: value for key, value in choices.to_dict().items()
             if key in allowed},
        )
        return {"choices": saved}

    # ---- deliveries / operations / recovery ----

    def deliveries(self) -> dict[str, Any]:
        return {"deliveries": self._workspace.list_deliveries(self._workspace_id)}

    def operation(self, operation_id: str) -> dict[str, Any]:
        record = self._workspace.get_operation(self._workspace_id, operation_id)
        if record is None:
            raise DemoUiError(404, "operation_not_found", "操作不存在")
        findings = self._workspace.inspect_recovery(self._workspace_id)
        allowed = next(
            (f["allowed_actions"] for f in findings
             if f["operation_id"] == operation_id), [],
        )
        return {"operation": record.to_dict(), "allowed_actions": allowed}

    def recover(self, operation_id: str, action: str, confirmed: bool) -> dict[str, Any]:
        return self._workspace.recover(
            self._workspace_id, operation_id, action, confirmed,
        )

    # ---- simulated scan ----

    def simulated_scan(self, scenario_id: str) -> dict[str, Any]:
        from umalib_ingest.demo import samples as sample_mod
        from umalib_ingest.workspace.service import MaintenanceCoordinator

        manifest = self._workspace.store().load(self._workspace_id)
        if manifest.mode != "synthetic":
            raise DemoUiError(409, "simulated_scan_forbidden",
                              "模拟扫描只允许 synthetic 工作区")
        scenario = {"sim-01-basics": sample_mod.SCAN_SCENARIO_ONE,
                    "sim-02-update": sample_mod.SCAN_SCENARIO_TWO}.get(scenario_id)
        if scenario is None:
            raise _bad("未知场景")
        paths = self._workspace.paths
        state_file = paths.watcher_state(self._workspace_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        snap_dir = paths.watcher_snapshots(self._workspace_id)
        snap_dir.mkdir(parents=True, exist_ok=True)
        from umalib_ingest.watcher.state import (
            WatcherState,
            atomic_write_json,
        )

        if not state_file.exists():
            state = WatcherState(
                schema_version="1", tid=30099686,
                canonical_thread_url="https://bbs.nga.cn/read.php?tid=30099686",
                last_page=173, known_floors={},
                baseline_created_at="2026-09-03T00:00:00+00:00",
                last_successful_scan_at=None, last_result_summary=None,
            )
            atomic_write_json(state_file, state.to_dict())
        result = self._simulated_scan(
            state_file=state_file,
            inbox_db=paths.inbox_db(self._workspace_id),
            snapshot_dir=snap_dir,
            scenario=scenario,
            profile_dir=paths.sub_path(self._workspace_id, "sim-profile"),
        )
        return {"scan": result, "simulated": True,
                "notice": "离线模拟，不代表已连接 NGA"}

    # ---- nga baseline ----

    def nga_baseline_preview(self, confirm_read: bool) -> dict[str, Any]:
        if self._nga is None:
            raise DemoUiError(409, "nga_not_enabled",
                              "本进程未启用 NGA（启动需 --enable-nga）")
        if type(confirm_read) is not bool or confirm_read is not True:
            raise _bad("confirm_read 必须为 true")
        preview = self._nga.build_preview()
        return {"preview": preview.to_dict(),
                "notice": "从现在开始：不补录历史，旧页日后编辑不保证被发现"}

    def nga_baseline_confirm(self, preview_id: str, observed_sha256: str,
                             confirm_skip_history: bool) -> dict[str, Any]:
        if self._nga is None:
            raise DemoUiError(409, "nga_not_enabled", "本进程未启用 NGA")
        if type(confirm_skip_history) is not bool or confirm_skip_history is not True:
            raise _bad("confirm_skip_history 必须为 true")
        try:
            return self._nga.confirm(preview_id, observed_sha256, True)
        except ValueError as exc:
            raise DemoUiError(409, "nga_baseline_confirm_failed", str(exc)) from None

    # ---- checkpoints ----

    def set_checkpoints(self, checkpoints) -> None:
        self._checkpoints = checkpoints

    def create_checkpoint(self, confirmed: bool) -> dict[str, Any]:
        return self._checkpoints.create(self._workspace_id, confirmed)

    def list_checkpoints(self) -> dict[str, Any]:
        from umalib_ingest.workspace.service import file_sha256

        directory = self._workspace.paths.checkpoints_dir(self._workspace_id)
        entries = []
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                checkpoint_json = entry / "checkpoint.json"
                if entry.is_dir() and checkpoint_json.exists():
                    import json

                    payload = json.loads(checkpoint_json.read_text(encoding="utf-8"))
                    entries.append({
                        "checkpoint_id": payload.get("checkpoint_id"),
                        "revision": payload.get("revision"),
                        "created_at": payload.get("created_at"),
                    })
        return {"checkpoints": entries}

    def restore_checkpoint(self, checkpoint_id: str, confirmed: bool) -> dict[str, Any]:
        return self._checkpoints.restore(self._workspace_id, checkpoint_id, confirmed)

    # ---- helpers ----

    def _current_fields(self, item) -> dict[str, Any]:
        # Choices and source edits must start from the current editable draft;
        # using original_draft here would silently undo the operator's review.
        draft = item.working_draft
        content = draft.get("content") or {}
        metadata = draft.get("metadata") or {}
        return {
            "title": metadata.get("title"),
            "author": metadata.get("author"),
            "translator": metadata.get("translator"),
            "series": metadata.get("series"),
            "tags": list(metadata.get("tags") or []),
            "sources": {
                key: list((metadata.get("sources") or {}).get(key) or [])
                for key in ("nga", "original_series", "original",
                            "translation_series", "translation")
            },
            "body_text": content.get("body_text"),
        }

    def _fields(self, fields: dict[str, Any]):
        from umalib_ingest.review import ReviewFields, ReviewSources

        return ReviewFields(
            title=fields["title"], author=fields["author"],
            translator=fields["translator"], series=fields["series"],
            tags=list(fields["tags"]),
            sources=ReviewSources(**{
                key: list(fields["sources"].get(key) or [])
                for key in ("nga", "original_series", "original",
                            "translation_series", "translation")
            }),
            body_text=fields["body_text"],
        )
