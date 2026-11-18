# -*- coding: utf-8 -*-
"""MDS-680A 油液微水传感器 Modbus RTU 模拟器（从机）

挂在 USB-RS485 转换器上（默认 COM13），模拟从机地址 1：
  - 9600 8N1
  - 功能码 0x04 读输入寄存器，起始 0x0000，数量 6
  - 返回 3 个大端 float（ABCD）：温度(°C) / 水活性(aw) / 含水量(ppm)
  - 数值随时间正弦波动，模拟真实油液缓慢变化

用法：
    D:\\anaconda3\\python.exe mds680a_sim.py [COM13]
"""
from __future__ import annotations

import math
import struct
import sys
import time

import serial

SLAVE_ADDR = 1


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc


def values(t: float) -> tuple[float, float, float]:
    temp = 25.0 + 2.0 * math.sin(t / 20.0)          # 23~27 °C
    aw = 0.35 + 0.05 * math.sin(t / 35.0)           # 0.30~0.40 aw
    ppm = 120.0 + 20.0 * math.sin(t / 50.0)         # 100~140 ppm
    return temp, aw, ppm


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else "COM13"
    s = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.05)
    print("[sim] MDS-680A 模拟器 @%s 9600 8N1, addr=%d" % (port, SLAVE_ADDR))

    buf = bytearray()
    t0 = time.time()
    n_req = 0
    while True:
        d = s.read(64)
        if d:
            buf += d
        # 按帧长 8 处理（读请求固定 8 字节）
        while len(buf) >= 8:
            frame = bytes(buf[:8])
            del buf[:8]
            addr, func = frame[0], frame[1]
            crc_ok = crc16(frame[:6]) == (frame[7] << 8 | frame[6])
            if addr != SLAVE_ADDR or func != 0x04 or not crc_ok:
                print("[sim] 丢弃帧 %s (addr=%02X func=%02X crc_ok=%s)"
                      % (frame.hex(), addr, func, crc_ok))
                continue
            reg = frame[2] << 8 | frame[3]
            cnt = frame[4] << 8 | frame[5]
            if reg != 0 or cnt != 6:
                # 异常响应 0x84 + 02（非法数据地址）
                exc = bytes([SLAVE_ADDR, 0x84, 0x02])
                c = crc16(exc)
                s.write(exc + bytes([c & 0xFF, c >> 8]))
                continue

            temp, aw, ppm = values(time.time() - t0)
            payload = struct.pack(">fff", temp, aw, ppm)
            resp = bytes([SLAVE_ADDR, 0x04, 12]) + payload
            c = crc16(resp)
            s.write(resp + bytes([c & 0xFF, c >> 8]))
            n_req += 1
            if n_req % 10 == 1:
                print("[sim] #%d t=%.1f aw=%.3f ppm=%.1f" % (n_req, temp, aw, ppm))


if __name__ == "__main__":
    raise SystemExit(main())
