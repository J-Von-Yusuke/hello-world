#!/usr/bin/env python3
"""tune_figs.py — tune の結果（work/tune/*.json）から、スライド用の図と説明を作る（work/tune/figs/）。
  python3 tune_figs.py <WORK> [名前..]     既定は全部。TUNE_W = 目的 BHR + w·OHR の w（既定 0.3）
図: トレースごとに (1) OHR–BMR の交換曲線（固定の下限 ff と自動 mv）(2) SSD 書き込みの内訳 (3) n=auto が学んだ再挿入の当たり方、
全トレースで (4) 目標の達成（基準 = 分類なし＋再挿入 n=1 との差）(5) 自動と固定の最良の差 (6) 分類の数 2/3/4 の比較。
figures.py の Fig / Ax と出力（index.html・captions.md・PNG・slides.pptx）を使う。標準ライブラリだけ。"""
import glob, json, os, sys, shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from figures import Fig, Ax, PAL, MUT, fmt, span, heat, to_png, write_index, write_pptx

WORK = sys.argv[1]; want = sys.argv[2:]; W = float(os.environ.get("TUNE_W", "0.3"))
T = {}
for f in sorted(glob.glob(f"{WORK}/tune/*.json")):
    d = json.load(open(f))
    if not want or d["name"] in want: T[d["name"]] = d
out = f"{WORK}/tune/figs"; shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
figs = []
BASE = "none_h1"
FF = ["st_lru", "st_ff50", "st_ff70", "st_ff80", "st_ff90", "st_ff95"]
MV = ["st_mv1", "st_mv0.3", "st_mv0.1", "st_mv0.03"]
HC = ["st_hc1", "st_hc0.3", "st_hc0.1", "st_hc0.03"]
J = lambda r: (1 - r["bmr"]) + W * r["ohr"]          # 目的 BHR + w·OHR


def add(slug, title, fig, point, read, define=""):
    figs.append((f"{len(figs)+1:02d}_{slug}", title, fig.svg(), point, read, define))


def best(R, ids):
    xs = [(J(R[i]), i) for i in ids if i in R]
    return max(xs)[1] if xs else None


