"""TASK-013 原文正文建议提取（M03）。

仅用于向审核员建议格式正文，不改 Parser 原始结果。规则固定：
CRLF/CR→LF；不 trim 每行、不合并空行；跳过头部触发行与字段块；已知 UBB
格式起始行是正文起点；字段块之后全部作为正文；只允许 <br> 换行与一次
HTML entity 解码，其他 HTML 结构提示人工填写。
"""

from __future__ import annotations

import html as html_module
import re

from umalib_ingest.formatting.models import BodyExtraction, FormatDiagnostic, is_format_tag

_FIELD_LINE_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
_TRIGGER_RE = re.compile(r"^\s*\[收录\]\s*$")
_HTML_STRUCT_RE = re.compile(r"<[a-zA-Z!/][^>]*>")


def extract_body_for_presentation(raw_text: str) -> BodyExtraction:
    """Extract a suggested body from raw submission text (pure)."""
    if not isinstance(raw_text, str):
        raise TypeError("raw_text 必须是字符串")
    diagnostics: list[FormatDiagnostic] = []
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # 找第一个非空行
    first_nonempty = None
    for index, line in enumerate(lines):
        if line.strip() != "":
            first_nonempty = index
            break
    if first_nonempty is None:
        return BodyExtraction(body="", mode="auto", diagnostics=tuple(diagnostics))

    cursor = first_nonempty
    if _TRIGGER_RE.match(lines[cursor].strip()):
        cursor += 1
        while cursor < len(lines):
            line = lines[cursor]
            if line.strip() == "":
                cursor += 1
                continue
            match = _FIELD_LINE_RE.match(line)
            if match is None or is_format_tag(match.group(1)):
                break
            cursor += 1

    body_lines = lines[cursor:]
    # 头部剩余空行保留（不 trim）；尾部空行保留
    body = "\n".join(body_lines)

    # HTML 传输表示：只允许 <br> 与一次 entity 解码
    tags = _HTML_STRUCT_RE.findall(body)
    if tags:
        allowed = {"<br>", "<br/>", "<br />"}
        if not set(tags).issubset(allowed):
            diagnostics.append(FormatDiagnostic(
                "blocker", "unsupported_html_extraction",
                "原文包含无法自动提取的 HTML 结构，请人工填写格式正文",
            ))
            return BodyExtraction(body="", mode="manual", diagnostics=tuple(diagnostics))
        body = re.sub(r"<br\s*/?>", "\n", body)
        body = html_module.unescape(body)

    return BodyExtraction(body=body, mode="auto", diagnostics=tuple(diagnostics))
