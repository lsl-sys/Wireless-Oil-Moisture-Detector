# -*- coding: utf-8 -*-
"""上位机网络后端

Discovery : 监听 UDP 8888，收集采集端广播的设备信息（供「设备匹配」页）
Link      : 连接设备 TCP 8080，按行读取 JSON 数据帧

两者都在工作线程中阻塞运行，通过 Qt 信号把结果回传主线程。
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback

from PyQt5.QtCore import QObject, pyqtSignal

DISCOVERY_PORT = 8888      # 与固件约定的 UDP 广播端口
DATA_PORT = 8080           # 与固件约定的 TCP 数据端口
DEVICE_TTL = 6.0           # 设备广播超过该秒数未更新即从列表移除
DATA_TIMEOUT = 8.0         # 已连接但超过该秒数无任何字节 → 判定链路假死，自动重连


def _safe_emit(signal, payload):
    """工作线程投递信号。

    退出阶段 QObject 可能已被销毁，此时 emit 会抛 RuntimeError，直接忽略即可。
    """
    try:
        signal.emit(payload)
    except RuntimeError:
        pass


class Discovery(QObject):
    """UDP 设备发现

    采集端每秒向 255.255.255.255:8888 广播一条 JSON：
        {"id":..., "name":..., "port":8080, "model":..., "rssi":..., "link":...}
    这里被动监听即可，不需要主动发包。
    """

    devicesChanged = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop = threading.Event()
        self._thread = None
        self._devices = {}
        self._lock = threading.Lock()
        self._sig = None          # 上次推送的设备列表指纹，避免无变化时反复刷新页面

    # ---------- 对外接口 ----------
    def start(self):
        """启动监听线程（重复调用无副作用）"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="udp-discover", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        """当前已知设备列表（按信号强度倒序）"""
        with self._lock:
            devs = list(self._devices.values())
        return sorted(devs, key=lambda d: d.get("rssi", -100), reverse=True)

    # ---------- 内部实现 ----------
    def _loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError:
            # 端口被占用（例如另一个实例在跑），直接退出，不影响主功能
            sock.close()
            return
        sock.settimeout(0.4)

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                data = None
            except OSError:
                break
            if data:
                self._ingest(data, addr)
            if self._stop.is_set():
                break
            self._prune_and_emit()

        sock.close()

    def _ingest(self, data, addr):
        try:
            info = json.loads(data.decode("utf-8", "ignore").strip())
        except Exception:
            return
        if not isinstance(info, dict):
            return

        ip = str(info.get("ip") or addr[0])
        rssi = info.get("rssi")
        try:
            rssi = int(rssi)
        except (TypeError, ValueError):
            rssi = -60

        dev = {
            "id": str(info.get("id") or ip),
            "name": str(info.get("name") or "油液水分采集端"),
            "ip": ip,
            "port": int(info.get("port") or DATA_PORT),
            "model": str(info.get("model") or "ESPC5-32E-H4"),
            "rssi": rssi,
            "bat": info.get("bat"),
            "lvl": info.get("lvl"),
            "link": str(info.get("link") or "WiFi"),
            "ts": time.time(),
        }
        with self._lock:
            self._devices[dev["id"]] = dev

    def _prune_and_emit(self):
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._devices.items() if now - v["ts"] > DEVICE_TTL]:
                self._devices.pop(key, None)
            devs = sorted(self._devices.values(), key=lambda d: d.get("rssi", -100), reverse=True)
            # 仅当设备列表内容真正变化时才推送，避免每 0.4s 无谓地重绘页面
            sig = tuple((d["id"], d["rssi"], d.get("bat"), d.get("lvl"), d["link"]) for d in devs)
            if sig == self._sig:
                return
            self._sig = sig
        _safe_emit(self.devicesChanged, [dict(d) for d in devs])


class Link(QObject):
    """TCP 数据链路

    连接设备后按行读取数据帧（每行一条 JSON），逐条投递 frameReceived。
    """

    frameReceived = pyqtSignal(str)      # 原始行文本
    stateChanged = pyqtSignal(dict)      # {state, name, ip, port, msg}

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sock = None
        self._stop = threading.Event()
        self._thread = None
        self._target = None
        self._gen = 0               # 连接代际：旧线程发现代际过期即退出，杜绝双线程竞争
        self._gen_lock = threading.Lock()

    @property
    def target(self):
        return self._target

    # ---------- 对外接口 ----------
    def connect_to(self, ip, port=DATA_PORT, name=""):
        """连接指定设备（会先断开旧连接；自动重连循环由线程内部维护）"""
        with self._gen_lock:
            self._gen += 1
            gen = self._gen
        self.disconnect()
        self._target = {"ip": str(ip), "port": int(port), "name": str(name or "")}
        self._stop.clear()
        _safe_emit(self.stateChanged, {
            "state": "connecting", "name": self._target["name"],
            "ip": self._target["ip"], "port": self._target["port"],
            "msg": "正在连接设备…",
        })
        self._thread = threading.Thread(target=self._loop, args=(gen,), daemon=True,
                                        name="tcp-link-%d" % gen)
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None

    # ---------- 内部实现 ----------
    def _stale(self, gen):
        """本线程对应连接代际已被更新的连接取代"""
        with self._gen_lock:
            return gen != self._gen

    def _loop(self, gen):
        try:
            self._loop_guarded(gen)
        except Exception:
            # 线程绝不能无声死亡：留下痕迹，方便事后定位
            print("[link] worker thread crashed:\n%s" % traceback.format_exc(), flush=True)

    def _loop_guarded(self, gen):
        tgt = self._target or {}
        ip, port, name = tgt.get("ip"), tgt.get("port"), tgt.get("name", "")

        retry = 0
        while not self._stop.is_set() and not self._stale(gen):
            try:
                sock = socket.create_connection((ip, port), timeout=6.0)
            except OSError as exc:
                retry += 1
                _safe_emit(self.stateChanged, {
                    "state": "connecting", "name": name, "ip": ip, "port": port,
                    "msg": "连接失败，2s 后第 %d 次重试（%s）" % (retry, exc),
                })
                self._stop.wait(2.0)
                continue

            self._sock = sock
            sock.settimeout(1.0)
            retry = 0
            _safe_emit(self.stateChanged, {
                "state": "connected", "name": name, "ip": ip, "port": port,
                "msg": "已连接",
            })

            buf = b""
            last_data = time.time()
            stall_reason = None
            while not self._stop.is_set() and not self._stale(gen):
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    chunk = None
                except OSError:
                    break
                if chunk is None:
                    # 应用层数据看门狗：TCP 连接还在但长期无字节
                    # （设备固件卡死 / 半开连接 / AP 抽风）→ 主动断开重连
                    if time.time() - last_data > DATA_TIMEOUT:
                        stall_reason = "数据中断"
                        break
                    continue
                if not chunk:
                    break  # 对端正常关闭
                last_data = time.time()
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if line:
                        self.frameReceived.emit(line.decode("utf-8", "ignore"))

            self._sock = None
            try:
                sock.close()
            except OSError:
                pass

            if self._stop.is_set() or self._stale(gen):
                break
            # 非正常断开（设备断电/重启/OTA/假死）→ 自动重连
            msg = ("连接中断，2s 后自动重连…" if stall_reason is None
                   else "%s（%ds 无数据），2s 后自动重连…" % (stall_reason, int(DATA_TIMEOUT)))
            _safe_emit(self.stateChanged, {
                "state": "connecting", "name": name, "ip": ip, "port": port,
                "msg": msg,
            })
            self._stop.wait(2.0)
