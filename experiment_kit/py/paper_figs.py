#!/usr/bin/env python3
"""paper_figs.py — 論文用の比較（./run.sh paper の結果 work/paper/<name>.json）から、図 1 枚と表 1 つを作る。

  python3 paper_figs.py <WORK> [--ext | --cond c0|c0m|f1|s2|s3]  PAPER_LANG=en で英語の文字。--ext = paper-ext の結果を足して work/paper/ext/ に出す。
                                       --cond = paper-robust のその条件の構成だけで同じ一式を work/paper/robust/<条件>/ に出す

比べる構成 = config.sh の組（PAPER_*_METHODS）で選んだ方式のうち結果のあるもの（run.sh が <場所>/methods.tsv に残す。その順と名前で並べる）。
方式の名前は部品の並び R-閾値-配分-再挿入[-adm][~変種]（config.sh の「方式の名前の付け方」）。図の名前は指定から作る（figures.method_label）。
基準 = r256-none-nat-n1（いまの CacheLib）、現在の提案 = ru-otsu-ghost-cb。役割の決まった方式と色は ids.py。
指標（図は値そのもの、表は基準との差 = 構成 − 基準）:
  BMR   バイトミス率 [%]（低いほど良い：元サーバへの転送）
  書き込み SSD に書いたバイト [TB]（受け入れ＋再挿入＋詰め物。低いほど良い）
  OHR   物のヒット率 [%]（高いほど良い。参考）
  アプリ層の書き込み増幅 = SSD に書いたバイト ÷ 受け入れたバイト（1 = 書き直しなし。表の基準の列）
出力（work/paper/）: fig_main.svg（3 面まとめ）と fig_bmr.svg / fig_write.svg / fig_ohr.svg（1 面ずつ）、
  補足の時間変化 fig_ops_<trace>.svg（横軸 = 要求数）/ fig_wr_<trace>.svg（横軸 = 累積 SSD 書き込み。PAPER_BINS = 区間のまとめ数、既定 100）。.png は変換器があれば, table_main.csv / .md / .tex, index.html。標準ライブラリだけ。"""
import csv, glob, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, PAL, INK, fmt, span, to_png, tw, method_label
from ids import BASE, PROPOSAL, CURVE, LCURVE, PT_BIG, PT_HOLLOW, ORTH, VAR, COL

WORK = sys.argv[1]; EN = os.environ.get("PAPER_LANG", "ja") == "en"
EXT = "--ext" in sys.argv   # paper-ext: CacheLib の n=0・n=3（work/paper/ext/）を足した比較。図と表は work/paper/ext/ に出す
FINAL2 = "--final2" in sys.argv   # paper-final2: 同じ読み方で、図と表は work/paper/final2/
FINAL = "--final" in sys.argv or FINAL2   # paper-final: work/paper と work/paper/ext の結果から final/methods.tsv の方式だけ。図と表は work/paper/final/
COND = sys.argv[sys.argv.index("--cond") + 1] if "--cond" in sys.argv else ""   # 頑健性の条件（c0 / f1 / s2 / s3）
# 11-20 トレースの群ごと：--group <category>/<source> = traces.tsv の category・source がそれの物だけで同じ一式を <図の場所>/groups/<category>__<source>/ に。
# --list-groups = 結果のある群を 1 行ずつ出して終わる（run.sh paper-figs が群ごとに呼ぶ）。群をまたいだ全体は今まで通り <図の場所> に
GROUP = sys.argv[sys.argv.index("--group") + 1] if "--group" in sys.argv else ""
LIST_GROUPS = "--list-groups" in sys.argv
# 11-24 図の組：--figset "名前:方式 方式 .." = 組の選択のうち並べた方式だけ（基準は必ず）で同じ一式を <図の場所>/figs_<名前>/ に。
# 系列が多すぎて図が潰れるため（final2 は 24 方式）。色は組全体の並びで決める（どの図の組でも 1 方式 1 色）
FIGSET = sys.argv[sys.argv.index("--figset") + 1] if "--figset" in sys.argv else ""
FS_NAME, _, FS_IDS = FIGSET.partition(":")
if COND:   # work/paper/robust の <構成>_<条件> だけを <構成> として読み、work/paper/robust/<条件>/ に同じ一式を出す
    SRC = [f"{WORK}/paper/robust"]; D = f"{WORK}/paper/robust/{COND}"
else:
    SRC = [f"{WORK}/paper"] + ([f"{WORK}/paper/ext"] if EXT or FINAL else [])   # 結果を読む場所（同じ構成は後の場所が優先）
    D = f"{WORK}/paper/final2" if FINAL2 else f"{WORK}/paper/final" if FINAL else SRC[-1]                             # 図と表を書く場所
