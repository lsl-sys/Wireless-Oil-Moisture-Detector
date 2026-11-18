# -*- coding: utf-8 -*-
"""
FR3 数据管线：老师的阻抗谱数据 -> 仪器网格 -> 物理增强训练集

数据源：工程管理/参考资料/11-4/fr3_130C_60ppm_predicted_noisy_eis_data.xlsx
  - EIS_data: 8 个老化天数 x 113 频点 (100kHz -> 0.01Hz)
  - Model_parameters: 每天一组 Cole-Cole 参数 (R, C, alpha)
    已验证: Z = R/(1+(jwRC)^alpha)，corner 频率与表内一致，偏差即噪声

增强原理：以 8 组老师参数为锚点，按天数插值 R/C/alpha + 合理抖动，
用 Cole-Cole 公式在仪器实际网格（61 点, 10kHz->0.01Hz）上生成谱，
再叠加三类仪器噪声（低频漂移 / 相对噪声 / 高频夹具伪影）。

标签链：老化天数 d -> 500kV 老化表插值 -> (tand, acid)；模型同时回归三者。
"""
import math
import os

import numpy as np
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.path.join(HERE, "..", "工程管理", "参考资料", "11-4",
                    "fr3_130C_60ppm_predicted_noisy_eis_data.xlsx")

# 仪器实测网格（61 点，10kHz -> 0.01Hz），与原 02/03 导出格式一致
INSTRUMENT_FREQS = [
    10000, 7943.28, 6309.57, 5011.87, 3981.07, 3162.28, 2511.89, 1995.26,
    1584.89, 1258.93, 1000, 794.328, 630.957, 501.187, 398.107, 316.228,
    251.189, 199.526, 158.489, 125.893, 100, 79.4328, 63.0957, 50.1187,
    39.8107, 31.6228, 25.1189, 19.9526, 15.8489, 12.5893, 10, 7.94328,
    6.30957, 5.01187, 3.98107, 3.16228, 2.51189, 1.99526, 1.58489, 1.25893,
    1, 0.794328, 0.630957, 0.501187, 0.398107, 0.316228, 0.251189, 0.199526,
    0.158489, 0.125893, 0.1, 0.079433, 0.063096, 0.050119, 0.039811,
    0.031623, 0.025119, 0.019953, 0.015849, 0.012589, 0.01,
]

# 500kV 老化表（郑老师）：天数 -> 酸值/介损/水分
TABLE_DAY = [0, 3, 7, 12, 18, 25, 32, 40]
TABLE_ACID = [0.02, 0.035, 0.03, 0.045, 0.05, 0.06, 0.1, 0.15]
TABLE_TAND = [0.01857, 0.08892, 0.13869, 0.155, 0.1856, 0.2123, 0.18657, 0.24858]
TABLE_WATER = [241.5, 106.8, 58.4, 88.5, 97, 122, 104, 160.1]


def load_fr3():
    """读 xlsx，返回 (anchors, spectra)：
    anchors = {day: (R, C, alpha)}
    spectra = {day: dict(freq, zre, zim, zabs, phase)}（原始 113 点）
    """
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    anchors = {}
    for r in list(wb["Model_parameters"].iter_rows(values_only=True))[1:]:
        if r[0] is None:
            continue
        anchors[int(float(r[0]))] = (float(r[3]), float(r[5]), float(r[7]))
    spectra = {}
    for r in list(wb["EIS_data"].iter_rows(values_only=True))[1:]:
        if r[0] is None:
            continue
        day = int(float(r[0]))
        spectra.setdefault(day, {"freq": [], "zre": [], "zim": []})
        spectra[day]["freq"].append(float(r[3]))
        spectra[day]["zre"].append(float(r[4]))
        spectra[day]["zim"].append(float(r[5]))
    for d in spectra:
        s = spectra[d]
        s["zabs"] = [math.hypot(a, b) for a, b in zip(s["zre"], s["zim"])]
        s["phase"] = [math.degrees(math.atan2(b, a))
                      for a, b in zip(s["zre"], s["zim"])]
    return anchors, spectra


