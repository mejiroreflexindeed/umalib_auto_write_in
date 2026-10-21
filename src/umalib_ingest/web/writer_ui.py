"""Writer UI service layer (TASK-009).

Orchestrates only the existing public APIs (ApprovedPackageService,
load_target_tag_catalog, build_umalib_change_plan, preview_candidate_write,
write_candidate_database, InboxRepository.transition). No Writer SQL, hash
algorithms or database validation logic is duplicated here.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from umalib_ingest.handoff import ApprovedPackageService, HandoffNotReadyError
from umalib_ingest.inbox import (
    InboxItem,
    InboxRepository,
    InboxItemNotFoundError,
)
from umalib_ingest.umalib_adapter import (
    AdapterChoices,
    TagChoice,
    build_umalib_change_plan,
)
from umalib_ingest.umalib_adapter.errors import AdapterError
from umalib_ingest.umalib_writer import (
    CandidateWriteApproval,
    UmaLibWriterError,
    WriterApprovalError,
    WriterNotReadyError,
    WriterPathError,
    load_target_tag_catalog,
    preview_candidate_write,
    write_candidate_database,
)
from umalib_ingest.umalib_writer.file_state import path_sha256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_PREVIEW_TOP_KEYS = {"candidate_file_name", "choices"}
_CHOICES_KEYS = {"upload_time_seconds", "note", "ordered_sources", "tag_choices"}
_WORKSPACE_CHOICES_KEYS = _CHOICES_KEYS | {
    "content_mode", "formatted_body", "plain_loss_confirmed",
}
_WORKSPACE_EXTRA_KEYS = {"operation_id", "workspace_revision"}
_TAG_CHOICE_KEYS = {"requested_kind", "requested_value", "existing_tag_id", "new_tag_type"}
_WRITE_EXTRA_KEYS = {
    "expected_plan_sha256",
    "expected_preview_sha256",
    "expected_source_database_sha256",
    "app_closed_confirmed",
}
_MAX_UPLOAD_TIME = 2147483647
_MAX_FILE_NAME = 255


class WriterUiError(Exception):
    """A stable, safe business error to map onto an HTTP response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WriterUiConfig:
    """CLI-fixed Writer configuration; never accepted from HTTP requests."""

    source_db: Path
    candidate_dir: Path


def _invalid(message: str = "请求形状不正确") -> WriterUiError:
    """Build the stable invalid-request error."""
    return WriterUiError(400, "writer_invalid_request", message)


