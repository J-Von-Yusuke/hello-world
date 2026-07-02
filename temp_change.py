# -*- coding: utf-8 -*-
"""
temp_change.py — Region世代CSV群を自動発見し「温度変化」を描画する。

入力: --dir 内の *.csv を自動走査。ファイル名で分類:
    <トレース名>__<構成名>.csv    例: tencent__nosplit.csv, tencent__p50_split.csv
    ("__" が無ければトレース名 "all" として1グループ扱い)

C計測の列(実装済みスキーマ):
    seq,is_last,slot,pool,born_vt,seal_vt,evict_vt,survived,n,n_never_in,
    sum_y_in,sum_sq_y_in,n_never_res,sum_y_res,sum_sq_y_res,RUsize,byte,
    bsum_in,bsum_res,bsy_in,bsy_res,reacc_n,reacc_b

規約(C側と一致させること):
    is_last=1 は計測終了時に取った行(evict_vt=-1, res系列=0 → res分析から除外)
    is_last=0 は evict による行(res系列が有効)
    ICC/温度推移は封緘済み全行(is_last問わず)で計算 — 削除行だけだと偏るため

定義:
    温度B(挿入)   T_in  = sum_y_in  / (n - n_never_in)    小さいほど熱い
    温度B(残余)   T_res = sum_y_res / (n - n_never_res)   削除の損害(is_last=0のみ)
    削除精度      prec  = n_never_res / n                  二度と来ない物の割合
    封緘されていない行(seal_vt<=0/NaN)は全図から除外

出力 (--out 既定 analysis/fig/temp_change/):
    <トレース>_temp_timeline.png : T_in の時間推移(構成別 中央値+IQR帯 / プール別)
    <トレース>_res_timeline.png  : 削除損害と削除精度の時間推移(res列がある場合)
    <トレース>_icc.png           : 構成別 ICC_B(全体+プール内)
    summary.json

    python analysis/temp_change.py --dir analysis/region_csv
    python analysis/temp_change.py --demo     # 合成CSVを生成して動作確認
"""
import os, json, glob, argparse, time
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

for _jp in ("Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP"):
    if _jp in {f.name for f in font_manager.fontManager.ttflist}:
        plt.rcParams["font.family"] = _jp; break
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})

LABEL = {"nosplit": "無分割", "wrong_split": "誤った分割",
         "p50_split": "p50分割", "optimal_split": "最適分割"}
COLOR = {"nosplit": "#B4B2A9", "wrong_split": "#E24B4A",
         "p50_split": "#EF9F27", "optimal_split": "#1D9E75"}
ORDER = ["nosplit", "wrong_split", "p50_split", "optimal_split"]
FALLBACK = ["#378ADD", "#7F77DD", "#D4537E", "#639922"]
NBIN = 28          # 時間ビン数
MINREG = 4         # ビン内の最少Region数(未満はNaN)


def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def order_key(c): return (ORDER.index(c), c) if c in ORDER else (len(ORDER), c)


def color_of(c, i): return COLOR.get(c, FALLBACK[i % len(FALLBACK)])


def discover(d):
    """dir内のcsvを {トレース: {構成: path}} に分類(ファイル名 <trace>__<config>.csv)。"""
    groups = {}
    for p in sorted(glob.glob(os.path.join(d, "*.csv"))):
        stem = os.path.splitext(os.path.basename(p))[0]
        tr, _, cfg = stem.partition("__")
        if not cfg: tr, cfg = "all", stem
        groups.setdefault(tr, {})[cfg] = p
    return groups


def load(path):
    r = np.atleast_1d(np.genfromtxt(path, delimiter=",", names=True))
    m = np.isfinite(r["seal_vt"]) & (r["seal_vt"] > 0)          # 封緘済みのみ
    return r[m], set(r.dtype.names)


def temp_of(r, kind):
    ne = r["n"] - r[f"n_never_{kind}"]
    T = np.full(len(r), np.nan)
    ok = ne > 0
    T[ok] = r[f"sum_y_{kind}"][ok] / ne[ok]
    return T


