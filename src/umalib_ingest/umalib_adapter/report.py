"""Deterministic inspection report model (TASK-006)."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

REPORT_SCHEMA_VERSION = "1.0"

_SEVERITY_ORDER = {"blocker": 0, "warning": 1, "info": 2}


def _stable_json(obj: dict[str, object]) -> str:
    """Serialize with stable key order for deterministic reports."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class InspectionIssue:
    """One deterministic inspection issue."""

    severity: str
    code: str
    message: str


def sort_issues(issues: list[InspectionIssue]) -> list[InspectionIssue]:
    """Return issues ordered by severity (blocker, warning, info) then code."""
    return sorted(
        issues,
        key=lambda issue: (_SEVERITY_ORDER.get(issue.severity, 99), issue.code, issue.message),
    )


@dataclass(frozen=True, init=False)
class UmaLibInspectionReport:
    """A deterministic, minimal-disclosure read-only inspection report."""

    report_schema_version: str
    _database: dict[str, object] = field(repr=False)
    _schema: dict[str, object] = field(repr=False)
    _counts: dict[str, object] = field(repr=False)
    _tag_audit: dict[str, object] = field(repr=False)
    _article_features: dict[str, object] = field(repr=False)
    _integrity: dict[str, object] = field(repr=False)
    issues: tuple[InspectionIssue, ...] = field(default_factory=tuple)

    def __init__(
        self,
        *,
        report_schema_version: str,
        database: dict[str, object],
        schema: dict[str, object],
        counts: dict[str, object],
        tag_audit: dict[str, object],
        article_features: dict[str, object],
        integrity: dict[str, object],
        issues: tuple[InspectionIssue, ...] = (),
    ) -> None:
        """Copy all mutable inputs so the report cannot be polluted externally."""
        object.__setattr__(self, "report_schema_version", report_schema_version)
        object.__setattr__(self, "_database", copy.deepcopy(database))
        object.__setattr__(self, "_schema", copy.deepcopy(schema))
        object.__setattr__(self, "_counts", copy.deepcopy(counts))
        object.__setattr__(self, "_tag_audit", copy.deepcopy(tag_audit))
        object.__setattr__(self, "_article_features", copy.deepcopy(article_features))
        object.__setattr__(self, "_integrity", copy.deepcopy(integrity))
        object.__setattr__(self, "issues", tuple(issues))

    @property
    def database(self) -> dict[str, object]:
        """Return a fresh copy of database identification fields."""
        return copy.deepcopy(self._database)

    @property
    def schema(self) -> dict[str, object]:
        """Return a fresh copy of schema inspection fields."""
        return copy.deepcopy(self._schema)

    @property
    def counts(self) -> dict[str, object]:
        """Return a fresh copy of table row counts."""
        return copy.deepcopy(self._counts)

    @property
    def tag_audit(self) -> dict[str, object]:
        """Return a fresh copy of tag audit details."""
        return copy.deepcopy(self._tag_audit)

    @property
    def article_features(self) -> dict[str, object]:
        """Return a fresh copy of article feature counts."""
        return copy.deepcopy(self._article_features)

    @property
    def integrity(self) -> dict[str, object]:
        """Return a fresh copy of integrity results."""
        return copy.deepcopy(self._integrity)

    def to_dict(self) -> dict[str, object]:
        """Return a fresh deep copy of the report as a plain dict."""
        return {
            "report_schema_version": self.report_schema_version,
            "database": copy.deepcopy(self._database),
            "schema": copy.deepcopy(self._schema),
            "counts": copy.deepcopy(self._counts),
            "tag_audit": copy.deepcopy(self._tag_audit),
            "article_features": copy.deepcopy(self._article_features),
            "integrity": copy.deepcopy(self._integrity),
            "issues": [
                {"severity": issue.severity, "code": issue.code, "message": issue.message}
                for issue in self.issues
            ],
        }

    def to_json(self) -> str:
        """Return the stable JSON serialization of the full report."""
        return _stable_json(self.to_dict())