class WriterUiService:
    """Orchestrates Writer preview/write for the local web UI."""

    def __init__(
        self,
        repository: InboxRepository,
        config: WriterUiConfig | None,
        lock: threading.RLock,
        workspace_service=None,
    ) -> None:
        """Bind the service to the Inbox, optional Writer config and shared lock.

        workspace_service enables the cumulative-workspace branch (TASK-013);
        when absent the legacy single-write behaviour is unchanged.
        """
        self._repository = repository
        self._config = config
        self._lock = lock
        self._workspace = workspace_service
        self._package_service = ApprovedPackageService(repository)

    @property
    def workspace_enabled(self) -> bool:
        return self._workspace is not None

    @property
    def enabled(self) -> bool:
        """Return whether the Writer is configured."""
        return self._config is not None

    def config_dict(self) -> dict[str, Any]:
        """Return the minimal config view without absolute paths."""
        if self._config is None:
            return {"enabled": False}
        return {
            "enabled": True,
            "source_file_name": self._config.source_db.name,
            "source_path_sha256": path_sha256(self._config.source_db),
            "candidate_dir_name": self._config.candidate_dir.name,
            "candidate_dir_path_sha256": path_sha256(self._config.candidate_dir),
        }

    def catalog(self) -> dict[str, Any]:
        """Load the current target tag catalog read-only."""
        config = self._require_config()
        try:
            catalog = load_target_tag_catalog(config.source_db)
        except UmaLibWriterError:
            raise WriterUiError(
                409, "writer_source_not_ready", "源数据库不可用或状态异常，无法加载标签目录"
            ) from None
        return {
            "database_sha256": catalog.database_sha256,
            "schema_fingerprint_sha256": catalog.schema_fingerprint_sha256,
            "tags": [
                {"id": tag.id, "name": tag.name, "type": tag.type, "usage_count": tag.usage_count}
                for tag in catalog.tags
            ],
        }

    def preview(self, item_id: int, dto: dict[str, Any],
                workspace_id: str | None = None) -> dict[str, Any]:
        """Rebuild package/catalog/plan/preview from current state; never writes."""
        config = self._resolve_config(workspace_id)
        parsed = (
            _parse_preview_dto(dto)
            if workspace_id is None
            else _parse_workspace_preview_dto(dto)
        )
        item = self._require_approved_item(item_id)
        package = self._build_package(item_id)
        catalog = self._load_catalog(config)
        plan = self._build_plan(package, catalog, parsed)
        candidate = config.candidate_dir / parsed["candidate_file_name"]
        try:
            preview = preview_candidate_write(plan, config.source_db, candidate)
        except UmaLibWriterError:
            raise WriterUiError(
                409, "writer_preview_not_ready", "无法完成 dry-run 预览"
            ) from None
        return {"plan": plan.to_dict(), "preview": preview.to_dict()}

    def write(self, item_id: int, dto: dict[str, Any],
              workspace_id: str | None = None) -> dict[str, Any]:
        """Rebuild everything under the shared lock, write and mark exported.

        Workspace mode additionally: idempotent operation replay, HEAD-derived
        source, cumulative candidate naming and delivery records.
        """
        with self._lock:
            config = self._resolve_config(workspace_id)
            parsed = _parse_write_dto(dto) if workspace_id is None \
                else _parse_workspace_write_dto(dto)
            if workspace_id is not None:
                return self._write_workspace(
                    item_id, workspace_id, config, dto, parsed,
                )
            return self._write_legacy(item_id, config, parsed)

    # ---- workspace branch (TASK-013) ----

    def _write_workspace(
        self,
        item_id: int,
        workspace_id: str,
        config: WriterUiConfig,
        dto: dict[str, Any],
        parsed: dict[str, Any],
    ) -> dict[str, Any]:
        from umalib_ingest.workspace.errors import (
            OperationConflictError,
            RecoveryRequiredError,
            WorkspaceError,
        )
        from umalib_ingest.workspace.operations import build_request_fingerprint
        from umalib_ingest.workspace.service import file_sha256

        workspace = self._workspace
        if workspace is None:
            raise WriterUiError(409, "workspace_not_active", "当前不是工作区模式")
        operation_id = _validate_operation_id(dto.get("operation_id"))
        workspace_revision = _validate_workspace_revision(dto.get("workspace_revision"))
        expected_basename = f"candidate-{operation_id}.db"
        if parsed["candidate_file_name"] != expected_basename:
            raise WriterUiError(400, "writer_invalid_request",
                                "候选文件名必须等于 candidate-<operation_id>.db")

        # 6.2：重试先查询操作记录，不先重建批准包（exported 项不得误报未批准）
        existing = workspace.get_operation(workspace_id, operation_id)
        retry_choices = {
            "upload_time_seconds": parsed["upload_time_seconds"],
            "note": parsed["note"],
            "ordered_sources": parsed["ordered_sources"],
            "tag_choices": parsed["tag_choices"],
            "content_mode": parsed["content_mode"],
            "formatted_body": parsed["formatted_body"],
            "plain_loss_confirmed": parsed["plain_loss_confirmed"],
        }
        if existing is not None:
            from umalib_ingest.workspace.models import canonical_json

            same_request = (
                existing.item_id == item_id
                and existing.candidate_file_name == expected_basename
                and existing.plan_sha256 == parsed["expected_plan_sha256"]
                and existing.preview_sha256 == parsed["expected_preview_sha256"]
                and existing.request_choices is not None
                and canonical_json(existing.request_choices) == canonical_json(retry_choices)
            )
            if not same_request:
                raise WriterUiError(409, "operation_conflict",
                                    "操作已存在但请求内容不同，不能重复执行")
            if existing.stage == "COMMITTED":
                return self._replay_response(
                    existing,
                    self._delivery_for_operation(workspace_id, operation_id),
                    self._repository.get(item_id),
                )
            if existing.stage in ("PREPARED", "CANDIDATE_VERIFIED"):
                return self._pending_response(existing, self._repository.get(item_id))
            raise WriterUiError(409, "operation_abandoned",
                                "该操作已放弃，请使用新的操作 UUID")

        head, _ = workspace.resolve_head(workspace_id)
        if head.revision != workspace_revision:
            raise WriterUiError(409, "workspace_head_changed",
                                "工作区 HEAD 已变化，请刷新后重新预览")

        item = self._require_approved_item(item_id)
        package = self._build_package(item_id)
        catalog = self._load_catalog(config)
        plan = self._build_plan(package, catalog, parsed)
        candidate = config.candidate_dir / expected_basename
        try:
            preview = preview_candidate_write(plan, config.source_db, candidate)
        except UmaLibWriterError:
            raise WriterUiError(409, "writer_preview_not_ready",
                                "无法完成 dry-run 预览") from None
        if not preview.ready_to_write:
            blockers = preview.to_dict()["diagnostics"]["blockers"]
            codes = [b["code"] for b in blockers]
            if "candidate_exists" in codes or "candidate_sidecar_present" in codes:
                raise WriterUiError(409, "writer_candidate_exists",
                                    "候选文件已存在，禁止覆盖")
            raise WriterUiError(409, "writer_preview_not_ready",
                                "Preview 存在 blockers，不能写入")
        if not plan.ready:
            raise WriterUiError(409, "writer_plan_not_ready", "计划未就绪，不能写入")

        fingerprint = build_request_fingerprint(
            workspace_id=workspace_id,
            item_id=item_id,
            package_sha256=package.package_sha256,
            parent_revision=head.revision,
            parent_head_sha256=head.sha256,
            choices={
                "upload_time_seconds": parsed["upload_time_seconds"],
                "note": parsed["note"],
                "ordered_sources": parsed["ordered_sources"],
                "tag_choices": parsed["tag_choices"],
                "content_mode": parsed["content_mode"],
                "formatted_body": parsed["formatted_body"],
                "plain_loss_confirmed": parsed["plain_loss_confirmed"],
            },
            plan_sha256=plan.plan_sha256,
            preview_sha256=preview.preview_sha256,
            candidate_file_name=expected_basename,
        )
        try:
            record = workspace.begin_operation(
                workspace_id=workspace_id,
                operation_id=operation_id,
                item_id=item_id,
                request_fingerprint=fingerprint,
                request_choices=retry_choices,
                parent_revision=head.revision,
                parent_head_sha256=head.sha256,
                candidate_file_name=expected_basename,
                plan_sha256=plan.plan_sha256,
                preview_sha256=preview.preview_sha256,
            )
        except OperationConflictError:
            raise WriterUiError(409, "operation_conflict",
                                "操作已存在但请求内容不同，不能重复执行") from None
        except RecoveryRequiredError as exc:
            raise WriterUiError(409, "recovery_required", exc.message) from None
        except WorkspaceError as exc:
            raise WriterUiError(409, exc.code, exc.message) from None

        if record.stage == "COMMITTED":
            delivery = self._delivery_for_operation(workspace_id, operation_id)
            return self._replay_response(record, delivery, item)
        if record.stage == "CANDIDATE_VERIFIED":
            return self._pending_response(record, item)
        if record.stage == "PREPARED" and candidate.exists():
            return self._pending_response(record, item)
        if record.stage == "ABANDONED":
            raise WriterUiError(409, "operation_abandoned",
                                "该操作已放弃，请使用新的操作 UUID")

        approval = CandidateWriteApproval(
            plan_sha256=parsed["expected_plan_sha256"],
            preview_sha256=parsed["expected_preview_sha256"],
            source_database_sha256=parsed["expected_source_database_sha256"],
            app_closed_confirmed=parsed["app_closed_confirmed"],
        )
        try:
            result = write_candidate_database(plan, config.source_db, candidate, approval)
        except WriterApprovalError:
            raise WriterUiError(
                409, "writer_approval_mismatch", "批准哈希或关闭确认与当前状态不一致"
            ) from None
        except WriterNotReadyError:
            raise WriterUiError(
                409, "writer_preview_not_ready", "Preview 存在 blockers，不能写入"
            ) from None
        except WriterPathError:
            raise WriterUiError(
                409, "writer_candidate_exists", "候选文件已存在或路径不可用，禁止覆盖"
            ) from None
        except UmaLibWriterError:
            raise WriterUiError(500, "writer_execution_failed", "候选数据库生成失败") from None

        workspace.record_verified(workspace_id, operation_id, result.to_dict(),
                                  expected_basename)
        reason = (
            f"候选写入成功: {expected_basename} "
            f"result_sha256={result.result_sha256} operation_id={operation_id}"
        )
        try:
            item_after = self._repository.transition(item_id, "exported", reason)
        except Exception:
            raise WriterUiError(
                500,
                "writer_post_write_state_failed",
                "候选已生成并验证通过，但 Inbox 状态更新失败；"
                f"候选文件 {expected_basename} 与 result_sha256={result.result_sha256} "
                f"operation_id={operation_id} 已保留，请通过交付与恢复页面完成收尾。",
            ) from None
        candidate_sha = file_sha256(candidate)
        counts = result.to_dict().get("counts_after") or {}
        delivery = workspace.commit_operation(
            workspace_id=workspace_id,
            operation_id=operation_id,
            candidate_file_name=expected_basename,
            candidate_sha256=candidate_sha,
            article_name=str((plan.to_dict().get("article") or {}).get("name") or ""),
            count_after=int(counts.get("article") or 0),
            result_sha256=result.result_sha256,
        )
        return {
            "result": result.to_dict(),
            "item": item_after,
            "exported": True,
            "operation": record.to_dict(),
            "delivery": delivery.to_dict(),
        }

    def _delivery_for_operation(self, workspace_id: str, operation_id: str):
        for delivery in self._workspace.list_deliveries(workspace_id):
            if delivery.get("operation_id") == operation_id:
                return delivery
        raise WriterUiError(409, "delivery_missing", "交付记录缺失，请人工核对")

    def _replay_response(self, record, delivery, item) -> dict[str, Any]:
        return {
            "result": record.result,
            "item": item,
            "exported": True,
            "replayed": True,
            "operation": record.to_dict(),
            "delivery": delivery,
        }

    def _pending_response(self, record, item) -> dict[str, Any]:
        return {
            "result": None,
            "item": item,
            "exported": False,
            "pending": True,
            "operation": record.to_dict(),
        }

    # ---- legacy branch (unchanged behaviour) ----

    def _write_legacy(
        self, item_id: int, config: WriterUiConfig, parsed: dict[str, Any]
    ) -> dict[str, Any]:
        item = self._require_approved_item(item_id)
        package = self._build_package(item_id)
        catalog = self._load_catalog(config)
        plan = self._build_plan(package, catalog, parsed)
        candidate = config.candidate_dir / parsed["candidate_file_name"]
        try:
            preview = preview_candidate_write(plan, config.source_db, candidate)
        except UmaLibWriterError:
            raise WriterUiError(
                409, "writer_preview_not_ready", "无法完成 dry-run 预览"
            ) from None
        if not preview.ready_to_write:
            blockers = preview.to_dict()["diagnostics"]["blockers"]
            codes = [b["code"] for b in blockers]
            if "candidate_exists" in codes or "candidate_sidecar_present" in codes:
                raise WriterUiError(409, "writer_candidate_exists", "候选文件已存在，禁止覆盖")
            raise WriterUiError(409, "writer_preview_not_ready", "Preview 存在 blockers，不能写入")
        if not plan.ready:
            raise WriterUiError(409, "writer_plan_not_ready", "计划未就绪，不能写入")
        approval = CandidateWriteApproval(
            plan_sha256=parsed["expected_plan_sha256"],
            preview_sha256=parsed["expected_preview_sha256"],
            source_database_sha256=parsed["expected_source_database_sha256"],
            app_closed_confirmed=parsed["app_closed_confirmed"],
        )
        try:
            result = write_candidate_database(
                plan, config.source_db, candidate, approval
            )
        except WriterApprovalError:
            raise WriterUiError(
                409, "writer_approval_mismatch", "批准哈希或关闭确认与当前状态不一致"
            ) from None
        except WriterNotReadyError:
            raise WriterUiError(
                409, "writer_preview_not_ready", "Preview 存在 blockers，不能写入"
            ) from None
        except WriterPathError:
            raise WriterUiError(
                409, "writer_candidate_exists", "候选文件已存在或路径不可用，禁止覆盖"
            ) from None
        except UmaLibWriterError:
            raise WriterUiError(
                500, "writer_execution_failed", "候选数据库生成失败"
            ) from None
        reason = (
            f"候选写入成功: {parsed['candidate_file_name']} "
            f"result_sha256={result.result_sha256}"
        )
        try:
            item_after = self._repository.transition(item_id, "exported", reason)
        except Exception:
            # The candidate has already been published and verified.  Any
            # failure after that point must use the recovery response so an
            # operator is never told only that an ordinary internal error
            # occurred while a real candidate is left on disk.
            raise WriterUiError(
                500,
                "writer_post_write_state_failed",
                "候选已生成并验证通过，但 Inbox 状态更新失败；请人工复核后重试，"
                f"候选文件 {parsed['candidate_file_name']} 与 "
                f"result_sha256={result.result_sha256} 已保留，不要删除。",
            ) from None
        return {
            "result": result.to_dict(),
            "item": item_after,
            "exported": True,
        }

    # ---- internals ----

    def _resolve_config(self, workspace_id: str | None) -> WriterUiConfig:
        """Resolve the Writer config: legacy fixed config or workspace HEAD."""
        if workspace_id is None:
            return self._require_config()
        if self._workspace is None:
            raise WriterUiError(409, "workspace_not_active", "当前不是工作区模式")
        try:
            _, source = self._workspace.resolve_head(workspace_id)
        except Exception as exc:
            code = getattr(exc, "code", "workspace_resolve_failed")
            raise WriterUiError(409, code, str(getattr(exc, "message", exc))) from None
        return WriterUiConfig(
            source_db=source,
            candidate_dir=self._workspace._paths.candidates_dir(workspace_id),
        )

    def _require_config(self) -> WriterUiConfig:
        """Return the config or raise the not-configured error."""
        if self._config is None:
            raise WriterUiError(409, "writer_not_configured", "启动时未配置 Writer（缺 --umalib-db / --candidate-dir）")
        return self._config

    def _require_approved_item(self, item_id: int) -> InboxItem:
        """Return the item if it exists and is currently approved."""
        item = self._repository.get(item_id)
        if item is None:
            raise WriterUiError(404, "item_not_found", "记录不存在")
        if item.status != "approved":
            raise WriterUiError(409, "writer_item_not_approved", "只有 approved 记录可以生成候选")
        return item

    def _build_package(self, item_id: int):
        """Build the current approved package from the Inbox state."""
        try:
            return self._package_service.build(item_id)
        except InboxItemNotFoundError:
            raise WriterUiError(404, "item_not_found", "记录不存在") from None
        except HandoffNotReadyError:
            raise WriterUiError(
                409, "writer_item_not_approved", "批准包暂不可生成，无法继续"
            ) from None

    def _load_catalog(self, config: WriterUiConfig):
        """Load the current target catalog read-only."""
        try:
            return load_target_tag_catalog(config.source_db)
        except UmaLibWriterError:
            raise WriterUiError(
                409, "writer_source_not_ready", "源数据库不可用或状态异常"
            ) from None

    @staticmethod
    def _build_plan(package, catalog, parsed: dict[str, Any]):
        """Build the change plan from the strict DTO."""
        try:
            choices = AdapterChoices(
                upload_time_seconds=parsed["upload_time_seconds"],
                note=parsed["note"],
                ordered_sources=tuple(parsed["ordered_sources"]),
                tag_choices=tuple(
                    TagChoice(
                        requested_kind=item["requested_kind"],
                        requested_value=item["requested_value"],
                        existing_tag_id=item["existing_tag_id"],
                        new_tag_type=item["new_tag_type"],
                    )
                    for item in parsed["tag_choices"]
                ),
                content_mode=parsed.get("content_mode", "plain"),
                formatted_body=parsed.get("formatted_body"),
            )
            return build_umalib_change_plan(package, choices, catalog)
        except AdapterError:
            raise _invalid("choices 无法构造有效的计划") from None


