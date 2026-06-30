"""
pipeline.py — 任意の .oracleGeneral.zst に対する「時間窓→全体」代表性分析（トレース非依存）。

  python pipeline.py <trace.zst> [name]

各トレースで生成 (analysis/fig/<name>/):
  histogram_combined.png : 個数(塗り)＋バイト量(////) を1枚に, CDF25/50/75着色(重複時50優先)
  hero_combined.png      : 上記＋各ビンに 1h窓 p10–p90 のひげ → 代表形状と時間変動を1枚で
  sweep.png              : 窓サイズスイープ(JSD vs 窓), 灰=サンプリング雑音床
  temporal.png           : バイト分布の時間ヒートマップ＋JSD時系列
  summary.json           : 数値サマリ

設計の要点:
  - clock は trace により unix/相対秒どちらもあり得る → tmin/tmax をデータから自動検出
  - サイズ範囲も自動 (active bucket = global count>0 の範囲)
  - 「同形状」基準 = サンプリング雑音床(ブートストラップ) ＋ 効果量バンド(JSD/TV)
"""
import sys, os, json, time
import numpy as np, zstandard
from scipy.spatial.distance import jensenshannon
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Patch

_av = {f.name for f in font_manager.fontManager.ttflist}
for _jp in ("Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP"):
    if _jp in _av:
        plt.rcParams["font.family"] = _jp; break
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})

NB, SEC, OBS = 48, 3600, 100_000
C25, C50, C75, BAR = "#95C62A", "#FDD000", "#1ABCEF", "#C9CED6"
EDGE = "#5b6675"
dt = np.dtype([('clock', '<u4'), ('oid', '<u8'), ('size', '<u4'), ('nv', '<i8')])
UMAP = {0: "B", 10: "K", 20: "M", 30: "G", 40: "T"}
UNAME = {0: "B", 10: "kiB", 20: "MiB", 30: "GiB", 40: "TiB"}

def parse_size(s):
    """'512K'/'1M'/'2G'/'1048576' → バイト数(int)。None/空ならNone。"""
    if not s: return None
    s = str(s).strip().upper().rstrip("IB")  # 'KiB'等も許容
    mul = 1
    if s and s[-1] in "KMGT":
        mul = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[s[-1]]; s = s[:-1]
    return int(float(s) * mul)

def size_label(b):
    for thr in (40, 30, 20, 10, 0):
        if b >= thr: return f"{1<<(b-thr)}{UMAP[thr]}"
def unit_of(b):
    u = "B"
    for thr in (0, 10, 20, 30, 40):
        if b >= thr: u = UNAME[thr]
    return u

def norm(v):
    s = v.sum(); return v / s if s > 0 else v
def jsd(p, q):
    d = jensenshannon(p, q, base=2); return float(d * d) if np.isfinite(d) else np.nan
def tvd(p, q):
    return float(0.5 * np.abs(p - q).sum())
def cdf_bins(p):
    c = np.cumsum(p); return {q: int(np.searchsorted(c, q)) for q in (.25, .5, .75)}
def bar_colors(n, marks):
    cols = [BAR] * n
    for q, col in [(.25, C25), (.75, C75), (.5, C50)]:   # 50を最後=重複時優先
        if 0 <= marks[q] < n: cols[marks[q]] = col
    return cols

