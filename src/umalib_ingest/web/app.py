"""Local review web UI (TASK-004, extended by TASK-009)."""

from __future__ import annotations

import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from umalib_ingest.domain.models import SubmissionOrigin
from umalib_ingest.handoff import ApprovedPackageService, HandoffNotReadyError
from umalib_ingest.inbox import (
    InboxItemLockedError,
    InboxItemNotFoundError,
    InboxRepository,
    InvalidStatusTransitionError,
    VALID_STATUSES,
)
from umalib_ingest.inbox.repository import EDITABLE_STATUSES
from umalib_ingest.parser import parse_submission
from umalib_ingest.review import (
    InvalidReviewFieldsError,
    ReviewFields,
    ReviewService,
    ReviewSources,
)
from umalib_ingest.umalib_writer import UmaLibWriterError
from umalib_ingest.umalib_writer.file_state import validate_source_path
from umalib_ingest.web.watcher_ui import WatcherUiConfig, WatcherUiError, WatcherUiService
from umalib_ingest.web.writer_ui import WriterUiConfig, WriterUiError, WriterUiService

MAX_BODY_BYTES = 2 * 1024 * 1024
HOSTNAME = "127.0.0.1"

_STATIC_DIR = Path(__file__).parent / "static"

_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/demo.js": ("demo.js", "application/javascript; charset=utf-8"),
    "/demo.css": ("demo.css", "text/css; charset=utf-8"),
}

_ITEM_RE = re.compile(r"^/api/items/(\d+)$")
_ITEM_ACTION_RE = re.compile(r"^/api/items/(\d+)/(review|transition)$")
_ITEM_PACKAGE_RE = re.compile(r"^/api/items/(\d+)/approved-package$")
_ITEM_WRITER_PREVIEW_RE = re.compile(r"^/api/items/(\d+)/writer/preview$")
_ITEM_WRITER_WRITE_RE = re.compile(r"^/api/items/(\d+)/writer/write$")
_DEMO_OPERATION_RE = re.compile(r"^/api/demo/operations/([0-9a-fA-F-]{36})$")
_DEMO_CHECKPOINT_RESTORE_RE = re.compile(
    r"^/api/demo/checkpoints/([0-9a-fA-F-]{36})/restore$"
)
_DEMO_ITEM_CHOICES_RE = re.compile(r"^/api/demo/items/(\d+)/choices$")
_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?$", re.IGNORECASE)
_CONTENT_LENGTH_RE = re.compile(r"^[0-9]+$")
_REVIEW_KEYS = {"title", "author", "translator", "series", "tags", "sources", "body_text"}
_SOURCE_KEYS = {"nga", "original_series", "original", "translation_series", "translation"}
_IMPORT_KEYS = {"raw_text", "canonical_url"}
_TRANSITION_KEYS = {"to_status", "reason"}

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