# 系列 = config.sh の組で選んだ方式のうち、どれか 1 本のトレースで結果がある物（下の SEL。並べた順が図と表の順）。
# ここには方式の一覧を持たない（11-20：以前は ext・頑健性の並びをここに手で書いていた）。
NAME = {}   # id → (日本語, 英語) の図の名前。カタログの 3 番目の欄、無ければ指定から method_label で作る（11-20）
# 色・基準・線・2×2 の方式は ids.py（11-20 に部品の名前に変えた）
# run.sh が残した選択（methods.tsv: id・図の名前・指定。./run.sh paper-figs は描く前に今の config.sh から作り直す）。
#   paper / final / final2: その場所の methods.tsv。ext: paper の選択 ＋ ext の選択（ext は paper の結果も読む）。
#   頑健性の条件ごと: robust の methods.tsv のうち、その条件の方式（末尾 _<条件> を外した名前で並べる）。
# methods.tsv が無いとき（手で結果を置いた場合など）は、結果のある方式を全部（基準を先頭に）。
SEL, SPEC, CATNAME = [], {}, set()
for _m in ([f"{WORK}/paper/robust/methods.tsv"] if COND else ([f"{WORK}/paper/methods.tsv"] if EXT else []) + [f"{D}/methods.tsv"]):
    if not os.path.exists(_m): continue
    for r in (l.rstrip("\n").split("\t") for l in open(_m, encoding="utf-8") if l.strip()):
        c = r[0][:-len(COND) - 1] if COND and r[0].endswith(VAR + COND) else r[0]
        if COND and c == r[0]: continue   # 別の条件の方式
        if c not in SEL: SEL.append(c)
        if len(r) > 1 and r[1]: NAME[c] = (r[1], r[1]); CATNAME.add(c)
        if len(r) > 2: SPEC[c] = r[2]
if FINAL and not SEL: sys.exit(f"{D}/methods.tsv が無い（./run.sh paper-final{'2' if FINAL2 else ''} で作る）")
if BASE in SEL: SEL.remove(BASE)
ORDER = [BASE] + SEL
PAL_ORDER = list(ORDER)   # 色の割り当ては図の組で絞る前の並びで
if FIGSET:
    SEL = [c for c in dict.fromkeys(FS_IDS.split()) if c in SEL]
    if not SEL: sys.exit(0)   # この組の選択にその図の組の方式が無い
    ORDER = [BASE] + SEL


for c in [BASE] + SEL:   # 基準（CacheLib）も同じ並びで
    if c in SPEC and c not in CATNAME: NAME[c] = method_label(SPEC[c])   # 11-20 名前は指定から同じ並びで（カタログに図の名前があればそれ）
nm = lambda c: NAME.get(c, (c, c))[EN]
T = lambda ja, en: en if EN else ja
colr = lambda c: (COL.get(c) or PAL[PAL_ORDER.index(c) % len(PAL)]) if c in PAL_ORDER else COL.get(c, "#888")   # 1 方式 1 色（どの図でも同じ）

runs, rundir = {}, {}   # rundir[(trace, config)] = その構成の出力のフォルダ（P.interval.csv の場所）
for src in SRC:
    for f in sorted(glob.glob(f"{src}/*.json")):
        d = json.load(open(f))
        if not (isinstance(d, dict) and "runs" in d): continue
        cur = runs.setdefault(d["name"], {k: v for k, v in d.items() if k != "runs"} | {"runs": {}})
        for c, r in d["runs"].items():
            if COND:   # r256-none-nat-n1~c0 → r256-none-nat-n1（その条件の構成だけ）
                if not c.endswith(VAR + COND): continue
                key = c[: -len(COND) - 1]
            else: key = c
            cur["runs"][key] = r; rundir[(d["name"], key)] = f"{src}/{d['name']}/{c}"
runs = {n: d for n, d in runs.items() if BASE in d["runs"]}
grp = lambda n: f"{runs[n].get('category', '') or '-'}/{runs[n].get('source', '') or '-'}"
if LIST_GROUPS:
    print("\n".join(sorted({grp(n) for n in runs}))); sys.exit(0)
if GROUP:
    runs = {n: d for n, d in runs.items() if grp(n) == GROUP}
    D = f"{D}/groups/" + "__".join(re.sub(r"[^A-Za-z0-9_.-]", "_", x) for x in GROUP.split("/", 1))
if FIGSET: D = f"{D}/figs_{re.sub(r'[^A-Za-z0-9_.-]', '_', FS_NAME)}"
if not runs: sys.exit(f"{SRC[0]}/*.json に {BASE}{VAR + COND if COND else ''} を含む結果が無い")
os.makedirs(D, exist_ok=True)
names = sorted(runs, key=lambda n: (runs[n].get("category", ""), runs[n].get("source", ""), n))
if not SEL:   # methods.tsv が無い: 結果のある方式を全部（基準を先頭に）
    ORDER = [BASE] + sorted({c for n in names for c in runs[n]["runs"]} - {BASE})
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

# ---------------------------------------------------------------- 受け入れ × 置き場所（直交性）：4 構成が揃ったトレースだけ
# 置き場所の得を、受け入れの篩なし（置き場所あり − なし）と、篩あり（同じ差）で並べる。組は ids.ORTH（現在の提案・以前の提案）。
# 差（相互作用）が 0 に近ければ直交、篩ありで得が消えるなら、篩が置き場所の効く理由（追い出しの圧力）を取り去っている
orth = []
for oj, oe, QO in ORTH:
  for n in names:
    R = runs[n]["runs"]
    if not all(q in R for q in QO): continue
    a0p0, a0p1, a1p0, a1p1 = (R[q] for q in QO)
    wr = lambda a, b: TB(a) / TB(b) if TB(b) else None
    orth.append(dict(set=T(oj, oe), trace=n, place_dbmr_pt=BMR(a0p1) - BMR(a0p0), place_dbmr_with_adm_pt=BMR(a1p1) - BMR(a1p0),
                     adm_dbmr_pt=BMR(a1p0) - BMR(a0p0), interaction_dbmr_pt=(BMR(a1p1) - BMR(a1p0)) - (BMR(a0p1) - BMR(a0p0)),
                     place_write_ratio=wr(a0p1, a0p0), place_write_ratio_with_adm=wr(a1p1, a1p0), adm_write_ratio=wr(a1p0, a0p0),
                     place_dohr_pt=OHR(a0p1) - OHR(a0p0), place_dohr_with_adm_pt=OHR(a1p1) - OHR(a1p0)))
