#!/usr/bin/env python3
"""paper_sens_figs.py — 感度（./run.sh paper-sens）の結果を図と表にする（work/paper/sens/ に出す）。11-20。

  python3 paper_sens_figs.py <WORK>          PAPER_LANG=en で英語の文字

SSD 容量・DRAM・RU（Region の大きさ）を一つずつ変えたとき、各方式と CacheLib（r256-none-nat-n1、R=256 MiB・分類なし・n=1）の差が
同じ向きに残るかを見る。既定の点は work/paper の結果、変えた点は work/paper/sens/<点>/ の結果（<点> = ssd_x<倍>・dram_<SSD÷DRAM>・
ru_<MiB>M）。RU の点では CacheLib は回さない（Region の大きさを自分で持つので既定の点と同じ run）ので、既定の点の値と比べる。
方式は sens/methods.tsv（run.sh が config.sh の PAPER_SENS_METHODS から作る）の順と名前。
出力: fig_sens_<トレース>.svg（上 = ΔBMR [pt]、下 = SSD 書き込みの比。左から SSD 容量・DRAM・RU）、table_sens.csv / .md。
書き込みはアプリ層（Region に書いたバイト）。CacheLib 側の DLWA（実機で R=256 MiB・RU 626.5 MiB のとき 1.4）は掛けていない。
RU を変えると CacheLib の DLWA も変わるので、NAND 書き込みで比べるときは RU ごとに実機の値か見積りを掛ける。標準ライブラリだけ。"""
import csv, glob, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, PAL, span, to_png, method_label

WORK = sys.argv[1]; EN = os.environ.get("PAPER_LANG", "ja") == "en"
T = lambda ja, en: en if EN else ja
P = f"{WORK}/paper"; S = f"{P}/sens"
from ids import BASE
if not os.path.exists(f"{S}/methods.tsv"): sys.exit(f"{S}/methods.tsv が無い（./run.sh paper-sens がまだ）")
SEL, NAME = [], {}
for r in (l.rstrip("\n").split("\t") for l in open(f"{S}/methods.tsv", encoding="utf-8") if l.strip()):
    SEL.append(r[0]); NAME[r[0]] = r[1] if len(r) > 1 and r[1] else method_label(r[2])[EN] if len(r) > 2 and r[2] else r[0]
METH = [c for c in SEL if c != BASE]
COL = {c: PAL[i % len(PAL)] for i, c in enumerate(METH)}


def load(d):  # {trace: (runs, run の設定 {ssd, dram, region})}
    out = {}
    for f in sorted(glob.glob(f"{d}/*.json")):
        x = json.load(open(f))
        if not (isinstance(x, dict) and "runs" in x): continue
        c = f"{d}/{x['name']}/config.json"
        out[x["name"]] = (x["runs"], json.load(open(c)) if os.path.exists(c) else {})
    return out


DEF = load(P)
AXES = [("ssd", T("SSD 容量（既定 = 1 倍）", "SSD capacity (default = 1×)"), lambda c, c0: c["ssd"] / c0["ssd"]),
        ("dram", T("SSD ÷ DRAM", "SSD / DRAM"), lambda c, c0: c["ssd"] / c["dram"]),
        ("ru", T("RU = Region [MiB]", "RU = Region [MiB]"), lambda c, c0: c["region"] / 2**20)]
PTS = {a: [] for a, *_ in AXES}   # 軸 → [点の場所]
for d in sorted(glob.glob(f"{S}/*/")):
    k = os.path.basename(d.rstrip("/"))
    m = re.match(r"(ssd|dram|ru)_", k)
    if m and glob.glob(f"{d}*.json"): PTS[m.group(1)].append(load(d.rstrip("/")))
names = [n for n in DEF if any(n in p for v in PTS.values() for p in v)]
if not names: sys.exit(f"{S} に感度の結果が無い（./run.sh paper-sens がまだ）")

rows = []   # 1 行 = 軸・x・トレース・方式
for n in names:
    R0, c0 = DEF[n]
    for a, _, fx in AXES:
        for runs, cfg in [(R0, c0)] + [p[n] for p in PTS[a] if n in p]:
            base = runs.get(BASE) or (R0.get(BASE) if a == "ru" else None)   # RU の点の CacheLib = 既定の点
            if not base or not cfg or not c0: continue
            for m in METH:
                r = runs.get(m)
                if not r: continue
                rows.append(dict(axis=a, x=round(fx(cfg, c0), 4), trace=n, method=m, d_bmr_pt=100 * (r["bmr"] - base["bmr"]),
                                 d_ohr_pt=100 * (r["ohr"] - base["ohr"]), write_ratio=r["write"] / base["write"] if base["write"] else None,
                                 default=int(cfg is c0)))

