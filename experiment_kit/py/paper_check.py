#!/usr/bin/env python3
"""paper_check.py — ./run.sh paper の結果が正しく測れているかを確かめる（work/paper/<trace>/<config>/P.json を読む）。

  python3 paper_check.py <WORK> [trace..]

トレースごとに、構成ごとに次を出す（基準 = r256_base）:
  1. 設定   region_size・chunk・分類の閾値 taus・分類数 k が意図どおりか
  2. 全量   total.reqs がトレースの要求数（info.json の n、埋め値を除いた変種）と一致するか
  3. 恒等式 書き込み = 受け入れ + 再挿入 + 詰め物 が成り立つか（残差）
  4. 内訳   基準からの書き込みの差 [TB] を、受け入れ・再挿入・詰め物に分ける
            受け入れの差は、ほぼ BMR の差 × 要求バイトになるはず（全受け入れなら受け入れ ≒ ミスしたバイト）
  5. 自動化 n=auto が選んだ閾値（分類ごと）、下限の山登りの ff（終わり・時間平均）
標準ライブラリだけ。"""
import json, os, sys

WORK = sys.argv[1]; want = sys.argv[2:]
D = f"{WORK}/paper"; BASE = "r256_base"
TB = 1e12


def load(n, c):
    p = f"{D}/{n}/{c}/P.json"
    if not os.path.exists(p): return None
    d = json.load(open(p)); ra = f"{D}/{n}/{c}/P.reauto.json"
    d["_reauto"] = json.load(open(ra)) if os.path.exists(ra) else None
    return d


def n_reqs(n):  # paper で使った変種の info.json の要求数（drop / delete なら埋め値を除いた数）
    s = f"{D}/{n}.json"
    var = json.load(open(s)).get("variant") if os.path.exists(s) else None
    p = f"{WORK}/out/{n}/{var}/info.json"
    return (json.load(open(p))["n"], var) if var and os.path.exists(p) else (None, var)


def n_auto(d):
    a = d.get("_reauto")
    if not a: return ""
    out = []
    for c in a["class"]:
        nu = c["n_used"]; tot = sum(nu) or 1
        out.append(str(1 + max(range(len(nu)), key=lambda i: nu[i])) + f"({sum((i + 1) * v for i, v in enumerate(nu)) / tot:.1f})")
    return "/".join(out)


traces = sorted(x for x in os.listdir(D) if os.path.isdir(f"{D}/{x}") and (not want or x in want)) if os.path.isdir(D) else []
if not traces: sys.exit(f"{D} に結果が無い")
bad = 0
for n in traces:
    cfgs = sorted(c for c in os.listdir(f"{D}/{n}") if os.path.exists(f"{D}/{n}/{c}/P.json"))
    b = load(n, BASE)
    nr, var = n_reqs(n)
    print(f"\n=== {n}（変種 {var}、要求数 {nr:,}）" if nr else f"\n=== {n}")
    print(f"{'config':16s} {'R MiB':>7s} {'chunk':>6s} {'k':>2s} {'taus':>12s} {'reqs ok':>7s} {'恒等式残差':>9s} "
          f"{'BMR%':>7s} {'OHR%':>6s} {'書込TB':>7s} | {'Δ書込':>6s} {'=Δ受入':>7s} {'+Δ再挿入':>8s} {'+Δ詰物':>7s} | {'ΔBMR×要求B':>10s} | n=auto  ff(end/mean)")
    for c in cfgs:
        d = load(n, c); t = d["total"]; cf = d["config"]
        W, A, Rn, P = (t["flash_region_write_bytes"], t["ssd_admit_bytes"], t["reinsert_bytes"], t["pad_bytes"])
        resid = (W - A - Rn - P) / W if W else 0
        ok = "–" if nr is None else ("ok" if t["reqs"] == nr else f"NG {t['reqs']:,}")
        if nr is not None and t["reqs"] != nr: bad += 1
        if abs(resid) > 0.01: bad += 1
        line = (f"{c:16s} {cf['region_size'] / 2**20:7.1f} {cf['chunk'] // 1024:5d}K {cf.get('k', ''):>2} {str(cf.get('taus', []))[:12]:>12s} {ok:>7s} "
                f"{100 * resid:8.2f}% {100 * t['bmr']:7.3f} {100 * t['ohr']:6.2f} {W / TB:7.2f} | ")
        if b and c != BASE:
            bt = b["total"]
            dW, dA, dR, dP = (W - bt["flash_region_write_bytes"]) / TB, (A - bt["ssd_admit_bytes"]) / TB, (Rn - bt["reinsert_bytes"]) / TB, (P - bt["pad_bytes"]) / TB
            dB = (t["bmr"] - bt["bmr"]) * t["req_bytes"] / TB
            line += f"{dW:+6.2f} {dA:+7.2f} {dR:+8.2f} {dP:+7.2f} | {dB:+10.2f} | "
        else:
            line += f"{'':6s} {'':7s} {'':8s} {'':7s} | {'':10s} | "
        rp = d.get("region_policy")
        ff = f"{rp['ff_end']:.2f}/{rp['ff_mean']:.2f}" if isinstance(rp, dict) and "ff_end" in rp else ""
        print(line + f"{n_auto(d):7s} {ff}")
print("\n読み方: 恒等式残差 は |残差| < 1% なら書き込みの集計に矛盾なし。Δ受入 ≒ ΔBMR×要求B なら、受け入れの差は BMR の差で説明できる。"
      "BMR がほぼ同じで書き込みが大きく減っているなら、その差の大半は Δ再挿入 に出るはず。"
      "r256_base と ru_base の差は Region の大きさだけ、*_none_auto との差が分類（＋下限）の効果。")
print("確認で引っかかった数:", bad)