if orth:
    with open(f"{D}/table_orth.csv", "w", newline="") as f:
        w = csv.DictWriter(f, list(orth[0])); w.writeheader(); w.writerows(orth)
    f3 = lambda v, s="+.2f": "–" if v is None else format(v, s)
    om = ["| " + " | ".join([T("組", "Set"), T("トレース", "Trace"), T("置き場所の ΔBMR（篩なし）", "Placement ΔBMR (no adm.)"),
                             T("置き場所の ΔBMR（篩あり）", "Placement ΔBMR (with adm.)"), T("篩の ΔBMR", "Admission ΔBMR"),
                             T("相互作用", "Interaction"), T("置き場所の書込比（篩なし / あり）", "Placement write ratio (no / with adm.)"),
                             T("篩の書込比", "Admission write ratio")]) + " |", "|" + "---|" * 8]
    for o in orth:
        om.append("| " + " | ".join([o["set"], o["trace"], f3(o["place_dbmr_pt"]), f3(o["place_dbmr_with_adm_pt"]), f3(o["adm_dbmr_pt"]),
                                     f3(o["interaction_dbmr_pt"]), f3(o["place_write_ratio"], ".3f") + " / " + f3(o["place_write_ratio_with_adm"], ".3f"),
                                     f3(o["adm_write_ratio"], ".3f")]) + " |")
    om.append("\n" + T("ΔBMR [pt]（負 = 良い）。置き場所 = サイズ分類＋容量の配分（現在の提案 = 大津＋名簿、以前の提案 = st＋山登り）、篩 = S3-FIFO 型：DRAM（ghost を含む）で 2 回以上"
                       "リクエストされた物だけ書く（admit=dram:min=1/ghost=0.9）。"
                       "相互作用 = 篩ありの置き場所の得 − 篩なしの得（0 なら足し算で効く。正なら篩で得が減る）。書込比 = 構成 ÷ 相方（1 未満 = 減る）。",
                       "ΔBMR [pt] (negative = better). Placement = size classification + capacity allocation (current: Otsu + ghost; previous: st + climb); admission = S3-FIFO style: write only "
                       "objects requested at least twice in DRAM including the ghost (admit=dram:min=1/ghost=0.9). Interaction = placement gain with "
                       "admission − without (0 = additive; positive = admission removes part of the gain). Write ratio = config / its counterpart (< 1 = fewer writes)."))
    open(f"{D}/table_orth.md", "w", encoding="utf-8").write("\n".join(om) + "\n")

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
    "write": (T("SSD 書き込み量（低いほど良い）", "SSD writes (lower is better)"), T("書き込み [TB]", "Writes [TB]"), lambda r, b: TB(r), False, 2, "abs"),
    "ohr": (T("OHR（参考。高いほど良い）", "OHR (for reference)"), "OHR [%]", lambda r, b: OHR(r), True, 1, "abs"),
    "dbmr": (T("ΔBMR（CacheLib 比。低いほど良い）", "ΔBMR vs. CacheLib (lower is better)"), "ΔBMR [pt]", lambda r, b: BMR(r) - BMR(b), True, 2, "diff"),
    "nwrite": (T("SSD 書き込み量（CacheLib の合計 = 1。低いほど良い）", "SSD writes (CacheLib total = 1; lower is better)"),
               T("書き込み（正規化）", "Normalized writes"), lambda r, b: TB(r) / TB(b) if TB(b) else None, True, 2, "ratio"),
    "dohr": (T("ΔOHR（CacheLib 比。参考）", "ΔOHR vs. CacheLib (for reference)"), "ΔOHR [pt]", lambda r, b: OHR(r) - OHR(b), True, 1, "diff"),
}


def stacked(a, key, shown, mean):
    """書き込みの棒を 取得（濃い）＋ 再書き込み（薄い）に分けて積む。nwrite は基準の書き込み全体 = 1 で割る。詰め物は描かない（表に残す）"""
    k = len(shown); w = a.bw * 0.8 / max(k, 1)
    for gi, c in enumerate(shown):
        col = colr(c)
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
            stack_labels(a, x, w, (y0, y1, y2), p)


def stack_labels(a, x, w, ys, p):
    """ラベル（小数第 2 位）：合計 = 棒の上、取得 = 濃い段の中（白）、再書き込み = 薄い段の中。
    棒の幅に横書きが入らなければ縦書き（-90°）。段の中に入らない長さなら、その段のラベルだけ省く（合計は必ず出す）"""
    y0, y1, y2 = ys; cx = x + w / 2
    tot, adm, rew = fmt(p[0] + p[1], 2), fmt(p[0], 2), fmt(p[1], 2)
    fs = 18 if w > 70 else 15
    horiz = tw(tot, fs) + 6 <= w
    fv = min(16, w - 4)                       # 縦書きの字の大きさ（棒の幅に収める）
    if horiz:
        a.f.t(cx, y2 - 8, tot, fs, "middle", INK, "bold")
        for txt, top, bot, colr in ((adm, y1, y0, "#fff"), (rew, y2, y1, INK)):
            if bot - top >= fs + 6: a.f.t(cx, (top + bot) / 2 + fs * 0.35, txt, fs, "middle", colr, "bold" if colr == "#fff" else "normal")
    elif fv >= 9:
        tx = cx + fv * 0.35
        a.f.t(tx, y2 - 6, tot, fv, "start", INK, "bold", rot=-90)
        for txt, top, bot, colr in ((adm, y1, y0, "#fff"), (rew, y2, y1, INK)):
            if bot - top >= tw(txt, fv) + 10: a.f.t(tx, (top + bot) / 2 + tw(txt, fv) / 2, txt, fv, "start", colr, "bold" if colr == "#fff" else "normal", rot=-90)


