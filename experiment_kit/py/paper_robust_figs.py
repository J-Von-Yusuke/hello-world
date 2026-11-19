#!/usr/bin/env python3
"""paper_robust_figs.py — 頑健性の確認（./run.sh paper-robust）の結果を図と表にする（work/paper/robust/ に出す）。

  python3 paper_robust_figs.py <WORK>          PAPER_LANG=en で英語の文字

条件ごとに、提案（RU＋分類の自動、ru_auto）と CacheLib（R=256MiB、分類なし）の差を並べる:
  既定        work/paper の ru_auto と r256_base（n3 は work/paper/ext の r256_n3）
  chunk=0     _c0：片に分けず、物を 1 Region の中に連続で置く（片を 1 つ失って物ごと消える RegionFlash の扱いが起きない）。
              実機の置き方ではない（CacheLib の piece-wise caching は片を独立の項目にする）。R より大きい物は入らない
  chunk=0m    _c0m：_c0 に、受け入れの上限を全構成で 256 MiB にそろえたもの。256 MiB を超える物（nha ではバイトの 44%）は
              全構成でミスなので、256 MiB 以下の物の中での傾向だけを見る診断
  上限なし    _f1：再挿入の上限なし（max_frac=1）
  種 2 / 種 3 _s2 / _s3：乱数の種
出力: fig_robust.svg（提案 − CacheLib n=1）、fig_robust_n3.svg（提案 − CacheLib n=3。n=3 のある条件だけ）、
      _w0 = 目的を BHR だけにした提案、_rubase = R=RU にしただけ（ru_base）、fig_robust_vs_rubase.svg = 提案 − ru_base
      （Region を RU にした効果を除いた、分類＋下限＋n=auto の効果。R の違いによる片の扱いの差が _c0m で消えるかを見る）。
      それぞれ左 = ΔBMR [pt]（0 = 比べる相手、下が良い）、右 = 書き込みの比（比べる相手 = 1、1 未満が良い）。table_robust.csv / .md。
どの条件でも棒が同じ側にあれば、結論は RegionFlash の作りや乱数に頼っていない。標準ライブラリだけ。"""
import csv, glob, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, span, to_png

WORK = sys.argv[1]; EN = os.environ.get("PAPER_LANG", "ja") == "en"
T = lambda ja, en: en if EN else ja
P = f"{WORK}/paper"; D = f"{P}/robust"; os.makedirs(D, exist_ok=True)
COND = [("", T("既定", "default"), "#1f4e8c"), ("_c0", T("chunk=0", "chunk=0"), "#9fd3b0"),
        ("_c0m", T("chunk=0・受入上限 256MiB", "chunk=0, admit ≤ 256MiB"), "#3f9a64"), ("_f1", T("再挿入の上限なし", "no rewrite cap"), "#c9683a"),
        ("_s2", T("乱数の種 2", "seed 2"), "#8a5fb5"), ("_s3", T("乱数の種 3", "seed 3"), "#b8a02a")]

runs = {}   # runs[trace][config] = tune_one の要約（work/paper → ext → robust の順に重ねる）
for src in (P, f"{P}/ext", D):
    for f in sorted(glob.glob(f"{src}/*.json")):
        d = json.load(open(f))
        if isinstance(d, dict) and "runs" in d: runs.setdefault(d["name"], {}).update(d["runs"])
# 比べる組：(構成, 比べる相手, 図の名前, 題の構成名)。構成と相手の両方の結果がある条件だけを描く
PROPS = [("ru_auto", "", T("提案", "proposal")), ("ru_auto_w0", "_w0", T("提案・BHR のみ", "proposal, BHR only")),
         ("ru_base", "_rubase", T("R=RU のみ", "R=RU only"))]
PAIRS = [(p, b, f"{slug}{ps}", lab, ref) for p, ps, lab in PROPS
         for b, slug, ref in (("r256_base", "fig_robust", T("CacheLib n=1", "CacheLib n=1")), ("r256_n3", "fig_robust_n3", T("CacheLib n=3", "CacheLib n=3")))]
PAIRS.append(("ru_auto", "ru_base", "fig_robust_vs_rubase", T("提案", "proposal"), T("R=RU のみ", "R=RU only")))
names = sorted(n for n, R in runs.items() if any(f"{p}{s}" in R and f"{b}{s}" in R for s, _, _ in COND[1:] for p, b, *_ in PAIRS))
if not names: sys.exit(f"{D} に頑健性の結果が無い（./run.sh paper-robust がまだ）")