made = []
for n in names:
    f = Fig(1600, 980); f.add_legend([(NAME[m], COL[m], "line") for m in METH])
    top = f.legend_h() + 60
    for j, (a, xl, _) in enumerate(AXES):
        rs = [r for r in rows if r["trace"] == n and r["axis"] == a]
        if not rs: continue
        xs = sorted({r["x"] for r in rs}); xr = (min(xs) * 0.9, max(xs) * 1.05) if len(xs) > 1 else (xs[0] * 0.5, xs[0] * 1.5)
        x0 = 140 + j * 490
        for i, (key, yl, ref) in enumerate((("d_bmr_pt", T("ΔBMR [pt]（下 = 良い）", "ΔBMR [pt] (lower is better)"), 0.0),
                                             ("write_ratio", T("書き込みの比（CacheLib = 1）", "Write ratio (CacheLib = 1)"), 1.0))):
            ys = [r[key] for r in rs if r[key] is not None] + [ref]
            y0 = top + i * ((980 - top - 70) // 2)
            ax = Ax(f, (x0, y0 + 30, x0 + 400, y0 + (980 - top - 70) // 2 - 50), xr=xr, yr=span(ys, zero=(i == 0)),
                    ylabel=yl if j == 0 else "", title=xl if i == 0 else None)
            ax.hline(ref, col="#4d4d4d", dash=True)
            dx = [r["x"] for r in rs if r["default"]]
            if dx: ax.vline(dx[0], col="#bbbbbb")
            for m in METH:
                pts = sorted((r["x"], r[key]) for r in rs if r["method"] == m)
                if pts: ax.line([p[0] for p in pts], [p[1] for p in pts], COL[m], marker=True)
    f.t(800, 970, T(f"{n}：縦の灰線 = 既定の点。RU の点の CacheLib は既定の点の値。書き込みは DLWA を掛けていない",
                    f"{n}: grey vertical = default point. CacheLib at RU points = default point. Writes without DLWA"), 20, "middle")
    slug = f"fig_sens_{re.sub(r'[^A-Za-z0-9_]', '_', n)}"
    open(f"{S}/{slug}.svg", "w", encoding="utf-8").write(f.svg()); made.append(slug)

with open(f"{S}/table_sens.csv", "w", newline="", encoding="utf-8") as fo:
    w = csv.DictWriter(fo, ["axis", "x", "trace", "method", "d_bmr_pt", "d_ohr_pt", "write_ratio", "default"]); w.writeheader(); w.writerows(rows)
md = [f"| {T('軸', 'Axis')} | x | {T('トレース', 'Trace')} | " + " | ".join(NAME[m] for m in METH) + " |", "|---|---|---|" + "---|" * len(METH)]
for a, *_ in AXES:
    for n in names:
        for x in sorted({r["x"] for r in rows if r["axis"] == a and r["trace"] == n}):
            c = {r["method"]: r for r in rows if r["axis"] == a and r["trace"] == n and r["x"] == x}
            md.append(f"| {a} | {x:g}{'*' if any(r['default'] for r in c.values()) else ''} | {n} | " + " | ".join(
                f"{c[m]['d_bmr_pt']:+.3f} / {c[m]['write_ratio']:.3f}" if m in c and c[m]["write_ratio"] is not None else "–" for m in METH) + " |")
open(f"{S}/table_sens.md", "w", encoding="utf-8").write("\n".join(md) + "\n\n" + T(
    "各セル = ΔBMR [pt]（方式 − CacheLib、負 = 良い）/ 書き込みの比（方式 ÷ CacheLib、DLWA なし）。* = 既定の点。\n",
    "Each cell: ΔBMR [pt] (method − CacheLib; negative = better) / write ratio (method ÷ CacheLib, no DLWA). * = default point.\n"))
to_png(S, made)
print(f"paper-sens: {len(names)} 本 × {len(METH)} 方式 → {S}/" + "、".join(f"{s}.svg" for s in made) + "、table_sens.(csv|md)")
