"""TASK-015 Q1：受限合成 transport（仅当 UMALIB_OPERATOR_TEST_TRANSPORT 明确设置时启用）。

场景文件由 UMALIB_OPERATOR_TEST_SCENARIO 指定，必须位于本轮证据根内：
{"tid": {"last_page": n, "floors": {"<page>": [[pid, content, posted_at], ...]}}}
产品默认不启用；所有页面经真实 fetch/解析/Watcher 链。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


class _Resp:
    def __init__(self, url, body):
        self.url = url
        self.status = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


def _floor_html(pid, content, posted_at):
    esc = (content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (f'<tr id="post1strow{pid}"><td><a id="pid{pid}Anchor"></a>'
            f'<span class="postdate" id="postdate{pid}">{posted_at}</span>'
            f'<span class="postcontent" id="postcontent{pid}">{esc}</span></td></tr>')


def _html(tid, last_page, floors):
    head = (f"<script>var __PAGE = {{0:'/read.php?tid={tid}',1:{last_page},2:1,3:20}};"
            "commonui.pageBtn(document.getElementById('pageBtnHere').parentNode,__PAGE,true)</script>")
    return ("<html><head><title>合成帖</title></head><body>" + head
            + "".join(_floor_html(pid, content, posted)
                      for pid, content, posted in floors)
            + "</body></html>").encode("utf-8")


class _Page:
    def __init__(self, scenario):
        self._scenario = scenario

    def goto(self, url, wait_until=None):
        tid = url.split("tid=")[1].split("&")[0]
        entry = self._scenario[tid]
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1])
        floors = entry["floors"].get(str(page), [])
        return _Resp(url, _html(tid, entry["last_page"], [tuple(f) for f in floors]))

    def title(self):
        return "合成帖"


class _Session:
    def __init__(self, scenario):
        self.page = _Page(scenario)

    def close(self):
        pass


def make_factory():
    scenario_path = os.environ["UMALIB_OPERATOR_TEST_SCENARIO"]
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))

    def factory():
        # 每次会话重读场景文件，允许旅程中途追加楼层
        current = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
        return _Session(current)

    return factory
