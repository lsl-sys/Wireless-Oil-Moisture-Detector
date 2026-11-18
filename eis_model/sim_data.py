# -*- coding: utf-8 -*-
"""
EIS 仿真数据生成器（物理自洽版）

物理模型：绝缘油测试池的介电响应 = 体相 R_b||C_g（并联）+ 电极极化 CPE（串联）
    Z(w) = R_b / (1 + j*w*R_b*C_g) + 1 / (Q*(j*w)^n)

老化因果链（关键设计：老化天数同时驱动电路参数与标签，谱↔标签自洽）：
    老化天数 d → 插值郑老师 500kV 老化表得 acid(d)、tand(d)（实验室真值标签）
    d → 体相电阻 R_b(d) 随老化下降（酸性产物电离 -> 电导上升）
    d → 电极极化导纳 Q(d) 随老化上升（极化阻抗下降）
    注：老化表介损为实验室工频测量真值；仿真池的几何常数未知，故不强求
    50Hz 模型介损与表值绝对一致——v0 仿真预训练只需谱形随老化单调漂移、
    且与真实谱（02/03）同量级同形态，绝对值标定留给真实数据微调阶段。

用法：python sim_data.py            # 生成数据集到 data/ 并画 sanity 图到 out/
"""
import json
import math
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 频率网格：与真实仪器导出（02/03 xlsx）完全一致，61 点 10kHz -> 0.01Hz
FREQS = [
    10000, 7943.28, 6309.57, 5011.87, 3981.07, 3162.28, 2511.89, 1995.26,
    1584.89, 1258.93, 1000, 794.328, 630.957, 501.187, 398.107, 316.228,
    251.189, 199.526, 158.489, 125.893, 100, 79.4328, 63.0957, 50.1187,
    39.8107, 31.6228, 25.1189, 19.9526, 15.8489, 12.5893, 10, 7.94328,
    6.30957, 5.01187, 3.98107, 3.16228, 2.51189, 1.99526, 1.58489, 1.25893,
    1, 0.794328, 0.630957, 0.501187, 0.398107, 0.316228, 0.251189, 0.199526,
    0.158489, 0.125893, 0.1, 0.079433, 0.063096, 0.050119, 0.039811,
    0.031623, 0.025119, 0.019953, 0.015849, 0.012589, 0.01,
]

# ---- 郑老师老化表（500kV 配方油+纸+铜片，120C 加速老化）
TABLE_DAY = [0, 3, 7, 12, 18, 25, 32, 40]
TABLE_ACID = [0.02, 0.035, 0.03, 0.045, 0.05, 0.06, 0.1, 0.15]
TABLE_TAND = [0.01857, 0.08892, 0.13869, 0.155, 0.1856, 0.2123, 0.18657, 0.24858]
TABLE_WATER = [241.5, 106.8, 58.4, 88.5, 97, 122, 104, 160.1]

C_G_BASE = 53e-12          # 几何电容，由 02 号谱 10kHz 处 |Z|~=3e5 欧反推 (~53pF)
F_POWER = 50.0             # 介损定义频率（工频 50Hz）


def interp_table(day):
    """老化表线性插值，day 可为 [0,40] 内任意浮点。"""
    acid = float(np.interp(day, TABLE_DAY, TABLE_ACID))
    tand = float(np.interp(day, TABLE_DAY, TABLE_TAND))
    water = float(np.interp(day, TABLE_DAY, TABLE_WATER))
    return acid, tand, water


def params_from_day(day, rng):
    """由老化天数生成等效电路参数（含批次/个体抖动）。"""
    acid, tand, water = interp_table(day)
    # 体相电阻：新油 ~2.8e10 欧（锚定真实 02 谱 0.01Hz 处 |Z|~=2.9e10），
    # 老化 -> 酸性产物电离 -> 电导上升 -> R_b 下降约 65%
    r_b = 2.8e10 * (1.0 - 0.65 * day / 40.0) * rng.lognormal(0.0, 0.06)
    # 几何电容：由 02 谱 10kHz 处 |Z|~=3e5 欧反推 (~53pF)，基本不随老化变
    c_g = C_G_BASE * rng.lognormal(0.0, 0.05)
    # 电极极化 CPE：老化 -> 极化导纳 Q 上升（极化阻抗下降）
    q = 1.5e-9 * (1.0 + 3.0 * day / 40.0) * rng.lognormal(0.0, 0.08)
    n = min(0.98, 0.88 + 0.02 * (day / 40.0) + rng.normal(0.0, 0.01))
    # 标签 = 老化表实验室真值（含测量抖动）
    acid_j = acid * rng.lognormal(0.0, 0.05)
    tand_j = tand * rng.lognormal(0.0, 0.06)
    return dict(r_b=r_b, c_g=c_g, q=q, n=n, acid=acid_j, tand=tand_j,
                day=float(day), water=water)


