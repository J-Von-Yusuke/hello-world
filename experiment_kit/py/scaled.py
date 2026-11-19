#!/usr/bin/env python3
"""scaled.py — SSD 規模（240 GiB、Region 256/626 MiB、64 KiB/32 MiB、chunk 1 MiB）を 1/64 に縮めた負荷で、
予測（docs/synth_scaled_prediction.html）を確かめ、再挿入で BMR がどれだけ戻るかを測る（~/synth で実行）。
  python3 scaled.py > results/scaled.json
縮小：64 KiB → 1 KiB、32 MiB → 512 KiB、chunk 1 MiB → 16 KiB、R 256 MiB → 4 MiB、626 MiB → 9.78 MiB、容量 3.75 GiB。
1 Region の物の数と Region の数はそのままなので、モデルの上では同じ負荷。
"""
import csv, json, os, re, shutil, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

H = os.environ.get("SYNTH_HOME", os.path.expanduser("~/synth")); SIM = os.environ.get("CACHESIM", f"{H}/b/bin/cachesim")
NREQ = int(os.environ.get("SYNTH_SCALED_N", 200_000_000)); WARM = NREQ // 4
CM = os.environ.get("CHUNK_MODE", "piece")   # 片の扱い（config.sh の CHUNK_MODE。512K の物は 16K の片 32 個）
CMS = ",chunk-mode=piece" if CM == "piece" else ""
C = 240 * 2**30 // 64
RS = {"256M": 256 * 2**20 // 64, "626M": 626 * 2**20 // 64}
WL = {"r3to1": "1K:4194304:0.75:0.9 512K:25600:0.25:0.9", "r95to5": "1K:4194304:0.95:0.9 512K:25600:0.05:0.9"}
POL = {"none": "hint=none", "split": "hint=fixed:tau=23170",  # τ = √(1K·512K)
       "none+re": "hint=none,reinsert=hits:n=1", "split+re": "hint=fixed:tau=23170,reinsert=hits:n=1"}


def sh(c): return subprocess.run(c, shell=True, capture_output=True, text=True, cwd=H).stdout


REC = 24   # synth_gen の 1 要求のバイト数（uint32 t, uint64 id, uint32 size, int64 next）


def trace(w):  # sc_<w>.bin を NREQ 要求で用意する。長さが違えば（前の SYNTH_SCALED_N の残り・書きかけ）作り直す
    p, need = f"{H}/sc_{w}.bin", REC * NREQ
    if os.path.exists(p) and os.path.getsize(p) == need: return
    if os.path.exists(p):
        print(f"scaled: {p} は {os.path.getsize(p) // REC} 要求分（今回 {NREQ}）→ 消して作り直す", file=sys.stderr); os.remove(p)
    for q in (f"{p}.tmp",):
        if os.path.exists(q): os.remove(q)
    free = shutil.disk_usage(H).free
    if free < need * 1.02:
        raise RuntimeError(f"ディスクが足りない: {p} に {need / 1e9:.1f} GB 要るが、空きは {free / 1e9:.1f} GB（{H}）。"
                           "SYNTH_SCALED_N を減らすか、work/synth の古いファイルを消す")
    r = subprocess.run(f"./synth_gen {p}.tmp {NREQ} 1 {WL[w]}", shell=True, cwd=H, capture_output=True, text=True)
    if r.returncode or not os.path.exists(f"{p}.tmp") or os.path.getsize(f"{p}.tmp") != need:
        got = os.path.getsize(f"{p}.tmp") if os.path.exists(f"{p}.tmp") else 0
        raise RuntimeError(f"synth_gen が {p} を作れなかった（終了コード {r.returncode}、{got / 1e9:.2f} / {need / 1e9:.2f} GB 書いた）。"
                           f"synth_gen は 1 要求 8 B のメモリ（{8 * NREQ / 1e9:.1f} GB）と {need / 1e9:.1f} GB のディスクを使う。\n"
                           f"--- synth_gen の出力 ---\n{(r.stderr or r.stdout)[-800:]}")
    os.replace(f"{p}.tmp", p)   # 書き終えてから置き換える（途中で止まっても書きかけを残さない）


def cachesim(tag, cmd):  # 出力を <tag>.log に残し、<tag>.json ができなければ理由を付けて止まる（前の結果は先に消す）
    for ext in (".json", ".interval.csv"):
        if os.path.exists(f"{H}/{tag}{ext}"): os.remove(f"{H}/{tag}{ext}")
    r = subprocess.run(f"{cmd} > {tag}.log 2>&1", shell=True, cwd=H)
    if not os.path.exists(f"{H}/{tag}.json"):
        tail = open(f"{H}/{tag}.log", errors="replace").read()[-800:]
        hint = "（強制終了: メモリ不足の可能性。SYNTH_P を下げる）" if r.returncode in (137, -9) else ""
        raise RuntimeError(f"cachesim が {tag}.json を書かなかった（終了コード {r.returncode}）{hint}\n{cmd}\n--- {tag}.log の末尾 ---\n{tail}")


def rf(w, r, pol):
    tag = f"o/sc_{w}_{r}_{pol}"
    cachesim(tag, f'{SIM} sc_{w}.bin oracleGeneral RegionFlash {C} -e "dram-size=0,region-size={RS[r]},region-evict=lru,'
                  f'seal-on-repartition=inactive,seed=1,chunk=16K{CMS},interval=1000000,warmup-req={WARM},{POL[pol]},out={tag}"')
    d = json.load(open(f"{H}/{tag}.json")); t = d["total"]
    iv = [x for x in csv.DictReader(open(f"{H}/{tag}.interval.csv"))]
    keys = ("reqs", "misses", "req_bytes", "miss_bytes", "flash_region_write_bytes")
    ser = [{k: float(x[k]) for k in keys} for x in iv if x.get("kind", "interval") == "interval"]
    return dict(ohr=t["ohr"], bmr=t["bmr"], write=t["flash_region_write_bytes"], admit=t["ssd_admit_bytes"],
                reinsert=t.get("reinsert_bytes", 0), cls=[(c["ohr"], c["bmr"]) for c in d["class"]], series=ser)


def lru(w):
    out = sh(f"{SIM} sc_{w}.bin oracleGeneral lru {C} --warmup-sec={WARM} 2>&1")
    m = re.search(r"miss ratio ([\d.]+), byte miss ratio ([\d.]+)", out)
    if not m: raise RuntimeError(f"cachesim lru（{w}）の結果の行が無い:\n{out[-800:]}")
    return dict(ohr=1 - float(m[1]), bmr=float(m[2]))


def model(w, r, h):
    out = sh(f"./synth_model -f {C / sum(int(s.split(':')[1]) * (1024 if s.startswith('1K') else 512 * 1024) for s in WL[w].split())} "
             f"-R {RS[r]} -H {h} {WL[w]}")
    return {v: tuple(map(float, re.search(re.escape(v) + r"\s+ohr=([\d.]+) bhr=([\d.]+)", out).groups()))
            for v in ("lru", "rlru[0]", "rlru[1]", "part[1]")}


for w in WL: trace(w)  # トレース（2 億要求、4.8 GB ずつ）
res = {"chunk_mode": CM}   # run.sh はこれが今の CHUNK_MODE と違えば回し直す
with ThreadPoolExecutor(int(os.environ.get("SYNTH_P", "10"))) as ex:
    jobs = {(w, r, p): ex.submit(rf, w, r, p) for w in WL for r in RS for p in POL}
    jobs.update({(w, "lru"): ex.submit(lru, w) for w in WL})
    jobs.update({(w, r, f"model_H{h}"): ex.submit(model, w, r, h) for w in WL for r in RS for h in ("0", "%g" % NREQ)})
    for k, j in jobs.items(): res["|".join(k)] = j.result()
print(json.dumps(res))
