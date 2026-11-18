# -*- coding: utf-8 -*-
"""油液水分监测系统 · 桌面上位机（PyQt5 + QtWebEngine，免安装单文件）

入口 + 主窗口装配。各层职责已拆到独立模块：
  ui_server.py   内置 HTTP 服务托管 oil-moisture-monitor 界面资源
  backend.py     UDP 8888 设备发现 + TCP 数据链路
  bridge.py      宿主 ↔ 页面桥接（runJavaScript 推送 + 命令队列路由）
  cli.py         127.0.0.1:9099 命令行 HTTP 通道（selftest 依赖）
  csv_store.py   实时 CSV 追加 / 自动切分 / 快照导出（13 列）
  eis_loader.py  阻抗谱 xlsx 解析
  config.py      %APPDATA% 用户偏好读写
  demo_source.py 演示模式模拟数据源
  ota.py         无线 OTA（multipart POST 到设备 /update）

桥接口（详见 app.js 顶部说明）：
  宿主 → 页面   window.bridge.setDevices / setScanning / setConnState
                pushFrame / pushLog / setTransport / setConfig / setEIS / setPaused
  页面 → 宿主   window.bridge.post({cmd:...}) 由 CommandRouter 轮询 drainCommands 取走

兼容契约（selftest 依赖，勿改）：
  main.MainWindow / main.CSV_HEADER / main.build_csv_row
  实例属性 win._js(code) / win._exec(cmd) / win._config / win.view / win.discovery / win.link
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PyQt5.QtCore import Qt, QPointF, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QPolygonF
from PyQt5.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox, QShortcut

# 必须在 QApplication 之前导入 QtWebEngine，以便正确初始化 OpenGL 共享上下文
from PyQt5.QtWebEngineWidgets import QWebEngineSettings, QWebEngineView

from backend import DISCOVERY_PORT, Discovery, Link
from bridge import CommandRouter, PageBridge
from cli import CLI_PORT, CliServer
from config import CONFIG_DIR, load_config, save_config
from csv_store import CSV_HEADER, CsvStore, build_csv_row   # noqa: F401  (selftest 复用)
from demo_source import DemoSource
from eis_loader import load_eis_dir
from ota import upload_firmware
from serial_provision import SerialProvisioner
from ui_server import UiServer, ui_dir

APP_NAME = "油液水分监测系统"
APP_VERSION = "1.5"


# --------------------------------------------------------------------------- #
# 窗口图标（运行时用 QPainter 画一个水滴，不依赖外部图片文件）
# --------------------------------------------------------------------------- #
def make_icon() -> QIcon:
    size = 256
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)

    p.setBrush(QColor("#1257d6"))
    p.drawRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)

    p.setBrush(QColor("#ffffff"))
    cx, cy, r = size * 0.5, size * 0.62, size * 0.235
    p.drawEllipse(QPointF(cx, cy), r, r)
    p.drawPolygon(QPolygonF([
        QPointF(cx - r * 0.78, cy - r * 0.30),
        QPointF(cx + r * 0.78, cy - r * 0.30),
        QPointF(cx, size * 0.135),
    ]))
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    cliCommand = pyqtSignal(dict)   # CLI HTTP 线程 → GUI 线程的命令通道

    def __init__(self):
        super().__init__()
        self.setWindowTitle("%s  v%s" % (APP_NAME, APP_VERSION))
        self.setWindowIcon(make_icon())
        # 按屏幕可用区域自适应窗口，避免超出屏幕或显得过大
        scr = QApplication.primaryScreen()
        avail = scr.availableGeometry() if scr else None
        if avail is not None:
            w = max(960, min(int(avail.width() * 0.94), 1680))
            h = max(600, min(int(avail.height() * 0.92), 1050))
        else:
            w, h = 1280, 800
        self.setMinimumSize(940, 600)
        self.resize(w, h)

        # 启动偏好（是否自动全屏）持久化在 config.json
        self._config = load_config()

        # F11 切换全屏 / Esc 退出全屏
        QShortcut(QKeySequence("F11"), self, activated=self._toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self._exit_fullscreen)

        # ---- 界面视图 ----
        self.view = QWebEngineView(self)
        st = self.view.settings()
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        st.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, False)
        st.setAttribute(QWebEngineSettings.ScrollAnimatorEnabled, True)
        self.setCentralWidget(self.view)

        # ---- 页面桥接（宿主 → 页面 推送 / 页面 → 宿主 命令）----
        self.bridge = PageBridge(self)
        self.router = CommandRouter(self)

        # ---- 网络后端 ----
        self.discovery = Discovery(self)
        self.discovery.devicesChanged.connect(self._on_devices)
        self.link = Link(self)
        self.link.frameReceived.connect(self._on_frame)
        self.link.stateChanged.connect(self._on_conn_state)

        # ---- 启动内置服务并加载界面 ----
        self.ui_server = UiServer(ui_dir())
        port = self.ui_server.start()
        self.view.load(QUrl("http://127.0.0.1:%d/%s" % (port, self._html_name())))
        self.view.loadFinished.connect(self._on_loaded)

        # ---- 演示数据源 ----
        self.demo = DemoSource(self)
        self.demo.frameReady.connect(self._on_demo_frame)

        # ---- USB 串口配网（有线配网） ----
        self.prov = SerialProvisioner(self)
        self.prov.provEvent.connect(self._on_prov_event)

        # ---- 实时 CSV 追加（支持 csv_split_minutes 自动切分）----
        self.csv = CsvStore(self._config, CONFIG_DIR / "csv")

        # ---- CLI 状态跟踪 + 命令通道 ----
        self._conn_state = {"state": "idle", "name": "", "ip": "", "port": 0, "msg": "未连接设备"}
        self._last_frame = None
        self.cliCommand.connect(self._exec)
        self._cli = CliServer(self)
        if self._cli.start():
            print("[cli] 命令行接口已启动: http://127.0.0.1:%d/cli/status" % CLI_PORT)
        else:
            print("[cli] 端口 %d 被占用，CLI 不可用" % CLI_PORT)

        # ---- 页面命令轮询 ----
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(200)
        self._poll_timer.timeout.connect(self.router.poll)
        self._poll_timer.start()

        # 扫描动画持续时间（列表本身会一直刷新）
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.timeout.connect(lambda: self.bridge.set_scanning(False))

        # 启动即开始发现设备
        self.discovery.start()

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #
    def _html_name(self) -> str:
        files = sorted(ui_dir().glob("*.html"))
        return files[0].name if files else "index.html"

    def _js(self, code: str):
        """向页面注入一段 JS（PageBridge 每次调用时动态取本方法，selftest 可替换）"""
        self.view.page().runJavaScript(code)

    # ------------------------------------------------------------------ #
    # 页面加载完成 → 推送初始状态
    # ------------------------------------------------------------------ #
    def _on_loaded(self, ok):
        if not ok:
            QMessageBox.warning(self, APP_NAME, "界面资源加载失败，请检查 oil-moisture-monitor 目录是否完整。")
            return
        # 页面（重）加载完成后必须回推当前真实状态，不能硬编码 idle：
        # /cli/reload 或页面刷新后链路可能仍连着，硬编码会让顶栏误显「未连接」
        self._on_conn_state(self._conn_state)
        if self.demo.running:
            self.bridge.set_transport("demo")
        self.discovery.devicesChanged.emit(self.discovery.snapshot())
        self.bridge.push_log("界面已加载，正在监听设备广播 (UDP %d)" % DISCOVERY_PORT)
        self._push_config()

    # ------------------------------------------------------------------ #
    # 后端信号 → 页面
    # ------------------------------------------------------------------ #
    def _on_devices(self, devs):
        self.bridge.set_devices(devs)

    def _on_frame(self, line: str):
        # line 是设备发来的原始 JSON 文本
        self._last_frame = line  # CLI /cli/frame 用
        self._append_csv(line)   # 实时追加 CSV（支持自动切分）
        self.bridge.push_frame(line)

    def _on_conn_state(self, state: dict):
        self._conn_state = state  # CLI /cli/status 用
        transport = "wifi" if state.get("state") == "connected" else "none"
        self.bridge.set_transport(transport)
        self.bridge.set_conn_state(state)

    # ------------------------------------------------------------------ #
    # 页面命令（命令队列轮询在 CommandRouter）
    # ------------------------------------------------------------------ #
    def _exec(self, cmd: dict):
        """CLI 线程 / 页面命令统一入口（selftest 直调本方法）"""
        self.router.dispatch(cmd)

    # ------------------------------------------------------------------ #
    # 阻抗谱（EIS）导入
    # ------------------------------------------------------------------ #
    def _import_eis_dir(self, d: str):
        try:
            payload = load_eis_dir(d)
        except Exception as exc:
            self.bridge.push_log("阻抗谱导入失败：%s" % json.dumps(str(exc))[1:-1])
            return
        self._push_eis(payload)

    def _push_eis(self, payload: dict):
        sets = payload.get("sets") or []
        skipped = int(payload.get("skipped") or 0)
        self.bridge.set_eis(payload)
        if sets:
            self.bridge.push_log("阻抗谱导入完成：%d 个文件%s"
                                 % (len(sets), ("，跳过 %d 个非阻抗谱文件" % skipped) if skipped else ""))
        else:
            self.bridge.push_log("未在该文件夹找到有效的阻抗谱 xlsx")

    # ------------------------------------------------------------------ #
    # 演示模式
    # ------------------------------------------------------------------ #
    def _start_demo(self):
        self.demo.start()
        self.bridge.set_transport("demo")
        self.bridge.push_log("已进入演示模式（模拟数据）")

    def _stop_demo(self):
        if not self.demo.running:
            return
        self.demo.stop()
        self.bridge.push_log("已退出演示模式")

    def _on_demo_frame(self, frame: dict):
        self.bridge.push_frame(frame)

    # ------------------------------------------------------------------ #
    # USB 串口配网事件 → 页面 + 日志
    # ------------------------------------------------------------------ #
    def _on_prov_event(self, d: dict):
        self.bridge.set_prov_state(d)
        stage = d.get("stage")
        msg = ""
        if stage == "hello":
            msg = ("有线配网：连接成功 %s (%s) fw=%s" % (d.get("dev"), d.get("mac"), d.get("fw"))) \
                if d.get("ok") else "有线配网：握手失败（%s）" % (d.get("err") or "无应答")
        elif stage == "scan":
            msg = ("有线配网：扫描到 %d 个网络" % len(d.get("nets") or [])) \
                if d.get("ok") else "有线配网：扫描失败（%s）" % (d.get("err") or "")
        elif stage == "set":
            msg = ("有线配网：配对成功，设备重启中，IP=%s" % d.get("ip")) \
                if d.get("ok") else "有线配网：配对失败（%s）" % (d.get("err") or "")
        elif stage == "closed":
            msg = "有线配网：串口已关闭"
        elif stage == "error":
            msg = "有线配网：%s" % (d.get("msg") or "错误")
        if msg:
            self.bridge.push_log(json.dumps(msg)[1:-1])

    def _ota_log(self, text: str):
        self.bridge.push_log(text)

    def _ota_upload(self, ip: str, bin_path: str):
        upload_firmware(ip, bin_path, self._ota_log)

    # ------------------------------------------------------------------ #
    # CSV 导出（页面历史数据保存为文件）
    # ------------------------------------------------------------------ #
    def _save_csv(self, csv_text: str):
        if not csv_text:
            return
        default = "oil_moisture_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "保存历史数据", default, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            # JS 侧 buildCSV 已带一个 \uFEFF，这里先剥掉再补一个，避免出现双 BOM（Excel 首格变乱码）
            text = csv_text.lstrip("\ufeff")
            Path(path).write_text("\ufeff" + text, encoding="utf-8")  # BOM 便于 Excel 打开
            self.bridge.push_log(json.dumps(path)[1:-1])
            QMessageBox.information(self, APP_NAME, "已保存：\n%s" % path)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, "保存失败：%s" % exc)

    # ------------------------------------------------------------------ #
    # 窗口显示模式 / 配置
    # ------------------------------------------------------------------ #
    def apply_startup_mode(self):
        """按配置决定启动方式：全屏或最大化（保证铺满屏幕、自适应分辨率）"""
        if self._config.get("auto_fullscreen", True):
            self.showFullScreen()
        else:
            self.showMaximized()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized()
        else:
            self.showFullScreen()

    def _exit_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized()

    def _push_config(self):
        self.bridge.set_config(self._config)

    def save_config(self):
        """把当前内存配置写回 config.json"""
        save_config(self._config)

    def ask_firmware_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "选择固件文件 (.bin)",
                                              "", "固件文件 (*.bin);;所有文件 (*)")
        return path

    def ask_eis_dir(self) -> str:
        return QFileDialog.getExistingDirectory(self, "选择阻抗谱数据文件夹", "")

    # ------------------------------------------------------------------ #
    # 实时 CSV 追加（CsvStore：追加 / 切分 / 快照导出）
    # ------------------------------------------------------------------ #
    def _append_csv(self, line: str):
        self.csv.append_line(line)

    def closeEvent(self, event):
        self.demo.stop()
        self.prov.close()
        self._poll_timer.stop()
        self.discovery.stop()
        self.link.disconnect()
        self.ui_server.stop()
        self._cli.stop()
        self.csv.close()
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
def main():
    # 关键：禁用 Chromium 沙箱。企业 EDR / Defender ASR 规则会拦截
    # QtWebEngine 沙箱子进程的创建，导致 Qt5Core 初始化阶段 0xc0000409 崩溃
    # （表现为「必须管理员才能打开」）。禁用沙箱后普通用户双击即可运行。
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"

    # 关键：禁用 Chromium 后台节流。窗口被遮挡/最小化/失焦后，Chromium 默认会
    # 冻结 JS timer 与 rAF，导致状态卡片/曲线停止刷新（实测 DOM 冻结近 30 分钟，
    # 而 pushFrame 仍在执行、内部计数照涨——极具迷惑性）。必须在 QApplication
    # 创建前设置，且只对之后启动的 QtWebEngine 进程生效。
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
        os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") +
        " --no-sandbox"
        " --disable-background-timer-throttling"
        " --disable-renderer-backgrounding"
        " --disable-backgrounding-occluded-windows"
        # 关键：禁用 GPU 合成（软件合成）。GPU 合成下窗口尺寸/全屏状态突变时
        # Chromium 要重建 swapchain，瞬间输出黑帧——这就是「ESC 退出全屏黑一下」
        # 的物理根因（已排除窗口 flags 重建因素：纯 resize 仍黑）。本应用为 2D
        # 图表仪表盘，软件合成性能完全足够，换来全屏切换零黑屏。
        " --disable-direct-composition"
    ).strip()

    try:
        # 允许 125% / 150% 等分数缩放，避免高分屏下内容被放大得过大
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception:
        pass

    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    win = MainWindow()
    win.apply_startup_mode()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
