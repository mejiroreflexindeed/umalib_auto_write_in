"""Playwright browser adapter for the NGA authenticated-read PoC (TASK-010).

Playwright is imported lazily; the default application never imports it.
Only the system Microsoft Edge channel is used, headless=False, in an
explicit independent persistent profile directory.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from umalib_ingest.nga_access.errors import NgaAccessError

_NGA_EXACT_HOSTS = ("bbs.nga.cn", "www.nga.cn", "nga.cn")
_NGA_HOST_SUFFIXES = (".nga.cn", ".nga.178.com", ".178.com")
_BLOCKED_RESOURCE_TYPES = ("image", "media", "font")


def should_block_request(url: str, resource_type: str) -> bool:
    """Decide whether a subresource request must be aborted (pure).

    Blocks image/media/font downloads and any request to a non-NGA host.
    Main-frame document navigations are not blocked here; the final URL is
    validated strictly before any content is saved.
    """
    if resource_type in _BLOCKED_RESOURCE_TYPES:
        return True
    if resource_type == "document":
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    if host in _NGA_EXACT_HOSTS:
        return False
    return not any(host.endswith(suffix) for suffix in _NGA_HOST_SUFFIXES)


def install_subresource_guard(context) -> None:
    """Abort blocked subresource requests; never touches the main document."""

    def handler(route) -> None:
        request = route.request
        if should_block_request(request.url, request.resource_type):
            route.abort()
        else:
            route.continue_()

    context.route("**/*", handler)


class ProbeSession:
    """Owns a Playwright instance and one persistent Edge context."""

    def __init__(self, playwright, context) -> None:
        self._playwright = playwright
        self._context = context
        self._closed = False

    @property
    def context(self):
        """Return the persistent browser context."""
        return self._context

    @property
    def page(self):
        """Return the single context page (created on demand)."""
        pages = self._context.pages
        if pages:
            return pages[0]
        return self._context.new_page()

    def close(self) -> None:
        """Always close the context and the Playwright driver."""
        if self._closed:
            return
        self._closed = True
        try:
            self._context.close()
        finally:
            self._playwright.stop()


def playwright_not_installed_error() -> NgaAccessError:
    """Safe error when the optional Playwright dependency is missing."""
    return NgaAccessError(
        "playwright_not_installed",
        "未安装可选依赖 playwright。请先创建项目虚拟环境并安装（见 docs/10 安装命令），"
        "再运行本命令；默认审核/Writer 功能不依赖它。",
    )


def _import_sync_playwright():
    """Import the sync Playwright API; only ever called lazily."""
    from playwright.sync_api import sync_playwright

    return sync_playwright


def launch_probe_session(profile_dir: Path) -> ProbeSession:
    """Launch the system Edge channel in an independent persistent profile.

    Raises playwright_not_installed / edge_channel_unavailable / unsafe
    errors; never downloads browsers and never touches default profiles.
    """
    try:
        sync_playwright = _import_sync_playwright()
    except ImportError:
        raise playwright_not_installed_error() from None

    playwright = sync_playwright().start()
    try:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="msedge",
            headless=False,
        )
        install_subresource_guard(context)
    except Exception as exc:
        playwright.stop()
        message = str(exc)
        if "Executable doesn't exist" in message or "msedge" in message.lower():
            raise NgaAccessError(
                "edge_channel_unavailable",
                "本机未找到可用的 Microsoft Edge 稳定版 channel；"
                "本任务不下载或覆盖浏览器，请先安装 Edge 后重试。",
            ) from None
        raise NgaAccessError(
            "edge_channel_unavailable",
            "无法启动系统 Edge（未下载任何浏览器）。",
        ) from None
    return ProbeSession(playwright, context)
