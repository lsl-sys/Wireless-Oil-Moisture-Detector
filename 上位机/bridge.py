# -*- coding: utf-8 -*-
"""页面桥接层

PageBridge : 宿主 → 页面 的单向 JS 注入（window.bridge.* 调用入口）
CommandRouter : 页面 → 宿主 的命令队列轮询与路由（window.bridge.drainCommands）

关键约束：所有 JS 注入每次调用时都动态取 host._js，不能缓存绑定方法——
selftest 的 ui 步会把 win._js 换成 spy 来断言注入内容，缓存会让 spy 失效。
"""

from __future__ import annotations

import json
import threading

from PyQt5.QtWebEngineWidgets import QWebEnginePage

from backend import DATA_PORT


class PageBridge:
    """宿主 → 页面（所有推送都必须经过这里，便于统一转义与自检）"""

    def __init__(self, host):
        self.host = host

    def emit(self, code: str):
        self.host._js(code)

    def set_devices(self, devs):
        self.emit("window.bridge && window.bridge.setDevices(%s)" % json.dumps(devs))

    def set_scanning(self, on: bool):
        self.emit("window.bridge && window.bridge.setScanning(%s)"
                  % ("true" if on else "false"))

    def set_conn_state(self, state: dict):
        self.emit("window.bridge && window.bridge.setConnState(%s)" % json.dumps(state))

    def push_frame(self, payload):
        """payload 可以是原始 JSON 文本（设备行）或 dict（演示帧）"""
        self.emit("window.bridge && window.bridge.pushFrame(%s)" % json.dumps(payload))

    def push_log(self, text: str):
        """text 需是已转义的单行文本（调用方负责 json.dumps(x)[1:-1]）"""
        self.emit("window.bridge && window.bridge.pushLog('%s')" % text)

    def set_transport(self, name: str):
        self.emit("window.bridge && window.bridge.setTransport('%s')" % name)

    def set_config(self, cfg: dict):
        self.emit("window.bridge && window.bridge.setConfig(%s)" % json.dumps(cfg))

    def set_eis(self, payload: dict):
        self.emit("window.bridge && window.bridge.setEIS(%s)" % json.dumps(payload))

    def set_paused(self, on: bool):
        self.emit("window.bridge && window.bridge.setPaused(%s)" % ("true" if on else "false"))

    def set_prov_state(self, d: dict):
        """有线配网状态推送（stage: ports/hello/scan/set/closed/error）"""
        self.emit("window.bridge && window.bridge.setProvState(%s)" % json.dumps(d))

    def reload_page(self):
        self.host.view.page().triggerAction(QWebEnginePage.ReloadAndBypassCache)


