# -*- coding: utf-8 -*-
"""打包免安装单文件 exe（PyInstaller）

用法： D:\\anaconda3\\python.exe build.py

为什么不用 PowerShell 写这个逻辑：
  Windows PowerShell 5.1 读取「无 BOM 的 UTF-8 脚本」时会按 GBK 解码，
  脚本里的中文字面量（例如 exe 名称「油液水分监测系统」）会变成乱码，
  最终生成一个乱码文件名的 exe。改用 Python 则源码固定按 UTF-8 解析，
  中文名称永远正确。build.ps1 只是一层 ASCII 调用外壳。

背景：本项目使用 Anaconda 自带的 PyQt5，其 Qt 运行库位于
      D:\\anaconda3\\Library\\bin，文件名带 "_conda" 后缀，
      PyInstaller 自带钩子匹配不到，必须显式加进来。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "油液水分监测系统"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

QT_BIN = Path(r"D:\anaconda3\Library\bin")
QT_RES = Path(r"D:\anaconda3\Library\resources")
QT_LOC = Path(r"D:\anaconda3\Library\translations\qtwebengine_locales")
QT_PLUGINS = Path(r"D:\anaconda3\Library\plugins")

QT_DLLS = [
    "Qt5Core_conda.dll", "Qt5Gui_conda.dll", "Qt5Widgets_conda.dll", "Qt5Network_conda.dll",
    "Qt5PrintSupport_conda.dll", "Qt5Qml_conda.dll", "Qt5Quick_conda.dll", "Qt5QuickWidgets_conda.dll",
    "Qt5WebChannel_conda.dll", "Qt5WebEngineCore_conda.dll", "Qt5WebEngineWidgets_conda.dll",
    "Qt5Positioning_conda.dll", "Qt5Svg_conda.dll", "Qt5OpenGL_conda.dll", "Qt5WebSockets_conda.dll",
    "Qt5DBus_conda.dll",
    "libEGL.dll", "libGLESv2.dll", "opengl32sw.dll",
    "QtWebEngineProcess.exe",
]

QT_RES_FILES = [
    "icudtl.dat", "qtwebengine_resources.pak", "qtwebengine_resources_100p.pak",
    "qtwebengine_resources_200p.pak", "qtwebengine_devtools_resources.pak",
]

QT_PLUGIN_DIRS = ["platforms", "imageformats", "iconengines", "styles"]


def collect_extra_args() -> list[str]:
    args: list[str] = []

    for dll in QT_DLLS:
        p = QT_BIN / dll
        if p.exists():
            args += ["--add-binary", "%s;." % p]
        else:
            print("  [警告] 未找到 Qt 运行库：%s" % dll)

    for f in QT_RES_FILES:
        p = QT_RES / f
        if p.exists():
            args += ["--add-data", "%s;." % p]
        else:
            print("  [警告] 未找到 Qt 资源：%s" % f)

    if QT_LOC.exists():
        args += ["--add-data", "%s;qtwebengine_locales" % QT_LOC]

    for pl in QT_PLUGIN_DIRS:
        p = QT_PLUGINS / pl
        if p.exists():
            args += ["--add-data", "%s;plugins\\%s" % (p, pl)]

    return args


def clean_old_output():
    """清理旧产物。

    dist 目录可能被资源管理器打开而无法整体删除，因此逐个删除其中的文件、
    保留目录本身；PyInstaller 仍可正常把新 exe 写进去。
    """
    for d in (HERE / "build",):
        shutil.rmtree(str(d), ignore_errors=True)

    dist = HERE / "dist"
    if dist.exists():
        for item in dist.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(str(item), ignore_errors=True)
                else:
                    item.unlink()
            except OSError:
                print("  [警告] 无法删除（可能被占用）：%s" % item.name)

    for spec in HERE.glob("*.spec"):
        try:
            spec.unlink()
        except OSError:
            pass


def main() -> int:
    print("[1/4] 生成图标 ...")
    subprocess.run([PY, str(HERE / "make_icon.py")], check=True)

    print("[2/4] 收集 Qt 运行库 ...")
    extra = collect_extra_args()

    print("[3/4] 清理旧产物 ...")
    clean_old_output()

    print("[4/4] 打包中 ...")
    cmd = [
        PY, "-m", "PyInstaller",
        "--onefile", "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME,
        "--icon", str(HERE / "app.ico"),
        "--add-data", "%s;oil-moisture-monitor" % (ROOT / "oil-moisture-monitor"),
        # EIS→介损/酸值模型权重（纯 Python 推理，见 eis_model.py；换模型只换此文件）
        "--add-data", "%s;." % (HERE / "eis_transformer_weights.json"),
        "--hidden-import", "PyQt5.QtWebEngineWidgets",
        "--hidden-import", "PyQt5.QtWebEngineCore",
        # eis_model 在 eis_loader 内惰性 import，静态分析抓不到，必须显式声明
        "--hidden-import", "eis_model",
        # serial_provision 在方法内惰性 import serial / serial.tools.list_ports；
        # pyserial 的 list_ports 运行时按平台动态加载 list_ports_windows，静态分析抓不到
        "--hidden-import", "serial",
        "--hidden-import", "serial.tools.list_ports",
        "--hidden-import", "serial.tools.list_ports_windows",
        # openpyxl 会 try-import numpy，PyInstaller 静态分析照单全收，
        # 连带 Anaconda MKL 近 230MB（exe 139MB→367MB）；项目只用纯 Python 路径，排除之
        "--exclude-module", "numpy",
        "--exclude-module", "pandas",
        "--exclude-module", "scipy",
        "--exclude-module", "matplotlib",
        # 排除 numpy 后依赖图变化导致 pyexpat.pyd 未被打包，而 pkg_resources
        # 运行时钩子（pyi_rth_pkgres）启动时经 plistlib→xml.parsers.expat 需要它，
        # 缺失即「Unhandled exception in script」启动即崩，必须显式声明
        "--hidden-import", "pyexpat",
        # Anaconda 的 pyexpat.pyd 依赖 Library\bin\libexpat.dll（非标准 DLLs 布局，
        # PyInstaller 依赖追踪抓不到），必须显式打进包，否则 pyexpat 仍 ImportError
        "--add-binary", r"D:\Anaconda3\Library\bin\libexpat.dll;.",
        # 同理：pyserial 的 win32 后端运行时 import ctypes，而 Anaconda 的 _ctypes.pyd
        # 依赖 Library\bin\ffi.dll（打包日志有 'could not resolve ffi.dll' 警告），
        # 缺失即 exe 内「DLL load failed while importing _ctypes」，串口配网全灭
        "--add-binary", r"D:\Anaconda3\Library\bin\ffi.dll;.",
        # pkg_resources 运行时钩子对上述缺失最敏感；应用本身用不到它，直接排除
        "--exclude-module", "pkg_resources",
        # 固定输出/工作/配置目录，避免产物散落到项目根目录
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE),
        *extra,
        str(HERE / "main.py"),
    ]
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print("打包失败，退出码 %d" % rc)
        return rc

    out = HERE / "dist" / ("%s.exe" % APP_NAME)
    print("")
    print("完成 -> %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
