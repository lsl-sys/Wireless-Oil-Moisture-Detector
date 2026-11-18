# -*- coding: utf-8 -*-
"""
轻量 Transformer 训练 + 线性基线对照 + 权重导出 JSON

模型：61 频点 x 5 特征 -> Linear -> 2 层 TransformerEncoder -> mean pool -> MLP -> 3 输出
输出：log10(tand), log10(acid), day/40（标准化后回归）
对照：Ridge 岭回归（郑老师"近似线性"判断的基线，若基线已够好须如实汇报）

用法：D:/anaconda3/python.exe train.py
"""
import json
import math
import os
import time

import numpy as np
import torch
import torch.nn as nn

import sim_data as sd

HERE = os.path.dirname(os.path.abspath(__file__))
D_MODEL = 64
N_HEAD = 4
N_LAYER = 2
D_FF = 128
EPOCHS = 50
BATCH = 128
LR = 1e-3
SEED = 0


class EisTransformer(nn.Module):
    def __init__(self, n_feat=5, d_model=D_MODEL, nhead=N_HEAD,
                 num_layers=N_LAYER, dim_ff=D_FF, n_out=3):
        super().__init__()
        self.proj = nn.Linear(n_feat, d_model)
        self.pos = self._sinusoidal_pe(61, d_model)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=dim_ff,
            activation="gelu", batch_first=True, norm_first=True)
        self.encoder = nn.TransformerEncoder(layer, num_layers=num_layers)
        self.head = nn.Sequential(
            nn.Linear(d_model, 32), nn.GELU(), nn.Linear(32, n_out))

    @staticmethod
    def _sinusoidal_pe(seq_len, d_model):
        pos = torch.arange(seq_len, dtype=torch.float32).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2, dtype=torch.float32)
                        * (-math.log(10000.0) / d_model))
        pe = torch.zeros(seq_len, d_model)
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        return nn.Parameter(pe.unsqueeze(0), requires_grad=False)

    def forward(self, x):
        h = self.proj(x) + self.pos
        h = self.encoder(h)
        return self.head(h.mean(dim=1))


def load_xy():
    with open(os.path.join(HERE, "data", "sim_dataset.json"),
              encoding="utf-8") as fp:
        ds = json.load(fp)
    X = np.stack([sd.featurize(s) for s in ds]).astype(np.float32)
    Y = np.stack([sd.make_targets(s) for s in ds]).astype(np.float32)
    return X, Y


def mae_in_original_units(pred, true):
    """pred/true 为标准化前的 (N,3)：log10tand, log10acid, day/40。"""
    tand_mae = float(np.mean(np.abs(10 ** pred[:, 0] - 10 ** true[:, 0])))
    acid_mae = float(np.mean(np.abs(10 ** pred[:, 1] - 10 ** true[:, 1])))
    day_mae = float(np.mean(np.abs(pred[:, 2] - true[:, 2])) * 40.0)
    return tand_mae, acid_mae, day_mae


