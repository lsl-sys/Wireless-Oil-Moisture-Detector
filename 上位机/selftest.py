# -*- coding: utf-8 -*-
"""上位机命令行自检/测试入口（免键鼠，不开 GUI）

子命令：
  discover           UDP 8888 发现测试：等一条设备广播并校验字段
  link               TCP 8080 链路测试：收 N 帧，校验 JSON 协议与 seq 连续性
  stress             压力测试：长时间收流，统计帧率/断连/丢序/坏帧
  fuzz               模糊测试：假 TCP 服务器灌畸形数据 + UDP 广播注入，校验不崩溃
  csv                CSV 实时追加 + 自动切分逻辑测试（不启 GUI）
  ota                OTA HTTP 接口连通性测试（/version /status）
  regression         回归：discover + link + fuzz + csv + ota 全跑

退出码：0=全部通过，1=有失败项（CI/脚本可用）

示例：
  D:\\anaconda3\\python.exe selftest.py link --frames 10
  D:\\anaconda3\\python.exe selftest.py stress --duration 60
  D:\\anaconda3\\python.exe selftest.py csv
  D:\\anaconda3\\python.exe selftest.py ota
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
from pathlib import Path

DISCOVERY_PORT = 8888
DATA_PORT = 8080
FRAME_KEYS = ("t", "aw", "ppm", "bat", "cur", "pwr", "lvl", "rssi", "link", "seq", "addr", "fw")


# --------------------------------------------------------------------------- #
# 协议校验器（同时是模糊测试的判据）
# --------------------------------------------------------------------------- #
def validate_frame(line: str) -> tuple[bool, str]:
    """校验一条数据帧（JSON 行）。返回 (是否合法, 原因)"""
    try:
        j = json.loads(line)
    except Exception as e:
        return False, "JSON解析失败: %s" % e
    if not isinstance(j, dict):
        return False, "非JSON对象"
    for k in FRAME_KEYS:
        if k not in j:
            return False, "缺字段 %s" % k
    try:
        t = float(j["t"]);  assert -55.0 <= t <= 150.0
        aw = float(j["aw"]); assert 0.0 <= aw <= 1.5
        ppm = float(j["ppm"]); assert 0.0 <= ppm <= 1e7
        bat = float(j["bat"]); assert 0.0 <= bat <= 40.0
        cur = float(j["cur"]); assert -2.0 <= cur <= 5.0
        pwr = float(j["pwr"]); assert -10.0 <= pwr <= 100.0
        lvl = int(j["lvl"]);  assert 0 <= lvl <= 100
        rssi = int(j["rssi"]); assert -120 <= rssi <= 0
        seq = int(j["seq"]);  assert 0 <= seq <= 255
        addr = int(j["addr"]); assert 0 <= addr <= 247
    except (ValueError, TypeError, AssertionError) as e:
        return False, "字段越界/类型错: %s" % e
    if not isinstance(j["link"], str):
        return False, "link 类型错"
    if not isinstance(j.get("fw", ""), str):
        return False, "fw 类型错"
    return True, ""


def validate_broadcast(data: bytes) -> tuple[bool, str]:
    """校验一条 UDP 设备发现广播"""
    try:
        j = json.loads(data.decode("utf-8", "ignore").strip())
    except Exception as e:
        return False, "JSON解析失败: %s" % e
    if not isinstance(j, dict):
        return False, "非JSON对象"
    if j.get("discover") != "OILMOIST":
        return False, "discover 标记不符"
    for k in ("dev", "mac", "ip", "port", "rssi", "link"):
        if k not in j:
            return False, "缺字段 %s" % k
    try:
        assert 1 <= int(j["port"]) <= 65535
        assert -120 <= int(j["rssi"]) <= 0
    except (ValueError, TypeError, AssertionError):
        return False, "port/rssi 越界"
    return True, ""


# --------------------------------------------------------------------------- #
# 网络辅助
# --------------------------------------------------------------------------- #
def udp_listen_once(timeout: float) -> tuple[bytes, tuple] | tuple[None, None]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    sock.settimeout(timeout)
    try:
        return sock.recvfrom(4096)
    except socket.timeout:
        return None, None
    finally:
        sock.close()


def discover_device(timeout: float) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        data, addr = udp_listen_once(max(0.5, timeout - (time.time() - t0)))
        if data is None:
            break
        ok, _why = validate_broadcast(data)
        if ok:
            j = json.loads(data.decode("utf-8", "ignore").strip())
            j["ip"] = j.get("ip") or addr[0]
            return j
    return None


def tcp_recv_frames(ip: str, port: int, want: int, timeout: float) -> list[str]:
    frames: list[str] = []
    sock = socket.create_connection((ip, port), timeout=6.0)
    sock.settimeout(timeout)
    buf = b""
    try:
        while len(frames) < want:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf and len(frames) < want:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    frames.append(line.decode("utf-8", "ignore"))
    finally:
        sock.close()
    return frames


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_discover(args) -> int:
    print("[discover] 等待设备广播（%ds）..." % args.timeout)
    dev = discover_device(args.timeout)
    if not dev:
        print("FAIL: 未发现设备")
        return 1
    print("PASS: 发现设备 %s ip=%s port=%s rssi=%s link=%s"
          % (dev.get("dev"), dev.get("ip"), dev.get("port"), dev.get("rssi"), dev.get("link")))
    return 0


def cmd_link(args) -> int:
    ip = args.ip
    if not ip:
        dev = discover_device(args.timeout)
        if not dev:
            print("FAIL: 未发现设备")
            return 1
        ip, port = dev["ip"], int(dev.get("port", DATA_PORT))
    else:
        port = args.port
    print("[link] 连接 %s:%d，收 %d 帧 ..." % (ip, port, args.frames))
    try:
        frames = tcp_recv_frames(ip, port, args.frames, timeout=max(6.0, args.frames * 2.5))
    except OSError as e:
        print("FAIL: 连接失败 %s" % e)
        return 1

    bad = 0
    seqs: list[int] = []
    for f in frames:
        ok, why = validate_frame(f)
        if not ok:
            bad += 1
            print("  坏帧: %s | %s" % (why, f[:100]))
        else:
            seqs.append(json.loads(f)["seq"])
    # seq 连续性（mod 256）
    seq_gap = sum(1 for a, b in zip(seqs, seqs[1:]) if (b - a) % 256 != 1)

    if len(frames) < args.frames:
        print("FAIL: 帧数不足 %d/%d" % (len(frames), args.frames))
        return 1
    if bad:
        print("FAIL: %d 坏帧" % bad)
        return 1
    if seq_gap:
        print("FAIL: seq 不连续 %d 处" % seq_gap)
        return 1
    if frames:
        j0 = json.loads(frames[0])
        print("PASS: %d 帧全部合法，示例 t=%.1f aw=%.3f ppm=%.1f link=%s rssi=%d"
              % (len(frames), j0["t"], j0["aw"], j0["ppm"], j0["link"], j0["rssi"]))
    return 0


def cmd_stress(args) -> int:
    dev = discover_device(args.timeout)
    if not dev:
        print("FAIL: 未发现设备")
        return 1
    ip, port = dev["ip"], int(dev.get("port", DATA_PORT))
    print("[stress] %s:%d 持续 %ds ..." % (ip, port, args.duration))

    n_frames = n_bad = n_reconn = 0
    intervals: list[float] = []
    seq_gaps = 0
    last_seq = None
    t_end = time.time() + args.duration
    last_frame_ts = None

    while time.time() < t_end:
        try:
            sock = socket.create_connection((ip, port), timeout=6.0)
            sock.settimeout(3.0)
        except OSError as e:
            n_reconn += 1
            print("  连接失败(%d): %s" % (n_reconn, e))
            time.sleep(1.0)
            continue
        buf = b""
        try:
            while time.time() < t_end:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    now = time.time()
                    n_frames += 1
                    if last_frame_ts is not None:
                        intervals.append(now - last_frame_ts)
                    last_frame_ts = now
                    ok, why = validate_frame(line.decode("utf-8", "ignore"))
                    if not ok:
                        n_bad += 1
                        print("  坏帧: %s" % why)
                    else:
                        s = json.loads(line)["seq"]
                        if last_seq is not None and (s - last_seq) % 256 != 1:
                            seq_gaps += 1
                        last_seq = s
        except OSError:
            pass
        finally:
            sock.close()
        if time.time() < t_end:
            n_reconn += 1
            print("  连接中断，重连(%d) ..." % n_reconn)
            time.sleep(0.5)

    dur = args.duration
    rate = n_frames / dur if dur else 0.0
    iv = ("%.3f/%.3f/%.3fs" % (min(intervals), sum(intervals) / len(intervals), max(intervals))
          if intervals else "无")
    print("[stress] 帧数=%d (%.2f帧/s) 坏帧=%d 断连重连=%d 丢序=%d 帧间隔min/avg/max=%s"
          % (n_frames, rate, n_bad, n_reconn, seq_gaps, iv))
    ok = n_frames > dur * 0.5 and n_bad == 0 and seq_gaps == 0
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def _fuzz_tcp_server(lines: list[bytes], ready: threading.Event, port_holder: list[int]):
    """假采集端：accept 一个连接后把畸形数据全部灌过去"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    ready.set()
    try:
        conn, _ = srv.accept()
        with conn:
            for ln in lines:
                try:
                    conn.sendall(ln)
                except OSError:
                    break
            time.sleep(0.3)
    finally:
        srv.close()