# ---------------- スキャン (2パス) ----------------
def read_records(path, chunk_bytes=1 << 23):
    d = zstandard.ZstdDecompressor()
    leftover = b''
    with open(path, 'rb') as fh:
        r = d.stream_reader(fh)
        while True:
            c = r.read(chunk_bytes)
            if not c: break
            buf = leftover + c
            m = (len(buf) // 24) * 24
            yield np.frombuffer(buf[:m], dtype=dt)
            leftover = buf[m:]

def scan(path, cap=None):
    """cap(bytes)指定時は size>cap のレコードを除外して集計。"""
    t0 = time.time()
    tmin, tmax, n, dropped = 2**64, 0, 0, 0
    for arr in read_records(path):
        if len(arr) == 0: continue
        if cap is not None:
            tot0 = len(arr); arr = arr[arr['size'] <= cap]; dropped += tot0 - len(arr)
            if len(arr) == 0: continue
        clk = arr['clock']; tmin = min(tmin, int(clk.min())); tmax = max(tmax, int(clk.max())); n += len(arr)
    n_hours = (tmax - tmin) // SEC + 1
    n_chunks = n // OBS + 2
    hc = np.zeros((n_hours, NB)); hb = np.zeros((n_hours, NB))
    cc = np.zeros((n_chunks, NB)); cb = np.zeros((n_chunks, NB))
    seen = 0
    for arr in read_records(path):
        if len(arr) == 0: continue
        if cap is not None:
            arr = arr[arr['size'] <= cap]
        k = len(arr)
        if k == 0: continue
        sizes = arr['size'].astype(np.float64)
        b = np.clip(np.floor(np.log2(np.maximum(arr['size'].astype(np.uint64), 1))).astype(np.int64), 0, NB - 1)
        h = np.clip((arr['clock'].astype(np.int64) - tmin) // SEC, 0, n_hours - 1)
        i1 = h * NB + b
        hc += np.bincount(i1, minlength=n_hours * NB)[:n_hours * NB].reshape(n_hours, NB)
        hb += np.bincount(i1, weights=sizes, minlength=n_hours * NB)[:n_hours * NB].reshape(n_hours, NB)
        ch = (seen + np.arange(k)) // OBS
        i2 = ch * NB + b
        cc += np.bincount(i2, minlength=n_chunks * NB)[:n_chunks * NB].reshape(n_chunks, NB)
        cb += np.bincount(i2, weights=sizes, minlength=n_chunks * NB)[:n_chunks * NB].reshape(n_chunks, NB)
        seen += k
    def trim(a):
        nz = np.where(a.sum(1) > 0)[0]; return a[:nz.max() + 1] if len(nz) else a[:0]
    hc, hb = trim(hc), trim(hb)
    nz = np.where(cc.sum(1) > 0)[0]; last = nz.max() + 1 if len(nz) else 0
    cc, cb = cc[:last], cb[:last]
    cap_msg = f" cap={cap}B dropped={dropped}({100*dropped/(n+dropped):.2f}%)" if cap is not None else ""
    print(f"  scan: records={n} hours={hc.shape[0]} chunks={cc.shape[0]} span={tmax-tmin}s elapsed={time.time()-t0:.1f}s{cap_msg}")
    return dict(hc=hc, hb=hb, cc=cc, cb=cb, n=n, tmin=tmin, tmax=tmax, cap=cap, dropped=dropped)

# ---------------- 解析 ----------------
def analyze_numbers(S):
    hc, hb = S['hc'], S['hb']
    gc, gb = hc.sum(0), hb.sum(0)
    active = np.where(gc > 0)[0]; LO, HI = int(active.min()), int(active.max()); sl = slice(LO, HI + 1)
    gPc, gPb = norm(gc[sl]), norm(gb[sl])
    msz = np.divide(gb, gc, out=np.zeros_like(gb), where=gc > 0)[sl]

    def floor(N, reps=300):
        N = int(min(N, 4_000_000)); rng = np.random.default_rng(0); jc = []; jb = []
        for _ in range(reps):
            dr = rng.multinomial(N, gPc).astype(float)
            jc.append(jsd(norm(dr), gPc)); jb.append(jsd(norm(dr * msz), gPb))
        return dict(count=float(np.percentile(jc, 90)), byte=float(np.percentile(jb, 90)))

    def sweep(mc, mb, mults, fmt, unit):
        out = []
        W = mc.shape[0]
        for L in mults:
            nw = W // L
            if nw < 2: continue
            jc = []; jb = []; tc = []; tb = []; ns = []
            for w in range(nw):
                c = mc[w*L:(w+1)*L].sum(0); bb = mb[w*L:(w+1)*L].sum(0); N = c.sum()
                if N <= 0: continue
                pc, pb = norm(c[sl]), norm(bb[sl])
                jc.append(jsd(pc, gPc)); jb.append(jsd(pb, gPb)); tc.append(tvd(pc, gPc)); tb.append(tvd(pb, gPb)); ns.append(N)
            jc, jb, tc, tb, ns = map(np.array, (jc, jb, tc, tb, ns))
            out.append(dict(label=fmt(L), span=L*unit, N_med=float(np.median(ns)), nwin=len(jc),
                            count=dict(p50=float(np.median(jc)), p90=float(np.percentile(jc,90)), max=float(jc.max()), tv90=float(np.percentile(tc,90))),
                            byte=dict(p50=float(np.median(jb)), p90=float(np.percentile(jb,90)), max=float(jb.max()), tv90=float(np.percentile(tb,90))),
                            floor=floor(np.median(ns))))
        return out

    wall = sweep(hc, hb, [1,2,3,6,12,24], lambda L: f"{L}h", SEC)
    obs  = sweep(S['cc'], S['cb'], [1,2,5,10,16,32], lambda L: f"{L*100}k", OBS)

    def p50b(h):
        tot = h.sum();
        if tot == 0: return -1
        return int(np.searchsorted(np.cumsum(h), tot/2))
    gcp, gbp = p50b(gc), p50b(gb)
    cp = np.array([p50b(hc[w]) for w in range(hc.shape[0])])
    bp = np.array([p50b(hb[w]) for w in range(hb.shape[0])])
    stab = dict(count_p50=size_label(gcp), byte_p50=size_label(gbp), band_oct=int(gbp-gcp),
                count_match=float(np.mean(cp == gcp)), byte_match=float(np.mean(bp == gbp)),
                count_pm1=float(np.mean(np.abs(cp-gcp) <= 1)), byte_pm1=float(np.mean(np.abs(bp-gbp) <= 1)))
    return dict(LO=LO, HI=HI, gc=gc, gb=gb, gPc=gPc.tolist(), gPb=gPb.tolist(),
                wall=wall, obs=obs, stab=stab, n=S['n'], span_s=S['tmax']-S['tmin'])

# ---------------- 作図 ----------------
def _columns_at(LO, HI, gc, gb, eps):
    """『個数割合<eps かつ バイト割合<eps』が2連続以上の区間を1本の集約バーに畳む。"""
    rng = list(range(LO, HI + 1))
    tc = gc[LO:HI + 1].sum() or 1.0; tb = gb[LO:HI + 1].sum() or 1.0
    keep = [(gc[b] / tc >= eps or gb[b] / tb >= eps) for b in rng]
    cols = []; i = 0; nL = len(rng)
    while i < nL:
        if keep[i]:
            cols.append(dict(label=size_label(rng[i]), buckets=[rng[i]], agg=False)); i += 1
        else:
            j = i
            while j < nL and not keep[j]: j += 1
            run = rng[i:j]
            if len(run) < 2:
                cols += [dict(label=size_label(b), buckets=[b], agg=False) for b in run]
            else:
                a, b = run[0], run[-1]
                lab = f"≤{size_label(b)}" if i == 0 else (f"≥{size_label(a)}" if j >= nL else f"{size_label(a)}–{size_label(b)}")
                cols.append(dict(label=lab, buckets=run, agg=True))
            i = j
    return cols

def make_columns(LO, HI, gc, gb, compact, eps=0.005, target=16):
    """表示列を作る。compact時は裾を集約。列数>target なら eps を上げて target列以下に圧縮(スライド向け)。"""
    if not compact:
        return [dict(label=size_label(b), buckets=[b], agg=False) for b in range(LO, HI + 1)]
    cols = _columns_at(LO, HI, gc, gb, eps)
    while len(cols) > target and eps < 0.06:
        eps *= 1.4; cols = _columns_at(LO, HI, gc, gb, eps)
    return cols

def _col_props(cols, gc, gb, LO, HI):
    tc = gc[LO:HI + 1].sum() or 1.0; tb = gb[LO:HI + 1].sum() or 1.0
    pc = np.array([sum(gc[b] for b in c['buckets']) for c in cols]) / tc
    pb = np.array([sum(gb[b] for b in c['buckets']) for c in cols]) / tb
    return pc, pb

def _cdf_col(cols, gv, LO, HI):
    """CDF25/50/75% を含む *列* index(各分布ごと)。重複時50優先はbar_colors側で処理。"""
    c = np.cumsum(norm(gv[LO:HI + 1])); out = {}
    for q in (.25, .5, .75):
        babs = LO + int(np.searchsorted(c, q))
        out[q] = next(ci for ci, col in enumerate(cols) if babs in col['buckets'])
    return out

def _unitbands(ax, cols):
    ymax = ax.get_ylim()[1]; prev = None; start = 0; n = len(cols)
    rot = any(len(c['label']) > 3 for c in cols) or n > 14
    for i, col in enumerate(cols):
        u = unit_of(col['buckets'][0])
        if u != prev:
            if prev is not None:
                ax.annotate(prev, ((start + i - 1) / 2, -ymax * (0.20 if rot else 0.13)), ha="center", va="top",
                            fontsize=10, weight="bold", color="#555", annotation_clip=False)
                ax.axvline(i - 0.5, color="#aaa", lw=0.8, ls=":", zorder=1)
            prev = u; start = i
    ax.annotate(prev, ((start + n - 1) / 2, -ymax * (0.20 if rot else 0.13)), ha="center", va="top",
                fontsize=10, weight="bold", color="#555", annotation_clip=False)

def _draw(ax, cols, gc, gb, LO, HI, hc=None, hb=None, whisker=False):
    pc, pb = _col_props(cols, gc, gb, LO, HI)
    cc = bar_colors(len(cols), _cdf_col(cols, gc, LO, HI))
    cb = bar_colors(len(cols), _cdf_col(cols, gb, LO, HI))
    x = np.arange(len(cols)); w = 0.42; xc, xb = x - w / 2 - .01, x + w / 2 + .01
    ax.bar(xc, pc, width=w, color=cc, edgecolor=EDGE, lw=0.7, zorder=3)
    ax.bar(xb, pb, width=w, color=cb, edgecolor=EDGE, lw=0.7, hatch="////", zorder=3)
    if whisker and hc is not None:
        def band(mat):
            tot = mat[:, LO:HI + 1].sum(1)
            cm = np.stack([mat[:, c['buckets']].sum(1) for c in cols], axis=1)
            with np.errstate(invalid='ignore', divide='ignore'):
                pw = cm / tot[:, None]
            pw = pw[tot > 0]
            return np.percentile(pw, 10, 0), np.percentile(pw, 90, 0)
        c10, c90 = band(hc); b10, b90 = band(hb)
        ax.errorbar(xc, pc, yerr=[np.maximum(pc - c10, 0), np.maximum(c90 - pc, 0)], fmt="none", ecolor="#222", elinewidth=1.1, capsize=2.3, zorder=4)
        ax.errorbar(xb, pb, yerr=[np.maximum(pb - b10, 0), np.maximum(b90 - pb, 0)], fmt="none", ecolor="#222", elinewidth=1.1, capsize=2.3, zorder=4)
    rot = any(len(c['label']) > 3 for c in cols) or len(cols) > 14
    ax.set_xticks(x); ax.set_xticklabels([c['label'] for c in cols], rotation=90 if rot else 0, fontsize=8 if len(cols) > 14 else 9)
    ax.grid(axis="y", alpha=0.25, zorder=0)
    return pc, pb

def _legend2(ax, whisker=False):
    shape = [Patch(fc="#dfe3ea", ec=EDGE, label="個数 (塗り)"), Patch(fc="#dfe3ea", ec=EDGE, hatch="////", label="バイト量 (////)")]
    if whisker: shape.append(plt.Line2D([0], [0], color="#222", lw=1.2, label="1h窓 p10–p90"))
    cdf = [Patch(fc=C25, ec=EDGE, label="CDF25%"), Patch(fc=C50, ec=EDGE, label="CDF50%(優先)"), Patch(fc=C75, ec=EDGE, label="CDF75%")]
    l1 = ax.legend(handles=shape, loc="upper left", frameon=False, fontsize=9); ax.add_artist(l1)
    ax.legend(handles=cdf, loc="upper right", frameon=False, fontsize=9)

def fig_histogram(R, name, outdir, compact=False):
    LO, HI = R['LO'], R['HI']
    cols = make_columns(LO, HI, R['gc'], R['gb'], compact)
    fig, ax = plt.subplots(figsize=(max(10, len(cols) * 0.78), 5.0))
    _draw(ax, cols, R['gc'], R['gb'], LO, HI)
    ax.set_ylabel("出現頻度割合 (正規化)")
    ax.set_title(f"{name} — サイズ分布{' (軸圧縮)' if compact else ''} (log2ビン, CDF着色)", weight="bold")
    _unitbands(ax, cols); _legend2(ax)
    fname = "histogram_compact.png" if compact else "histogram_combined.png"
    fig.tight_layout(); fig.savefig(os.path.join(outdir, fname), dpi=160, bbox_inches="tight"); plt.close(fig)

def fig_hero(R, S, name, outdir, compact=False):
    LO, HI = R['LO'], R['HI']
    cols = make_columns(LO, HI, R['gc'], R['gb'], compact)
    fig, ax = plt.subplots(figsize=(max(10, len(cols) * 0.78), 5.2))
    _draw(ax, cols, R['gc'], R['gb'], LO, HI, S['hc'], S['hb'], whisker=True)
    ax.set_ylabel("出現頻度割合 (正規化)")
    ax.set_title(f"{name} — 代表形状＋時間変動幅{' (軸圧縮)' if compact else ''} (ひげ=1h窓 p10–p90)", weight="bold")
    _unitbands(ax, cols); _legend2(ax, whisker=True)
    fname = "hero_compact.png" if compact else "hero_combined.png"
    fig.tight_layout(); fig.savefig(os.path.join(outdir, fname), dpi=170, bbox_inches="tight"); plt.close(fig)

# ---- レジーム検出(クラスタリング) ＋ small multiples ----
def fig_regimes(R, S, name, outdir, K=3, force=False):
    LO, HI = R['LO'], R['HI']; sl = slice(LO, HI + 1)
    p90 = R['wall'][0]['byte']['p90'] if R['wall'] else 0.0
    if not (p90 >= 0.05 or force):
        print(f"  regimes: 安定 (1h byte JSD p90={p90:.3f} < 0.05) のため省略 (--force-regimes で強制可)")
        return
    hc, hb = S['hc'], S['hb']; H = hc.shape[0]
    feats = []; widx = []
    for w in range(H):
        cs = hc[w][sl].sum(); bs = hb[w][sl].sum()
        if cs > 0 and bs > 0:
            feats.append(np.concatenate([hc[w][sl] / cs, hb[w][sl] / bs])); widx.append(w)
    feats = np.array(feats); widx = np.array(widx)
    if len(feats) < K:
        print("  regimes: 窓数不足でスキップ"); return
    from scipy.cluster.vq import kmeans2
    _, lab = kmeans2(feats, K, seed=0, minit='++', missing='warn')
    groups = [widx[lab == k] for k in range(K)]
    groups = [g for g in groups if len(g) > 0]
    # バイト加重平均サイズで昇順に並べ替え
    def gdist(g):
        gcm = hc[g].sum(0); gbm = hb[g].sum(0); return gcm, gbm
    def mean_b(gbm):
        p = norm(gbm[sl]); return float((np.arange(LO, HI + 1) * p).sum())
    groups.sort(key=lambda g: mean_b(gdist(g)[1]))
    Keff = len(groups)
    cols = make_columns(LO, HI, R['gc'], R['gb'], compact=True)   # 全体基準で列固定→比較可能
    pal = ['#4C78A8', '#F58518', '#54A24B', '#E45756', '#72B7B2', '#B279A2']
    # パネル & y上限統一
    panel = []
    for g in groups:
        gcm, gbm = gdist(g); pc, pb = _col_props(cols, gcm, gbm, LO, HI); panel.append((g, gcm, gbm, max(pc.max(), pb.max())))
    ymax = max(p[3] for p in panel) * 1.12
    fig = plt.figure(figsize=(max(12, 3.4 * Keff), 5.6))
    gs = fig.add_gridspec(2, Keff, height_ratios=[4, 0.7], hspace=0.5, wspace=0.22)
    for i, (g, gcm, gbm, _) in enumerate(panel):
        ax = fig.add_subplot(gs[0, i])
        _draw(ax, cols, gcm, gbm, LO, HI, hc[g], hb[g], whisker=True)
        ax.set_ylim(0, ymax)
        if i == 0: ax.set_ylabel("出現頻度割合")
        share = 100 * len(g) / len(widx)
        ax.set_title(f"レジーム{i+1}  {len(g)}窓 ({share:.0f}%)", color=pal[i % len(pal)], weight="bold", fontsize=11)
        _unitbands(ax, cols)
    # 時間帯→レジーム 帯
    axt = fig.add_subplot(gs[1, :])
    lab2 = np.full(H, -1)
    for i, (g, *_2) in enumerate(panel):
        lab2[g] = i
    from matplotlib.colors import ListedColormap
    cmap = ListedColormap([pal[i % len(pal)] for i in range(Keff)])
    masked = np.ma.masked_less(lab2.reshape(1, -1), 0)
    axt.imshow(masked, aspect="auto", cmap=cmap, vmin=0, vmax=Keff - 1, extent=[0, H, 0, 1])
    axt.set_yticks([]); axt.set_xlabel("経過時間 (時)"); axt.set_title("各時間窓のレジーム帰属", fontsize=10)
    sh = [Patch(fc="#dfe3ea", ec=EDGE, label="個数(塗り)"), Patch(fc="#dfe3ea", ec=EDGE, hatch="////", label="バイト量(////)"),
          plt.Line2D([0], [0], color="#222", lw=1.2, label="窓内 p10–p90")]
    fig.legend(handles=sh, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.02), fontsize=9)
    fig.suptitle(f"{name} — レジーム別 代表分布 (k-means, K={Keff})", y=1.07, weight="bold", fontsize=13)
    fig.savefig(os.path.join(outdir, "regimes.png"), dpi=160, bbox_inches="tight"); plt.close(fig)
    print(f"  regimes: K={Keff} 出力 (時間シェア: " + ", ".join(f"R{i+1}={100*len(g)/len(widx):.0f}%" for i, (g, *_3) in enumerate(panel)) + ")")

def fig_sweep(R, name, outdir):
    def one(ax, rows, xkey, xlabel):
        if not rows: ax.set_visible(False); return
        xs = [(r['span']/3600 if xkey=='h' else r['N_med']) for r in rows]
        for met, col, lab in [('byte','#d62728','byte'),('count','#1f77b4','count')]:
            ax.plot(xs, [r[met]['p90'] for r in rows], 'o-', color=col, lw=1.6, ms=4, label=f"{lab} p90")
            ax.plot(xs, [r[met]['max'] for r in rows], 'o:', color=col, lw=1.0, ms=3, alpha=0.6)
        fl = [r['floor']['byte'] for r in rows]
        ax.plot(xs, fl, 's--', color="#888", lw=1.1, ms=3, label="雑音床 p90")
        ax.fill_between(xs, 1e-7, fl, color="#bbb", alpha=0.4)
        ax.set_xscale('log'); ax.set_yscale('log'); ax.set_xlabel(xlabel); ax.set_ylabel("JS divergence (bits)")
        ax.grid(alpha=0.3, which='both'); ax.legend(fontsize=8, frameon=False)
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.4))
    one(axes[0], R['wall'], 'h', "壁時計 窓サイズ (時間, log)"); axes[0].set_title("壁時計時間窓", weight="bold")
    one(axes[1], R['obs'], 'N', "観測数 窓サイズ (req, log)"); axes[1].set_title("観測数窓", weight="bold")
    fig.suptitle(f"{name} — 代表性スイープ (実線=p90, 点線=最大, 灰=雑音床)", y=1.02, weight="bold")
    fig.tight_layout(); fig.savefig(os.path.join(outdir, "sweep.png"), dpi=150, bbox_inches="tight"); plt.close(fig)

