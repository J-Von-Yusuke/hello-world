#!/usr/bin/env python3
"""tune_aggregate.py — tune の結果（work/tune/<name>.json）を、構成ごと・出典・種類ごとにまとめる。aggregate.py から呼ばれる（単独でも動く）。

  python3 tune_aggregate.py <kit> <work> [--by category,source]

基準 = none_h1（分類なし＋再挿入 n=1、既存の構成）。差の向きはどれも「正 = 良くなった」:
  d_ohr   = OHR(構成) − OHR(基準)                [pt]
  d_bmr   = BMR(基準) − BMR(構成)                [pt]（BMR の減少）
  d_write = 1 − 書き込み(構成) ÷ 書き込み(基準)   [%]（SSD 書き込みの減少。書き込み = 受け入れ＋再挿入＋詰め物）
  goal    = 3 つとも TOL より大きい（目標の達成）
  gap     = 目的 J = (1−BMR) + w·OHR の、同じトレースの固定の下限の格子（st_lru, st_ff*）の最良との差 [pt]
出力（work/results/）: tune_per_trace.csv, tune_by_<列>.csv, tune_report.html。aggregate.py は同じ表を report.html にも入れる。
標準ライブラリだけ。"""
import csv, glob, html, json, os, statistics as st, sys, time

esc = html.escape
BASE = "none_h1"
FF = ["st_lru", "st_ff50", "st_ff70", "st_ff80", "st_ff90", "st_ff95"]
TOL = 0.02   # pt（d_write は % なので 0.02%）
# 表に出す順（無い構成は飛ばす。TUNE_JOBS に足した構成も末尾に出る）
ORDER = ["mini_hc0.1_auto", "st_hc0.1_auto", "st_ff90_auto", "st_ff90_h3", "st_ff90_h1", "st_lru_auto", "none_auto",
         "mini_lru", "st_hc0.03", "st_hc0.1", "st_hc0.3", "st_hc1", "st_mv0.3_auto", "k3_lru_auto", "k4_lru_auto"]


def load(kit, work):
    meta = {}
    tsv = os.environ.get("TRACES_TSV") or f"{kit}/traces.tsv"
    if os.path.exists(tsv):
        ls = [l.rstrip("\n") for l in open(tsv, encoding="utf-8") if l.strip() and not l.startswith("#")]
        if ls:
            hdr = ls[0].split("\t")
            for l in ls[1:]:
                c = l.split("\t"); c += [""] * (len(hdr) - len(c)); d = dict(zip(hdr, c)); meta[d["name"]] = d
    T = {}
    for f in sorted(glob.glob(f"{work}/tune/*.json")):
        d = json.load(open(f))
        if isinstance(d, dict) and "runs" in d: T[d["name"]] = d
    return meta, T


def rows_of(meta, T, w):
    out = []
    for n, d in T.items():
        R = d["runs"]; b = R.get(BASE)
        if not b: continue
        m = meta.get(n, {})
        info = {k: v for k, v in m.items() if k not in ("name", "path", "params", "note", "format", "ssd")}
        info["category"] = m.get("category") or d.get("category", ""); info["source"] = m.get("source") or d.get("source", "")
        info["category/source"] = f'{info["category"]}/{info["source"]}'
        J = lambda r: (1 - r["bmr"]) + w * r["ohr"]
        ffs = [R[i] for i in FF if i in R]; jbest = max(map(J, ffs)) if ffs else None
        for cid, r in R.items():
            dw = 100 * (1 - r["write"] / b["write"]) if b["write"] else None
            x = dict(name=n, config=cid, variant=d.get("variant", ""), **info, ohr=r["ohr"], bmr=r["bmr"], write=r["write"],
                     reinsert=r["reinsert"], d_ohr=100 * (r["ohr"] - b["ohr"]), d_bmr=100 * (b["bmr"] - r["bmr"]), d_write=dw,
                     gap=100 * (J(r) - jbest) if jbest is not None else None, k=r.get("k"))
            x["goal"] = int(all(v is not None and v > TOL for v in (x["d_ohr"], x["d_bmr"], x["d_write"])))
            if r.get("reauto"): x["n_auto"] = "/".join(str(c["n_mode"]) for c in r["reauto"])
            if r.get("share"): x["share"] = "/".join(f"{v:.2f}" for v in r["share"])
            out.append(x)
    return out


def order(cids):
    return [c for c in ORDER if c in cids] + sorted(c for c in cids if c not in ORDER and c != BASE)


