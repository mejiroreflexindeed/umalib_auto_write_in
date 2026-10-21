"""Deterministic response encoding detection and decoding (TASK-010)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_META_CHARSET_RE = re.compile(
    rb"<meta[^>]*charset\s*=\s*[\"']?\s*([\w-]+)", re.IGNORECASE
)
_CONTENT_TYPE_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class DecodeResult:
    """Deterministic decode outcome; never silently drops characters."""

    text: str | None
    encoding: str | None
    diagnostic: str | None  # "encoding_uncertain" or None


def decode_response(
    body: bytes,
    content_type: str | None,
    html_head: bytes | None = None,
) -> DecodeResult:
    """Decode response bytes using the documented priority order."""
    if not isinstance(body, bytes) or body == b"":
        return DecodeResult(text="", encoding="utf-8", diagnostic=None)

    candidates: list[str] = []
    if content_type:
        match = _CONTENT_TYPE_CHARSET_RE.search(content_type)
        if match:
            candidates.append(match.group(1).lower())
    head = html_head if html_head is not None else body[:4096]
    meta = _META_CHARSET_RE.search(head)
    if meta:
        candidates.append(meta.group(1).decode("ascii", "replace").lower())
    candidates.append("utf-8")
    candidates.append("gb18030")

    tried: set[str] = set()
    for codec in candidates:
        normalized = _normalize_codec(codec)
        if normalized is None or normalized in tried:
            continue
        tried.add(normalized)
        try:
            text = body.decode(normalized)
            return DecodeResult(text=text, encoding=normalized, diagnostic=None)
        except (UnicodeDecodeError, LookupError):
            continue
    return DecodeResult(text=None, encoding=None, diagnostic="encoding_uncertain")


def _normalize_codec(codec: str) -> str | None:
    """Map declared charsets onto Python codecs we trust."""
    value = codec.strip().lower()
    if value in ("gbk", "gb2312", "gb18030", "cp936"):
        return "gb18030"
    if value in ("utf-8", "utf8", "utf_8"):
        return "utf-8"
    try:
        import codecs

        codecs.lookup(value)
        return value
    except LookupError:
        return None
