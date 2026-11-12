#!/usr/bin/env python3
"""paper_check.py — ./run.sh paper の結果が正しく測れているかを確かめる（work/paper/<trace>/<config>/P.json を読む）。

  python3 paper_check.py <WORK> [trace..] [--ext] [--robust]   --ext / --robust で paper-ext・paper-robust の結果も並べる

トレースごとに、構成ごとに次を出す（基準 = r256_base）:
  1. 設定   region_size・chunk・分類の閾値 taus・分類数 k が意図どおりか
  2. 全量   total.reqs がトレースの要求数（info.json の n、埋め値を除いた変種）と一致するか
  3. 恒等式 書き込み = 受け入れ + 再挿入 + 詰め物 が成り立つか（残差）
  4. 内訳   基準からの書き込みの差 [TB] を、受け入れ・再挿入・詰め物に分ける
            受け入れの差は、ほぼ BMR の差 × 要求バイトになるはず（全受け入れなら受け入れ ≒ ミスしたバイト）
  5. 自動化 n=auto が選んだ閾値（分類ごと）、下限の山登りの ff（終わり・時間平均）
標準ライブラリだけ。"""
import json, os, sys

WORK = sys.argv[1]; want = [a for a in sys.argv[2:] if not a.startswith("--")]
D = f"{WORK}/paper"; BASE = "r256_base"
DIRS = [D] + [f"{D}/{x}" for x in ("ext", "robust") if f"--{x}" in sys.argv]   # --ext / --robust: paper-ext / paper-robust の結果も並べる
TB = 1e12


def where(n, c):  # その構成の出力フォルダ（後の場所を優先）
    for dd in reversed(DIRS):
        if os.path.exists(f"{dd}/{n}/{c}/P.json"): return f"{dd}/{n}/{c}"
    return None


def load(n, c):
    w = where(n, c)
    if not w: return None
    d = json.load(open(f"{w}/P.json")); ra = f"{w}/P.reauto.json"
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
    cfgs = sorted({c for dd in DIRS if os.path.isdir(f"{dd}/{n}") for c in os.listdir(f"{dd}/{n}") if os.path.exists(f"{dd}/{n}/{c}/P.json")})
    b = load(n, BASE)
    nr, var = n_reqs(n)
    print(f"\n=== {n}（変種 {var}、要求数 {nr:,}）" if nr else f"\n=== {n}")
    print(f"{'config':16s} {'R MiB':>7s} {'chunk':>6s} {'k':>2s} {'taus':>12s} {'reqs ok':>7s} {'恒等式残差':>9s} "
          f"{'BMR%':>7s} {'OHR%':>6s} {'書込TB':>7s} | {'Δ書込':>6s} {'=Δ受入':>7s} {'+Δ再挿入':>8s} {'+Δ詰物':>7s} | {'ΔBMR×要求B':>10s} | "
          f"{'再挿入/追出R':>10s} {'片の損失':>8s} {'孤立片TB':>8s} {'大き過ぎTB':>9s} | n=auto  ff(end/mean)")
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
        # 再挿入の上限（追い出す Region あたり R×max_frac）にどれだけ当たっているか、片の損失（chunk>0 のとき）、大き過ぎて入れなかった量
        ev = t.get("regions_evicted", 0); cap = Rn / (ev * cf["region_size"]) if ev else 0
        loss = t.get("chunk_loss_head", 0) + t.get("chunk_loss_other", 0)
        line += f"{cap:10.3f} {loss:8d} {t.get('chunk_orphan_bytes', 0) / TB:8.2f} {t.get('too_large_bytes', 0) / TB:9.2f} | "
        rp = d.get("region_policy")
        ff = f"{rp['ff_end']:.2f}/{rp['ff_mean']:.2f}" if isinstance(rp, dict) and "ff_end" in rp else ""
        print(line + f"{n_auto(d):7s} {ff}")
    if "--robust" in sys.argv:   # 条件ごとに 提案（ru_auto）と 基準（r256_base）・CacheLib n=3 の差
        print(f"  頑健性: {'条件':22s} {'ΔBMR(提案−基準)':>15s} {'書込比(提案/基準)':>17s} {'ΔBMR(提案−n3)':>14s} {'書込比(提案/n3)':>15s}")
        for suf, lab in (("", "既定"), ("_c0", "chunk=0（CacheLib の置き方）"), ("_f1", "再挿入の上限なし"), ("_s2", "乱数の種 2"), ("_s3", "乱数の種 3")):
            P_, B_, N_ = load(n, "ru_auto" + suf), load(n, "r256_base" + suf), load(n, "r256_n3" + suf)
            if not (P_ and B_): continue
            pt, bt = P_["total"], B_["total"]
            row = f"  {'':6s} {lab:22s} {100 * (pt['bmr'] - bt['bmr']):+15.3f} {pt['flash_region_write_bytes'] / bt['flash_region_write_bytes']:17.3f}"
            if N_:
                nt = N_["total"]; row += f" {100 * (pt['bmr'] - nt['bmr']):+14.3f} {pt['flash_region_write_bytes'] / nt['flash_region_write_bytes']:15.3f}"
            print(row)
print("\n読み方: 恒等式残差 は |残差| < 1% なら書き込みの集計に矛盾なし。Δ受入 ≒ ΔBMR×要求B なら、受け入れの差は BMR の差で説明できる。"
      "BMR がほぼ同じで書き込みが大きく減っているなら、その差の大半は Δ再挿入 に出るはず。"
      "r256_base と ru_base の差は Region の大きさだけ、*_none_auto との差が分類（＋下限）の効果。")
print("再挿入/追出R = 書き直したバイト ÷（追い出した Region 数 × R）。既定の上限は 0.5 なので、これが 0.5 に近い構成は上限に当たっている（_f1 で上限を外して確かめる）。"
      "片の損失・孤立片 = chunk>0 で、片を 1 つ失って消えた物の数と、他の Region に残った無駄な片の量（CacheLib には無い損。_c0 で確かめる）。"
      "頑健性の表で、どの条件でも ΔBMR と書込比の向きが既定と同じなら、結論は RegionFlash の作りや乱数に頼っていない。")
print("確認で引っかかった数:", bad)
