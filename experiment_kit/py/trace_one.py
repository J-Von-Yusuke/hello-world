#!/usr/bin/env python3
"""trace_one.py — 1 本のトレース（1 変種）の RegionFlash 実験結果を 1 つの JSON にまとめる。

  python3 trace_one.py <name> <asis|fix|drop|delete> <work/out/name/var>   →  work/results/<name>.<var>.json

入力（run.sh が作る）: lru.csv（cdn_prefix_check: 物単位 LRU と K(N)）、<id>/P.interval.csv（RegionFlash の累積値）、
                      info.json（og_info）、../stats.csv（og_trace_stats の窓ごとの値）
定義（すべて「先頭 N 要求」での値。要求バイト req_bytes で割って %）:
  bmr(id, N)      = miss_bytes / req_bytes
  効果 eff        = bmr(分類なし) − bmr(分類あり)   [pt]  正 = 分類で BMR が下がった。再挿入あり同士・予算あり同士で比べる
  collateral      = bmr(none) − bmr(物単位 LRU)     [pt]  Region 単位で消すことによる余分なミス
  conv            = 以後ずっと |eff(N') − eff(全量)| ≤ tol となる最小の N（tol = 0.1 pt, 0.02 pt）。小さいほど部分トレースで足りる
  sign_at         = 先頭 1/3/10/31% の時点で効果の符号が全量と一致するか
"""
import csv, json, math, os, re, sys

name, var, O = sys.argv[1], sys.argv[2], sys.argv[3]
WORK = os.path.abspath(os.path.join(O, "..", "..", ".."))
cfg = json.load(open(f"{O}/config.json")); info = json.load(open(f"{O}/info.json"))
ssd = cfg["ssd"]

# ---- 物単位 LRU
lru = {}
for r in csv.DictReader(l for l in open(f"{O}/lru.csv") if not l.startswith("#")):
    lru[int(r["n"])] = {k: int(v) for k, v in r.items()}

# ---- RegionFlash の累積値
KEEP = ("req_bytes", "miss_bytes", "flash_region_write_bytes", "reinsert_bytes", "ssd_admit_bytes")
runs = {}
for d in sorted(os.listdir(O)):
    f = f"{O}/{d}/P.interval.csv"
    if not os.path.exists(f"{O}/{d}/P.json") or not os.path.exists(f): continue
    s = {}
    for r in csv.DictReader(open(f)):
        if r["kind"] == "cumulative": s[int(r["reqs"])] = {k: int(r[k]) for k in KEEP}
    if s: runs[d] = s
if "none" not in runs: sys.exit(f"{name}/{var}: none の run が無い（失敗した？ {O}/none/stdout を見る）")
ns = sorted(set(lru).intersection(*[set(s) for s in runs.values()]))
FULL = ns[-1]
t_sec = max(lru[FULL]["trace_sec"], 1)   # NREQ で打ち切った run では、その範囲の経過秒
bmr = lambda i, n: 100.0 * runs[i][n]["miss_bytes"] / runs[i][n]["req_bytes"]
lbmr = lambda n: 100.0 * lru[n]["lru_miss_bytes"] / lru[n]["req_bytes"]

# ---- 効果の組: (分類あり id) → (基準 id)
base_of = {}
for i in runs:
    m = re.match(r"^(d\d+_)?(.+?)(_h1)?$", i)
    if i.startswith("obj_") or not m or m[2] == "none": continue
    b = (m[1] or "") + "none" + (m[3] or "")
    if b in runs: base_of[i] = b
obj_of = {i: "obj_" + i.split("_")[0] for i in runs if re.match(r"^d\d+_none$", i) and ("obj_" + i.split("_")[0]) in runs}
eff = lambda i, n: bmr(base_of[i], n) - bmr(i, n)

out = dict(name=name, variant=var, config=cfg, info=info, full_n=FULL, K_full=lru[FULL]["K_pieces"], t_sec=t_sec)
# ---- トレースの特徴（og_trace_stats の窓の平均）
feat = dict(mean_size=info["req_bytes"] / max(info["n"], 1), size_change=info["size_change_reqs"] / max(info["n"], 1),
            sat_frac=info["sat_reqs"] / max(info["n"], 1), uniq=info["uniq"], footprint=info["footprint"],
            req_per_obj=info["n"] / max(info["uniq"], 1), days=t_sec / 86400.0)
sf = f"{O}/../stats.csv"
if os.path.exists(sf):
    rows = list(csv.DictReader(open(sf)))
    for k, c in (("var_logs_b", "b:all:var_s"), ("var_loglam_b", "b:all:var_lam"), ("size_share_b", "b:all:share_size"),
                 ("var_logs_r", "r:all:var_s"), ("size_share_r", "r:all:share_size"), ("rho", "rho"), ("gini", "gini")):
        if rows and c in rows[0]: feat[k] = sum(float(r[c]) for r in rows) / len(rows)
out["feat"] = feat

# ---- 全量での値
full = {i: dict(bmr=bmr(i, FULL), write=runs[i][FULL]["flash_region_write_bytes"], reinsert=runs[i][FULL]["reinsert_bytes"],
                admit=runs[i][FULL]["ssd_admit_bytes"]) for i in runs}
