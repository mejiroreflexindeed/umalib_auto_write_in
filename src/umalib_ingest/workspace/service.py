"""TASK-013 工作区服务：HEAD/choices/交付/恢复与累计写入编排（M06/M07）。

只编排现有公共 API（Inbox、ApprovedPackage、Adapter、Writer）；不复制 SQL。
写入顺序固定（任务书 6.2）；恢复动作只允许 complete 与 abandon_unverified。
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from umalib_ingest.workspace.errors import (
    OperationConflictError,
    RecoveryRequiredError,
    WorkspaceBusyError,
    WorkspaceConflictError,
    WorkspaceError,
    WorkspaceManifestError,
)
from umalib_ingest.workspace.models import (
    ChoicesFile,
    DeliveryRecord,
    HeadBinding,
    OperationRecord,
    WorkspaceManifest,
    canonical_sha256,
    is_sha256,
    utc_now,
)
from umalib_ingest.workspace.operations import DeliveryLog, OperationLog
from umalib_ingest.workspace.paths import WorkspacePaths
from umalib_ingest.workspace.store import InstanceLock, WorkspaceStore

MAINTENANCE_FILE_NAME = "maintenance"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkspaceService:
    """One workspace at a time; the caller holds the instance lock."""

    def __init__(
        self,
        paths: WorkspacePaths,
        store: WorkspaceStore,
        real_source_db: Path | None,
        coordinator: "MaintenanceCoordinator | None" = None,
        repository_factory=None,
        package_service_factory=None,
    ) -> None:
        self._paths = paths
        self._store = store
        self._real_source_db = real_source_db
        self._coordinator = coordinator or MaintenanceCoordinator()
        from umalib_ingest.handoff import ApprovedPackageService
        from umalib_ingest.inbox import InboxRepository

        self._repository_factory = repository_factory or (lambda db: InboxRepository(db))
        self._package_factory = package_service_factory or (
            lambda repo: ApprovedPackageService(repo)
        )

    def _repo(self, workspace_id: str):
        """Fresh repository handle for the workspace inbox (path-bound only)."""
        return self._repository_factory(self._paths.inbox_db(workspace_id))

    def _packages(self, workspace_id: str):
        return self._package_factory(self._repo(workspace_id))

    @property
    def paths(self) -> WorkspacePaths:
        return self._paths

    def store(self) -> WorkspaceStore:
        return self._store

    def coordinator(self) -> MaintenanceCoordinator:
        return self._coordinator

    # ---- 生命周期 ----

    def create(self, mode: str, name: str) -> WorkspaceManifest:
        """Create workspace dir, manifest and Inbox; synthetic also gets base.db."""
        if self._coordinator.maintenance:
            raise WorkspaceBusyError()
        with self._coordinator.mutation():
            manifest = self._store.create(mode, name)
            self._repo(manifest.workspace_id).initialize()
            self._paths.choices_dir(manifest.workspace_id).mkdir(parents=True)
            self._paths.operations_dir(manifest.workspace_id).mkdir(parents=True)
            self._paths.candidates_dir(manifest.workspace_id).mkdir(parents=True)
            self._paths.deliveries_dir(manifest.workspace_id).mkdir(parents=True)
            if mode == "synthetic":
                from umalib_ingest.demo.samples import create_synthetic_base

                summary = create_synthetic_base(self._paths.base_db(manifest.workspace_id))
                updated = WorkspaceManifest.from_dict(manifest.to_dict())
                updated.base_sha256 = summary["sha256"]
                updated.updated_at = utc_now()
                atomic = self._atomic_manifest(updated)
                return atomic
            if self._real_source_db is None:
                # 源库尚未绑定（operator 产品在 source 向导中绑定，见 source-binding.json）
                updated = WorkspaceManifest.from_dict(manifest.to_dict())
                updated.updated_at = utc_now()
                return self._atomic_manifest(updated)
            base_sha = file_sha256(self._real_source_db)
            updated = WorkspaceManifest.from_dict(manifest.to_dict())
            updated.base_sha256 = base_sha
            updated.updated_at = utc_now()
            return self._atomic_manifest(updated)

    def open(self, workspace_id: str) -> tuple[WorkspaceManifest, InstanceLock]:
        """Load manifest and hold the instance lock until released."""
        manifest = self._store.load(workspace_id)
        lock = InstanceLock(self._paths.lock_file(workspace_id))
        lock.acquire()
        return manifest, lock

    def _atomic_manifest(self, new_manifest: WorkspaceManifest) -> WorkspaceManifest:
        """CAS-write the manifest using the current on-disk bytes."""
        current_bytes = self._store.load_bytes(new_manifest.workspace_id)
        current = self._store.load(new_manifest.workspace_id)
        return self._store.compare_and_swap(
            new_manifest.workspace_id,
            current.revision,
            canonical_sha256(current.to_dict()),
            new_manifest,
        )

    # ---- HEAD 解析 ----

    def resolve_head(self, workspace_id: str) -> tuple[HeadBinding, Path]:
        """Return the verified head binding and the real source DB path.

        synthetic: base.db or the last committed candidate.
        real_copy: fixed main-copy or the last committed candidate.
        """
        manifest = self._store.load(workspace_id)
        base_source = (
            self._real_source_db
            if manifest.mode == "real_copy"
            else self._paths.base_db(workspace_id)
        )
        head = manifest.head
        if head is None:
            if not base_source.exists():
                raise WorkspaceError("base_missing", "基础源数据库不存在")
            if manifest.mode == "synthetic" and manifest.base_sha256:
                if file_sha256(base_source) != manifest.base_sha256:
                    raise WorkspaceManifestError("合成基础库 hash 与 manifest 不一致")
            return HeadBinding(None, None, None, manifest.revision), base_source
        candidate = self._paths.candidates_dir(workspace_id) / head["candidate_file_name"]
        if not candidate.exists():
            raise WorkspaceError(
                "head_candidate_missing",
                f"HEAD 候选文件缺失: {head['candidate_file_name']}",
            )
        if file_sha256(candidate) != head["sha256"]:
            raise WorkspaceError("head_candidate_hash_mismatch", "HEAD 候选 hash 不符")
        return (
            HeadBinding(head["operation_id"], head["candidate_file_name"],
                        head["sha256"], manifest.revision),
            candidate,
        )

    def bind_real_source(
        self,
        workspace_id: str,
        source_db: Path,
        expected_sha256: str,
    ) -> WorkspaceManifest:
        """Bind one verified read-only source to an empty real-copy workspace.

        Operator first-run setup calls this only after explicit confirmation.  A
        workspace that already has a HEAD or a different base binding must not be
        silently repointed to another database.
        """
        source = Path(source_db).resolve()
        if not source.exists() or not source.is_file():
            raise WorkspaceError("base_missing", "基础源数据库不存在")
        actual_sha = file_sha256(source)
        if actual_sha != expected_sha256:
            raise WorkspaceError("base_changed", "源数据库在确认前发生变化，请重新校验")
        manifest = self._store.load(workspace_id)
        if manifest.mode != "real_copy":
            raise WorkspaceError("invalid_mode", "只有实际副本工作区可以绑定源数据库")
        if manifest.head is not None:
            raise WorkspaceError("workspace_has_head", "已有候选的工作区不能更换源数据库")
        if manifest.base_sha256 not in ("", actual_sha):
            raise WorkspaceError("source_binding_conflict", "工作区已绑定其他源数据库")
        updated = WorkspaceManifest.from_dict(manifest.to_dict())
        updated.base_sha256 = actual_sha
        updated.updated_at = utc_now()
        result = self._atomic_manifest(updated)
        self._real_source_db = source
        return result

    # ---- choices ----

    def choices_path(self, workspace_id: str, item_id: int) -> Path:
        return self._paths.choices_dir(workspace_id) / f"{item_id}.json"

    def load_choices(self, workspace_id: str, item_id: int) -> dict[str, Any] | None:
        path = self.choices_path(workspace_id, item_id)
        if not path.exists():
            return None
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            choices = ChoicesFile.from_dict(data)
        except (OSError, json.JSONDecodeError, WorkspaceManifestError):
            raise WorkspaceManifestError(f"choices 文件损坏: item {item_id}")
        item = self._repo(workspace_id).get(item_id)
        if item is None:
            return choices.to_dict()
        draft_sha = canonical_sha256(item.working_draft)
        package_sha = self._package_sha(workspace_id, item_id)
        stale = choices.working_draft_sha256 != draft_sha
        payload = choices.to_dict()
        payload["stale"] = stale
        payload["stale_reason"] = "稿件在保存决定后又被修改，需要复核" if stale else None
        payload["current_working_draft_sha256"] = draft_sha
        payload["current_package_sha256"] = package_sha
        return payload

    def save_choices(self, workspace_id: str, item_id: int, dto: dict[str, Any]) -> dict[str, Any]:
        """Save human decisions bound to the current working draft fingerprint."""
        if self._coordinator.maintenance:
            raise WorkspaceBusyError()
        with self._coordinator.mutation():
            item = self._repo(workspace_id).get(item_id)
            if item is None:
                raise WorkspaceError("item_not_found", "记录不存在")
            draft_sha = canonical_sha256(item.working_draft)
            package_sha = self._package_sha(workspace_id, item_id)
            choices = ChoicesFile.from_dict({
                **dto,
                "version": 1,
                "item_id": item_id,
                "working_draft_sha256": draft_sha,
                "package_sha256": package_sha,
                "updated_at": utc_now(),
            })
            from umalib_ingest.workspace.store import atomic_write_json

            atomic_write_json(self.choices_path(workspace_id, item_id), choices.to_dict())
            return choices.to_dict()

    def _package_sha(self, workspace_id: str, item_id: int) -> str | None:
        try:
            return self._packages(workspace_id).build(item_id).package_sha256
        except Exception:
            return None

    # ---- 交付 ----

    def list_deliveries(self, workspace_id: str) -> list[dict[str, Any]]:
        log = DeliveryLog(self._paths.deliveries_dir(workspace_id))
        manifest = self._store.load(workspace_id)
        latest_name = manifest.head["candidate_file_name"] if manifest.head else None
        out = []
        for record in log.list_records():
            payload = record.to_dict()
            payload["latest"] = record.candidate_file_name == latest_name
            out.append(payload)
        return out

    # ---- 写入编排（writer_ui 工作区分支调用） ----

    def get_operation(self, workspace_id: str, operation_id: str) -> OperationRecord | None:
        """Read one operation record (retry/recovery queries)."""
        log = OperationLog(self._paths.operations_dir(workspace_id))
        return log.get(operation_id)

    def begin_operation(
        self,
        workspace_id: str,
        operation_id: str,
        item_id: int,
        request_fingerprint: str,
        request_choices: dict | None,
        parent_revision: int,
        parent_head_sha256: str | None,
        candidate_file_name: str,
        plan_sha256: str,
        preview_sha256: str,
    ) -> OperationRecord:
        """Return an existing identical operation or atomically PREPARE a new one."""
        if self._coordinator.maintenance:
            raise WorkspaceBusyError()
        with self._coordinator.mutation():
            log = OperationLog(self._paths.operations_dir(workspace_id))
            existing = log.get(operation_id)
            if existing is not None:
                if existing.request_fingerprint != request_fingerprint:
                    raise OperationConflictError(operation_id)
                return existing
            manifest = self._store.load(workspace_id)
            if manifest.pending_operation_id is not None:
                pending = log.get(manifest.pending_operation_id)
                if pending is None:
                    raise RecoveryRequiredError(manifest.pending_operation_id)
                if pending.stage in ("PREPARED", "CANDIDATE_VERIFIED"):
                    raise RecoveryRequiredError(manifest.pending_operation_id)
            record = OperationRecord(
                operation_id=operation_id,
                stage="PREPARED",
                workspace_id=workspace_id,
                item_id=item_id,
                request_fingerprint=request_fingerprint,
                request_choices=request_choices,
                parent_revision=parent_revision,
                parent_head_sha256=parent_head_sha256,
                candidate_file_name=candidate_file_name,
                plan_sha256=plan_sha256,
                preview_sha256=preview_sha256,
                result_sha256=None,
                result=None,
                error=None,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            log.write(record)
            updated = WorkspaceManifest.from_dict(manifest.to_dict())
            updated.pending_operation_id = operation_id
            updated.updated_at = utc_now()
            self._atomic_manifest(updated)
            return record

    def record_verified(
        self,
        workspace_id: str,
        operation_id: str,
        result: dict[str, Any],
        candidate_file_name: str,
    ) -> OperationRecord:
        """Advance PREPARED -> CANDIDATE_VERIFIED with the full Writer result."""
        with self._coordinator.mutation():
            log = OperationLog(self._paths.operations_dir(workspace_id))
            record = log.get(operation_id)
            if record is None:
                raise WorkspaceError("operation_missing", "操作记录不存在")
            if record.stage != "PREPARED":
                return record
            updated = OperationRecord(
                operation_id=record.operation_id,
                stage="CANDIDATE_VERIFIED",
                workspace_id=record.workspace_id,
                item_id=record.item_id,
                request_fingerprint=record.request_fingerprint,
                request_choices=record.request_choices,
                parent_revision=record.parent_revision,
                parent_head_sha256=record.parent_head_sha256,
                candidate_file_name=candidate_file_name,
                plan_sha256=record.plan_sha256,
                preview_sha256=record.preview_sha256,
                result_sha256=result.get("result_sha256"),
                result=result,
                error=None,
                created_at=record.created_at,
                updated_at=utc_now(),
            )
            log.write(updated)
            return updated

    def commit_operation(
        self,
        workspace_id: str,
        operation_id: str,
        candidate_file_name: str,
        candidate_sha256: str,
        article_name: str,
        count_after: int,
        result_sha256: str,
    ) -> DeliveryRecord:
        """Advance HEAD (revision+1), mark COMMITTED and publish the delivery."""
        with self._coordinator.mutation():
            log = OperationLog(self._paths.operations_dir(workspace_id))
            record = log.get(operation_id)
            if record is None:
                raise WorkspaceError("operation_missing", "操作记录不存在")
            manifest = self._store.load(workspace_id)
            if manifest.pending_operation_id != operation_id:
                if manifest.head and manifest.head["operation_id"] == operation_id:
                    return self._delivery_for(workspace_id, operation_id)
                raise WorkspaceConflictError("pending 操作与当前操作不一致")
            updated = WorkspaceManifest.from_dict(manifest.to_dict())
            updated.revision = manifest.revision + 1
            updated.head = {
                "operation_id": operation_id,
                "candidate_file_name": candidate_file_name,
                "sha256": candidate_sha256,
            }
            updated.pending_operation_id = None
            updated.updated_at = utc_now()
            self._atomic_manifest(updated)
            if record.stage == "CANDIDATE_VERIFIED":
                committed = OperationRecord(
                    operation_id=record.operation_id,
                    stage="COMMITTED",
                    workspace_id=record.workspace_id,
                    item_id=record.item_id,
                    request_fingerprint=record.request_fingerprint,
                    request_choices=record.request_choices,
                    parent_revision=record.parent_revision,
                    parent_head_sha256=record.parent_head_sha256,
                    candidate_file_name=record.candidate_file_name,
                    plan_sha256=record.plan_sha256,
                    preview_sha256=record.preview_sha256,
                    result_sha256=record.result_sha256 or result_sha256,
                    result=record.result,
                    error=None,
                    created_at=record.created_at,
                    updated_at=utc_now(),
                )
                log.write(committed)
            delivery = DeliveryRecord(
                operation_id=operation_id,
                workspace_id=workspace_id,
                item_id=record.item_id,
                candidate_file_name=candidate_file_name,
                candidate_sha256=candidate_sha256,
                result_sha256=record.result_sha256 or result_sha256,
                article_name=article_name,
                count_after=count_after,
                created_at=utc_now(),
            )
            DeliveryLog(self._paths.deliveries_dir(workspace_id)).write(delivery)
            return delivery

    def _delivery_for(self, workspace_id: str, operation_id: str) -> DeliveryRecord:
        for record in DeliveryLog(self._paths.deliveries_dir(workspace_id)).list_records():
            if record.operation_id == operation_id:
                return record
        raise WorkspaceError("delivery_missing", "交付记录缺失")

    def abandon_operation(self, workspace_id: str, operation_id: str) -> OperationRecord:
        """Abandon an unverified PREPARED attempt; candidate file is kept."""
        with self._coordinator.mutation():
            log = OperationLog(self._paths.operations_dir(workspace_id))
            record = log.get(operation_id)
            if record is None:
                raise WorkspaceError("operation_missing", "操作记录不存在")
            if record.stage != "PREPARED":
                raise WorkspaceError("operation_not_abandonable",
                                     "只有未验证的准备中操作可以放弃")
            manifest = self._store.load(workspace_id)
            if manifest.pending_operation_id == operation_id:
                updated = WorkspaceManifest.from_dict(manifest.to_dict())
                updated.pending_operation_id = None
                updated.updated_at = utc_now()
                self._atomic_manifest(updated)
            abandoned = OperationRecord(
                operation_id=record.operation_id,
                stage="ABANDONED",
                workspace_id=record.workspace_id,
                item_id=record.item_id,
                request_fingerprint=record.request_fingerprint,
                request_choices=record.request_choices,
                parent_revision=record.parent_revision,
                parent_head_sha256=record.parent_head_sha256,
                candidate_file_name=record.candidate_file_name,
                plan_sha256=record.plan_sha256,
                preview_sha256=record.preview_sha256,
                result_sha256=record.result_sha256,
                result=record.result,
                error=None,
                created_at=record.created_at,
                updated_at=utc_now(),
            )
            log.write(abandoned)
            return abandoned

    # ---- 恢复诊断 ----

    def inspect_recovery(self, workspace_id: str) -> list[dict[str, Any]]:
        """Cross-check operation log vs manifest and return actionable states."""
        manifest = self._store.load(workspace_id)
        log = OperationLog(self._paths.operations_dir(workspace_id))
        records = {r.operation_id: r for r in log.list_records()}
        pending_id = manifest.pending_operation_id
        head_id = manifest.head["operation_id"] if manifest.head else None
        findings: list[dict[str, Any]] = []
        for record in records.values():
            if record.stage == "PREPARED":
                candidate = self._paths.candidates_dir(workspace_id) / record.candidate_file_name
                if candidate.exists():
                    findings.append({
                        "operation_id": record.operation_id,
                        "state": "RECOVERY_REQUIRED",
                        "reason": "PREPARED 但候选已存在且没有可信 Writer result",
                        "allowed_actions": ["abandon_unverified"],
                    })
                else:
                    findings.append({
                        "operation_id": record.operation_id,
                        "state": "ABANDONABLE",
                        "reason": "PREPARED 且候选不存在，可放弃",
                        "allowed_actions": ["abandon_unverified"],
                    })
            elif record.stage == "CANDIDATE_VERIFIED":
                item = self._repo(workspace_id).get(record.item_id)
                exported = item is not None and item.status == "exported"
                if head_id == record.operation_id:
                    findings.append({
                        "operation_id": record.operation_id,
                        "state": "COMPLETE_PENDING_LOG",
                        "reason": "HEAD 已推进但操作日志未收尾",
                        "allowed_actions": ["complete"],
                    })
                elif exported:
                    findings.append({
                        "operation_id": record.operation_id,
                        "state": "COMPLETE_PENDING_HEAD",
                        "reason": "Inbox 已 exported 但 HEAD 未推进",
                        "allowed_actions": ["complete"],
                    })
                else:
                    findings.append({
                        "operation_id": record.operation_id,
                        "state": "RECOVERY_REQUIRED",
                        "reason": "候选已验证但 Inbox 未 exported",
                        "allowed_actions": ["complete"],
                    })
        if pending_id is not None and pending_id not in records:
            findings.append({
                "operation_id": pending_id,
                "state": "PENDING_WITHOUT_LOG",
                "reason": "意图已落盘但操作记录缺失，需要人工核对",
                "allowed_actions": [],
            })
        if head_id is not None and head_id not in records:
            findings.append({
                "operation_id": head_id,
                "state": "HEAD_WITHOUT_LOG",
                "reason": "HEAD 已推进但日志缺失，需要人工核对",
                "allowed_actions": [],
            })
        return findings

    def recover(self, workspace_id: str, operation_id: str, action: str, confirmed: bool) -> dict[str, Any]:
        """Execute one explicit recovery action (complete / abandon_unverified)."""
        if type(confirmed) is not bool or confirmed is not True:
            raise WorkspaceError("recovery_not_confirmed", "恢复动作必须显式确认")
        if action not in ("complete", "abandon_unverified"):
            raise WorkspaceError("invalid_recovery_action", "不支持的恢复动作")
        if action == "abandon_unverified":
            self.abandon_operation(workspace_id, operation_id)
            return {"operation_id": operation_id, "action": action, "status": "ABANDONED"}
        findings = self.inspect_recovery(workspace_id)
        match = next((f for f in findings if f["operation_id"] == operation_id), None)
        if match is None:
            raise WorkspaceError("recovery_not_required", "该操作当前不需要恢复")
        if "complete" not in match["allowed_actions"]:
            raise WorkspaceError("recovery_action_not_allowed", f"当前状态不允许 {action}")
        # 补收尾：如果 Inbox 未 exported，由调用方先行补状态；这里只补 HEAD/日志/交付
        log = OperationLog(self._paths.operations_dir(workspace_id))
        record = log.get(operation_id)
        if record is None:
            raise WorkspaceError("operation_missing", "操作记录不存在")
        candidate = self._paths.candidates_dir(workspace_id) / record.candidate_file_name
        if not candidate.exists():
            raise WorkspaceError("candidate_missing", "候选文件缺失，无法完成恢复")
        sha = file_sha256(candidate)
        result = record.result or {}
        expected_candidate_sha = ((result.get("candidate") or {}).get("database_sha256")
                                  if isinstance(result.get("candidate"), dict) else None)
        if expected_candidate_sha and expected_candidate_sha != sha:
            raise WorkspaceError("candidate_tampered",
                                 "候选文件与已验证结果不一致，恢复被阻断")
        result_sha = record.result_sha256 or result.get("result_sha256", "")
        count_after = result.get("counts_after", {}).get("article")
        if count_after is None:
            raise WorkspaceError("recovery_result_incomplete", "缺少可信结果，无法完成恢复")
        manifest = self._store.load(workspace_id)
        if manifest.head and manifest.head["operation_id"] == operation_id:
            delivery = self._ensure_delivery(
                workspace_id, record, sha, result_sha, count_after,
            )
        else:
            delivery = self.commit_operation(
                workspace_id, operation_id, record.candidate_file_name, sha,
                str(result.get("article") or ""), int(count_after), result_sha,
            )
        return {"operation_id": operation_id, "action": action, "status": "COMMITTED",
                "delivery": delivery.to_dict()}

    def _ensure_delivery(
        self, workspace_id: str, record: OperationRecord, candidate_sha: str,
        result_sha: str, count_after: int,
    ) -> DeliveryRecord:
        delivery_log = DeliveryLog(self._paths.deliveries_dir(workspace_id))
        for delivery in delivery_log.list_records():
            if delivery.operation_id == record.operation_id:
                return delivery
        delivery = DeliveryRecord(
            operation_id=record.operation_id,
            workspace_id=workspace_id,
            item_id=record.item_id,
            candidate_file_name=record.candidate_file_name,
            candidate_sha256=candidate_sha,
            result_sha256=result_sha,
            article_name=str((record.result or {}).get("article") or ""),
            count_after=int(count_after),
            created_at=utc_now(),
        )
        delivery_log.write(delivery)
        committed = OperationRecord(
            operation_id=record.operation_id,
            stage="COMMITTED",
            workspace_id=record.workspace_id,
            item_id=record.item_id,
            request_fingerprint=record.request_fingerprint,
            request_choices=record.request_choices,
            parent_revision=record.parent_revision,
            parent_head_sha256=record.parent_head_sha256,
            candidate_file_name=record.candidate_file_name,
            plan_sha256=record.plan_sha256,
            preview_sha256=record.preview_sha256,
            result_sha256=record.result_sha256 or result_sha,
            result=record.result,
            error=None,
            created_at=record.created_at,
            updated_at=utc_now(),
        )
        OperationLog(self._paths.operations_dir(workspace_id)).write(committed)
        return delivery


class MaintenanceCoordinator:
    """Short-lived gate: maintenance flag + active mutation counter."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.maintenance = False
        self._active = 0

    def mutation(self):
        return _MutationScope(self)

    def begin_mutation(self) -> None:
        with self._lock:
            if self.maintenance:
                raise WorkspaceBusyError("工作区正在维护")
            self._active += 1

    def end_mutation(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    def enter_maintenance(self) -> bool:
        """True when maintenance was entered (no active mutations)."""
        with self._lock:
            if self.maintenance:
                return False
            if self._active > 0:
                raise WorkspaceBusyError("存在活动操作，无法进入维护")
            self.maintenance = True
            return True

    def leave_maintenance(self) -> None:
        with self._lock:
            self.maintenance = False


class _MutationScope:
    def __init__(self, coordinator: MaintenanceCoordinator) -> None:
        self._coordinator = coordinator

    def __enter__(self) -> "_MutationScope":
        self._coordinator.begin_mutation()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._coordinator.end_mutation()
