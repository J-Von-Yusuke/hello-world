#!/usr/bin/env python3
"""tune_one.py — tune の 1 トレース分（work/tune/<name>/<id>/P.json）を 1 つの JSON にまとめる（標準出力）。
  python3 tune_one.py work/tune/<name>
各構成: OHR・BMR、要求バイトあたりの SSD 書き込み（受け入れ・再挿入・詰め物の内訳）、分類の数と閾値、
mv の容量の取り分、n=auto が学んだ「再挿入した物が次の滞在でまた当たった割合」と使われた n。"""
import json, os, sys

O = sys.argv[1]; runs = {}
for id in sorted(os.listdir(O)):
    f = f"{O}/{id}/P.json"
    if not os.path.exists(f): continue
    d = json.load(open(f)); t = d["total"]; B = t["req_bytes"] or 1; cfg = d["config"]
    r = dict(ohr=t["ohr"], bmr=t["bmr"], reqs=t["reqs"], req_bytes=t["req_bytes"],
             write=t["flash_region_write_bytes"] / B, admit=t["ssd_admit_bytes"] / B, reinsert=t["reinsert_bytes"] / B,
             pad=t["pad_bytes"] / B, regions_sealed=t["regions_sealed"], repartition_seals=t["repartition_seals"],
             k=cfg.get("k"), taus=cfg.get("taus"),
             cls=[dict(ohr_hits=c["ssd_hits"], admit=c["ssd_admit_bytes"] / B, reinsert=c["reinsert_bytes"] / B,
                       sealed=c["regions_sealed"]) for c in d.get("class", [])])
    rp = d.get("region_policy")
    if isinstance(rp, dict) and "share" in rp: r["share"] = rp["share"]
    fa = f"{O}/{id}/P.reauto.json"
    if os.path.exists(fa):
        a = json.load(open(fa)); r["reauto"] = []
        for c in a["class"]:
            p = [(h + 1) / (n + 2) for h, n in zip(c["hit"], c["try"])]           # 当たった割合（Laplace）、h = 1..8
            nu = c["n_used"]; tot = sum(nu) or 1
            r["reauto"].append(dict(p_hit=p, tries=c["try"], n_mode=1 + max(range(9), key=lambda i: nu[i]),
                                    n_mean=sum((i + 1) * v for i, v in enumerate(nu)) / tot))
        r["target"] = a["target"]
    runs[id] = r
cfg = json.load(open(f"{O}/config.json")) if os.path.exists(f"{O}/config.json") else {}
print(json.dumps(dict(name=os.path.basename(O.rstrip("/")), config=cfg, runs=runs)))
