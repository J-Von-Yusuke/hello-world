#!/usr/bin/env python3
"""synth_run.py — 合成トレースを作り、理論（synth_model）と実測（cachesim）を並べた JSON を書く。

  python3 synth_run.py <name> <spec...> [--f 0.05,0.1,0.2,0.3] [--g 0,0,1] [--n 20000000]
  spec = size:nobj:share:alpha（synth_gen と同じ）

実測（容量ごと）:
  lru        物単位 LRU（全体）
  part[1]    クラスごとの部分トレースに LRU、容量はモデルの OHR 最適な C_k（静的分割）
  rfifo[0/1] RegionFlash、Region FIFO、分類なし / サイズ分類
  rlru[0/1]  RegionFlash、Region LRU、分類なし / サイズ分類
  rlru[2]    --g の対応で分類（τ はグループの境目）
いずれも DRAM なし、全受け入れ、先頭 25% の要求で温める。
"""
import json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor

HOME = os.environ.get("SYNTH_HOME", os.path.expanduser("~/synth"))
SIM = os.environ.get("CACHESIM", f"{HOME}/b/bin/cachesim")  # 正本（Windows 側）から作ったビルド
U = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}


def size(s):
    return int(float(s[:-1]) * U[s[-1]]) if s[-1] in U else int(s)


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=HOME).stdout


def lcs(trace, cap, warm):  # 物単位 LRU → (要求数, OHR, BHR)
    out = sh(f"{SIM} {trace} oracleGeneral lru {int(cap)} --warmup-sec={warm} 2>/dev/null")
    m = re.search(r"(\d+) req, miss ratio ([\d.]+), byte miss ratio ([\d.]+)", out)
    return int(m[1]), 1 - float(m[2]), 1 - float(m[3])


def rf(trace, cap, R, evict, hint, warm_req, tag):
    out = f"{HOME}/o/{tag}"
    sh(f'{SIM} {trace} oracleGeneral RegionFlash {int(cap)} -e "dram-size=0,region-size={int(R)},'
       f'region-evict={evict},seal-on-repartition=inactive,seed=1,interval=1000000000,'
       f'warmup-req={warm_req},hint={hint},out={out}" > /dev/null 2>&1')
    t = json.load(open(out + ".json"))["total"]
    return t["ohr"], 1 - t["bmr"]


def model(spec, f, g):
    out = sh(f"./synth_model -f {f} {'-g ' + g if g else ''} {' '.join(spec)}")
    res, cur = {}, None
    for ln in out.splitlines():
        if ln.startswith("f="):
            cur = res.setdefault(ln.split()[0][2:], {"C": float(ln.split()[1][2:]), "R": float(ln.split()[2][2:])})
        elif ln.startswith("  "):
            w = ln.split()
            cur[w[0]] = {k: v for k, v in (x.split("=") for x in w[1:])}
    return res


def taus(sizes, groups):  # 分類の境目：隣のグループとの間の幾何平均
    order = sorted(range(len(sizes)), key=lambda k: sizes[k])
    t = [int((sizes[a] * sizes[b]) ** 0.5) for a, b in zip(order, order[1:]) if groups[a] != groups[b]]
    return "fixed:tau=" + "/".join(map(str, t)) if t else "none"


def main():
    a = sys.argv[1:]
    name, opt, spec = a[0], {}, []
    i = 1
    while i < len(a):
        if a[i].startswith("--"): opt[a[i][2:]] = a[i + 1]; i += 2
        else: spec.append(a[i]); i += 1
    n = int(opt.get("n", 20_000_000)); f = opt.get("f", "0.05,0.1,0.2,0.3"); g = opt.get("g")
    sizes = [size(s.split(":")[0]) for s in spec]
    tr = f"{name}.bin"
    os.makedirs(f"{HOME}/o", exist_ok=True)
    sh(f"./synth_gen {tr} {n} 1 {' '.join(spec)} -S")
    M = model(spec, f, g)
    warm_req, warm_sec = n // 4, n // 4
    jobs = {}
    with ThreadPoolExecutor(int(os.environ.get("SYNTH_P", "12"))) as ex:
        for fk, m in M.items():
            C, R = m["C"], m["R"]
            jobs[fk, "lru"] = ex.submit(lcs, tr, C, warm_sec)
            for k, ck in enumerate(m["part[1]"]["Cg"].split(",")):
                jobs[fk, f"part{k}"] = ex.submit(lcs, f"{tr}.c{k}", float(ck), warm_sec)
            hints = [("0", "none"), ("1", taus(sizes, list(range(len(sizes)))))]
            if g: hints.append(("2", taus(sizes, [int(x) for x in g.split(",")])))
            for v, h in hints:
                for ev, nm in (("lru", "rlru"), ("fifo", "rfifo")):
                    if nm == "rfifo" and v == "2": continue
                    jobs[fk, f"{nm}[{v}]"] = ex.submit(rf, tr, C, R, ev, h, warm_req, f"{name}_{fk}_{nm}{v}")
    S = {}
    for (fk, nm), j in jobs.items():
        S.setdefault(fk, {})[nm] = j.result()
    for fk, s in S.items():  # 静的分割：部分トレースの結果を要求数・バイトで足し合わせる
        parts = [s.pop(k) for k in sorted(x for x in list(s) if x.startswith("part"))]
        req = sum(p[0] for p in parts); byt = sum(p[0] * sz for p, sz in zip(parts, sizes))
        s["part[1]"] = (sum(p[0] * p[1] for p in parts) / req,
                        sum(p[0] * sz * p[2] for p, sz in zip(parts, sizes)) / byt)
        s["lru"] = s["lru"][1:]
    json.dump({"name": name, "spec": spec, "group": g, "model": M, "sim": S},
              open(f"{HOME}/{name}.json", "w"), indent=1)
    for fk in M:
        print(f"f={fk}")
        for nm in ("lru", "part[1]", "rfifo[0]", "rfifo[1]", "rlru[0]", "rlru[1]", "rlru[2]"):
            mk = "rfifo" if nm.startswith("rfifo") else nm
            if nm in S[fk] and mk in M[fk]:
                mo, mb = float(M[fk][mk]["ohr"]), float(M[fk][mk]["bhr"])
                so, sb = S[fk][nm]
                print(f"  {nm:9s} OHR model {mo:.4f} sim {so:.4f} ({so - mo:+.4f})   BHR model {mb:.4f} sim {sb:.4f} ({sb - mb:+.4f})")


main()