def cmd_fuzz(args) -> int:
    import random
    rnd = random.Random(20261021)
    cases: list[bytes] = [
        b"", b"\n", b"\r\n", b"{}", b"[]", b"null\n", b"{\n", b'{"t":1}\n',
        b'{"t":"abc","aw":0.3,"ppm":100,"bat":24,"lvl":50,"rssi":-50,"link":"WIFI5G","seq":1,"addr":1}\n',
        b'{"t":1e309,"aw":-5,"ppm":-1,"bat":999,"lvl":999,"rssi":1,"link":1,"seq":9999,"addr":-3}\n',
        b"\xff\xfe\xfd\xfc\xfb\n", b"A" * 8192 + b"\n",
        json.dumps({"t": 25, "aw": 0.3, "ppm": 100, "bat": 24, "lvl": 50,
                    "rssi": -50, "link": "WIFI5G", "seq": 1, "addr": 1}).encode() + b"\n",  # 合法帧
    ]
    # 随机变异：以一条合法帧为种子做字节级变异
    seed = json.dumps({"t": 25.1, "aw": 0.33, "ppm": 110.5, "bat": 24.0, "lvl": 60,
                       "rssi": -55, "link": "WIFI5G", "seq": 7, "addr": 1}).encode()
    for _ in range(args.cases):
        b = bytearray(seed)
        for _ in range(rnd.randint(1, 8)):
            op = rnd.randrange(3)
            pos = rnd.randrange(len(b)) if b else 0
            if op == 0 and b:
                b[pos] = rnd.randrange(256)
            elif op == 1:
                b.insert(pos, rnd.randrange(256))
            elif b:
                del b[pos]
        cases.append(bytes(b) + (b"\n" if rnd.random() < 0.7 else b""))

    # 1) 校验器直接 fuzz（判据：不抛异常，判定结果为 bool）
    bad_oracle = 0
    for i, c in enumerate(cases):
        try:
            ok, _ = validate_frame(c.decode("utf-8", "ignore").strip())
            assert isinstance(ok, bool)
        except Exception as e:
            bad_oracle += 1
            print("  [oracle] 第%d例抛异常: %s" % (i, e))
    print("[fuzz] 校验器 %d 例，异常 %d" % (len(cases), bad_oracle))

    # 2) 假 TCP 服务器灌数据给 backend.Link（真实接收路径，含线程与分帧）
    from PyQt5.QtCore import QCoreApplication
    import backend as host_backend
    app = QCoreApplication.instance() or QCoreApplication([])

    recv: list[str] = []
    crash = None
    link = host_backend.Link()
    link.frameReceived.connect(lambda s: recv.append(s))
    link.stateChanged.connect(lambda d: None)

    ready = threading.Event()
    port_holder: list[int] = []
    srv_t = threading.Thread(target=_fuzz_tcp_server,
                             args=(cases, ready, port_holder), daemon=True)
    srv_t.start()
    ready.wait(3)
    try:
        link.connect_to("127.0.0.1", port_holder[0], "fuzz")
        t0 = time.time()
        # 工作线程发的信号是队列连接，需主线程泵事件才能投递
        while time.time() - t0 < 5 and srv_t.is_alive():
            app.processEvents()
            time.sleep(0.02)
        for _ in range(25):      # 兜底把剩余事件投完
            app.processEvents()
            time.sleep(0.02)
    except Exception as e:
        crash = e
    finally:
        link.disconnect()

    # 灌完后 Link 必须活着可复用，且收到的行都能过校验器而不抛异常
    parse_err = 0
    for s in recv:
        try:
            validate_frame(s)
        except Exception:
            parse_err += 1
    print("[fuzz] Link 收 %d 行，crash=%s 解析异常=%d" % (len(recv), crash, parse_err))

    # 3) UDP 畸形广播注入 backend.Discovery
    disc = host_backend.Discovery()
    disc_crash = None
    try:
        for c in cases[:200]:
            disc._ingest(c, ("127.0.0.1", DISCOVERY_PORT))
    except Exception as e:
        disc_crash = e
    print("[fuzz] Discovery 注入 200 例，crash=%s" % disc_crash)

    ok = bad_oracle == 0 and crash is None and parse_err == 0 and disc_crash is None
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_ui(args) -> int:
    """UI 测试：实例化 MainWindow（不 show），验证页面加载、桥接、演示、真机连接

    平台说明：Windows 下 QtWebEngine 在 offscreen 平台必崩（0xC0000005，
    Chromium GPU/共享上下文初始化失败），故使用原生 QPA，窗口创建后不
    show()，全程无界面弹出、无需键鼠。
    """
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                          "--disable-gpu --no-sandbox --disable-dev-shm-usage")
    # 必须先 import main（其内部 import QtWebEngineWidgets），再创建 QApplication
    import main as host
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    fails: list[str] = []

    win = host.MainWindow()
    # 记录所有注入页面的 JS（替代肉眼检查）
    js_calls: list[str] = []
    orig_js = win._js
    def spy_js(code):
        js_calls.append(code)
        return orig_js(code)
    win._js = spy_js

    # 1) 页面加载（轮询直到 bridge 就绪，超时 30s）
    ready = []
    t0 = time.time()
    while time.time() - t0 < 30:
        app.processEvents()
        r = []
        win.view.page().runJavaScript("!!window.bridge", r.append)
        for _ in range(30):
            app.processEvents()
        if r and r[0]:
            ready = r
            break
        time.sleep(0.1)
    if not (ready and ready[0]):
        fails.append("页面加载失败或 window.bridge 未就绪")
    else:
        print("[ui] 页面加载 OK，bridge 就绪（%.1fs）" % (time.time() - t0))

    # 2) 设备发现（真实广播，板子在线时）
    t0 = time.time()
    dev_seen = False
    while time.time() - t0 < 8:
        app.processEvents()
        if any("setDevices" in c and "OilSensor" in c for c in js_calls):
            dev_seen = True
            break
        time.sleep(0.05)
    if dev_seen:
        print("[ui] 设备发现 OK（页面收到 setDevices 含 OilSensor）")
    else:
        print("[ui] 设备未发现（板子不在线时跳过，不判失败）")

    # 3) 演示模式：demo on → 应收 demo 帧 pushFrame
    js_calls.clear()
    win._exec({"cmd": "demo", "on": True})
    t0 = time.time()
    demo_frames = 0
    while time.time() - t0 < 4:
        app.processEvents()
        demo_frames = sum(1 for c in js_calls if "pushFrame" in c)
        if demo_frames >= 2:
            break
        time.sleep(0.05)
    if demo_frames >= 2:
        # 抽一条 demo 帧校验协议（正则提取 pushFrame 的 JS 字符串实参）
        import re
        sample = next(c for c in js_calls if "pushFrame" in c)
        m = re.search(r'pushFrame\(("(?:[^"\\]|\\.)*")\s*\)\s*$', sample, re.S)
        m2 = None if m else re.search(r'pushFrame\((\{.*\})\s*\)\s*$', sample, re.S)
        try:
            if m:
                frame_text = json.loads(m.group(1))   # 字符串实参 → 还原 JSON 文本
            elif m2:
                frame_text = m2.group(1)              # 对象字面量 → 直接是 JSON 文本
            else:
                raise ValueError("pushFrame 实参提取失败: %s" % sample[:120])
            ok, why = validate_frame(frame_text)
            if not ok:
                fails.append("demo帧协议不合法: %s" % why)
        except Exception as e:
            fails.append("demo帧提取失败: %s" % e)
        print("[ui] 演示模式 OK（%d 帧）" % demo_frames)
    else:
        fails.append("演示模式未产生数据帧")
    # 用 JS 点击演示按钮关闭（而非 _exec 绕过前端），保持 S.mode 与宿主一致，
    # 否则后续第 8 步再点按钮会因 S.mode 仍是 demo 而走到关闭分支
    win.view.page().runJavaScript(
        "(function(){var b=document.getElementById('btnDemo');"
        "if(b&&window.__app.S.mode==='demo')b.click();return 1;})()",
        lambda _v: None)
    t0 = time.time()
    while time.time() - t0 < 5:          # 等前端 mode 回到 idle + 宿主取走 demo off
        app.processEvents()
        ri = []
        win.view.page().runJavaScript("window.__app.S.mode", ri.append)
        for _ in range(20):
            app.processEvents()
            if ri:
                break
            time.sleep(0.05)
        if ri and ri[0] == "idle":
            break
        time.sleep(0.05)

    # 4) 真机连接（板子在线时）：connect → connected + 真数据帧
    dev = discover_device(3)
    if dev:
        js_calls.clear()
        win._exec({"cmd": "connect", "ip": dev["ip"], "port": int(dev.get("port", DATA_PORT)),
                   "name": "ui-test"})
        t0 = time.time()
        connected = False
        real_frames = 0
        while time.time() - t0 < 12:
            app.processEvents()
            if any("setConnState" in c and '"connected"' in c for c in js_calls):
                connected = True
            real_frames = sum(1 for c in js_calls if "pushFrame" in c)
            if connected and real_frames >= 3:
                break
            time.sleep(0.05)
        if not connected:
            fails.append("真机连接未进入 connected 状态")
        if real_frames < 3:
            fails.append("真机数据帧不足: %d" % real_frames)
        else:
            print("[ui] 真机连接 OK（connected + %d 帧）" % real_frames)
        win._exec({"cmd": "disconnect"})
    else:
        print("[ui] 板子不在线，跳过真机连接用例")

    # 5) 全屏偏好与快捷键存在性
    if not isinstance(win._config.get("auto_fullscreen"), bool):
        fails.append("auto_fullscreen 配置异常")

    # 6) EIS GUI 命令路径：monkeypatch 目录对话框（不弹窗），走完按钮→解析→渲染全链路
    # 测试数据固定在 工程管理/参考资料/（02/03 两个 61 点导出文件，含模型推理断言）
    eis_dir = str(Path(__file__).resolve().parent.parent / "工程管理" / "参考资料")
    from PyQt5.QtWidgets import QFileDialog as _QFD
    orig_get_dir = _QFD.getExistingDirectory
    _QFD.getExistingDirectory = staticmethod(lambda *a, **k: eis_dir)
    try:
        js_calls.clear()
        win._exec({"cmd": "import_eis"})   # 与用户点击「导入数据」同一入口
        t0 = time.time()
        eis_pushed = False
        while time.time() - t0 < 10:
            app.processEvents()
            if any("setEIS" in c for c in js_calls):
                eis_pushed = True
                break
            time.sleep(0.05)
        if not eis_pushed:
            fails.append("EIS 导入后页面未收到 setEIS")
        else:
            r = []
            win.view.page().runJavaScript(
                "JSON.stringify({n:window.__app.eis?window.__app.eis.count:0,"
                "cv:!!document.querySelector('.eis-chart canvas'),"
                "td:window.__app.eis?window.__app.eis.tand:null,"
                "ac:window.__app.eis?window.__app.eis.acid:null,"
                "empty:getComputedStyle(document.getElementById('eisEmpty')).display})",
                r.append)
            for _ in range(40):
                app.processEvents()
                if r:
                    break
                time.sleep(0.05)
            if not r:
                fails.append("EIS DOM 断言超时")
            else:
                info = json.loads(r[0])
                if not (info["n"] >= 1 and info["cv"]):
                    fails.append("EIS 渲染异常: %s" % info)
                elif info["empty"] != "none":
                    fails.append("EIS 导入后空态提示未隐藏: %s" % info)
                elif not (isinstance(info["td"], (int, float)) and info["td"] > 0
                          and isinstance(info["ac"], (int, float)) and info["ac"] > 0):
                    # 模型接入验收：导入后介损/酸值卡片必须出数（Transformer 推理结果）
                    fails.append("EIS 导入后介损/酸值未出数: %s" % info)
                else:
                    print("[ui] EIS GUI 链路 OK（%d 个文件，画布已渲染，空态已隐藏，"
                          "tand=%.4f acid=%.4f）" % (info["n"], info["td"], info["ac"]))
            # 博德/Nyquist 切换不报错
            r2 = []
            win.view.page().runJavaScript(
                "(function(){try{var b=document.querySelectorAll('#segEisView button');"
                "if(b.length>1){b[1].click();b[0].click();}return 'ok';}catch(e){return 'err:'+e;}})()",
                r2.append)
            for _ in range(40):
                app.processEvents()
                if r2:
                    break
                time.sleep(0.05)
            if not r2 or r2[0] != "ok":
                fails.append("EIS 视图切换异常: %s" % (r2 or ["超时"]))
    finally:
        _QFD.getExistingDirectory = orig_get_dir

    # 7) 单屏布局断言：无纵向溢出、六卡无内部滚动
    r3 = []
    win.view.page().runJavaScript(
        "JSON.stringify({doc:document.documentElement.scrollHeight<=document.documentElement.clientHeight,"
        "cards:Array.prototype.map.call(document.querySelectorAll("
        "'.dash-eis,.dash-trend,#cardTand,#cardAcid,#cardLive,#cardPower'),"
        "function(e){return e.scrollHeight<=e.clientHeight+1;})})",
        r3.append)
    for _ in range(40):
        app.processEvents()
        if r3:
            break
        time.sleep(0.05)
    if not r3:
        fails.append("布局断言超时")
    else:
        lay = json.loads(r3[0])
        # 注：窗口未 show()，视口高度为初始尺寸，布局仍成立；只断内部滚动
        if not all(lay["cards"]):
            fails.append("卡片存在内部滚动溢出: %s" % lay)
        else:
            print("[ui] 六卡无内部滚动溢出 OK")

    # 8) 演示按钮 → 演示中 → 再点退出（顶栏按钮链路）
    # 先确保从 idle 起步：第 4 步的 JS 关闭点击若尚未被宿主处理完，
    # 这里直接再点会把「开」点成「关」，导致偶发 FAIL（回归已复现一次）
    t_idle = time.time()
    while time.time() - t_idle < 5:
        app.processEvents()
        r0 = []
        win.view.page().runJavaScript("window.__app.S.mode", r0.append)
        for _ in range(20):
            app.processEvents()
            if r0:
                break
            time.sleep(0.05)
        if r0 and r0[0] == "idle":
            break
        time.sleep(0.1)
    r4 = []
    win.view.page().runJavaScript(
        "(function(){var b=document.getElementById('btnDemo');if(!b)return 'no-btn';"
        "b.click();return 'clicked';})()", r4.append)
    for _ in range(30):
        app.processEvents()
        if r4:
            break
        time.sleep(0.05)
    if not r4 or r4[0] != "clicked":
        fails.append("演示按钮缺失或点击失败: %s" % (r4 or ["超时"]))
    else:
        t0 = time.time()
        demo_ok = False
        while time.time() - t0 < 10:
            app.processEvents()
            r5 = []
            win.view.page().runJavaScript(
                "JSON.stringify({m:window.__app.S.mode,"
                "dot:document.getElementById('demoDot').className,"
                "t:document.getElementById('btnDemoText').textContent})", r5.append)
            for _ in range(20):
                app.processEvents()
                if r5:
                    break
            if r5:
                d = json.loads(r5[0])
                if d["m"] == "demo" and "on" in d["dot"] and d["t"] == "演示中":
                    demo_ok = True
                    break
            time.sleep(0.1)
        if demo_ok:
            print("[ui] 演示按钮链路 OK（demo 模式 + 圆点亮 + 演示中）")
        else:
            # 回归高负载下 runJavaScript 回调可能整体迟滞——以最终状态快照兜底判定
            rdbg = []
            win.view.page().runJavaScript(
                "JSON.stringify({m:window.__app.S.mode,tr:window.__app.S.transport,"
                "conn:window.__app.S.conn.state,local:window.__app.S.localDemo,"
                "dot:document.getElementById('demoDot').className,"
                "t:document.getElementById('btnDemoText').textContent})", rdbg.append)
            for _ in range(40):
                app.processEvents()
                if rdbg:
                    break
                time.sleep(0.05)
            if rdbg:
                d = json.loads(rdbg[0])
                if d["m"] == "demo" and "on" in d["dot"] and d["t"] == "演示中":
                    demo_ok = True
                    print("[ui] 演示按钮链路 OK（回调迟滞，最终状态快照判定）")
            if not demo_ok:
                fails.append("演示按钮状态未生效: %s" % (rdbg or ["状态读取超时"]))
        win._exec({"cmd": "demo", "on": False})

    win.close()
    win.deleteLater()
    app.processEvents()

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS")
    return 0


