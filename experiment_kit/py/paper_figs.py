#!/usr/bin/env python3
"""paper_figs.py — 論文用の比較（./run.sh paper の結果 work/paper/<name>.json）から、図 1 枚と表 1 つを作る。

  python3 paper_figs.py <WORK> [--ext | --cond c0|c0m|f1|s2|s3]  PAPER_LANG=en で英語の文字。--ext = paper-ext の結果を足して work/paper/ext/ に出す。
                                       --cond = paper-robust のその条件の構成だけで同じ一式を work/paper/robust/<条件>/ に出す

比べる構成（config.sh の PAPER_JOBS）:
  r256_base     Region 256 MiB（CacheLib の上限）・分類なし・再挿入 n=1        ← 基準（いまの CacheLib）
  ru_base       Region = RU（626.5 MiB）・分類なし・再挿入 n=1                ← 上限を外して RU に合わせただけ
  ru_ff90_h3    Region = RU・サイズ分類・大物の下限 0.9・再挿入 n=3（手で調整）  ← 提案
  ru_auto       Region = RU・サイズ分類（SplitThreshold）・下限の山登り・再挿入 n=auto（全部自動） ← 提案
  r256_ff90_h3, r256_auto  同じ方式を 256 MiB で                              ← 分類の効果が Region の大きさによらないか
  ru_none_auto, r256_none_auto  分類なしで再挿入の閾値の自動化（n=auto）だけ     ← 切り分け：*_auto との差が分類＋下限の効果
指標（図は値そのもの、表は基準との差 = 構成 − 基準）:
  BMR   バイトミス率 [%]（低いほど良い：元サーバへの転送）
  書き込み SSD に書いたバイト [TB]（受け入れ＋再挿入＋詰め物。低いほど良い）
  OHR   物のヒット率 [%]（高いほど良い。参考）
  アプリ層の書き込み増幅 = SSD に書いたバイト ÷ 受け入れたバイト（1 = 書き直しなし。表の基準の列）
出力（work/paper/）: fig_main.svg（3 面まとめ）と fig_bmr.svg / fig_write.svg / fig_ohr.svg（1 面ずつ）、
  補足の時間変化 fig_ops_<trace>.svg（横軸 = 要求数）/ fig_wr_<trace>.svg（横軸 = 累積 SSD 書き込み。PAPER_BINS = 区間のまとめ数、既定 100）。.png は変換器があれば, table_main.csv / .md / .tex, index.html。標準ライブラリだけ。"""
import csv, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, PAL, span, to_png

WORK = sys.argv[1]; EN = os.environ.get("PAPER_LANG", "ja") == "en"
EXT = "--ext" in sys.argv   # paper-ext: CacheLib の n=0・n=3（work/paper/ext/）を足した比較。図と表は work/paper/ext/ に出す
COND = sys.argv[sys.argv.index("--cond") + 1] if "--cond" in sys.argv else ""   # 頑健性の条件（c0 / f1 / s2 / s3）
if COND:   # work/paper/robust の <構成>_<条件> だけを <構成> として読み、work/paper/robust/<条件>/ に同じ一式を出す
    SRC = [f"{WORK}/paper/robust"]; D = f"{WORK}/paper/robust/{COND}"
else:
    SRC = [f"{WORK}/paper"] + ([f"{WORK}/paper/ext"] if EXT else [])   # 結果を読む場所（同じ構成は後の場所が優先）
    D = SRC[-1]                                                          # 図と表を書く場所
BASE = "r256_base"
ORDER = (["r256_base", "r256_n0", "r256_n2", "r256_n3", "r256_none_auto", "ru_none_auto", "ru_ff90_h3", "ru_auto"] if EXT else   # ext: CacheLib の閾値違いと提案
         ["r256_base", "r256_n0", "r256_n2", "r256_n3", "ru_base", "ru_none_auto", "ru_ff90_h3", "ru_auto", "r256_none_auto", "r256_ff90_h3", "r256_auto"]
         if COND else   # 条件ごと: その条件で回した構成を全部
         ["r256_base", "ru_base", "ru_none_auto", "ru_ff90_h3", "ru_auto", "r256_none_auto", "r256_ff90_h3", "r256_auto"])   # 図と表に並べる順