def draw(keys):  # keys の面を横に並べた 1 枚の図
    kinds = {PANELS[k][5] for k in keys}
    shown = cfgs if "abs" in kinds else others          # 差・比の図には基準の棒を出さない（基準 = 0 / 1 の線）
    f = Fig(); f.add_legend([(nm(c), colr(c), "box") for i, c in enumerate(cfgs) if c in shown])
    if {"write", "nwrite"} & set(keys):
        f.add_legend([(T("書き込み：濃い = 取得（ミス）、薄い = 再書き込み。数字 = 段の中が各量、上が合計", "Writes: solid = admitted (misses), light = rewrites; numbers = each part inside, total on top"), "#bbbbbb", "box")])
    if "ratio" in kinds:   # 基準の線の名前は凡例に（線の上に書くと棒の上の数字と重なる）
        f.add_legend([(T("CacheLib = 1", "CacheLib = 1"), "#4d4d4d", "dash")])
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
                    for c in shown for n in names if c in runs[n]["runs"]
                    and (key != "nwrite" or TB(runs[n]["runs"][BASE]))] or [0]   # 基準が SSD に書いていないトレースは比が無い（棒も描かない）
        if kind == "diff": yr = span(vals)                           # 0 を含む（正負の差）
        else: yr = (0, max(vals + ([1] if kind == "ratio" else [])) * (1.3 if key in ("write", "nwrite") else 1.1) or 1)   # 0 から（書き込みは上のラベルの分を空ける）
        a = Ax(f, (x0 + k * (W + gap), top, x0 + k * (W + gap) + W, 760), yr=yr, cats=cats_m if mean else names, ylabel=ylab, title=title)
        if key in ("write", "nwrite"): stacked(a, key, shown, mean)
        else: a.bars([(nm(c), colr(c), data[c], None) for i, c in enumerate(cfgs) if c in shown],
                     d=d, sign=kind == "diff")
        if kind == "ratio": a.hline(1.0, col="#4d4d4d")
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
    f = Fig(); f.add_legend([(nm(c), colr(c), "dash" if c == BASE else "line") for i, c in enumerate(cfgs) if c in S])
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
            if c in S: a.line([X(p) for p in S[c]], [p[j] for p in S[c]], colr(c), w=3, dash=c == BASE)
    return f.svg()


tl = []
for n in names:
    for xkey in ("ops", "wr"):
        svg = tl_fig(n, xkey)
        if svg:
            slug = f"fig_{xkey}_{n}"; open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(svg); tl.append(slug)