def icc_of(r):
    """ICC_B = 級間/全分散(y_in)。sum_sq が無ければ NaN。"""
    ne = r["n"] - r["n_never_in"]
    ok = ne > 0
    ne, s, q = ne[ok], r["sum_y_in"][ok], r["sum_sq_y_in"][ok]
    N, S, Q = ne.sum(), s.sum(), q.sum()
    tot = Q - S * S / N
    return float(((s ** 2 / ne).sum() - S * S / N) / tot) if tot > 0 else np.nan


def timeline(x, v, edges, stat="median"):
    """時間ビンごとの (代表値, p25, p75)。二峰分布で中央値が飛ぶ場合は stat="mean"。"""
    ctr = np.full(len(edges) - 1, np.nan); lo = ctr.copy(); hi = ctr.copy()
    idx = np.digitize(x, edges) - 1
    for b in range(len(edges) - 1):
        seg = v[(idx == b) & np.isfinite(v)]
        if len(seg) >= MINREG:
            lo[b], hi[b] = np.percentile(seg, (25, 75))
            ctr[b] = np.mean(seg) if stat == "mean" else np.median(seg)
    return ctr, lo, hi


def icc_timeline(r, edges, min_reg=8):
    """時間ビンごとのICC_B(そのビンで封緘された世代のみで級間/全分散)。"""
    out = np.full(len(edges) - 1, np.nan)
    idx = np.digitize(r["seal_vt"], edges) - 1
    for b in range(len(edges) - 1):
        seg = r[idx == b]
        if len(seg) >= min_reg: out[b] = icc_of(seg)
    return out


def plot_temp(trace, data, outdir, summary):
    """左=時間窓ごとのICC_B(局所性の推移), 右=プール別温度B(実線/破線)。"""
    vmax = max(d[0]["seal_vt"].max() for d in data.values())
    edges = np.linspace(0, vmax * 1.001, NBIN + 1); cx = (edges[:-1] + edges[1:]) / 2
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.4))
    for i, cfg in enumerate(sorted(data, key=order_key)):
        r, cols = data[cfg]; c = color_of(cfg, i); lab = LABEL.get(cfg, cfg)
        T = temp_of(r, "in")
        if "sum_sq_y_in" in cols:
            axs[0].plot(cx, icc_timeline(r, edges), color=c, lw=2, label=lab)
        for pool, ls in ((0, "-"), (1, "--")):
            m = r["pool"] == pool
            if m.sum() < MINREG: continue
            med, _, _ = timeline(r["seal_vt"][m], T[m], edges)
            axs[1].plot(cx, med, color=c, lw=1.8, ls=ls,
                        label=f"{lab} pool{pool}")
        summary[trace][cfg]["temp_in_median"] = float(np.nanmedian(T))
    axs[0].set_title("局所性 ICC_B の時間推移(封緘窓ごと)")
    axs[0].set_ylabel("ICC_B(↑ほどRegion内が均質)"); axs[0].set_ylim(0, 1)
    axs[1].set_title("プール別 温度B(実線=pool0小物, 破線=pool1大物)")
    axs[1].set_ylabel("温度B = mean ln(gap)  (小さいほど熱い)")
    for ax in axs:
        ax.set_xlabel("封緘時刻 seal_vt"); ax.legend(fontsize=9)
    fig.suptitle(f"{trace}: 温度局所性の時間変化", y=1.0)
    fig.tight_layout(); fig.savefig(f"{outdir}/{trace}_temp_timeline.png", dpi=130)
    plt.close(fig)


def dead_mask(r, cols):
    """evictで書かれた行(res系列が有効)。is_last=1(終了時取得, evict_vt=-1)を除外。"""
    m = np.isfinite(r["evict_vt"]) & (r["evict_vt"] > 0)
    if "is_last" in cols: m &= r["is_last"] == 0
    if "survived" in cols: m &= r["survived"] == 0
    return m


