# -*- coding: utf-8 -*-
"""mds680a_test.py — MDS-680A 油液微水传感器 Modbus RTU 通信测试

用法：
  D:\\anaconda3\\python.exe mds680a_test.py              # 自动找 COM
  D:\\anaconda3\\python.exe mds680a_test.py COM13        # 指定端口
  D:\\anaconda3\\python.exe mds680a_test.py COM13 -n 5  # 读 5 次退出

协议：9600 8N1，Modbus RTU，地址 1，功能码 0x04 读 0x0000 起 6 个寄存器
      响应 17 字节：温度(4B) + 水活性(4B) + 含水量(4B)，IEEE754 大端 float
"""

import argparse
import socket
import struct
import sys
import time

try:
    import serial
    from serial import SerialException
except ImportError:
    print("需要先装 pyserial: D:\\anaconda3\\python.exe -m pip install pyserial")
    sys.exit(1)


# ---------- Modbus CRC16 ----------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_read_holding(addr: int, reg: int, count: int) -> bytes:
    frame = bytes([addr, 0x04, (reg >> 8) & 0xFF, reg & 0xFF,
                   (count >> 8) & 0xFF, count & 0xFF])
    crc = crc16_modbus(frame)
    return frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# ---------- 工具 ----------
def find_com():
    """列出所有 COM 口并让用户选；只有一个就直接用"""
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("没找到任何 COM 口")
        return None
    print("可用 COM 口：")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  {p.description}")
    if len(ports) == 1:
        print(f"只有一个，直接用 {ports[0].device}")
        return ports[0].device
    try:
        idx = int(input("选一个编号："))
        return ports[idx].device
    except (ValueError, IndexError):
        return None


def parse_response(resp: bytes):
    """解析 17 字节响应 → (temp, aw, ppm) 或 None"""
    if len(resp) != 17:
        print(f"  [WARN] 长度不对：收到 {len(resp)} 字节（应 17）")
        return None
    if resp[0] != 0x01 or resp[1] != 0x04 or resp[2] != 12:
        print(f"  [WARN] 响应头不对：{resp[:3].hex().upper()}")
        return None
    crc_calc = crc16_modbus(resp[:-2])
    crc_recv = (resp[-1] << 8) | resp[-2]
    if crc_calc != crc_recv:
        print(f"  [WARN] CRC 校验失败：计算 0x{crc_calc:04X} 接收 0x{crc_recv:04X}")
        return None
    temp = struct.unpack(">f", resp[3:7])[0]
    aw   = struct.unpack(">f", resp[7:11])[0]
    ppm  = struct.unpack(">f", resp[11:15])[0]
    return temp, aw, ppm


# ---------- 主循环 ----------
def read_once(ser, addr=1):
    req = build_read_holding(addr, 0x0000, 6)  # 读从寄存器 0 起 6 个
    ser.reset_input_buffer()
    ser.write(req)
    ser.flush()
    # 等响应：最多 300ms
    t0 = time.time()
    buf = bytearray()
    while time.time() - t0 < 0.300:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
            # 收齐 17 字节就算了
            if len(buf) >= 17:
                # 等最后一个字节稳定再跳出
                time.sleep(0.020)
                n2 = ser.in_waiting
                if n2:
                    buf.extend(ser.read(n2))
                break
        else:
            time.sleep(0.005)

    print(f"  TX: {req.hex().upper()}")
    if not buf:
        print("  RX: (无响应，超时)")
        return None
    print(f"  RX: {bytes(buf).hex().upper()}  ({len(buf)} 字节)")
    return parse_response(bytes(buf))


def main():
    import serial.tools.list_ports

    ap = argparse.ArgumentParser(description="MDS-680A Modbus RTU 通信测试")
    ap.add_argument("port", nargs="?", help="COM 口号（如 COM13）；不填则交互选择")
    ap.add_argument("-n", "--count", type=int, default=0,
                    help="连续读取 N 次后退出（0=Ctrl+C 手动停）")
    ap.add_argument("-a", "--addr", type=int, default=1,
                    help="传感器 Modbus 地址（默认 1）")
    ap.add_argument("-i", "--interval", type=float, default=1.5,
                    help="读取间隔秒（默认 1.5s）")
    args = ap.parse_args()

    port = args.port or find_com()
    if not port:
        print("没选端口，退出")
        sys.exit(1)

    print(f"\n打开 {port} @ 9600 8N1，从机地址 {args.addr}")
    ser = serial.Serial(port=port, baudrate=9600, bytesize=8,
                        parity='N', stopbits=1, timeout=0.3)
    time.sleep(0.5)  # 传感器上电 3 秒才稳，但 USB-RS485 不会断电，0.5s 缓冲够了

    print("开始读取，Ctrl+C 停止...\n")
    ok = fail = 0
    try:
        while True:
            t = time.strftime("%H:%M:%S")
            res = read_once(ser, args.addr)
            if res is None:
                fail += 1
                print(f"  {t} 读取失败\n")
            else:
                temp, aw, ppm = res
                ok += 1
                print(f"  {t} T={temp:7.2f} ℃  aw={aw:.3f}  ppm={ppm:8.2f}  [OK]")
            print()
            if args.count and ok + fail >= args.count:
                break
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        print(f"\n汇总：成功 {ok} 次 / 失败 {fail} 次")


if __name__ == "__main__":
    main()
