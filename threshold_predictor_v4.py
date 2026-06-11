#!/usr/bin/env python3
"""
threshold_predictor_v4.py  —  RegionSplit 最適閾値予測器 v4 (β ベース)
====================================================================
threshold_rethink_report.md の H1/H2/H3 を実装した v3.2 の後継。

【中核指標 β】 再利用リクエスト質量
    β_b = 1 − n_b/N_b = 1 − 1/(r_b + (1−r_b)·rho_b)
  そのビンへのリクエストのうち再参照 (理論上ヒットになり得るアクセス) の割合。
  バイト換算でも同値。r (オブジェクト個数ベース) と異なり rho を畳み込むため、
  「r が高くても rho が大きく実は宝庫」(例: cldphyBlk bin18) を誤判定しない。

【判定経路】優先順位: A' → B → D → C
  Path A': β クラッシュ — β≥0.85 の宝庫ゾーン確認後、β が (ピーク−0.25) 以下に
                          崩落するビンの下端を T とする (v3 Path A の置換)。
                          崩落が複数ある場合は全候補を列挙し、
                            --objective mr  (既定): 最初の崩落 (リクエスト核の直上)
                            --objective bmr        : 崩落帯のデッドバイト質量
                                                     Σ(1−β)·byte_share が最大の崩落
                          を採用する。大サイズ側のジャンプほど GC Region に与える
                          バイト影響が大きいため、BMR では通常大サイズ側が選ばれる
                          (metaKV: MR→16KiB / BMR(10%)→256KiB の実測と整合)。
                          バルク占有ビン (msrBlk bin16: 91%) が宝庫ゾーンの直後に
                          ある場合も崩落が先に検出されるため B より優先する
  Path B : バルク隔離   — 単一ビンがバイトの >80% を占有 かつ そのビンの β<0.60 の
                          ときのみ発動。β が高いバルクは再利用されるため隔離が
                          逆効果になり得る (実測 2026-06-11: β≈0.8 のバルクビンを持つ
                          トレースでは閾値なしが最良)。その場合は分割なしを推奨し、
                          スコープ外バルク (mean_size>S_max) なら --max-bin で
                          マスクした再予測を案内する
  Path D : OHW V字谷    — r が前後比 ≥0.05 の谷 かつ N_b が前後平均 1.5x 以上
                          (metaCDN bin19 型。β では山に見えるため r 信号を維持)
  Path C : CDF 交差点   — F_req + F_byte ≥ 1.0 の最初のビン (β 均一時のフォールバック。
                          p50 区間則はこの経路の系)

【v4 の追加出力】
  - BMR 符号診断 (H2): 大プールに β<0.4 のバイト質量がコントラスト付きで存在するか
  - equal-β banding (H3): T より上の β 変化点 (|Δβ|≥0.15) を追加閾値候補として提示
  - admission 診断 (§10): プール別 OHW 挿入バイト率とスクリーニング税
  - 全体 β が低い場合の中立警告 (twiKV 型: 分割よりも admission を推奨)

使用例:
  python threshold_predictor_v4.py files/metaKV.measurement.json
  python threshold_predictor_v4.py files/*.measurement.json --format csv
  python threshold_predictor_v4.py files/metaCDN.measurement.json --max-bin 27 --format json
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

# ─── 較正定数 (9トレースで検証済み; 感度分析は E6 系実験で) ──────────
BETA_HIGH      = 0.85   # A': 宝庫ゾーン認定の β 下限
BETA_DROP      = 0.25   # A': ピーク β からの崩落幅
BETA_BULK_MAX  = 0.60   # B : バルク隔離を許す β の上限 (2026-06-11 実測知見)
BETA_JUNK      = 0.40   # H2: 「ゴミ」とみなす β 上限
BETA_STEP      = 0.12   # H3: 追加閾値候補とみなす隣接ビン β 変化幅
BULK_THRESHOLD = 0.80   # B : 単一ビンのバイト占有率
VDIP_R_DROP    = 0.05   # D : r 谷の最小深さ
VDIP_N_RATIO   = 1.5    # D : N_b の前後平均比
CDF_CROSSOVER  = 1.0    # C : F_req + F_byte の交差値
SIG_REQ_MIN    = 0.005  # 有意ビン: 最低リクエスト割合
SIG_BYTE_MIN   = 0.005  # 有意ビン: 最低バイト割合
BETA_TOTAL_LOW = 0.40   # 全体 β がこれ未満なら「OHW 支配」警告 (twiKV=0.37, cldphyBlk=0.43)
JUNK_BYTE_MIN  = 0.15   # H2: BMR 改善に必要な大プール内ゴミバイト質量
SMALL_CACHE    = 0.15   # これ以下のキャッシュ比率で閾値を 1 段下げる

CSV_FIELDS = [
    "workload", "cache_ratio", "max_bin",
    "threshold_bytes", "threshold_human", "path",
    "signal_bin_idx", "signal_bin_beta", "signal_bin_r",
    "bmr_improvable", "extra_thresholds", "beta_total",
    "large_ohw_byte_pct", "screening_tax_pct",
    "explanation",
]

PATH_LABELS = {
    "bulk":           "Path B : バルク隔離",
    "bulk_high_beta": "Path B : バルク高β → 分割非推奨",
    "beta_crash":     "Path A': βクラッシュ",
    "vdip":       "Path D : OHW V字谷",
    "cdf_cross":  "Path C : CDF交差点",
    "neutral":    "Neutral: 効果限定的",
}


def human_bytes(n):
    if n is None:
        return "N/A"
    for unit, t in [("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)]:
        if n >= t:
            v = n / t
            frac = int(round((v - int(v)) * 10))
            if frac == 10:
                v += 1; frac = 0
            return f"{int(v)}{'.' + str(frac) if frac else ''}{unit}"
    return f"{int(n)}B"


def beta_from_r_rho(r, rho):
    """β = 1 − 1/(r + (1−r)·rho)。n_b/N_b が直接得られない入力形式用。"""
    d = r + (1.0 - r) * max(rho, 1.0)
    return 1.0 - 1.0 / d if d > 0 else 0.0


# ─── 入力 ────────────────────────────────────────────────────────
def load_bins(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "_measurement" in data:
        raw = data["_measurement"].get("bins_detail", [])
    elif isinstance(data, dict) and "bins" in data:
        raw = data["bins"]
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError(f"未対応のJSONフォーマット: {list(data)[:5]}")

    bins = []
    for i, b in enumerate(raw):
        idx = b.get("bin", b.get("bin_idx", i))
        N   = b.get("N_b", b.get("req_count", 0)) or 0
        n   = b.get("n_b", 0) or 0
        m   = b.get("m_b", 0) or 0
        np_ = b.get("np_b", max(n - m, 0)) or 0
        r   = b.get("r", b.get("ohw_rate", 0.0)) or 0.0
        rho = b.get("rho", 1.0) or 1.0
        ms  = b.get("mean_size", 0) or 0
        if N <= 0:
            continue
        if ms <= 0:
            ms = 1.5 * (1 << idx)
        # β: n_b があれば直接、なければ r/rho から導出
        beta = (1.0 - n / N) if (n > 0) else beta_from_r_rho(r, rho)
        bins.append(dict(
            idx=idx, lo=1 << idx, label=human_bytes(1 << idx),
            N=N, n=n, m=m, np=np_, r=r, rho=rho, ms=ms,
            bytes=N * ms, beta=beta,
        ))
    bins.sort(key=lambda x: x["idx"])
    return bins


# ─── 補助診断 ────────────────────────────────────────────────────
def diagnose_bmr(bins, t_bin):
    """H2: 単一閾値で BMR 改善が見込めるか (大プールの低βバイト質量 + コントラスト)。"""
    total_bytes = sum(b["bytes"] for b in bins) or 1
    large = [b for b in bins if b["idx"] >= t_bin]
    small = [b for b in bins if b["idx"] < t_bin]
    junk = sum(b["bytes"] for b in large if b["beta"] < BETA_JUNK) / total_bytes
    small_max_beta = max((b["beta"] for b in small), default=0.0)
    # 宝庫といえる β≥0.75 帯が小プール側にあること (コントラスト条件)。
    # metaBlk (small max β=0.71, 実測Δ≈0) を弾き cldphyBlk (0.85, 実測+9〜13%) を通す。
    improvable = junk >= JUNK_BYTE_MIN and small_max_beta >= 0.75
    return dict(
        junk_byte_share=junk,
        small_max_beta=small_max_beta,
        improvable=improvable,
        note=("大プールに β<0.4 のバイト質量が {:.0f}% あり、BMR 改善余地あり".format(junk * 100)
              if improvable else
              "大プールの低βバイト質量が {:.0f}% (<{:.0f}%) のため、単一閾値での BMR 改善は"
              "見込み薄 (LRU同等以下)。equal-β 多閾値か大プール admission を検討".format(
                  junk * 100, JUNK_BYTE_MIN * 100)),
    )


def suggest_extra_thresholds(sig, t_bin):
    """H3: T 以上の有意ビン列で β 変化点 (|Δβ|≥0.15) を追加閾値候補として返す。"""
    above = [b for b in sig if b["idx"] >= t_bin]
    out = []
    for prev, cur in zip(above, above[1:]):
        if abs(cur["beta"] - prev["beta"]) >= BETA_STEP:
            out.append(cur["lo"])
    return out


def diagnose_admission(bins, t_bin):
    """§10: プール別 OHW 挿入バイト率とスクリーニング税。n_b の無い入力では概算。"""
    def pool(bs):
        ub = sum(b["n"] * b["ms"] for b in bs)
        ob = sum(b["m"] * b["ms"] for b in bs)
        np_sum = sum(b["np"] for b in bs)
        max_hits = sum(b["np"] * max(b["rho"] - 1.0, 0.0) for b in bs)
        return dict(
            ohw_insert_byte_pct=(ob / ub * 100) if ub > 0 else None,
            screening_tax_pct=(np_sum / max_hits * 100) if max_hits > 0 else None,
        )
    return dict(small=pool([b for b in bins if b["idx"] < t_bin]),
                large=pool([b for b in bins if b["idx"] >= t_bin]))


# ─── 中核: 4 経路 (A' → B → D → C) ───────────────────────────────
def predict_threshold(bins, cache_ratio=0.30, objective="mr"):
    if not bins:
        return dict(threshold_bytes=None, path="neutral",
                    explanation="入力データが空です", signal_bin=None,
                    advice="データを確認してください", warnings=[])

    total_req = sum(b["N"] for b in bins) or 1
    total_bytes = sum(b["bytes"] for b in bins) or 1
    for b in bins:
        b["rs"] = b["N"] / total_req
        b["bs"] = b["bytes"] / total_bytes
    sig = [b for b in bins if b["rs"] >= SIG_REQ_MIN or b["bs"] >= SIG_BYTE_MIN] or bins

    warnings = []
    beta_total = 1.0 - (sum(b["n"] for b in bins) / total_req) if all(b["n"] for b in sig) \
        else sum(b["beta"] * b["N"] for b in bins) / total_req
    if beta_total < BETA_TOTAL_LOW:
        warnings.append(
            f"全体 β={beta_total:.2f} と低く OHW 支配型 (twiKV 型)。サイズ分割の利得は小さく、"
            f"admission (S3-FIFO 型 probation) の方が効果的な見込み。")

    def small_cache_adjust(T):
        return T // 2 if cache_ratio <= SMALL_CACHE else T

    # Path A': β クラッシュ (msrBlk のようにバルク占有ビン自体が崩落ビンの近くに
    # ある場合があるため B より先に判定する。metaCDN bin29 は β=0.90 と高く
    # 崩落しないので、スコープ外バルクは正しく B に流れる)
    # 崩落は複数あり得るため全候補を列挙し、目的関数で選択する (2026-06-11):
    #   mr : 最初の崩落 (リクエスト核の直上で宝庫を最大限保護)
    #   bmr: 崩落帯デッドバイト質量 Σ(1−β)·byte_share 最大の崩落
    #        (大サイズ帯ほど GC Region へのバイト影響が大きい)
    peak = None
    crashes = []          # [{bin, dead_mass}]
    in_crash = False
    for b in sig:
        if b["beta"] >= BETA_HIGH:
            peak = max(peak or 0.0, b["beta"])
        if peak is not None and b["beta"] <= peak - BETA_DROP:
            if not in_crash:
                crashes.append({"bin": b, "dead_mass": 0.0})
                in_crash = True
            crashes[-1]["dead_mass"] += (1.0 - b["beta"]) * b["bs"]
        else:
            in_crash = False
    if crashes:
        chosen = crashes[0] if objective == "mr" else max(crashes, key=lambda c: c["dead_mass"])
        b = chosen["bin"]
        T = small_cache_adjust(b["lo"])
        cand_str = ", ".join(f"bin{c['bin']['idx']}({c['bin']['label']}, "
                             f"dead={c['dead_mass']*100:.1f}%)" for c in crashes)
        expl = (f"βクラッシュ: ビン{b['idx']} ({b['label']}) で β={b['beta']:.2f} に崩落 "
                f"(宝庫ゾーンのピーク β={peak:.2f}, 候補 {len(crashes)} 個: {cand_str}) "
                f"→ 閾値を {human_bytes(T)} に設定 [objective={objective}]")
        adv = (f"βクラッシュ型 (旧 Path A の β 版): 崩落ビン以上を大プールへ隔離します。"
               f"r ベース判定と異なり高 rho の宝庫ビンを誤って隔離しません "
               f"(cldphyBlk は細粒度再計測で T=512KiB が実測最良と完全一致)。")
        if len(crashes) > 1:
            adv += (" 崩落候補が複数あります: MR は最初の崩落、BMR はデッドバイト最大の崩落が"
                    "原則ですが、質量が近い場合は両候補の実測比較を推奨します。"
                    "複数崩落は equal-β banding (追加閾値候補) の適用サインでもあります。")
        res = _finish(T, "beta_crash", expl, b, adv, bins, sig, warnings, cache_ratio)
        res["crash_candidates"] = [
            {"bin_idx": c["bin"]["idx"], "threshold_bytes": c["bin"]["lo"],
             "threshold_human": c["bin"]["label"], "beta": round(c["bin"]["beta"], 3),
             "dead_byte_mass_pct": round(c["dead_mass"] * 100, 2)} for c in crashes]
        return res

    # Path B: バルク隔離 (β ゲート付き, 2026-06-11)
    max_b = max(bins, key=lambda x: x["bs"])
    if max_b["bs"] > BULK_THRESHOLD:
        if max_b["beta"] < BETA_BULK_MAX:
            T = max_b["lo"]
            expl = (f"バルク隔離: ビン{max_b['idx']} ({max_b['label']}〜) がバイトの "
                    f"{max_b['bs']*100:.0f}% を占有し β={max_b['beta']:.2f} と低い "
                    f"→ このビンを大プールへ完全隔離")
            adv = (f"バルク型: 占有ビンが実装スコープ外 (S_max 超) の場合は T=S_max/2 へ"
                   f"フォールバックし、--max-bin で除外した再予測も実行してください "
                   f"(metaCDN bin28/29 の事例)。")
            return _finish(T, "bulk", expl, max_b, adv, bins, sig, warnings, cache_ratio)
        # β が高いバルクは隔離しない (実測: β≈0.8 のバルクでは閾値なしが最良)
        expl = (f"バルクビン{max_b['idx']} ({max_b['label']}〜) がバイトの "
                f"{max_b['bs']*100:.0f}% を占有するが β={max_b['beta']:.2f} ≥ {BETA_BULK_MAX} "
                f"と高く、リクエストバイトの大半が再利用される → 隔離は LRU の動的配分に劣るため非発動")
        adv = ("分割なし (LRU) を推奨。固定容量配分は高βバルクの再利用を阻害します。"
               "バルクビンが実装スコープ外 (mean_size > S_max) の場合のみ、--max-bin で"
               "マスクした再予測を実行してください (残りの分布に別の構造があり得ます)。")
        res = _finish(None, "bulk_high_beta", expl, max_b, adv, bins, sig, warnings, cache_ratio)
        res["threshold_bytes"] = None
        res["threshold_human"] = "なし (分割非推奨)"
        return res

    # Path D: OHW V字谷 (r 信号 + N_b 集中。β では山に見える metaCDN bin19 型)
    for i in range(1, len(sig) - 1):
        bp, bc, bn = sig[i - 1], sig[i], sig[i + 1]
        dl, dr = bp["r"] - bc["r"], bn["r"] - bc["r"]
        if dl >= VDIP_R_DROP and dr >= VDIP_R_DROP:
            n_ratio = bc["N"] / (((bp["N"] + bn["N"]) / 2) or 1)
            if n_ratio >= VDIP_N_RATIO:
                T = small_cache_adjust(bc["lo"])
                bc["vdip"] = (n_ratio, dl, dr)
                expl = (f"OHW V字谷: ビン{bc['idx']} ({bc['label']}) が r={bc['r']:.3f} の谷 "
                        f"(-{dl:.3f}/-{dr:.3f}) かつ N_b 前後比 {n_ratio:.1f}x "
                        f"→ このビンの下端 {human_bytes(T)} を閾値とし大プールへ保護")
                adv = ("V字谷型: 谷ビンを小プールに入れると OHW バイトが流入し BMR が悪化します。"
                       "±1 ビンで谷の深さを実測確認してください。")
                return _finish(T, "vdip", expl, bc, adv, bins, sig, warnings, cache_ratio)

    # Path C: CDF 交差点 (β 均一時のフォールバック; p50 区間則を包含)
    cr = cb = 0.0
    for b in bins:
        cr += b["rs"]; cb += b["bs"]
        if cr + cb >= CDF_CROSSOVER:
            T = small_cache_adjust(b["lo"])
            betas = [x["beta"] for x in sig]
            contrast = max(betas) - min(betas)
            expl = (f"CDF交差点: ビン{b['idx']} ({b['label']}) で F_req+F_byte={cr+cb:.2f} ≥ 1 "
                    f"→ 閾値を {human_bytes(T)} に設定 (有意ビン β コントラスト={contrast:.2f})")
            adv = ("CDF交差型: β 均一トレースのリクエスト密度分離です。経験上、実測最良は "
                   "+1〜2 ビン上に出ることがあるため (wikiCDN)、T, 2T, 4T の 3 点実測を推奨。")
            if contrast < 0.30:
                warnings.append(
                    f"β コントラスト {contrast:.2f} < 0.30: 分割利得が小さい可能性 "
                    f"(metaBlk/tncntCDN 型)。実測で LRU 比 ±2% 未満なら分割なしを推奨。")
            return _finish(T, "cdf_cross", expl, b, adv, bins, sig, warnings, cache_ratio)

    return dict(threshold_bytes=None, threshold_human="N/A", path="neutral",
                explanation="全ビンで β・バイト占有・CDF 構造が均一。RegionSplit の効果は限定的。",
                signal_bin=None, advice="分割なし構成 (LRU) を推奨。", warnings=warnings,
                bmr=None, extra_thresholds=[], admission=None, beta_total=beta_total)


def _finish(T, path, expl, sig_bin, adv, bins, sig, warnings, cache_ratio):
    t_bin = int(math.log2(T)) if T else 0
    bmr = diagnose_bmr(bins, t_bin)
    extra = suggest_extra_thresholds(sig, t_bin)
    adm = diagnose_admission(bins, t_bin)
    total_req = sum(b["N"] for b in bins) or 1
    beta_total = sum(b["beta"] * b["N"] for b in bins) / total_req
    if cache_ratio <= SMALL_CACHE:
        warnings.append("小キャッシュ補正 (≤15%) により閾値を 1 段下げています。")
    return dict(
        threshold_bytes=T, threshold_human=human_bytes(T), path=path,
        explanation=expl, signal_bin=sig_bin, advice=adv, warnings=warnings,
        bmr=bmr, extra_thresholds=extra, admission=adm, beta_total=beta_total,
        crash_candidates=[],
    )


# ─── 出力 ────────────────────────────────────────────────────────
def print_text(workload, res, verbose=True):
    print()
    print(f"  ワークロード: {workload}")
    print("═" * 64)
    print(f"  推奨閾値 T = {res['threshold_human']}")
    print(f"  判定経路  = {PATH_LABELS.get(res['path'], res['path'])}")
    print("═" * 64)
    print(f"\n  判定根拠:\n    {res['explanation']}")
    print(f"\n  アドバイス:\n    {res['advice']}")
    if res.get("bmr"):
        mark = "○" if res["bmr"]["improvable"] else "×"
        print(f"\n  BMR 符号診断 (H2): {mark} {res['bmr']['note']}")
    if len(res.get("crash_candidates", [])) > 1:
        print("\n  β崩落候補 (複数):")
        for c in res["crash_candidates"]:
            print(f"    bin{c['bin_idx']} ({c['threshold_human']})  β={c['beta']}  "
                  f"デッドバイト質量={c['dead_byte_mass_pct']}%")
        print("    MR=最初の崩落 / BMR=デッドバイト最大 (--objective で切替)。")
    if res.get("extra_thresholds"):
        ts = ", ".join(human_bytes(t) for t in res["extra_thresholds"])
        print(f"\n  追加閾値候補 (equal-β banding, H3): {ts}")
        print("    大プール内の β 変化点です。N を増やす場合は 2^k 等比でなくここに置いてください。")
    adm = res.get("admission")
    if adm and verbose:
        lg = adm["large"]
        if lg["ohw_insert_byte_pct"] is not None:
            tax = lg['screening_tax_pct']
            print(f"\n  admission 診断 (§10): 大プール OHW挿入バイト率 "
                  f"{lg['ohw_insert_byte_pct']:.0f}%, スクリーニング税 "
                  f"{tax:.0f}%" if tax is not None else "")
            if lg["ohw_insert_byte_pct"] >= 60:
                print("    → 大プール限定の OHW フィルタ (probation) が有効な見込み。")
    sb = res.get("signal_bin")
    if verbose and sb:
        print(f"\n  シグナルビン詳細: bin{sb['idx']} ({sb['label']})  "
              f"β={sb['beta']:.3f}  r={sb['r']:.3f}  rho={sb['rho']:.1f}  "
              f"req={sb['rs']*100:.2f}%  byte={sb['bs']*100:.2f}%")
    for w in res.get("warnings", []):
        print(f"\n  [警告] {w}")
    print()


def to_json_record(workload, cache_ratio, max_bin, res):
    sb = res.get("signal_bin")
    return {
        "workload": workload, "cache_ratio": cache_ratio, "max_bin": max_bin,
        "threshold_bytes": res["threshold_bytes"],
        "threshold_human": res["threshold_human"],
        "path": res["path"],
        "signal_bin": ({"bin_idx": sb["idx"], "label": sb["label"],
                        "beta": round(sb["beta"], 4), "r": round(sb["r"], 4),
                        "rho": round(sb["rho"], 2),
                        "req_pct": round(sb["rs"] * 100, 3),
                        "byte_pct": round(sb["bs"] * 100, 3)} if sb else None),
        "beta_total": round(res.get("beta_total", 0), 4),
        "crash_candidates": res.get("crash_candidates", []),
        "bmr_diagnosis": res.get("bmr"),
        "extra_thresholds_bytes": res.get("extra_thresholds", []),
        "admission_diagnosis": res.get("admission"),
        "explanation": res["explanation"],
        "advice": res["advice"],
        "warnings": res.get("warnings", []),
    }


def to_csv_row(workload, cache_ratio, max_bin, res):
    sb = res.get("signal_bin") or {}
    adm = (res.get("admission") or {}).get("large", {})
    return {
        "workload": workload,
        "cache_ratio": f"{cache_ratio:.2f}",
        "max_bin": str(max_bin) if max_bin is not None else "",
        "threshold_bytes": str(res["threshold_bytes"]) if res["threshold_bytes"] else "",
        "threshold_human": res["threshold_human"],
        "path": res["path"],
        "signal_bin_idx": str(sb.get("idx", "")),
        "signal_bin_beta": f"{sb['beta']:.3f}" if sb else "",
        "signal_bin_r": f"{sb['r']:.3f}" if sb else "",
        "bmr_improvable": str((res.get("bmr") or {}).get("improvable", "")),
        "extra_thresholds": ";".join(human_bytes(t) for t in res.get("extra_thresholds", [])),
        "beta_total": f"{res.get('beta_total', 0):.3f}",
        "large_ohw_byte_pct": (f"{adm['ohw_insert_byte_pct']:.1f}"
                               if adm.get("ohw_insert_byte_pct") is not None else ""),
        "screening_tax_pct": (f"{adm['screening_tax_pct']:.1f}"
                              if adm.get("screening_tax_pct") is not None else ""),
        "explanation": res["explanation"],
    }


# ─── main ────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="RegionSplit 閾値予測器 v4 (β ベース)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("input", nargs="+", help="ビン統計 JSON (複数可)")
    ap.add_argument("--format", choices=["text", "json", "csv"], default="text")
    ap.add_argument("--cache-ratio", type=float, default=0.30, metavar="R")
    ap.add_argument("--max-bin", type=int, default=None, metavar="N",
                    help="分析対象の最大ビン番号 (スコープ外除外)")
    ap.add_argument("--objective", choices=["mr", "bmr"], default="mr",
                    help="最適化目的 (既定 mr)。bmr は複数崩落時にデッドバイト最大側を選択")
    ap.add_argument("-o", "--output", default=None, metavar="FILE")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.json:
        args.format = "json"

    out = open(args.output, "w", encoding="utf-8", newline="") if args.output else sys.stdout
    recs, rows, errors = [], [], []
    for fpath in args.input:
        workload = Path(fpath).stem.replace(".measurement", "")
        try:
            bins = load_bins(fpath)
        except Exception as e:
            errors.append(f"{fpath}: {e}")
            continue
        if args.max_bin is not None:
            bins = [b for b in bins if b["idx"] <= args.max_bin]
        res = predict_threshold(bins, cache_ratio=args.cache_ratio, objective=args.objective)
        if args.format == "text":
            print_text(workload, res, verbose=not args.quiet)
        elif args.format == "json":
            recs.append(to_json_record(workload, args.cache_ratio, args.max_bin, res))
        else:
            rows.append(to_csv_row(workload, args.cache_ratio, args.max_bin, res))

    if args.format == "json":
        json.dump(recs[0] if len(recs) == 1 else recs, out, ensure_ascii=False, indent=2)
        out.write("\n")
    elif args.format == "csv":
        w = csv.DictWriter(out, fieldnames=CSV_FIELDS, lineterminator="\n")
        w.writeheader(); w.writerows(rows)
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)
    if args.output:
        out.close()
        print(f"保存先: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
