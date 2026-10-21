"""TASK-013 安全 Quill 子集渲染与独立验证（M03/M04）。

render_quill_html 输出规范确定性 HTML：
  <p>…</p>、<p><br></p>、<strong>、<em>、<u>、<s>、<span style="color: #rrggbb;">
  固定嵌套顺序 strong/em/u/s/span；文本正确转义。
validate_quill_subset 只接受上述子集与原有纯文本子集（p/br + 转义文本），
拒绝其他标签、属性、事件、脚本、SVG、图片、URL 与 CSS 表达式。
"""

from __future__ import annotations

import html
import re

_ESCAPE = lambda text: html.escape(text, quote=False)  # noqa: E731

_ANNOTATION_RE = re.compile(r"^\[[^\]]+\]\s*\S.*$")


def render_quill_html(document) -> str:
    """Render a FormattedDocument into canonical Quill-subset HTML."""
    parts: list[str] = []
    for paragraph in document.paragraphs:
        if not paragraph.runs:
            parts.append("<p><br></p>")
            continue
        if paragraph.annotation:
            parts.append(f"<p>{_ESCAPE(paragraph.plain_text)}</p>")
            continue
        content: list[str] = []
        for run in paragraph.runs:
            text = _ESCAPE(run.text)
            if run.color is not None:
                text = f'<span style="color: {run.color};">{text}</span>'
            if run.strike:
                text = f"<s>{text}</s>"
            if run.underline:
                text = f"<u>{text}</u>"
            if run.italic:
                text = f"<em>{text}</em>"
            if run.bold:
                text = f"<strong>{text}</strong>"
            content.append(text)
        parts.append(f"<p>{''.join(content)}</p>")
    return "".join(parts)


_ALLOWED_SPAN_STYLE_RE = re.compile(r'^style="color: #[0-9a-f]{6};"$')
_ALLOWED_TAGS = {"p", "br", "strong", "em", "u", "s", "span"}
_VOID = {"br"}


def validate_quill_subset(quill_html: str) -> bool:
    """Strictly validate the canonical Quill subset (no parser shortcuts).

    Accepts: <p>, <br>（仅作为整个 <p> 内部）, <strong>, <em>, <u>, <s>,
    <span style="color: #rrggbb;"> and escaped text. 保留旧纯文本语义：
    拒绝控制字符、空 <p></p>、<p> 内混合文本与 <br>、整体无有效文本。
    """
    if not isinstance(quill_html, str) or quill_html == "":
        return False
    if any(ord(char) < 32 and char != "\t" for char in quill_html):
        return False
    position = 0
    length = len(quill_html)
    stack: list[str] = []
    has_text = False
    paragraph_kind: str | None = None  # None 尚未有内容 / "br" / "text"

    def fail() -> bool:
        return False

    while position < length:
        char = quill_html[position]
        if char == "<":
            end = quill_html.find(">", position)
            if end == -1:
                return fail()
            inner = quill_html[position + 1:end]
            if inner.startswith("/"):
                tag = inner[1:].strip()
                if tag == "p":
                    if not stack or stack[-1] != "p":
                        return fail()
                    stack.pop()
                    if paragraph_kind is None:
                        return fail()  # 空 <p></p>
                    paragraph_kind = None
                else:
                    if tag not in _ALLOWED_TAGS or tag in _VOID:
                        return fail()
                    if not stack or stack[-1] != tag:
                        return fail()
                    stack.pop()
                position = end + 1
                continue
            parts = inner.split(None, 1)
            tag = parts[0]
            if tag == "p":
                if len(parts) > 1:
                    return fail()
                stack.append("p")
                paragraph_kind = None
                position = end + 1
                continue
            if tag == "br":
                if len(parts) > 1:
                    return fail()
                if not stack or stack[-1] != "p":
                    return fail()
                if paragraph_kind is not None:
                    return fail()  # <p> 内混合 <br> 与内容
                paragraph_kind = "br"
                position = end + 1
                continue
            if tag not in _ALLOWED_TAGS:
                return fail()
            if tag == "span":
                attrs = parts[1].strip() if len(parts) > 1 else ""
                if _ALLOWED_SPAN_STYLE_RE.match(attrs) is None:
                    return fail()
            elif len(parts) > 1:
                return fail()
            if not stack or stack[-1] not in _ALLOWED_TAGS:
                return fail()
            stack.append(tag)
            if paragraph_kind == "br":
                return fail()
            if paragraph_kind is None:
                paragraph_kind = "text"
            position = end + 1
            continue
        if char == "&":
            semicolon = quill_html.find(";", position)
            if semicolon == -1 or semicolon - position > 8:
                return fail()
            entity = quill_html[position:semicolon + 1]
            if entity not in ("&lt;", "&gt;", "&amp;"):
                return fail()
            if not stack or paragraph_kind == "br":
                return fail()
            paragraph_kind = "text"
            has_text = True
            position = semicolon + 1
            continue
        if not stack or paragraph_kind == "br":
            return fail()
        if char not in (" ", "\t", "\n"):
            has_text = True
        if paragraph_kind is None:
            paragraph_kind = "text"
        position += 1
    return not stack and has_text


def validate_plain_quill_subset(quill_html: str) -> bool:
    """Accept only the plain-text subset (<p>, <br> and escaped text)."""
    if not isinstance(quill_html, str):
        return False
    position = 0
    length = len(quill_html)
    stack: list[str] = []
    while position < length:
        char = quill_html[position]
        if char == "<":
            end = quill_html.find(">", position)
            if end == -1:
                return False
            inner = quill_html[position + 1:end]
            if inner.startswith("/"):
                tag = inner[1:].strip()
                if tag != "p" or not stack or stack[-1] != "p":
                    return False
                stack.pop()
            elif inner == "p":
                stack.append("p")
            elif inner == "br":
                pass
            else:
                return False
            position = end + 1
            continue
        if char == "&":
            semicolon = quill_html.find(";", position)
            if semicolon == -1 or semicolon - position > 8:
                return False
            entity = quill_html[position:semicolon + 1]
            if entity not in ("&lt;", "&gt;", "&amp;"):
                return False
            position = semicolon + 1
            continue
        position += 1
    return not stack
