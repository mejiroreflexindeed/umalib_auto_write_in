"""TASK-014 W02：单一 GUI worker 上的 tkinter 源文件对话框；无 tkinter 回退。"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any


def pick_source_file() -> dict[str, Any]:
    """阻塞式原生文件选择；取消返回 {cancelled: True}。

    tkinter 只在主线程可用；此处用专用 GUI worker 线程/进程封装。无 tkinter
    或抛异常时返回 {error: "..."} 供前端回退到路径输入。
    """
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askopenfilename(
                title="选择 UmaLib 数据库副本（.db / .sqlite）",
                filetypes=[("SQLite 数据库", "*.db *.sqlite"), ("所有文件", "*.*")],
            )
            root.destroy()
            if not chosen:
                result["cancelled"] = True
            else:
                result["path"] = chosen
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=120)
    if not result and thread.is_alive():
        result["error"] = "选择超时，请使用路径输入"
    return result
