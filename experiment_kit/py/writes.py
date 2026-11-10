#!/usr/bin/env python3
"""writes.py — flash への書き込みの内訳（受け入れ＝ミスしたバイト、再挿入）を、分類なし／サイズ分類＋大物の容量の下限、
再挿入の有無で比べる（~/synth で、scaled.py のトレースを使う）。分類なし＋再挿入が定常に入るよう 2 億要求、温め 5,000 万。
書き込み = 受け入れ（BMR × 要求バイト）＋ 再挿入 なので、同じ BMR なら差は再挿入の量。
  python3 writes.py > results/writes.json
"""
import json, os, subprocess
from concurrent.futures import ThreadPoolExecutor

H = os.environ.get("SYNTH_HOME", os.path.expanduser("~/synth")); SIM = os.environ.get("CACHESIM", f"{H}/b/bin/cachesim")
NREQ = int(os.environ.get("SYNTH_SCALED_N", 200_000_000))
C = 240 * 2**30 // 64; RS = {"256M": 4 * 2**20, "626M": 626 * 2**20 // 64}
CFG = [("none", 0)] + [("split", ff) for ff in (0, 0.75, 0.85, 0.9, 0.95)]


def run(w, r, kind, ff, re):
    tag = f"o/wr_{w}_{r}_{kind}_{ff}_{re}"
    hint = "hint=none" if kind == "none" else f"hint=fixed:tau=23170,region-params=fc=1/ff={ff}"
    e = (f"dram-size=0,region-size={RS[r]},region-evict=lru,seal-on-repartition=inactive,seed=1,chunk=16K,"
         f"interval=1000000000,warmup-req={NREQ // 4},{hint}" + (",reinsert=hits:n=1" if re else "") + f",out={tag}")
    subprocess.run(f'{SIM} sc_{w}.bin oracleGeneral RegionFlash {C} -e "{e}" >/dev/null 2>&1', shell=True, cwd=H)
    d = json.load(open(f"{H}/{tag}.json")); t = d["total"]
    return dict(w=w, r=r, kind=kind, ff=ff, re=re, ohr=t["ohr"], bmr=t["bmr"], reqs=t["reqs"], req_bytes=t["req_bytes"],
                write=t["flash_region_write_bytes"], admit=t["ssd_admit_bytes"], reinsert=t["reinsert_bytes"],
                pad=t["pad_bytes"], cls=[dict(admit=c["ssd_admit_bytes"], reinsert=c["reinsert_bytes"]) for c in d["class"]])


jobs = [(w, r, k, ff, re) for w in ("r3to1", "r95to5") for r in RS for k, ff in CFG for re in (0, 1)]
with ThreadPoolExecutor(int(os.environ.get("SYNTH_P", "12"))) as ex:
    print(json.dumps(list(ex.map(lambda a: run(*a), jobs))))