def plot_res(trace, data, outdir, summary):
    """削除損害 T_res(削除された世代のみ, x=evict_vt) と削除精度の推移。"""
    have = {c: d for c, d in data.items()
            if {"sum_y_res", "n_never_res", "evict_vt"} <= d[1]}
    if not have: return
    vm = [d[0]["evict_vt"][dead_mask(*d)].max() for d in have.values()
          if dead_mask(*d).any()]
    if not vm: return
    vmax = max(vm)
    edges = np.linspace(0, vmax * 1.001, NBIN + 1); cx = (edges[:-1] + edges[1:]) / 2
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.4))
    for i, cfg in enumerate(sorted(have, key=order_key)):
        r, cols = have[cfg]; c = color_of(cfg, i); lab = LABEL.get(cfg, cfg)
        rd = r[dead_mask(r, cols)]
        if len(rd) < MINREG: continue
        T = temp_of(rd, "res")                       # 大きいほど損害小
        med, lo, hi = timeline(rd["evict_vt"], T, edges, stat="mean")
        axs[0].fill_between(cx, lo, hi, color=c, alpha=0.18)
        axs[0].plot(cx, med, color=c, lw=2, label=lab)
        prec = rd["n_never_res"] / rd["n"]
        med, _, _ = timeline(rd["evict_vt"], prec, edges, stat="mean")
        axs[1].plot(cx, med, color=c, lw=2, label=lab)
        summary[trace][cfg]["res_median"] = float(np.nanmedian(T))
        summary[trace][cfg]["precision_median"] = float(np.nanmedian(prec))
    axs[0].set_title("削除損害: 残余温度 T_res の推移 — 高いほど正しい削除")
    axs[0].set_ylabel("T_res = mean ln(残余gap)"); axs[0].set_xlabel("削除時刻 evict_vt")
    axs[1].set_title("削除精度: 二度と来ない物の割合")
    axs[1].set_ylabel("n_never_res / n"); axs[1].set_xlabel("削除時刻 evict_vt")
    axs[1].set_ylim(0, 1)
    for ax in axs: ax.legend(fontsize=9)
    fig.suptitle(f"{trace}: 削除された世代の質", y=1.0)
    fig.tight_layout(); fig.savefig(f"{outdir}/{trace}_res_timeline.png", dpi=130)
    plt.close(fig)


def plot_icc(trace, data, outdir, summary):
    have = {c: d for c, d in data.items() if "sum_sq_y_in" in d[1]}
    if not have: return
    cfgs = sorted(have, key=order_key)
    fig, ax = plt.subplots(figsize=(1.6 + 1.5 * len(cfgs), 4.2))
    for i, cfg in enumerate(cfgs):
        r, _ = have[cfg]; c = color_of(cfg, i)
        v = icc_of(r)
        ax.bar(i, v, 0.42, color=c, edgecolor="#333")
        for pool, dx in ((0, 0.26), (1, 0.42)):
            m = r["pool"] == pool
            if m.sum() >= MINREG:
                ax.bar(i + dx, icc_of(r[m]), 0.14, color=c, alpha=0.45, edgecolor="#666")
        summary[trace][cfg]["icc"] = v
    ax.set_xticks(range(len(cfgs)))
    ax.set_xticklabels([LABEL.get(c, c) for c in cfgs], fontsize=10)
    ax.set_ylabel("ICC_B(太=全体, 細=プール内)")
    ax.set_title(f"{trace}: 局所性 ICC_B — プール間分離は太バーにのみ含まれる")
    fig.tight_layout(); fig.savefig(f"{outdir}/{trace}_icc.png", dpi=130)
    plt.close(fig)


# ---------------- 合成デモ(--demo): C計測と同一スキーマのCSVを模擬 ----------------
HDR = ("seq,is_last,slot,pool,born_vt,seal_vt,evict_vt,survived,n,n_never_in,"
       "sum_y_in,sum_sq_y_in,n_never_res,sum_y_res,sum_sq_y_res,RUsize,byte,"
       "bsum_in,bsum_res,bsy_in,bsy_res,reacc_n,reacc_b\n")


