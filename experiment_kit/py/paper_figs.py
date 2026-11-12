#!/usr/bin/env python3
"""paper_figs.py — 論文用の比較（./run.sh paper の結果 work/paper/<name>.json）から、図 1 枚と表 1 つを作る。

  python3 paper_figs.py <WORK>          PAPER_LANG=en で英語の文字

比べる構成（config.sh の PAPER_JOBS）:
  r256_base     Region 256 MiB（CacheLib の上限）・分類なし・再挿入 n=1        ← 基準（いまの CacheLib）
  ru_base       Region = RU（626.5 MiB）・分類なし・再挿入 n=1                ← 上限を外して RU に合わせただけ
  ru_ff90_h3    Region = RU・サイズ分類・大物の下限 0.9・再挿入 n=3（手で調整）  ← 提案
  ru_auto       Region = RU・サイズ分類（SplitThreshold）・下限の山登り・再挿入 n=auto（全部自動） ← 提案
  r256_ff90_h3, r256_auto  同じ方式を 256 MiB で                              ← 分類の効果が Region の大きさによらないか
指標（図は値そのもの、表は基準との差 = 構成 − 基準）:
  BMR   バイトミス率 [%]（低いほど良い：元サーバへの転送）
  書き込み SSD に書いたバイト [TB]（受け入れ＋再挿入＋詰め物。低いほど良い）
  OHR   物のヒット率 [%]（高いほど良い。参考）
  アプリ層の書き込み増幅 = SSD に書いたバイト ÷ 受け入れたバイト（1 = 書き直しなし。表の基準の列）
出力（work/paper/）: fig_main.svg（3 面まとめ）と fig_bmr.svg / fig_write.svg / fig_ohr.svg（1 面ずつ。.png は変換器があれば）, table_main.csv / .md / .tex, index.html。標準ライブラリだけ。"""
import csv, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, PAL, span, to_png

WORK = sys.argv[1]; EN = os.environ.get("PAPER_LANG", "ja") == "en"
D = f"{WORK}/paper"
BASE = "r256_base"
ORDER = ["r256_base", "ru_base", "ru_ff90_h3", "ru_auto", "r256_ff90_h3", "r256_auto"]   # 図と表に並べる順
NAME = {
    "r256_base": ("CacheLib（R=256MiB）", "CacheLib (R=256MiB)"),
    "ru_base": ("R=RU", "R=RU"),
    "ru_ff90_h3": ("R=RU＋分類（手動）", "R=RU + class. (manual)"),
    "ru_auto": ("R=RU＋分類（自動）", "R=RU + class. (auto)"),
    "r256_ff90_h3": ("R=256MiB＋分類（手動）", "R=256MiB + class. (manual)"),
    "r256_auto": ("R=256MiB＋分類（自動）", "R=256MiB + class. (auto)"),
}
COL = {"r256_base": "#4d4d4d", "ru_base": "#a6a6a6", "ru_ff90_h3": "#3b7dbf", "ru_auto": "#1f4e8c",
       "r256_ff90_h3": "#e3a37c", "r256_auto": "#c9683a"}
nm = lambda c: NAME.get(c, (c, c))[EN]
T = lambda ja, en: en if EN else ja

runs = {}
for f in sorted(glob.glob(f"{D}/*.json")):
    d = json.load(open(f))
    if isinstance(d, dict) and BASE in d.get("runs", {}): runs[d["name"]] = d
if not runs: sys.exit(f"{D}/*.json に {BASE} を含む結果が無い（./run.sh paper がまだ）")
names = sorted(runs, key=lambda n: (runs[n].get("category", ""), runs[n].get("source", ""), n))
cfgs = [c for c in ORDER if any(c in runs[n]["runs"] for n in names)]
others = [c for c in cfgs if c != BASE]

# 指標（図は値そのもの）
BMR = lambda r: 100 * r["bmr"]
OHR = lambda r: 100 * r["ohr"]
TB = lambda r: r["write"] * r["req_bytes"] / 1e12            # SSD に書いたバイト [TB]
AWAF = lambda r: r["write"] / r["admit"] if r.get("admit") else None

# ---------------------------------------------------------------- 表（CSV は全部、MD / TeX は主な列）
rows = []
for n in names:
    R = runs[n]["runs"]; b = R[BASE]
    for c in cfgs:
        if c not in R: continue
        r = R[c]
        rows.append(dict(trace=n, category=runs[n].get("category", ""), source=runs[n].get("source", ""), config=c, label=nm(c),
                         bmr_pct=BMR(r), ohr_pct=OHR(r), ssd_write_TB=TB(r), write_per_req_byte=r["write"],
                         reinsert_per_req_byte=r["reinsert"], app_waf=AWAF(r),
                         d_bmr_pt=BMR(r) - BMR(b), d_write_pct=100 * (TB(r) / TB(b) - 1) if TB(b) else None, d_ohr_pt=OHR(r) - OHR(b)))
with open(f"{D}/table_main.csv", "w", newline="") as f:
    w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)


def cell(n, c, tex=False):
    R = runs[n]["runs"]
    if c not in R: return "–"
    b, r = R[BASE], R[c]
    v = (BMR(r) - BMR(b), 100 * (TB(r) / TB(b) - 1) if TB(b) else float("nan"), OHR(r) - OHR(b))
    s = (f"{v[0]:+.2f}", f"{v[1]:+.1f}", f"{v[2]:+.1f}")
    return " / ".join(f"${x}$" for x in s) if tex else " / ".join(s)


