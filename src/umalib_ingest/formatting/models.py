"""TASK-013 格式化模块：不可变文档结构与提取模型（M03）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_FORMAT_DEPTH = 16

_NAMED_COLORS: dict[str, str] = {
    "black": "#000000",
    "white": "#ffffff",
    "red": "#ff0000",
    "green": "#008000",
    "blue": "#0000ff",
    "yellow": "#ffff00",
    "orange": "#ffa500",
    "purple": "#800080",
    "gray": "#808080",
    "grey": "#808080",
}

_FORMAT_TAGS = ("b", "i", "u", "s")


@dataclass(frozen=True)
class Run:
    """One styled text run; color is normalized lowercase #rrggbb."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str | None = None

    @property
    def formatted(self) -> bool:
        return self.bold or self.italic or self.underline or self.strike or self.color is not None


@dataclass(frozen=True)
class Paragraph:
    """An ordered sequence of runs."""

    runs: tuple[Run, ...] = ()
    annotation: bool = False

    @property
    def plain_text(self) -> str:
        return "".join(run.text for run in self.runs)

    @property
    def formatted(self) -> bool:
        return any(run.formatted for run in self.runs)


@dataclass(frozen=True)
class FormattedDocument:
    """Immutable ordered paragraphs; no HTML/CSS/URL/event content."""

    paragraphs: tuple[Paragraph, ...] = ()

    @property
    def plain_text(self) -> str:
        return "\n".join(p.plain_text for p in self.paragraphs)


@dataclass(frozen=True)
class FormatDiagnostic:
    """One machine-identifiable formatting diagnostic."""

    severity: str  # blocker / warning / info
    code: str
    message: str


@dataclass(frozen=True)
class FormatResult:
    """Result of parsing basic UBB into a document."""

    document: FormattedDocument
    plain_text: str
    diagnostics: tuple[FormatDiagnostic, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(d.severity == "blocker" for d in self.diagnostics)


@dataclass(frozen=True)
class BodyExtraction:
    """Suggested presentation body; never modifies parser results."""

    body: str
    mode: str  # auto / manual
    diagnostics: tuple[FormatDiagnostic, ...] = ()

    @property
    def ready(self) -> bool:
        return not any(d.severity == "blocker" for d in self.diagnostics)


def normalize_color(value: str) -> str | None:
    """Normalize a color token to lowercase #rrggbb or return None."""
    token = value.strip().lower()
    if token in _NAMED_COLORS:
        return _NAMED_COLORS[token]
    if len(token) == 7 and token[0] == "#":
        body = token[1:]
        if all(c in "0123456789abcdef" for c in body):
            return "#" + body
        return None
    if len(token) == 4 and token[0] == "#":
        body = token[1:]
        if all(c in "0123456789abcdef" for c in body):
            return "#" + "".join(c * 2 for c in body)
        return None
    return None


def is_format_tag(name: str) -> bool:
    """True when a bracket token is a known UBB format tag."""
    lowered = name.lower()
    if lowered in _FORMAT_TAGS:
        return True
    if lowered.startswith("color="):
        return True
    if lowered.startswith("/") and lowered[1:] in _FORMAT_TAGS:
        return True
    return False
