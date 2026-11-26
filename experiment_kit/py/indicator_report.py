#!/usr/bin/env python3
"""indicator_report.py — 2026-11-26: does the proposal help this workload? The trace-only indicators (work/indicator/<name>.json, og_ind)
next to the measured classification gain from the paper results (work/paper/<name>.json):
  gain = proposal (ru-otsu-ghost-cb) − RU without classification (ru-none-nat-cb); with the filter: the same pair with -adm.
Indicators (per trace, without and with the one-hit filter; og_ind = object-level Che with DRAM in front):
  turnover  = how many times the SSD capacity is written over the trace (pressure; below ~1–2 nothing is evicted, nothing can help)
  spread    = Otsu's score of the per-size reuse value ln(requests reaching the SSD / bytes written) at the best single size cut
              (the reuse spread one size cut separates), and eta = its share of the total spread
Writes work/indicator/report.html, table_indicator.csv, fig_indicator.svg.   python3 indicator_report.py <work>"""
import csv, glob, html, json, math, os, re, sys
WORK = sys.argv[1]; D = f"{WORK}/indicator"
PROP, BASE0 = "ru-otsu-ghost-cb", "ru-none-nat-cb"
COND = [("no_filter", "", "篩なし"), ("filter", "-adm", "篩あり")]
PAL = ["#2b6cb0", "#c05621", "#2f855a", "#6b46c1", "#b83280", "#2c7a7b", "#975a16", "#4a5568"]

def load(p):
    try: return json.load(open(p))
    except (OSError, ValueError): return None

def gain(pj):   # measured class gain from a paper result JSON: (ΔBMR pt, ΔOHR pt, measured turnover of the no-class run) or Nones
    if not pj: return None, None, None
    R = pj.get("runs", {}); p, b = R.get(PROP), R.get(BASE0); ssd = (pj.get("config") or {}).get("ssd")
    if not (p and b): return None, None, None
    to = b["write"] * b["req_bytes"] / ssd if ssd else None
    return 100 * (p["bmr"] - b["bmr"]), 100 * (p["ohr"] - b["ohr"]), to