def make_demo(d):
    os.makedirs(d, exist_ok=True); rng = np.random.default_rng(7)
    VMAX, OBJ, RU = 6e6, 4096, 1 << 22
    for cfg, purity in (("nosplit", 0.0), ("wrong_split", 0.3),
                        ("p50_split", 0.8), ("optimal_split", 0.95)):
        R = 900; seal = np.sort(rng.uniform(2e4, VMAX, R))
        with open(os.path.join(d, f"demo__{cfg}.csv"), "w", encoding="utf-8") as f:
            f.write(HDR)
            for g in range(R):
                drift = 1.2 * np.sin(2 * np.pi * seal[g] / 2.5e6)
                ext = 0.97 if rng.random() < 0.6 else 0.03      # 純化時のホット率
                h = purity * ext + (1 - purity) * 0.7           # このRegionのホット率
                pool = 0 if (cfg != "nosplit" and h >= 0.5) else (1 if cfg != "nosplit" else 0)
                n = int(rng.integers(300, 600))
                nv_in = rng.binomial(n, 0.03 + 0.3 * (1 - h)); ne = n - nv_in
                nh = int(round(ne * h))
                y = np.concatenate([rng.normal(3 + drift, 0.7, nh),
                                    rng.normal(12 + 0.3 * drift, 0.9, ne - nh)])
                # 寿命はホット率に連続依存(LRU: 熱いRegionほど長生き)
                life = 10 ** (4.6 + 2.2 * h + rng.normal(0, 0.25))
                ev = seal[g] + life; last = 1 if ev > VMAX else 0
                if last:                                        # 終了時取得: res系列=0
                    nv_res, sy_res, sq_res = 0, 0.0, 0.0
                else:
                    nv_res = rng.binomial(n, 0.05 + 0.5 * (1 - h)); nr = n - nv_res
                    nhr = int(round(nr * h))
                    yr = np.concatenate([rng.normal(2.5 + drift, 0.8, nhr),
                                         rng.normal(11.5, 1.0, nr - nhr)])
                    sy_res, sq_res = yr.sum(), (yr * yr).sum()
                reacc = rng.binomial(n, 0.6 * h)
                f.write(f"{g},{last},{g % 64},{pool},{seal[g] - n * 20:.0f},{seal[g]:.0f},"
                        f"{-1 if last else format(ev, '.0f')},{last},{n},{nv_in},"
                        f"{y.sum():.4f},{(y * y).sum():.4f},{nv_res},{sy_res:.4f},{sq_res:.4f},"
                        f"{RU},{n * OBJ},{ne * OBJ},{0 if last else (n - nv_res) * OBJ},"
                        f"{y.sum() * OBJ:.2f},{sy_res * OBJ:.2f},{reacc},{reacc * OBJ}\n")
    log(f"demo CSV → {d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="analysis/region_csv")
    ap.add_argument("--out", default="analysis/fig/temp_change")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    if a.demo:
        a.dir = "analysis/region_csv_demo"; a.out = "analysis/fig/temp_change_demo"
        make_demo(a.dir)
    groups = discover(a.dir)
    if not groups:
        log(f"csvが見つかりません: {a.dir}"); return
    os.makedirs(a.out, exist_ok=True)
    summary = {}
    for trace, cfgs in groups.items():
        data = {}
        for cfg, path in cfgs.items():
            r, cols = load(path)
            log(f"[{trace}/{cfg}] {os.path.basename(path)}: 封緘済み世代={len(r)} 列={len(cols)}")
            if len(r): data[cfg] = (r, cols)
        if not data: continue
        summary[trace] = {c: {} for c in data}
        plot_temp(trace, data, a.out, summary)
        plot_res(trace, data, a.out, summary)
        plot_icc(trace, data, a.out, summary)
        for cfg in sorted(data, key=order_key):
            s = summary[trace][cfg]
            log(f"  {cfg}: ICC={s.get('icc', float('nan')):.3f} "
                f"温度B中央値={s.get('temp_in_median', float('nan')):.2f} "
                f"削除精度={s.get('precision_median', float('nan')):.2f}")
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    log(f"done → {a.out}")


if __name__ == "__main__":
    main()
