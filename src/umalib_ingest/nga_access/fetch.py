"""Public authenticated thread-page fetching API (TASK-011 extraction).

Reusable, evidence-driven reading of one target thread page: strict final-URL
validation, deterministic decoding, floor parsing and versioned snapshots.
The TASK-010 probe and the Watcher both use this API; security and snapshot
behaviour are unchanged.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from umalib_ingest.nga_access import codec, parsing, snapshot, urls
from umalib_ingest.nga_access.errors import NgaAccessError

_LOGIN_PAGE_HINTS = re.compile(r"登录|login", re.IGNORECASE)
_CAPTCHA_HINTS = re.compile(r"验证码|captcha|人机验证|安全验证", re.IGNORECASE)


@dataclass(frozen=True)
class FetchedThreadPage:
    """One fetched target thread page; never holds body or content."""

    tid: int
    page: int
    last_page: int | None
    status: int
    final_url: str
    content_type: str
    encoding: str | None
    body_sha256: str
    body_bytes: int
    snapshot_outcome: str
    floors: tuple[parsing.FloorSnapshot, ...]
    diagnostics: tuple[str, ...]


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _url_without_query(url: str) -> str:
    """Return scheme://netloc/path only, so diagnostic metadata never persists
    query tokens or fragments (e.g. login redirect parameters)."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _classify_page(
    status: int, final_url: str, title: str, body_head: str, body: bytes
) -> tuple[str, ...]:
    """Return deterministic diagnostics for one page response."""
    diagnostics: list[str] = []
    if status == 403:
        diagnostics.append("thread_forbidden")
    elif status == 404:
        diagnostics.append("thread_not_found")
    if _CAPTCHA_HINTS.search(title) or _CAPTCHA_HINTS.search(body_head):
        diagnostics.append("captcha_or_challenge")
    if _LOGIN_PAGE_HINTS.search(title) and "nga.cn" in final_url:
        diagnostics.append("manual_login_required")
    if "bbs.nga.cn" not in final_url:
        diagnostics.append("unexpected_redirect")
    if status not in (200, 304):
        diagnostics.append("response_not_captured")
    return tuple(diagnostics)


def fetch_thread_page(
    session,
    tid: int,
    page: int,
    snapshot_dir: Path,
) -> FetchedThreadPage:
    """Navigate to one canonical page, capture, decode, parse and snapshot.

    The final URL is strictly validated before any content is saved; login,
    captcha, forbidden, unrecognized and malformed pages produce content-free
    diagnostic metadata only.
    """
    url = urls.canonical_thread_url(tid, page)
    page_obj = session.page
    response = page_obj.goto(url, wait_until="domcontentloaded")
    if response is None:
        raise NgaAccessError(
            "response_not_captured", "未能捕获目标文档响应，未保存任何内容"
        )
    final_url = response.url
    headers = response.headers
    content_type = headers.get("content-type", "")
    status = response.status
    body_bytes = response.body()
    body_sha = snapshot.sha256_bytes(body_bytes)
    title = page_obj.title()
    body_head = body_bytes[:2048].decode("ascii", "replace")

    def diagnostic_only(codes: tuple[str, ...]) -> FetchedThreadPage:
        meta = {
            "tid": tid,
            "page_label": str(page),
            "status": status,
            "final_url": _url_without_query(final_url),
            "content_type": content_type,
            "captured_at_utc": _utc_now(),
            "body_bytes": len(body_bytes),
            "body_sha256": body_sha,
            "diagnostics": list(codes),
        }
        snapshot.write_diagnostic_meta(snapshot_dir, tid, str(page), meta)
        return FetchedThreadPage(
            tid=tid,
            page=page,
            last_page=None,
            status=status,
            final_url=final_url,
            content_type=content_type,
            encoding=None,
            body_sha256=body_sha,
            body_bytes=len(body_bytes),
            snapshot_outcome="skipped",
            floors=(),
            diagnostics=tuple(codes),
        )

    # 官方登录页（HTTPS + 官方 host + 明确登录路径）：保持 Edge 停在官方页面，
    # 只落无正文诊断，绝不按通用意外重定向处理，也不写正文快照。
    if urls.is_official_login_url(final_url):
        return diagnostic_only(("manual_login_required",))

    # 异常重定向必须在保存正文前停止（非 NGA host/HTTP 降级/错误 tid/其他路径）。
    urls.validate_final_url(final_url, tid)
    diagnostics = list(
        _classify_page(status, final_url, title, body_head, body_bytes)
    )

    if any(
        code in diagnostics
        for code in (
            "thread_forbidden",
            "thread_not_found",
            "captcha_or_challenge",
            "manual_login_required",
            "response_not_captured",
        )
    ):
        return diagnostic_only(tuple(diagnostics))

    decode = codec.decode_response(body_bytes, content_type)
    if decode.encoding is None or decode.diagnostic is not None:
        return diagnostic_only((decode.diagnostic or "encoding_uncertain",))
    decoded_text = decode.text if decode.encoding is not None else None
    # The ASCII preview cannot recognize Chinese challenge text. Inspect the
    # deterministically decoded header before storing any response content.
    if _CAPTCHA_HINTS.search((decoded_text or "")[:2048]):
        return diagnostic_only(("captcha_or_challenge",))
    thread = None
    if decoded_text is not None:
        thread = parsing.parse_html_thread(decoded_text, tid)

    # 楼层结构无法识别时只保存无正文诊断。
    if thread is not None and "floor_structure_unrecognized" in thread.diagnostics:
        diagnostics.extend(thread.diagnostics)
        return diagnostic_only(tuple(diagnostics))

    capture = snapshot.Capture(
        tid=tid,
        page_label=str(page),
        status=status,
        final_url=final_url,
        content_type=content_type,
        body_bytes=body_bytes,
        captured_at_utc=_utc_now(),
        diagnostics=tuple(diagnostics),
    )
    outcome = snapshot.write_response_snapshot(snapshot_dir, capture, decoded_text)

    floors: tuple[parsing.FloorSnapshot, ...] = ()
    last_page: int | None = None
    if thread is not None:
        floors = thread.floors
        last_page = thread.last_page
        diagnostics.extend(thread.diagnostics)
    return FetchedThreadPage(
        tid=tid,
        page=page,
        last_page=last_page,
        status=status,
        final_url=final_url,
        content_type=content_type,
        encoding=decode.encoding,
        body_sha256=body_sha,
        body_bytes=len(body_bytes),
        snapshot_outcome=outcome.status,
        floors=floors,
        diagnostics=tuple(diagnostics),
    )