def main():
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    torch.set_num_threads(os.cpu_count() or 8)
    X, Y = load_xy()
    n = len(X)
    idx = np.random.permutation(n)
    n_tr, n_va = int(n * 0.8), int(n * 0.9)
    tr, va, te = idx[:n_tr], idx[n_tr:n_va], idx[n_va:]

    y_mean, y_std = Y[tr].mean(0), Y[tr].std(0) + 1e-9
    Yn = (Y - y_mean) / y_std

    # ---------- Transformer ----------
    model = EisTransformer()
    n_params = sum(p.numel() for p in model.parameters())
    opt = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS)
    lossf = nn.MSELoss()
    Xt = torch.from_numpy(X)
    Yt = torch.from_numpy(Yn)

    t0 = time.time()
    for ep in range(EPOCHS):
        model.train()
        perm = torch.randperm(len(tr))
        tot = 0.0
        for i in range(0, len(tr), BATCH):
            b = tr[perm[i:i + BATCH].numpy()]
            opt.zero_grad()
            out = model(Xt[b])
            loss = lossf(out, Yt[b])
            loss.backward()
            opt.step()
            tot += loss.item() * len(b)
        sched.step()
        if (ep + 1) % 5 == 0:
            model.eval()
            with torch.no_grad():
                va_loss = lossf(model(Xt[va]), Yt[va]).item()
            print(f"ep {ep+1:3d}  train {tot/len(tr):.5f}  val {va_loss:.5f}",
                  flush=True)

    model.eval()
    with torch.no_grad():
        pred_te = model(Xt[te]).numpy() * y_std + y_mean
    t_tand, t_acid, t_day = mae_in_original_units(pred_te, Y[te])
    print(f"\n[Transformer] params={n_params}  train_time={time.time()-t0:.0f}s")
    print(f"  test MAE  tand={t_tand:.5f}  acid={t_acid:.5f}  day={t_day:.2f}d")

    # ---------- 线性基线（Ridge） ----------
    from sklearn.linear_model import Ridge
    Xf = X.reshape(n, -1)
    ridge = Ridge(alpha=1.0)
    ridge.fit(Xf[tr], Y[tr])
    pred_r = ridge.predict(Xf[te])
    r_tand, r_acid, r_day = mae_in_original_units(pred_r, Y[te])
    print(f"[Ridge baseline]")
    print(f"  test MAE  tand={r_tand:.5f}  acid={r_acid:.5f}  day={r_day:.2f}d")

    # ---------- 导出权重 JSON（供上位机纯 Python 推理） ----------
    export = {
        "config": {"n_feat": 5, "seq_len": 61, "d_model": D_MODEL,
                   "nhead": N_HEAD, "num_layers": N_LAYER, "dim_ff": D_FF,
                   "n_out": 3},
        "y_mean": y_mean.tolist(), "y_std": y_std.tolist(),
        "state_dict": {k: v.detach().numpy().tolist()
                       for k, v in model.state_dict().items()},
    }
    wpath = os.path.join(HERE, "out", "eis_transformer_weights.json")
    with open(wpath, "w", encoding="utf-8") as fp:
        json.dump(export, fp)
    size_kb = os.path.getsize(wpath) / 1024
    print(f"weights -> {wpath} ({size_kb:.0f} KB)")

    # ---------- 纯 Python 推理一致性校验 ----------
    import infer_pure
    net = infer_pure.PureTransformer(wpath)
    with torch.no_grad():
        ref = model(Xt[te[:5]]).numpy()
    got = np.array([net.forward_norm(X[i].tolist()) for i in te[:5]])
    diff = float(np.max(np.abs(ref - got)))
    print(f"pure-python vs torch max|diff| = {diff:.2e} "
          f"({'PASS' if diff < 1e-4 else 'FAIL'})")

    # ---------- 真实 02/03 谱无标签合理性检查 ----------
    import openpyxl
    for name in ["02(Freq,Zre,Zim,Z,phase).xlsx", "03(Freq,Zre,Zim,Z,phase).xlsx"]:
        wb = openpyxl.load_workbook(
            os.path.join(HERE, "..", "工程管理", "temp", "20261029_eis", name),
            data_only=True)
        rows = [r for r in wb.active.iter_rows(values_only=True)][2:]
        rows = [r for r in rows if r[0] is not None]
        s = {"freq": [r[0] for r in rows], "zre": [r[1] for r in rows],
             "zim": [r[2] for r in rows], "zabs": [r[3] for r in rows],
             "phase": [r[4] for r in rows]}
        x = sd.featurize(s)[None].astype(np.float32)
        with torch.no_grad():
            p = model(torch.from_numpy(x)).numpy()[0] * y_std + y_mean
        print(f"REAL {name[:2]}: pred tand={10**p[0]:.4f} "
              f"acid={10**p[1]:.4f} day={p[2]*40:.1f}")


if __name__ == "__main__":
    main()
