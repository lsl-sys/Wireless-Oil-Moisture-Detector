# -*- coding: utf-8 -*-
"""模拟油液水分采集端（用电脑冒充 ESP32-C5 + JWA2），验证上位机链路/曲线

- UDP 8888 每秒广播设备发现包（发 127.0.0.1，本机自收）
- TCP 18080 数据服务：上位机连接后每秒推一帧正弦模拟数据
- 帧字段与真实固件一致：t/aw/ppm/bat/cur/pwr/lvl/rssi/link/seq/addr/fw
- 模拟「传感器失联」：启动 60s 后 t/aw/ppm 停更（冻结），90s 后恢复，
  用于验证上位机对传感器级失联的显示（后续固件 s_ok 字段联调）

用法：
  D:\\anaconda3\\python.exe fake_device.py            # 默认 127.0.0.1:18080
  D:\\anaconda3\\python.exe fake_device.py --port 18081
然后上位机「手动连接」填 127.0.0.1 端口 18080，或命令行：
  curl.exe "http://127.0.0.1:9099/cli/connect?ip=127.0.0.1&port=18080"
"""

from __future__ import annotations

import argparse
import json
import math
import random
import socket
import threading
import time

BCAST_PORT = 8888


def make_frame(seq: int, phase: float, sensor_alive: bool, frozen: dict | None) -> dict:
    if sensor_alive or frozen is None:
        t = 36.5 + 3.2 * math.sin(phase / 7.0) + random.uniform(-0.15, 0.15)
        aw = min(0.99, max(0.05, 0.42 + 0.18 * math.sin(phase / 11.0) + random.uniform(-0.01, 0.01)))
        ppm = max(5.0, 120.0 + 70.0 * math.sin(phase / 9.0) + random.uniform(-4.0, 4.0))
        cur = round(max(0.03, 0.18 + random.uniform(-0.02, 0.05)), 3)
        live = {"t": round(t, 2), "aw": round(aw, 3), "ppm": round(ppm, 1), "cur": cur}
    else:
        live = frozen  # 传感器失联：读数冻结在最后一次
    bat = max(18.0, 25.2 - phase * 0.0008)
    # 模拟原始 485 收发帧（与固件 rtx/rrx 字段一致）
    rtx = "0104000000067008"
    if sensor_alive:
        rrx = "01040C" + "".join("%02X" % random.randint(0, 255) for _ in range(12)) + "0000"
    else:
        rrx = ""  # 失联：超时无响应
    return {
        "t": live["t"], "aw": live["aw"], "ppm": live["ppm"],
        "bat": round(bat, 2),
        "cur": live["cur"],
        "pwr": round(bat * live["cur"], 2),
        "lvl": int((bat - 18.0) / 7.2 * 100),
        "rssi": -28 - int(5 * abs(math.sin(phase / 13.0))),
        "link": "SIM",
        "seq": seq % 256,
        "addr": 1,
        "fw": "SIM-1.0",
        "s_ok": 1 if sensor_alive else 0,
        "rtx": rtx,
        "rrx": rrx,
    }


def broadcast_loop(stop: threading.Event, data_port: int):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pkt = {
        "discover": "OILMOIST",
        "dev": "油液水分采集端(模拟)",
        "id": "SIM-JWA2",
        "name": "油液水分采集端(模拟)",
        "mac": "FA:KE:00:00:00:01",
        "model": "SIM-JWA2",
        "ip": "127.0.0.1",
        "port": data_port,
        "rssi": -30,
        "link": "SIM",
        "bat": 25.1,
        "lvl": 95,
    }
    data = (json.dumps(pkt, ensure_ascii=False) + "\n").encode("utf-8")
    while not stop.is_set():
        try:
            sock.sendto(data, ("127.0.0.1", BCAST_PORT))
        except OSError:
            pass
        stop.wait(1.0)
    sock.close()


def client_loop(conn: socket.socket, addr, stop: threading.Event):
    print("[sim] 上位机已连接 %s:%d" % addr)
    seq = 0
    t0 = time.time()
    frozen = None
    try:
        while not stop.is_set():
            phase = time.time() - t0
            # 60s~90s 之间模拟传感器失联（冻结读数）
            sensor_alive = not (60.0 <= phase % 120.0 < 90.0)
            frame = make_frame(seq, phase, sensor_alive, frozen)
            if sensor_alive:
                frozen = {"t": frame["t"], "aw": frame["aw"], "ppm": frame["ppm"], "cur": frame["cur"]}
            conn.sendall((json.dumps(frame) + "\n").encode("utf-8"))
            seq += 1
            time.sleep(1.0)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
        print("[sim] 上位机已断开 %s:%d" % addr)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟油液水分采集端")
    ap.add_argument("--port", type=int, default=18080, help="TCP 数据端口（默认 18080）")
    args = ap.parse_args()

    stop = threading.Event()
    threading.Thread(target=broadcast_loop, args=(stop, args.port), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(4)
    print("[sim] 模拟采集端已启动：UDP 广播->8888，TCP 数据=%d" % args.port)
    print("[sim] 上位机手动连接 127.0.0.1:%d，或 curl.exe \"http://127.0.0.1:9099/cli/connect?ip=127.0.0.1&port=%d\""
          % (args.port, args.port))
    print("[sim] 60s 后模拟传感器失联 30s（读数冻结），Ctrl+C 退出")

    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=client_loop, args=(conn, addr, stop), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
