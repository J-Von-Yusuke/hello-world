#!/usr/bin/env python3
"""scaled.py — SSD 規模（240 GiB、Region 256/626 MiB、64 KiB/32 MiB、chunk 1 MiB）を 1/64 に縮めた負荷で、
予測（docs/synth_scaled_prediction.html）を確かめ、再挿入で BMR がどれだけ戻るかを測る（~/synth で実行）。
  python3 scaled.py > results/scaled.json
縮小：64 KiB → 1 KiB、32 MiB → 512 KiB、chunk 1 MiB → 16 KiB、R 256 MiB → 4 MiB、626 MiB → 9.78 MiB、容量 3.75 GiB。
1 Region の物の数と Region の数はそのままなので、モデルの上では同じ負荷。
"""
import csv, json, os, re, subprocess
from concurrent.futures import ThreadPoolExecutor

H = os.environ.get("SYNTH_HOME", os.path.expanduser("~/synth")); SIM = os.environ.get("CACHESIM", f"{H}/b/bin/cachesim")
NREQ = int(os.environ.get("SYNTH_SCALED_N", 200_000_000)); WARM = NREQ // 4
C = 240 * 2**30 // 64
RS = {"256M": 256 * 2**20 // 64, "626M": 626 * 2**20 // 64}
WL = {"r3to1": "1K:4194304:0.75:0.9 512K:25600:0.25:0.9", "r95to5": "1K:4194304:0.95:0.9 512K:25600:0.05:0.9"}
POL = {"none": "hint=none", "split": "hint=fixed:tau=23170",  # τ = √(1K·512K)
       "none+re": "hint=none,reinsert=hits:n=1", "split+re": "hint=fixed:tau=23170,reinsert=hits:n=1"}


def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True, cwd=H).stdout


def rf(w, r, pol):
    tag = f"o/sc_{w}_{r}_{pol}"
    sh(f'{SIM} sc_{w}.bin oracleGeneral RegionFlash {C} -e "dram-size=0,region-size={RS[r]},region-evict=lru,'
       f'seal-on-repartition=inactive,seed=1,chunk=16K,interval=1000000,warmup-req={WARM},{POL[pol]},out={tag}" >/dev/null 2>&1')
    d = json.load(open(f"{H}/{tag}.json")); t = d["total"]
    iv = [x for x in csv.DictReader(open(f"{H}/{tag}.interval.csv"))]
    keys = ("reqs", "misses", "req_bytes", "miss_bytes", "flash_region_write_bytes")
    ser = [{k: float(x[k]) for k in keys} for x in iv if x.get("kind", "interval") == "interval"]
    return dict(ohr=t["ohr"], bmr=t["bmr"], write=t["flash_region_write_bytes"], admit=t["ssd_admit_bytes"],
                reinsert=t.get("reinsert_bytes", 0), cls=[(c["ohr"], c["bmr"]) for c in d["class"]], series=ser)


def lru(w):
    m = re.search(r"miss ratio ([\d.]+), byte miss ratio ([\d.]+)",
                  sh(f"{SIM} sc_{w}.bin oracleGeneral lru {C} --warmup-sec={WARM} 2>/dev/null"))
    return dict(ohr=1 - float(m[1]), bmr=float(m[2]))


def model(w, r, h):
    out = sh(f"./synth_model -f {C / sum(int(s.split(':')[1]) * (1024 if s.startswith('1K') else 512 * 1024) for s in WL[w].split())} "
             f"-R {RS[r]} -H {h} {WL[w]}")
    return {v: tuple(map(float, re.search(re.escape(v) + r"\s+ohr=([\d.]+) bhr=([\d.]+)", out).groups()))
            for v in ("lru", "rlru[0]", "rlru[1]", "part[1]")}


for w, spec in WL.items():  # トレース（2 億要求、4.8 GB）
    if not os.path.exists(f"{H}/sc_{w}.bin"): sh(f"./synth_gen sc_{w}.bin {NREQ} 1 {spec}")
res = {}
with ThreadPoolExecutor(int(os.environ.get("SYNTH_P", "10"))) as ex:
    jobs = {(w, r, p): ex.submit(rf, w, r, p) for w in WL for r in RS for p in POL}
    jobs.update({(w, "lru"): ex.submit(lru, w) for w in WL})
    jobs.update({(w, r, f"model_H{h}"): ex.submit(model, w, r, h) for w in WL for r in RS for h in ("0", "%g" % NREQ)})
    for k, j in jobs.items(): res["|".join(k)] = j.result()
print(json.dumps(res))
