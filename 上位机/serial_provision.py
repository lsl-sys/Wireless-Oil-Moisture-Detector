# -*- coding: utf-8 -*-
"""USB 串口配网（有线配网）

ESP32-C5 采集端的 UART0（板载 CH343，921600 8N1）常驻单行 JSON 配网协议：
  上位机 → 设备：
    {"cmd":"hello"}                          握手（返回 dev/mac/fw/当前连接状态）
    {"cmd":"scan"}                           扫描周围 WiFi（设备端异步，实测最长 ~15s）
    {"cmd":"set","ssid":"...","pass":"..."}  定向 BSSID+信道连接（含重试），
                                             成功写 NVS 并自动重启
  设备 → 上位机：以 {"src":"prov" 开头的单行 JSON（ev=hello/scan/set），
                其余行是调试日志，直接忽略。

线程模型：与 backend.Discovery/Link 一致——QObject + 后台 worker 线程持有串口，
结果经 provEvent(dict) 信号回 GUI 线程；CLI 线程用 call() 同步等待终态事件。

关键硬件细节：打开串口前必须 dtr=False / rts=False，否则 CH343 的自动复位
电路会在开 port 瞬间把板子按进复位态（实测现象：串口完全无输出）。
"""

from __future__ import annotations

import json
import queue
import threading
import time

from PyQt5.QtCore import QObject, pyqtSignal

BAUD = 921600
SCAN_TIMEOUT = 30.0    # 设备端扫描超时 25s + 余量
SET_TIMEOUT = 60.0     # set 最坏路径：定向×2 + 盲扫×1（各 8s）+ 边界

# 板载 CH343（C5 采集端 UART0 日志/配网口）——串口列表里的推荐口
TARGET_VID = 0x1A86


def list_ports():
    """枚举本机串口，推荐口（CH343）排前并标注 auto=True"""
    from serial.tools import list_ports as lp
    out = []
    for p in lp.comports():
        auto = bool(p.vid is not None and p.vid == TARGET_VID)
        out.append({"port": p.device, "desc": p.description or "",
                    "hwid": p.hwid or "", "auto": auto})
    out.sort(key=lambda d: (not d["auto"], d["port"]))
    return out


class SerialProvisioner(QObject):
    """串口配网会话：GUI 异步接口（request_*）+ CLI 同步接口（call）"""

    provEvent = pyqtSignal(dict)   # worker → GUI：{"stage": ports/hello/scan/set/closed/error, ...}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q = queue.Queue()
        self._ser = None
        self._waiter = None          # (Event, box) —— CLI 同步等待槽（同时仅一个）
        self._waiter_lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    # ---------------- GUI 侧异步接口（入队即返） ----------------
    def request_ports(self):
        self._q.put(("ports", {}))

    def request_open(self, port):
        self._q.put(("open", {"port": port}))

    def request_scan(self):
        self._q.put(("scan", {}))

    def request_set(self, ssid, password):
        self._q.put(("set", {"ssid": ssid, "pass": password}))

    def request_close(self):
        self._q.put(("close", {}))

    # ---------------- CLI 侧同步接口 ----------------
    def call(self, action, args=None, timeout=30.0):
        ev, box = threading.Event(), {}
        with self._waiter_lock:
            self._waiter = (ev, box)
        self._q.put((action, args or {}))
        if not ev.wait(timeout):
            with self._waiter_lock:
                self._waiter = None
            return {"stage": "error", "msg": "timeout"}
        return box.get("ev") or {}

    # ---------------- 内部：worker ----------------
    def _run(self):
        while True:
            job = self._q.get()
            if job is None:
                break
            action, args = job
            try:
                if action == "ports":
                    self._emit({"stage": "ports", "ports": list_ports()})
                elif action == "open":
                    self._do_open(args.get("port") or "")
                elif action == "scan":
                    self._do_simple("scan", SCAN_TIMEOUT)
                elif action == "set":
                    self._do_set(args.get("ssid") or "", args.get("pass") or "")
                elif action == "close":
                    self._close_ser()
                    self._emit({"stage": "closed"})
            except Exception as exc:
                self._close_ser()
                self._emit({"stage": "error", "msg": "%s: %s" % (type(exc).__name__, exc)})

    def _emit(self, d):
        self.provEvent.emit(d)
        with self._waiter_lock:
            w = self._waiter
            self._waiter = None
        if w:
            w[1]["ev"] = d
            w[0].set()

    def _close_ser(self):
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def _do_open(self, port):
        if not port:
            self._emit({"stage": "hello", "ok": 0, "err": "no_port"})
            return
        import serial
        self._close_ser()
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = BAUD
        ser.timeout = 0.2
        ser.dtr = False   # 关键：开 port 前置无效，避免 CH343 自动复位电路按住板子
        ser.rts = False
        ser.open()
        time.sleep(0.3)
        ser.reset_input_buffer()
        self._ser = ser
        # 打开后立即握手验证（板子可能正处上电 3s 按键检测窗口，重试 3 次）
        obj = None
        for _ in range(3):
            self._write({"cmd": "hello"})
            obj = self._wait_ev("hello", 5.0)
            if obj:
                break
        if not obj:
            self._close_ser()
            self._emit({"stage": "hello", "ok": 0, "err": "no_reply"})
            return
        obj["stage"] = "hello"
        self._emit(obj)

    def _do_simple(self, cmd, timeout):
        if self._ser is None:
            self._emit({"stage": "error", "msg": "串口未打开"})
            return
        self._write({"cmd": cmd})
        obj = self._wait_ev(cmd, timeout)
        if obj is None:
            self._emit({"stage": cmd, "ok": 0, "err": "timeout"})
            return
        obj["stage"] = cmd
        self._emit(obj)

    def _do_set(self, ssid, password):
        if self._ser is None:
            self._emit({"stage": "error", "msg": "串口未打开"})
            return
        if not ssid:
            self._emit({"stage": "set", "ok": 0, "err": "bad_ssid"})
            return
        self._write({"cmd": "set", "ssid": ssid, "pass": password})
        obj = self._wait_ev("set", SET_TIMEOUT)
        if obj is None:
            self._emit({"stage": "set", "ok": 0, "err": "timeout"})
            return
        obj["stage"] = "set"
        self._emit(obj)

    def _write(self, d):
        self._ser.write((json.dumps(d, ensure_ascii=False) + "\n").encode("utf-8"))

    def _wait_ev(self, ev, timeout):
        """读到一条 {"src":"prov","ev":<ev>} 应答；其余行（设备调试日志）丢弃"""
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            ser = self._ser
            if ser is None or not ser.is_open:
                return None
            chunk = ser.read(ser.in_waiting or 1)
            if chunk:
                buf += chunk
            while b"\n" in buf:
                line_b, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                line = line_b.decode("utf-8", "replace").strip()
                if not line.startswith('{"src":"prov"'):
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("ev") == ev:
                    return obj
        return None

    def close(self):
        self._close_ser()
