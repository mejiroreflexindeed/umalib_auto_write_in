"""TASK-013 基本 UBB 解析器（M03）。

栈式解析，不做正则链替换。支持 b/i/u/s/color 的正确嵌套；深度上限 16；
未闭合/交叉闭合/非法颜色为 blocker；未知 `[关键词]` 文本与不支持格式保留
原文（不静默删除）。结尾连续 `[关键词]注释` 段落识别为 annotation：
含格式时 blocker `annotation_format_unsupported`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from umalib_ingest.formatting.models import (
    MAX_FORMAT_DEPTH,
    FormatDiagnostic,
    FormatResult,
    FormattedDocument,
    Paragraph,
    Run,
    normalize_color,
)

_TAG_RE = re.compile(r"^\[(/?)([^\]]+)\]$")
_ANNOTATION_RE = re.compile(r"^\[([^\]]+)\]\s*\S.*$")
_UNSUPPORTED_TAGS = frozenset({
    "img", "attach", "table", "size", "align", "collapse", "quote",
    "url", "list", "code", "del", "sub", "sup", "email", "flash",
})


def _is_annotation_text(text: str) -> bool:
    """True when the paragraph is entirely one unknown-bracket annotation."""
    from umalib_ingest.formatting.models import is_format_tag

    match = _ANNOTATION_RE.match(text)
    if match is None:
        return False
    return not is_format_tag(match.group(1))


@dataclass
class _Style:
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str | None = None

    def copy(self) -> "_Style":
        return _Style(self.bold, self.italic, self.underline, self.strike, self.color)


def _style_keys() -> tuple[str, ...]:
    return ("bold", "italic", "underline", "strike", "color")


def parse_basic_ubb(body: str) -> FormatResult:
    """Parse basic UBB text into an immutable FormattedDocument."""
    if not isinstance(body, str):
        raise TypeError("body 必须是字符串")
    diagnostics: list[FormatDiagnostic] = []
    paragraphs: list[Paragraph] = []
    runs: list[Run] = []
    text_buffer: list[str] = []
    stack: list[tuple[str, str]] = []  # (tag, color-or-"")
    style = _Style()
    normalized = body.replace("\r\n", "\n").replace("\r", "\n")

    def flush_text() -> None:
        if text_buffer:
            runs.append(Run(text="".join(text_buffer), **style.__dict__))
            text_buffer.clear()

    def push(tag: str, color: str) -> bool:
        if len(stack) >= MAX_FORMAT_DEPTH:
            diagnostics.append(FormatDiagnostic(
                "blocker", "format_depth_exceeded",
                f"格式嵌套超过上限 {MAX_FORMAT_DEPTH}",
            ))
            return False
        stack.append((tag, color))
        if tag == "b":
            style.bold = True
        elif tag == "i":
            style.italic = True
        elif tag == "u":
            style.underline = True
        elif tag == "s":
            style.strike = True
        elif tag == "color":
            style.color = color
        return True

    def pop(tag: str) -> None:
        if not stack or stack[-1][0] != tag:
            diagnostics.append(FormatDiagnostic(
                "blocker", "format_cross_close", f"闭合标记 [/ {tag}] 与打开顺序不符",
            ))
            return
        stack.pop()
        if tag == "b":
            style.bold = False
        elif tag == "i":
            style.italic = False
        elif tag == "u":
            style.underline = False
        elif tag == "s":
            style.strike = False
        elif tag == "color":
            style.color = None

    for line in normalized.split("\n"):
        # 逐段扫描该行内的 UBB 标记
        position = 0
        buffer: list[str] = []
        length = len(line)

        def flush_line_buffer() -> None:
            if buffer:
                text_buffer.append("".join(buffer))
                buffer.clear()

        while position < length:
            if line[position] != "[":
                buffer.append(line[position])
                position += 1
                continue
            end = line.find("]", position)
            if end == -1:
                buffer.append(line[position:])
                position = length
                continue
            token = line[position + 1:end]
            match = _TAG_RE.match(line[position:end + 1])
            if match is None:
                # 非格式方括号（如 [术语]注释）：原样保留为文本
                buffer.append(line[position:end + 1])
                position = end + 1
                continue
            closing = match.group(1) == "/"
            name = match.group(2).strip().lower()
            if not closing and name in _UNSUPPORTED_TAGS:
                diagnostics.append(FormatDiagnostic(
                    "warning", "unsupported_format", f"不支持的格式标记 [{name}]，已保留原文",
                ))
                buffer.append(line[position:end + 1])
                position = end + 1
                continue
            if not closing and name.startswith("color="):
                color = normalize_color(name[len("color="):])
                if color is None:
                    diagnostics.append(FormatDiagnostic(
                        "blocker", "invalid_color", f"非法颜色值: {name[len('color='):]!r}",
                    ))
                    buffer.append(line[position:end + 1])
                    position = end + 1
                    continue
                flush_line_buffer()
                flush_text()
                push("color", color)
                position = end + 1
                continue
            if not closing and name in ("b", "i", "u", "s"):
                flush_line_buffer()
                flush_text()
                push(name, "")
                position = end + 1
                continue
            if closing and name in ("b", "i", "u", "s", "color"):
                flush_line_buffer()
                flush_text()
                pop(name)
                position = end + 1
                continue
            # 其他（/img 之类）：保留原文
            buffer.append(line[position:end + 1])
            position = end + 1
        flush_line_buffer()
        if text_buffer:
            runs.append(Run(text="".join(text_buffer), **style.__dict__))
            text_buffer.clear()
        paragraphs.append(Paragraph(tuple(runs)))
        runs = []
        # 行尾未闭合：保留到行级检查（跨行未闭合不合并，但格式状态跨行延续是合法的 NGA 行为）
    if stack:
        diagnostics.append(FormatDiagnostic(
            "blocker", "format_unclosed",
            f"存在未闭合的格式标记: {', '.join(tag for tag, _ in stack)}",
        ))

    paragraphs = [_merge_runs(p) for p in paragraphs]

    # 结尾连续注释段落识别
    paragraphs = _mark_trailing_annotations(paragraphs, diagnostics)

    document = FormattedDocument(tuple(paragraphs))
    return FormatResult(document=document, plain_text=document.plain_text,
                        diagnostics=tuple(diagnostics))


def _merge_runs(paragraph: Paragraph) -> Paragraph:
    """Merge adjacent runs with identical style (fixed serialization rule)."""
    merged: list[Run] = []
    for run in paragraph.runs:
        if merged and merged[-1].bold == run.bold and merged[-1].italic == run.italic \
                and merged[-1].underline == run.underline and merged[-1].strike == run.strike \
                and merged[-1].color == run.color:
            previous = merged[-1]
            merged[-1] = Run(
                text=previous.text + run.text,
                bold=run.bold, italic=run.italic, underline=run.underline,
                strike=run.strike, color=run.color,
            )
        else:
            merged.append(run)
    return Paragraph(tuple(merged), annotation=paragraph.annotation)


def _mark_trailing_annotations(
    paragraphs: list[Paragraph], diagnostics: list[FormatDiagnostic]
) -> list[Paragraph]:
    """Mark trailing consecutive annotation paragraphs; format in them blocks."""
    marked = list(paragraphs)
    index = len(marked)
    while index > 0:
        candidate = marked[index - 1]
        text = candidate.plain_text.strip()
        if not text or not _is_annotation_text(text):
            break
        if candidate.formatted:
            diagnostics.append(FormatDiagnostic(
                "blocker", "annotation_format_unsupported",
                f"结尾注释定义段含格式，需人工去掉该段格式: {text[:40]}",
            ))
            break
        marked[index - 1] = Paragraph(runs=candidate.runs, annotation=True)
        index -= 1
    return marked