NAME = {
    "r256_base": ("CacheLib（R=256MiB）", "CacheLib (R=256MiB)"),
    "r256_n0": ("CacheLib n=0（書き直しなし）", "CacheLib n=0 (no rewrite)"),
    "r256_n2": ("CacheLib n=2", "CacheLib n=2"),
    "r256_n3": ("CacheLib n=3", "CacheLib n=3"),
    "ru_base": ("R=RU", "R=RU"),
    "ru_none_auto": ("R=RU＋n=auto（分類なし）", "R=RU + auto n (no class.)"),
    "r256_none_auto": ("R=256MiB＋n=auto（分類なし）", "R=256MiB + auto n (no class.)"),
    "ru_ff90_h3": ("R=RU＋分類（手動）", "R=RU + class. (manual)"),
    "ru_auto": ("R=RU＋分類（自動）", "R=RU + class. (auto)"),
    "r256_ff90_h3": ("R=256MiB＋分類（手動）", "R=256MiB + class. (manual)"),
    "r256_auto": ("R=256MiB＋分類（自動）", "R=256MiB + class. (auto)"),
}
COL = {"r256_base": "#4d4d4d", "r256_n0": "#d0d0d0", "r256_n2": "#a0a0a0", "r256_n3": "#7f7f7f", "ru_base": "#a6a6a6", "ru_none_auto": "#9fb6cc", "r256_none_auto": "#dcc3ae", "ru_ff90_h3": "#3b7dbf", "ru_auto": "#1f4e8c",
       "r256_ff90_h3": "#e3a37c", "r256_auto": "#c9683a"}
nm = lambda c: NAME.get(c, (c, c))[EN]
T = lambda ja, en: en if EN else ja

runs, rundir = {}, {}   # rundir[(trace, config)] = その構成の出力のフォルダ（P.interval.csv の場所）
for src in SRC:
    for f in sorted(glob.glob(f"{src}/*.json")):
        d = json.load(open(f))
        if not (isinstance(d, dict) and "runs" in d): continue
        cur = runs.setdefault(d["name"], {k: v for k, v in d.items() if k != "runs"} | {"runs": {}})
        for c, r in d["runs"].items():
            if COND:   # r256_base_c0 → r256_base（その条件の構成だけ）
                if not c.endswith("_" + COND): continue
                key = c[: -len(COND) - 1]
            else: key = c
            cur["runs"][key] = r; rundir[(d["name"], key)] = f"{src}/{d['name']}/{c}"
runs = {n: d for n, d in runs.items() if BASE in d["runs"]}
if not runs: sys.exit(f"{SRC[0]}/*.json に {BASE}{'_' + COND if COND else ''} を含む結果が無い")
os.makedirs(D, exist_ok=True)
names = sorted(runs, key=lambda n: (runs[n].get("category", ""), runs[n].get("source", ""), n))
cfgs = [c for c in ORDER if any(c in runs[n]["runs"] for n in names)]
others = [c for c in cfgs if c != BASE]

# 指標（図は値そのもの）
BMR = lambda r: 100 * r["bmr"]
OHR = lambda r: 100 * r["ohr"]
TB = lambda r: r["write"] * r["req_bytes"] / 1e12            # SSD に書いたバイト [TB]（取得＋再書き込み＋詰め物）
ATB = lambda r: r["admit"] * r["req_bytes"] / 1e12           # 取得：ミスして受け入れたバイト [TB]
RTB = lambda r: r["reinsert"] * r["req_bytes"] / 1e12        # 再書き込み：追い出しの際に書き直したバイト [TB]（アプリ層の GC）
AWAF = lambda r: r["write"] / r["admit"] if r.get("admit") else None

# ---------------------------------------------------------------- 表（CSV は全部、MD / TeX は主な列）
rows = []
for n in names:
    R = runs[n]["runs"]; b = R[BASE]
    for c in cfgs:
        if c not in R: continue
        r = R[c]
        rows.append(dict(trace=n, category=runs[n].get("category", ""), source=runs[n].get("source", ""), config=c, label=nm(c),
                         bmr_pct=BMR(r), ohr_pct=OHR(r), ssd_write_TB=TB(r), admit_TB=ATB(r), reinsert_TB=RTB(r), pad_TB=TB(r) - ATB(r) - RTB(r), write_per_req_byte=r["write"],
                         reinsert_per_req_byte=r["reinsert"], app_waf=AWAF(r),
                         d_bmr_pt=BMR(r) - BMR(b), write_norm=TB(r) / TB(b) if TB(b) else None, d_ohr_pt=OHR(r) - OHR(b)))
with open(f"{D}/table_main.csv", "w", newline="") as f:
    w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)


def cell(n, c, tex=False):
    R = runs[n]["runs"]
    if c not in R: return "–"
    b, r = R[BASE], R[c]
    v = (BMR(r) - BMR(b), TB(r) / TB(b) if TB(b) else float("nan"), OHR(r) - OHR(b))
    s = (f"{v[0]:+.2f}", f"{v[1]:.3f}", f"{v[2]:+.1f}")
    return " / ".join(f"${x}$" for x in s) if tex else " / ".join(s)


hdr = [T("トレース", "Trace"), T("基準 BMR [%]", "Base BMR [%]"), T("基準 書込 [TB]（取得＋再書込）", "Base writes [TB] (admit + rewrite)"),
       T("基準 OHR [%]", "Base OHR [%]"), T("基準 書込増幅", "Base app. WAF")] + [nm(c) for c in others]
md = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
tex = [r"\begin{table*}[t]", r"\centering\small", r"\setlength{\tabcolsep}{4pt}",
       r"\begin{tabular}{l" + "r" * (4 + len(others)) + "}", r"\toprule",
       " & ".join(hdr).replace("%", r"\%").replace("＋", "+") + r" \\", r"\midrule"]
for n in names:
    b = runs[n]["runs"][BASE]
    base = [f"{BMR(b):.2f}", f"{TB(b):.1f}（{ATB(b):.1f}＋{RTB(b):.1f}）" if not EN else f"{TB(b):.1f} ({ATB(b):.1f}+{RTB(b):.1f})", f"{OHR(b):.1f}", f"{AWAF(b):.2f}" if AWAF(b) else "–"]
    md.append("| " + " | ".join([n] + base + [cell(n, c) for c in others]) + " |")
    tex.append(" & ".join([n.replace("_", r"\_")] + base + [cell(n, c, True) for c in others]) + r" \\")
cap = T("各セル = ΔBMR [pt]（構成 − 基準、負 = 良い）/ SSD 書き込み（基準 = 1 に正規化、1 未満 = 良い）/ ΔOHR [pt]（構成 − 基準、正 = 良い）。"
        "基準 = CacheLib 相当（Region 256 MiB、分類なし、再挿入 n=1）。書込増幅 = SSD に書いたバイト ÷ 受け入れたバイト。",
        "Each cell: ΔBMR [pt] (config − baseline; negative = better) / SSD writes normalized to the baseline (< 1 = better) / "
        "ΔOHR [pt] (config − baseline; positive = better). Baseline: CacheLib-like (256 MiB regions, no classification, reinsertion n=1). "
        "App. WAF = bytes written to SSD / bytes admitted.")
tex += [r"\bottomrule", r"\end{tabular}", r"\caption{" + cap.replace("%", r"\%").replace("＋", "+") + "}", r"\label{tab:main}", r"\end{table*}"]
open(f"{D}/table_main.md", "w", encoding="utf-8").write("\n".join(md) + "\n\n" + cap + "\n")
open(f"{D}/table_main.tex", "w", encoding="utf-8").write("\n".join(tex) + "\n")

# ---------------------------------------------------------------- 図（1 枚、横に 3 面：BMR・書き込み量・OHR。値そのもの）
multi = len(names) > 1
def series(c, fn, mean=True):
    v = [fn(runs[n]["runs"][c]) if c in runs[n]["runs"] else None for n in names]
    if multi and mean:
        ok = [x for x in v if x is not None]; v.append(sum(ok) / len(ok) if ok else None)
    return v
cats_m = names + ([T("平均", "Mean")] if multi else [])
# 値そのもの（縦軸は 0 から）と、基準（CacheLib、R=256MiB）との差・比（基準の線を引く）
rel = lambda fn, c, n: fn(runs[n]["runs"][c], runs[n]["runs"][BASE])
PANELS = {  # slug: (題, 縦軸, 値の関数 (r, b), 平均をつけるか, 小数, 種類 abs|diff|ratio)
    "bmr": (T("BMR（低いほど良い）", "BMR (lower is better)"), "BMR [%]", lambda r, b: BMR(r), True, 2, "abs"),
    "write": (T("SSD 書き込み量（低いほど良い）", "SSD writes (lower is better)"), T("書き込み [TB]", "Writes [TB]"), lambda r, b: TB(r), False, 1, "abs"),
    "ohr": (T("OHR（参考。高いほど良い）", "OHR (for reference)"), "OHR [%]", lambda r, b: OHR(r), True, 1, "abs"),
    "dbmr": (T("ΔBMR（CacheLib 比。低いほど良い）", "ΔBMR vs. CacheLib (lower is better)"), "ΔBMR [pt]", lambda r, b: BMR(r) - BMR(b), True, 2, "diff"),
    "nwrite": (T("SSD 書き込み量（CacheLib の合計 = 1。低いほど良い）", "SSD writes (CacheLib total = 1; lower is better)"),
               T("書き込み（正規化）", "Normalized writes"), lambda r, b: TB(r) / TB(b) if TB(b) else None, True, 3, "ratio"),
    "dohr": (T("ΔOHR（CacheLib 比。参考）", "ΔOHR vs. CacheLib (for reference)"), "ΔOHR [pt]", lambda r, b: OHR(r) - OHR(b), True, 1, "diff"),
}


