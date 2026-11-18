# -*- coding: utf-8 -*-
"""
纯 Python 推理器（零依赖，连 numpy 都不用）

上位机集成用：exe 打包排除了 numpy/torch，权重导成 JSON 后用它前向推理。
结构必须严格对齐 train.py 的 EisTransformer（PyTorch norm_first=True）：
    x = x + MHA(LN1(x));  x = x + FF(LN2(x));  FF = Linear->GELU(erf)->Linear

61 token x d_model 64 x 2 层，纯 Python 单次前向 < 0.5s，足够（EIS 导入是低频操作）。
"""
import json
import math


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
        p = f"encoder.layers.{i}."
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
