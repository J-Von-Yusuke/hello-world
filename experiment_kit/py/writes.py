#!/usr/bin/env python3
"""writes.py — flash への書き込みの内訳（受け入れ＝ミスしたバイト、再挿入）を、分類なし／サイズ分類＋大物の容量の下限、
再挿入の有無で比べる（~/synth で、scaled.py のトレースを使う）。分類なし＋再挿入が定常に入るよう 2 億要求、温め 5,000 万。
書き込み = 受け入れ（BMR × 要求バイト）＋ 再挿入 なので、同じ BMR なら差は再挿入の量。
  python3 writes.py > results/writes.json
"""
import json, os, shutil, subprocess
from concurrent.futures import ThreadPoolExecutor

H = os.environ.get("SYNTH_HOME", os.path.expanduser("~/synth")); SIM = os.environ.get("CACHESIM", f"{H}/b/bin/cachesim")
NREQ = int(os.environ.get("SYNTH_SCALED_N", 200_000_000))
C = 240 * 2**30 // 64; RS = {"256M": 4 * 2**20, "626M": 626 * 2**20 // 64}
CFG = [("none", 0)] + [("split", ff) for ff in (0, 0.75, 0.85, 0.9, 0.95)]
WL = {"r3to1": "1K:4194304:0.75:0.9 512K:25600:0.25:0.9", "r95to5": "1K:4194304:0.95:0.9 512K:25600:0.05:0.9"}  # scaled.py と同じ
REC = 24   # synth_gen の 1 要求のバイト数


def trace(w):  # scaled.py と同じ：sc_<w>.bin が NREQ 要求でなければ消して作り直す（前の SYNTH_SCALED_N の残り・書きかけ）
    p, need = f"{H}/sc_{w}.bin", REC * NREQ
    if os.path.exists(p) and os.path.getsize(p) == need: return
    for q in (p, f"{p}.tmp"):
        if os.path.exists(q): os.remove(q)
    free = shutil.disk_usage(H).free
    if free < need * 1.02:
        raise RuntimeError(f"ディスクが足りない: {p} に {need / 1e9:.1f} GB 要るが、空きは {free / 1e9:.1f} GB（{H}）")
    r = subprocess.run(f"./synth_gen {p}.tmp {NREQ} 1 {WL[w]}", shell=True, cwd=H, capture_output=True, text=True)
    if r.returncode or not os.path.exists(f"{p}.tmp") or os.path.getsize(f"{p}.tmp") != need:
        raise RuntimeError(f"synth_gen が {p} を作れなかった（終了コード {r.returncode}）。"
                           f"メモリ {8 * NREQ / 1e9:.1f} GB とディスク {need / 1e9:.1f} GB を使う。\n{(r.stderr or r.stdout)[-800:]}")
    os.replace(f"{p}.tmp", p)


def run(w, r, kind, ff, re):
    tag = f"o/wr_{w}_{r}_{kind}_{ff}_{re}"
    hint = "hint=none" if kind == "none" else f"hint=fixed:tau=23170,region-params=fc=1/ff={ff}"
    e = (f"dram-size=0,region-size={RS[r]},region-evict=lru,seal-on-repartition=inactive,seed=1,chunk=16K,"
         f"interval=1000000000,warmup-req={NREQ // 4},{hint}" + (",reinsert=hits:n=1" if re else "") + f",out={tag}")
    if os.path.exists(f"{H}/{tag}.json"): os.remove(f"{H}/{tag}.json")   # 前の結果を読まない
    cmd = f'{SIM} sc_{w}.bin oracleGeneral RegionFlash {C} -e "{e}"'
    p = subprocess.run(f"{cmd} > {tag}.log 2>&1", shell=True, cwd=H)       # 出力は <tag>.log に残す
    if not os.path.exists(f"{H}/{tag}.json"):
        tail = open(f"{H}/{tag}.log", errors="replace").read()[-800:]
        hint = "（強制終了: メモリ不足の可能性。SYNTH_P を下げる）" if p.returncode in (137, -9) else ""
        raise RuntimeError(f"cachesim が {tag}.json を書かなかった（終了コード {p.returncode}）{hint}\n{cmd}\n--- {tag}.log の末尾 ---\n{tail}")
    d = json.load(open(f"{H}/{tag}.json")); t = d["total"]
    return dict(w=w, r=r, kind=kind, ff=ff, re=re, ohr=t["ohr"], bmr=t["bmr"], reqs=t["reqs"], req_bytes=t["req_bytes"],
                write=t["flash_region_write_bytes"], admit=t["ssd_admit_bytes"], reinsert=t["reinsert_bytes"],
                pad=t["pad_bytes"], cls=[dict(admit=c["ssd_admit_bytes"], reinsert=c["reinsert_bytes"]) for c in d["class"]])


for w in WL: trace(w)
jobs = [(w, r, k, ff, re) for w in WL for r in RS for k, ff in CFG for re in (0, 1)]
with ThreadPoolExecutor(int(os.environ.get("SYNTH_P", "12"))) as ex:
    print(json.dumps(list(ex.map(lambda a: run(*a), jobs))))