def z_spectrum(params, freqs=FREQS):
    """按等效电路计算复阻抗谱，返回 (zre, zim, zabs, phase_deg) 四个数组。"""
    w = 2 * math.pi * np.asarray(freqs, dtype=float)
    jw = 1j * w
    z_bulk = params["r_b"] / (1.0 + jw * params["r_b"] * params["c_g"])
    z_cpe = 1.0 / (params["q"] * np.power(jw, params["n"]))
    z = z_bulk + z_cpe
    zre = z.real
    zim = z.imag
    zabs = np.abs(z)
    phase = np.degrees(np.angle(z))
    return zre, zim, zabs, phase


def add_measurement_noise(zre, zim, rng):
    """模拟仪器噪声：小幅相对噪声 + 低频端轻微基线漂移。"""
    n_pts = len(zre)
    rel = np.linspace(0.004, 0.002, n_pts)
    drift = np.linspace(0.0, 0.003, n_pts) * np.abs(zre).mean()
    zre_n = zre * (1 + rng.normal(0, rel)) + rng.normal(0, 1, n_pts) * drift
    zim_n = zim * (1 + rng.normal(0, rel))
    return zre_n, zim_n


def make_sample(day, rng):
    p = params_from_day(day, rng)
    zre, zim, _, _ = z_spectrum(p)
    zre, zim = add_measurement_noise(zre, zim, rng)
    zabs = np.sqrt(zre ** 2 + zim ** 2)
    phase = np.degrees(np.arctan2(zim, zre))
    return {
        "freq": FREQS,
        "zre": zre.tolist(),
        "zim": zim.tolist(),
        "zabs": zabs.tolist(),
        "phase": phase.tolist(),
        "tand": p["tand"],
        "acid": p["acid"],
        "day": p["day"],
    }


def make_dataset(n_samples=4000, seed=42, day_lo=0.0, day_hi=40.0):
    rng = np.random.default_rng(seed)
    days = rng.uniform(day_lo, day_hi, n_samples)
    return [make_sample(d, rng) for d in days]


def signed_log(x):
    """带符号 log10 压缩（Zre 可能为负，跨 12 个数量级）。"""
    x = np.asarray(x, dtype=float)
    return np.sign(x) * np.log10(1.0 + np.abs(x))


def featurize(sample):
    """样本 -> (61, 5) 特征矩阵：log10f, slog(Zre), slog(Zim), log10|Z|, phase/90"""
    f = np.log10(np.asarray(sample["freq"], dtype=float))
    zre = signed_log(sample["zre"])
    zim = signed_log(sample["zim"])
    zabs = np.log10(np.asarray(sample["zabs"], dtype=float))
    ph = np.asarray(sample["phase"], dtype=float) / 90.0
    return np.stack([f, zre, zim, zabs, ph], axis=1)


def make_targets(sample):
    """回归目标：log10(tand), log10(acid), day/40"""
    return np.array([
        math.log10(sample["tand"]),
        math.log10(sample["acid"]),
        sample["day"] / 40.0,
    ], dtype=float)


def main():
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    ds = make_dataset()
    path = os.path.join(HERE, "data", "sim_dataset.json")
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(ds, fp)
    print(f"dataset: {len(ds)} samples -> {path}")

    # sanity：几个代表天数 + 真实 02/03 谱叠画对比
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(7)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for day in [0, 7, 18, 40]:
        s = make_sample(day, rng)
        axes[0].loglog(s["freq"], s["zabs"], label=f"day {day}")
        axes[1].semilogx(s["freq"], s["phase"], label=f"day {day}")

    for name in ["02(Freq,Zre,Zim,Z,phase).xlsx", "03(Freq,Zre,Zim,Z,phase).xlsx"]:
        import openpyxl
        wb = openpyxl.load_workbook(
            os.path.join(HERE, "..", "工程管理", "temp", "20261029_eis", name),
            data_only=True)
        rows = [r for r in wb.active.iter_rows(values_only=True)][2:]
        fr = [r[0] for r in rows if r[0] is not None]
        za = [r[3] for r in rows if r[3] is not None]
        ph = [r[4] for r in rows if r[4] is not None]
        axes[0].loglog(fr, za, "k--" if name.startswith("02") else "r--",
                       label=f"REAL {name[:2]}", linewidth=2)
        axes[1].semilogx(fr, ph, "k--" if name.startswith("02") else "r--",
                         label=f"REAL {name[:2]}", linewidth=2)

    axes[0].set_xlabel("Freq (Hz)"); axes[0].set_ylabel("|Z| (Ohm)")
    axes[0].set_title("Bode magnitude: simulated vs real")
    axes[1].set_xlabel("Freq (Hz)"); axes[1].set_ylabel("phase (deg)")
    axes[1].set_title("Bode phase: simulated vs real")
    for ax in axes:
        ax.legend(fontsize=8); ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    out_png = os.path.join(HERE, "out", "sim_vs_real.png")
    fig.savefig(out_png, dpi=120)
    print(f"sanity plot -> {out_png}")


if __name__ == "__main__":
    main()
