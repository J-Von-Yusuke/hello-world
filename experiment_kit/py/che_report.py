#!/usr/bin/env python3
"""che_report.py — work/che/*.json（che_one.py の出力）を 1 枚の report.html と che_summary.csv にまとめる。
  python3 che_report.py <work/che>

読み方（付録 §N.3〜N.5）：Zipf の傾き α が大きいほど
  (a) 上限（物単位 LRU のミス率）が小さく、静的分割の予測利得も小さい
  (b) 最適な小物の取り分は下がるのに、Region LRU の自然な取り分は下がらない（行き過ぎ）
  (c) 熱い物に固定された Region（先頭 10% に封じて最後まで残る）が増える
の 3 つが揃えば、「傾きの大きいトレースでは分類が逆効果」をモデルの筋で説明できる。揃わなければ IRM の外（人気の時間変化など）を疑う。
"""
import csv, glob, html, json, os, sys

D = sys.argv[1]
rows = []
for f in sorted(glob.glob(f"{D}/*.json")):
    try:
        d = json.load(open(f))
    except ValueError:
        continue
    if "summary" in d: rows.append(d)
if not rows:
    sys.exit(f"che_report: {D}/*.json が無い（先に ./run.sh che）")

COLS = [("name", "トレース"), ("category", "種類"), ("source", "出典"),
        ("alpha_all", "α（全体）"), ("alpha_cls", "α（分類ごと）"), ("req_share_small", "小物の要求の割合"),
        ("ceiling_sim", "上限：物 LRU のミス率（実測）"), ("ceiling_model", "同（Che）"), ("model_lru_err", "Che − 実測の OHR"),
        ("pred_gain_ohr", "予測：静的分割の OHR 利得"), ("pred_gain_bhr", "予測：BHR 最適分割の BHR 利得"), ("pred_gain_j", "予測：J 最適の J 利得"),
        ("opt_small_share_ohr", "最適な小物の取り分（OHR）"), ("opt_small_share_j", "同（J）"), ("lru_small_share", "共有 LRU の小物の取り分"),
        ("nat_small_share", "Region LRU 分類の小物の取り分（実測、後半平均）"),
        ("meas_gain_ohr", "実測：分類 − 混在の OHR"), ("meas_gain_bmr", "実測：分類 − 混在の BMR"),
        ("pinned_first10", "先頭 10% に封じて残った Region の割合"), ("st_taus", "split_threshold の τ（終了時）"),
        ("st_tau_changes", "τ が変わった回数"), ("model_taus", "予測に使った τ")]


def fmt(v, k):
    if v is None: return ""
    if isinstance(v, list): return ", ".join(fmt(x, k) for x in v)
    if isinstance(v, float):
        if k.startswith(("alpha",)): return f"{v:.2f}"
        if "gain" in k or k == "model_lru_err": return f"{v * 100:+.1f}pt"
        return f"{v * 100:.1f}%"
    if k.endswith("taus") and isinstance(v, int): return str(v)
    return str(v)


with open(f"{D}/che_summary.csv", "w", newline="") as f:
    wr = csv.writer(f); wr.writerow([k for k, _ in COLS])
    for d in rows:
        s = dict(d["summary"], name=d["name"], category=d["category"], source=d["source"])
        wr.writerow([json.dumps(s.get(k)) if isinstance(s.get(k), list) else s.get(k) for k, _ in COLS])


def scatter(xk, yk, xl, yl, diag=False, pct_x=False):
    pts = [(d["summary"].get(xk), d["summary"].get(yk), d["name"]) for d in rows]
    pts = [(x, y, n) for x, y, n in pts if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if not pts: return f"<p class=mut>{html.escape(yl)} と {html.escape(xl)}：値が無い</p>"
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs); y0, y1 = min(ys + [0]), max(ys + [0])
    if diag: x0 = y0 = min(x0, y0); x1 = y1 = max(x1, y1)
    px = lambda x: 60 + 340 * (x - x0) / ((x1 - x0) or 1); py = lambda y: 250 - 220 * (y - y0) / ((y1 - y0) or 1)
    g = [f'<line x1="60" y1="250" x2="400" y2="250" stroke="var(--mut)"/><line x1="60" y1="30" x2="60" y2="250" stroke="var(--mut)"/>']
    if y0 < 0 < y1: g.append(f'<line x1="60" y1="{py(0):.1f}" x2="400" y2="{py(0):.1f}" stroke="var(--line)" stroke-dasharray="4 3"/>')
    if diag: g.append(f'<line x1="{px(x0):.1f}" y1="{py(y0):.1f}" x2="{px(x1):.1f}" y2="{py(y1):.1f}" stroke="var(--line)" stroke-dasharray="4 3"/>')
    for x, y, n in pts:
        g.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="5" fill="var(--a)"><title>{html.escape(n)}</title></circle>'
                 f'<text x="{px(x) + 7:.1f}" y="{py(y) - 6:.1f}" font-size="11">{html.escape(n)}</text>')
    lab = lambda v, p: f"{v * 100:.0f}%" if p else f"{v:.2f}"
    g.append(f'<text x="60" y="268" text-anchor="middle" font-size="11">{lab(x0, pct_x)}</text><text x="400" y="268" text-anchor="middle" font-size="11">{lab(x1, pct_x)}</text>'
             f'<text x="54" y="254" text-anchor="end" font-size="11">{y0 * 100:.0f}</text><text x="54" y="34" text-anchor="end" font-size="11">{y1 * 100:.0f}</text>'
             f'<text x="250" y="290" text-anchor="middle">{html.escape(xl)}</text><text x="14" y="140" transform="rotate(-90 14 140)" text-anchor="middle">{html.escape(yl)}</text>')
    return f'<svg viewBox="0 0 470 300" role="img" aria-label="{html.escape(yl)}">{"".join(g)}</svg>'