def stat(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return None if not xs else dict(n=len(xs), mean=st.mean(xs), median=st.median(xs), min=min(xs), max=max(xs))


def run(kit, work, by, w):
    meta, T = load(kit, work)
    rows = rows_of(meta, T, w)
    if not rows: return "", 0
    R = f"{work}/results"; os.makedirs(R, exist_ok=True)
    cols = ["name", "config", "category", "source", "variant", "ohr", "bmr", "write", "reinsert", "d_ohr", "d_bmr", "d_write", "goal", "gap",
            "k", "n_auto", "share"]
    cols += sorted({k for r in rows for k in r} - set(cols) - {"category/source"})
    with open(f"{R}/tune_per_trace.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, cols, extrasaction="ignore"); wr.writeheader(); wr.writerows(rows)
    cids = order({r["config"] for r in rows})
    H = [f"<h2>自動推定（tune）: 基準（分類なし＋再挿入 n=1）との差</h2>",
         f"<p class='m'>トレース {len(T)} 本。差はどれも正 = 良くなった: OHR の向上 [pt]、BMR の減少 [pt]、SSD 書き込みの減少 [%]。"
         f"目標達成 = 3 つとも正のトレース数。gap = 目的 (1−BMR)+{w}·OHR の、固定の下限の格子の最良との差 [pt]（0 に近いほど自動で最良に届く）。</p>"]
    for b in by:
        g = {}
        for r in rows: g.setdefault(r.get(b, "") or "(空)", []).append(r)
        with open(f"{R}/tune_by_{b.replace('/', '_')}.csv", "w", newline="") as f:
            wr = csv.writer(f); wr.writerow([b, "config", "metric", "n", "mean", "median", "min", "max", "n_goal", "traces"])
            for gv in sorted(g):
                for c in cids:
                    rs = [r for r in g[gv] if r["config"] == c]
                    for m in ("d_ohr", "d_bmr", "d_write", "gap"):
                        s = stat([r.get(m) for r in rs])
                        if s: wr.writerow([gv, c, m, s["n"], f'{s["mean"]:.4f}', f'{s["median"]:.4f}', f'{s["min"]:.4f}', f'{s["max"]:.4f}',
                                           sum(r["goal"] for r in rs), " ".join(r["name"] for r in rs)])
        H.append(f"<h3>「{esc(b)}」ごと（平均。括弧内 = 最小…最大）</h3>")
        for gv in sorted(g):
            body = []
            for c in cids:
                rs = [r for r in g[gv] if r["config"] == c]
                if not rs: continue
                cell = lambda m, p: (lambda s: "" if s is None else f'{s["mean"]:+.{p}f} <span class="m">({s["min"]:+.{p}f}…{s["max"]:+.{p}f})</span>')(stat([r.get(m) for r in rs]))
                body.append([esc(c), str(len(rs)), cell("d_ohr", 2), cell("d_bmr", 3), cell("d_write", 1), f'{sum(r["goal"] for r in rs)}/{len(rs)}', cell("gap", 2)])
            H.append(f"<p><b>{esc(gv)}</b></p>" + table(["構成", "トレース数", "OHR の向上 pt", "BMR の減少 pt", "書き込みの減少 %", "目標達成", "gap pt"], body))
    H.append("<h3>トレースごと（主な構成）</h3>")
    main = [c for c in ("mini_hc0.1_auto", "st_hc0.1_auto", "st_ff90_auto") if c in cids]
    body = []
    for n in sorted(T, key=lambda n: (next((r["category"] for r in rows if r["name"] == n), ""), n)):
        for c in main:
            r = next((x for x in rows if x["name"] == n and x["config"] == c), None)
            if r: body.append([esc(n), esc(r["category"]), esc(c), f'{r["ohr"]:.4f}', f'{r["bmr"]:.4f}', f'{r["write"]:.4f}',
                               f'{r["d_ohr"]:+.2f}', f'{r["d_bmr"]:+.3f}', "" if r["d_write"] is None else f'{r["d_write"]:+.1f}',
                               "○" if r["goal"] else "×", "" if r["gap"] is None else f'{r["gap"]:+.2f}', esc(r.get("n_auto", ""))])
    H.append(table(["name", "category", "構成", "OHR", "BMR", "書き込み/要求B", "OHR 差", "BMR 減", "書込減 %", "目標", "gap", "n=auto"], body))
    snippet = "\n".join(H)
    page = (f"<!doctype html><html lang='ja'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>自動推定の集計</title><style>{CSS}</style></head><body><main><h1>自動推定（tune）の集計</h1>"
            f"<p class='m'>{time.strftime('%Y-%m-%d %H:%M')} 生成。図は work/tune/figs/index.html。</p>{snippet}</main></body></html>")
    open(f"{R}/tune_report.html", "w", encoding="utf-8").write(page)
    return snippet, len(T)


def table(head, body):
    return ("<div class='wrap'><table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>" +
            "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body) + "</table></div>")


CSS = """:root{--bg:#fbfaf7;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732}}
body{background:var(--bg);color:var(--fg);font:14.5px/1.7 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}main{max-width:1000px;margin:0 auto;padding:20px 16px 60px}
h2{font-size:1.2rem;border-bottom:2px solid var(--line)}.m{color:var(--mut);font-size:12px}.wrap{overflow-x:auto}
table{border-collapse:collapse;font-size:13px;margin:.5em 0}th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}"""

if __name__ == "__main__":
    by = ["category", "source", "category/source"]
    if "--by" in sys.argv: by = sys.argv[sys.argv.index("--by") + 1].split(",")
    _, n = run(sys.argv[1], sys.argv[2], by, float(os.environ.get("TUNE_W", "0.3")))
    print(f"tune の集計: {n} 本 → {sys.argv[2]}/results/tune_report.html, tune_per_trace.csv, tune_by_*.csv" if n else "tune の結果が無い（./run.sh tune / tune-synth がまだ）")