def sens_report():
    """--sens：paper-sens の点（ssd_x*・dram_*・ru_*M）ごとに、指標（RU の点は既定の点の指標）と分類の得を並べる → work/indicator/sens/"""
    SD = f"{D}/sens"; os.makedirs(SD, exist_ok=True)
    pts = sorted({os.path.basename(p) for p in glob.glob(f"{WORK}/paper/sens/*") if os.path.isdir(p) and re.match(r"(ssd_x|dram_|ru_)", os.path.basename(p))})
    names = sorted(os.path.basename(f)[:-5] for f in glob.glob(f"{D}/*.json"))
    rows = []
    for n in names:
        d0, p0 = load(f"{D}/{n}.json"), load(f"{WORK}/paper/{n}.json")
        if not d0 or not p0: continue
        c0 = p0.get("config") or {}
        for pt in ["default"] + pts:
            if pt == "default": dI, pj, grp = d0, p0, None
            else:
                pj = load(f"{WORK}/paper/sens/{pt}/{n}.json"); grp = pt.split("_")[0]
                dI = d0 if grp == "ru" else load(f"{SD}/{pt}/{n}.json")
            if not pj or not dI: continue
            c = pj.get("config") or c0
            g, go, tom = gain(pj)
            x = dict(ssd=c.get("ssd", 0) / c0["ssd"] if c0.get("ssd") else None, dram=c["ssd"] / c["dram"] if c.get("dram") else None,
                     ru=c.get("region", 0) / 1048576 if c.get("region") else None)
            for gr in (["ssd", "dram", "ru"] if pt == "default" else [grp]):
                rows.append(dict(trace=n, group=gr, point=pt, x=x[gr], turnover=dI["no_filter"]["turnover"], turnover_meas=tom,
                                 spread=dI["no_filter"]["spread_between"], eta=dI["no_filter"]["eta"], cut_log2=dI["no_filter"]["cut_log2"],
                                 turnover_filter=dI["filter"]["turnover"], spread_filter=dI["filter"]["spread_between"],
                                 meas_dbmr=g, meas_dohr=go))
    if not rows: sys.exit(f"{WORK}/paper/sens/ の結果か {D}/*.json が無い（./run.sh paper-sens、./run.sh indicator --sens）")
    rows.sort(key=lambda r: (r["trace"], r["group"], r["x"] or 0))
    with open(f"{SD}/table_indicator_sens.csv", "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)
    GL = {"ssd": "SSD 容量（既定の何倍）", "dram": "DRAM（SSD ÷ DRAM）", "ru": "Region = RU [MiB]"}
    ML = [("turnover", "入れ替わり（実線＝予言、破線＝実測）", True), ("spread", "大きさで分けられるばらつき", True),
          ("meas_dbmr", "分類の得 ΔBMR [pt]（実測、負が良い）", False), ("meas_dohr", "分類の得 ΔOHR [pt]（実測、正が良い）", False)]
    figs = []
    for n in sorted({r["trace"] for r in rows}):
        o = []
        for gi, gr in enumerate(("ssd", "dram", "ru")):
            rr = [r for r in rows if r["trace"] == n and r["group"] == gr and r["x"]]
            x0 = 80 + gi * 300; o.append(f'<text x="{x0 + 110}" y="28" text-anchor="middle" font-weight="700" font-size="15">{GL[gr]}</text>')
            if not rr: o.append(f'<text x="{x0 + 110}" y="120" text-anchor="middle" font-size="12" fill="#999">点なし</text>'); continue
            xs = [math.log(r["x"]) for r in rr]; xl, xh = min(xs), max(xs); xl, xh = (xl - 0.5, xh + 0.5) if xh == xl else (xl - 0.1 * (xh - xl), xh + 0.1 * (xh - xl))
            X = lambda v: x0 + (math.log(v) - xl) / (xh - xl) * 220
            for mi, (key, lab, lg) in enumerate(ML):
                y0 = 78 + mi * 200; H = 140
                vals = [r[key] for r in rr if r[key] is not None] + ([r["turnover_meas"] for r in rr if r["turnover_meas"]] if key == "turnover" else [])
                if gi == 0: o.append(f'<text x="80" y="{y0 - 9}" font-size="13" font-weight="700" fill="#444">{lab}</text>')
                o.append(f'<rect x="{x0}" y="{y0}" width="220" height="{H}" fill="none" stroke="#ccc"/>')
                if not vals: continue
                if lg: vals = [max(v, 1e-5) for v in vals]; tr = math.log10
                else: vals = vals + [0]; tr = lambda v: v
                lo, hi = min(map(tr, vals)), max(map(tr, vals)); pad = 0.1 * (hi - lo or 1); lo, hi = lo - pad, hi + pad
                Y = lambda v: y0 + H - (tr(max(v, 1e-5) if lg else v) - lo) / (hi - lo) * H
                if not lg: o.append(f'<line x1="{x0}" x2="{x0 + 220}" y1="{Y(0):.1f}" y2="{Y(0):.1f}" stroke="#999"/>')
                if key == "turnover" and lo < 0.301 < hi: o.append(f'<line x1="{x0}" x2="{x0 + 220}" y1="{Y(2):.1f}" y2="{Y(2):.1f}" stroke="#c53030" stroke-dasharray="4 3"/><text x="{x0 + 218}" y="{Y(2) - 3:.1f}" text-anchor="end" font-size="10" fill="#c53030">2 回</text>')
                for ser, dash in ((key, ""), ("turnover_meas", ' stroke-dasharray="5 4"') if key == "turnover" else (None, "")):
                    if not ser: continue
                    pp = [(X(r["x"]), Y(r[ser])) for r in rr if r[ser] is not None]
                    if pp: o.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pp)}" fill="none" stroke="#2b6cb0" stroke-width="2"{dash}/>' + "".join(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3.5" fill="#2b6cb0"/>' for a, b in pp))
                for r in rr:
                    if r[key] is not None: o.append(f'<text x="{X(r["x"]):.1f}" y="{Y(r[key]) - 7:.1f}" text-anchor="middle" font-size="10">{r[key]:.3g}</text>')
                if mi == len(ML) - 1:
                    for r in rr: o.append(f'<text x="{X(r["x"]):.1f}" y="{y0 + H + 14}" text-anchor="middle" font-size="10" fill="#666">{r["x"]:.3g}{"（既定）" if r["point"] == "default" else ""}</text>')
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 980 890" font-family="Noto Sans JP, Yu Gothic UI, sans-serif"><rect width="980" height="890" fill="#fff"/>{"".join(o)}</svg>'
        open(f"{SD}/fig_sens_{n}.svg", "w", encoding="utf-8").write(svg); figs.append(n)
    f2 = lambda v, s_: "–" if v is None else format(v, s_)
    trs = "".join(f"<tr><td>{html.escape(r['trace'])}</td><td>{r['group']}</td><td>{html.escape(r['point'])}</td><td class=n>{f2(r['x'], '.3g')}</td>"
                  f"<td class=n>{r['turnover']:.2f}</td><td class=n>{f2(r['turnover_meas'], '.2f')}</td><td class=n>{r['spread']:.4f}</td><td class=n>{r['eta']:.2f}</td>"
                  f"<td class=n>{r['turnover_filter']:.2f}</td><td class=n>{f2(r['meas_dbmr'], '+.3f')}</td><td class=n>{f2(r['meas_dohr'], '+.2f')}</td></tr>" for r in rows)
    open(f"{SD}/report.html", "w", encoding="utf-8").write(f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>指標の感度</title>
<style>:root{{--bg:#fbfaf7;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9}}@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.75 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}}main{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}}
img{{width:100%;background:#fff;border:1px solid var(--line);border-radius:6px;margin:6px 0}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}.m{{color:var(--mut);font-size:13px}}</style></head><body><main>
<h1>提案が効くかの指標：容量・DRAM・RU を変えたとき</h1>
<p class="m">paper-sens の点ごとに、トレースだけから出した指標（og_ind。RU の点は Region の大きさがモデルに入らないので既定の点の指標）と、その点の実測の分類の得（{PROP} − {BASE0}）を並べた。
段は上から、SSD の入れ替わり（実線＝予言、破線＝分類しない run の実測、赤の破線＝2 回）、大きさで分けられる再利用のばらつき（大津の得点）、分類の得 ΔBMR（負が良い）、分類の得 ΔOHR（正が良い）。
容量を増やして得が消える（あるいは逆転する）とき、入れ替わりやばらつきが同じ向きに下がっていれば、指標で事前に予言できたことになる。</p>
{"".join(f'<h2>{html.escape(n)}</h2><img src="fig_sens_{html.escape(n)}.svg" alt="{html.escape(n)} の感度">' for n in figs)}
<table><tr><th>トレース</th><th>軸</th><th>点</th><th>値</th><th>入れ替わり（予言）</th><th>入れ替わり（実測）</th><th>ばらつき</th><th>η</th><th>入れ替わり（篩あり、予言）</th><th>ΔBMR [pt]</th><th>ΔOHR [pt]</th></tr>{trs}</table>
<p class="m">表の元：table_indicator_sens.csv。作り直し：./run.sh indicator --sens（指標の計算から）、./run.sh indicator-report --sens（表と図だけ）。</p></main></body></html>""")
    print(f"indicator --sens: {len(figs)} トレース、{len(pts)} 点 → {SD}/report.html")

if "--sens" in sys.argv: sens_report(); sys.exit(0)
ind = {os.path.basename(f)[:-5]: load(f) for f in sorted(glob.glob(f"{D}/*.json"))}
ind = {k: v for k, v in ind.items() if v and "no_filter" in v}
if not ind: sys.exit(f"{D}/*.json が無い（./run.sh indicator で作る）")
rows = []
for n, d in ind.items():
    runs = {}
    for src in (f"{WORK}/paper/{n}.json", f"{WORK}/paper/ext/{n}.json"):
        P = load(src)
        if P: runs.update(P.get("runs", {}))
    for key, suf, lab in COND:
        x = d[key]; p, b = runs.get(PROP + suf), runs.get(BASE0 + suf)
        r = dict(trace=n, category=d.get("category", ""), source=d.get("source", ""), cond=lab, C_over_fp=d["C_over_fp"],
                 one_hit_frac=d["one_hit_frac"], turnover=x["turnover"], spread=x["spread_between"], spread_total=x["spread_total"],
                 eta=x["eta"], cut_log2=x["cut_log2"], model_bhr=x["model_bhr"],
                 meas_dbmr=100 * (p["bmr"] - b["bmr"]) if p and b else None, meas_dohr=100 * (p["ohr"] - b["ohr"]) if p and b else None,
                 base_bmr=100 * b["bmr"] if b else None)
        r["meas_rel_dbmr"] = 100 * r["meas_dbmr"] / r["base_bmr"] if r["meas_dbmr"] is not None and r["base_bmr"] else None
        rows.append(r)
with open(f"{D}/table_indicator.csv", "w", newline="") as f:
    w = csv.DictWriter(f, list(rows[0])); w.writeheader(); w.writerows(rows)

cats = sorted({r["category"] or "-" for r in rows}); col = {c: PAL[i % len(PAL)] for i, c in enumerate(cats)}
def panel(x0, cond, key, ylab):
    pts = [r for r in rows if r["cond"] == cond and r[key] is not None]
    S, top = 380, 70
    xs = [math.log10(max(r["spread"], 1e-4)) for r in pts] or [-4, 0]; ys = [r[key] for r in pts] + [0]
    xl, xh = min(xs + [-3]) - 0.2, max(xs + [-1]) + 0.2; yl, yh = min(ys), max(ys); pad = 0.12 * (yh - yl or 1); yl, yh = yl - pad, yh + pad
    X = lambda v: x0 + (math.log10(max(v, 1e-4)) - xl) / (xh - xl) * S; Y = lambda v: top + S - (v - yl) / (yh - yl) * S
    o = [f'<text x="{x0 + S / 2}" y="{top - 30}" text-anchor="middle" font-weight="700" font-size="16">{cond}：{ylab}</text>',
         f'<rect x="{x0}" y="{top}" width="{S}" height="{S}" fill="none" stroke="#bbb"/>',
         f'<line x1="{x0}" x2="{x0 + S}" y1="{Y(0)}" y2="{Y(0)}" stroke="#999"/>',
         f'<text x="{x0 + S / 2}" y="{top + S + 36}" text-anchor="middle" font-size="13">大きさで分けられる再利用のばらつき（対数目盛り）</text>']
    for e in range(math.ceil(xl), math.floor(xh) + 1):
        xv = x0 + (e - xl) / (xh - xl) * S
        o.append(f'<line x1="{xv:.1f}" x2="{xv:.1f}" y1="{top}" y2="{top + S}" stroke="#eee"/><text x="{xv:.1f}" y="{top + S + 16}" text-anchor="middle" font-size="11" fill="#666">1e{e}</text>')
    for v in (yl, 0, yh):
        o.append(f'<text x="{x0 - 6}" y="{Y(v) + 4:.1f}" text-anchor="end" font-size="11" fill="#666">{v:+.2f}</text>')
    for r in pts:
        c = col[r["category"] or "-"]; hol = r["turnover"] < 2
        o.append(f'<circle cx="{X(r["spread"]):.1f}" cy="{Y(r[key]):.1f}" r="7" fill="{"#fff" if hol else c}" stroke="{c}" stroke-width="2.5"><title>{html.escape(r["trace"])} 入れ替わり {r["turnover"]:.2f}</title></circle>'
                 f'<text x="{X(r["spread"]) + 10:.1f}" y="{Y(r[key]) - 8:.1f}" font-size="11">{html.escape(r["trace"])}</text>')
    return "".join(o)
leg = "".join(f'<circle cx="{70 + i * 160}" cy="1540" r="7" fill="{col[c]}"/><text x="{82 + i * 160}" y="1545" font-size="13">{html.escape(c)}</text>' for i, c in enumerate(cats))
leg += '<circle cx="70" cy="1568" r="7" fill="#fff" stroke="#555" stroke-width="2.5"/><text x="82" y="1573" font-size="13">白抜き = 予言した入れ替わりが 2 回未満（追い出しがほとんど起きない）</text>'
svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 1590" font-family="Noto Sans JP, Yu Gothic UI, sans-serif"><rect width="960" height="1590" fill="#fff"/>'
       + panel(90, "篩なし", "meas_dbmr", "分類の得 ΔBMR [pt]（負が良い）") + panel(560, "篩あり", "meas_dbmr", "分類の得 ΔBMR [pt]（負が良い）")
       + '<g transform="translate(0 500)">' + panel(90, "篩なし", "meas_rel_dbmr", "ΔBMR ÷ 基準の BMR [%]") + panel(560, "篩あり", "meas_rel_dbmr", "ΔBMR ÷ 基準の BMR [%]") + "</g>"
       + '<g transform="translate(0 1000)">' + panel(90, "篩なし", "meas_dohr", "分類の得 ΔOHR [pt]（正が良い）") + panel(560, "篩あり", "meas_dohr", "分類の得 ΔOHR [pt]（正が良い）") + "</g>"
       + leg + "</svg>")
open(f"{D}/fig_indicator.svg", "w", encoding="utf-8").write(svg)

f2 = lambda v, s: "–" if v is None else format(v, s)
trs = "".join(f"<tr><td>{html.escape(r['trace'])}</td><td>{html.escape(r['category'])}/{html.escape(r['source'])}</td><td>{r['cond']}</td>"
              f"<td class=n>{r['turnover']:.2f}</td><td class=n>{r['spread']:.4f}</td><td class=n>{r['eta']:.2f}</td><td class=n>2^{r['cut_log2']}</td>"
              f"<td class=n>{f2(r['meas_dbmr'], '+.3f')}</td><td class=n>{f2(r['meas_rel_dbmr'], '+.2f')}</td><td class=n>{f2(r['meas_dohr'], '+.2f')}</td></tr>" for r in rows)
open(f"{D}/report.html", "w", encoding="utf-8").write(f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>効くかの指標</title>
<style>:root{{--bg:#fbfaf7;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9}}@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.75 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}}main{{max-width:1100px;margin:0 auto;padding:20px 16px 60px}}
img{{width:100%;background:#fff;border:1px solid var(--line);border-radius:6px}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}.m{{color:var(--mut);font-size:13px}}</style></head><body><main>
<h1>提案が効くワークロードの事前の指標</h1>
<p class="m">指標はトレースだけから計算する（og_ind：コンテンツ単位の Che 近似、DRAM を前段の LRU として、paper と同じ SSD・DRAM の大きさ。篩は「2 回以上リクエストされるコンテンツだけ入れる」で近似）。
分類の得 = {PROP} − {BASE0}（篩ありは両方に -adm）、paper の結果から。</p>
<ul class="m"><li><b>入れ替わり</b>：SSD の容量が何回書かれるか（圧力）。2 回より小さいと追い出しがほとんど起きず、書き分けも配分も効かない。</li>
<li><b>ばらつき</b>：大きさの段（大津と同じ [2^b, 2^(b+1))）ごとの ln（SSD に届くリクエスト ÷ 書いたバイト）を、大きさの線 1 本で分けたときの大津の得点。η はその全体に対する割合、線はそのときの切れ目。</li>
<li>限界：コンテンツ単位のモデルなので、Region の巻き添えは入らない。巨大なコンテンツを分割して持つトレース（nha など）や、容量が足跡のごく一部のトレースでは、入れ替わりの水準が合わない。時間局所性も入らない（IRM）。</li></ul>
<img src="fig_indicator.svg" alt="指標と分類の得">
<table><tr><th>トレース</th><th>category/source</th><th>篩</th><th>入れ替わり</th><th>ばらつき</th><th>η</th><th>切れ目</th><th>ΔBMR [pt]</th><th>ΔBMR÷BMR [%]</th><th>ΔOHR [pt]</th></tr>{trs}</table>
<p class="m">表の元：table_indicator.csv。指標だけを作り直す：./run.sh indicator、表と図だけ：./run.sh indicator-report。</p></main></body></html>""")
print(f"indicator: {len(ind)} トレース → {D}/report.html・fig_indicator.svg・table_indicator.csv"
      + ("" if any(r["meas_dbmr"] is not None for r in rows) else f"（paper の結果に {PROP}・{BASE0} が無いので、分類の得は空）"))