def fig_temporal(R, S, name, outdir):
    LO, HI = R['LO'], R['HI']; sl = slice(LO, HI+1)
    gPc, gPb = np.array(R['gPc']), np.array(R['gPb'])
    hb = S['hb'][:, sl]; rs = hb.sum(1, keepdims=True)
    mat = np.divide(hb, rs, out=np.zeros_like(hb), where=rs > 0).T
    Hn = mat.shape[1]
    jc = np.array([jsd(norm(S['hc'][w][sl]), gPc) if S['hc'][w][sl].sum()>0 else np.nan for w in range(S['hc'].shape[0])])
    jb = np.array([jsd(norm(S['hb'][w][sl]), gPb) if S['hb'][w][sl].sum()>0 else np.nan for w in range(S['hb'].shape[0])])
    fig = plt.figure(figsize=(13, 6)); gs = fig.add_gridspec(2, 1, height_ratios=[2.2, 1], hspace=0.3)
    ax0 = fig.add_subplot(gs[0])
    im = ax0.imshow(mat, aspect="auto", origin="lower", cmap="viridis", extent=[0, Hn, LO-0.5, HI+0.5])
    ax0.set_yticks(range(LO, HI+1)); ax0.set_yticklabels([size_label(b) for b in range(LO, HI+1)], fontsize=7)
    ax0.set_ylabel("オブジェクトサイズ"); ax0.set_xlabel("経過時間 (時)"); ax0.set_title(f"{name} — バイト量分布の時間変化", weight="bold")
    fig.colorbar(im, ax=ax0, pad=0.01).set_label("頻度割合")
    ax1 = fig.add_subplot(gs[1])
    ax1.plot(np.arange(len(jc))+.5, jc, color="#1f77b4", lw=1.1, label="個数 JSD")
    ax1.plot(np.arange(len(jb))+.5, jb, color="#d62728", lw=1.1, label="バイト量 JSD")
    ax1.set_xlim(0, max(len(jc), 1)); ax1.set_xlabel("経過時間 (時)"); ax1.set_ylabel("JSD vs 全体")
    ax1.grid(alpha=0.3); ax1.legend(fontsize=9, frameon=False, ncol=2)
    fig.savefig(os.path.join(outdir, "temporal.png"), dpi=150, bbox_inches="tight"); plt.close(fig)

