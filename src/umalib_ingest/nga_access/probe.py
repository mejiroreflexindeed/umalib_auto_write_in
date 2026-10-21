"""NGA authenticated-read probe orchestration (TASK-010)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from umalib_ingest.nga_access import snapshot, urls
from umalib_ingest.nga_access.errors import NgaAccessError
from umalib_ingest.nga_access.fetch import _utc_now
from umalib_ingest.nga_access.paths import (
    ensure_distinct,
    validate_profile_dir,
    validate_snapshot_dir,
)


@dataclass(frozen=True)
class PageProbeResult:
    """Minimal summary of one page read; never holds body/content."""

    page_label: str
    status: int
    final_url: str
    content_type: str
    body_sha256: str
    body_bytes: int
    encoding: str | None
    snapshot_status: str
    floor_count: int
    unique_pid_count: int
    last_page: int | None
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class ProbeResult:
    """Overall probe outcome summary."""

    tid: int
    pages: tuple[PageProbeResult, ...]
    json_observation: dict[str, Any] | None
    diagnostics: tuple[str, ...]


def _wait_for_login_confirmation(wait_file: Path, timeout_seconds: float) -> None:
    """Poll for a user-authored login-confirmation file; never reads content."""
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if wait_file.exists():
            return
        time.sleep(2.0)
    raise NgaAccessError(
        "login_not_confirmed",
        "等待用户在官方页面手工登录确认超时；请确认已登录后重试。",
    )


def read_page(
    session,
    tid: int,
    page: int,
    page_label: str,
    snapshot_dir: Path,
    navigation_delay_seconds: float,
) -> PageProbeResult:
    """Navigate to one canonical page via the shared fetch API."""
    from umalib_ingest.nga_access.fetch import fetch_thread_page

    fetched = fetch_thread_page(session, tid, page, snapshot_dir)
    return PageProbeResult(
        page_label=page_label,
        status=fetched.status,
        final_url=fetched.final_url,
        content_type=fetched.content_type,
        body_sha256=fetched.body_sha256,
        body_bytes=fetched.body_bytes,
        encoding=fetched.encoding,
        snapshot_status=fetched.snapshot_outcome,
        floor_count=len(fetched.floors),
        unique_pid_count=len({floor.pid for floor in fetched.floors}),
        last_page=fetched.last_page,
        diagnostics=fetched.diagnostics,
    )


def observe_json(
    session,
    tid: int,
    page: int,
) -> dict[str, Any]:
    """One-shot optional __output=11 observation; never the success path."""
    url = urls.json_probe_url(tid, page)
    response = session.page.goto(url, wait_until="domcontentloaded")
    if response is None:
        return {"status": "no_response", "content_type": None, "body_sha256": None}
    body_bytes = response.body()
    content_type = response.headers.get("content-type", "")
    body_sha = snapshot.sha256_bytes(body_bytes)
    payload = None
    for encoding in ("utf-8", "gb18030"):
        payload = None
        try:
            payload = json.loads(body_bytes.decode(encoding))
            break
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
    top_shape = "not_json"
    if isinstance(payload, dict):
        top_shape = "object"
    elif isinstance(payload, list):
        top_shape = "list"
    return {
        "status": response.status,
        "content_type": content_type,
        "body_sha256": body_sha,
        "top_shape": top_shape,
    }

def run_probe(
    thread_url: str,
    profile_dir: str | Path,
    snapshot_dir: str | Path,
    *,
    max_pages: int = 2,
    navigation_delay_seconds: float = 3.0,
    json_probe: bool = True,
    confirm_login: Callable[[], None] | None = None,
    login_wait_file: str | Path | None = None,
    login_wait_timeout_seconds: float = 600.0,
) -> ProbeResult:
    """Run the fixed-target authenticated read probe end to end."""
    tid, _ = urls.parse_thread_url(thread_url)
    urls.check_online_tid(tid)
    if max_pages < 1 or max_pages > urls.MAX_PAGES:
        raise NgaAccessError("invalid_thread_url", f"max_pages 必须在 1..{urls.MAX_PAGES}")
    if navigation_delay_seconds < urls.MIN_NAVIGATION_DELAY_SECONDS:
        raise NgaAccessError(
            "invalid_thread_url",
            f"navigation-delay-seconds 不得低于 {urls.MIN_NAVIGATION_DELAY_SECONDS}",
        )
    profile = validate_profile_dir(profile_dir)
    snapshots = validate_snapshot_dir(snapshot_dir)
    ensure_distinct(profile, snapshots)
    snapshots.mkdir(parents=True, exist_ok=True)

    from umalib_ingest.nga_access.browser import launch_probe_session

    session = launch_probe_session(profile)
    try:
        page = session.page
        page.goto("https://bbs.nga.cn/", wait_until="domcontentloaded")
        if confirm_login is not None:
            confirm_login()
        elif login_wait_file is not None:
            _wait_for_login_confirmation(Path(login_wait_file), login_wait_timeout_seconds)
        else:
            input("请只在已打开的官方 NGA 页面完成手工登录；完成后按 Enter 继续…")
        first = read_page(session, tid, 1, "1", snapshots, navigation_delay_seconds)

        pages = [first]
        last_page = first.last_page
        if last_page is not None and last_page != 1 and max_pages >= 2:
            page.wait_for_timeout(int(navigation_delay_seconds * 1000))
            last_result = read_page(
                session, tid, last_page, str(last_page), snapshots, navigation_delay_seconds
            )
            pages.append(last_result)

        json_observation = None
        if json_probe and not any(
            "thread_forbidden" in p.diagnostics or "response_not_captured" in p.diagnostics
            for p in pages
        ):
            # 可选 JSON 观察前同样执行最低间隔等待。
            page.wait_for_timeout(int(navigation_delay_seconds * 1000))
            try:
                json_observation = observe_json(session, tid, 1)
            except NgaAccessError:
                json_observation = {"status": "observation_failed"}

        diagnostics: list[str] = []
        for p in pages:
            if "captcha_or_challenge" in p.diagnostics:
                diagnostics.append("captcha_or_challenge")
            if "thread_forbidden" in p.diagnostics:
                diagnostics.append("thread_forbidden")
            if "pagination_uncertain" in p.diagnostics:
                diagnostics.append("pagination_uncertain")
            if "floor_structure_unrecognized" in p.diagnostics:
                diagnostics.append("floor_structure_unrecognized")
        manifest = {
            "tid": tid,
            "captured_at_utc": _utc_now(),
            "pages": [
                {
                    "page_label": p.page_label,
                    "status": p.status,
                    "final_url": p.final_url,
                    "content_type": p.content_type,
                    "body_sha256": p.body_sha256,
                    "body_bytes": p.body_bytes,
                    "encoding": p.encoding,
                    "snapshot_status": p.snapshot_status,
                    "floor_count": p.floor_count,
                    "unique_pid_count": p.unique_pid_count,
                    "last_page": p.last_page,
                    "diagnostics": list(p.diagnostics),
                }
                for p in pages
            ],
            "json_observation": json_observation,
            "diagnostics": diagnostics,
        }
        snapshot.write_probe_manifest(snapshots, manifest)
        return ProbeResult(
            tid=tid,
            pages=tuple(pages),
            json_observation=json_observation,
            diagnostics=tuple(diagnostics),
        )
    finally:
        session.close()
