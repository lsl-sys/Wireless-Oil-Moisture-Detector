# -*- coding: utf-8 -*-
"""演示模式数据源：无设备时按 1Hz 生成正弦模拟帧

字段与固件数据帧一致（t/aw/ppm/bat/cur/pwr/lvl/rssi/link/seq/addr/fw/demo），
宿主收到 frameReady 后原样推给页面（pushFrame 唯一实时入口）。
"""

from __future__ import annotations

import math
import random
import time

from PyQt5.QtCore import QObject, QTimer, pyqtSignal


class DemoSource(QObject):
    frameReady = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._seq = 0
        self._t0 = time.time()
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def start(self):
        self._t0 = time.time()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _tick(self):
        if not self._timer.isActive():
            return
        self._seq += 1
        phase = time.time() - self._t0

        t = 27.4 + 0.9 * math.sin(phase / 6.0) + random.uniform(-0.08, 0.08)
        aw = min(0.99, max(0.05, 0.58 + 0.10 * math.sin(phase / 9.0) + random.uniform(-0.012, 0.012)))
        ppm = 30.0 + 18.0 * math.sin(phase / 8.0) + random.uniform(-1.5, 1.5)
        bat = max(18.0, 25.2 - phase * 0.0015)

        self.frameReady.emit({
            "t": round(t, 2),
            "aw": round(aw, 3),
            "ppm": round(ppm, 1),
            "bat": round(bat, 2),
            "lvl": int((bat - 18.0) / (25.2 - 18.0) * 100),
            "rssi": -50 - int(8 * abs(math.sin(phase / 11.0))),
            "link": "WIFI5G",
            "seq": self._seq,
            "addr": 1,
            "cur": round(max(0.05, 0.25 + random.uniform(-0.02, 0.08)), 3),
            "pwr": round(max(0.5, bat * (0.25 + random.uniform(-0.02, 0.08))), 2),
            "fw": "1.0.4",
            "demo": True,
        })
