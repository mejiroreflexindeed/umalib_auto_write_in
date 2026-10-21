"""TASK-013 来源 URL 规范化纯函数（M05）。

只在 UI 中显式应用；不自动修改 Parser 结果、不访问外站。仅对明确的
bbs.nga.cn/read.php 做楼层规范化；其他 HTTP(S) 链接只做基本形状校验并原样
保留。重复冲突、非法 ID、userinfo、非默认端口均给明确错误，不猜测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_NGA_HOSTS = ("bbs.nga.cn", "www.bbs.nga.cn", "nga.178.com")
_PID_RE = re.compile(r"^[1-9][0-9]*$")
_ASCII_WHITESPACE = " \t\n\r\v\f"


@dataclass(frozen=True)
class SourceUrlResult:
    """Normalization outcome for one source URL."""

    original: str
    normalized: str | None = None
    changed: bool = False
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers


def normalize_source_url(url: str) -> SourceUrlResult:
    """Normalize one source URL; never guesses, never makes network calls."""
    if not isinstance(url, str) or url == "":
        return SourceUrlResult(original=url or "", blockers=("url_empty",))
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        return SourceUrlResult(original=url, blockers=("url_control_chars",))
    if any(char in _ASCII_WHITESPACE for char in url):
        return SourceUrlResult(
            original=url,
            warnings=("url_contains_whitespace",),
            blockers=("url_contains_whitespace",),
        )
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return SourceUrlResult(original=url, blockers=("url_unparseable",))
    if parts.scheme.lower() not in ("http", "https"):
        return SourceUrlResult(original=url, blockers=("url_scheme_unsupported",))
    host = (parts.hostname or "").lower()
    if host == "":
        return SourceUrlResult(original=url, blockers=("url_no_host",))
    if parts.username is not None or parts.password is not None:
        return SourceUrlResult(original=url, blockers=("url_userinfo_forbidden",))
    if port is not None and port not in (80, 443):
        return SourceUrlResult(original=url, blockers=("url_nondefault_port",))
    if parts.fragment:
        return SourceUrlResult(original=url, blockers=("url_fragment_forbidden",))

    if host not in _NGA_HOSTS or parts.path != "/read.php":
        # 其他正常 HTTP(S) 外链：仅基本形状校验，原样保留
        return SourceUrlResult(original=url, normalized=url, changed=False)

    query_pairs: list[tuple[str, str]] = []
    tid: str | None = None
    pid: str | None = None
    warnings: list[str] = []
    blockers: list[str] = []
    for pair in parts.query.split("&"):
        if pair == "":
            continue
        if "=" not in pair:
            blockers.append("url_query_malformed")
            continue
        key, value = pair.split("=", 1)
        if key in ("tid", "pid", "page", "to"):
            if key in ("tid", "pid"):
                if key == "tid" and tid is not None and tid != value:
                    blockers.append("url_tid_conflict")
                if key == "pid" and pid is not None and pid != value:
                    blockers.append("url_pid_conflict")
                if key == "tid":
                    tid = value
                else:
                    pid = value
            elif key == "to" and value == "1":
                warnings.append("url_to_removed")
            # page 与定位无关：去除
            continue
        blockers.append(f"url_unknown_param:{key}")
    if blockers:
        return SourceUrlResult(original=url, warnings=tuple(warnings), blockers=tuple(blockers))
    if tid is not None and _PID_RE.fullmatch(tid) is None:
        return SourceUrlResult(original=url, warnings=tuple(warnings), blockers=("url_tid_invalid",))
    if pid is not None and _PID_RE.fullmatch(pid) is None:
        return SourceUrlResult(original=url, warnings=tuple(warnings), blockers=("url_pid_invalid",))
    if tid is None and pid is None:
        return SourceUrlResult(original=url, warnings=tuple(warnings), blockers=("url_no_tid",))
    if tid is None:
        return SourceUrlResult(
            original=url, warnings=tuple(warnings), blockers=("url_pid_without_tid",),
        )
    if pid is None:
        warnings.append("url_tid_only_no_floor")
        normalized = f"https://bbs.nga.cn/read.php?tid={tid}"
    else:
        normalized = f"https://bbs.nga.cn/read.php?tid={tid}&pid={pid}"
    return SourceUrlResult(
        original=url,
        normalized=normalized,
        changed=normalized != url,
        warnings=tuple(warnings),
    )
