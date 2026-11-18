# -*- coding: utf-8 -*-
"""阻抗谱（EIS）数据导入：读取文件夹下所有 .xlsx，解析 Freq/Zre/Zim/Z/phase

仪器导出的 xlsx 约定：第 1 行是标题（如 Segment1(控制电位EIS)），第 2 行才是表头。
非阻抗谱文件静默跳过并计数（界面只提示跳过数量）。
"""

from __future__ import annotations

import math
from pathlib import Path


def _match_eis_columns(header):
    """模糊匹配 EIS 表头（按列索引），返回 {key: 列下标}；缺 freq/zre/zim 任一返回 None"""
    want = {"freq": None, "zre": None, "zim": None, "z": None, "phase": None}
    for i, c in enumerate(header or ()):
        s = str(c).strip().lower() if c is not None else ""
        if not s or s.startswith("unnamed"):
            continue
        if "freq" in s:
            want["freq"] = i
        elif "zre" in s or "z'" in s:
            want["zre"] = i
        elif "zim" in s or 'z"' in s or "z''" in s:
            want["zim"] = i
        elif "phase" in s:
            want["phase"] = i
        elif s.startswith("z") and want["z"] is None:
            want["z"] = i
    if want["freq"] is None or want["zre"] is None or want["zim"] is None:
        return None
    return want


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def load_eis_dir(dir_path) -> dict:
    """解析文件夹下全部阻抗谱 xlsx（非阻抗谱文件静默跳过并计数）。

    用 openpyxl 而不是 pandas：pandas 会把 numpy+MKL 一起打进 exe（139MB→910MB），
    openpyxl 是纯 Python 库，打包体积几乎不变。

    返回推给页面的 payload：
      {"sets":[{"name","freq","zre","zim","z","phase"}], "skipped":N, "dir":..., "count":N}
    """
    from openpyxl import load_workbook  # 惰性导入，避免拖慢启动
    d = Path(dir_path)
    if not d.is_dir():
        raise OSError("文件夹不存在：%s" % dir_path)
    sets, skipped = [], 0
    for f in sorted(d.glob("*.xlsx")):
        if f.name.startswith("~$"):  # Excel 临时锁文件
            continue
        try:
            # 不用 read_only：仪器软件导出的 xlsx 维度标记不规范，只读模式会丢行
            wb = load_workbook(f, data_only=True)
            try:
                ws = wb.worksheets[0]
                it = ws.iter_rows(values_only=True)
                next(it, None)                    # 第 1 行：仪器标题
                header = next(it, None)           # 第 2 行：表头
                cols = _match_eis_columns(header)
                if cols is None:
                    skipped += 1
                    continue
                fr, zr, zi, zl, ph = [], [], [], [], []
                for row in it:
                    fv = _to_float(row[cols["freq"]] if cols["freq"] < len(row) else None)
                    rv = _to_float(row[cols["zre"]] if cols["zre"] < len(row) else None)
                    iv = _to_float(row[cols["zim"]] if cols["zim"] < len(row) else None)
                    if not (fv == fv and rv == rv and iv == iv) or fv <= 0:
                        continue
                    zv = (_to_float(row[cols["z"]]) if cols["z"] is not None and cols["z"] < len(row)
                          else float("nan"))
                    pv = (_to_float(row[cols["phase"]]) if cols["phase"] is not None and cols["phase"] < len(row)
                          else float("nan"))
                    fr.append(fv); zr.append(rv); zi.append(iv); zl.append(zv); ph.append(pv)
            finally:
                wb.close()
        except Exception:
            skipped += 1
            continue
        if len(fr) < 5:
            skipped += 1
            continue
        out_z = [round(zl[k], 6) if zl[k] == zl[k] else round(math.hypot(zr[k], zi[k]), 6)
                 for k in range(len(fr))]
        out_ph = [round(ph[k], 4) if ph[k] == ph[k]
                  else round(math.degrees(math.atan2(zi[k], zr[k])), 4)
                  for k in range(len(fr))]
        sets.append({"name": f.stem, "freq": fr, "zre": zr, "zim": zi,
                     "z": out_z, "phase": out_ph})
    payload = {"sets": sets, "skipped": skipped, "dir": str(d), "count": len(sets)}
    # 模型推理：对第一个 set 反演介损 tanδ/酸值，就地填 payload["tand"]/["acid"]
    # （前端 applyEIS 读 payload.eval || payload，卡片自动出数）；
    # 惰性导入避免拖慢启动，推理失败不影响谱线显示
    if sets:
        try:
            import eis_model
            eis_model.fill_payload(payload)
        except Exception:
            pass
    return payload
