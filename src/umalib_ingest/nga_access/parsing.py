"""Minimal NGA thread/floor structure parsing PoC (TASK-010).

Conservative, structure-ID based adapters. Selectors are documented as initial
structure assumptions that MUST be re-validated against the first real
logged-in response; if nothing recognizable is found the probe reports
floor_structure_unrecognized instead of fabricating results.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Any

from umalib_ingest.nga_access.errors import NgaAccessError

_FLOOR_ROW_RE = re.compile(
    r"<tr[^>]*\bid\s*=\s*['\"]post1strow\d+['\"][^>]*>", re.IGNORECASE
)
_PID_ANCHOR_RE = re.compile(
    r"<a[^>]*\bid\s*=\s*['\"]pid(\d+)Anchor['\"]", re.IGNORECASE
)
_AUTHOR_ANCHOR_RE = re.compile(
    r"<a[^>]*\bid\s*=\s*['\"]postauthor\d+['\"]", re.IGNORECASE
)
_DATE_SPAN_RE = re.compile(
    r"<span[^>]*\bid\s*=\s*['\"]postdate\d+['\"][^>]*>(.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
_CONTENT_SPAN_RE = re.compile(
    r"<span[^>]*\bid\s*=\s*['\"]postcontent\d+['\"][^>]*>(.*?)</span>",
    re.IGNORECASE | re.DOTALL,
)
_PAGE_CFG_BODY_RE = re.compile(r"var\s+__PAGE\s*=\s*\{([^}]*)\}", re.IGNORECASE)
_PAGE_KEY_RE = re.compile(r"(?:^|,)\s*(\d+)\s*:\s*(\d+)")
_NEXT_PAGE_RE = re.compile(
    r"read\.php\?tid=\d+&(?:amp;)?page=(\d+)[^>]*title=['\"]下一页",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# 已取证的可剥离 UBB 标签（保留 [术语] 等未知标记）。
_UBB_TAG_RE = re.compile(
    r"\[/?(?:b|u|i|s|size|color|url|quote|pid|uid|img|code|list|spoiler|"
    r"align|font|sub|sup|hr|email|audio|video|flash|tw|md|reply|author)"
    r"(?:=[^\]]*)?\]",
    re.IGNORECASE,
)
_UBB_EMOTICON_RE = re.compile(r"\[s:[^\]]*\]", re.IGNORECASE)


@dataclass(frozen=True)
class FloorSnapshot:
    """One floor's minimal, audit-friendly structure."""

    pid: str
    floor_number: int | None
    author_present: bool
    posted_at_raw: str | None
    edited_at_raw: str | None
    content_format: str  # "html" | "text"
    content_text: str
    content_raw_fragment: str


@dataclass(frozen=True)
class ThreadSnapshot:
    """One page's minimal thread structure."""

    tid: int
    title_present: bool
    current_page: int | None
    last_page: int | None
    floors: tuple[FloorSnapshot, ...]
    diagnostics: tuple[str, ...]


def _extract_balanced_div(html_text: str, start: int) -> tuple[str, int]:
    """Extract the balanced div block starting at the opening tag."""
    depth = 0
    index = start
    length = len(html_text)
    while index < length:
        next_open = html_text.find("<div", index)
        next_close = html_text.find("</div>", index)
        if next_open == -1 and next_close == -1:
            return html_text[start:], length
        if next_close == -1 or (next_open != -1 and next_open < next_close):
            depth += 1
            index = next_open + 4
        else:
            depth -= 1
            index = next_close + 6
            if depth == 0:
                return html_text[start:index], index
    return html_text[start:], length


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_lib.unescape(text).strip()


def _strip_ubb(fragment: str) -> str:
    """Remove known UBB tags and emoticons; keeps unknown [术语] tokens."""
    text = _UBB_TAG_RE.sub("", fragment)
    text = _UBB_EMOTICON_RE.sub("", text)
    return text.strip()


def _first_text(pattern: re.Pattern[str], fragment: str) -> str | None:
    match = pattern.search(fragment)
    if not match:
        return None
    return _strip_tags(match.group(1))


def _first_raw(pattern: re.Pattern[str], fragment: str) -> str | None:
    match = pattern.search(fragment)
    if not match:
        return None
    return match.group(1)