BMR = lambda r: 100 * r["bmr"]
WR = lambda r: r["write"] * r["req_bytes"]


def diffs(base, prop):  # {条件: ([ΔBMR], [書込比])}（トレース順、2 本以上なら最後に平均）。prop = 提案の構成名（条件なしの名前）
    out = {}
    for suf, lab, col in COND:
        b, p = f"{base}{suf}", f"{prop}{suf}"
        db = [BMR(runs[n][p]) - BMR(runs[n][b]) if p in runs[n] and b in runs[n] else None for n in names]
        wr = [WR(runs[n][p]) / WR(runs[n][b]) if p in runs[n] and b in runs[n] and WR(runs[n][b]) else None for n in names]
        if all(x is None for x in db): continue
        if len(names) > 1:
            for v in (db, wr):
                ok = [x for x in v if x is not None]; v.append(sum(ok) / len(ok) if ok else None)
        out[suf] = (db, wr)
    return out


def fig(base, title_ref, prop, title_prop):
    dd = diffs(base, prop)
    if not dd: return None, dd
    cats = names + ([T("平均", "Mean")] if len(names) > 1 else [])
    f = Fig(); f.add_legend([(lab, col, "box") for suf, lab, col in COND if suf in dd])
    top = f.legend_h() + 50
    a1 = Ax(f, (130, top, 770, 760), yr=span([x for v in dd.values() for x in v[0] if x is not None]), cats=cats,
            ylabel=T("ΔBMR [pt]（下 = 良い）", "ΔBMR [pt] (lower is better)"), title=f"ΔBMR（{title_prop} − {title_ref}）")
    a1.bars([(lab, col, dd[suf][0], None) for suf, lab, col in COND if suf in dd], d=2)
    hi = max([x for v in dd.values() for x in v[1] if x is not None] + [1])
    a2 = Ax(f, (920, top, 1560, 760), yr=(0, hi * 1.1), cats=cats,
            ylabel=T("書き込みの比（下 = 良い）", "Write ratio (lower is better)"), title=T(f"SSD 書き込み（{title_ref} = 1）", f"SSD writes ({title_ref} = 1)"))
    a2.bars([(lab, col, dd[suf][1], None) for suf, lab, col in COND if suf in dd], d=3, sign=False)
    a2.hline(1.0, col="#4d4d4d", label=f"{title_ref} = 1")
    return f.svg(), dd


rows, made = [], []
for prop, base, slug, tp, ref in PAIRS:   # 提案（BHR+0.1·OHR / BHR だけ）・R=RU のみ × CacheLib n=1 / n=3、提案 − R=RU のみ
    svg, dd = fig(base, ref, prop, tp)
    if not svg: continue
    open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(svg); made.append(slug)
    for suf, lab, _ in COND:
        if suf not in dd: continue
        for i, n in enumerate(names):
            rows.append(dict(proposal=prop, reference=base, condition=lab, trace=n, d_bmr_pt=dd[suf][0][i], write_ratio=dd[suf][1][i]))
with open(f"{D}/table_robust.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, ["proposal", "reference", "condition", "trace", "d_bmr_pt", "write_ratio"]); w.writeheader(); w.writerows(rows)
md = [f"| {T('構成', 'Config')} | {T('比べる相手', 'Reference')} | {T('条件', 'Condition')} | " + " | ".join(names) + " |", "|---|---|---|" + "---|" * len(names)]
for prop, base, *_ in PAIRS:
    for _, lab, _ in COND:
        rs = {r["trace"]: r for r in rows if r["proposal"] == prop and r["reference"] == base and r["condition"] == lab}
        if rs:
            md.append(f"| {prop} | {base} | {lab} | " + " | ".join(
                "–" if n not in rs or rs[n]["d_bmr_pt"] is None else f"{rs[n]['d_bmr_pt']:+.3f} / {rs[n]['write_ratio']:.3f}" for n in names) + " |")
open(f"{D}/table_robust.md", "w", encoding="utf-8").write("\n".join(md) + "\n\n" + T(
    "各セル = ΔBMR [pt]（構成 − 比べる相手、負 = 良い）/ 書き込みの比（構成 ÷ 比べる相手、1 未満 = 良い）。\n",
    "Each cell: ΔBMR [pt] (config − reference; negative = better) / write ratio (config ÷ reference; < 1 = better).\n"))
to_png(D, made)
print(f"paper-robust: {len(names)} 本 → {D}/" + "、".join(f"{s}.svg" for s in made) + "、table_robust.(csv|md)")