hdr = [T("トレース", "Trace"), T("基準 BMR [%]", "Base BMR [%]"), T("基準 書込 [TB]", "Base writes [TB]"),
       T("基準 OHR [%]", "Base OHR [%]"), T("基準 書込増幅", "Base app. WAF")] + [nm(c) for c in others]
md = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
tex = [r"\begin{table*}[t]", r"\centering\small", r"\setlength{\tabcolsep}{4pt}",
       r"\begin{tabular}{l" + "r" * (4 + len(others)) + "}", r"\toprule",
       " & ".join(hdr).replace("%", r"\%").replace("＋", "+") + r" \\", r"\midrule"]
for n in names:
    b = runs[n]["runs"][BASE]
    base = [f"{BMR(b):.2f}", f"{TB(b):.1f}", f"{OHR(b):.1f}", f"{AWAF(b):.2f}" if AWAF(b) else "–"]
    md.append("| " + " | ".join([n] + base + [cell(n, c) for c in others]) + " |")
    tex.append(" & ".join([n.replace("_", r"\_")] + base + [cell(n, c, True) for c in others]) + r" \\")
cap = T("各セル = 基準との差（構成 − 基準）：BMR [pt]（負 = 良い）/ SSD 書き込み [%]（負 = 良い）/ OHR [pt]（正 = 良い）。"
        "基準 = CacheLib 相当（Region 256 MiB、分類なし、再挿入 n=1）。書込増幅 = SSD に書いたバイト ÷ 受け入れたバイト。",
        "Each cell: difference from the baseline (config − baseline): BMR [pt] (negative = better) / SSD writes [%] (negative = better) / "
        "OHR [pt] (positive = better). Baseline: CacheLib-like (256 MiB regions, no classification, reinsertion n=1). "
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
PANELS = {  # slug: (題, 縦軸, 関数, 平均をつけるか, 小数)
    "bmr": (T("BMR（低いほど良い）", "BMR (lower is better)"), "BMR [%]", BMR, True, 2),
    "write": (T("SSD 書き込み量（低いほど良い）", "SSD writes (lower is better)"), T("書き込み [TB]", "Writes [TB]"), TB, False, 1),
    "ohr": (T("OHR（参考。高いほど良い）", "OHR (for reference)"), "OHR [%]", OHR, True, 1),
}


def draw(keys):  # keys の面を横に並べた 1 枚の図
    f = Fig(); f.add_legend([(nm(c), COL.get(c, PAL[i % len(PAL)]), "box") for i, c in enumerate(cfgs)])
    top, x0, gap = f.legend_h() + 50, 110, 115
    W = (1600 - x0 - 30 - gap * (len(keys) - 1)) // len(keys)
    for k, key in enumerate(keys):
        title, ylab, fn, mean, d = PANELS[key]
        data = {c: series(c, fn, mean) for c in cfgs}
        vals = [x for v in data.values() for x in v if x is not None]
        lo, hi = span(vals, zero=False)
        lo = max(0.0, lo - (hi - lo) * 0.5)   # 差が見えるよう 0 からにはしないが、下限は 0
        a = Ax(f, (x0 + k * (W + gap), top, x0 + k * (W + gap) + W, 760), yr=(lo, hi), cats=cats_m if mean else names,
               ylabel=ylab, title=title)
        a.bars([(nm(c), COL.get(c, PAL[i % len(PAL)]), data[c], None) for i, c in enumerate(cfgs)], d=d, sign=False)
    return f.svg()


FIGS = {"fig_main": list(PANELS), **{f"fig_{k}": [k] for k in PANELS}}   # 3 面まとめた 1 枚と、1 面ずつの 3 枚
for slug, keys in FIGS.items():
    open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(draw(keys))
png = to_png(D, list(FIGS))

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
<h1>論文用の比較（図 1 枚・表 1 つ）</h1><p class="m">トレース {len(names)} 本：{", ".join(names)}。</p>
<h2>図：fig_main.svg（3 面まとめ）{" / .png" if png else ""}</h2><img src="fig_main.svg" alt="BMR・SSD 書き込み量・OHR の比較"><p class="m">1 面ずつの図：fig_bmr.svg・fig_write.svg・fig_ohr.svg（論文で 1 つだけ使うとき）。</p>
<p><b>要点</b>（BMR・OHR はトレースの平均、書き込みは合計）：{pts}。</p>
<p class="m">左 = BMR（低いほど良い）。中 = SSD に書いたバイト（受け入れ＋再挿入＋詰め物、低いほど良い。トレースごとに規模が違うので平均は出さない）。右 = OHR（参考）。
濃い灰 = 基準（CacheLib 相当、Region 256 MiB、分類なし、再挿入 n=1）。薄い灰 = Region を RU（626.5 MiB）に合わせただけ（DLWA = 1 だが、Region が大きいぶん道連れと書き直しが増える）。
青 = RU＋書き込み時のサイズ分類（手動 / 自動）。橙 = 256 MiB＋分類（分類の効果が Region の大きさによらないかの確認）。</p>
<h2>表：table_main.md / .tex / .csv</h2><pre>{open(f"{D}/table_main.md", encoding="utf-8").read()}</pre></main></body></html>"""
open(f"{D}/index.html", "w", encoding="utf-8").write(html)
print(f"paper: {len(names)} 本 → {D}/fig_main.svg, fig_bmr.svg, fig_write.svg, fig_ohr.svg{'（.png も）' if png else ''}, table_main.(md|tex|csv), index.html")