def stacked(a, key, shown, mean):
    """書き込みの棒を 取得（濃い）＋ 再書き込み（薄い）に分けて積む。nwrite は基準の書き込み全体 = 1 で割る。詰め物は描かない（表に残す）"""
    k = len(shown); w = a.bw * 0.8 / max(k, 1)
    for gi, c in enumerate(shown):
        col = COL.get(c, PAL[cfgs.index(c) % len(PAL)])
        parts = []
        for n in names:
            R = runs[n]["runs"]
            if c not in R: parts.append(None); continue
            z = TB(R[BASE]) if key == "nwrite" else 1
            parts.append((ATB(R[c]) / z, RTB(R[c]) / z) if z else None)
        if mean and multi:
            ok = [p for p in parts if p]; parts.append((sum(p[0] for p in ok) / len(ok), sum(p[1] for p in ok) / len(ok)) if ok else None)
        for i, p in enumerate(parts):
            if not p: continue
            x = a.x0 + a.bw * i + a.bw * 0.1 + w * gi
            y0, y1, y2 = a.Y(0), a.Y(p[0]), a.Y(p[0] + p[1])
            a.f.e.append(f'<rect x="{x+2:.1f}" y="{y1:.1f}" width="{max(w-4, 1):.1f}" height="{y0-y1:.1f}" fill="{col}"/>')
            a.f.e.append(f'<rect x="{x+2:.1f}" y="{y2:.1f}" width="{max(w-4, 1):.1f}" height="{y1-y2:.1f}" fill="{col}" fill-opacity="0.35" stroke="{col}" stroke-width="1.5"/>')


def draw(keys):  # keys の面を横に並べた 1 枚の図
    kinds = {PANELS[k][5] for k in keys}
    shown = cfgs if "abs" in kinds else others          # 差・比の図には基準の棒を出さない（基準 = 0 / 1 の線）
    f = Fig(); f.add_legend([(nm(c), COL.get(c, PAL[i % len(PAL)]), "box") for i, c in enumerate(cfgs) if c in shown])
    if {"write", "nwrite"} & set(keys):
        f.add_legend([(T("書き込み：濃い = 取得（ミス）、薄い = 再書き込み", "Writes: solid = admitted (misses), light = rewrites"), "#bbbbbb", "box")])
    top, x0, gap = f.legend_h() + 50, 110, 115
    W = (1600 - x0 - 30 - gap * (len(keys) - 1)) // len(keys)
    for k, key in enumerate(keys):
        title, ylab, fn, mean, d, kind = PANELS[key]
        data = {}
        for c in shown:
            v = [rel(fn, c, n) if c in runs[n]["runs"] else None for n in names]
            if multi and mean:
                ok = [x for x in v if x is not None]; v.append(sum(ok) / len(ok) if ok else None)
            data[c] = v
        vals = [x for v in data.values() for x in v if x is not None] or [0]
        if key in ("write", "nwrite"):
            vals = [(ATB(runs[n]["runs"][c]) + RTB(runs[n]["runs"][c])) / (TB(runs[n]["runs"][BASE]) if key == "nwrite" else 1)
                    for c in shown for n in names if c in runs[n]["runs"]] or [0]
        if kind == "diff": yr = span(vals)                           # 0 を含む（正負の差）
        else: yr = (0, max(vals + ([1] if kind == "ratio" else [])) * 1.1 or 1)   # 0 から
        a = Ax(f, (x0 + k * (W + gap), top, x0 + k * (W + gap) + W, 760), yr=yr, cats=cats_m if mean else names, ylabel=ylab, title=title)
        if key in ("write", "nwrite"): stacked(a, key, shown, mean)
        else: a.bars([(nm(c), COL.get(c, PAL[i % len(PAL)]), data[c], None) for i, c in enumerate(cfgs) if c in shown],
                     d=d, sign=kind == "diff")
        if kind == "ratio": a.hline(1.0, col="#4d4d4d", label=T("CacheLib = 1", "CacheLib = 1"))
    return f.svg()


FIGS = {"fig_main": ["bmr", "write", "ohr"], "fig_rel": ["dbmr", "nwrite", "dohr"],   # 値そのもの / CacheLib 比の 3 面まとめ
        **{f"fig_{k}": [k] for k in PANELS}}                                         # 1 面ずつ（6 枚）
for slug, keys in FIGS.items():
    open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(draw(keys))


# ---------------------------------------------------------------- 時間変化（補足の図。トレースごとに 2 枚）
#   fig_ops_<trace>: 横軸 = 処理した要求数。BMR・OHR = 区間ごと（約 BINS 区間にまとめる）、取得・再書き込み = それぞれの累積 TB
#   fig_wr_<trace> : 横軸 = その構成の累積 SSD 書き込み [TB]。BMR・OHR = そこまでの累積（同じ量を書いた時点の比較）
BINS = int(os.environ.get("PAPER_BINS", "100"))
K = ("reqs", "req_bytes", "dram_hits", "ssd_hits", "hit_bytes", "miss_bytes", "flash_region_write_bytes", "ssd_admit_bytes", "reinsert_bytes")


def interval_rows(n, c):  # P.interval.csv の区間の行（累積でない方）。無ければ None
    p = f"{rundir.get((n, c), f'{D}/{n}/{c}')}/P.interval.csv"
    if not os.path.exists(p): return None
    return [{k: int(r[k]) for k in K} for r in csv.DictReader(open(p)) if r["kind"] == "interval"]


