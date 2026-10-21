"""Strict thread URL normalization and validation (TASK-010, pure)."""

from __future__ import annotations

from urllib.parse import urlsplit

from umalib_ingest.nga_access.errors import NgaAccessError

ALLOWED_TID = 30099686
MAX_PAGES = 2
MIN_NAVIGATION_DELAY_SECONDS = 3.0

_HOST = "bbs.nga.cn"
_PATH = "/read.php"


def invalid(message: str) -> NgaAccessError:
    """Build the invalid-thread-url error."""
    return NgaAccessError("invalid_thread_url", message)


def parse_thread_url(url: str) -> tuple[int, int]:
    """Strictly parse a thread URL into (tid, page); raises on any violation."""
    if not isinstance(url, str) or url == "":
        raise invalid("URL 为空")
    if any(ord(c) < 32 or ord(c) == 127 for c in url):
        raise invalid("URL 含控制字符")
    if any(c in url for c in " \t\r\n"):
        raise invalid("URL 含空白字符")
    if "%" in url:
        raise invalid("URL 含编码分隔符，不接受百分号编码")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise invalid("URL 无法解析") from None
    if parts.scheme.lower() != "https":
        raise invalid("scheme 必须是 https")
    if parts.username is not None or parts.password is not None:
        raise invalid("URL 不得包含 userinfo")
    if parts.fragment:
        raise invalid("URL 不得包含 fragment")
    try:
        port = parts.port
    except ValueError:
        raise invalid("URL 端口非法") from None
    if port is not None:
        raise invalid("URL 不得包含非默认端口")
    if parts.hostname is None or parts.hostname.lower() != _HOST:
        raise invalid("host 必须精确为 bbs.nga.cn")
    if parts.path != _PATH:
        raise invalid("path 必须精确为 /read.php")
    if parts.query == "":
        raise invalid("缺少 tid 参数")
    tid: int | None = None
    page: int | None = None
    for pair in parts.query.split("&"):
        if "=" not in pair:
            raise invalid("查询参数格式错误")
        key, value = pair.split("=", 1)
        if key not in ("tid", "page"):
            raise invalid(f"不允许的查询参数: {key}")
        if value == "" or not value.isdecimal():
            raise invalid(f"参数 {key} 必须是十进制正整数")
        parsed_value = int(value)
        if parsed_value <= 0:
            raise invalid(f"参数 {key} 必须是正整数")
        if key == "tid":
            if tid is not None:
                raise invalid("tid 重复")
            tid = parsed_value
        else:
            if page is not None:
                raise invalid("page 重复")
            page = parsed_value
    if tid is None:
        raise invalid("缺少 tid")
    if page is None:
        page = 1
    return tid, page


def canonical_thread_url(tid: int, page: int = 1) -> str:
    """Build the program-generated canonical thread URL."""
    if type(tid) is not int or tid <= 0:
        raise invalid("tid 必须是正整数")
    if type(page) is not int or page <= 0:
        raise invalid("page 必须是正整数")
    if page <= 1:
        return f"https://{_HOST}{_PATH}?tid={tid}"
    return f"https://{_HOST}{_PATH}?tid={tid}&page={page}"


def json_probe_url(tid: int, page: int = 1) -> str:
    """Build the experimental one-shot __output=11 observation URL.

    Only ever used as an additional observation in the same browser context
    after the HTML path succeeded; never the sole success path.
    """
    base = canonical_thread_url(tid, page)
    return f"{base}&__output=11"


def check_online_tid(tid: int) -> None:
    """Enforce the fixed online-acceptance target tid."""
    if tid != ALLOWED_TID:
        raise invalid(f"本次在线验收只允许固定目标帖 tid={ALLOWED_TID}")


_LOGIN_PATHS = ("/login.php", "/login", "/login/")


def is_official_login_url(url: str) -> bool:
    """Precisely classify an HTTPS official NGA login page URL.

    Only HTTPS + official NGA host + a clearly login-related path qualifies,
    and userinfo / non-default ports are rejected like parse_thread_url does.
    This is intentionally narrow: other NGA pages are not login pages, and
    non-NGA hosts / HTTP / wrong tid remain unexpected redirects.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    if parts.username is not None or parts.password is not None:
        return False
    if port is not None:
        return False
    if host != "bbs.nga.cn" and not host.endswith(".nga.cn") and host != "nga.cn":
        return False
    path = parts.path.lower()
    if path in _LOGIN_PATHS:
        return True
    if path == "/nuke.php" and "func=login" in parts.query.lower():
        return True
    return False


def validate_final_url(url: str, expected_tid: int) -> tuple[int, int]:
    """Strictly validate the navigated final URL before saving any content.

    Raises unexpected_redirect when the final URL is not exactly the canonical
    target thread (scheme/host/path/tid) — the caller must stop before saving
    body bytes.
    """
    try:
        tid, page = parse_thread_url(url)
    except NgaAccessError:
        raise NgaAccessError(
            "unexpected_redirect", "最终页面地址不符合目标帖 URL 规范，已停止"
        ) from None
    if tid != expected_tid:
        raise NgaAccessError(
            "unexpected_redirect", f"最终页面 tid 与目标帖不一致（{tid}），已停止"
        )
    return tid, page
