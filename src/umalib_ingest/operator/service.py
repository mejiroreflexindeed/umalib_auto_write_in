"""TASK-014 W01：HTTP 用例层（组合 threads/jobs/review/workspace/checkpoints）。"""

from __future__ import annotations

import datetime
import threading
import uuid
from pathlib import Path
from typing import Any

from umalib_ingest.operator import jobs as jobs_module
from umalib_ingest.operator.models import (
    JsonValidationError,
    SourceBinding,
    ThreadConfig,
    ThreadStart,
    canonical_json,
    canonical_sha256,
    utc_now,
)
from umalib_ingest.operator.paths import OperatorPaths
from umalib_ingest.operator.state_v2 import (
    WatcherStateV2,
    read_state_file,
)
from umalib_ingest.operator.thread_registry import ThreadRegistry, atomic_write_json


class OperatorError(Exception):
    def __init__(self, status: int, code: str, message: str,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable


class OperatorService:
    """Use-case layer; owns the workspace-bound services and the job service."""

    def __init__(
        self,
        paths: OperatorPaths,
        workspace_id: str,
        workspace_service,
        repository,
        review_service,
        writer_service,
        checkpoint_service,
        coordinator,
        profile_dir: Path,
        source_verifier,
        session_factory=None,
        nav_delay: float = 3.0,
    ) -> None:
        self.paths = paths
        self.workspace_id = workspace_id
        self._workspace = workspace_service
        self._repository = repository
        self._review = review_service
        self._writer = writer_service
        self._checkpoints = checkpoint_service
        self._coordinator = coordinator
        self._mutation_lock = threading.RLock()
        self._profile_dir = profile_dir
        self._source_verifier = source_verifier
        self._session_factory = session_factory
        self._nav_delay = nav_delay
        self._registry = ThreadRegistry(paths, workspace_id)
        profile_lock_path = None
        if profile_dir is not None:
            profile_lock_path = Path(str(profile_dir) + ".lock")
        self._jobs = jobs_module.NetworkJobService(self._persist_run,
                                                   profile_lock_path=profile_lock_path)
        self._jobs.register_executor("login", self._exec_login)
        self._jobs.register_executor("initialize_preview", self._exec_initialize_preview)
        self._jobs.register_executor("scan", self._exec_scan)
        self._jobs.register_executor("rescan", self._exec_rescan)
        self._restore_runs()
        self._jobs.start()
        self._previews: dict[str, dict[str, Any]] = {}
        self._latest_preview_run: dict[str, str] = {}
        self._login_transport_holder: dict[str, Any] = {}
        self._source_candidates: dict[str, dict[str, Any]] = {}
        self._detected_source: dict[str, Any] | None = None

    # ---- persistence ----

    def _persist_run(self, record) -> None:
        path = self.paths.run_json(record.workspace_id, record.tid, record.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, record.to_dict())

    def _restore_runs(self) -> None:
        """Load this workspace's persisted run history without resuming network."""
        import json

        from umalib_ingest.operator.models import RunRecord

        records = []
        threads_dir = self.paths.threads_dir(self.workspace_id)
        if threads_dir.exists():
            for path in sorted(threads_dir.glob("*/runs/*.json")):
                try:
                    records.append(RunRecord.from_dict(
                        json.loads(path.read_text(encoding="utf-8"))))
                except Exception:
                    # A damaged run log must not prevent offline access to the
                    # workspace; it remains on disk for diagnostics.
                    continue
        self._jobs.restore(records)

    def _submit_job(self, tid: str, kind: str, request: dict[str, Any]) -> str:
        try:
            return self._jobs.submit(self.workspace_id, tid, kind, request)
        except jobs_module.BusyError as exc:
            raise OperatorError(409, "network_busy", str(exc), retryable=True) from None

    # ---- maintenance gate ----

    def mutation_scope(self):
        """Shared coordinator gate: refuses writes during maintenance/backup."""
        from contextlib import contextmanager

        from umalib_ingest.workspace.errors import WorkspaceBusyError

        @contextmanager
        def _scope():
            try:
                with self._mutation_lock, self._coordinator.mutation():
                    yield
            except WorkspaceBusyError as exc:
                raise OperatorError(409, exc.code, exc.message, retryable=True) from None

        return _scope()

    def require_writable(self) -> None:
        if getattr(self._coordinator, "maintenance", False):
            raise OperatorError(503, "maintenance_active",
                                "工作区正在维护，请稍后再试", retryable=True)

    # ---- source binding ----

    def read_source_binding(self) -> SourceBinding | None:
        path = self.paths.source_binding(self.workspace_id)
        if not path.exists():
            return None
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        return SourceBinding.from_dict(data)

    def verify_source(self, source_path: Path) -> dict[str, Any]:
        verified = dict(self._source_verifier(source_path))
        token = str(uuid.uuid4())
        self._source_candidates[token] = verified
        return {**verified, "source_id": token}

    def register_detected_source(self, source_path: Path) -> dict[str, Any]:
        self._detected_source = self.verify_source(source_path)
        return dict(self._detected_source)

    def detected_source(self) -> dict[str, Any] | None:
        return dict(self._detected_source) if self._detected_source is not None else None

    def bind_source(self, source_id: str, name: str, confirmed: bool) -> dict[str, Any]:
        """Persist an explicitly verified source for the current empty workspace."""
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认使用此数据库副本")
        candidate = self._source_candidates.pop(source_id, None)
        if candidate is None:
            raise OperatorError(409, "source_token_stale", "源库校验已失效，请重新校验")
        try:
            current = self._source_verifier(Path(candidate["path"]))
        except Exception as exc:
            raise OperatorError(409, "source_not_ready", f"源库不可用：{exc}") from None
        if current["sha256"] != candidate["sha256"]:
            raise OperatorError(409, "source_changed", "源库在确认前发生变化，请重新校验")
        binding = SourceBinding(
            schema_version="1", source_id="sha256:" + current["sha256"],
            path=current["path"], file_name=current["file_name"],
            sha256=current["sha256"], size=current["size"],
            bound_at=utc_now(),
            schema_fingerprint_sha256=current["schema_fingerprint_sha256"],
        )
        with self.mutation_scope():
            self._workspace.bind_real_source(
                self.workspace_id, Path(binding.path), binding.sha256)
            atomic_write_json(self.paths.source_binding(self.workspace_id), binding.to_dict())
        return {"workspace_id": self.workspace_id, "source": binding.to_dict(),
                "restart_required": False}

    # ---- G01：工作区列表/切换/以其他副本新建 ----

    def list_workspaces(self) -> dict[str, Any]:
        """List existing workspaces (read-only) plus the current one."""
        from umalib_ingest.workspace.errors import WorkspaceError

        store = self._workspace.store()
        entries: list[dict[str, Any]] = []
        root = self.paths.workspaces_dir
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    manifest = store.load(entry.name)
                except WorkspaceError:
                    continue
                entries.append({
                    "workspace_id": manifest.workspace_id,
                    "name": manifest.name,
                    "mode": manifest.mode,
                    "created_at": manifest.created_at,
                    "updated_at": manifest.updated_at,
                    "current": manifest.workspace_id == self.workspace_id,
                })
        entries.sort(key=lambda item: item["updated_at"] or "", reverse=True)
        return {"workspaces": entries, "current": self.workspace_id}

    def select_workspace(self, workspace_id: str, confirmed: bool) -> dict[str, Any]:
        """Point the launcher at another existing workspace (restart to enter)."""
        from umalib_ingest.workspace.errors import WorkspaceError

        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认切换工作区")
        store = self._workspace.store()
        try:
            manifest = store.load(workspace_id)
        except WorkspaceError:
            raise OperatorError(404, "workspace_not_found", "工作区不存在") from None
        launcher = store.read_launcher()
        store.write_launcher({
            **launcher,
            "last_workspace_id": manifest.workspace_id,
            "last_workspace_by_mode": {
                **(launcher.get("last_workspace_by_mode") or {}),
                manifest.mode: manifest.workspace_id,
            },
        })
        return {"restart_required": True, "workspace_id": manifest.workspace_id,
                "name": manifest.name, "mode": manifest.mode}

    def create_workspace_from_source(self, source_id: str, name: str,
                                     confirmed: bool) -> dict[str, Any]:
        """Create a NEW workspace bound to another verified copy (restart to enter)."""
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认以该副本新建工作区")
        candidate = self._source_candidates.pop(source_id, None)
        if candidate is None:
            raise OperatorError(409, "source_token_stale", "源库校验已失效，请重新校验")
        try:
            current = self._source_verifier(Path(candidate["path"]))
        except Exception as exc:
            raise OperatorError(409, "source_not_ready", f"源库不可用：{exc}") from None
        if current["sha256"] != candidate["sha256"]:
            raise OperatorError(409, "source_changed", "源库在确认前发生变化，请重新校验")
        store = self._workspace.store()
        with self.mutation_scope():
            manifest = store.create("real_copy", (name or "新工作区").strip() or "新工作区")
            new_id = manifest.workspace_id
            binding = SourceBinding(
                schema_version="1", source_id="sha256:" + current["sha256"],
                path=current["path"], file_name=current["file_name"],
                sha256=current["sha256"], size=current["size"],
                bound_at=utc_now(),
                schema_fingerprint_sha256=current["schema_fingerprint_sha256"],
            )
            for sub in ("choices", "operations", "candidates", "deliveries",
                        "threads", "watcher"):
                self._workspace.paths.sub_path(new_id, sub).mkdir(
                    parents=True, exist_ok=True)
            # F012：新工作区必须完成 Inbox 初始化（复用既有初始化职责），
            # 否则重开进入该区后任何读取都会因 user_version=0 失败。
            from umalib_ingest.inbox import InboxRepository

            InboxRepository(self.paths.inbox_db(new_id)).initialize()
            atomic_write_json(self.paths.source_binding(new_id), binding.to_dict())
            fresh = store.load(new_id)
            updated = type(fresh).from_dict({
                **fresh.to_dict(), "base_sha256": current["sha256"], "updated_at": utc_now(),
            })
            store.compare_and_swap(new_id, fresh.revision,
                                   canonical_sha256(fresh.to_dict()), updated)
            launcher = store.read_launcher()
            store.write_launcher({
                **launcher,
                "last_workspace_id": new_id,
                "last_workspace_by_mode": {
                    **(launcher.get("last_workspace_by_mode") or {}), "real_copy": new_id,
                },
            })
        return {"restart_required": True, "workspace_id": new_id,
                "source": binding.to_dict()}

    # ---- threads ----

    def list_threads(self) -> dict[str, Any]:
        index = self._registry.read_index()
        return {"revision": index.revision, "active_tid": index.active_tid,
                "entries": [e.to_dict() for e in index.entries]}

    def add_thread(self, url: str, name: str | None,
                   expected_revision: int) -> dict[str, Any]:
        from umalib_ingest.operator.thread_registry import ConflictError

        try:
            with self.mutation_scope():
                entry = self._registry.add_thread(url, name, expected_revision)
        except ConflictError:
            raise OperatorError(409, "thread_conflict", "帖子列表已变化，请刷新") from None
        except (ValueError, JsonValidationError) as exc:
            raise OperatorError(422, "thread_url_invalid", str(exc)) from None
        index = self._registry.read_index()
        return {"entry": entry.to_dict(), "revision": index.revision}

    def patch_thread(self, tid: str, expected_revision: int, *, name=None,
                     enabled=None, make_active=None) -> dict[str, Any]:
        from umalib_ingest.operator.thread_registry import ConflictError

        try:
            with self.mutation_scope():
                index = self._registry.patch_thread(
                    tid, expected_revision, name=name, enabled=enabled,
                    make_active=make_active)
        except ConflictError:
            raise OperatorError(409, "thread_conflict", "帖子列表已变化，请刷新") from None
        except KeyError:
            raise OperatorError(404, "thread_not_found", "帖子不存在") from None
        except ValueError as exc:
            raise OperatorError(400, "thread_change_invalid", str(exc)) from None
        return {"revision": index.revision, "active_tid": index.active_tid,
                "entries": [e.to_dict() for e in index.entries]}

    def thread_status(self, tid: str) -> dict[str, Any]:
        config = self._registry.read_config(tid)
        state = None
        state_sha256 = None
        state_path = self.paths.state_json(self.workspace_id, tid)
        if state_path.exists():
            import hashlib

            state = read_state_file(state_path)
            state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()
        return {
            "tid": tid,
            "name": config.name,
            "enabled": config.enabled,
            "config_revision": config.revision,
            "start": config.start.to_dict(),
            "start_confirmed": config.start_confirmed,
            "initialized": config.initialized,
            "state": state.to_dict() if state else None,
            "state_sha256": state_sha256,
        }

    def manual_import(self, raw_text: str) -> dict[str, Any]:
        from umalib_ingest.parser import parse_submission

        if not isinstance(raw_text, str) or raw_text.strip() == "":
            raise OperatorError(400, "raw_text_invalid", "raw_text 必须是非空字符串")
        with self.mutation_scope():
            item = self._repository.import_draft(parse_submission(raw_text))
        return {"id": item.id, "status": item.status}

    # ---- job submit ----

    def initialize_preview(self, tid: str, start_dto: dict[str, Any],
                           expected_config_revision: int,
                           confirmed_network: bool) -> str:
        if type(confirmed_network) is not bool or confirmed_network is not True:
            raise OperatorError(400, "network_not_confirmed", "请确认允许访问 NGA")
        config = self._registry.read_config(tid)
        if not config.enabled:
            raise OperatorError(409, "thread_disabled", "帖子已停用，请先重新启用")
        if config.revision != expected_config_revision:
            raise OperatorError(409, "config_conflict", "帖子配置已变化，请刷新")
        start = self._validate_start(start_dto)
        request = {
            "start": start.to_dict(),
            "expected_config_revision": expected_config_revision,
        }
        return self._submit_job(tid, "initialize_preview", request)

    def initialize_confirm(self, tid: str, run_id: str, preview_sha256: str,
                           expected_config_revision: int, confirmed: bool) -> dict[str, Any]:
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认")
        preview = self._previews.get(run_id)
        if preview is None or preview.get("preview_sha256") != preview_sha256:
            raise OperatorError(409, "preview_stale", "预览已失效，请重新预览")
        if preview.get("tid") != tid:
            raise OperatorError(409, "preview_mismatch", "预览与目标帖子不一致，请重新预览")
        if preview.get("workspace_id") not in (None, self.workspace_id):
            raise OperatorError(409, "preview_mismatch", "预览与当前工作区不一致，请重新预览")
        run = self._jobs.get(run_id)
        if (run is None or run.tid != tid or run.workspace_id != self.workspace_id
                or run.kind != "initialize_preview" or run.status != "succeeded"
                or run.request.get("expected_config_revision") != expected_config_revision):
            raise OperatorError(409, "preview_stale", "预览状态或配置版本已变化，请重新预览")
        latest = self._latest_preview_run.get(tid)
        if latest is not None and latest != run_id:
            raise OperatorError(409, "preview_superseded",
                                "已有更新的起点预览，旧预览的迟到确认已拒绝")
        config = self._registry.read_config(tid)
        if not config.enabled:
            raise OperatorError(409, "thread_disabled", "帖子已停用，请先重新启用")
        if config.revision != expected_config_revision:
            raise OperatorError(409, "config_conflict", "帖子配置已变化，请刷新")
        # 写 config（start_confirmed+initialized）与 state v2；先写 state 再 CAS config，
        # CAS 失败回滚 state，避免"已初始化但无 state"的半套配置。
        from umalib_ingest.operator.thread_registry import ConflictError

        start = ThreadStart.from_dict(preview["start"])
        state_path = self.paths.state_json(self.workspace_id, tid)
        if state_path.exists():
            raise OperatorError(409, "state_exists", "扫描进度已存在，不能覆盖")
        with self.mutation_scope():
            if self._registry.read_config(tid).revision != config.revision:
                raise OperatorError(409, "config_conflict", "帖子配置已变化，请刷新")
            if state_path.exists():
                raise OperatorError(409, "state_exists", "扫描进度已存在，不能覆盖")
            state = self._build_initial_state(preview, start)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(state_path, state.to_dict())
            new_config = ThreadConfig(
                schema_version="1", revision=config.revision + 1, tid=config.tid,
                canonical_url=config.canonical_url, name=config.name, enabled=config.enabled,
                start=start, start_confirmed=True, initialized=True,
                created_at=config.created_at, updated_at=utc_now(),
            )
            try:
                self._registry.cas_config(tid, config.revision, new_config)
            except ConflictError:
                try:
                    state_path.unlink()
                except OSError:
                    pass
                raise OperatorError(409, "config_conflict", "帖子配置已变化，请刷新") from None
            except OSError:
                state_path.unlink(missing_ok=True)
                raise
        return {"tid": tid, "initialized": True,
                "state": state.to_dict(), "skip_note": preview["skip_note"]}

    def login(self, tid: str, confirmed_network: bool) -> str:
        if type(confirmed_network) is not bool or confirmed_network is not True:
            raise OperatorError(400, "network_not_confirmed", "请确认允许访问 NGA")
        config = self._registry.read_config(tid)
        if not config.enabled:
            raise OperatorError(409, "thread_disabled", "帖子已停用，请先重新启用")
        return self._submit_job(tid, "login", {"confirmed_network": True})

    def scan(self, tid: str, dry_run: bool, confirm_import: bool,
             expected_state_sha256: str) -> str:
        if type(confirm_import) is not bool or confirm_import is not True:
            raise OperatorError(400, "import_not_confirmed", "请确认允许导入")
        config = self._registry.read_config(tid)
        if not config.enabled:
            raise OperatorError(409, "thread_disabled", "帖子已停用，请先重新启用")
        if not config.initialized:
            raise OperatorError(409, "not_initialized", "请先初始化扫描进度")
        state_path = self.paths.state_json(self.workspace_id, tid)
        state = read_state_file(state_path)
        import hashlib

        actual = hashlib.sha256(state_path.read_bytes()).hexdigest()
        if actual != expected_state_sha256:
            raise OperatorError(409, "state_stale", "扫描进度已变化，请刷新")
        return self._submit_job(tid, "scan", {
            "dry_run": dry_run, "confirm_import": confirm_import,
            "expected_state_sha256": expected_state_sha256,
        })

    def rescan(self, tid: str, dto: dict[str, Any]) -> str:
        config = self._registry.read_config(tid)
        if not config.enabled:
            raise OperatorError(409, "thread_disabled", "帖子已停用，请先重新启用")
        if not config.initialized:
            raise OperatorError(409, "not_initialized", "请先初始化扫描进度")
        if type(dto.get("confirmed")) is not bool or dto["confirmed"] is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认")
        expected_state_sha256 = dto.get("expected_state_sha256")
        if not isinstance(expected_state_sha256, str) or len(expected_state_sha256) != 64:
            raise OperatorError(400, "state_sha_invalid", "expected_state_sha256 非法")
        state_path = self.paths.state_json(self.workspace_id, tid)
        import hashlib

        actual = hashlib.sha256(state_path.read_bytes()).hexdigest()
        if actual != expected_state_sha256:
            raise OperatorError(409, "state_stale", "扫描进度已变化，请刷新")
        kind = dto.get("kind")
        allowed = {"kind", "confirmed", "expected_state_sha256", "from_page", "to_page", "lower_bound_date"}
        if set(dto) - allowed:
            raise OperatorError(400, "rescan_invalid", "补查参数包含未知字段")
        request = {"confirmed": True, "kind": kind}
        if kind == "pages":
            first, last = dto.get("from_page"), dto.get("to_page")
            if (type(first) is not int or type(last) is not int or first < 1
                    or last < first or dto.get("lower_bound_date") is not None):
                raise OperatorError(400, "rescan_invalid", "补查页范围必须是递增的正整数，且不能混填日期")
            observed = read_state_file(state_path).observed_last_page
            if observed is None or last > observed:
                raise OperatorError(422, "rescan_invalid", "补查范围超过已确认的帖子尾页")
            request.update({"from_page": dto.get("from_page"), "to_page": dto.get("to_page"),
                            "lower_bound_date": None,
                            "expected_state_sha256": dto.get("expected_state_sha256")})
        elif kind == "date":
            if dto.get("from_page") is not None or dto.get("to_page") is not None:
                raise OperatorError(400, "rescan_invalid", "日期补查不能混填页范围")
            self._validate_start({"mode": "date", "page": None,
                                  "date": dto.get("lower_bound_date"), "timezone": "UTC+08:00"})
            request.update({"from_page": None, "to_page": None,
                            "lower_bound_date": dto.get("lower_bound_date"),
                            "expected_state_sha256": dto.get("expected_state_sha256")})
        else:
            raise OperatorError(400, "rescan_invalid", "补查类型非法")
        return self._submit_job(tid, "rescan", request)

    def get_run(self, run_id: str) -> dict[str, Any]:
        record = self._jobs.get(run_id)
        if record is None:
            raise OperatorError(404, "run_not_found", "任务不存在")
        return record.to_dict()

    def login_confirm(self, run_id: str, confirmed: bool) -> dict[str, Any]:
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认")
        try:
            record = self._jobs.login_confirm(run_id)
        except jobs_module.NotAwaitingError as exc:
            raise OperatorError(409, "not_awaiting_login", str(exc)) from None
        return {"run_id": run_id, "status": record.status if record else "unknown"}

    def cancel(self, run_id: str) -> dict[str, Any]:
        record = self._jobs.cancel(run_id)
        if record is None:
            raise OperatorError(404, "run_not_found", "任务不存在")
        return {"run_id": run_id, "status": record.status}

    def shutdown(self, confirmed: bool) -> dict[str, Any]:
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认")
        self._jobs.shutdown()
        return {"shutdown": True}

    # ---- W11：独立预览与累计候选 ----

    def _previews_dir(self) -> Path:
        return self.paths._ws(self.workspace_id) / "previews"

    def writer_preview(self, item_id: int, candidate_file_name: str) -> dict[str, Any]:
        from umalib_ingest.web.writer_ui import WriterUiError
        from umalib_ingest.workspace.models import canonical_sha256

        self.require_writable()
        if not isinstance(candidate_file_name, str) or not candidate_file_name.endswith(".db") \
                or "/" in candidate_file_name or "\\" in candidate_file_name:
            raise OperatorError(400, "candidate_invalid", "候选文件名必须是安全 basename.db")
        item = self._repository.get(item_id)
        if item is None:
            raise OperatorError(404, "item_not_found", "记录不存在")
        choices = self._workspace.load_choices(self.workspace_id, item_id)
        if choices is None:
            raise OperatorError(409, "choices_missing", "请先保存人工决定")
        if choices.get("stale"):
            raise OperatorError(409, "choices_stale", "稿件已修改，请重新保存决定")
        operation_id = str(uuid.uuid4())
        preview_id = str(uuid.uuid4())
        # candidate basename 必须等于 candidate-<operation_id>.db
        expected_name = f"candidate-{operation_id}.db"
        choices_dto = {
            "upload_time_seconds": choices["upload_time_seconds"],
            "note": choices["note"],
            "ordered_sources": choices["ordered_sources"],
            "tag_choices": choices["tag_choices"],
            "content_mode": choices["content_mode"],
            "formatted_body": choices["formatted_body"],
            "plain_loss_confirmed": choices["plain_loss_confirmed"],
        }
        try:
            result = self._writer.preview(
                item_id, {"candidate_file_name": expected_name, "choices": choices_dto},
                self.workspace_id)
        except WriterUiError as exc:
            raise OperatorError(exc.status, exc.code, exc.message) from None
        plan = result["plan"]
        preview = result["preview"]
        head, _ = self._workspace.resolve_head(self.workspace_id)
        record = {
            "schema_version": "1", "preview_id": preview_id,
            "operation_id": operation_id, "workspace_id": self.workspace_id,
            "item_id": item_id, "candidate_file_name": expected_name,
            "choices_sha256": canonical_sha256(choices_dto),
            "working_draft_sha256": canonical_sha256(item.working_draft),
            "plan_sha256": plan["plan_sha256"],
            "preview_sha256": preview["preview_sha256"],
            "source_database_sha256": plan["input"]["target_database_sha256"],
            "workspace_revision": head.revision,
            "ready_to_write": preview["ready_to_write"],
            "created_at": utc_now(),
        }
        previews_dir = self._previews_dir()
        previews_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(previews_dir / f"{preview_id}.json", record)
        return {
            "preview_id": preview_id,
            "operation_id": operation_id,
            "plan": plan, "preview": preview,
            "expected_plan_sha256": record["plan_sha256"],
            "expected_preview_sha256": record["preview_sha256"],
            "expected_source_database_sha256": record["source_database_sha256"],
            "workspace_revision": record["workspace_revision"],
        }

    def writer_write(self, item_id: int, preview_id: str,
                     app_closed_confirmed: bool) -> dict[str, Any]:
        import json

        from umalib_ingest.web.writer_ui import WriterUiError
        from umalib_ingest.workspace.models import canonical_sha256

        self.require_writable()
        if type(app_closed_confirmed) is not bool or app_closed_confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请确认大书库已关闭")
        path = self._previews_dir() / f"{preview_id}.json"
        if not path.exists():
            raise OperatorError(404, "preview_not_found", "预览不存在")
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("item_id") != item_id:
            raise OperatorError(409, "preview_stale", "预览与稿件不一致")
        item = self._repository.get(item_id)
        if item is None:
            raise OperatorError(404, "item_not_found", "记录不存在")
        if record["working_draft_sha256"] != canonical_sha256(item.working_draft):
            raise OperatorError(409, "preview_stale", "稿件已修改，请重新预览")
        choices = self._workspace.load_choices(self.workspace_id, item_id)
        if choices is None or choices.get("stale"):
            raise OperatorError(409, "preview_stale", "人工决定已变化，请重新预览")
        choices_dto = {
            "upload_time_seconds": choices["upload_time_seconds"],
            "note": choices["note"],
            "ordered_sources": choices["ordered_sources"],
            "tag_choices": choices["tag_choices"],
            "content_mode": choices["content_mode"],
            "formatted_body": choices["formatted_body"],
            "plain_loss_confirmed": choices["plain_loss_confirmed"],
        }
        if record["choices_sha256"] != canonical_sha256(choices_dto):
            raise OperatorError(409, "preview_stale", "人工决定已变化，请重新预览")
        write_dto = {
            "candidate_file_name": record["candidate_file_name"],
            "choices": choices_dto,
            "expected_plan_sha256": record["plan_sha256"],
            "expected_preview_sha256": record["preview_sha256"],
            "expected_source_database_sha256": record["source_database_sha256"],
            "app_closed_confirmed": True,
            "operation_id": record["operation_id"],
            "workspace_revision": record["workspace_revision"],
        }
        try:
            result = self._writer.write(item_id, write_dto, self.workspace_id)
        except WriterUiError as exc:
            raise OperatorError(exc.status, exc.code, exc.message) from None
        item = result.get("item")
        if item is not None and not isinstance(item, dict):
            result["item"] = {
                "id": getattr(item, "id", None),
                "status": getattr(item, "status", None),
            }
        return result

    def delivery_view_copy(self, operation_id: str, confirmed: bool) -> dict[str, Any]:
        """Create a disposable DB for opening in UmaLib; preserve the candidate."""
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认生成查看副本")
        import os
        import shutil
        import tempfile
        from umalib_ingest.workspace.service import file_sha256

        delivery = next((entry for entry in self._workspace.list_deliveries(
            self.workspace_id) if entry.get("operation_id") == operation_id), None)
        if delivery is None:
            raise OperatorError(404, "delivery_not_found", "交付记录不存在")
        source = self._workspace.paths.candidates_dir(
            self.workspace_id) / delivery["candidate_file_name"]
        if not source.is_file() or file_sha256(source) != delivery["candidate_sha256"]:
            raise OperatorError(409, "delivery_changed", "权威候选缺失或哈希不符")
        views = self._workspace.paths.workspace_dir(self.workspace_id) / "views"
        views.mkdir(parents=True, exist_ok=True)
        target = views / f"查看副本-{operation_id}.db"
        if target.exists():
            if file_sha256(target) != delivery["candidate_sha256"]:
                raise OperatorError(409, "view_copy_conflict", "同名查看副本已被修改")
        else:
            fd, temp_name = tempfile.mkstemp(dir=str(views), prefix=".view-", suffix=".tmp")
            os.close(fd)
            try:
                shutil.copyfile(source, temp_name)
                if file_sha256(Path(temp_name)) != delivery["candidate_sha256"]:
                    raise OSError("查看副本复制后哈希不一致")
                os.replace(temp_name, target)
            except BaseException:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass
                raise
        return {"operation_id": operation_id, "view_copy_path": str(target.resolve()),
                "candidate_path": str(source.resolve()),
                "sha256": delivery["candidate_sha256"]}

    # ---- checkpoints（D5：恢复后更新产品级 launcher 选中项） ----

    def create_checkpoint(self, confirmed: bool) -> dict[str, Any]:
        return self._checkpoints.create(self.workspace_id, confirmed)

    def restore_checkpoint(self, checkpoint_id: str, confirmed: bool) -> dict[str, Any]:
        result = self._checkpoints.restore(self.workspace_id, checkpoint_id, confirmed)
        new_id = result["new_workspace_id"]
        store = self._workspace.store()
        launcher = store.read_launcher()
        store.write_launcher({
            **launcher,
            "last_workspace_id": new_id,
            "last_workspace_by_mode": {
                **(launcher.get("last_workspace_by_mode") or {}),
            },
        })
        return result

    def open_delivery_folder(self, operation_id: str, confirmed: bool) -> dict[str, Any]:
        """Open only the verified candidate directory; never accept a client path."""
        if type(confirmed) is not bool or confirmed is not True:
            raise OperatorError(400, "not_confirmed", "请明确确认打开目录")
        delivery = next((entry for entry in self._workspace.list_deliveries(
            self.workspace_id) if entry.get("operation_id") == operation_id), None)
        if delivery is None:
            raise OperatorError(404, "delivery_not_found", "交付记录不存在")
        candidate = self._workspace.paths.candidates_dir(
            self.workspace_id) / delivery["candidate_file_name"]
        if not candidate.is_file():
            raise OperatorError(409, "delivery_missing", "候选文件不存在")
        import os

        try:
            os.startfile(str(candidate.parent))
        except (AttributeError, OSError):
            raise OperatorError(503, "folder_open_failed", "无法打开候选所在目录") from None
        return {"opened": True, "directory": str(candidate.parent.resolve())}


    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        from umalib_ingest.nga_access.browser import launch_probe_session

        return launch_probe_session(self._profile_dir)

    def _exec_login(self, record, controller) -> None:
        from umalib_ingest.operator.scan_service import Transport

        self._registry.read_config(record.tid)
        controller.check_cancel()
        snap = self.paths.snapshots_dir(self.workspace_id, record.tid)
        snap.mkdir(parents=True, exist_ok=True)
        transport = Transport(self._session, snap, record.tid, delay=self._nav_delay,
                              controller=controller)
        try:
            page = transport.fetch(1)
            if "manual_login_required" in page.diagnostics:
                controller.login_required()
                confirmed = controller.wait_login(300.0)
                if not confirmed:
                    controller.set_status("failed", error={
                        "code": "login_timeout",
                        "message": "等待登录超时，未完成登录验证",
                        "retryable": True,
                    })
                    return
                page = transport.fetch(1)
            blockers = [code for code in page.diagnostics if code in (
                "thread_forbidden", "thread_not_found", "captcha_or_challenge",
                "manual_login_required", "unexpected_redirect",
                "response_not_captured", "floor_structure_unrecognized",
                "encoding_uncertain",
            )]
            if int(page.tid) != int(record.tid):
                blockers.append("tid_mismatch")
            if page.last_page is None:
                blockers.append("page_structure_unverified")
            if blockers:
                controller.set_status("failed", error={
                    "code": "login_failed",
                    "message": "登录未完成或目标不可访问: " + ", ".join(blockers),
                    "retryable": True,
                })
                return
            controller.set_status("succeeded", result={
                "pages_scanned": 1, "floors_seen": len(page.floors),
                "discussion_count": 0, "triggered_count": 0, "imported_count": 0,
                "incomplete_count": 0, "updated_count": 0,
                "date_excluded_count": 0, "date_unresolved_count": 0,
                "remaining_pages": 0, "state_sha256": "",
                "login_verified": True,
            })
        finally:
            transport.close()

    def _exec_initialize_preview(self, record, controller) -> None:
        from umalib_ingest.operator.scan_service import Transport, initialize_preview

        config = self._registry.read_config(record.tid)
        request_start = record.request.get("start") or {}
        start = ThreadStart.from_dict(request_start)
        snap = self.paths.snapshots_dir(self.workspace_id, record.tid)
        snap.mkdir(parents=True, exist_ok=True)
        controller.check_cancel()
        transport = Transport(self._session, snap, record.tid, delay=self._nav_delay,
                              controller=controller)
        try:
            preview = initialize_preview(config, transport, start=start)
            preview["start"] = start.to_dict()
            preview["workspace_id"] = self.workspace_id
            preview["preview_sha256"] = canonical_sha256({
                "tid": record.tid, "last_page": preview["observed_last_page"],
                "floor_pids": preview["floor_pids"], "start": start.to_dict(),
            })
            self._previews[record.run_id] = preview
            self._latest_preview_run[record.tid] = record.run_id
            controller.set_status("succeeded", result=preview)
        finally:
            transport.close()

    def _exec_scan(self, record, controller) -> None:
        self._run_scan_job(record, controller, dry_run=record.request.get("dry_run", False))

    def _exec_rescan(self, record, controller) -> None:
        request = record.request
        kind = request.get("kind")
        lower = None
        if request.get("lower_bound_date"):
            lower = datetime.datetime.strptime(
                request["lower_bound_date"], "%Y-%m-%d").replace(
                tzinfo=datetime.timezone(datetime.timedelta(hours=8))).astimezone(
                datetime.timezone.utc)
        state_path = self.paths.state_json(self.workspace_id, record.tid)
        state = read_state_file(state_path)
        active = state.active_rescan
        resume = False
        if kind == "date":
            from_page = 1
            to_page = state.observed_last_page
            if (active is not None and active.kind == "date"
                    and active.lower_bound_utc == (lower.isoformat() if lower else None)
                    and active.next_page):
                from_page = active.next_page
                to_page = active.to_page
                resume = True
        else:
            from_page = request.get("from_page")
            to_page = request.get("to_page")
            if (active is not None and active.kind == "pages"
                    and active.from_page == from_page and active.to_page == to_page
                    and active.next_page):
                from_page = active.next_page
                resume = True
        self._run_scan_job(
            record, controller, dry_run=False,
            from_page=from_page, to_page=to_page,
            date_lower_bound=lower, rescan=True,
            rescan_id=active.rescan_id if resume else record.run_id, rescan_kind=kind,
            rescan_from_page=active.from_page if resume else from_page)

    def _run_scan_job(self, record, controller, *, dry_run, from_page=None,
                      to_page=None, date_lower_bound=None, rescan=False,
                      rescan_id=None, rescan_kind=None, rescan_from_page=None) -> None:
        from umalib_ingest.operator.scan_service import Transport, run_batch_scan

        config = self._registry.read_config(record.tid)
        state_path = self.paths.state_json(self.workspace_id, record.tid)
        import hashlib
        import json

        expected_state_sha256 = record.request.get("expected_state_sha256")
        with self._mutation_lock:
            state_bytes = state_path.read_bytes()
            if expected_state_sha256 != hashlib.sha256(state_bytes).hexdigest():
                raise OperatorError(409, "state_stale", "任务开始前扫描进度已变化，请刷新")
            state = WatcherStateV2.from_dict(json.loads(state_bytes))
        snap = self.paths.snapshots_dir(self.workspace_id, record.tid)
        snap.mkdir(parents=True, exist_ok=True)
        controller.check_cancel()
        transport = Transport(self._session, snap, record.tid, delay=self._nav_delay,
                              controller=controller)
        def persist_page(new_state):
            nonlocal expected_state_sha256
            if hashlib.sha256(state_path.read_bytes()).hexdigest() != expected_state_sha256:
                raise OperatorError(409, "state_stale", "扫描期间进度文件发生变化，未写入结果")
            atomic_write_json(state_path, new_state.to_dict())
            expected_state_sha256 = hashlib.sha256(state_path.read_bytes()).hexdigest()

        try:
            # Every normal scan probes page 1 first so newly appended pages are
            # observable.  A stale initialization tail must never become a
            # permanent upper bound.  The probe doubles as the first page read
            # when the cursor starts at page 1 (single-page threads read once).
            prefetched = None
            if not rescan:
                probe = transport.fetch(1)
                if int(probe.tid) != int(record.tid) or probe.last_page is None:
                    raise OperatorError(409, "page_probe_failed", "无法确认帖子当前页数")
                state = WatcherStateV2(
                    schema_version=state.schema_version, tid=state.tid,
                    canonical_thread_url=state.canonical_thread_url,
                    frontier_page=state.frontier_page,
                    observed_last_page=probe.last_page,
                    next_scan_page=state.next_scan_page,
                    known_floors=state.known_floors,
                    date_exclusions=state.date_exclusions,
                    baseline_created_at=state.baseline_created_at,
                    last_successful_scan_at=state.last_successful_scan_at,
                    last_result_summary=state.last_result_summary,
                    active_rescan=state.active_rescan,
                )
                if from_page is None and state.next_scan_page == 1:
                    prefetched = probe
            if date_lower_bound is None and config.start.mode == "date":
                date_lower_bound = datetime.datetime.fromisoformat(
                    config.start.lower_bound_utc)
            new_state, counters = run_batch_scan(
                state, config, transport,
                self.paths.inbox_db(self.workspace_id),
                dry_run=dry_run, controller=controller,
                from_page=from_page, to_page=to_page,
                date_lower_bound=date_lower_bound, rescan=rescan,
                rescan_id=rescan_id, rescan_kind=rescan_kind,
                rescan_from_page=rescan_from_page,
                write_gate=self.mutation_scope, persist_page=persist_page,
                prefetched=prefetched,
            )
            counters["state_sha256"] = hashlib.sha256(
                state_path.read_bytes()).hexdigest() if not dry_run else ""
            if counters["remaining_pages"] > 0:
                controller.set_status("partial", result=counters)
            else:
                controller.set_status("succeeded", result=counters)
        finally:
            transport.close()

    # ---- helpers ----

    def _validate_start(self, dto: dict[str, Any]) -> ThreadStart:
        from umalib_ingest.operator.models import utc_to_shanghai_lower_bound

        lower_bound = dto.get("lower_bound_utc")
        if dto.get("mode") == "date" and lower_bound is None \
                and isinstance(dto.get("date"), str):
            try:
                lower_bound = utc_to_shanghai_lower_bound(dto["date"])
            except ValueError:
                pass
        try:
            start = ThreadStart.from_dict({
                "mode": dto.get("mode"), "page": dto.get("page"),
                "date": dto.get("date"), "timezone": dto.get("timezone", "UTC+08:00"),
                "lower_bound_utc": lower_bound,
            })
            today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
            if start.mode == "date" and datetime.date.fromisoformat(start.date) > today:
                raise ValueError("起始日期不能晚于今天（UTC+08:00）")
            return start
        except (JsonValidationError, ValueError) as exc:
            raise OperatorError(422, "start_invalid", str(exc)) from None

    def _build_initial_state(self, preview: dict[str, Any], start: ThreadStart) -> WatcherStateV2:
        from umalib_ingest.operator.state_v2 import WatcherStateV2

        last_page = preview["observed_last_page"]
        if start.mode == "now":
            frontier = last_page
            next_page = last_page
            known = {}
            for item in preview.get("tail_floor_hashes", []):
                known[item["pid"]] = {
                    "pid": item["pid"], "content_sha256": item["content_sha256"],
                    "last_seen_page": item["last_seen_page"],
                    "trigger_found": False, "inbox_item_id": None,
                    "imported_content_sha256": None, "changed_after_import": False,
                    "has_trigger": bool(item.get("has_trigger")),
                }
        elif start.mode == "page":
            frontier = start.page - 1
            next_page = start.page
            known = {}
        else:
            frontier = 0
            next_page = 1
            known = {}
        return WatcherStateV2(
            schema_version="2", tid=preview["tid"],
            canonical_thread_url=f"https://bbs.nga.cn/read.php?tid={preview['tid']}",
            frontier_page=frontier, observed_last_page=last_page,
            next_scan_page=next_page, known_floors=known, date_exclusions={},
            baseline_created_at=utc_now(), last_successful_scan_at=None,
            last_result_summary=None, active_rescan=None,
        )