out["full"] = full
out["lru"] = dict(bmr=lbmr(FULL), cold=100.0 * lru[FULL]["cold_bytes"] / lru[FULL]["req_bytes"])
out["collateral"] = dict(all=bmr("none", FULL) - lbmr(FULL))
for i, o in obj_of.items(): out["collateral"][i.split("_")[0]] = bmr(i, FULL) - bmr(o, FULL)
out["effect"] = {i: eff(i, FULL) for i in base_of}
out["dwpd_all"] = full["none"]["write"] / ssd / (t_sec / 86400.0)
out["write_TB"] = full["none"]["write"] / 1e12
if "d3_none" in runs: out["dwpd_d3_real"] = full["d3_none"]["write"] / ssd / (t_sec / 86400.0)

# ---- 先頭 N の曲線と収束
pts = sorted({min(ns, key=lambda x, t=t: abs(math.log(x) - math.log(t))) for t in
              [ns[0] * (FULL / ns[0]) ** (k / 39) for k in range(40)]} | {FULL}) if len(ns) > 1 else ns
out["curve"] = dict(n=pts, K=[lru[n]["K_pieces"] for n in pts], bmr_none=[bmr("none", n) for n in pts],
                    bmr_lru=[lbmr(n) for n in pts], collateral=[bmr("none", n) - lbmr(n) for n in pts],
                    eff={i: [eff(i, n) for n in pts] for i in base_of})
conv, sign = {}, {}
for i in base_of:
    ef = out["effect"][i]; c = {}
    for tol in (0.1, 0.02):
        ok = None
        for n in reversed(ns):
            if abs(eff(i, n) - ef) <= tol: ok = n
            else: break
        c[str(tol)] = dict(n=ok, frac=None if ok is None else ok / FULL, K=None if ok is None else lru[ok]["K_pieces"])
    conv[i] = c
    sg = {}
    for fr in (0.01, 0.03, 0.1, 0.31):
        n = min((x for x in ns if x >= fr * FULL), default=FULL)
        sg[str(fr)] = None if abs(ef) < 0.005 else (eff(i, n) > 0) == (ef > 0)
    sign[i] = sg
out["conv"], out["sign_at"] = conv, sign
# ---- 「先頭 W TB 書き込んだ時点」: 各構成の基準（分類なし・同じ条件）の SSD 書き込み量が W に達する最初の N で、効果が全量と合うか。
#      予算つきの構成は書き込みが遅いので、全受け入れの N ではなく、その基準の書き込み量で数える。
wb = lambda i, n: runs[base_of[i]][n]["flash_region_write_bytes"] / 1e12
wtb = lambda n: runs["none"][n]["flash_region_write_bytes"] / 1e12
out["curve"]["write_TB"] = [wtb(n) for n in pts]
out["full_write_TB"] = wtb(FULL)
out["full_write_TB_by_key"] = {i: wb(i, FULL) for i in base_of}
sgn = lambda i, n: (eff(i, n) > 0) == (out["effect"][i] > 0)
aw = {}
for W in [float(x) for x in os.environ.get("WRITE_MARKS_TB", "1.5").split()]:
    nk = {i: next((n for n in ns if wb(i, n) >= W), None) for i in base_of}
    nw = next((n for n in ns if wtb(n) >= W), None)
    aw[str(W)] = None if nw is None else dict(
        n=nw, frac=nw / FULL, K=lru[nw]["K_pieces"], write_TB=wtb(nw), req_TB=runs["none"][nw]["req_bytes"] / 1e12,
        bmr_none=bmr("none", nw), bmr_lru=lbmr(nw), collateral=bmr("none", nw) - lbmr(nw),
        n_key=nk, eff={i: None if n is None else eff(i, n) for i, n in nk.items()},
        diff={i: None if n is None else eff(i, n) - out["effect"][i] for i, n in nk.items()},
        sign_ok={i: None if n is None or abs(out["effect"][i]) < 0.005 else sgn(i, n) for i, n in nk.items()})
out["at_writes"] = aw
# 効果の符号が、以後ずっと全量と同じになる最小の書き込み量 [TB]（基準の書き込み量で数える）。効果が小さい（|全量| < 0.005 pt）ものは None
stable = {}
for i in base_of:
    if abs(out["effect"][i]) < 0.005: stable[i] = None; continue
    ok = None
    for n in reversed(ns):
        if sgn(i, n): ok = n
        else: break
    stable[i] = None if ok is None else dict(n=ok, frac=ok / FULL, write_TB=wb(i, ok))
out["sign_stable"] = stable
os.makedirs(f"{WORK}/results", exist_ok=True)
json.dump(out, open(f"{WORK}/results/{name}.{var}.json", "w"), indent=1)
print(f"{name}/{var}: N={FULL} BMR none={bmr('none', FULL):.2f}% LRU={lbmr(FULL):.2f}% collateral={out['collateral']['all']:+.2f}pt "
      f"DWPD(all)={out['dwpd_all']:.1f}  効果: " + " ".join(f"{i}={v:+.3f}" for i, v in out["effect"].items() if not i.startswith("d")), file=sys.stderr)
