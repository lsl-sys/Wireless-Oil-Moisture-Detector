# -*- coding: utf-8 -*-
"""命令行控制接口（CLI over HTTP，仅监听 127.0.0.1，供脚本/AI 免键鼠操作）

  GET /cli/status                    连接状态 + 设备列表 + 最新帧 + 演示状态 + CSV 路径
  GET /cli/devices                   已发现设备列表
  GET /cli/frame                     最新一条数据帧（JSON）
  GET /cli/connect?ip=&port=&name=   连接设备（默认端口 8080）
  GET /cli/disconnect                断开连接
  GET /cli/demo?on=1|0               进入 / 退出演示模式
  GET /cli/pause?on=1|0              暂停 / 恢复页面数据刷新
  GET /cli/js?expr=...               页面内执行 JS 并回传结果（UI 自检通道）
  GET /cli/reload                    绕过缓存重载页面
  GET /cli/export                    落盘当前 CSV 快照副本（不弹窗），返回路径+行数
  GET /cli/import_eis?dir=           指定目录直接导入阻抗谱（绕过弹窗）
  GET /cli/prov_ports                列出串口（auto=true 为板载 CH343 推荐口）
  GET /cli/prov_hello?port=COM6      打开串口并握手（返回 dev/mac/fw/当前连接）
  GET /cli/prov_scan                 经串口令设备扫描 WiFi，返回网络列表
  GET /cli/prov_set?ssid=&pass=      下发凭据，设备连接成功后写 NVS 自动重启
  GET /cli/prov_close                关闭配网串口

示例（PowerShell）：
  curl.exe http://127.0.0.1:9099/cli/status
  curl.exe "http://127.0.0.1:9099/cli/connect?ip=10.0.0.156"
"""

from __future__ import annotations

import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from backend import DATA_PORT
from eis_loader import load_eis_dir

CLI_PORT = 9099


class CliServer:
    def __init__(self, win):
        self.win = win
        self.httpd = None
        self._thread = None

    def start(self) -> bool:
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                win = outer.win
                try:
                    if u.path == "/cli/status":
                        self._reply({
                            "conn": win._conn_state,
                            "target": win.link.target,
                            "demo": win.demo.running,
                            "link_thread_alive": bool(win.link._thread and win.link._thread.is_alive()),
                            "devices": win.discovery.snapshot(),
                            "last_frame": json.loads(win._last_frame) if win._last_frame else None,
                            "csv": str(win.csv.path) if win.csv.path else None,
                            "config": win._config,
                        })
                    elif u.path == "/cli/devices":
                        self._reply({"devices": win.discovery.snapshot()})
                    elif u.path == "/cli/frame":
                        if win._last_frame:
                            self._reply(json.loads(win._last_frame))
                        else:
                            self._reply({"error": "no frame yet"}, 404)
                    elif u.path == "/cli/connect":
                        ip = (q.get("ip") or "").strip()
                        if not ip:
                            self._reply({"error": "missing ip"}, 400)
                            return
                        try:
                            port = int(q.get("port") or DATA_PORT)
                        except ValueError:
                            port = DATA_PORT
                        win.cliCommand.emit({"cmd": "connect", "ip": ip, "port": port,
                                             "name": q.get("name") or ""})
                        self._reply({"ok": True, "connecting": {"ip": ip, "port": port}})
                    elif u.path == "/cli/disconnect":
                        win.cliCommand.emit({"cmd": "disconnect"})
                        self._reply({"ok": True})
                    elif u.path == "/cli/demo":
                        on = q.get("on", "1") not in ("0", "false", "off")
                        win.cliCommand.emit({"cmd": "demo", "on": on})
                        self._reply({"ok": True, "demo": on})
                    elif u.path == "/cli/pause":
                        on = q.get("on", "1") not in ("0", "false", "off")
                        win.cliCommand.emit({"cmd": "pause", "on": on})
                        self._reply({"ok": True, "paused": on})
                    elif u.path == "/cli/js":
                        # 在页面里执行一段 JS 并回传结果（命令行 UI 自检用）
                        expr = q.get("expr") or ""
                        if not expr:
                            self._reply({"error": "missing expr"}, 400)
                            return
                        ev, box = threading.Event(), {}
                        win.cliCommand.emit({"cmd": "js", "expr": expr, "_ev": ev, "_box": box})
                        if ev.wait(6.0):
                            self._reply({"ok": True, "result": box.get("val")})
                        else:
                            self._reply({"error": "js eval timeout"}, 504)
                    elif u.path == "/cli/reload":
                        # 绕过缓存强制刷新页面（JS 改动后自检用）
                        win.cliCommand.emit({"cmd": "reload"})
                        self._reply({"ok": True})
                    elif u.path == "/cli/export":
                        # 落盘当前实时 CSV 的快照副本（不弹窗，活文件继续追加）
                        try:
                            snap = win.csv.export_snapshot()
                        except OSError:
                            self._reply({"error": "no csv data yet"}, 404)
                        except Exception as exc:
                            self._reply({"error": str(exc)}, 500)
                        else:
                            self._reply({"ok": True, "path": snap["path"], "rows": snap["rows"]})
                    elif u.path == "/cli/import_eis":
                        # 免弹窗导入阻抗谱文件夹（命令行测试用；界面按钮走 import_eis 命令弹目录框）
                        d = (q.get("dir") or "").strip()
                        if not d:
                            self._reply({"error": "missing dir"}, 400)
                            return
                        try:
                            payload = load_eis_dir(d)
                        except Exception as exc:
                            self._reply({"error": str(exc)}, 500)
                            return
                        win.cliCommand.emit({"cmd": "push_eis", "payload": payload})
                        self._reply({"ok": True, "count": len(payload["sets"]),
                                     "skipped": payload["skipped"],
                                     "names": [s["name"] for s in payload["sets"]]})
                    # ---- USB 串口配网（有线配网）：同步等待设备应答 ----
                    elif u.path == "/cli/prov_ports":
                        self._reply(win.prov.call("ports", timeout=8))
                    elif u.path == "/cli/prov_hello":
                        port = (q.get("port") or "").strip()
                        self._reply(win.prov.call("open", {"port": port}, timeout=20))
                    elif u.path == "/cli/prov_scan":
                        self._reply(win.prov.call("scan", timeout=35))
                    elif u.path == "/cli/prov_set":
                        ssid = q.get("ssid") or ""
                        pw = q.get("pass") or ""
                        self._reply(win.prov.call("set", {"ssid": ssid, "pass": pw}, timeout=65))
                    elif u.path == "/cli/prov_close":
                        self._reply(win.prov.call("close", timeout=5))
                    else:
                        self._reply({"error": "unknown cli path"}, 404)
                except Exception as exc:
                    self._reply({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", CLI_PORT), Handler)
        except OSError:
            return False  # 端口被占用（例如已有实例在跑），CLI 不可用但 GUI 照常
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
