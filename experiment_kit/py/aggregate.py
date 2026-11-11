#!/usr/bin/env python3
"""aggregate.py — トレースごとの結果（work/results/*.json）を、出典・種類ごとにまとめて CSV と report.html にする。

  python3 aggregate.py <kit> <work> [--by category,source]

  --by    まとめる列（traces.tsv の列名。カンマ区切り。列を足せばその列でもまとめられる）。
          既定は category,source と、その組（category/source）。
出力（work/results/）:
  per_trace.csv       1 トレース 1 行（特徴・LRU・Region・各分類の効果・収束）
  by_<列>.csv         グループごとの n / 平均 / 中央値 / 最小 / 最大 / 効果が正・負のトレース数（指標ごとに 1 行）
  synth_summary.csv   合成負荷の理論と実測
  report.html         上をまとめた図表（自己完結）
標準ライブラリだけ（numpy 不要）。"""
import csv, glob, html, json, math, os, re, statistics as st, sys, time

kit, work = sys.argv[1], sys.argv[2]
by = ["category", "source", "category/source"]
if "--by" in sys.argv: by = sys.argv[sys.argv.index("--by") + 1].split(",")
R = f"{work}/results"
esc = html.escape
TOL = 0.02   # 効果の「正」「負」とみなす幅 [pt]

# ---------------------------------------------------------------- 入力
meta = {}
tsv = os.environ.get("TRACES_TSV") or f"{kit}/traces.tsv"
lines = [l.rstrip("\n") for l in open(tsv, encoding="utf-8") if l.strip() and not l.startswith("#")]
hdr = lines[0].split("\t")
for l in lines[1:]:
    c = l.split("\t"); c += [""] * (len(hdr) - len(c)); d = dict(zip(hdr, c)); meta[d["name"]] = d
res = {}   # name -> {variant: json}
for f in sorted(glob.glob(f"{R}/*.*.json")):
    j = json.load(open(f)); res.setdefault(j["name"], {})[j["variant"]] = j
PREF = ["drop", "delete", "fix", "asis"]   # 主結果にする変種の優先順（--variant で指定すればそれ）
if "--variant" in sys.argv: PREF = [sys.argv[sys.argv.index("--variant") + 1]]
rows = []
for n, v in res.items():
    j = next((v[x] for x in PREF if x in v), None)
    if j is None: continue
    m = meta.get(n, {})
    r = dict(name=n, variant=j["variant"], **{k: m.get(k, "") for k in hdr if k not in ("name", "path", "params", "note")})
    r["category/source"] = f'{m.get("category", "")}/{m.get("source", "")}'
    r.update(N=j["full_n"], K_M=j["K_full"] / 1e6, days=j["t_sec"] / 86400, SSD_GiB=j["config"]["ssd"] / 2**30,
             lru_bmr=j["lru"]["bmr"], cold=j["lru"]["cold"], bmr_none=j["full"]["none"]["bmr"], collateral=j["collateral"]["all"],
             dwpd_all=j["dwpd_all"], write_TB=j["write_TB"])
    for k, x in j["feat"].items(): r["f_" + k] = x
    for k, x in j["effect"].items():
        r["eff_" + k] = x
        r["conv01_" + k] = (j["conv"][k]["0.1"]["frac"])
        s = j["sign_at"][k]["0.31"]; r["sign31_" + k] = "" if s is None else int(s)
    for k, x in j["collateral"].items():
        if k != "all": r["collateral_" + k] = x
    r["_j"] = j
    r["_v"] = v   # すべての変種（感度の表用）
    rows.append(r)
if not rows:   # トレース実験の結果が無くても、合成トレースの図だけは作る
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import figures
    os.makedirs(R, exist_ok=True); fo, nf, png = figures.build(R, [], work, {}, None)
    sys.exit(f"results/*.json が無い（./run.sh validity がまだ）。合成トレースの図だけ作った: {nf} 枚 → {fo}/index.html" if nf else
             "results/*.json も合成トレースの結果も無い。先に ./run.sh validity か ./run.sh synth")
rows.sort(key=lambda r: (r.get("category", ""), r.get("source", ""), r["name"]))
effkeys = sorted({k[4:] for r in rows for k in r if k.startswith("eff_") and not k.startswith("eff_asis_")},
                 key=lambda k: (k.startswith("d"), k))

