"""Candidate write result contract (TASK-008)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from umalib_ingest.umalib_writer.preview import _deep_freeze, _deep_thaw

RESULT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class CandidateWriteResult:
    """Deep-immutable evidence that a candidate copy write completed."""

    schema_version: str
    plan_sha256: str
    preview_sha256: str
    source: Mapping[str, object]
    candidate: Mapping[str, object]
    inserted: Mapping[str, object]
    counts_before: Mapping[str, object]
    counts_after: Mapping[str, object]
    checks: Mapping[str, object]
    success: bool = True
    result_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _deep_freeze(self.source))
        object.__setattr__(self, "candidate", _deep_freeze(self.candidate))
        object.__setattr__(self, "inserted", _deep_freeze(self.inserted))
        object.__setattr__(self, "counts_before", _deep_freeze(self.counts_before))
        object.__setattr__(self, "counts_after", _deep_freeze(self.counts_after))
        object.__setattr__(self, "checks", _deep_freeze(self.checks))

    def to_dict(self) -> dict[str, object]:
        """Return a fresh deep copy of the result as a plain dict."""
        return {
            "schema_version": self.schema_version,
            "success": self.success,
            "plan_sha256": self.plan_sha256,
            "preview_sha256": self.preview_sha256,
            "source": _deep_thaw(self.source),
            "candidate": _deep_thaw(self.candidate),
            "inserted": _deep_thaw(self.inserted),
            "counts_before": _deep_thaw(self.counts_before),
            "counts_after": _deep_thaw(self.counts_after),
            "checks": _deep_thaw(self.checks),
            "result_sha256": self.result_sha256,
        }