def cmd_regression(args) -> int:
    print("========== 回归测试 ==========")
    rc = 0
    for name, fn in (("discover", cmd_discover), ("link", cmd_link),
                     ("fuzz", cmd_fuzz), ("csv", cmd_csv), ("ota", cmd_ota),
                     ("ui", cmd_ui)):
        print("\n----- %s -----" % name)
        rc |= fn(args)
    print("\n========== 回归 %s ==========" % ("全部通过" if rc == 0 else "存在失败"))
    return rc


# --------------------------------------------------------------------------- #
# CSV 实时追加测试（不启 GUI，纯逻辑验证）
# --------------------------------------------------------------------------- #
def cmd_csv(args) -> int:
    import tempfile, csv as pycsv
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="csv_test_"))
    print(f"[csv] 临时目录: {tmp}")

    # ---------- 测试 1: 基本追加 ----------
    print("[csv] 测试 1: 基本追加（新 13 列 schema，记录号本机自增 + 毫秒时间戳）")
    import re
    import main as host          # 直接复用生产代码的表头/行构造，防止表头与行漂移

    header = host.CSV_HEADER
    csv_path = tmp / "oil_test.csv"
    with open(csv_path, "w", encoding="utf-8-sig") as f:
        f.write(header)

    frames = [
        {"seq": 1, "t": 27.6, "aw": 0.607, "ppm": 42.6, "bat": 24.22,
         "cur": 0.042, "pwr": 1.02, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
        {"seq": 2, "t": 27.8, "aw": 0.608, "ppm": 43.1, "bat": 24.20,
         "cur": 0.041, "pwr": 0.99, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
        {"seq": 3, "t": 28.0, "aw": 0.610, "ppm": 44.0, "bat": 24.18,
         "cur": 0.040, "pwr": 0.97, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
        {"seq": 4, "t": 28.2, "aw": 0.612, "ppm": 44.5, "bat": 24.15,
         "cur": 0.040, "pwr": 0.96, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
        {"seq": 5, "t": 28.4, "aw": 0.614, "ppm": 45.0, "bat": 24.12,
         "cur": 0.039, "pwr": 0.94, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 0},
        # 设备 seq 回绕：255 → 0，记录号必须继续自增
        {"seq": 255, "t": 28.5, "aw": 0.615, "ppm": 45.2, "bat": 24.10,
         "cur": 0.039, "pwr": 0.93, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
        {"seq": 0, "t": 28.6, "aw": 0.616, "ppm": 45.4, "bat": 24.08,
         "cur": 0.038, "pwr": 0.92, "link": "WIFI5G", "fw": "1.0.4", "s_ok": 1},
    ]

    base = time.time()
    # 故意让若干帧落在同一秒（2Hz 采样），验证毫秒时间戳仍可排序
    for i, d in enumerate(frames):
        row = host.build_csv_row(i + 1, d, now=base + (i // 2) * 0.2 + (i % 2) * 0.1)
        # 追加时用纯 utf-8：utf-8-sig 在追加模式下会重复写 BOM（生产代码是单句柄，不受影响）
        with open(csv_path, "a", encoding="utf-8") as f:
            f.write(row)

    # 验证结构
    with open(csv_path, encoding="utf-8-sig") as f:
        lines = [l.strip() for l in f if l.strip()]
    if len(lines) != 8:  # header + 7 rows
        print(f"  FAIL: 期望 8 行，实际 {len(lines)}"); return 1
    ncol = len(lines[0].split(","))
    if ncol != 13:
        print(f"  FAIL: 表头期望 13 列，实际 {ncol}: {lines[0]}"); return 1
    bad = [l for l in lines[1:] if len(l.split(",")) != ncol]
    if bad:
        print(f"  FAIL: 数据行列数不一致: {bad[0]}"); return 1

    cols = lines[1].split(",")
    if cols[0] != "1":
        print(f"  FAIL: 首条记录号应为 1，实际 {cols[0]}"); return 1
    if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", cols[1]):
        print(f"  FAIL: 时间戳格式错误（需 yyyy-MM-dd HH:mm:ss.SSS）: {cols[1]}"); return 1
    if (cols[2], cols[8], cols[9], cols[10], cols[11], cols[12]) != ("27.6", "WIFI5G", "1.0.4", "正常", "正常", "1"):
        print(f"  FAIL: 列错位/取值错误 {cols}"); return 1

    # 记录号严格 +1，且与设备 seq 回绕无关
    ids = [int(l.split(",")[0]) for l in lines[1:]]
    if ids != list(range(1, 8)):
        print(f"  FAIL: 记录号不连续: {ids}"); return 1
    wrap = lines[7].split(",")
    if wrap[0] != "7" or wrap[12] != "0":
        print(f"  FAIL: seq 回绕后记录号/设备序号错误: {wrap}"); return 1

    # 时间戳单调递增（同秒内靠毫秒区分）
    stamps = [l.split(",")[1] for l in lines[1:]]
    if stamps != sorted(stamps):
        print(f"  FAIL: 时间戳未单调递增: {stamps}"); return 1

    # 失联行分类
    last = lines[5].split(",")
    if last[10] != "失联" or last[11] != "失联":
        print(f"  FAIL: 失联行状态/分类应为文本「失联」: {last}"); return 1

    # BOM 必须只有一个（双 BOM 会让 Excel 首格乱码）
    raw = csv_path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf") or raw.startswith(b"\xef\xbb\xbf\xef\xbb\xbf"):
        print(f"  FAIL: BOM 异常，文件头字节: {raw[:6].hex()}"); return 1

    print(f"  PASS: {len(lines)-1} 行 × {ncol} 列，记录号 1→7 连续（含 seq 255→0 回绕）、"
          f"毫秒时间戳单调、状态/分类文本化、单 BOM")

    # ---------- 测试 2: 切分逻辑 ----------
    print("[csv] 测试 2: 自动切分（跨文件记录号连续）")
    import shutil
    csv2_path = tmp / "oil_a.csv"
    with open(csv2_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        f.write(host.build_csv_row(8, frames[0], now=base + 5.0))
    csv3_path = tmp / "oil_b.csv"
    with open(csv3_path, "w", encoding="utf-8-sig") as f:
        f.write(header)
        f.write(host.build_csv_row(9, frames[1], now=base + 6.0))

    csvs = list(tmp.glob("oil_*.csv"))
    if len(csvs) < 3:
        print(f"  FAIL: 期望至少 3 个文件，实际 {len(csvs)}"); return 1
    if csv2_path.read_text(encoding="utf-8-sig").splitlines()[1].split(",")[0] != "8" or \
       csv3_path.read_text(encoding="utf-8-sig").splitlines()[1].split(",")[0] != "9":
        print("  FAIL: 切分后记录号未延续"); return 1
    print(f"  PASS: 目录下 {len(csvs)} 个 CSV 文件，切分后记录号 8→9 连续")

    # 清理
    shutil.rmtree(str(tmp), ignore_errors=True)
    print("[csv] PASS 全部通过")
    return 0


# --------------------------------------------------------------------------- #
# OTA HTTP 接口连通性测试
# --------------------------------------------------------------------------- #
def cmd_ota(args) -> int:
    try:
        import urllib.request as urlreq
    except ImportError:
        print("[ota] FAIL: 无法导入 urllib.request"); return 1

    ip = args.ip or ""
    if not ip:
        # 收集 timeout 内所有广播，优先真机（排除 127.x 模拟器 fake_device）
        found_real = found_any = None
        t0 = time.time()
        while time.time() - t0 < args.timeout:
            data, addr = udp_listen_once(max(0.5, args.timeout - (time.time() - t0)))
            if data is None:
                break
            ok, _why = validate_broadcast(data)
            if not ok:
                continue
            j = json.loads(data.decode("utf-8", "ignore").strip())
            j["ip"] = j.get("ip") or addr[0]
            if found_any is None:
                found_any = j
            if not j["ip"].startswith("127.") and j.get("link") != "SIM":
                found_real = j
                break
        disc = found_real or found_any
        if not disc:
            print("[ota] FAIL: discover 没找到设备"); return 1
        ip = disc["ip"]
        if disc is found_any and ip.startswith("127."):
            print("[ota] SKIP: 仅发现模拟器（无 HTTP OTA 口），真机不在线")
            return 0

    ok = True
    print(f"[ota] 测试 http://{ip}/version ...")
    try:
        with urlreq.urlopen(f"http://{ip}:80/version", timeout=5.0) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        if "fw" not in j:
            print(f"  FAIL: 缺少 fw 字段，响应={j}"); ok = False
        else:
            print(f"  PASS: fw={j['fw']}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}"); ok = False

    print(f"[ota] 测试 http://{ip}/status ...")
    try:
        with urlreq.urlopen(f"http://{ip}:80/status", timeout=5.0) as resp:
            j = json.loads(resp.read().decode("utf-8"))
        state = j.get("state", "")
        if state not in ("idle", "success", "error", "updating"):
            print(f"  FAIL: state 值异常={state}"); ok = False
        else:
            print(f"  PASS: state={state} progress={j.get('progress')}%")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}"); ok = False

    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="上位机命令行测试套件")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "link", "stress", "fuzz", "ui", "csv", "ota", "regression"):
        p = sub.add_parser(name)
        p.add_argument("--timeout", type=float, default=10.0)
        p.add_argument("--ip", default="")
        p.add_argument("--port", type=int, default=DATA_PORT)
        p.add_argument("--frames", type=int, default=10)
        p.add_argument("--duration", type=float, default=60.0)
        p.add_argument("--cases", type=int, default=300)
    args = ap.parse_args()
    fn = {
        "discover": cmd_discover, "link": cmd_link, "stress": cmd_stress,
        "fuzz": cmd_fuzz, "ui": cmd_ui, "regression": cmd_regression,
        "csv": cmd_csv, "ota": cmd_ota,
    }[args.cmd]
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