head = "".join(f"<th>{html.escape(l)}</th>" for _, l in COLS)
body = ""
for d in rows:
    s = dict(d["summary"], name=d["name"], category=d["category"], source=d["source"])
    body += "<tr>" + "".join(f"<td>{html.escape(fmt(s.get(k), k))}</td>" for k, _ in COLS) + "</tr>"

figs = [scatter("alpha_all", "meas_gain_ohr", "Zipf の傾き α（全体）", "実測 分類 − 混在 OHR [pt]"),
        scatter("alpha_all", "meas_gain_bmr", "Zipf の傾き α（全体）", "実測 分類 − 混在 BMR [pt]（正 = 悪化）"),
        scatter("alpha_all", "pred_gain_ohr", "Zipf の傾き α（全体）", "予測 静的分割の OHR 利得 [pt]"),
        scatter("pred_gain_ohr", "meas_gain_ohr", "予測 静的分割の OHR 利得（上限の目安）", "実測 分類 − 混在 OHR [pt]", pct_x=True),
        scatter("opt_small_share_ohr", "nat_small_share", "最適な小物の取り分（OHR、Che）", "Region LRU 分類の小物の取り分（実測）[%]", diag=True, pct_x=True),
        scatter("alpha_all", "pinned_first10", "Zipf の傾き α（全体）", "先頭 10% に封じて残った Region [%]")]

page = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Zipf の傾きと分類の利得</title><style>
:root{{--bg:#fff;--fg:#1d2330;--mut:#5b6577;--card:#f4f6fa;--line:#d5dbe6;--a:#2b6cb0}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#14171d;--fg:#e6e9ef;--mut:#9aa4b5;--card:#1d222b;--line:#2f3744;--a:#63b3ed}}}}
:root[data-theme="dark"]{{--bg:#14171d;--fg:#e6e9ef;--mut:#9aa4b5;--card:#1d222b;--line:#2f3744;--a:#63b3ed}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.7 system-ui,"Hiragino Sans","Yu Gothic",sans-serif}}
main{{max-width:1200px;margin:0 auto;padding:20px 16px 60px}}.mut{{color:var(--mut)}}
.tbl{{overflow-x:auto}}table{{border-collapse:collapse;font-size:12.5px}}th,td{{border-bottom:1px solid var(--line);padding:4px 7px;text-align:right;white-space:nowrap}}th{{color:var(--mut);white-space:normal;min-width:70px;vertical-align:bottom}}
td:first-child,th:first-child{{text-align:left}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}
svg{{max-width:100%;height:auto;display:block}}svg text{{fill:var(--fg);font-size:12px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px;margin:12px 0}}
</style></head><body><main>
<h1 style="font-size:21px">Zipf の傾きと分類の利得（付録 §N.5 の確認）</h1>
<p class="mut">{len(rows)} 本。予測は Che 近似（IRM、λ = 要求回数 ÷ 全要求、物単位 LRU の分割。Region の効果は入らない）。実測は RegionFlash（分類なし = none、分類 = split_threshold・Region LRU・下限なし）と、上限の素の LRU（cachesim lru、SSD と同じ容量、DRAM なし）。CSV は che_summary.csv。</p>
<div class="card"><b>読み方</b>
<ol>
<li><b>上限</b>：物単位 LRU のミス率より多くは、分類でも取り返せない。α が大きいトレースでここが数 % なら、利得が出ないことはそれだけで説明できる。</li>
<li><b>予測利得と実測</b>：静的分割の予測利得は Region の損失を含まない上限の目安。実測がこれを大きく下回り、しかも負なら、Region LRU の配分が悪い（次の 3）か IRM の外。</li>
<li><b>取り分</b>：Region LRU の自然な小物の取り分が最適（対角線）より上にあれば「小物へ寄せ過ぎ」。付録 §N.2 の予想では、α が大きいほど上にずれる。</li>
<li><b>固定された Region</b>：先頭 10% に封じて最後まで残った Region の割合。α が大きいほど増えるなら、熱い物に固定されて回らない Region が容量を抱えている（§N.4）。</li>
<li><b>Che − 実測の OHR</b>：IRM の当てはまり。大きく負なら時間局所性が強く、大きく正なら再要求の間隔が規則的（または人気の入れ替わり）で、どちらも Che の数字（予測利得・最適な取り分）は目安にとどまる。</li>
<li><b>τ が変わった回数</b>：split_threshold が実行中に閾値を変えた回数。多いトレースでは、取り分の時系列が別々の閾値の分類を混ぜた数字になり、終了時の τ（予測に使った τ）とも対応しない。そのトレースは tune の固定閾値（hint=fixed）の結果で確かめる。</li>
</ol></div>
<div class="grid">{"".join(figs)}</div>
<h2 style="font-size:17px">トレースごと</h2><div class="tbl"><table><tr>{head}</tr>{body}</table></div>
</main></body></html>"""
open(f"{D}/report.html", "w").write(page)
print(f"{D}/report.html, {D}/che_summary.csv（{len(rows)} 本）")