class CommandRouter:
    """页面命令队列（{cmd:...}）的轮询、解析与分发"""

    def __init__(self, host):
        self.host = host
        self.bridge = host.bridge
        self._handlers = {
            "scan": self._scan,
            "connect": self._connect,
            "disconnect": self._disconnect,
            "demo": self._demo,
            "save_csv": self._save_csv,
            "set_autofullscreen": self._set_autofullscreen,
            "set_csv_split": self._set_csv_split,
            "set_max_records": self._set_max_records,
            "toggle_fullscreen": self._toggle_fullscreen,
            "ota_upgrade": self._ota_upgrade,
            "ping": lambda cmd: None,
            "pause": self._pause,
            "js": self._js_eval,
            "reload": lambda cmd: self.bridge.reload_page(),
            "import_eis": self._import_eis,
            "push_eis": self._push_eis,
            "prov_ports": lambda cmd: self.host.prov.request_ports(),
            "prov_open": lambda cmd: self.host.prov.request_open(str(cmd.get("port") or "")),
            "prov_scan": lambda cmd: self.host.prov.request_scan(),
            "prov_set": lambda cmd: self.host.prov.request_set(
                str(cmd.get("ssid") or ""), str(cmd.get("pass") or "")),
            "prov_close": lambda cmd: self.host.prov.request_close(),
        }

    # ---------- 队列 ---------- #
    def poll(self):
        self.host.view.page().runJavaScript(
            "(function(){try{return JSON.stringify((window.bridge&&window.bridge.drainCommands)"
            "?window.bridge.drainCommands():[])}catch(e){return '[]'}})()",
            self._on_commands,
        )

    def _on_commands(self, result):
        if not result or result == "[]":
            return
        try:
            cmds = json.loads(result)
        except Exception:
            return
        if not isinstance(cmds, list):
            return
        for cmd in cmds:
            if isinstance(cmd, dict):
                self.dispatch(cmd)

    # ---------- 分发 ---------- #
    def dispatch(self, cmd: dict):
        handler = self._handlers.get(cmd.get("cmd"))
        if handler:
            handler(cmd)

    # ---------- 各命令 ---------- #
    def _scan(self, cmd):
        win = self.host
        win.discovery.start()
        self.bridge.set_scanning(True)
        win._scan_timer.start(5000)
        self.bridge.push_log("开始扫描设备…")

    def _connect(self, cmd):
        win = self.host
        ip = str(cmd.get("ip") or "").strip()
        if not ip:
            return
        try:
            port = int(cmd.get("port") or DATA_PORT)
        except (TypeError, ValueError):
            port = DATA_PORT
        win._stop_demo()  # 连真设备时自动退出演示
        win.link.connect_to(ip, port, str(cmd.get("name") or ""))
        self.bridge.push_log("正在连接 %s:%d …" % (ip, port))

    def _disconnect(self, cmd):
        win = self.host
        win.link.disconnect()
        win._on_conn_state({"state": "idle", "name": "", "ip": "", "port": 0, "msg": "已断开"})
        self.bridge.push_log("已断开设备连接")

    def _demo(self, cmd):
        if cmd.get("on"):
            self.host._start_demo()
        else:
            self.host._stop_demo()

    def _save_csv(self, cmd):
        self.host._save_csv(cmd.get("csv") or "")

    def _set_autofullscreen(self, cmd):
        win = self.host
        on = bool(cmd.get("on"))
        win._config["auto_fullscreen"] = on
        win.save_config()
        self.bridge.push_log("%s启动时自动全屏" % ("已开启：" if on else "已关闭："))

    def _set_csv_split(self, cmd):
        win = self.host
        try:
            win._config["csv_split_minutes"] = max(0, int(cmd.get("minutes", 0)))
        except (TypeError, ValueError):
            win._config["csv_split_minutes"] = 0
        win.save_config()

    def _set_max_records(self, cmd):
        win = self.host
        try:
            win._config["max_records"] = max(100, int(cmd.get("count", 20000)))
        except (TypeError, ValueError):
            win._config["max_records"] = 20000
        win.save_config()

    def _toggle_fullscreen(self, cmd):
        self.host._toggle_fullscreen()

    def _ota_upgrade(self, cmd):
        win = self.host
        ip = str(cmd.get("ip") or (getattr(win.link, "target", {}) or {}).get("ip") or "")
        if not ip:
            self.bridge.push_log("请先连接设备后再升级固件")
            return
        path = win.ask_firmware_file()
        if not path:
            return
        threading.Thread(target=win._ota_upload, args=(ip, path), daemon=True).start()

    def _pause(self, cmd):
        self.bridge.set_paused(bool(cmd.get("on")))

    def _js_eval(self, cmd):
        # /cli/js 自检：页面内执行表达式，结果经事件回传 HTTP 线程
        ev, box = cmd.get("_ev"), cmd.get("_box")

        def _cb(v, _ev=ev, _box=box):
            _box["val"] = v
            if _ev is not None:
                _ev.set()

        self.host.view.page().runJavaScript(str(cmd.get("expr") or "null"), _cb)

    def _import_eis(self, cmd):
        d = self.host.ask_eis_dir()
        if d:
            self.host._import_eis_dir(d)

    def _push_eis(self, cmd):
        # /cli/import_eis 已在 HTTP 线程完成解析，这里只负责把结果推给页面
        self.host._push_eis(cmd.get("payload") or {})