class _ReviewHandler(BaseHTTPRequestHandler):
    """HTTP handler for the review UI; repository attached per server instance."""

    protocol_version = "HTTP/1.0"
    repository: InboxRepository
    review_service: ReviewService
    demo_service: Any = None

    def log_message(self, format: str, *args: object) -> None:
        pass

    # ---- dispatch ----

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except Exception:
            self._send_error(500, "internal_error", "服务器内部错误")

    def do_POST(self) -> None:
        try:
            self._handle_write("POST")
        except Exception:
            self._send_error(500, "internal_error", "服务器内部错误")

    def do_PUT(self) -> None:
        try:
            self._handle_write("PUT")
        except Exception:
            self._send_error(500, "internal_error", "服务器内部错误")

    # ---- routing ----

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _handle_get(self) -> None:
        if not self._check_host():
            return
        path = self._path()
        if path == "/api/items":
            self._handle_list_items()
            return
        if path == "/api/writer/config":
            self._send_json(200, self.writer_service.config_dict())
            return
        if path == "/api/writer/catalog":
            try:
                self._send_json(200, self.writer_service.catalog())
            except WriterUiError as exc:
                self._send_error(exc.status, exc.code, exc.message)
            return
        if path == "/api/watcher/config":
            self._send_json(200, self.watcher_service.config_dict())
            return
        if self.demo_service is not None and path.startswith("/api/demo/"):
            _DemoRoutes._handle_demo_get(self, path)
            return
        if path == "/api/watcher/status":
            try:
                self._send_json(200, self.watcher_service.status())
            except WatcherUiError as exc:
                self._send_error(exc.status, exc.code, exc.message)
            return
        package_match = _ITEM_PACKAGE_RE.match(path)
        if package_match:
            self._handle_approved_package(int(package_match.group(1)))
            return
        match = _ITEM_RE.match(path)
        if match:
            item = self.repository.get(int(match.group(1)))
            if item is None:
                self._send_error(404, "item_not_found", "记录不存在")
                return
            self._send_json(200, self._item_detail(item))
            return
        if path.startswith("/api/items/"):
            self._send_error(400, "invalid_request", "路径 ID 非法")
            return
        if path.startswith("/api/"):
            self._send_error(404, "not_found", "路径不存在")
            return
        if path in _STATIC_FILES:
            self._send_static(path)
            return
        self._send_error(404, "not_found", "路径不存在")

    def _handle_write(self, method: str) -> None:
        if not self._check_host():
            return
        raw_body = self._read_body_bytes()
        if raw_body is None:
            return
        if not self._check_origin():
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._send_error(415, "unsupported_media_type", "写请求必须使用 application/json")
            return
        payload = self._parse_json_body(raw_body)
        if payload is None:
            return
        path = self._path()
        if self.demo_service is not None and path.startswith("/api/demo/"):
            _DemoRoutes._handle_demo_write(self, method, path, payload)
            return
        if method == "POST" and path == "/api/import":
            self._handle_import(payload)
            return
        if method == "POST" and path == "/api/watcher/scan":
            self._handle_watcher_scan(payload)
            return
        if method == "POST" and path == "/api/watcher/login-confirm":
            self._handle_watcher_login_confirm(payload)
            return
        writer_preview_match = _ITEM_WRITER_PREVIEW_RE.match(path)
        if writer_preview_match is not None and method == "POST":
            self._handle_writer_preview(int(writer_preview_match.group(1)), payload)
            return
        writer_write_match = _ITEM_WRITER_WRITE_RE.match(path)
        if writer_write_match is not None and method == "POST":
            self._handle_writer_write(int(writer_write_match.group(1)), payload)
            return
        action_match = _ITEM_ACTION_RE.match(path)
        if action_match is not None:
            item_id = int(action_match.group(1))
            action = action_match.group(2)
            if method == "PUT" and action == "review":
                self._handle_review(item_id, payload)
                return
            if method == "POST" and action == "transition":
                self._handle_transition(item_id, payload)
                return
        if path.startswith("/api/items/"):
            self._send_error(400, "invalid_request", "路径 ID 非法")
            return
        if path.startswith("/api/"):
            self._send_error(404, "not_found", "路径不存在")
            return
        self._send_error(404, "not_found", "路径不存在")

    # ---- endpoints ----

    def _handle_list_items(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        status: str | None = None
        if "status" in query and query["status"][0] != "":
            value = query["status"][0]
            if value not in VALID_STATUSES:
                self._send_error(400, "invalid_status", "非法状态")
                return
            status = value
        items = [self._item_summary(item) for item in self.repository.list_items(status)]
        self._send_json(200, {"items": items})

    def _handle_import(self, payload: dict[str, Any]) -> None:
        unknown = set(payload) - _IMPORT_KEYS
        if unknown:
            self._send_error(400, "invalid_request", "未知请求键")
            return
        raw_text = payload.get("raw_text")
        if not isinstance(raw_text, str):
            self._send_error(400, "invalid_request", "raw_text 必须是字符串")
            return
        canonical_url = payload.get("canonical_url")
        if canonical_url is not None and not isinstance(canonical_url, str):
            self._send_error(400, "invalid_request", "canonical_url 必须是字符串或 null")
            return
        if canonical_url is not None:
            canonical_url = canonical_url.strip() or None
        origin = SubmissionOrigin(platform="nga", canonical_url=canonical_url)
        draft = parse_submission(raw_text, origin=origin)
        if canonical_url is not None and canonical_url not in draft.metadata.sources.nga:
            draft.metadata.sources.nga.append(canonical_url)
        item = self.repository.import_draft(draft)
        self._send_json(200, self._item_detail(item))

    def _handle_watcher_scan(self, payload: dict[str, Any]) -> None:
        try:
            data = self.watcher_service.scan(payload)
        except WatcherUiError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self._send_json(200, data)

    def _handle_watcher_login_confirm(self, payload: dict[str, Any]) -> None:
        try:
            data = self.watcher_service.login_confirm()
        except WatcherUiError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self._send_json(200, data)

    def _handle_approved_package(self, item_id: int) -> None:
        try:
            package = self.approved_package_service.build(item_id)
        except InboxItemNotFoundError:
            self._send_error(404, "item_not_found", "记录不存在")
            return
        except HandoffNotReadyError as exc:
            self._send_error(409, "handoff_not_ready", str(exc))
            return
        self._send_json(200, package.to_dict())

    def _handle_writer_preview(self, item_id: int, payload: dict[str, Any]) -> None:
        try:
            workspace_id = self.demo_service.workspace_id if self.demo_service else None
            data = self.writer_service.preview(item_id, payload, workspace_id)
        except WriterUiError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        self._send_json(200, data)

    def _handle_writer_write(self, item_id: int, payload: dict[str, Any]) -> None:
        try:
            workspace_id = self.demo_service.workspace_id if self.demo_service else None
            data = self.writer_service.write(item_id, payload, workspace_id)
        except WriterUiError as exc:
            self._send_error(exc.status, exc.code, exc.message)
            return
        item = data["item"]
        self._send_json(
            200,
            {
                "result": data["result"],
                "item": self._item_detail(item),
                "exported": True,
            },
        )

    def _handle_review(self, item_id: int, payload: dict[str, Any]) -> None:
        if set(payload) != _REVIEW_KEYS:
            self._send_error(400, "invalid_request", "审核字段必须完整且不能包含未知键")
            return
        sources = payload.get("sources")
        if not isinstance(sources, dict):
            self._send_error(400, "invalid_request", "sources 必须是对象")
            return
        if set(sources) != _SOURCE_KEYS:
            self._send_error(400, "invalid_request", "sources 字段必须完整且不能包含未知键")
            return
        fields = ReviewFields(
            title=payload.get("title"),
            author=payload.get("author"),
            translator=payload.get("translator"),
            series=payload.get("series"),
            tags=payload.get("tags"),
            sources=ReviewSources(
                nga=sources.get("nga"),
                original_series=sources.get("original_series"),
                original=sources.get("original"),
                translation_series=sources.get("translation_series"),
                translation=sources.get("translation"),
            ),
            body_text=payload.get("body_text"),
        )
        try:
            item = self.review_service.save(item_id, fields)
        except InvalidReviewFieldsError as exc:
            self._send_error(400, "invalid_review_fields", str(exc))
            return
        except InboxItemNotFoundError:
            self._send_error(404, "item_not_found", "记录不存在")
            return
        except InboxItemLockedError:
            self._send_error(409, "item_locked", "当前状态不可编辑")
            return
        self._send_json(200, self._item_detail(item))

    def _handle_transition(self, item_id: int, payload: dict[str, Any]) -> None:
        unknown = set(payload) - _TRANSITION_KEYS
        if unknown:
            self._send_error(400, "invalid_request", "未知请求键")
            return
        to_status = payload.get("to_status")
        reason = payload.get("reason")
        if not isinstance(to_status, str) or not isinstance(reason, str):
            self._send_error(400, "invalid_request", "to_status 和 reason 必须是字符串")
            return
        if to_status == "exported":
            self._send_error(400, "invalid_request", "本版本禁止设置 exported")
            return
        with self.writer_lock:
            item = self.repository.get(item_id)
            if item is None:
                self._send_error(404, "item_not_found", "记录不存在")
                return
            if to_status == "approved":
                validation = self.review_service.validate(self.review_service.load(item_id))
                if validation.missing_fields:
                    self._send_error(409, "review_incomplete", "必要字段缺失，不能批准")
                    return
            try:
                item = self.repository.transition(item_id, to_status, reason)
            except InboxItemNotFoundError:
                self._send_error(404, "item_not_found", "记录不存在")
                return
            except InvalidStatusTransitionError:
                self._send_error(409, "invalid_transition", "非法状态转换")
                return
        self._send_json(200, self._item_detail(item))

    # ---- payload assembly ----

    def _item_summary(self, item: Any) -> dict[str, Any]:
        working = item.working_draft
        metadata = working["metadata"]
        return {
            "id": item.id,
            "status": item.status,
            "title": metadata["title"],
            "author": metadata["author"],
            "missing_fields": list(working["diagnostics"]["missing_fields"]),
            "updated_at": item.updated_at,
        }

    def _item_detail(self, item: Any) -> dict[str, Any]:
        fields = self.review_service.load(item.id)
        original = item.original_draft
        working = item.working_draft
        history = self.repository.list_history(item.id)
        duplicates = self.repository.find_exact_body_duplicates(item.id)
        editable = item.status in EDITABLE_STATUSES
        approved_package_available = False
        if item.status == "approved":
            try:
                self.approved_package_service.build(item.id)
                approved_package_available = True
            except (HandoffNotReadyError, InvalidReviewFieldsError):
                approved_package_available = False
        return {
            "id": item.id,
            "status": item.status,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "editable": editable,
            "approved_package_available": approved_package_available,
            "review_fields": {
                "title": fields.title,
                "author": fields.author,
                "translator": fields.translator,
                "series": fields.series,
                "tags": list(fields.tags),
                "sources": {
                    "nga": list(fields.sources.nga),
                    "original_series": list(fields.sources.original_series),
                    "original": list(fields.sources.original),
                    "translation_series": list(fields.sources.translation_series),
                    "translation": list(fields.sources.translation),
                },
                "body_text": fields.body_text,
            },
            "raw_text": original["content"]["raw_text"],
            "missing_fields": list(working["diagnostics"]["missing_fields"]),
            "warnings": original["diagnostics"]["warnings"],
            "unknown_fields": original["diagnostics"]["unknown_fields"],
            "raw_fields": original["raw_fields"],
            "history": [
                {
                    "id": entry.id,
                    "from_status": entry.from_status,
                    "to_status": entry.to_status,
                    "reason": entry.reason,
                    "changed_at": entry.changed_at,
                }
                for entry in history
            ],
            "duplicates": [
                {
                    "id": dup.id,
                    "status": dup.status,
                    "title": dup.working_draft["metadata"]["title"],
                    "author": dup.working_draft["metadata"]["author"],
                }
                for dup in duplicates
            ],
        }

    # ---- security ----

    def _check_host(self) -> bool:
        host_values = self.headers.get_all("Host", [])
        host = host_values[0] if len(host_values) == 1 else ""
        match = _HOST_RE.fullmatch(host)
        actual_port = int(self.server.server_address[1])
        if match is None:
            self._send_error(403, "invalid_host", "非法 Host")
            return False
        hostname = match.group(1).lower()
        supplied_port = int(match.group(2)) if match.group(2) is not None else None
        if supplied_port is not None and supplied_port != actual_port:
            self._send_error(403, "invalid_host", "非法 Host")
            return False
        self._host_name = hostname
        self._host_port = actual_port
        return True

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parts = urllib.parse.urlsplit(origin)
            same_site = (
                parts.scheme == "http"
                and parts.username is None
                and parts.password is None
                and (parts.hostname or "").lower() == self._host_name
                and (parts.port if parts.port is not None else 80) == self._host_port
                and parts.path == ""
                and parts.query == ""
                and parts.fragment == ""
            )
        except ValueError:
            same_site = False
        if not same_site:
            self._send_error(403, "invalid_origin", "非法 Origin")
            return False
        return True

    def _read_body_bytes(self) -> bytes | None:
        """Read and validate the request body length, returning raw bytes."""
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) != 1:
            self._send_error(400, "invalid_request", "缺少 Content-Length")
            return None
        raw_length = length_values[0]
        if _CONTENT_LENGTH_RE.fullmatch(raw_length) is None:
            self._send_error(400, "invalid_request", "非法的 Content-Length")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self._send_error(400, "invalid_request", "非法的 Content-Length")
            return None
        if length > MAX_BODY_BYTES:
            self._send_error(413, "payload_too_large", "请求体超过 2 MiB")
            return None
        return self.rfile.read(length)

    def _parse_json_body(self, raw_body: bytes) -> dict[str, Any] | None:
        """Decode and validate a JSON object request body."""
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(400, "invalid_request", "请求体不是合法 JSON")
            return None
        if not isinstance(payload, dict):
            self._send_error(400, "invalid_request", "请求体必须是 JSON 对象")
            return None
        return payload

    # ---- responses ----

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", _CSP)
        try:
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # The caller has gone away. Do not try to send a second 500 response
            # down the same closed connection; the service can keep running.
            self.close_connection = True

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        body = json.dumps({"ok": True, "data": data}, ensure_ascii=False).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send_error(self, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"ok": False, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(status, "application/json; charset=utf-8", body)

    def _send_static(self, path: str) -> None:
        name, content_type = _STATIC_FILES[path]
        try:
            data = (_STATIC_DIR / name).read_bytes()
        except OSError:
            self._send_error(404, "not_found", "资源不存在")
            return
        self._send(200, content_type, data)


def bind_demo_service(server: ThreadingHTTPServer, demo_service: Any) -> None:
    """Attach the optional demo service to a bound server (TASK-013)."""
    server.demo_service = demo_service
    server.RequestHandlerClass.demo_service = demo_service
    # The demo launcher creates a workspace-aware WriterUiService separately.
    # Rebinding only demo routes leaves the generic handler's legacy writer in
    # place, which rejects every candidate request as workspace_not_active.
    writer = getattr(demo_service, "writer_service", None)
    if writer is None:
        writer = getattr(demo_service, "_writer", None)
    if writer is not None:
        server.RequestHandlerClass.writer_service = writer


class _DemoRoutes:
    """Dispatch helpers for /api/demo/* (kept on the handler mixin path)."""

    @staticmethod
    def _handle_demo_get(handler: Any, path: str) -> None:
        from umalib_ingest.web.demo_ui import DemoUiError

        service = handler.demo_service
        try:
            if path == "/api/demo/status":
                handler._send_json(200, service.status())
                return
            if path == "/api/demo/samples":
                handler._send_json(200, service.samples())
                return
            if path == "/api/demo/catalog":
                handler._send_json(200, service.catalog())
                return
            if path == "/api/demo/deliveries":
                handler._send_json(200, service.deliveries())
                return
            if path == "/api/demo/checkpoints":
                handler._send_json(200, service.list_checkpoints())
                return
            match = _DEMO_OPERATION_RE.match(path)
            if match:
                handler._send_json(200, service.operation(match.group(1)))
                return
            choices_match = _DEMO_ITEM_CHOICES_RE.match(path)
            if choices_match:
                handler._send_json(200, service.get_choices(int(choices_match.group(1))))
                return
            handler._send_error(404, "not_found", "路径不存在")
        except DemoUiError as exc:
            handler._send_error(exc.status, exc.code, exc.message)

    @staticmethod
    def _handle_demo_write(handler: Any, method: str, path: str, payload: dict) -> None:
        from umalib_ingest.web.demo_ui import DemoUiError

        service = handler.demo_service
        try:
            if method == "POST" and path == "/api/demo/samples/import":
                handler._send_json(200, service.import_sample(
                    _require_string(payload, "sample_id")))
                return
            if method == "POST" and path == "/api/demo/import":
                handler._send_json(200, service.import_manual(
                    _require_string(payload, "raw_text")))
                return
            if method == "POST" and path == "/api/demo/format-preview":
                formatted = payload.get("formatted_body")
                handler._send_json(200, service.format_preview(
                    _require_string(payload, "raw_text"), formatted))
                return
            if method == "POST" and path == "/api/demo/source-preview":
                handler._send_json(200, service.source_preview(
                    _require_list(payload, "urls")))
                return
            if method == "POST" and path == "/api/demo/simulated-scan":
                handler._send_json(200, service.simulated_scan(
                    _require_string(payload, "scenario_id")))
                return
            if method == "POST" and path == "/api/demo/nga/baseline-preview":
                handler._send_json(200, service.nga_baseline_preview(
                    _require_bool(payload, "confirm_read")))
                return
            if method == "POST" and path == "/api/demo/nga/baseline-confirm":
                handler._send_json(200, service.nga_baseline_confirm(
                    _require_string(payload, "preview_id"),
                    _require_string(payload, "observed_sha256"),
                    _require_bool(payload, "confirm_skip_history")))
                return
            if method == "POST" and path == "/api/demo/checkpoints":
                handler._send_json(200, service.create_checkpoint(
                    _require_bool(payload, "confirmed")))
                return
            match = _DEMO_CHECKPOINT_RESTORE_RE.match(path)
            if match and method == "POST":
                handler._send_json(200, service.restore_checkpoint(
                    match.group(1), _require_bool(payload, "confirmed")))
                return
            match = _DEMO_OPERATION_RE.match(path)
            if match and method == "POST":
                handler._send_json(200, service.recover(
                    match.group(1),
                    _require_string(payload, "action"),
                    _require_bool(payload, "confirmed")))
                return
            choices_match = _DEMO_ITEM_CHOICES_RE.match(path)
            if choices_match and method == "PUT":
                handler._send_json(200, service.put_choices(
                    int(choices_match.group(1)), payload))
                return
            handler._send_error(404, "not_found", "路径不存在")
        except DemoUiError as exc:
            handler._send_error(exc.status, exc.code, exc.message)


def _require_string(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        from umalib_ingest.web.demo_ui import DemoUiError

        raise DemoUiError(400, "demo_invalid_request", f"{key} 必须是字符串")
    return value


def _require_bool(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        from umalib_ingest.web.demo_ui import DemoUiError

        raise DemoUiError(400, "demo_invalid_request", f"{key} 必须是布尔值")
    return value


def _require_list(payload: dict, key: str) -> list:
    value = payload.get(key)
    if not isinstance(value, list):
        from umalib_ingest.web.demo_ui import DemoUiError

        raise DemoUiError(400, "demo_invalid_request", f"{key} 必须是数组")
    return value


def create_server(
    repository: InboxRepository,
    port: int = 0,
    writer_config: WriterUiConfig | None = None,
    watcher_config: WatcherUiConfig | None = None,
    watcher_runner=None,
) -> ThreadingHTTPServer:
    """Create a 127.0.0.1-bound review UI server for the given Inbox repository."""
    from umalib_ingest.watcher.scan import run_scan as _run_scan

    writer_lock = threading.RLock()
    handler = type(
        "_BoundReviewHandler",
        (_ReviewHandler,),
        {
            "repository": repository,
            "review_service": ReviewService(repository),
            "approved_package_service": ApprovedPackageService(repository),
            "writer_service": WriterUiService(repository, writer_config, writer_lock),
            "watcher_service": WatcherUiService(
                watcher_config or WatcherUiConfig(None, None, None, None),
                runner=watcher_runner or _run_scan,
            ),
            "writer_lock": writer_lock,
        },
    )
    server = ThreadingHTTPServer((HOSTNAME, port), handler)
    server.daemon_threads = True
    server.watcher_service = handler.watcher_service
    return server


def run(
    db_path: str | Path,
    port: int = 8765,
    initialize: bool = False,
    umalib_db: str | Path | None = None,
    candidate_dir: str | Path | None = None,
    watcher_state: str | Path | None = None,
    watcher_profile: str | Path | None = None,
    watcher_snapshot_dir: str | Path | None = None,
    watcher_login_timeout_seconds: float = 300.0,
    watcher_max_scan_pages: int = 12,
) -> None:
    """Open the Inbox at db_path and serve the review UI on 127.0.0.1."""
    path = Path(db_path)
    if not path.exists() and not initialize:
        print(f"错误: Inbox 数据库不存在: {path}", file=sys.stderr)
        raise SystemExit(1)
    repository = InboxRepository(path)
    try:
        repository.initialize()
    except Exception as exc:
        print(f"错误: 无法打开 Inbox 数据库: {exc}", file=sys.stderr)
        raise SystemExit(1)
    writer_config = _build_writer_config(path, umalib_db, candidate_dir)
    watcher_config = _build_watcher_config(
        path,
        watcher_state,
        watcher_profile,
        watcher_snapshot_dir,
        watcher_login_timeout_seconds,
        watcher_max_scan_pages,
    )
    server = create_server(repository, port, writer_config, watcher_config)
    try:
        print(
            f"投稿审核台已启动: http://127.0.0.1:{server.server_address[1]}",
            flush=True,
        )
        print(f"Inbox 数据库: {path}", flush=True)
        if writer_config is not None:
            print(f"UmaLib 源数据库(只读): {writer_config.source_db}", flush=True)
            print(f"候选输出目录: {writer_config.candidate_dir}", flush=True)
        if watcher_config is not None and watcher_config.enabled:
            print("Watcher 已配置（tid=30099686）", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("已停止", flush=True)
    finally:
        watcher_service = getattr(server, "watcher_service", None)
        if watcher_service is not None:
            try:
                watcher_service.shutdown()
            except Exception:
                pass
        server.server_close()


def _build_watcher_config(
    inbox_path: Path,
    watcher_state: str | Path | None,
    watcher_profile: str | Path | None,
    watcher_snapshot_dir: str | Path | None,
    login_timeout_seconds: float,
    max_scan_pages: int,
) -> WatcherUiConfig | None:
    """Validate CLI-fixed Watcher paths at startup; all-or-none, fail closed."""
    provided = [
        value is not None
        for value in (watcher_state, watcher_profile, watcher_snapshot_dir)
    ]
    if not any(provided):
        return WatcherUiConfig(None, None, None, None)
    if not all(provided):
        print(
            "错误: --watcher-state / --watcher-profile / --watcher-snapshot-dir "
            "必须同时提供或同时省略",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if login_timeout_seconds <= 0:
        print("错误: --watcher-login-timeout-seconds 必须是正数", file=sys.stderr)
        raise SystemExit(2)
    if not 1 <= max_scan_pages <= 25:
        print("错误: --watcher-max-scan-pages 必须在 1..25", file=sys.stderr)
        raise SystemExit(2)
    from umalib_ingest.nga_access.errors import NgaAccessError
    from umalib_ingest.watcher.state import WatcherConfig

    try:
        WatcherConfig(
            state_file=watcher_state,
            profile_dir=watcher_profile,
            snapshot_dir=watcher_snapshot_dir,
            inbox_db=inbox_path,
            navigation_delay_seconds=3.0,
            max_scan_pages=max_scan_pages,
        )
    except NgaAccessError as exc:
        print(f"错误: Watcher 配置不合法: {exc.message}", file=sys.stderr)
        raise SystemExit(2)
    return WatcherUiConfig(
        state_file=Path(watcher_state),
        profile_dir=Path(watcher_profile),
        snapshot_dir=Path(watcher_snapshot_dir),
        inbox_db=inbox_path,
        login_timeout_seconds=login_timeout_seconds,
        max_scan_pages=max_scan_pages,
    )


def _build_writer_config(
    inbox_path: Path,
    umalib_db: str | Path | None,
    candidate_dir: str | Path | None,
) -> WriterUiConfig | None:
    """Validate the CLI-fixed Writer paths without creating any file."""
    if umalib_db is None and candidate_dir is None:
        return None
    if umalib_db is None or candidate_dir is None:
        print(
            "错误: --umalib-db 与 --candidate-dir 必须同时提供或同时省略",
            file=sys.stderr,
        )
        raise SystemExit(2)
    source = Path(umalib_db)
    if not source.exists():
        print(f"错误: UmaLib 源数据库不存在: {source}", file=sys.stderr)
        raise SystemExit(2)
    if source.is_symlink():
        print(f"错误: UmaLib 源数据库不能是符号链接: {source}", file=sys.stderr)
        raise SystemExit(2)
    if not source.is_file():
        print(f"错误: UmaLib 源数据库不是普通文件: {source}", file=sys.stderr)
        raise SystemExit(2)
    if source.resolve() == inbox_path.resolve():
        print("错误: UmaLib 源数据库不能与 Inbox 数据库相同", file=sys.stderr)
        raise SystemExit(2)
    try:
        validate_source_path(source)
        from umalib_ingest.umalib_writer import load_target_tag_catalog

        load_target_tag_catalog(source)
    except UmaLibWriterError as exc:
        print(f"错误: UmaLib 源数据库不可用: {exc}", file=sys.stderr)
        raise SystemExit(2)
    directory = Path(candidate_dir)
    if not directory.exists():
        print(f"错误: 候选输出目录不存在: {directory}", file=sys.stderr)
        raise SystemExit(2)
    if directory.is_symlink():
        print(f"错误: 候选输出目录不能是符号链接: {directory}", file=sys.stderr)
        raise SystemExit(2)
    if not directory.is_dir():
        print(f"错误: 候选输出目录不是目录: {directory}", file=sys.stderr)
        raise SystemExit(2)
    return WriterUiConfig(source_db=source, candidate_dir=directory)