# ---------------------------------------------------------------- 自分で決める値の動き（11-20）：P.knobs.csv（区間の終わりごと）
#   閾値 τ、大の分類の下限と各分類の実際の容量の割合、n=cb の基準（名簿の限界の価値）と実質の n を、分類ごとに別の線で。
#   線の色 = 方式、線の形 = 分類（分類 0 = 破線、分類 1 以上 = 実線）、下限 = 細い薄い線。値が時間で変わらない方式は描かない
def knob_rows(n, c):
    p = f"{rundir.get((n, c), f'{D}/{n}/{c}')}/P.knobs.csv"
    if not os.path.exists(p): return None
    R = list(csv.DictReader(open(p)))
    if len(R) > 400: R = R[::-(-len(R) // 400)] + ([R[-1]] if (len(R) - 1) % -(-len(R) // 400) else [])   # 図は 400 点まで（最後の区間は残す）
    return R or None


fnum = lambda v: float(v) if v not in ("", None) else None
KN = []   # 表：最後の 4 分の 1 の平均（大体どこに落ち着いたか）と、下限が最終値の ±0.05 に初めて入った区間
kn = []
for n in names:
    KS = {c: r for c in cfgs if (r := knob_rows(n, c))}
    KS = {c: r for c, r in KS.items() if any(r[0].get(k) for k in ("tau_1", "floor_class", "bar_0"))
         and (len({x.get("tau_1") for x in r}) > 1 or r[0].get("floor_class") or r[0].get("bar_0") or r[-1].get("bar_0"))}
    if not KS: continue
    ncls = max(sum(1 for k in r[0] if k.startswith("share_")) for r in KS.values())
    f = Fig(1600, 900)
    f.add_legend([(nm(c), colr(c), "line") for c in KS] + [(T("右 2 面：破線 = 分類 0（小）", "right panels: dashed = class 0 (small)"), "#555555", "dash"),
                                                          (T("実線 = 分類 1（大）", "solid = class 1 (large)"), "#555555", "line")])
    top, x0, gap = f.legend_h() + 50, 110, 140
    W = (1600 - x0 - 30 - gap * 3) // 4
    X = lambda r: int(r["t_end"]) / 1e6
    xs = [X(r) for v in KS.values() for r in v]
    panels = [(T("閾値 τ（log2 バイト）", "Threshold τ (log2 bytes)"), "log2 τ"),
              (T("大の分類の下限 ff（太）と実際の割合（細）", "Large-class floor ff (thick) and actual share (thin)"), T("SSD に占める割合", "share of SSD")),
              (T("書き直しの基準（名簿の限界の価値）", "Rewrite bar (marginal value)"), T("ヒット/バイト/滞在", "hits/byte/stay")),
              (T("実質の n（書き直す最少のヒット数）", "Effective n (min hits to rewrite)"), "n")]
    import math
    def series(r, key):
        return [(X(x), fnum(x.get(key))) for x in r]
    for k, (title, ylab) in enumerate(panels):
        lines = []   # (方式, xs, ys, 破線, 細い)
        for c, r in KS.items():
            if k == 0 and r[0].get("tau_1") is not None:
                for i in range(1, ncls):
                    pts = [(a, math.log2(b + 1)) for a, b in series(r, f"tau_{i}") if b is not None]
                    if pts: lines.append((c, pts, False, False))
            if k == 1:   # 容量配分：下限 ff を太い線、その分類が実際に持つ割合を細い線で（下限なしで分類する方式は、大の分類の自然な割合だけ細い線）
                fc = r[0].get("floor_class")
                if fc:
                    pts = [(a, b) for a, b in series(r, "floor") if b is not None]
                    if pts: lines.append((c, pts, False, False))
                if fc or r[0].get("tau_1"):
                    pts = [(a, b) for a, b in series(r, f"share_{fc or 1}") if b is not None]
                    if pts: lines.append((c, pts, False, True))
            for cl in range(ncls):
                key = {2: f"bar_{cl}", 3: f"n_{cl}"}.get(k)
                if key is None or key not in r[0]: continue
                pts = [(a, b) for a, b in series(r, key) if b is not None]
                if pts: lines.append((c, pts, cl == 0, False))
        ys = [y for _, p, _, _ in lines for _, y in p]
        if not ys: continue
        yr = (0, 1.0) if k == 1 else (min(ys) - 0.5, max(ys) + 0.5) if k in (0, 3) else (0, max(ys) * 1.1 or 1)
        a = Ax(f, (x0 + k * (W + gap), top, x0 + k * (W + gap) + W, 790), xr=(0, max(xs) or 1), yr=yr,
               xlabel=T("処理した要求 [百万]", "Requests processed [M]") if k == 1 else "", ylabel=ylab, title=title)
        for c, p, dash, thin in lines:
            a.line([q[0] for q in p], [q[1] for q in p], colr(c), w=2 if thin else 3, dash=dash, op=0.45 if thin else 1.0)
    slug = f"fig_knobs_{n}"; open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(f.svg()); kn.append(slug)
    for c, r in KS.items():   # 表
        q = r[-max(1, len(r) // 4):]
        avg = lambda key: (lambda v: sum(v) / len(v) if v else None)([fnum(x.get(key)) for x in q if fnum(x.get(key)) is not None])
        row = dict(trace=n, config=c, label=nm(c), intervals=len(r), tau_last=r[-1].get("tau_1") or "", tau_changes=sum(
            1 for a_, b_ in zip(r, r[1:]) if a_.get("tau_1") != b_.get("tau_1")), floor_last_q=avg("floor"))
        fl = [fnum(x.get("floor")) for x in r]
        if row["floor_last_q"] is not None:
            row["floor_settle_interval"] = next((i for i, v in enumerate(fl) if v is not None and abs(v - row["floor_last_q"]) <= 0.05), "")
        for cl in range(ncls):
            for key in ("share", "bar", "n"): row[f"{key}_{cl}_last_q"] = avg(f"{key}_{cl}")
        KN.append(row)
if KN:
    cols = list(dict.fromkeys(k for r in KN for k in r))
    with open(f"{D}/table_knobs.csv", "w", newline="", encoding="utf-8") as fo:
        w_ = csv.DictWriter(fo, cols); w_.writeheader(); w_.writerows(KN)
    show = [k for k in cols if k not in ("trace", "config", "label")]
    g = lambda v: "–" if v in (None, "") else f"{v:.3g}" if isinstance(v, float) else str(v)
    open(f"{D}/table_knobs.md", "w", encoding="utf-8").write("\n".join(
        [f"| {T('トレース', 'Trace')} | {T('方式', 'Config')} | " + " | ".join(show) + " |", "|---|---|" + "---|" * len(show)]
        + [f"| {r['trace']} | {r['label']} | " + " | ".join(g(r.get(k)) for k in show) + " |" for r in KN]) + "\n\n" + T(
        "*_last_q = 最後の 4 分の 1 の区間の平均（大体どこに落ち着いたか）。floor_settle_interval = 下限がその値の ±0.05 に初めて入った区間。"
        "bar = 名簿の限界の価値（ヒット/バイト/滞在）、n = 書き直す最少のヒット数（9 = 書き直さない）。分類 0 = 小、1 = 大。\n",
        "*_last_q = mean over the last quarter of the intervals (where it settled). floor_settle_interval = first interval within ±0.05 of it. "
        "bar = marginal value from the ghost (hits/byte/stay), n = min hits to rewrite (9 = none). class 0 = small, 1 = large.\n"))

# ---------------------------------------------------------------- 交換関係の図（--ext のみ）：横 = SSD 書き込み（CacheLib n=1 = 1）、縦 = ΔBMR [pt]
#   CacheLib の n=0..3 を線でつなぐ（書き直しを増やすほど書き込みが増え BMR が下がる、という交換関係）。
#   提案の点がその線の左下にあれば、同じ BMR を少ない書き込みで（同じ書き込みで低い BMR を）出している。
#   S3-FIFO 型の篩（受け入れ、admit=dram:min=1/ghost=0.9）の点も重ねる：書き込みは少なく左に来るので、
#   縦の位置を n=0（書き直しなし）の点と比べると、篩が帯域（BMR）と引き換えにどれだけ書き込みを減らしたかが分かる
PTS = [(c, PT_BIG.get(c, 9), c in PT_HOLLOW) for c in cfgs if c not in {k for k, _ in CURVE} | {k for k, _ in LCURVE[1:]}]   # 系列は全部点で（CacheLib の n=0〜3 は線）

def tradeoff(keys):
    f = Fig()
    lam = [c for c, _ in LCURVE[1:] if c in cfgs and any(c in runs[n]["runs"] for n in keys)]
    f.add_legend([(T("CacheLib（R=256MiB）n=0〜3", "CacheLib (R=256MiB) n=0..3"), "#4d4d4d", "line")] +
                 ([(T("現在の提案の λ = 0〜1（書き込みの値段）", "proposal, λ = 0..1 (write price)"), colr(PROPOSAL), "dash")] if lam else []) +
                 [(nm(c), colr(c), "hollow" if hol else "dot") for c, _, hol in PTS if any(c in runs[n]["runs"] for n in keys)])
    top = f.legend_h() + 50; cols = min(3, len(keys)); rows_ = (len(keys) + cols - 1) // cols
    W = (1600 - 110 - 30 - 115 * (cols - 1)) // cols; H = (870 - top - 70 * (rows_ - 1)) // rows_
    for i, n in enumerate(keys):
        R = runs[n]["runs"]; b = R[BASE]
        if not TB(b): continue
        P = lambda c: (TB(R[c]) / TB(b), BMR(R[c]) - BMR(b))
        cur = [(P(c), lab) for c, lab in CURVE if c in R]
        dots = [(P(c), c, r, hol) for c, r, hol in PTS if c in R]
        lp = [P(c) for c, _ in LCURVE if c in R]
        xs = [p[0] for p, *_ in cur + dots] + [p[0] for p in lp]; ys = [p[1] for p, *_ in cur + dots] + [p[1] for p in lp]
        cx, cy = i % cols, i // cols
        box = (110 + cx * (W + 115), top + cy * (H + 70), 110 + cx * (W + 115) + W, top + cy * (H + 70) + H)
        a = Ax(f, box, xr=(0, max(xs + [1]) * 1.08), yr=span(ys), title=n,
               xlabel=T("SSD 書き込み（CacheLib n=1 = 1）", "SSD writes (CacheLib n=1 = 1)") if cy == rows_ - 1 and cx == cols // 2 else "",
               ylabel=T("ΔBMR [pt]（下 = 良い）", "ΔBMR [pt] (lower is better)") if cx == 0 else "")
        cur.sort(key=lambda t: t[0][0])
        a.line([p[0] for p, _ in cur], [p[1] for p, _ in cur], "#4d4d4d", w=3, marker=True)
        for p, lab in cur: f.t(a.X(p[0]) + 10, a.Y(p[1]) - 10, lab, 18, "start", "#4d4d4d")
        lc = [(P(c), lab) for c, lab in LCURVE if c in R and (c == PROPOSAL or c in lam)]
        if len(lc) > 1:   # λ を上げるほど書き込みが減る（左へ）
            a.line([p[0] for p, _ in lc], [p[1] for p, _ in lc], colr(PROPOSAL), w=3, dash=True, marker=True)
            for p, lab in lc[1:]: f.t(a.X(p[0]) + 10, a.Y(p[1]) + 24, lab, 18, "start", colr(PROPOSAL))
        for p, c, r, hol in dots: a.dot(p[0], p[1], colr(c), r, hollow=hol)
    return f.svg()


tr = []
if (EXT or COND or FINAL) and any(c in runs[n]["runs"] for n in names for c, _ in CURVE if c != BASE):
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
    f = Fig(); f.add_legend([(nm(c), colr(c), "line") for j, c in enumerate(others) if c in C] + [(T("指定した書き込み量", "write marks"), "#c0504a", "dash")])
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
            if c in C: a.line(xs, ys[c], colr(c), w=3)
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
# ---------------------------------------------------------------- 書き直しの閾値がなぜその値か（11-20、n=cb の方式）：P.reauto.json の最終値
#   分類ごとの面に、今の滞在で当たった回数の段（1〜8 以上）ごとの「書き直した物が次の滞在で 1 バイトあたり何回当たったか」の棒と、
#   比べる相手（名簿で測った端の密度 bar ＋ 書き込みの値段 λ）の横線。線を越えた段は書き直す（濃い棒）、越えない段は書き直さない（薄い棒）。
#   越える最初の段が実質の n（CacheLib の n に当たる）。データが 32 件未満の段は学習中で、比べずに書き直す（白抜き）
rw = []
for n in names:
    for c in cfgs:
        if "n=cb" not in SPEC.get(c, ""): continue
        p = f"{rundir.get((n, c), f'{D}/{n}/{c}')}/P.reauto.json"
        if not os.path.exists(p): continue
        cls = [(i, x) for i, x in enumerate(json.load(open(p)).get("class", [])) if x.get("next_stay_hits_per_byte") and x.get("bar", -1) >= 0]
        if not cls: continue
        f = Fig(1600, 870)
        f.add_legend([(T("線を越える段（書き直す）", "above the bar (rewrite)"), colr(c), "box"),
                      (T("越えない段（書き直さない）", "below the bar (no rewrite)"), "#c8c8c8", "box"),
                      (T("名簿で測った端の密度 ＋ λ", "marginal value from the ghost + λ"), "#c0504a", "dash")])
        top = f.legend_h() + 70; W = (1600 - 140 - 30 - 130 * (len(cls) - 1)) // len(cls)
        cats = ["1", "2", "3", "4", "5", "6", "7", T("8 以上", "8+")]
        for j, (ci, x) in enumerate(cls):
            v = x["next_stay_hits_per_byte"]; tries = x.get("try", [99] * 8); bar = x["bar"] + x.get("lambda", 0)
            ok = [tries[b] < 32 or v[b] >= bar for b in range(len(v))]
            neff = next((b + 1 for b in range(len(v)) if ok[b]), 9)
            x0 = 140 + j * (W + 130)
            name = {0: T("分類 0（小）", "class 0 (small)"), 1: T("分類 1（大）", "class 1 (large)")}.get(ci, T(f"分類 {ci}", f"class {ci}"))
            a = Ax(f, (x0, top, x0 + W, 720), yr=(0, max(v + [bar]) * 1.15), cats=cats,
                   xlabel=T("今の滞在で当たった回数（段）", "SSD hits in this stay (bucket)") if j == len(cls) // 2 else "",
                   ylabel=T("次の滞在のヒット / バイト", "next-stay hits per byte") if j == 0 else "",
                   title=T(f"{name}：実質の n = {neff if neff < 9 else 'なし'}", f"{name}: effective n = {neff if neff < 9 else 'none'}"))
            for b, y in enumerate(v):
                xc = a.x0 + a.bw * (b + 0.5); w_ = a.bw * 0.6; y0, y1 = a.Y(0), a.Y(y)
                learn = tries[b] < 32
                f.e.append(f'<rect x="{xc - w_ / 2:.1f}" y="{y1:.1f}" width="{w_:.1f}" height="{max(y0 - y1, 1):.1f}" '
                           f'fill="{"#ffffff" if learn else colr(c) if ok[b] else "#c8c8c8"}" stroke="{colr(c) if ok[b] else "#999999"}" stroke-width="2"/>')
                f.t(xc, y1 - 8, f"{y:.2f}", 18, "middle")
            a.hline(bar, col="#c0504a", label=f"{bar:.2f}")
        f.t(800, 800, T(f"{n}・{nm(c)}（最終値。白抜き = データ 32 件未満で学習中）", f"{n} · {nm(c)} (final values; hollow = < 32 samples, still learning)"), 20, "middle")
        slug = f"fig_rewrite_{n}_{c}"; open(f"{D}/{slug}.svg", "w", encoding="utf-8").write(f.svg()); rw.append(slug)
png = to_png(D, list(FIGS) + tl + tr + mk + kn + rw)

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
<h1>{f"［{GROUP}］" if GROUP else ""}{f"〈図の組 {FS_NAME}〉" if FIGSET else ""}{f"頑健性の条件 {COND} での比較" if COND else "追加の比較：CacheLib の n=0・n=3 と提案" if EXT else "論文の本番用の比較（paper-final2：現在の提案 ru-otsu-ghost-cb）" if FINAL2 else "論文の本番用の比較（paper-final）" if FINAL else "論文用の比較（図 1 枚・表 1 つ）"}</h1><p class="m">トレース {len(names)} 本：{", ".join(names)}。</p>
""" + ("<h2>交換関係：fig_tradeoff.svg（トレースごとは fig_tradeoff_&lt;trace&gt;.svg）</h2><img src='fig_tradeoff.svg' alt='SSD 書き込みと BMR の交換関係'>"
       "<p class='m'>横 = SSD 書き込み（CacheLib n=1 = 1、0 から）、縦 = ΔBMR [pt]（CacheLib n=1 との差、下が良い）。灰の線 = CacheLib（R=256MiB）の n=0〜3。"
       "書き直しを増やす（n を下げる）ほど書き込みが増えて BMR が下がる、という CacheLib の交換関係。濃い青 = RU＋分類（自動）、白抜き = RU＋分類（手動）と分類なしの n=auto。"
       "点が灰の線より左下にあれば、同じ BMR を少ない書き込みで出している。"
       "紫系 = DRAM（ghost を含む）で 2 回以上リクエストされた物だけ書く篩（S3-FIFO 型の受け入れ）：CacheLib＋篩・RU＋篩＋分類（塗り）、RU＋篩のみ（白抜き）。"
       "篩の点は書き込みが桁違いに少なく左端に来る。縦の位置を n=0（書き直しなし）の点と比べると、篩が帯域（BMR）と引き換えにどれだけ書き込みを減らしたかが分かる"
       "（n=0 より上なら、書き込みを減らす代わりに元のサーバからの転送が増えている）。</p>" if tr else "") + f"""<h2>図：fig_main.svg（3 面まとめ）{" / .png" if png else ""}</h2><img src="fig_main.svg" alt="BMR・SSD 書き込み量・OHR の比較"><h2>図：fig_rel.svg（CacheLib 比の 3 面まとめ）</h2><img src="fig_rel.svg" alt="CacheLib 比の BMR・書き込み・OHR"><p class="m">左 = ΔBMR（構成 − CacheLib、0 = CacheLib、負が良い）。中 = SSD 書き込み（CacheLib = 1 に正規化、1 未満が良い）。右 = ΔOHR（参考）。1 面ずつの図：fig_bmr / fig_write / fig_ohr（値そのもの）、fig_dbmr / fig_nwrite / fig_dohr（CacheLib 比）。</p>
<p><b>要点</b>（BMR・OHR はトレースの平均、書き込みは合計）：{pts}。</p>
<p class="m">左 = BMR（低いほど良い）。中 = SSD に書いたバイト（受け入れ＋再挿入＋詰め物、低いほど良い。トレースごとに規模が違うので平均は出さない）。右 = OHR（参考）。
濃い灰 = 基準（CacheLib 相当、Region 256 MiB、分類なし、再挿入 n=1）。薄い灰 = Region を RU（626.5 MiB）に合わせただけ（DLWA = 1 だが、Region が大きいぶん道連れと書き直しが増える）。
青 = RU＋書き込み時のサイズ分類（手動 / 自動）。橙 = 256 MiB＋分類（分類の効果が Region の大きさによらないかの確認）。</p>
""" + ("<h2>補足：時間変化</h2><p class='m'>fig_ops_* = 横軸が処理した要求数（BMR・OHR は区間ごと、SSD 書き込みは累積）。"
       "fig_wr_* = 横軸がその構成の累積 SSD 書き込み量（BMR・OHR はそこまでの累積。同じ量を書いた時点での比較）。破線 = 基準。</p>"
       + "".join(f'<h3>{s}</h3><img src="{s}.svg" alt="{s}">' for s in tl) if tl else "") + (
    "<h2>自分で決める値の動き：fig_knobs_&lt;trace&gt;.svg</h2><p class='m'>左から 閾値 τ、容量配分（大の分類の下限 ff を太い線、その分類が実際に SSD に持つ割合を同じ色の細い線。下限なしで分類する方式は大の分類の自然な割合だけ）、"
    "書き直しの基準（名簿の限界の価値）、実質の n（その時点で書き直す最少のヒット数。CacheLib の n に当たる）。色 = 方式。右の 2 面は分類ごとに線を分け、破線 = 分類 0（小）、実線 = 分類 1（大）。下限 ff は大の分類が最低限持つ割合で、細い線（実際の割合）が太い線の上にあれば下限は効いておらず（自然な配分のほうが多い）、重なっていれば下限で配分を決めている。"
    "どこに落ち着いたかは table_knobs.md（最後の 4 分の 1 の平均と、下限が落ち着いた区間）。</p>"
    + "".join(f'<h3>{s_}</h3><img src="{s_}.svg" alt="{s_}">' for s_ in kn)
    + (f"<pre>{open(f'{D}/table_knobs.md', encoding='utf-8').read()}</pre>" if KN else "") if kn else "") + (
    "<h2>書き直しの閾値がなぜその値か：fig_rewrite_&lt;trace&gt;_&lt;方式&gt;.svg</h2><p class='m'>n=cb の方式ごと、分類ごとの面。棒 = 今の滞在で当たった回数の段（1〜8 以上）ごとに、"
    "その段で書き直した物が次の滞在で 1 バイトあたり何回当たったか（実測の平均）。赤い破線 = 比べる相手（名簿で測った端の密度 ＋ 書き込みの値段 λ）。"
    "線を越えた段（濃い棒）は書き直し、越えない段（薄い棒）は書き直さない。越える最初の段が実質の n（CacheLib の n に当たる）。白抜き = データ 32 件未満で、比べずに書き直している段。最終値。</p>"
    + "".join(f'<h3>{s_}</h3><img src="{s_}.svg" alt="{s_}">' for s_ in rw) if rw else "") + f"""
""" + ("<h2>書き込み量の印での結果（部分トレースで足りるか）</h2><p class='m'>fig_marks_&lt;trace&gt;：横 = CacheLib が SSD に書いた量（先頭の部分トレースの長さ）、縦 = ΔBMR と書き込みの比。"
       "線が右端（全量）の値に早く落ち着き、赤い縦線（指定した書き込み量）の時点で全量と同じ側にあれば、その長さの部分トレースで結論が言える。数値は table_marks.md / .csv（✗ = 全量と向きが違う）。</p>"
       + "".join(f'<img src="{s_}.svg" alt="{s_}">' for s_ in mk) + (f"<pre>{open(f'{D}/table_marks.md', encoding='utf-8').read()}</pre>" if mrows else "") if mk else "") + f"""
<h2>表：table_main.md / .tex / .csv</h2><pre>{open(f"{D}/table_main.md", encoding="utf-8").read()}</pre>""" + (
    f"<h2>受け入れ × 置き場所（直交性）：table_orth.md / .csv</h2><pre>{open(f'{D}/table_orth.md', encoding='utf-8').read()}</pre>" if orth else "") + """</main></body></html>"""
open(f"{D}/index.html", "w", encoding="utf-8").write(html)
print(f"paper: {len(names)} 本 → {D}/fig_main.svg・fig_rel.svg（と 1 面ずつ 6 枚）, 時間変化 {len(tl)} 枚（fig_ops_* / fig_wr_*）{f'、交換関係 {len(tr)} 枚（fig_tradeoff*）' if tr else ''}{'（.png も）' if png else ''}, table_main.(md|tex|csv), index.html")
