# -*- coding: utf-8 -*-
"""
EIS → 介损 tanδ / 酸值 Transformer 推理（上位机集成版，纯 Python 零依赖）

来源（不要重造，数学与训练侧严格对齐）：
  - 网络结构/前向：eis_model/infer_pure.py 的 PureTransformer（逐行一致，
    与 PyTorch 输出一致性已验证 max|diff|≈1e-6）
  - 特征化：eis_model/sim_data.py 的 featurize() 的纯 Python 等价改写：
    每点 [log10(freq), signed_log(Zre), signed_log(Zim), log10|Z|, phase/90]，
    signed_log(x) = sign(x)*log10(1+|x|)
  - 频率网格/重采样：eis_model/fr3_data.py 的 INSTRUMENT_FREQS /
    resample_to_instrument() 纯 Python 等价（log10|Z| 与相位在 log10(freq)
    上线性插值，端点外推取端点值，再还原 zre/zim）

打包约束：禁止 numpy/torch；权重 JSON 经 build.py --add-data 打包，
运行时经 sys._MEIPASS 定位（PyInstaller onefile）。
更换模型只需替换 eis_transformer_weights.json，本文件不动。
"""

import json
import math
import os
import sys

# 仪器实测网格（61 点，10kHz -> 0.01Hz），与 02/03 导出行序一致
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
_N_GRID = len(INSTRUMENT_FREQS)


# ============================ 纯 Python 前向（同 infer_pure.py） ============================

def _matvec_T(w, x, b):
    """y = x @ w.T + b，w: (out,in) 嵌套列表。"""
    return [sum(wi * xi for wi, xi in zip(row, x)) + bi
            for row, bi in zip(w, b)]


def _linear(w, b, seq):
    return [_matvec_T(w, x, b) for x in seq]


def _layernorm(g, be, seq, eps=1e-5):
    out = []
    for x in seq:
        mu = sum(x) / len(x)
        var = sum((v - mu) ** 2 for v in x) / len(x)
        inv = 1.0 / math.sqrt(var + eps)
        out.append([(v - mu) * inv * gi + bi for v, gi, bi in zip(x, g, be)])
    return out


def _gelu(x):
    return [0.5 * v * (1.0 + math.erf(v / math.sqrt(2.0))) for v in x]


def _add(a, b):
    return [[x + y for x, y in zip(ra, rb)] for ra, rb in zip(a, b)]


class PureTransformer:
    def __init__(self, weights_json_path):
        with open(weights_json_path, encoding="utf-8") as fp:
            w = json.load(fp)
        self.cfg = w["config"]
        self.y_mean = w["y_mean"]
        self.y_std = w["y_std"]
        self.sd = w["state_dict"]
        self.d = self.cfg["d_model"]
        self.nh = self.cfg["nhead"]
        self.hd = self.d // self.nh

    def _mha(self, seq, pfx):
        """多头自注意力，对齐 PyTorch MultiheadAttention（batch_first）。"""
        sd = self.sd
        ipw, ipb = sd[pfx + "in_proj_weight"], sd[pfx + "in_proj_bias"]
        d = self.d
        qkv = [_matvec_T(ipw, x, ipb) for x in seq]
        heads_out = [[] for _ in seq]
        scale = 1.0 / math.sqrt(self.hd)
        for h in range(self.nh):
            sl = slice(h * self.hd, (h + 1) * self.hd)
            Q = [v[sl] for v in (x[0:d] for x in qkv)]
            K = [v[sl] for v in (x[d:2 * d] for x in qkv)]
            V = [v[sl] for v in (x[2 * d:3 * d] for x in qkv)]
            for i in range(len(seq)):
                scores = [sum(qi * ki for qi, ki in zip(Q[i], K[j])) * scale
                          for j in range(len(seq))]
                mx = max(scores)
                ex = [math.exp(s - mx) for s in scores]
                sm = sum(ex)
                att = [e / sm for e in ex]
                ctx = [sum(a * V[j][c] for j, a in enumerate(att))
                       for c in range(self.hd)]
                heads_out[i].extend(ctx)
        return _linear(sd[pfx + "out_proj.weight"], sd[pfx + "out_proj.bias"],
                       heads_out)

    def _encoder_layer(self, seq, i):
        p = "encoder.layers.%d." % i
        h = _layernorm(self.sd[p + "norm1.weight"], self.sd[p + "norm1.bias"], seq)
        seq = _add(seq, self._mha(h, p + "self_attn."))
        h = _layernorm(self.sd[p + "norm2.weight"], self.sd[p + "norm2.bias"], seq)
        h = _linear(self.sd[p + "linear1.weight"], self.sd[p + "linear1.bias"], h)
        h = [_gelu(x) for x in h]
        h = _linear(self.sd[p + "linear2.weight"], self.sd[p + "linear2.bias"], h)
        return _add(seq, h)

    def forward_norm(self, x61x5):
        """返回标准化的 3 维输出。"""
        seq = _linear(self.sd["proj.weight"], self.sd["proj.bias"], x61x5)
        seq = _add(seq, self.sd["pos"][0])
        for i in range(self.cfg["num_layers"]):
            seq = self._encoder_layer(seq, i)
        pooled = [sum(x[c] for x in seq) / len(seq) for c in range(self.d)]
        h = _gelu(_matvec_T(self.sd["head.0.weight"], pooled,
                            self.sd["head.0.bias"]))
        return _matvec_T(self.sd["head.2.weight"], h, self.sd["head.2.bias"])

    def forward(self, x61x5):
        """反标准化，返回 [log10_tand, log10_acid, day/40]。"""
        out = self.forward_norm(x61x5)
        return [o * s + m for o, m, s in zip(out, self.y_mean, self.y_std)]

    def predict(self, x61x5):
        """返回工程单位 dict：tand / acid / day。"""
        p = self.forward(x61x5)
        return {"tand": 10 ** p[0], "acid": 10 ** p[1], "day": p[2] * 40.0}