def run(path, name=None, max_size=None, regimes=3, force_regimes=False):
    name = name or os.path.basename(path).split('.')[0]
    if max_size is not None: name = f"{name}_le{size_label(int(np.floor(np.log2(max(max_size,1)))))}"
    outdir = os.path.join(os.path.dirname(__file__), "fig", name); os.makedirs(outdir, exist_ok=True)
    print(f"[{name}]")
    S = scan(path, cap=max_size)
    R = analyze_numbers(S)
    fig_histogram(R, name, outdir, compact=False); fig_histogram(R, name, outdir, compact=True)
    fig_hero(R, S, name, outdir, compact=False);   fig_hero(R, S, name, outdir, compact=True)
    fig_sweep(R, name, outdir); fig_temporal(R, S, name, outdir)
    fig_regimes(R, S, name, outdir, K=regimes, force=force_regimes)
    R_save = {k: v for k, v in R.items() if k not in ('gc', 'gb')}
    R_save['max_size'] = max_size; R_save['dropped'] = S['dropped']
    json.dump(R_save, open(os.path.join(outdir, "summary.json"), "w"), ensure_ascii=False)
    st = R['stab']
    print(f"  size: {size_label(R['LO'])}..{size_label(R['HI'])} | count_p50={st['count_p50']} byte_p50={st['byte_p50']} band={st['band_oct']}oct")
    print(f"  count_p50 一致={st['count_match']*100:.1f}% (±1={st['count_pm1']*100:.1f}%) / byte_p50 一致={st['byte_match']*100:.1f}% (±1={st['byte_pm1']*100:.1f}%)")
    if R['wall']:
        w1 = R['wall'][0]
        print(f"  1h窓 JSD: count p90={w1['count']['p90']:.4f} max={w1['count']['max']:.4f} / byte p90={w1['byte']['p90']:.4f} max={w1['byte']['max']:.4f} (床byte={w1['floor']['byte']:.4f})")
        print(f"  1h窓 TV p90: count={w1['count']['tv90']*100:.1f}% byte={w1['byte']['tv90']*100:.1f}%")
    return R

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="oracleGeneral サイズ分布 代表性分析")
    ap.add_argument("trace"); ap.add_argument("name", nargs="?", default=None)
    ap.add_argument("--max-size", default=None, help="このサイズ超のコンテンツを除外 (例 512K, 1M, 2G)")
    ap.add_argument("--regimes", type=int, default=3, help="レジーム small multiples のクラスタ数 K")
    ap.add_argument("--force-regimes", action="store_true", help="安定でもレジーム図を出力")
    a = ap.parse_args()
    run(a.trace, a.name, max_size=parse_size(a.max_size), regimes=a.regimes, force_regimes=a.force_regimes)