def timeline(rows):  # → [(累積要求, 区間 BMR, 区間 OHR, 累積書込 TB, 累積 BMR, 累積 OHR, 累積取得 TB, 累積再書込 TB)]
    g = max(1, len(rows) // BINS); out = []; cum = dict.fromkeys(K, 0)
    for i in range(0, len(rows), g):
        w = {k: sum(r[k] for r in rows[i:i + g]) for k in K}
        for k in K: cum[k] += w[k]
        if not w["reqs"] or not w["req_bytes"]: continue
        out.append((cum["reqs"], 100 * w["miss_bytes"] / w["req_bytes"], 100 * (w["dram_hits"] + w["ssd_hits"]) / w["reqs"],
                    cum["flash_region_write_bytes"] / 1e12, 100 * cum["miss_bytes"] / cum["req_bytes"],
                    100 * (cum["dram_hits"] + cum["ssd_hits"]) / cum["reqs"], cum["ssd_admit_bytes"] / 1e12, cum["reinsert_bytes"] / 1e12))
    return out


def tl_fig(n, xkey):
    S = {c: timeline(rows) for c in cfgs if (rows := interval_rows(n, c))}
    if not S: return None
    f = Fig(); f.add_legend([(nm(c), COL.get(c, PAL[i % len(PAL)]), "dash" if c == BASE else "line") for i, c in enumerate(cfgs) if c in S])
    top, x0, gap = f.legend_h() + 50, 110, 115
    if xkey == "ops":
        X = lambda p: p[0] / 1e6; xlab = T("処理した要求 [百万]", "Requests processed [M]")
        panel = [(T("BMR（区間ごと）", "BMR (per interval)"), "BMR [%]", 1),
                 (T("取得の書き込み（累積）", "Admitted bytes (cumulative)"), "TB", 6),
                 (T("再書き込み（累積）", "Rewritten bytes (cumulative)"), "TB", 7),
                 (T("OHR（区間ごと）", "OHR (per interval)"), "OHR [%]", 2)]
    else:
        X = lambda p: p[3]; xlab = T("SSD に書いた量 [TB]（その構成の累積）", "Bytes written to SSD [TB] (cumulative)")
        panel = [(T("BMR（累積）", "BMR (cumulative)"), "BMR [%]", 4), (T("OHR（累積）", "OHR (cumulative)"), "OHR [%]", 5)]
    W = (1600 - x0 - 30 - gap * (len(panel) - 1)) // len(panel)
    xs = [X(p) for s in S.values() for p in s]
    for k, (title, ylab, j) in enumerate(panel):
        ys = [p[j] for s in S.values() for p in s]
        a = Ax(f, (x0 + k * (W + gap), top, x0 + k * (W + gap) + W, 790), xr=(0, max(xs) or 1), yr=(0, max(ys) * 1.08 or 1),
               xlabel=xlab if k == len(panel) // 2 else "", ylabel=ylab, title=title)
        for i, c in enumerate(cfgs):
            if c in S: a.line([X(p) for p in S[c]], [p[j] for p in S[c]], COL.get(c, PAL[i % len(PAL)]), w=3, dash=c == BASE)
    return f.svg()


tl = []
for n in names:
    for xkey in ("ops", "wr"):
        svg = tl_fig(n, xkey)
        if svg:
            slug = f"fig_{xkey}_{n}"; open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(svg); tl.append(slug)

# ---------------------------------------------------------------- 交換関係の図（--ext のみ）：横 = SSD 書き込み（CacheLib n=1 = 1）、縦 = ΔBMR [pt]
#   CacheLib の n=0..3 を線でつなぐ（書き直しを増やすほど書き込みが増え BMR が下がる、という交換関係）。
#   提案の点がその線の左下にあれば、同じ BMR を少ない書き込みで（同じ書き込みで低い BMR を）出している
CURVE = [("r256_n0", "n=0"), ("r256_base", "n=1"), ("r256_n2", "n=2"), ("r256_n3", "n=3")]
PTS = [("ru_auto", 11, False), ("ru_ff90_h3", 10, True), ("ru_none_auto", 8, True), ("r256_none_auto", 8, True)]


def tradeoff(keys):
    f = Fig()
    f.add_legend([(T("CacheLib（R=256MiB）n=0〜3", "CacheLib (R=256MiB) n=0..3"), "#4d4d4d", "line")] +
                 [(nm(c), COL.get(c, "#888"), "hollow" if hol else "dot") for c, _, hol in PTS if any(c in runs[n]["runs"] for n in keys)])
    top = f.legend_h() + 50; cols = min(3, len(keys)); rows_ = (len(keys) + cols - 1) // cols
    W = (1600 - 110 - 30 - 115 * (cols - 1)) // cols; H = (870 - top - 70 * (rows_ - 1)) // rows_
    for i, n in enumerate(keys):
        R = runs[n]["runs"]; b = R[BASE]
        if not TB(b): continue
        P = lambda c: (TB(R[c]) / TB(b), BMR(R[c]) - BMR(b))
        cur = [(P(c), lab) for c, lab in CURVE if c in R]
        dots = [(P(c), c, r, hol) for c, r, hol in PTS if c in R]
        xs = [p[0] for p, *_ in cur + dots]; ys = [p[1] for p, *_ in cur + dots]
        cx, cy = i % cols, i // cols
        box = (110 + cx * (W + 115), top + cy * (H + 70), 110 + cx * (W + 115) + W, top + cy * (H + 70) + H)
        a = Ax(f, box, xr=(0, max(xs + [1]) * 1.08), yr=span(ys), title=n,
               xlabel=T("SSD 書き込み（CacheLib n=1 = 1）", "SSD writes (CacheLib n=1 = 1)") if cy == rows_ - 1 and cx == cols // 2 else "",
               ylabel=T("ΔBMR [pt]（下 = 良い）", "ΔBMR [pt] (lower is better)") if cx == 0 else "")
        cur.sort(key=lambda t: t[0][0])
        a.line([p[0] for p, _ in cur], [p[1] for p, _ in cur], "#4d4d4d", w=3, marker=True)
        for p, lab in cur: f.t(a.X(p[0]) + 10, a.Y(p[1]) - 10, lab, 18, "start", "#4d4d4d")
        for p, c, r, hol in dots: a.dot(p[0], p[1], COL.get(c, "#888"), r, hollow=hol)
    return f.svg()


tr = []
if (EXT or COND) and any("r256_n3" in runs[n]["runs"] or "r256_n0" in runs[n]["runs"] for n in names):
    open(f"{D}/fig_tradeoff.svg", "w", encoding="utf-8").write(tradeoff(names)); tr.append("fig_tradeoff")
    for n in names:
        open(f"{D}/fig_tradeoff_{n}.svg", "w", encoding="utf-8").write(tradeoff([n])); tr.append(f"fig_tradeoff_{n}")

# ---------------------------------------------------------------- 書き込み量の印での結果（部分トレースで足りるか）
#   基準（CacheLib）の累積 SSD 書き込みが W に達した区間を「先頭 W の部分トレース」とし、その区間の各構成の累積値を全量と比べる。
#   W は traces.tsv の marks 列（例 1.5T,5T,500G。単位なしは TB）、無ければ環境変数 WRITE_MARKS_TB（config.sh、既定 1.5）。
def parse_w(x):
    x = x.strip().upper()
    if not x: return None
    m = {"P": 1e15, "T": 1e12, "G": 1e9}
    return float(x[:-1]) * m[x[-1]] if x[-1] in m else float(x) * 1e12


def cum_rows(n, c):  # P.interval.csv の累積の行（区間の順）
    p = f"{rundir.get((n, c), f'{D}/{n}/{c}')}/P.interval.csv"
    if not os.path.exists(p): return None
    return [{k: int(r[k]) for k in K} for r in csv.DictReader(open(p)) if r["kind"] == "cumulative"]


def at(row):  # 累積の行 → (BMR %, OHR %, 書き込み TB)
    return (100 * row["miss_bytes"] / row["req_bytes"] if row["req_bytes"] else None,
            100 * (row["dram_hits"] + row["ssd_hits"]) / row["reqs"] if row["reqs"] else None, row["flash_region_write_bytes"] / 1e12)


mk, mrows = [], []
for n in names:
    base = cum_rows(n, BASE)
    if not base or base[-1]["flash_region_write_bytes"] <= 0: continue
    C = {c: cum_rows(n, c) for c in others}; C = {c: v for c, v in C.items() if v and len(v) == len(base)}
    if not C: continue
    full_w = base[-1]["flash_region_write_bytes"]
    marks = [w for w in (parse_w(x) for x in (runs[n].get("marks") or os.environ.get("WRITE_MARKS_TB", "1.5")).split(",")) if w and w < full_w]
    idx_of = lambda w: next(i for i, r in enumerate(base) if r["flash_region_write_bytes"] >= w)
    fullv = {c: (at(C[c][-1])[0] - at(base[-1])[0], at(C[c][-1])[2] / at(base[-1])[2]) for c in C}
    for lab, i in [(f"{w / 1e12:g}TB", idx_of(w)) for w in marks] + [(T("全量", "full"), len(base) - 1)]:
        b0 = at(base[i])
        for c in C:
            v = at(C[c][i]); db = v[0] - b0[0]; wr = v[2] / b0[2] if b0[2] else None
            mrows.append(dict(trace=n, mark=lab, reqs=base[i]["reqs"], frac_reqs=base[i]["reqs"] / base[-1]["reqs"], config=c, label=nm(c),
                              bmr_pct=v[0], ohr_pct=v[1], write_TB=v[2], d_bmr_pt=db, write_ratio=wr, d_ohr_pt=v[1] - b0[1],
                              sign_bmr_ok=int((db < 0) == (fullv[c][0] < 0)), sign_write_ok=int((wr < 1) == (fullv[c][1] < 1)) if wr else ""))
    # 曲線：基準の書き込み量を対数で約 60 点に刻む
    w0 = max(base[0]["flash_region_write_bytes"], full_w * 1e-3, 1)
    grid = sorted({idx_of(min(full_w, w0 * (full_w / w0) ** (k / 59))) for k in range(60)})
    xs = [base[i]["flash_region_write_bytes"] / 1e12 for i in grid]
    f = Fig(); f.add_legend([(nm(c), COL.get(c, PAL[j % len(PAL)]), "line") for j, c in enumerate(others) if c in C] + [(T("指定した書き込み量", "write marks"), "#c0504a", "dash")])
    top = f.legend_h() + 50
    for k, (title, ylab, fn, ref) in enumerate([
            (T("ΔBMR（CacheLib 比、下が良い）", "ΔBMR vs. CacheLib (lower is better)"), "ΔBMR [pt]", lambda c, i: at(C[c][i])[0] - at(base[i])[0], 0.0),
            (T("SSD 書き込み（CacheLib = 1）", "SSD writes (CacheLib = 1)"), T("書き込みの比", "Write ratio"), lambda c, i: at(C[c][i])[2] / at(base[i])[2] if at(base[i])[2] else None, 1.0)]):
        ys = {c: [fn(c, i) for i in grid] for c in C}
        vals = [y for v in ys.values() for y in v if y is not None] + [ref]
        x0 = 130 + k * 785
        a = Ax(f, (x0, top, x0 + 640, 770), xr=(min(xs), max(xs)), xlog=min(xs) > 0 and max(xs) / min(xs) > 10, yr=span(vals) if ref == 0 else (0, max(vals) * 1.1),
               xlabel=T("CacheLib が SSD に書いた量 [TB]（= 先頭の部分トレースの長さ）", "Bytes CacheLib wrote to SSD [TB] (= prefix length)"), ylabel=ylab, title=title)
        a.hline(ref, col="#4d4d4d")
        for j, c in enumerate(others):
            if c in C: a.line(xs, ys[c], COL.get(c, PAL[j % len(PAL)]), w=3)
        for w in marks: a.vline(w / 1e12, label=f"{w / 1e12:g}TB")
    slug = f"fig_marks_{n}"; open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(f.svg()); mk.append(slug)
if mrows:
    with open(f"{D}/table_marks.csv", "w", newline="", encoding="utf-8") as fo:
        w_ = csv.DictWriter(fo, list(mrows[0])); w_.writeheader(); w_.writerows(mrows)
    md = [f"| {T('トレース', 'Trace')} | {T('印', 'Mark')} | {T('先頭の割合', 'Prefix')} | " + " | ".join(nm(c) for c in others) + " |", "|---|---|---|" + "---|" * len(others)]
    for (n, lab) in dict.fromkeys((r["trace"], r["mark"]) for r in mrows):
        rs = {r["config"]: r for r in mrows if r["trace"] == n and r["mark"] == lab}; r0 = next(iter(rs.values()))
        md.append(f"| {n} | {lab} | {100 * r0['frac_reqs']:.1f}% | " + " | ".join(
            "–" if c not in rs else f"{rs[c]['d_bmr_pt']:+.3f} / {rs[c]['write_ratio']:.3f}" + ("" if rs[c]["sign_bmr_ok"] and rs[c]["sign_write_ok"] else " ✗") for c in others) + " |")
    open(f"{D}/table_marks.md", "w", encoding="utf-8").write("\n".join(md) + "\n\n" + T(
        "各セル = ΔBMR [pt]（構成 − CacheLib）/ 書き込みの比（構成 ÷ CacheLib）。印 = CacheLib の累積 SSD 書き込みがその量に達した時点（先頭の割合 = そこまでの要求の割合）。✗ = 全量と差の向きが違う。\n",
        "Each cell: ΔBMR [pt] (config − CacheLib) / write ratio (config ÷ CacheLib), at the point where CacheLib has written that much to the SSD. ✗ = sign differs from the full trace.\n"))
png = to_png(D, list(FIGS) + tl + tr + mk)

# ---------------------------------------------------------------- 説明のページ
def mean_of(fn, c):
    v = [fn(runs[n]["runs"][c]) for n in names if c in runs[n]["runs"]]
    return sum(v) / len(v) if v else None
pts = "；".join(f"{nm(c)}: BMR {mean_of(BMR, c):.2f}%、書込 {sum(TB(runs[n]['runs'][c]) for n in names if c in runs[n]['runs']):.1f} TB、OHR {mean_of(OHR, c):.1f}%"
                for c in cfgs)
html = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>論文用の比較</title>
<style>:root{{--bg:#fbfaf7;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9}}@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.75 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}}main{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}}
img{{width:100%;background:#fff;border:1px solid var(--line);border-radius:6px}}pre{{overflow-x:auto;border:1px solid var(--line);padding:8px;font-size:12.5px}}.m{{color:var(--mut);font-size:13px}}</style></head><body><main>
<h1>{f"頑健性の条件 {COND} での比較" if COND else "追加の比較：CacheLib の n=0・n=3 と提案" if EXT else "論文用の比較（図 1 枚・表 1 つ）"}</h1><p class="m">トレース {len(names)} 本：{", ".join(names)}。</p>
""" + ("<h2>交換関係：fig_tradeoff.svg（トレースごとは fig_tradeoff_&lt;trace&gt;.svg）</h2><img src='fig_tradeoff.svg' alt='SSD 書き込みと BMR の交換関係'>"
       "<p class='m'>横 = SSD 書き込み（CacheLib n=1 = 1、0 から）、縦 = ΔBMR [pt]（CacheLib n=1 との差、下が良い）。灰の線 = CacheLib（R=256MiB）の n=0〜3。"
       "書き直しを増やす（n を下げる）ほど書き込みが増えて BMR が下がる、という CacheLib の交換関係。濃い青 = RU＋分類（自動）、白抜き = RU＋分類（手動）と分類なしの n=auto。"
       "点が灰の線より左下にあれば、同じ BMR を少ない書き込みで出している。</p>" if tr else "") + f"""<h2>図：fig_main.svg（3 面まとめ）{" / .png" if png else ""}</h2><img src="fig_main.svg" alt="BMR・SSD 書き込み量・OHR の比較"><h2>図：fig_rel.svg（CacheLib 比の 3 面まとめ）</h2><img src="fig_rel.svg" alt="CacheLib 比の BMR・書き込み・OHR"><p class="m">左 = ΔBMR（構成 − CacheLib、0 = CacheLib、負が良い）。中 = SSD 書き込み（CacheLib = 1 に正規化、1 未満が良い）。右 = ΔOHR（参考）。1 面ずつの図：fig_bmr / fig_write / fig_ohr（値そのもの）、fig_dbmr / fig_nwrite / fig_dohr（CacheLib 比）。</p>
<p><b>要点</b>（BMR・OHR はトレースの平均、書き込みは合計）：{pts}。</p>
<p class="m">左 = BMR（低いほど良い）。中 = SSD に書いたバイト（受け入れ＋再挿入＋詰め物、低いほど良い。トレースごとに規模が違うので平均は出さない）。右 = OHR（参考）。
濃い灰 = 基準（CacheLib 相当、Region 256 MiB、分類なし、再挿入 n=1）。薄い灰 = Region を RU（626.5 MiB）に合わせただけ（DLWA = 1 だが、Region が大きいぶん道連れと書き直しが増える）。
青 = RU＋書き込み時のサイズ分類（手動 / 自動）。橙 = 256 MiB＋分類（分類の効果が Region の大きさによらないかの確認）。</p>
""" + ("<h2>補足：時間変化</h2><p class='m'>fig_ops_* = 横軸が処理した要求数（BMR・OHR は区間ごと、SSD 書き込みは累積）。"
       "fig_wr_* = 横軸がその構成の累積 SSD 書き込み量（BMR・OHR はそこまでの累積。同じ量を書いた時点での比較）。破線 = 基準。</p>"
       + "".join(f'<h3>{s}</h3><img src="{s}.svg" alt="{s}">' for s in tl) if tl else "") + f"""
""" + ("<h2>書き込み量の印での結果（部分トレースで足りるか）</h2><p class='m'>fig_marks_&lt;trace&gt;：横 = CacheLib が SSD に書いた量（先頭の部分トレースの長さ）、縦 = ΔBMR と書き込みの比。"
       "線が右端（全量）の値に早く落ち着き、赤い縦線（指定した書き込み量）の時点で全量と同じ側にあれば、その長さの部分トレースで結論が言える。数値は table_marks.md / .csv（✗ = 全量と向きが違う）。</p>"
       + "".join(f'<img src="{s_}.svg" alt="{s_}">' for s_ in mk) + (f"<pre>{open(f'{D}/table_marks.md', encoding='utf-8').read()}</pre>" if mrows else "") if mk else "") + f"""
<h2>表：table_main.md / .tex / .csv</h2><pre>{open(f"{D}/table_main.md", encoding="utf-8").read()}</pre></main></body></html>"""
open(f"{D}/index.html", "w", encoding="utf-8").write(html)
print(f"paper: {len(names)} 本 → {D}/fig_main.svg・fig_rel.svg（と 1 面ずつ 6 枚）, 時間変化 {len(tl)} 枚（fig_ops_* / fig_wr_*）{f'、交換関係 {len(tr)} 枚（fig_tradeoff*）' if tr else ''}{'（.png も）' if png else ''}, table_main.(md|tex|csv), index.html")
