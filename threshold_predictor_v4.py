#!/usr/bin/env python3
"""
threshold_predictor_v4.py  —  RegionSplit 最適閾値予測器 v4.1 (β ベース)
====================================================================
threshold_rethink_report.md の H1/H2/H3 を実装した v3.2 の後継。
v4.1 (2026-06-11): 細粒度実測 (30% キャッシュ, 8 トレース) による再較正。

【中核指標 β】 再利用リクエスト質量
    β_b = 1 − n_b/N_b = 1 − 1/(r_b + (1−r_b)·rho_b)
  そのビンへのリクエストのうち再参照 (理論上ヒットになり得るアクセス) の割合。
  バイト換算でも同値 (再利用バイト RB_b = β_b·N_b·mean_size)。r (オブジェクト個数
  ベース) と異なり rho を畳み込むため、「r が高くても rho が大きく実は宝庫」
  (例: cldphyBlk bin18) を誤判定しない。

【判定経路】優先順位: A' → B → D → C
  Path A': β クラッシュ — β≥0.85 の宝庫ゾーン確認後、β が (ピーク−0.25) 以下に
                          崩落する帯を検出。アンカー = 帯内で質量 (req%+byte%) が
                          3% 以上の最初のビン。微小な崩落ビンは小プール側に吸収する
                          (v4.1: msrBlk の bin13/14 (質量<3%) を飛ばし bin15=32KiB が
                           実測最良と一致。metaKV bin14・cldphyBlk bin19 は質量十分で不変)。
                          崩落帯が複数ある場合:
                            --objective mr  (既定): 最初の帯のアンカー
                            --objective bmr        : デッドバイト質量 Σ(1−β)·byte_share
                                                     最大の帯のアンカー (大サイズ側ほど
                                                     GC Region へのバイト影響が大きい)
  Path B : バルク隔離   — 単一ビンがバイトの >80% を占有 かつ そのビンの β<0.60 の
                          ときのみ発動。β が高いバルクは再利用されるため隔離が
                          逆効果 (実測: β≈0.8 のバルクでは閾値なしが最良)。
  Path D : OHW V字谷    — r が前後比 ≥0.05 の谷 かつ N_b が前後平均 1.5x 以上
                          (metaCDN bin19 型。β では山に見えるため r 信号を維持)
  Path C : CDF 交差点 + 再利用バイト重心 (v4.1 改訂) —
                          cross = F_req+F_byte ≥ 1.0 の最初のビン
                          p50RB = 再利用バイト (β·byte) の累積 50% ビン
                          p50RB − cross ≤ 1 なら T=cross (分布が狭く交差点が鋭い:
                          tncntBlk=16KiB ✓)、それ以外は T=p50RB (バイト裾が重い場合は
                          保護域を再利用バイト重心まで広げる: wikiCDN=512KiB ✓)。
                          予測区間 [cross, p50B] も出力 — 実測最良は 8/8 トレースで
                          この区間内に入った。ゴースト検証はこの区間で行うこと。

【BMR 推奨 (v4.1)】
  - Path A'/D 型 (β 構造あり): T_BMR = T_MR (実測: metaKV/msrBlk/cldphyBlk/metaCDN は
    MR/BMR とも同一 T が最良)
  - Path C 型 (β 均一) は大プールのバイト加重 β で分岐:
      ≥ 0.95 (実質 junk フリー): T_BMR = T_MR — 座礁バイトが無く Region 均質化 (H3-b)
              の利得が残る (実測: tncntBlk 16KiB で BMR +6.3%)
      < 0.95: 分割なしを推奨 — 中庸 β のバイトが固定配分で座礁し BMR 悪化
              (実測: tncntCDN=分割なし最良 (0.84), wikiCDN=最小閾値最良 (0.90))。
              強制分割時は最小閾値 (干渉最小化)

【検証 (30% キャッシュ, 細粒度実測 2026-06-11)】
  MR : metaKV 16KiB ✓ / tncntBlk 16KiB ✓ / cldphyBlk 512KiB ✓ / metaCDN(≤27) 512KiB ✓
       msrBlk 32KiB ✓ / wikiCDN 512KiB ✓ / tncntCDN 予測16KiB vs 実測32KiB (±1, 改善≈0,
       予測区間内) / metaBlk 改善なし ✓ (低コントラスト警告)
  BMR: metaKV 16KiB ✓ / msrBlk 32KiB ✓ / cldphyBlk 512KiB ✓ / metaCDN 512KiB ✓ /
       tncntBlk 16KiB ✓ / tncntCDN 分割なし ✓ / wikiCDN 分割なし(強制時は最小閾値) ✓

使用例:
  python threshold_predictor_v4.py files/metaKV.measurement.json
  python threshold_predictor_v4.py files/*.measurement.json --format csv
  python threshold_predictor_v4.py files/metaCDN.measurement.json --max-bin 27 --format json
  python threshold_predictor_v4.py files/wikiCDN.measurement.json --objective bmr
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

# ─── 較正定数 (細粒度実測 2026-06-11 で再較正; 感度分析は E6 系実験で) ──
BETA_HIGH       = 0.85   # A': 宝庫ゾーン認定の β 下限
BETA_DROP       = 0.25   # A': ピーク β からの崩落幅
CRASH_MASS_MIN  = 0.03   # A': 崩落アンカーに必要な質量 (req%+byte%)。未満は小プールへ吸収
BETA_BULK_MAX   = 0.60   # B : バルク隔離を許す β の上限
BETA_JUNK       = 0.40   # H2: 「ゴミ」とみなす β 上限
BETA_STEP       = 0.12   # H3: 追加閾値候補とみなす隣接ビン β 変化幅
BULK_THRESHOLD  = 0.80   # B : 単一ビンのバイト占有率
VDIP_R_DROP     = 0.05   # D : r 谷の最小深さ
VDIP_N_RATIO    = 1.5    # D : N_b の前後平均比
CDF_CROSSOVER   = 1.0    # C : F_req + F_byte の交差値
C_GAP_MAX       = 1      # C : p50RB − cross がこれ以下なら cross を採用
BETA_LARGE_PURE = 0.95   # C-BMR: 大プールバイト加重 β がこれ以上なら分割しても BMR 安全
SIG_REQ_MIN     = 0.005  # 有意ビン: 最低リクエスト割合
SIG_BYTE_MIN    = 0.005  # 有意ビン: 最低バイト割合
BETA_TOTAL_LOW  = 0.40   # 全体 β がこれ未満なら「OHW 支配」警告 (twiKV=0.37)
JUNK_BYTE_MIN   = 0.15   # H2: BMR 改善に必要な大プール内ゴミバイト質量
SMALL_CACHE     = 0.15   # これ以下のキャッシュ比率で閾値を 1 段下げる

CSV_FIELDS = [
    "workload", "cache_ratio", "max_bin",
    "threshold_bytes", "threshold_human", "path",
    "interval_lo", "interval_hi",
    "signal_bin_idx", "signal_bin_beta", "signal_bin_r",
    "bmr_improvable", "extra_thresholds", "beta_total",
    "large_ohw_byte_pct", "screening_tax_pct",
    "explanation",
]

PATH_LABELS = {
    "bulk":           "Path B : バルク隔離",
    "bulk_high_beta": "Path B : バルク高β → 分割非推奨",
    "beta_crash":     "Path A': βクラッシュ",
    "vdip":           "Path D : OHW V字谷",
    "cdf_cross":      "Path C : CDF交差点",
    "cdf_p50rb":      "Path C : 再利用バイト重心 (裾重)",
    "bmr_nosplit":    "Path C : BMR目的 → 分割非推奨",
    "neutral":        "Neutral: 効果限定的",
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
    """H2: 単一閾値で BMR 改善が見込めるか。junk 隔離 (低βバイト質量+コントラスト) と
    Region 均質化 (大プールが実質 junk フリー) の 2 機構を判定する。"""
    total_bytes = sum(b["bytes"] for b in bins) or 1
    large = [b for b in bins if b["idx"] >= t_bin]
    small = [b for b in bins if b["idx"] < t_bin]
    junk = sum(b["bytes"] for b in large if b["beta"] < BETA_JUNK) / total_bytes
    small_max_beta = max((b["beta"] for b in small), default=0.0)
    lb = sum(b["bytes"] for b in large) or 1
    large_beta_byte = sum(b["beta"] * b["bytes"] for b in large) / lb
    improvable = junk >= JUNK_BYTE_MIN and small_max_beta >= 0.75
    if improvable:
        note = "大プールに β<0.4 のバイト質量が {:.0f}% あり、junk 隔離で BMR 改善余地あり".format(junk * 100)
    elif large_beta_byte >= BETA_LARGE_PURE:
        note = ("大プールのバイト加重 β={:.2f} ≥ {:.2f} (実質 junk フリー): 座礁バイトが無く、"
                "Region 均質化により BMR は悪化しない〜小幅改善 (tncntBlk 型)".format(
                    large_beta_byte, BETA_LARGE_PURE))
    else:
        note = ("大プールの低βバイト質量 {:.0f}% (<{:.0f}%) かつバイト加重 β={:.2f} が中庸: "
                "固定配分で再利用バイトが座礁し BMR は悪化見込み。BMR 重視なら分割なし、"
                "または equal-β 多閾値・大プール admission を検討".format(
                    junk * 100, JUNK_BYTE_MIN * 100, large_beta_byte))
    return dict(
        junk_byte_share=junk,
        small_max_beta=small_max_beta,
        large_beta_byte=large_beta_byte,
        improvable=improvable,
        bmr_safe=(improvable or large_beta_byte >= BETA_LARGE_PURE),
        note=note,
    )


def suggest_extra_thresholds(sig, t_bin):
    """H3: T 以上の有意ビン列で β 変化点 (|Δβ|≥0.12) を追加閾値候補として返す。"""
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

    # ── Path A': β クラッシュ ──
    # 帯 (band) 単位で検出し、各帯のアンカー = 質量 (req+byte) ≥ CRASH_MASS_MIN の
    # 最初のビン。アンカーを持たない帯 (微小崩落) は無視し小プール側に吸収する。
    peak = None
    bands = []
    in_band = False
    for b in sig:
        if b["beta"] >= BETA_HIGH:
            peak = max(peak or 0.0, b["beta"])
        if peak is not None and b["beta"] <= peak - BETA_DROP:
            if not in_band:
                bands.append({"bins": [], "dead_mass": 0.0})
                in_band = True
            bands[-1]["bins"].append(b)
            bands[-1]["dead_mass"] += (1.0 - b["beta"]) * b["bs"]
        else:
            in_band = False
    candidates = []
    for band in bands:
        anchor = next((x for x in band["bins"] if x["rs"] + x["bs"] >= CRASH_MASS_MIN), None)
        if anchor is not None:
            skipped = [x["idx"] for x in band["bins"] if x["idx"] < anchor["idx"]]
            candidates.append({"bin": anchor, "dead_mass": band["dead_mass"],
                               "skipped_bins": skipped})
    if candidates:
        chosen = candidates[0] if objective == "mr" \
            else max(candidates, key=lambda c: c["dead_mass"])
        b = chosen["bin"]
        T = small_cache_adjust(b["lo"])
        cand_str = ", ".join(f"bin{c['bin']['idx']}({c['bin']['label']}, "
                             f"dead={c['dead_mass']*100:.1f}%)" for c in candidates)
        skip_str = (f" 質量<{CRASH_MASS_MIN*100:.0f}%の崩落ビン {chosen['skipped_bins']} は"
                    f"小プールへ吸収。" if chosen["skipped_bins"] else "")
        expl = (f"βクラッシュ: ビン{b['idx']} ({b['label']}) をアンカーに崩落帯を隔離 "
                f"(β={b['beta']:.2f}, ピーク β={peak:.2f}, 候補帯 {len(candidates)}: {cand_str})."
                f"{skip_str} → 閾値 {human_bytes(T)} [objective={objective}]")
        adv = (f"βクラッシュ型: 崩落アンカー以上を大プールへ隔離します。"
               f"検証: metaKV 16KiB / cldphyBlk 512KiB / msrBlk 32KiB が細粒度実測最良と一致。"
               f"BMR 目的でも同じ T が最良でした (β 構造があるトレースは MR/BMR の最適が一致)。")
        if len(candidates) > 1:
            adv += (" 崩落帯が複数あります: MR は最初の帯、BMR はデッドバイト最大の帯が原則。"
                    "質量が近い場合は両方の実測比較を推奨。equal-β banding の適用サインでもあります。")
        res = _finish(T, "beta_crash", expl, b, adv, bins, sig, warnings, cache_ratio)
        res["interval"] = [T, T]
        res["crash_candidates"] = [
            {"bin_idx": c["bin"]["idx"], "threshold_bytes": c["bin"]["lo"],
             "threshold_human": c["bin"]["label"], "beta": round(c["bin"]["beta"], 3),
             "dead_byte_mass_pct": round(c["dead_mass"] * 100, 2),
             "skipped_bins": c["skipped_bins"]} for c in candidates]
        return res

    # ── Path B: バルク隔離 (β ゲート付き) ──
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

    # ── Path D: OHW V字谷 (r 信号 + N_b 集中。β では山に見える metaCDN bin19 型) ──
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
                       "検証: metaCDN (--max-bin 27) で T=512KiB が MR/BMR とも細粒度実測最良と一致。")
                return _finish(T, "vdip", expl, bc, adv, bins, sig, warnings, cache_ratio)

    # ── Path C: CDF 交差点 + 再利用バイト重心 (v4.1) ──
    cr = cb = crb = 0.0
    total_rb = sum(b["beta"] * b["bytes"] for b in bins) or 1
    cross_b = p50b_b = p50rb_b = None
    for b in bins:
        cr += b["rs"]
        cb += b["bs"]
        crb += b["beta"] * b["bytes"] / total_rb
        if cross_b is None and cr + cb >= CDF_CROSSOVER:
            cross_b = b
        if p50b_b is None and cb >= 0.5:
            p50b_b = b
        if p50rb_b is None and crb >= 0.5:
            p50rb_b = b
    if cross_b is not None:
        p50rb_b = p50rb_b or bins[-1]
        p50b_b = p50b_b or bins[-1]
        gap = p50rb_b["idx"] - cross_b["idx"]
        use_cross = gap <= C_GAP_MAX
        pick = cross_b if use_cross else p50rb_b
        path = "cdf_cross" if use_cross else "cdf_p50rb"
        T = small_cache_adjust(pick["lo"])
        interval = [small_cache_adjust(cross_b["lo"]), small_cache_adjust(p50b_b["lo"])]
        betas = [x["beta"] for x in sig]
        contrast = max(betas) - min(betas)
        bmr_diag = diagnose_bmr(bins, pick["idx"])

        # BMR 目的: junk 隔離も Region 均質化も効かないトレースでは分割なしが最良
        if objective == "bmr" and not bmr_diag["bmr_safe"]:
            expl = (f"β 均一トレース (コントラスト={contrast:.2f}) で、大プールのバイト加重 "
                    f"β={bmr_diag['large_beta_byte']:.2f} が中庸 (junk なし・junk フリーでもない)。"
                    f"固定配分で再利用バイトが座礁するため BMR は分割で改善しない "
                    f"(実測: tncntCDN=分割なし, wikiCDN=最小閾値が BMR 最良)。")
            adv = ("BMR 目的では分割なし (LRU) を推奨。MR とのトレードオフで分割する場合は"
                   f"最小閾値 (1KiB 等) が BMR 劣化を最小化します。MR 重視なら "
                   f"{human_bytes(T)} (--objective mr の予測) を使用。")
            res = _finish(None, "bmr_nosplit", expl, pick, adv, bins, sig, warnings, cache_ratio)
            res["threshold_bytes"] = None
            res["threshold_human"] = "なし (BMR目的では分割非推奨)"
            res["interval"] = interval
            return res

        if use_cross:
            expl = (f"CDF交差点: ビン{cross_b['idx']} ({cross_b['label']}) で F_req+F_byte ≥ 1。"
                    f"p50(再利用バイト)=bin{p50rb_b['idx']} との乖離 {gap} ≤ {C_GAP_MAX} のため"
                    f"交差点を採用 → 閾値 {human_bytes(T)}")
        else:
            expl = (f"再利用バイト重心: CDF交差点 bin{cross_b['idx']} と p50(再利用バイト) "
                    f"bin{p50rb_b['idx']} が {gap} ビン乖離 (バイト裾が重い)。保護域を再利用バイト"
                    f"重心まで拡張 → 閾値 {human_bytes(T)} (wikiCDN 型)")
        adv = (f"Path C: 実測最良は 8/8 トレースで予測区間 [{human_bytes(interval[0])}, "
               f"{human_bytes(interval[1])}] 内。ゴースト検証はこの区間で実施を推奨。"
               f"検証: tncntBlk 16KiB ✓ / wikiCDN 512KiB ✓ (tncntCDN は ±1 ビン, 改善≈0)。")
        if contrast < 0.30:
            warnings.append(
                f"β コントラスト {contrast:.2f} < 0.30: 分割利得が小さい可能性 "
                f"(metaBlk/tncntCDN 型)。実測で LRU 比 ±2% 未満なら分割なしを推奨。")
        res = _finish(T, path, expl, pick, adv, bins, sig, warnings, cache_ratio)
        res["interval"] = interval
        return res

    return dict(threshold_bytes=None, threshold_human="N/A", path="neutral",
                explanation="全ビンで β・バイト占有・CDF 構造が均一。RegionSplit の効果は限定的。",
                signal_bin=None, advice="分割なし構成 (LRU) を推奨。", warnings=warnings,
                bmr=None, extra_thresholds=[], admission=None, beta_total=beta_total,
                crash_candidates=[], interval=None)


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
        crash_candidates=[], interval=None,
    )


# ─── 出力 ────────────────────────────────────────────────────────
def print_text(workload, res, verbose=True):
    print()
    print(f"  ワークロード: {workload}")
    print("═" * 64)
    print(f"  推奨閾値 T = {res['threshold_human']}")
    print(f"  判定経路  = {PATH_LABELS.get(res['path'], res['path'])}")
    if res.get("interval") and res["interval"][0] != res["interval"][1]:
        print(f"  予測区間  = [{human_bytes(res['interval'][0])}, "
              f"{human_bytes(res['interval'][1])}] (ゴースト検証推奨範囲)")
    print("═" * 64)
    print(f"\n  判定根拠:\n    {res['explanation']}")
    print(f"\n  アドバイス:\n    {res['advice']}")
    if res.get("bmr"):
        mark = "○" if res["bmr"]["bmr_safe"] else "×"
        print(f"\n  BMR 診断 (H2+H3b): {mark} {res['bmr']['note']}")
    if len(res.get("crash_candidates", [])) > 1:
        print("\n  β崩落候補 (複数):")
        for c in res["crash_candidates"]:
            print(f"    bin{c['bin_idx']} ({c['threshold_human']})  β={c['beta']}  "
                  f"デッドバイト質量={c['dead_byte_mass_pct']}%")
        print("    MR=最初の帯 / BMR=デッドバイト最大の帯 (--objective で切替)。")
    if res.get("extra_thresholds"):
        ts = ", ".join(human_bytes(t) for t in res["extra_thresholds"])
        print(f"\n  追加閾値候補 (equal-β banding, H3): {ts}")
        print("    大プール内の β 変化点です。N を増やす場合は 2^k 等比でなくここに置いてください。")
    adm = res.get("admission")
    if adm and verbose:
        lg = adm["large"]
        if lg["ohw_insert_byte_pct"] is not None:
            tax = lg['screening_tax_pct']
            line = f"\n  admission 診断 (§10): 大プール OHW挿入バイト率 {lg['ohw_insert_byte_pct']:.0f}%"
            if tax is not None:
                line += f", スクリーニング税 {tax:.0f}%"
            print(line)
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
        "interval_bytes": res.get("interval"),
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
    iv = res.get("interval") or [None, None]
    return {
        "workload": workload,
        "cache_ratio": f"{cache_ratio:.2f}",
        "max_bin": str(max_bin) if max_bin is not None else "",
        "threshold_bytes": str(res["threshold_bytes"]) if res["threshold_bytes"] else "",
        "threshold_human": res["threshold_human"],
        "path": res["path"],
        "interval_lo": human_bytes(iv[0]) if iv[0] else "",
        "interval_hi": human_bytes(iv[1]) if iv[1] else "",
        "signal_bin_idx": str(sb.get("idx", "")),
        "signal_bin_beta": f"{sb['beta']:.3f}" if sb else "",
        "signal_bin_r": f"{sb['r']:.3f}" if sb else "",
        "bmr_improvable": str((res.get("bmr") or {}).get("bmr_safe", "")),
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
        description="RegionSplit 閾値予測器 v4.1 (β ベース)",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("input", nargs="+", help="ビン統計 JSON (複数可)")
    ap.add_argument("--format", choices=["text", "json", "csv"], default="text")
    ap.add_argument("--cache-ratio", type=float, default=0.30, metavar="R")
    ap.add_argument("--max-bin", type=int, default=None, metavar="N",
                    help="分析対象の最大ビン番号 (スコープ外除外)")
    ap.add_argument("--objective", choices=["mr", "bmr"], default="mr",
                    help="最適化目的 (既定 mr)。bmr: 複数崩落帯はデッドバイト最大側、"
                         "β均一+大プール中庸βでは分割なしを推奨")
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