for name, d in T.items():
    R = d["runs"]
    if not R: continue
    # (1) 交換曲線
    f = Fig(); f.add_legend([("固定の下限 ff（分類 2、再挿入なし）", PAL[0], "line"), ("自動 hc（下限を山登り、w を変える）", PAL[3], "line"), ("自動 mv（末尾の密度、比較用）", PAL[1], "line"),
                             ("再挿入つき", PAL[2], "hollow"), ("基準・理想", "#7d7d7d", "dot")])
    pts = [R[i] for i in R]
    ax = Ax(f, (170, f.legend_h() + 40, 1540, 790), xr=span([100 * r["bmr"] for r in pts], zero=False),
            yr=span([100 * r["ohr"] for r in pts], zero=False), xlabel="BMR [%]（左ほど良い）", ylabel="OHR [%]（上ほど良い）",
            title=f"{name}: OHR と BMR の交換（目標は左上）")
    for ids, col in ((FF, PAL[0]), (MV, PAL[1]), (HC, PAL[3])):
        xs = [i for i in ids if i in R]
        ax.line([100 * R[i]["bmr"] for i in xs], [100 * R[i]["ohr"] for i in xs], col, marker=True)
        for i in xs: f.t(ax.X(100 * R[i]["bmr"]) + 12, ax.Y(100 * R[i]["ohr"]) - 10, i.split("_")[-1], 19, "start", col)
    same = {}   # 同じ点（分類 3/4 が 2 に退化したときなど）は名前をまとめて 1 つに
    for i, r in R.items():
        if i in FF or i in MV or i in HC: continue
        same.setdefault((round(100 * r["bmr"], 2), round(100 * r["ohr"], 2)), []).append(i)
    for (x, y), ids in same.items():
        base = all(i in ("none", "none_h1", "none_auto", "obj") for i in ids)
        ax.dot(x, y, "#7d7d7d" if base else PAL[2], 9, hollow=not base)
        f.t(ax.X(x) + 12, ax.Y(y) + 22, " = ".join(ids), 18, "start", MUT)
    b = R.get(BASE); bs = best(R, FF)
    add(f"pareto_{name}", f"{name}: OHR–BMR の交換曲線（固定の下限 vs 自動）", f,
        (f"基準（分類なし＋再挿入 n=1）は OHR {100*b['ohr']:.1f}% / BMR {100*b['bmr']:.1f}%。" if b else "") +
        (f"固定の下限の中で目的 BHR+{W}·OHR が最大なのは {bs}（OHR {100*R[bs]['ohr']:.1f}% / BMR {100*R[bs]['bmr']:.1f}%）。" if bs else ""),
        "青 = 大物に残す容量の下限 ff を変えた点（lru = 下限なし）。紫 = 下限 ff を実行中に山登りで決める hc（目的の重み w ごと）。橙 = 末尾のヒット密度で配分する mv（比較用）。"
        "白抜き = 再挿入つき・分類 3/4。灰 = 分類なし（none）と物単位の理想 SSD（obj）。紫の点が青の線の上に乗っていれば、自動で固定の最良に届いている。",
        "OHR = 物のヒット率、BMR = バイトのミス率。目的 = (1 − BMR) + w × OHR。")
    # (2) 書き込みの内訳
    ids = [i for i in ("none", "none_h1", "none_auto", "st_ff90", "st_ff90_h1", "st_ff90_h3", "st_ff90_auto", "st_mv0.3_auto", "k3_lru_auto", "k4_lru_auto") if i in R]
    if ids:
        f = Fig(); f.add_legend([("受け入れ（ミスした物）", PAL[0], "box"), ("再挿入（アプリ層の GC）", PAL[1], "box"), ("詰め物", PAL[7], "box")])
        hi = max(R[i]["write"] for i in ids)
        ax = Ax(f, (170, f.legend_h() + 40, 1540, 760), yr=(0, hi * 1.18), cats=ids, ylabel="SSD 書き込み ÷ 要求バイト",
                title=f"{name}: SSD への書き込みの内訳（上の数字 = OHR / BMR [%]）")
        ax.stack([("受け入れ", PAL[0], [R[i]["admit"] for i in ids]), ("再挿入", PAL[1], [R[i]["reinsert"] for i in ids]),
                  ("詰め物", PAL[7], [R[i]["pad"] for i in ids])])
        for k, i in enumerate(ids):
            f.t(ax.x0 + ax.bw * (k + 0.5), ax.Y(R[i]["write"]) - 12, f"{100*R[i]['ohr']:.0f}/{100*R[i]['bmr']:.1f}", 19, "middle")
        add(f"writes_{name}", f"{name}: SSD 書き込みの内訳", f,
            (f"基準 {BASE} の書き込みは要求バイトの {R[BASE]['write']:.3f} 倍（うち再挿入 {R[BASE]['reinsert']:.3f}）。" if BASE in R else "") +
            (f"st_ff90_auto は {R['st_ff90_auto']['write']:.3f} 倍（再挿入 {R['st_ff90_auto']['reinsert']:.3f}）。" if "st_ff90_auto" in R else ""),
            "棒 = SSD に書いたバイト ÷ 要求バイト。青 = ミスした物の受け入れ（≒ BMR に比例）、橙 = 追い出しの際の書き直し（アプリ層の GC に当たる）。"
            "同じ BMR で橙が短いほど、少ない書き込みで同じ性能。",
            "書き込み = 受け入れ + 再挿入 + 詰め物（Region の端数）。")
    # (3) n=auto の学習
    a = next((R[i] for i in ("st_ff90_auto", "st_mv0.3_auto", "none_auto") if i in R and R[i].get("reauto")), None)
    if a:
        f = Fig(); f.add_legend([(f"分類 {c}", PAL[c], "line") for c in range(len(a["reauto"]))] + [("目標", "#c0504a", "dash")])
        ax = Ax(f, (170, f.legend_h() + 40, 1540, 790), xr=(1, 8), yr=(0, 1), xlabel="書き直したときのヒット回数 h（8 = 8 回以上）",
                ylabel="次の滞在でまた当たった割合", title=f"{name}: 再挿入の閾値 n=auto が学んだこと")
        for c, x in enumerate(a["reauto"]):
            ax.line(list(range(1, 9)), x["p_hit"], PAL[c], marker=True, label=f"n≈{x['n_mode']}")
        ax.hline(a["target"], label=f"目標 {a['target']}")
        add(f"reauto_{name}", f"{name}: 再挿入の閾値の自動推定", f,
            "各分類で使われた閾値 n（最頻）: " + "、".join(f"分類 {c} → n={x['n_mode'] if x['n_mode'] < 9 else 'なし'}" for c, x in enumerate(a["reauto"])) + "。",
            "線 = 当たった回数 h の物を書き直したとき、次の滞在で 1 回以上当たった割合（実行中に数えた値）。赤い横線を越える最小の h が、その分類の閾値 n になる。"
            "目標 0.5 は「書き直し 1 回で、平均 0.5 回以上のミス（とその受け入れの書き込み）を防ぐ」。",
            "目標より下の h も、確率 eps（既定 0.02）で書き直して数え続ける（学び直しのため）。")

