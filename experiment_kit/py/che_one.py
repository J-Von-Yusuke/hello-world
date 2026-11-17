#!/usr/bin/env python3
"""che_one.py — che の 1 トレース分を 1 つの JSON にまとめる（標準出力）。付録 §N.5 の 4 項目。
  python3 che_one.py <work/che/name> <work/tune/name> <name> <category> <source> <variant> <w>

(1) 上限：物単位 LRU のミス率（Che の予測と、cachesim の素の LRU の実測。どちらも SSD と同じ容量、DRAM なし）
(2) 静的分割の予測：Che 近似（IRM、λ = 回数 ÷ 全要求）で、分けたときの OHR/BHR と最適な容量の取り分
(3) 容量の取り分：split_threshold の RegionFlash で、分類ごとの生きている Region の数（封じた − 追い出した）の時系列
(4) 固定された Region：終了時に残っている Region のうち、トレースの先頭 10% / 50% に封じたもの。追い出した Region の寿命
"""
import csv, json, os, sys

O, T, name, cat, src, var, w = sys.argv[1:8]
w = float(w)


def load(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def run(id):  # che の実行を優先（st_lru は記録つき）、無ければ tune の同じ構成
    for d in (f"{O}/{id}", f"{T}/{id}"):
        P = load(f"{d}/P.json")
        if P:
            t = P["total"]
            return d, dict(ohr=t["ohr"], bmr=t["bmr"], reqs=t["reqs"], taus=P["config"].get("taus"), src="che" if d.startswith(O) else "tune")
    return None, None


def q(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None


che = load(f"{O}/che.json")
cfg = load(f"{O}/config.json") or load(f"{T}/config.json") or {}
out = dict(name=name, category=cat, source=src, variant=var, w=w, config=cfg)
runs = {}
for id in ("none", "st_lru"):
    d, r = run(id)
    if r: runs[id] = r
try:  # 素の LRU（cachesim lru、SSD と同じ容量）："..., miss ratio 0.1118, byte miss ratio 0.0835, ..."
    import re
    ln = open(f"{O}/lru/res").read()
    mr, bmr = (float(x) for x in re.search(r" miss ratio ([\d.]+), byte miss ratio ([\d.]+)", ln).groups())
    runs["lru"] = dict(ohr=1 - mr, bmr=bmr, src="che")
except (OSError, AttributeError):
    pass
out["runs"] = runs

# ---- (1)(2) Che の予測：分類の閾値は st_lru の τ に最も近い組（無ければ最初の組）
if che:
    c = che["capacities"][0]
    sp = [s for s in c["splits"] if s["taus_eff"]]
    mix = next(s for s in c["splits"] if not s["taus_eff"])
    pick = sp[0] if sp else None
    st = (runs.get("st_lru") or {}).get("taus")
    if st and sp:
        import math
        pick = min(sp, key=lambda s: (len(s["taus_eff"]) != len(st), sum(abs(math.log2(a) - math.log2(b)) for a, b in zip(s["taus_eff"], st))))
    J = lambda o: o["bhr"] + w * o["ohr"]
    lru = c["lru"]
    out["model"] = dict(n=che["n"], uniq=che["uniq"], footprint=che["footprint"], one_hit_frac=che["one_hit_frac"],
                        compulsory_miss=che["compulsory_miss"], zipf_all=che["zipf_all"], C=c["C"], C_over_fp=c["C_over_fp"],
                        lru=lru, knap_ohr=c["knap_ohr"], knap_bhr=c["knap_bhr"])
    def summ(s):
        p = s["part"]; jw = p.get(f"J{w:g}")
        return dict(taus=s["taus_eff"], classes=s["classes"], ohr_opt=p["ohr"], bhr_opt=p["bhr"], j_opt=jw,
                    gain_ohr=p["ohr"]["ohr"] - lru["ohr"],                       # OHR 最適の分割 − 共有の LRU（分類で増やせる OHR の上限の目安）
                    gain_bhr=p["bhr"]["bhr"] - lru["bhr"],
                    gain_j=(J(jw) - J(lru)) if jw else None)
    out["model"]["split"] = summ(pick) if pick else None
    out["model"]["splits"] = [summ(s) for s in sp]
    out["model"]["mixed_check"] = mix["part"]["ohr"]   # 1 分類の「分割」= 共有の LRU（一致の確認用）

# ---- (3) 容量の取り分の時系列（split_threshold の RegionFlash）
d = f"{O}/st_lru"
fc = f"{d}/P.interval.class.csv"
N = int(cfg["ssd"] // cfg["region"]) if cfg.get("ssd") and cfg.get("region") else None
if os.path.exists(fc) and N:
    live, ser, n_all = {}, [], (runs.get("st_lru") or {}).get("reqs") or cfg.get("n") or 1
    rows = [r for r in csv.DictReader(open(fc)) if r["kind"] == "interval"]
    for t_end in sorted({int(r["t_end"]) for r in rows}):
        for r in (x for x in rows if int(x["t_end"]) == t_end):
            k = int(r["class"]); live[k] = live.get(k, 0) + int(r["regions_sealed"]) - int(r["regions_evicted"])
        ser.append((t_end / n_all, {k: v / N for k, v in sorted(live.items())}))
    K = max((max(s) for _, s in ser if s), default=0) + 1
    half = [s for x, s in ser if x >= 0.5]
    # split_threshold が τ を変えた回数（分類 0 の上端の変化）。多いと「分類ごとの取り分」は混ざった数字になる
    hi0 = [r["hi"] for r in rows if r["class"] == "0"]
    out["st_tau_changes"] = sum(1 for a, b in zip(hi0, hi0[1:]) if a != b)
    out["share_series"] = [[round(x, 4)] + [round(s.get(k, 0), 4) for k in range(K)] for x, s in ser[:: max(1, len(ser) // 200)]]
    out["share_mean_2nd_half"] = [sum(s.get(k, 0) for s in half) / len(half) for k in range(K)] if half else None

# ---- (4) 固定された Region と寿命
if os.path.exists(f"{d}/enddump") and N:
    n_all = (runs.get("st_lru") or {}).get("reqs") or cfg.get("n") or 1
    end = []
    for ln in open(f"{d}/enddump"):
        f = ln.split(maxsplit=4)
        if f[0] == "S": end.append((int(f[1]), int(f[2]) / n_all))
    ks = sorted({k for k, _ in end})
    out["pinned"] = {str(k): dict(at_end=sum(1 for c, _ in end if c == k) / N,
                                  sealed_first10=sum(1 for c, t in end if c == k and t < 0.1) / N,
                                  sealed_first50=sum(1 for c, t in end if c == k and t < 0.5) / N) for k in ks}
if os.path.exists(f"{d}/evlog"):
    n_all = (runs.get("st_lru") or {}).get("reqs") or cfg.get("n") or 1
    life = {}
    for ln in open(f"{d}/evlog"):
        f = ln.split(maxsplit=5)
        life.setdefault(int(f[1]), []).append(int(f[3]) / n_all)
    out["lifetime"] = {str(k): dict(evicted=len(v), p10=q(v, .1), p50=q(v, .5), p90=q(v, .9)) for k, v in sorted(life.items())}

# ---- まとめの数（report の表の列）
m = out.get("model") or {}; s = m.get("split") or {}
none, st, obj = runs.get("none"), runs.get("st_lru"), runs.get("lru")
out["summary"] = dict(
    alpha_all=(m.get("zipf_all") or {}).get("alpha"),
    alpha_cls=[c["alpha"] for c in s.get("classes", [])],
    req_share_small=s["classes"][0]["req_share"] if s else None,
    ceiling_model=1 - m["lru"]["ohr"] if m else None,                   # Che：物単位 LRU のミス率
    ceiling_sim=1 - obj["ohr"] if obj else None,                         # 実測：素の LRU（cachesim lru、SSD と同じ容量）のミス率
    model_lru_err=(m["lru"]["ohr"] - obj["ohr"]) if (m and obj) else None,   # IRM の当てはまり（負 = 時間局所性で実測の方が当たる）
    pred_gain_ohr=s.get("gain_ohr"), pred_gain_bhr=s.get("gain_bhr"), pred_gain_j=s.get("gain_j"),
    opt_small_share_ohr=s["ohr_opt"]["share"][0] if s else None,
    opt_small_share_j=(s.get("j_opt") or {}).get("share", [None])[0] if s else None,
    lru_small_share=s["classes"][0]["lru_share"] if s else None,
    nat_small_share=(out.get("share_mean_2nd_half") or [None])[0],
    meas_gain_ohr=(st["ohr"] - none["ohr"]) if (st and none) else None,
    meas_gain_bmr=(st["bmr"] - none["bmr"]) if (st and none) else None,   # 正 = 分類で BMR が悪化
    pinned_first10=sum(v["sealed_first10"] for v in (out.get("pinned") or {}).values()) if out.get("pinned") else None,
    st_taus=(st or {}).get("taus"), st_tau_changes=out.get("st_tau_changes"), model_taus=s.get("taus"))
json.dump(out, sys.stdout)