def ctx(k):   # 効果キー → (文脈, 分類名)。 d3_kf4_h1 → ("予算 3 DWPD + 再挿入", "kf4")
    m = re.match(r"^(d\d+_)?(.+?)(_h1)?$", k)
    return ((f"予算 {m[1][1:-1]} DWPD" if m[1] else "全受け入れ") + (" + 再挿入" if m[3] else ""), m[2])
ctxs = {}
for k in effkeys: ctxs.setdefault(ctx(k)[0], []).append(k)

def stat(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not math.isnan(x)]
    return None if not xs else dict(n=len(xs), mean=st.mean(xs), median=st.median(xs), min=min(xs), max=max(xs),
                                    pos=sum(x > TOL for x in xs), neg=sum(x < -TOL for x in xs))
def spearman(a, b):
    p = [(x, y) for x, y in zip(a, b) if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if len(p) < 4: return None
    def rk(v):
        o = sorted(range(len(v)), key=lambda i: v[i]); r = [0.0] * len(v); i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[o[j + 1]] == v[o[i]]: j += 1
            for t in range(i, j + 1): r[o[t]] = (i + j) / 2
            i = j + 1
        return r
    x, y = rk([q[0] for q in p]), rk([q[1] for q in p]); mx, my = st.mean(x), st.mean(y)
    sx, sy = sum((a - mx) ** 2 for a in x), sum((b - my) ** 2 for b in y)
    return None if sx == 0 or sy == 0 else sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx * sy)

# ---------------------------------------------------------------- CSV
flat = [{k: v for k, v in r.items() if k != "_j"} for r in rows]
cols = sorted({k for r in flat for k in r}, key=lambda k: (k not in ("name", "category", "source", "variant"), k))
with open(f"{R}/per_trace.csv", "w", newline="") as f:
    w = csv.DictWriter(f, cols); w.writeheader(); w.writerows(flat)
metrics = ["collateral", "lru_bmr", "bmr_none", "dwpd_all", "days", "K_M"] + [f"eff_{k}" for k in effkeys] + [f"conv01_{k}" for k in effkeys]
groups = {}
for b in by:
    keyf = (lambda r, b=b: r.get(b, "")) if b in rows[0] else None
    if keyf is None: print(f"--by {b}: traces.tsv に無い列。飛ばす", file=sys.stderr); continue
    g = {}
    for r in rows: g.setdefault(keyf(r) or "(空)", []).append(r)
    groups[b] = g
    with open(f"{R}/by_{b.replace('/', '_')}.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow([b, "metric", "n", "mean", "median", "min", "max", "n_positive", "n_negative", "traces"])
        for gv, rs in sorted(g.items()):
            for m in metrics:
                s = stat([r.get(m) for r in rs])
                if s: w.writerow([gv, m, s["n"], f'{s["mean"]:.4f}', f'{s["median"]:.4f}', f'{s["min"]:.4f}', f'{s["max"]:.4f}',
                                  s["pos"] if m.startswith("eff_") else "", s["neg"] if m.startswith("eff_") else "", " ".join(r["name"] for r in rs)])

# ---------------------------------------------------------------- SVG 部品
PAL = ["#3b7dbf", "#c9683a", "#3f9a64", "#8a5fb5", "#b8a02a", "#c0504a", "#2aa0a8", "#7d7d7d"]
cat_color = {}
def color(g): return cat_color.setdefault(g, PAL[len(cat_color) % len(PAL)])
def strip_svg(key, rowsx, groupkey="category"):
    """トレースごとの効果 [pt] の点を、グループ順に並べる。横軸 = トレース、縦軸 = 効果。"""
    ks = [k for k in effkeys if ctx(k)[0] == key]; W, H, ml, mt = 880, 260, 50, 24
    vals = [r.get("eff_" + k) for r in rowsx for k in ks if isinstance(r.get("eff_" + k), float)]
    if not vals: return ""
    lo, hi = min(vals + [0]), max(vals + [0]); pad = (hi - lo) * 0.1 or 0.1; lo -= pad; hi += pad
    y = lambda v: mt + (H - mt - 40) * (hi - v) / (hi - lo)
    dx = (W - ml - 10) / max(len(rowsx), 1)
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(key)}の効果">']
    for t in range(5):
        v = lo + (hi - lo) * t / 4; s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="gl"/><text x="{ml-6}" y="{y(v)+4:.1f}" text-anchor="end" class="m">{v:+.2f}</text>')
    s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/><text x="4" y="14" class="m">効果 [pt]（正 = 分類で BMR が下がる）　{esc(key)}</text>')
    for i, r in enumerate(rowsx):
        x = ml + dx * (i + 0.5); c = color(r.get(groupkey, ""))
        for j, k in enumerate(ks):
            v = r.get("eff_" + k)
            if isinstance(v, float):
                xx = x + (j - (len(ks) - 1) / 2) * 9
                s.append(f'<circle cx="{xx:.1f}" cy="{y(v):.1f}" r="4.5" fill="{c}" opacity="{0.45 + 0.55 * (j + 1) / len(ks):.2f}"><title>{esc(r["name"])} {esc(ctx(k)[1])}: {v:+.3f} pt</title></circle>')
        s.append(f'<text x="{x:.1f}" y="{H-24}" transform="rotate(35 {x:.1f} {H-24})" class="m">{esc(r["name"])}</text>')
    lx = ml
    for j, k in enumerate(ks): s.append(f'<circle cx="{lx+6}" cy="{H-6}" r="4" fill="#888" opacity="{0.45 + 0.55 * (j + 1) / len(ks):.2f}"/><text x="{lx+14}" y="{H-2}" class="m">{esc(ctx(k)[1])}</text>'); lx += 90
    return "".join(s) + "</svg>"