# ============================ 特征化 / 重采样（纯 Python 等价） ============================

def _signed_log(x):
    return math.copysign(math.log10(1.0 + abs(x)), x) if x else 0.0


def _featurize(freq, zre, zim, zabs, phase):
    """样本 -> (N,5)：log10f, slog(Zre), slog(Zim), log10|Z|, phase/90。
    与 sim_data.featurize() 逐元素数学相同（numpy -> 纯 Python）。"""
    rows = []
    for i in range(len(freq)):
        za = zabs[i] if zabs[i] > 0 else 1e-30
        rows.append([math.log10(freq[i]), _signed_log(zre[i]),
                     _signed_log(zim[i]), math.log10(za), phase[i] / 90.0])
    return rows


def _on_instrument_grid(freq):
    """是否恰为仪器 61 点网格（相对偏差 1% 容差）。"""
    if len(freq) != _N_GRID:
        return False
    for f, g in zip(freq, INSTRUMENT_FREQS):
        if abs(f - g) > abs(g) * 1e-2:
            return False
    return True


def _interp_clamped(x, xp, fp):
    """一维线性插值，端点外取端点值（同 numpy.interp 默认行为）。"""
    if x <= xp[0]:
        return fp[0]
    if x >= xp[-1]:
        return fp[-1]
    lo, hi = 0, len(xp) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xp[mid] <= x:
            lo = mid
        else:
            hi = mid
    t = (x - xp[lo]) / (xp[hi] - xp[lo])
    return fp[lo] + t * (fp[hi] - fp[lo])


def _resample_to_instrument(freq, zabs, phase):
    """任意网格谱 -> 仪器 61 点网格（log10|Z| 与相位在 log10(freq) 上线性插值）。
    与 fr3_data.resample_to_instrument() 数学等价。要求 freq 升序排列。"""
    lg_src = [math.log10(f) for f in freq]
    la_src = [math.log10(z if z > 0 else 1e-30) for z in zabs]
    out_zre, out_zim, out_zabs, out_ph = [], [], [], []
    for f in INSTRUMENT_FREQS:
        x = math.log10(f)
        la = _interp_clamped(x, lg_src, la_src)
        ph = _interp_clamped(x, lg_src, phase)
        za = 10 ** la
        rad = math.radians(ph)
        out_zabs.append(za)
        out_ph.append(ph)
        out_zre.append(za * math.cos(rad))
        out_zim.append(za * math.sin(rad))
    return out_zre, out_zim, out_zabs, out_ph


# ============================ 对外入口 ============================

_MODEL = None


def _weights_path():
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "eis_transformer_weights.json")


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = PureTransformer(_weights_path())
    return _MODEL


def evaluate_set(spec):
    """对一条谱推理，返回 {"tand","acid","day"}；失败返回 None。

    spec 字段：freq/zre/zim/zabs/phase 四个等长数组（zabs 即载荷里的 z）。
    非仪器网格（如 113 点原始导出）先重采样到 61 点。
    """
    freq = list(spec["freq"])
    zre = list(spec["zre"])
    zim = list(spec["zim"])
    zabs = list(spec["zabs"])
    phase = list(spec["phase"])
    if len(freq) < 5:
        return None
    if not _on_instrument_grid(freq):
        order = sorted(range(len(freq)), key=lambda k: freq[k])
        freq = [freq[k] for k in order]
        zabs = [zabs[k] for k in order]
        phase = [phase[k] for k in order]
        zre, zim, zabs, phase = _resample_to_instrument(freq, zabs, phase)
        # 重采样后数据点已换到仪器网格，频率数组必须同步替换；
        # 否则 _featurize 按原长（如 71 点）索引 61 点的数组会 IndexError
        freq = list(INSTRUMENT_FREQS)
    x = _featurize(freq, zre, zim, zabs, phase)
    return _model().predict(x)


def fill_payload(payload):
    """对 payload 里每一条谱分别推理，就地填 payload["per_set"] 与汇总字段。

    payload["per_set"] = [{"name","tand","acid","day"}]（推理失败的组跳过）
    payload["tand"/"acid"/"day"] = 各组均值，供卡片显示
    前端 applyEIS 读 payload.eval || payload；单组失败不影响其余组。
    """
    sets = payload.get("sets") or []
    if not sets:
        return payload
    per = []
    for s in sets:
        try:
            ev = evaluate_set({"freq": s["freq"], "zre": s["zre"], "zim": s["zim"],
                               "zabs": s["z"], "phase": s["phase"]})
        except Exception:
            ev = None
        if ev:
            per.append({"name": s.get("name", ""), "tand": ev["tand"],
                        "acid": ev["acid"], "day": ev["day"]})
    if not per:
        return payload
    n = float(len(per))
    payload["per_set"] = per
    payload["tand"] = sum(p["tand"] for p in per) / n
    payload["acid"] = sum(p["acid"] for p in per) / n
    payload["day"] = sum(p["day"] for p in per) / n
    return payload