# (4) 目標の達成（基準との差）
names = [n for n in T if BASE in T[n]["runs"]]
cands = ["mini_hc0.1_auto", "st_hc0.1_auto", "st_ff90_auto", "st_mv0.3_auto", "st_lru_auto", "k3_lru_auto"]
if names:
    rows, M = [], []
    for c in cands:
        for n in names:
            R = T[n]["runs"]
            if c not in R: continue
            b, r = R[BASE], R[c]
            rows.append(f"{n} / {c}")
            M.append([100 * (r["ohr"] - b["ohr"]), -100 * (r["bmr"] - b["bmr"]), -100 * (r["write"] / b["write"] - 1) if b["write"] else None])
    if rows:
        f = Fig(); f.H = max(900, 200 + 56 * len(rows))
        heat(f, (40, 170, 1560, f.H - 70), rows, ["OHR の向上 [pt]", "BMR の減少 [pt]", "書き込みの減少 [%]"], M,
             title="目標の達成: 基準（分類なし＋再挿入 n=1）との差。3 つとも青なら目標を満たす")
        add("goal", "目標の達成（OHR↑・BMR↓・書き込み↓）", f,
            f"3 つとも正の行: {sum(all(v is not None and v > 0 for v in m) for m in M)} / {len(M)}。",
            "行 = トレース / 構成。列 = OHR の向上、BMR の減少、SSD 書き込み（受け入れ＋再挿入）の減少。青 = 良くなった、赤 = 悪くなった。",
            "mini_hc0.1_auto = 分割閾値を縮小シミュレーション (hint=mini) + 下限を山登り (w=0.1) + 再挿入 n=auto（すべて自動）。"
            "st_hc0.1_auto = 分割閾値は SplitThreshold + 下限を山登り (w=0.1) + 再挿入 n=auto。st_ff90_auto = 固定の下限 0.9 + n=auto。st_mv0.3_auto = 末尾の密度の配分 + n=auto。")

