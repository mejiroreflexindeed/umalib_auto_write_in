"""TASK-014：operator 产品 HTTP 路由（/api/operator/*，固定 DTO）。"""

from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from umalib_ingest.operator.models import JsonValidationError, validate_tid
from umalib_ingest.operator.service import OperatorError

_STATIC_DIR = Path(__file__).parent / "static"
_OPERATOR_STATIC = {
    "/": ("operator.html", "text/html; charset=utf-8"),
    "/operator.html": ("operator.html", "text/html; charset=utf-8"),
    "/operator.js": ("operator.js", "application/javascript; charset=utf-8"),
    "/operator.css": ("operator.css", "text/css; charset=utf-8"),
}
MAX_BODY_BYTES = 2 * 1024 * 1024
_TID_RE = re.compile(r"^[1-9][0-9]*$")
_RUN_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
_ITEM_RE = re.compile(r"^(\d+)$")
_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?$", re.IGNORECASE)
_CONTENT_LENGTH_RE = re.compile(r"^[0-9]+$")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)


class _OperatorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    operator_service: Any = None
    repository: Any = None
    review_service: Any = None
    writer_service: Any = None
    workspace_service: Any = None
    checkpoint_service: Any = None

    def log_message(self, fmt, *args):
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        try:
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def _data(self, data: Any) -> None:
        self._send(200, {"data": data})

    def _error(self, status: int, code: str, message: str, retryable: bool = False) -> None:
        self._send(status, {"error": {"code": code, "message": message,
                                      "retryable": retryable}})

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def do_GET(self):
        try:
            self._route("GET")
        except OperatorError as exc:
            self._error(exc.status, exc.code, exc.message, exc.retryable)
        except Exception:
            self._error(500, "internal_error", "服务器内部错误")

    def do_POST(self):
        try:
            self._route("POST")
        except OperatorError as exc:
            self._error(exc.status, exc.code, exc.message, exc.retryable)
        except Exception:
            self._error(500, "internal_error", "服务器内部错误")

    def do_PUT(self):
        try:
            self._route("PUT")
        except OperatorError as exc:
            self._error(exc.status, exc.code, exc.message, exc.retryable)
        except Exception:
            self._error(500, "internal_error", "服务器内部错误")

    def do_PATCH(self):
        try:
            self._route("PATCH")
        except OperatorError as exc:
            self._error(exc.status, exc.code, exc.message, exc.retryable)
        except Exception:
            self._error(500, "internal_error", "服务器内部错误")

    def _route(self, method: str) -> None:
        self._check_host()
        if method in ("POST", "PUT", "PATCH"):
            self._check_origin()
        path = self._path()
        if path in _OPERATOR_STATIC and method == "GET":
            file_name, content_type = _OPERATOR_STATIC[path]
            body = (_STATIC_DIR / file_name).read_bytes()
            self.send_response(200)
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
                self.close_connection = True
            return
        if not path.startswith("/api/operator/"):
            self._error(404, "not_found", "路径不存在")
            return
        payload = None
        if method in ("POST", "PUT", "PATCH"):
            payload = self._read_body()
        rest = path[len("/api/operator/"):]
        self._dispatch(method, rest, payload)

    def _read_body(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise OperatorError(415, "unsupported_media_type",
                                "写操作只接受 application/json")
        length_values = self.headers.get_all("Content-Length", [])
        if len(length_values) != 1 or _CONTENT_LENGTH_RE.fullmatch(length_values[0]) is None:
            raise OperatorError(400, "invalid_request", "Content-Length 非法")
        length = int(length_values[0])
        if length > MAX_BODY_BYTES:
            raise OperatorError(413, "payload_too_large", "请求体超过 2 MiB")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OperatorError(400, "invalid_json", "请求体不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise OperatorError(400, "invalid_json", "请求体必须是 JSON 对象")
        return payload

    def _check_host(self) -> None:
        values = self.headers.get_all("Host", [])
        host = values[0] if len(values) == 1 else ""
        match = _HOST_RE.fullmatch(host)
        actual_port = int(self.server.server_address[1])
        if match is None:
            raise OperatorError(403, "invalid_host", "非法 Host")
        supplied_port = int(match.group(2)) if match.group(2) is not None else None
        if supplied_port is not None and supplied_port != actual_port:
            raise OperatorError(403, "invalid_host", "非法 Host")
        self._host_name = match.group(1).lower()
        self._host_port = actual_port

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None:
            return
        try:
            parts = urllib.parse.urlsplit(origin)
            valid = (
                parts.scheme == "http" and parts.username is None
                and parts.password is None
                and (parts.hostname or "").lower() == self._host_name
                and (parts.port if parts.port is not None else 80) == self._host_port
                and parts.path == "" and parts.query == "" and parts.fragment == ""
            )
        except ValueError:
            valid = False
        if not valid:
            raise OperatorError(403, "invalid_origin", "非法 Origin")

    def _dispatch(self, method: str, rest: str, payload: dict) -> None:
        svc = self.operator_service
        segments = [s for s in rest.split("/") if s != ""]

        if not segments:
            self._error(404, "not_found", "路径不存在")
            return

        if segments[0] == "status" and method == "GET":
            self._data(self._status())
            return
        if segments[0] == "threads":
            self._threads_route(method, segments[1:], payload)
            return
        if segments[0] == "validate-source-path" and method == "POST":
            path_value = _str(payload, "path")
            try:
                self._data(svc.verify_source(Path(path_value)))
            except Exception as exc:
                raise OperatorError(422, "source_not_ready", str(exc)) from None
            return
        if segments[0] == "choose-source" and method == "POST":
            from umalib_ingest.operator.source_picker import pick_source_file

            picked = pick_source_file()
            if picked.get("cancelled"):
                self._data({"cancelled": True})
                return
            if picked.get("error"):
                raise OperatorError(503, "source_picker_unavailable",
                                    "文件选择器不可用，请使用路径输入")
            try:
                self._data(svc.verify_source(Path(picked["path"])))
            except Exception as exc:
                raise OperatorError(422, "source_not_ready", str(exc)) from None
            return
        if segments[0] == "workspaces":
            svc = self.operator_service
            if method == "GET" and len(segments) == 1:
                self._data(svc.list_workspaces())
                return
            if method == "POST" and len(segments) == 1:
                self._data(svc.bind_source(
                    _str(payload, "source_id"), _str(payload, "name"),
                    _bool(payload, "confirmed")))
                return
            if method == "POST" and len(segments) == 2 and segments[1] == "select":
                self._data(svc.select_workspace(
                    _str(payload, "workspace_id"), _bool(payload, "confirmed")))
                return
            if method == "POST" and len(segments) == 2 and segments[1] == "new":
                self._data(svc.create_workspace_from_source(
                    _str(payload, "source_id"), _str(payload, "name"),
                    _bool(payload, "confirmed")))
                return
        if segments[0] == "runs":
            self._runs_route(method, segments[1:], payload)
            return
        if segments[0] == "items":
            self._items_route(method, segments[1:], payload)
            return
        if segments[0] == "catalog" and method == "GET":
            self._data(self._catalog())
            return
        if segments[0] == "deliveries" and method == "GET":
            self._data(self._deliveries())
            return
        if segments[0] == "deliveries" and len(segments) == 3 and method == "POST" \
                and _RUN_RE.fullmatch(segments[1]):
            if segments[2] == "view-copy":
                self._data(svc.delivery_view_copy(
                    segments[1], _bool(payload, "confirmed")))
                return
            if segments[2] == "open-folder":
                self._data(svc.open_delivery_folder(
                    segments[1], _bool(payload, "confirmed")))
                return
        if segments[0] == "operations" and method == "GET" and len(segments) == 2:
            self._data(self._operation(segments[1]))
            return
        if segments[0] == "checkpoints":
            self._checkpoints_route(method, segments[1:], payload)
            return
        if segments[0] == "shutdown" and method == "POST":
            self._data(svc.shutdown(_bool(payload, "confirmed")))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if segments[0] == "import" and method == "POST":
            self._data(svc.manual_import(_str(payload, "raw_text")))
            return
        self._error(404, "not_found", "路径不存在")

    # ---- status / source / threads ----

    def _status(self) -> dict[str, Any]:
        svc = self.operator_service
        manifest = svc._workspace.store().load(svc.workspace_id)
        binding = svc.read_source_binding()
        run = svc._jobs.active_run()
        detected = svc.detected_source()
        return {
            "workspace_id": svc.workspace_id,
            "workspace_name": manifest.name,
            "revision": manifest.revision,
            "source": {
                "bound": binding is not None,
                "file_name": binding.file_name if binding else (
                    detected.get("file_name") if detected else None),
                "sha256": binding.sha256 if binding else None,
                "schema_fingerprint": binding.schema_fingerprint_sha256 if binding else None,
                "detected": detected,
            },
            "active_run": run.to_dict() if run else None,
        }

    def _threads_route(self, method: str, rest: list[str], payload: dict) -> None:
        svc = self.operator_service
        if not rest:
            if method == "GET":
                self._data(svc.list_threads())
                return
            if method == "POST":
                url = _str(payload, "url")
                name = payload.get("name")
                rev = _int(payload, "expected_index_revision")
                self._data(svc.add_thread(url, name, rev))
                return
        if len(rest) == 1 and _TID_RE.fullmatch(rest[0]):
            tid = rest[0]
            if method == "PATCH":
                name = payload.get("name")
                enabled = payload.get("enabled")
                make_active = payload.get("make_active")
                if name is None and enabled is None and make_active is None:
                    raise OperatorError(400, "no_change", "至少提供一个变更键")
                self._data(svc.patch_thread(tid, _int(payload, "expected_index_revision"),
                                            name=name, enabled=enabled,
                                            make_active=make_active))
                return
        if len(rest) >= 2 and _TID_RE.fullmatch(rest[0]):
            tid = rest[0]
            action = rest[1]
            if action == "initialize-preview" and method == "POST":
                self._data({"run_id": svc.initialize_preview(
                    tid, payload.get("start") or {},
                    _int(payload, "expected_config_revision"),
                    _bool(payload, "confirmed_network"))})
                return
            if action == "initialize-confirm" and method == "POST":
                self._data(svc.initialize_confirm(
                    tid, _str(payload, "run_id"), _str(payload, "preview_sha256"),
                    _int(payload, "expected_config_revision"), _bool(payload, "confirmed")))
                return
            if action == "login" and method == "POST":
                self._data({"run_id": svc.login(tid, _bool(payload, "confirmed_network"))})
                return
            if action == "scan" and method == "POST":
                self._data({"run_id": svc.scan(
                    tid, payload.get("dry_run", False),
                    _bool(payload, "confirm_import"),
                    _str(payload, "expected_state_sha256"))})
                return
            if action == "rescan" and method == "POST":
                self._data({"run_id": svc.rescan(tid, payload)})
                return
            if action == "status" and method == "GET":
                self._data(svc.thread_status(tid))
                return
        self._error(404, "not_found", "路径不存在")

    def _runs_route(self, method: str, rest: list[str], payload: dict) -> None:
        svc = self.operator_service
        if len(rest) == 1 and _RUN_RE.fullmatch(rest[0]):
            run_id = rest[0]
            if method == "GET":
                self._data(svc.get_run(run_id))
                return
            if method == "POST":
                self._data(svc.cancel(run_id))
                return
        if len(rest) == 2 and _RUN_RE.fullmatch(rest[0]) and rest[1] == "login-confirm" \
                and method == "POST":
            self._data(svc.login_confirm(rest[0], _bool(payload, "confirmed")))
            return
        self._error(404, "not_found", "路径不存在")

    def _items_route(self, method: str, rest: list[str], payload: dict) -> None:
        from umalib_ingest.inbox import InboxItemNotFoundError
        from umalib_ingest.web.writer_ui import WriterUiError

        if not rest and method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            tid = query.get("tid", [""])[0] or None
            status = query.get("status", [""])[0] or None
            q = query.get("q", [""])[0] or None
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["50"])[0])
            self._data(self._list_items(tid, status, q, page, page_size))
            return
        if rest and rest[0].isdigit():
            item_id = int(rest[0])
            if len(rest) == 1 and method == "GET":
                self._data(self._item_detail(item_id))
                return
            if len(rest) == 2 and method == "PUT" and rest[1] == "review":
                self._data(self._review_save(item_id, payload))
                return
            if len(rest) == 2 and method == "POST" and rest[1] == "transition":
                self._data(self._transition(item_id, payload))
                return
            if len(rest) == 2 and method == "POST" and rest[1] == "format-preview":
                self._data(self._format_preview(item_id, payload))
                return
            if len(rest) == 2 and method == "POST" and rest[1] == "source-preview":
                from umalib_ingest.review.source_urls import normalize_source_url

                urls = payload.get("urls")
                if not isinstance(urls, list) or any(not isinstance(u, str) for u in urls):
                    raise OperatorError(400, "source_preview_invalid", "urls 必须是字符串数组")
                self._data({"results": [{
                    "original": u, "normalized": r.normalized, "changed": r.changed,
                    "warnings": list(r.warnings), "blockers": list(r.blockers),
                } for u in urls for r in [normalize_source_url(u)]]})
                return
            if len(rest) == 2 and rest[1] == "choices":
                if method == "GET":
                    self._data(self._get_choices(item_id))
                    return
                if method == "PUT":
                    self._data(self._put_choices(item_id, payload))
                    return
            if len(rest) == 3 and rest[1] == "writer":
                if rest[2] == "preview" and method == "POST":
                    self._data(self._writer_preview(item_id, payload))
                    return
                if rest[2] == "write" and method == "POST":
                    self._data(self._writer_write(item_id, payload))
                    return
        self._error(404, "not_found", "路径不存在")

    # ---- items delegates ----

    def _list_items(self, tid, status, q, page, page_size):
        if page_size != 50:
            raise OperatorError(400, "page_size_invalid", "page_size 只接受 50")
        if page < 1:
            raise OperatorError(400, "page_invalid", "page 必须为正整数")
        try:
            page_items, total = self.repository.list_items_page(
                status=status, thread_id=tid, query=q,
                page=page, page_size=page_size)
        except ValueError as exc:
            raise OperatorError(400, "filter_invalid", str(exc)) from None
        return {
            "items": [self._summary(item) for item in page_items],
            "total": total, "page": page, "page_size": page_size,
        }

    def _sibling_versions(self, item_id: int, origin: dict) -> list:
        """G03/F011：同一楼层（tid+pid）的其他稿件，供只读前后对照。

        经 Repository 的数据库侧精确查询，不受列表分页影响。
        """
        thread_id = origin.get("thread_id")
        post_id = origin.get("post_id")
        if not thread_id or not post_id:
            return []
        try:
            matches = self.repository.find_items_by_origin(str(thread_id), str(post_id))
        except AttributeError:
            matches = []
        siblings = []
        for other in matches:
            if other.id == item_id:
                continue
            other_fields = self.review_service.load(other.id)
            siblings.append({
                "id": other.id, "status": other.status,
                "title": other_fields.title, "author": other_fields.author,
                "body_text": other_fields.body_text,
                "original_body": ((other.original_draft.get("content") or {}).get(
                    "body_text") or ""),
                "updated": other.updated_at if hasattr(other, "updated_at") else None,
            })
        siblings.sort(key=lambda entry: entry["id"])
        return siblings

    def _summary(self, item):
        metadata = item.working_draft.get("metadata") or {}
        return {
            "id": item.id, "status": item.status,
            "title": metadata.get("title"),
            "author": metadata.get("author"),
            "missing_fields": list(
                (item.working_draft.get("diagnostics") or {}).get("missing_fields") or []),
            "updated": item.updated_at if hasattr(item, "updated_at") else None,
        }

    def _item_detail(self, item_id):
        item = self.repository.get(item_id)
        if item is None:
            raise OperatorError(404, "item_not_found", "记录不存在")
        origin = (item.original_draft.get("origin") or {})
        from umalib_ingest.workspace.models import canonical_sha256

        fields = self.review_service.load(item_id)
        history = self.repository.list_history(item_id)
        duplicates = self.repository.find_exact_body_duplicates(item_id)
        choices = self.operator_service._workspace.load_choices(
            self.operator_service.workspace_id, item_id)
        return {
            "id": item.id, "status": item.status, "editable": item.status in (
                "candidate", "incomplete", "suspected_duplicate"),
            "original_draft": item.original_draft,
            "review_fields": {
                "title": fields.title, "author": fields.author,
                "translator": fields.translator, "series": fields.series,
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
            "working_draft_sha256": canonical_sha256(item.working_draft),
            "choices": choices,
            "choices_stale": bool(choices and choices.get("stale")),
            "diagnostics": item.working_draft.get("diagnostics") or {},
            "history": [{
                "from_status": entry.from_status, "to_status": entry.to_status,
                "reason": entry.reason, "changed_at": entry.changed_at,
            } for entry in history],
            "duplicates": [self._summary(candidate) for candidate in duplicates],
            "siblings": self._sibling_versions(item_id, origin),
            "origin": {"thread_id": origin.get("thread_id"),
                       "post_id": origin.get("post_id"),
                       "canonical_url": origin.get("canonical_url")},
        }

    def _review_save(self, item_id, payload):
        from umalib_ingest.inbox import InboxItemLockedError, InboxItemNotFoundError
        from umalib_ingest.review.errors import InvalidReviewFieldsError

        allowed = {"title", "author", "translator", "series", "tags", "sources", "body_text"}
        if set(payload) != allowed:
            raise OperatorError(400, "review_invalid", "review 字段不完整或包含未知键")
        sources = payload["sources"]
        if not isinstance(sources, dict) or set(sources) != {
                "nga", "original_series", "original", "translation_series", "translation"}:
            raise OperatorError(400, "review_invalid", "sources 必须且只有五类现有键")
        from umalib_ingest.review import ReviewFields, ReviewSources

        fields = ReviewFields(
            title=payload["title"], author=payload["author"],
            translator=payload["translator"], series=payload["series"],
            tags=payload["tags"],
            sources=ReviewSources(**sources), body_text=payload["body_text"],
        )
        try:
            with self.operator_service.mutation_scope():
                item = self.review_service.save(item_id, fields)
        except InvalidReviewFieldsError as exc:
            raise OperatorError(400, "invalid_review_fields", str(exc)) from None
        except InboxItemNotFoundError:
            raise OperatorError(404, "item_not_found", "记录不存在") from None
        except InboxItemLockedError:
            raise OperatorError(409, "item_locked", "当前状态不可编辑") from None
        return {"id": item.id, "status": item.status}

    def _transition(self, item_id, payload):
        from umalib_ingest.inbox import (
            InboxItemNotFoundError,
            InvalidStatusTransitionError,
        )

        if set(payload) != {"to_status", "reason"}:
            raise OperatorError(400, "transition_invalid",
                                "transition 必须且只有 to_status、reason")
        to_status = payload.get("to_status")
        if to_status not in ("approved", "ignored", "candidate", "incomplete",
                             "suspected_duplicate"):
            raise OperatorError(400, "transition_invalid", "目标状态非法")
        reason = payload.get("reason")
        if not isinstance(reason, str) or reason.strip() == "":
            raise OperatorError(400, "transition_invalid", "reason 必须为非空人工理由")
        if to_status == "approved":
            try:
                validation = self.review_service.validate(
                    self.review_service.load(item_id))
            except InboxItemNotFoundError:
                raise OperatorError(404, "item_not_found", "记录不存在") from None
            if validation.missing_fields:
                raise OperatorError(
                    409, "review_incomplete",
                    "必要字段缺失，不能批准：" + ", ".join(validation.missing_fields))
        try:
            with self.operator_service.mutation_scope():
                item = self.repository.transition(item_id, to_status, reason)
        except InboxItemNotFoundError:
            raise OperatorError(404, "item_not_found", "记录不存在") from None
        except InvalidStatusTransitionError:
            raise OperatorError(409, "invalid_transition", "非法状态转换") from None
        return {"id": item.id, "status": item.status}

    def _format_preview(self, item_id, payload):
        from umalib_ingest.formatting.extract import extract_body_for_presentation
        from umalib_ingest.formatting.quill import render_quill_html
        from umalib_ingest.formatting.ubb import parse_basic_ubb

        if set(payload) != {"formatted_body"}:
            raise OperatorError(400, "format_preview_invalid",
                                "format-preview 必须且只有 formatted_body")
        formatted = payload.get("formatted_body")
        if formatted is not None and not isinstance(formatted, str):
            raise OperatorError(400, "format_preview_invalid",
                                "formatted_body 必须是字符串或 null")
        item = self.repository.get(item_id)
        if item is None:
            raise OperatorError(404, "item_not_found", "记录不存在")
        raw_text = (item.original_draft.get("content") or {}).get("raw_text") or ""
        if formatted is None or formatted.strip() == "":
            extraction = extract_body_for_presentation(raw_text or "")
            if not extraction.ready:
                return {"mode": extraction.mode, "document": None, "plain_text": None,
                        "diagnostics": [d.__dict__ for d in extraction.diagnostics]}
            result = parse_basic_ubb(extraction.body)
            return {"mode": "auto", "html": render_quill_html(result.document) if result.ready else None,
                    "plain_text": result.plain_text, "formatted_body": extraction.body,
                    "diagnostics": [d.__dict__ for d in result.diagnostics]}
        result = parse_basic_ubb(formatted)
        return {"mode": "manual", "html": render_quill_html(result.document) if result.ready else None,
                "plain_text": result.plain_text,
                "diagnostics": [d.__dict__ for d in result.diagnostics]}

    def _get_choices(self, item_id):
        from umalib_ingest.workspace.service import canonical_sha256

        svc = self.operator_service
        value = svc._workspace.load_choices(svc.workspace_id, item_id)
        item = self.repository.get(item_id)
        if item is None:
            raise OperatorError(404, "item_not_found", "记录不存在")
        if value is None:
            return {"choices": None, "working_draft_sha256": canonical_sha256(item.working_draft)}
        return {"choices": value}

    def _put_choices(self, item_id, payload):
        allowed = {"note", "upload_time_seconds", "ordered_sources", "tag_choices",
                   "content_mode", "formatted_body", "plain_loss_confirmed"}
        if set(payload) != allowed:
            raise OperatorError(400, "choices_invalid", "choices 字段不完整或包含未知键")
        svc = self.operator_service
        from umalib_ingest.workspace.errors import (
            WorkspaceBusyError,
            WorkspaceError,
            WorkspaceManifestError,
        )
        try:
            saved = svc._workspace.save_choices(svc.workspace_id, item_id, payload)
        except WorkspaceBusyError as exc:
            raise OperatorError(409, exc.code, exc.message, retryable=True) from None
        except WorkspaceManifestError as exc:
            raise OperatorError(400, "choices_invalid", exc.message) from None
        except WorkspaceError as exc:
            status = 404 if exc.code == "item_not_found" else 409
            raise OperatorError(status, exc.code, exc.message) from None
        return {"choices": saved}

    def _catalog(self):
        from umalib_ingest.umalib_writer import load_target_tag_catalog

        svc = self.operator_service
        try:
            _head, source = svc._workspace.resolve_head(svc.workspace_id)
            loaded = load_target_tag_catalog(source)
            catalog = {
                "database_sha256": loaded.database_sha256,
                "schema_fingerprint_sha256": loaded.schema_fingerprint_sha256,
                "tags": [{"id": t.id, "name": t.name, "type": t.type,
                          "usage_count": t.usage_count} for t in loaded.tags],
            }
        except Exception:
            raise OperatorError(409, "catalog_unavailable", "无法加载标签目录")
        return {"catalog": catalog}

    def _deliveries(self):
        svc = self.operator_service
        directory = svc._workspace.paths.candidates_dir(svc.workspace_id)
        deliveries = []
        for entry in svc._workspace.list_deliveries(svc.workspace_id):
            deliveries.append({
                **entry,
                "candidate_path": str((directory / entry["candidate_file_name"]).resolve()),
            })
        return {"deliveries": deliveries,
                "candidate_directory": str(directory.resolve())}

    def _operation(self, operation_id):
        svc = self.operator_service
        record = svc._workspace.get_operation(svc.workspace_id, operation_id)
        if record is None:
            raise OperatorError(404, "operation_not_found", "操作不存在")
        return {"operation": record.to_dict()}

    def _checkpoints_route(self, method, rest, payload):
        from umalib_ingest.workspace.errors import CheckpointError, WorkspaceError

        svc = self.operator_service
        try:
            if not rest and method == "GET":
                self._data(self._list_checkpoints())
                return
            if not rest and method == "POST":
                self._data(svc.create_checkpoint(_bool(payload, "confirmed")))
                return
            if len(rest) == 2 and rest[1] == "restore" and method == "POST":
                self._data(svc.restore_checkpoint(rest[0], _bool(payload, "confirmed")))
                return
        except CheckpointError as exc:
            raise OperatorError(409, exc.code, exc.message) from None
        except WorkspaceError as exc:
            raise OperatorError(409, exc.code, exc.message) from None
        self._error(404, "not_found", "路径不存在")

    def _list_checkpoints(self):
        svc = self.operator_service
        directory = svc._workspace.paths.checkpoints_dir(svc.workspace_id)
        entries = []
        if directory.exists():
            for entry in sorted(directory.iterdir()):
                cj = entry / "checkpoint.json"
                if entry.is_dir() and cj.exists():
                    data = json.loads(cj.read_text(encoding="utf-8"))
                    entries.append({"checkpoint_id": data.get("checkpoint_id"),
                                    "revision": data.get("revision"),
                                    "created_at": data.get("created_at")})
        return {"checkpoints": entries}

    def _writer_preview(self, item_id, payload):
        candidate = _str(payload, "candidate_file_name")
        return self.operator_service.writer_preview(item_id, candidate)

    def _writer_write(self, item_id, payload):
        preview_id = _str(payload, "preview_id")
        confirmed = _bool(payload, "app_closed_confirmed")
        return self.operator_service.writer_write(item_id, preview_id, confirmed)


def _str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise OperatorError(400, "invalid_request", f"{key} 必须是字符串")
    return value


def _int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise OperatorError(400, "invalid_request", f"{key} 必须是整数")
    return value


def _bool(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise OperatorError(400, "invalid_request", f"{key} 必须是布尔值")
    return value


def build_operator_server(service, repository, review_service, writer_service,
                          workspace_service, checkpoint_service, port: int = 0):
    handler = type(
        "_OperatorHandlerBound",
        (_OperatorHandler,),
        {
            "operator_service": service,
            "repository": repository,
            "review_service": review_service,
            "writer_service": writer_service,
            "workspace_service": workspace_service,
            "checkpoint_service": checkpoint_service,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server