def params_at_day(day, anchors, rng=None, jitter=True):
    """按天数插值 R/C/alpha（R 用 log 插值），可加个体抖动。"""
    days = sorted(anchors)
    Rs = [math.log10(anchors[d][0]) for d in days]
    Cs = [anchors[d][1] for d in days]
    As = [anchors[d][2] for d in days]
    R = 10 ** float(np.interp(day, days, Rs))
    C = float(np.interp(day, days, Cs))
    A = float(np.interp(day, days, As))
    if jitter and rng is not None:
        R *= rng.lognormal(0.0, 0.10)
        C *= rng.lognormal(0.0, 0.03)
        A = min(0.99, max(0.80, A + rng.normal(0.0, 0.012)))
    return R, C, A


def cole_cole(R, C, alpha, freqs):
    """Z = R/(1+(jwRC)^alpha)，返回 (zre, zim) numpy 数组。"""
    w = 2 * math.pi * np.asarray(freqs, dtype=float)
    Z = R / (1.0 + np.power(1j * w * R * C, alpha))
    return Z.real, Z.imag


def add_noise(zre, zim, freqs, rng):
    """三类噪声：低频漂移 + 全程相对噪声 + 高频夹具伪影。"""
    f = np.asarray(freqs, dtype=float)
    zre, zim = np.asarray(zre), np.asarray(zim)
    zabs = np.hypot(zre, zim)
    # 全程相对噪声（对标原数据 ~2.6% 中位偏差）
    rel = rng.uniform(0.008, 0.02)
    zre = zre * (1 + rng.normal(0, rel, len(f)))
    zim = zim * (1 + rng.normal(0, rel, len(f)))
    # 低频漂移（<0.1Hz 渐强）
    w_lo = np.clip(np.log10(0.1 / f) / 2.0, 0, 1)
    zre = zre + w_lo * rng.normal(0, 0.01) * zabs
    # 高频夹具伪影（>1kHz 相位抖动）
    w_hi = np.clip(np.log10(f / 1000.0), 0, 1)
    zim = zim + w_hi * rng.normal(0, 0.01) * zabs
    return zre, zim


def make_fr3_sample(day, anchors, rng):
    """生成一条仪器网格上的增强谱，带 (tand, acid, day) 标签。"""
    R, C, A = params_at_day(day, anchors, rng)
    zre, zim = cole_cole(R, C, A, INSTRUMENT_FREQS)
    zre, zim = add_noise(zre, zim, INSTRUMENT_FREQS, rng)
    zabs = np.hypot(zre, zim)
    phase = np.degrees(np.arctan2(zim, zre))
    return {
        "freq": INSTRUMENT_FREQS,
        "zre": zre.tolist(), "zim": zim.tolist(),
        "zabs": zabs.tolist(), "phase": phase.tolist(),
        "tand": float(np.interp(day, TABLE_DAY, TABLE_TAND)),
        "acid": float(np.interp(day, TABLE_DAY, TABLE_ACID)),
        "day": float(day),
    }


def make_fr3_dataset(anchors, n_samples=4000, seed=42):
    rng = np.random.default_rng(seed)
    days = rng.uniform(0.0, 40.0, n_samples)
    return [make_fr3_sample(d, anchors, rng) for d in days]


def resample_to_instrument(spec):
    """把原始 113 点谱重采样到仪器 61 点网格（log|Z| + 相位线性插值）。"""
    f_src = np.asarray(spec["freq"], dtype=float)
    order = np.argsort(f_src)
    f_src = f_src[order]
    la = np.log10(np.asarray(spec["zabs"], dtype=float))[order]
    ph = np.asarray(spec["phase"], dtype=float)[order]
    f_dst = np.asarray(INSTRUMENT_FREQS, dtype=float)
    la_i = np.interp(np.log10(f_dst), np.log10(f_src), la)
    ph_i = np.interp(np.log10(f_dst), np.log10(f_src), ph)
    zabs = 10 ** la_i
    rad = np.radians(ph_i)
    return {
        "freq": INSTRUMENT_FREQS,
        "zre": (zabs * np.cos(rad)).tolist(),
        "zim": (zabs * np.sin(rad)).tolist(),
        "zabs": zabs.tolist(),
        "phase": ph_i.tolist(),
    }