def curve_svg(k):
    """先頭 N 要求での効果の推移（横軸 = N / 全量。1 に近いほど全量）。線の色 = category。"""
    W, H, ml, mt = 880, 260, 50, 24; ser = []
    for r in rows:
        c = r["_j"]["curve"]
        if k in c["eff"]: ser.append((r, [n / r["_j"]["full_n"] for n in c["n"]], c["eff"][k]))
    if not ser: return ""
    ys = [v for _, _, e in ser for v in e]; lo, hi = min(ys + [0]), max(ys + [0]); pad = (hi - lo) * 0.1 or 0.1; lo -= pad; hi += pad
    xmin = math.log10(min(min(x) for _, x, _ in ser)); x = lambda f: ml + (W - ml - 10) * (math.log10(f) - xmin) / (0 - xmin)
    y = lambda v: mt + (H - mt - 26) * (hi - v) / (hi - lo)
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="先頭 N の効果 {esc(k)}">']
    for t in range(5):
        v = lo + (hi - lo) * t / 4; s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="gl"/><text x="{ml-6}" y="{y(v)+4:.1f}" text-anchor="end" class="m">{v:+.2f}</text>')
    s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/><text x="4" y="14" class="m">{esc(ctx(k)[1])}（{esc(ctx(k)[0])}）の効果 [pt] を、先頭 N 要求で測ったとき（横軸 = N ÷ 全要求数、対数）</text>')
    for e in range(int(math.floor(xmin)), 1):
        if 10 ** e >= 10 ** xmin: s.append(f'<text x="{x(10**e):.1f}" y="{H-8}" text-anchor="middle" class="m">{10**e:g}</text>')
    for r, xs, es in ser:
        pts = " ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in zip(xs, es))
        s.append(f'<polyline points="{pts}" fill="none" stroke="{color(r.get("category", ""))}" stroke-width="1.6" opacity=".8"><title>{esc(r["name"])}</title></polyline>')
    return "".join(s) + "</svg>"

# ---------------------------------------------------------------- HTML 表
def f(x, p=2): return "" if x in (None, "") else (f"{x:.{p}f}" if isinstance(x, float) else esc(str(x)))
def table(head, body): return "<div class='wrap'><table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body) + "</table></div>"
def cell(s, p=3): return "" if s is None else f'{s["mean"]:+.{p}f} <span class="m">({s["min"]:+.{p}f}…{s["max"]:+.{p}f}) +{s["pos"]}/−{s["neg"]}</span>'
def chip(g): return f'<span class="chip" style="background:{color(g)}">{esc(g)}</span>'