def parse_html_thread(html_text: str, tid: int) -> ThreadSnapshot:
    """Parse one NGA thread page into a minimal ThreadSnapshot.

    Structure based on the real logged-in response evidence (2026-08-31):
    <tr id='post1strowN'> rows with <a id='pid{PID}Anchor'>, postauthorN,
    postdateN and postcontentN (UBB). The topic row uses a pid=0 placeholder
    and is excluded from floors. Last page comes from __PAGE[1] when present.
    """
    if not isinstance(html_text, str):
        raise NgaAccessError("floor_structure_unrecognized", "页面文本类型非法")
    title_present = _TITLE_RE.search(html_text) is not None

    current_page: int | None = None
    last_page: int | None = None
    page_cfg = _PAGE_CFG_BODY_RE.search(html_text)
    if page_cfg:
        for key, value in _PAGE_KEY_RE.findall(page_cfg.group(1)):
            if key == "1":
                last_page = int(value)
            elif key == "2":
                current_page = int(value)
    if last_page is None:
        next_pages = [int(p) for p in _NEXT_PAGE_RE.findall(html_text)]
        if next_pages:
            last_page = max(next_pages)

    floors: list[FloorSnapshot] = []
    seen_pids: set[str] = set()
    cursor = 0
    while True:
        row_match = _FLOOR_ROW_RE.search(html_text, cursor)
        if not row_match:
            break
        row_start = row_match.start()
        next_row = _FLOOR_ROW_RE.search(html_text, row_match.end())
        row_end = next_row.start() if next_row else len(html_text)
        row = html_text[row_start:row_end]
        cursor = row_end

        pid_match = _PID_ANCHOR_RE.search(row)
        if pid_match is None:
            continue
        pid = pid_match.group(1)
        if pid == "0":
            continue
        if pid in seen_pids:
            raise NgaAccessError(
                "duplicate_floor_pid", f"同一快照内楼层 pid 重复: {pid}"
            )
        seen_pids.add(pid)

        author_present = _AUTHOR_ANCHOR_RE.search(row) is not None
        posted_at_raw = _first_text(_DATE_SPAN_RE, row)
        content_raw = _first_raw(_CONTENT_SPAN_RE, row)
        if content_raw is None:
            continue
        content_format = (
            "ubbcode"
            if _UBB_TAG_RE.search(content_raw) or _UBB_EMOTICON_RE.search(content_raw)
            else "text"
        )
        floors.append(
            FloorSnapshot(
                pid=pid,
                floor_number=None,
                author_present=author_present,
                posted_at_raw=posted_at_raw,
                edited_at_raw=None,
                content_format=content_format,
                content_text=_strip_ubb(_strip_tags(content_raw)),
                content_raw_fragment=content_raw,
            )
        )

    diagnostics: list[str] = []
    if not floors:
        diagnostics.append("floor_structure_unrecognized")
    if last_page is None:
        diagnostics.append("pagination_uncertain")
    return ThreadSnapshot(
        tid=tid,
        title_present=title_present,
        current_page=current_page,
        last_page=last_page,
        floors=tuple(floors),
        diagnostics=tuple(diagnostics),
    )


def parse_json_thread(payload: dict[str, Any], tid: int) -> ThreadSnapshot:
    """Experimental adapter for the real __output=11 shape (observed 2026-08-31).

    Shape: {data: {__PAGE: int, __ROWS: int, __R: [{pid, lou, postdate, content, ...}]}, time}.
    Not an official API; only records floors when the shape is unambiguous.
    """
    floors: list[FloorSnapshot] = []
    seen: set[str] = set()
    data = payload.get("data")
    rows = data.get("__R") if isinstance(data, dict) else None
    current_page = data.get("__PAGE") if isinstance(data, dict) else None
    if isinstance(rows, list):
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("pid")
            if pid is None or str(pid) == "0":
                continue
            pid_text = str(pid)
            if pid_text in seen:
                raise NgaAccessError("duplicate_floor_pid", f"JSON 楼层 pid 重复: {pid_text}")
            seen.add(pid_text)
            body = entry.get("content")
            if not isinstance(body, str):
                continue
            content_format = (
                "ubbcode"
                if _UBB_TAG_RE.search(body) or _UBB_EMOTICON_RE.search(body)
                else "text"
            )
            floors.append(
                FloorSnapshot(
                    pid=pid_text,
                    floor_number=entry.get("lou"),
                    author_present=entry.get("authorid") is not None,
                    posted_at_raw=entry.get("postdate"),
                    edited_at_raw=None,
                    content_format=content_format,
                    content_text=_strip_ubb(body),
                    content_raw_fragment=body,
                )
            )
    diagnostics: list[str] = []
    if not floors:
        diagnostics.append("floor_structure_unrecognized")
    diagnostics.append("json_observation_experimental")
    return ThreadSnapshot(
        tid=tid,
        title_present=isinstance(data, dict) and isinstance(data.get("__T"), dict),
        current_page=current_page if isinstance(current_page, int) else None,
        last_page=None,
        floors=tuple(floors),
        diagnostics=tuple(diagnostics),
    )


def load_json_payload(body_bytes: bytes, encoding: str | None) -> dict[str, Any] | None:
    """Try to parse a JSON observation body; returns None when not JSON."""
    if encoding is None:
        return None
    try:
        value = json.loads(body_bytes.decode(encoding))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value
