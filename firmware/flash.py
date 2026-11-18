# -*- coding: utf-8 -*-
"""一键烧录 ESP32-C5 采集板固件

用法（在任意目录执行均可）：
    D:\\anaconda3\\python.exe flash.py            # 自动找串口并烧录
    D:\\anaconda3\\python.exe flash.py COM7       # 指定串口
    D:\\anaconda3\\python.exe flash.py --erase    # 整片擦除（会清掉 NVS 配网信息）

说明：
  - 本板自带 DTR/RTS 自动下载电路（Q1/Q2 + BOOT/EN），无需手动按 BOOT 键。
  - 使用 PlatformIO Core 6.2.0（Anaconda 里的是 6.1.19，装不了本项目平台）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# PlatformIO 核心目录迁到 C:\pio（原 C:\Users\ASUS\.platformio 保留 junction）
# 目的：缩短 arduino-esp32 依赖包的安装路径，规避 Windows 260 字符长路径限制
PIO_CORE = r"C:\pio"
PIO = Path(PIO_CORE) / "penv" / "Scripts" / "pio.exe"


def pio_env() -> dict:
    env = os.environ.copy()
    env["PLATFORMIO_CORE_DIR"] = PIO_CORE
    return env


# 可能出现在采集板上的 USB 转串口 / 原生 USB 厂商号
USB_VIDS = ("VID:PID=303A", "VID:PID=1A86", "VID:PID=10C4", "VID:PID=0403")


def list_ports() -> list[dict]:
    try:
        out = subprocess.check_output(
            [str(PIO), "device", "list", "--json-output"],
            text=True, encoding="utf-8", stderr=subprocess.DEVNULL, env=pio_env(),
        )
        return json.loads(out)
    except Exception as e:
        print("[错误] 无法枚举串口：%s" % e)
        return []


def find_ports() -> list[dict]:
    ports = []
    for p in list_ports():
        hwid = (p.get("hwid") or "").upper()
        if "BTHENUM" in hwid:      # 蓝牙虚拟串口，不是开发板
            continue
        if any(v in hwid for v in USB_VIDS):
            ports.append(p)
    return ports


def main() -> int:
    args = sys.argv[1:]
    erase = "--erase" in args
    arg_port = next((a for a in args if a.upper().startswith("COM") and not a.startswith("--")), None)

    if not PIO.exists():
        print("[错误] 未找到 PlatformIO 6.2.0：%s" % PIO)
        return 1

    if arg_port:
        port = arg_port
        print("[1/2] 使用指定串口：%s" % port)
    else:
        print("[1/2] 自动检测开发板串口 ...")
        ports = find_ports()
        if not ports:
            print("")
            print("未检测到 ESP32 开发板的 USB 串口。当前串口如下：")
            for p in list_ports():
                print("  - %-6s %s" % (p.get("port"), p.get("description", "")))
            print("")
            print("请插好板子（USB-C），确认设备管理器出现新的 COM 口后重试。")
            return 1
        port = ports[0]["port"]
        print("      找到 %s  %s" % (port, ports[0].get("description", "")))
        if len(ports) > 1:
            print("      检测到多个候选口，默认用第一个；其他：%s"
                  % ", ".join(p["port"] for p in ports[1:]))

    if erase:
        print("[2/2] 整片擦除 %s ..." % port)
        cmd = [str(PIO), "run", "-d", str(HERE), "-e", "oil_device", "-t", "erase", "--upload-port", port]
    else:
        print("[2/2] 编译并烧录到 %s ..." % port)
        cmd = [str(PIO), "run", "-d", str(HERE), "-e", "oil_device", "-t", "upload", "--upload-port", port]

    rc = subprocess.run(cmd, env=pio_env()).returncode
    if rc != 0:
        print("")
        print("烧录失败（退出码 %d）。排查建议：" % rc)
        print("  1) 端口被占用：关掉串口助手 / PlatformIO 监视器 / 其他 IDE")
        print("  2) 仍未识别：按住 BOOT 再点一下 EN 进入下载模式，然后重试")
        print("  3) 速率过高：把 platformio.ini 里 upload_speed 改成 460800")
        return rc

    print("")
    print("烧录成功。首次上电若 NVS 为空，会自动进入配网：")
    print("  热点 OIL-CFG-XXXX / 密码 oil12345 → 浏览器打开 http://192.168.4.1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