def _parse_preview_dto(dto: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate the preview request DTO."""
    if not isinstance(dto, dict) or set(dto) != _PREVIEW_TOP_KEYS:
        raise _invalid("请求必须且只能包含 candidate_file_name 与 choices")
    file_name = _validate_candidate_file_name(dto["candidate_file_name"])
    choices = _parse_choices(dto["choices"])
    return {
        "candidate_file_name": file_name,
        "upload_time_seconds": choices["upload_time_seconds"],
        "note": choices["note"],
        "ordered_sources": choices["ordered_sources"],
        "tag_choices": choices["tag_choices"],
    }


def _parse_workspace_preview_dto(dto: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate the workspace preview DTO (candidate + format choices)."""
    if not isinstance(dto, dict) or set(dto) != _PREVIEW_TOP_KEYS:
        raise _invalid("请求必须且只能包含 candidate_file_name 与 choices")
    file_name = _validate_candidate_file_name(dto["candidate_file_name"])
    choices = _parse_workspace_choices(dto["choices"])
    return {"candidate_file_name": file_name, **choices}


def _parse_write_dto(dto: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate the write request DTO."""
    if not isinstance(dto, dict) or set(dto) != (_PREVIEW_TOP_KEYS | _WRITE_EXTRA_KEYS):
        raise _invalid("写入请求字段不完整或包含未知键")
    preview_part = {key: dto[key] for key in _PREVIEW_TOP_KEYS}
    parsed = _parse_preview_dto(preview_part)
    for key in (
        "expected_plan_sha256",
        "expected_preview_sha256",
        "expected_source_database_sha256",
    ):
        value = dto[key]
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise _invalid(f"{key} 必须是 64 位小写十六进制 SHA-256")
        parsed[key] = value
    confirmed = dto["app_closed_confirmed"]
    if type(confirmed) is not bool or confirmed is not True:
        raise _invalid("app_closed_confirmed 必须为 true（人工确认大书库已关闭）")
    parsed["app_closed_confirmed"] = confirmed
    return parsed


def _parse_workspace_write_dto(dto: dict[str, Any]) -> dict[str, Any]:
    """Strictly validate the workspace write DTO (operation fields + format)."""
    allowed = _PREVIEW_TOP_KEYS | _WRITE_EXTRA_KEYS | _WORKSPACE_EXTRA_KEYS
    if not isinstance(dto, dict) or set(dto) != allowed:
        raise _invalid("工作区写入请求字段不完整或包含未知键")
    parsed = _parse_workspace_preview_dto({key: dto[key] for key in _PREVIEW_TOP_KEYS})
    for key in (
        "expected_plan_sha256",
        "expected_preview_sha256",
        "expected_source_database_sha256",
    ):
        value = dto[key]
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            raise _invalid(f"{key} 必须是 64 位小写十六进制 SHA-256")
        parsed[key] = value
    confirmed = dto["app_closed_confirmed"]
    if type(confirmed) is not bool or confirmed is not True:
        raise _invalid("app_closed_confirmed 必须为 true（人工确认大书库已关闭）")
    parsed["app_closed_confirmed"] = confirmed
    parsed["operation_id"] = _validate_operation_id(dto.get("operation_id"))
    parsed["workspace_revision"] = _validate_workspace_revision(dto.get("workspace_revision"))
    return parsed


def _parse_workspace_choices(choices: Any) -> dict[str, Any]:
    """Strictly validate workspace choices (legacy fields + format fields)."""
    if not isinstance(choices, dict) or set(choices) != _WORKSPACE_CHOICES_KEYS:
        raise _invalid("choices 字段不完整或包含未知键")
    base = _parse_choices({key: choices[key] for key in _CHOICES_KEYS})
    content_mode = choices["content_mode"]
    if content_mode not in ("plain", "nga_ubb"):
        raise _invalid("content_mode 必须是 plain 或 nga_ubb")
    formatted_body = choices["formatted_body"]
    if formatted_body is not None and not isinstance(formatted_body, str):
        raise _invalid("formatted_body 必须是字符串或 null")
    if content_mode == "nga_ubb" and (
        not isinstance(formatted_body, str) or formatted_body.strip() == ""
    ):
        raise _invalid("nga_ubb 模式必须提供 formatted_body")
    confirmed = choices["plain_loss_confirmed"]
    if type(confirmed) is not bool:
        raise _invalid("plain_loss_confirmed 必须是布尔值")
    base["content_mode"] = content_mode
    base["formatted_body"] = formatted_body
    base["plain_loss_confirmed"] = confirmed
    return base


def _validate_operation_id(value: Any) -> str:
    """Operation id must be a canonical UUID."""
    import uuid as uuid_module

    if not isinstance(value, str):
        raise _invalid("operation_id 必须是字符串")
    try:
        parsed = uuid_module.UUID(value)
    except ValueError:
        raise _invalid("operation_id 必须是 UUID") from None
    if str(parsed) != value.lower():
        raise _invalid("operation_id 必须是规范小写 UUID")
    return value.lower()


def _validate_workspace_revision(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise _invalid("workspace_revision 必须是非负整数")
    return value


def _validate_candidate_file_name(value: Any) -> str:
    """Validate a single candidate basename; rejects paths and traversal."""
    if not isinstance(value, str) or value == "":
        raise _invalid("候选文件名必须是非空字符串")
    if value in (".", ".."):
        raise _invalid("候选文件名非法")
    if len(value) > _MAX_FILE_NAME:
        raise _invalid("候选文件名过长")
    if not value.endswith(".db"):
        raise _invalid("候选文件名必须以 .db 结尾")
    for char in value:
        if char in "/\\:\x00" or ord(char) < 32:
            raise _invalid("候选文件名含非法字符")
    return value


def _parse_choices(choices: Any) -> dict[str, Any]:
    """Strictly validate the choices container."""
    if not isinstance(choices, dict) or set(choices) != _CHOICES_KEYS:
        raise _invalid("choices 字段不完整或包含未知键")
    upload_time = choices["upload_time_seconds"]
    if type(upload_time) is not int or upload_time < 0 or upload_time > _MAX_UPLOAD_TIME:
        raise _invalid("upload_time_seconds 必须是 0..2147483647 的整数")
    note = choices["note"]
    if note is not None and not isinstance(note, str):
        raise _invalid("note 必须是字符串或 null")
    sources = choices["ordered_sources"]
    if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
        raise _invalid("ordered_sources 必须是字符串数组")
    tag_choices = choices["tag_choices"]
    if not isinstance(tag_choices, list):
        raise _invalid("tag_choices 必须是数组")
    parsed_choices: list[dict[str, Any]] = []
    for index, entry in enumerate(tag_choices):
        parsed_choices.append(_parse_tag_choice(entry, index))
    return {
        "upload_time_seconds": upload_time,
        "note": note,
        "ordered_sources": sources,
        "tag_choices": parsed_choices,
    }


def _parse_tag_choice(entry: Any, index: int) -> dict[str, Any]:
    """Strictly validate one tag choice entry."""
    if not isinstance(entry, dict) or set(entry) != _TAG_CHOICE_KEYS:
        raise _invalid(f"tag_choices[{index}] 字段不完整或包含未知键")
    kind = entry["requested_kind"]
    if kind not in ("tag", "series"):
        raise _invalid(f"tag_choices[{index}].requested_kind 必须是 tag 或 series")
    value = entry["requested_value"]
    if not isinstance(value, str) or value == "":
        raise _invalid(f"tag_choices[{index}].requested_value 必须是非空字符串")
    existing_id = entry["existing_tag_id"]
    if existing_id is not None and (type(existing_id) is not int or existing_id < 1):
        raise _invalid(f"tag_choices[{index}].existing_tag_id 必须是正整数")
    new_type = entry["new_tag_type"]
    if new_type is not None and (type(new_type) is not int or new_type not in (0, 1, 2, 3, 4)):
        raise _invalid(f"tag_choices[{index}].new_tag_type 必须是 0..4 的整数")
    if existing_id is not None and new_type is not None:
        raise _invalid(f"tag_choices[{index}] 不能同时指定 existing_tag_id 与 new_tag_type")
    return {
        "requested_kind": kind,
        "requested_value": value,
        "existing_tag_id": existing_id,
        "new_tag_type": new_type,
    }