# (5) 自動と固定の最良の差（同じ目的で、w ごと）
WS = [("0.03", 0.03), ("0.1", 0.1), ("0.3", 0.3), ("1", 1.0)]
gap = [(n, T[n]["runs"]) for n in T if any(i in T[n]["runs"] for i in FF)]
if gap:
    Jw = lambda r, w: (1 - r["bmr"]) + w * r["ohr"]
    cats = [f"{n} w={ws}" for n, _ in gap for ws, _ in WS]
    def g(kind):
        out = []
        for n, R in gap:
            for ws, w in WS:
                i = f"st_{kind}{ws}"; ff = [x for x in FF if x in R]
                out.append(100 * (Jw(R[i], w) - max(Jw(R[x], w) for x in ff)) if i in R and ff else None)
        return out
    hc, mv = g("hc"), g("mv")
    f = Fig(); f.add_legend([("hc（下限を山登り）", PAL[3], "box"), ("mv（末尾の密度）", PAL[1], "box")])
    ax = Ax(f, (170, f.legend_h() + 40, 1540, 740), yr=span([v for v in hc + mv if v is not None]), cats=cats,
            ylabel="目的の差 [pt]（0 = 固定の最良）", title="自動の配分は、固定の下限を後から選んだ最良にどこまで届くか")
    ax.bars([("hc", PAL[3], hc, None), ("mv", PAL[1], mv, None)])
    ok = [v for v in hc if v is not None]
    add("auto_gap", "自動の配分と、固定の下限の最良との差", f,
        (f"hc の差: 平均 {sum(ok)/len(ok):+.2f}pt、最悪 {min(ok):+.2f}pt。" if ok else "") + "判定の目安は −0.5pt 以内。",
        "各 w について、目的 (1 − BMR) + w × OHR を、自動の構成と「走らせた固定の下限 ff の格子の中で最良のもの」で比べた差。"
        "固定の最良は結果を見てから選んだもの（実運用では選べない上限）。正なら格子より良い。",
        "hc = 下限 ff を、窓（N 回の追い出し）ごとに測った目的が上がる向きへ 0.02 ずつ動かす。出発点は大物が受け入れたバイトの割合。")

# (6) 分類の数
ks = [(n, T[n]["runs"]) for n in T if all(i in T[n]["runs"] for i in ("st_lru_auto", "k3_lru_auto", "k4_lru_auto"))]
if ks:
    f = Fig(); f.add_legend([(n, PAL[i % len(PAL)], "line") for i, (n, _) in enumerate(ks)])
    top = f.legend_h() + 50
    for j, (key, lab, sc) in enumerate((("ohr", "OHR [%]", 100), ("bmr", "BMR [%]", 100), ("write", "書き込み ÷ 要求バイト", 1))):
        vs = [sc * R[i][key] for _, R in ks for i in ("st_lru_auto", "k3_lru_auto", "k4_lru_auto")]
        ax = Ax(f, (130 + 500 * j, top, 560 + 500 * j, 780), xr=(2, 4), yr=span(vs, zero=False), xlabel="" if j != 1 else "分類の数 K",
                ylabel=lab, title=lab)
        for i, (n, R) in enumerate(ks):
            ax.line([2, 3, 4], [sc * R[x][key] for x in ("st_lru_auto", "k3_lru_auto", "k4_lru_auto")], PAL[i % len(PAL)], marker=True)
    kk = [[R[x].get("k") for x in ("st_lru_auto", "k3_lru_auto", "k4_lru_auto")] for _, R in ks]
    add("classes", "分類の数（閾値 1〜3 個）の比較", f,
        "実際に使われた分類の数（トレースごと、K=2/3/4 の指定に対して）: " + "、".join(f"{n} {k}" for (n, _), k in zip(ks, kk)) + "。",
        "分類を増やすと、開いた Region（書き込み中の Region、FDP なら RU）が分類の数だけ要る。書き込み先が散るほど各 Region の書き込み速度が落ち、"
        "開いたままの時間が延び、空きも増える。増やす価値があるのは、OHR・BMR の改善がこの損を上回るときだけ。",
        "分類 3 = SplitThreshold ＋ ヒット率での細分（st_hr_refine k_max=3）、分類 4 = SplitThreshold の再帰（depth=2）。閾値が見つからなければ分類は減る。")

slugs = [s for s, *_ in figs]
for s, t, svg, *_ in figs: open(f"{out}/{s}.svg", "w", encoding="utf-8").write(svg)
png = to_png(out, slugs)
write_index(out, figs, png)
if png: write_pptx(f"{out}/slides.pptx", out, figs)
print(f"tune の図 {len(figs)} 枚: {out}/index.html", file=sys.stderr)
