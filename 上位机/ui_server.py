# -*- coding: utf-8 -*-
"""界面资源托管：内置 HTTP 服务（127.0.0.1 随机端口）

用 HTTP 而不是 file:// 打开页面，避开 QtWebEngine 对本地文件的各类限制
（跨目录资源、fetch、缓存行为等）。
"""

from __future__ import annotations

import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def ui_dir() -> Path:
    """定位 oil-moisture-monitor 界面目录（源码运行 / 打包运行两种情形）"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "oil-moisture-monitor"
    return Path(__file__).resolve().parent.parent / "oil-moisture-monitor"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 不往控制台刷日志
        pass


class UiServer:
    def __init__(self, root: Path):
        self.root = root
        self.httpd = None
        self.port = 0
        self._thread = None

    def start(self) -> int:
        handler = partial(_QuietHandler, directory=str(self.root))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