H = [f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>実験結果の集計</title><style>
:root{{--bg:#fbfaf7;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9;--card:#fff}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732;--card:#24221f}}}}
body{{background:var(--bg);color:var(--fg);font:14.5px/1.7 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}}main{{max-width:1000px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:1.5rem}}h2{{font-size:1.2rem;margin-top:2.2em;border-bottom:2px solid var(--line)}}h3{{font-size:1rem;margin-top:1.6em}}.m{{color:var(--mut);font-size:12px}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;font-size:13px;margin:.5em 0}}th,td{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;vertical-align:top;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}
.chip{{color:#fff;border-radius:4px;padding:0 6px;font-size:12px;font-weight:600}}.fig{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;margin:10px 0}}
svg{{max-width:100%;height:auto;display:block}}svg text{{fill:var(--fg);font:12px system-ui,sans-serif}}svg .m{{fill:var(--mut)}}svg .gl{{stroke:var(--line)}}svg .zero{{stroke:var(--mut);stroke-dasharray:4 3}}
.note{{border-left:5px solid #3f9a64;background:var(--card);padding:6px 12px;margin:10px 0}}</style></head><body><main>
<h1>実験結果の集計</h1><p class="m">{time.strftime('%Y-%m-%d %H:%M')} 生成。トレース {len(rows)} 本（変種: {', '.join(sorted({r['variant'] for r in rows}))}）。</p>
<div class="note"><b>読み方</b> 効果 = BMR（バイトミス率）の差 [pt] = 分類なし − 分類あり。<b>正なら分類でミスが減る</b>。比べる相手は同じ条件（全受け入れ / 予算つき、再挿入の有無）の分類なし。
collateral = Region 単位の追い出しによる余分なミス [pt]（物単位 LRU との差）。conv = 先頭何割の要求で全量と ±0.1 pt 以内になるか。</div>"""]

H.append("<h2>1. トレース一覧</h2>")
H.append(table(["name", "category", "source", "N（要求）", "K（百万 piece）", "日数", "SSD GiB", "LRU BMR %", "Region BMR %", "collateral pt", "DWPD（全受入）"] ,
               [[esc(r["name"]), chip(r.get("category", "")), esc(r.get("source", "")), f'{r["N"]:,}', f(r["K_M"], 2), f(r["days"], 1), f(r["SSD_GiB"], 1),
                 f(r["lru_bmr"]), f(r["bmr_none"]), f(r["collateral"], 3), f(r["dwpd_all"], 1)] for r in rows]))
for b, g in groups.items():
    H.append(f"<h2>2. 「{esc(b)}」ごとの傾向</h2>")
    for cx, ks in ctxs.items():
        H.append(f"<h3>{esc(cx)}: 分類の効果 [pt]　平均（最小…最大）　+正のトレース数/−負のトレース数</h3>")
        H.append(table([b, "トレース数", "collateral pt"] + [ctx(k)[1] for k in ks] + [f"conv(±0.1pt) {ctx(k)[1]}" for k in ks],
                       [[esc(gv), str(len(rs)), cell(stat([r["collateral"] for r in rs]), 2)] + [cell(stat([r.get("eff_" + k) for r in rs])) for k in ks] +
                        [f(stat([r.get("conv01_" + k) for r in rs])["mean"] if stat([r.get("conv01_" + k) for r in rs]) else None, 3) for k in ks]
                        for gv, rs in sorted(g.items())]))
    H.append(f"<h3>{esc(b)} ごとの基本量（平均）</h3>")
    H.append(table([b, "トレース数", "LRU BMR %", "Region BMR %", "DWPD（全受入）", "日数", "サイズ変化率", "size 分散 Var(log2 s)"],
                   [[esc(gv), str(len(rs))] + [f(stat([r.get(m) for r in rs])["mean"] if stat([r.get(m) for r in rs]) else None, p)
                    for m, p in (("lru_bmr", 2), ("bmr_none", 2), ("dwpd_all", 1), ("days", 1), ("f_size_change", 3), ("f_var_logs_b", 2))] for gv, rs in sorted(g.items())]))

H.append("<h2>3. トレースごとの効果（点の色 = category、順序 = category → source）</h2>")
H.append("<p class='m'>凡例: " + " ".join(chip(c) for c in sorted(cat_color) or sorted({r.get('category', '') for r in rows})) + "</p>")
for cx in ctxs:
    sv = strip_svg(cx, rows)
    if sv: H.append(f"<div class='fig'>{sv}</div>")
H.append("<p class='m'>" + " ".join(chip(c) for c in sorted({r.get('category', '') for r in rows})) + "</p>")

H.append("<h2>4. 部分トレースで足りるか: 先頭 N 要求での効果の推移</h2><p class='m'>線が最後（右端 = 全量）の値に早く落ち着くほど、短いトレースで傾向を再現できる。</p>")
for k in [k for k in effkeys if ctx(k)[0] == "全受け入れ"]:
    sv = curve_svg(k)
    if sv: H.append(f"<div class='fig'>{sv}</div>")
def curve_w_svg(k):
    """効果の推移を「全受け入れ・分類なしの SSD 書き込み量 [TB]」で測ったとき。縦の破線 = WRITE_MARKS_TB。"""
    W, H, ml, mt = 880, 260, 50, 24; ser = []
    for r in rows:
        c = r["_j"]["curve"]
        if k in c["eff"] and "write_TB" in c:
            p = [(w, e) for w, e in zip(c["write_TB"], c["eff"][k]) if w > 0]
            if p: ser.append((r, p))
    if not ser: return ""
    ys = [e for _, p in ser for _, e in p]; lo, hi = min(ys + [0]), max(ys + [0]); pad = (hi - lo) * 0.1 or 0.1; lo -= pad; hi += pad
    xs = [w for _, p in ser for w, _ in p]; xl, xh = math.log10(min(xs)), math.log10(max(xs)); xh = max(xh, xl + 0.1)
    x = lambda w: ml + (W - ml - 10) * (math.log10(w) - xl) / (xh - xl); y = lambda v: mt + (H - mt - 26) * (hi - v) / (hi - lo)
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="書き込み量で測った効果 {esc(k)}">']
    for t in range(5):
        v = lo + (hi - lo) * t / 4; s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="gl"/><text x="{ml-6}" y="{y(v)+4:.1f}" text-anchor="end" class="m">{v:+.2f}</text>')
    s.append(f'<line x1="{ml}" x2="{W-10}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/><text x="4" y="14" class="m">{esc(ctx(k)[1])}（{esc(ctx(k)[0])}）の効果 [pt]。横軸 = 先頭から SSD に書いた量 [TB]（全受け入れ・分類なし、対数）。破線 = 注目する書き込み量</text>')
    for e in range(int(math.floor(xl)), int(math.ceil(xh)) + 1):
        if xl <= e <= xh: s.append(f'<text x="{x(10**e):.1f}" y="{H-8}" text-anchor="middle" class="m">{10**e:g} TB</text>')
    for W_ in sorted({float(m) for r in rows for m in r["_j"].get("at_writes", {})}):
        if 10 ** xl <= W_ <= 10 ** xh: s.append(f'<line x1="{x(W_):.1f}" x2="{x(W_):.1f}" y1="{mt}" y2="{H-26}" stroke="#c0504a" stroke-dasharray="5 4"/><text x="{x(W_)+3:.1f}" y="{mt+10}" class="m">{W_:g} TB</text>')
    for r, p in ser:
        s.append(f'<polyline points="{" ".join(f"{x(a):.1f},{y(b):.1f}" for a, b in p)}" fill="none" stroke="{color(r.get("category", ""))}" stroke-width="1.6" opacity=".8"><title>{esc(r["name"])}</title></polyline>')
    return "".join(s) + "</svg>"

H.append("<h3>先頭 N 要求ではなく、SSD に書いた量で測ると</h3>")
for k in [k for k in effkeys if ctx(k)[0] == "全受け入れ"]:
    sv = curve_w_svg(k)
    if sv: H.append(f"<div class='fig'>{sv}</div>")
aw_rows = []
for r in rows:
    for W_, a in sorted(r["_j"].get("at_writes", {}).items(), key=lambda t: float(t[0])):
        if a is None: aw_rows.append([esc(r["name"]), f"{W_} TB", "全量が W に届かない", "", "", ""] + [""] * (3 * len([k for k in effkeys if ctx(k)[0] == "全受け入れ"]))); continue
        kk = [k for k in effkeys if ctx(k)[0] == "全受け入れ"]
        aw_rows.append([esc(r["name"]), f"{W_} TB", f'{a["n"]:,}', f(a["frac"], 3), f(a["req_TB"], 1), f(a["collateral"], 3)] +
                       [c for k in kk for c in (f(a["eff"].get(k), 3), f(r["_j"]["effect"].get(k), 3), {True: "○", False: "×", None: "–"}[a["sign_ok"].get(k)])])
kk = [k for k in effkeys if ctx(k)[0] == "全受け入れ"]
if aw_rows:
    H.append("<h3>先頭 W TB 書き込んだ時点の効果と、全量の効果（全受け入れ）</h3><p class='m'>W = 全受け入れ・分類なし(none)の SSD 書き込み量が W に達する最初の点。○ = 符号が全量と一致。差 = 効果(W) − 効果(全量)。</p>")
    H.append(table(["name", "W", "その時点の N（要求）", "N ÷ 全要求", "要求バイト TB", "collateral(W) pt"] + [h for k in kk for h in (f"効果(W) {ctx(k)[1]}", f"効果(全量) {ctx(k)[1]}", f"符号 {ctx(k)[1]}")], aw_rows))
H.append("<h3>全量の効果と収束（±0.1 pt / ±0.02 pt に入る先頭の割合、先頭 31% で符号が一致したか）</h3>")
k0 = [k for k in effkeys if ctx(k)[0] == "全受け入れ"]
H.append(table(["name", "category", "source"] + [h for k in k0 for h in (f"効果 {ctx(k)[1]}", f"conv0.1 {ctx(k)[1]}", f"conv0.02 {ctx(k)[1]}", f"符号一致31% {ctx(k)[1]}")],
               [[esc(r["name"]), chip(r.get("category", "")), esc(r.get("source", ""))] + [c for k in k0 for c in (
                   f(r.get("eff_" + k), 3), f(r["_j"]["conv"][k]["0.1"]["frac"], 3), f(r["_j"]["conv"][k]["0.02"]["frac"], 3),
                   {1: "○", 0: "×", "": "–"}[r.get("sign31_" + k, "")])] for r in rows]))

H.append("<h2>5. 特徴と効果の関係（トレース間の Spearman 順位相関）</h2>")
fk = sorted({k for r in rows for k in r if k.startswith("f_")}); tg = ["collateral"] + [f"eff_{k}" for k in k0]
if len(rows) >= 4:
    H.append(table(["特徴 \\ 相関する量"] + [t.replace("eff_", "効果 ") for t in tg],
                   [[esc(k[2:])] + [f(spearman([r.get(k) for r in rows], [r.get(t) for r in rows]), 2) or "–" for t in tg] for k in fk]))
    H.append(f"<p class='m'>トレース数 {len(rows)}。少ないうちは目安。|ρ| が大きい特徴が、効果の大小を説明する候補。</p>")
else: H.append("<p class='m'>トレースが 4 本未満なので省略。</p>")

sens = [r for r in rows if len(r["_v"]) > 1]
if sens:
    H.append("<h2>6. 埋め値（size = 2^32−1）の扱いによる違い</h2><p class='m'>drop = 無かったことに / delete = その物を消す操作（要求に数えない）/ fix = その物の他のサイズに置換 / asis = そのまま。主結果は先頭の変種。</p>")
    body = []
    for r in sens:
        for vn in sorted(r["_v"], key=lambda x: PREF.index(x) if x in PREF else 9):
            jj = r["_v"][vn]
            body.append([esc(r["name"]), esc(vn), f'{jj["full_n"]:,}', f(jj["lru"]["bmr"]), f(jj["full"]["none"]["bmr"]), f(jj["collateral"]["all"], 3), f(jj["dwpd_all"], 1)] +
                        [f(jj["effect"].get(k), 3) for k in k0])
    H.append(table(["name", "変種", "N（要求）", "LRU BMR %", "Region BMR %", "collateral pt", "DWPD（全受入）"] + [f"効果 {ctx(k)[1]}" for k in k0], body))

# ---------------------------------------------------------------- 合成負荷
sj = [json.load(open(p)) for p in sorted(glob.glob(f"{work}/synth/*.json")) if not p.endswith(("scaled.json", "writes.json"))]
sj = [j for j in sj if isinstance(j, dict) and "sim" in j]
if sj:
    H.append("<h2>7. 合成トレースの評価（理論 = Che 型モデル、実測 = cachesim）</h2><p class='m'>OHR / BHR は物ヒット率 / バイトヒット率。分割効果 = サイズ分類 rlru[1] − 分類なし rlru[0]。誤差 = 実測 − 理論（OHR, BHR）。</p>")
    out = open(f"{R}/synth_summary.csv", "w", newline=""); w = csv.writer(out); w.writerow(["workload", "f", "policy", "sim_ohr", "sim_bhr", "model_ohr", "model_bhr"])
    names = ["lru", "part[1]", "rfifo[0]", "rfifo[1]", "rlru[0]", "rlru[1]", "rlru[2]"]; body = []
    for j in sj:
        for fk, s in j["sim"].items():
            m = j["model"].get(fk, {}); cells = []
            for nm in names:
                if nm not in s: cells.append(""); continue
                mk = "rfifo" if nm.startswith("rfifo") else nm
                mo = m.get(mk); w.writerow([j["name"], fk, nm, s[nm][0], s[nm][1], mo["ohr"] if mo else "", mo["bhr"] if mo else ""])
                cells.append(f'{s[nm][0]:.3f}/{s[nm][1]:.3f}' + (f' <span class="m">({s[nm][0]-float(mo["ohr"]):+.3f}/{s[nm][1]-float(mo["bhr"]):+.3f})</span>' if mo else ""))
            gain = ""
            if "rlru[1]" in s and "rlru[0]" in s: gain = f'{s["rlru[1]"][0]-s["rlru[0]"][0]:+.3f}/{s["rlru[1]"][1]-s["rlru[0]"][1]:+.3f}'
            body.append([esc(j["name"]), f'{float(fk):.2f}'] + cells + [gain])
    out.close()
    H.append(table(["負荷", "容量比 f"] + names + ["分割効果"], body))
    H.append("<p class='m'>各セル: 実測 OHR/BHR（括弧内 = 実測 − 理論）。負荷の定義は synth_workloads.txt。</p>")
for sec, (nm, ttl) in enumerate((("scaled", "縮小した SSD 想定の負荷（scaled.py）"), ("writes", "書き込み量の内訳（writes.py）")), 8):
    p = f"{work}/synth/{nm}.json"
    if os.path.exists(p) and os.path.getsize(p) > 2:
        d = json.load(open(p)); H.append(f"<h2>{sec}. {ttl}</h2>")
        if isinstance(d, dict):
            body = [[esc(k)] + [f(v.get(x), 4) for x in ("ohr", "bmr", "write", "admit", "reinsert")] for k, v in d.items() if isinstance(v, dict) and "ohr" in v]
            H.append(table(["w|R|構成", "OHR", "BMR", "write B", "admit B", "reinsert B"], body))
        else:
            H.append(table(["w", "R", "構成", "ff", "再挿入", "OHR", "BMR", "write B", "reinsert B"], [[esc(x["w"]), esc(x["r"]), esc(x["kind"]), str(x["ff"]), str(x["re"]), f(x["ohr"], 4), f(x["bmr"], 4), f"{x['write']:.3e}", f"{x['reinsert']:.3e}"] for x in d]))
H.append("</main></body></html>")
open(f"{R}/report.html", "w", encoding="utf-8").write("\n".join(H))
print(f"集計: {len(rows)} 本 → {R}/report.html, per_trace.csv, by_*.csv" + (", synth_summary.csv" if sj else ""))

# ---------------------------------------------------------------- スライド用の図と説明（figs/）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import figures
fo, nf, png = figures.build(R, rows, work, groups, spearman)
print(f"図: {nf} 枚 → {fo}/index.html（説明つき）, *.svg" + (", *.png, slides.pptx" if png else
      "（PNG と slides.pptx は作れなかった: rsvg-convert / cairosvg / inkscape / soffice のどれかを入れる）"))
