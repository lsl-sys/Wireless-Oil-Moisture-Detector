# -*- coding: utf-8 -*-
"""CSV 实时落盘

表头与行构造同源（CSV_HEADER + build_csv_row），避免"表头改了、行没改"列错位。
CsvStore 负责文件句柄、记录号自增、按时长切分、快照导出。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

CSV_HEADER = (
    "记录号,时间,温度(℃),水活性(aw),含水量(ppm),电池(V),电流(A),功率(W),"
    "链路,固件,传感器状态,数据分类,设备序号\n"
)


def build_csv_row(rec_no: int, d: dict, now=None) -> str:
    """把一帧 JSON 转成 CSV 行。列顺序严格对齐 CSV_HEADER（13 列）。

    - 记录号：本机自增，跨文件连续（设备 seq 会 255→0 回绕，不能当编号）
    - 时间：yyyy-MM-dd HH:mm:ss.SSS，带毫秒，2Hz 同秒多条也能稳定排序
    - 传感器状态/数据分类：文本化（后端无告警阈值，分类按链路判定）
    """
    if now is None:
        now = time.time()
    ms = int(now * 1000) % 1000
    ts = "%s.%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), ms)
    seq = d.get("seq", "")
    temp = f"{d.get('t', 0):.1f}"
    aw = f"{d.get('aw', 0):.3f}"
    ppm = f"{d.get('ppm', 0):.1f}"
    bat = f"{d.get('bat', 0):.2f}"
    cur = f"{d.get('cur', d.get('bat_i', 0)):.3f}"
    pwr = f"{d.get('pwr', 0):.2f}"
    link = d.get("link", "")
    fw = d.get("fw", "")
    s_ok = d.get("s_ok", "")   # 1=正常 0=失联（旧固件无此字段则留空）
    if s_ok in (1, "1"):
        s_txt, cls = "正常", "正常"
    elif s_ok in (0, "0"):
        s_txt, cls = "失联", "失联"
    else:
        s_txt, cls = "", ""
    return (f"{rec_no},{ts},{temp},{aw},{ppm},{bat},{cur},{pwr},"
            f"{link},{fw},{s_txt},{cls},{seq}\n")


class CsvStore:
    """实时 CSV 追加器（单句柄，每条 flush，崩溃不丢数据）"""

    def __init__(self, cfg: dict, out_dir: Path):
        self.cfg = cfg            # 直接引用宿主配置（csv_split_minutes 可热改）
        self.out_dir = Path(out_dir)
        self._file = None
        self._path = None
        self._start_ts = 0.0
        self._rec_no = 0

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def file(self):
        return self._file

    def new_file(self):
        """开新 CSV 文件（自动创建目录 + 写表头）"""
        self.close_file()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self._path = self.out_dir / f"oil_{ts}.csv"
        self._file = open(self._path, "a", encoding="utf-8-sig")
        self._file.write(CSV_HEADER)
        self._start_ts = time.time()

    def close_file(self):
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def append_line(self, line) -> None:
        """解析 JSON 帧 → 转 CSV 行 → 检查切分 → 写文件（坏帧静默跳过）"""
        split_minutes = max(0, int(self.cfg.get("csv_split_minutes", 0)))
        try:
            d = json.loads(line) if isinstance(line, str) else line
        except Exception:
            return
        if not isinstance(d, dict):
            return

        if self._file is None:
            self.new_file()

        if split_minutes > 0 and self._start_ts > 0:
            if time.time() - self._start_ts > split_minutes * 60:
                self.new_file()

        self._rec_no += 1
        try:
            self._file.write(build_csv_row(self._rec_no, d))
            self._file.flush()  # 每条都 flush，防止程序崩溃丢数据
        except Exception:
            pass

    def export_snapshot(self) -> dict:
        """把当前实时 CSV 复制成快照（活文件继续追加），返回 {path, rows}"""
        if not self._path or not self._path.exists():
            raise OSError("no csv data yet")
        if self._file:
            self._file.flush()
        dst = self._path.with_name(self._path.stem + "_export_%s.csv" % time.strftime("%H%M%S"))
        shutil.copyfile(self._path, dst)
        rows = sum(1 for _ in open(dst, encoding="utf-8-sig")) - 1
        return {"path": str(dst), "rows": max(0, rows)}

    def close(self):
        self.close_file()
