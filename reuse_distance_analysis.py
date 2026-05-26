"""
reuse_distance_analysis.py
==========================
OracleGeneral の next_access_vtime を使った再利用距離分析

【目的・位置づけ】
  これまでの実験で「閾値パラメータと性能の相関が低い」という結果が得られた。
  原因仮説:「同サイズのコンテンツはreuse timeが似通う」という前提が崩れている。

  本スクリプトは next_access_vtime を直接使い、以下を定量的に検証する:

  【検証1】サイズクラス内 reuse distance 分散 vs クラス間分散
    → クラス内分散 >> クラス間分散 なら「仮定は間違い」と確定
    → 逆なら「仮定は正しいが別の要因で相関が低い」

  【検証2】サイズクラスごとの reuse distance 分布形状
    → 分布が重なりが大きい = サイズで分けても reuse time の均質性は得られない

  【検証3】One-Hit-Wonder 率のサイズクラス別比較
    → 大きいオブジェクトほど OHW が多い = 退避汚染の直接証拠

  【検証4】Reuse Distance と サイズの相関
    → 負の相関 → 大オブジェクトほど再利用が少ない = 仮説2（人気度スキュー）の根拠

【使い方】
  python reuse_distance_analysis.py \
      --trace ./traces/cdn_trace.oracleGeneral \
      --thresholds 256 4096 65536 1048576 \
      --out ./output/reuse_dist

  # 複数トレース一括
  python reuse_distance_analysis.py \
      --trace-dir ./traces \
      --out ./output/reuse_dist

【出力】
  {out_dir}/{trace_name}_rd_stats.csv        - サイズクラス別 RD 統計
  {out_dir}/{trace_name}_rd_variance.csv     - クラス内/クラス間分散比 (η²)
  {out_dir}/{trace_name}_rd_distribution.png - RD 分布の可視化
  {out_dir}/{trace_name}_hypothesis_test.txt - 仮説検定結果サマリー
  {out_dir}/ALL_TRACES_summary.csv           - 全トレース横断サマリー
"""

import argparse
import os
import struct
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import spearmanr, kruskal, mannwhitneyu
from scipy.special import kl_div as scipy_kl_div

# ─────────────────────────────────────────────
# 定数
# ─────────────────────────────────────────────
DEFAULT_THRESHOLDS = [256, 4_096, 65_536, 1_048_576]
OG_FORMAT      = "=IQIq"
OG_RECORD_SIZE = struct.calcsize(OG_FORMAT)


# ─────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────

def get_size_class(size: int, thresholds: list) -> int:
    for i, t in enumerate(thresholds):
        if size < t:
            return i
    return len(thresholds)


def build_class_labels(thresholds: list) -> dict:
    units = [(1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB"), (1, "B")]

    def fmt(b):
        for div, unit in units:
            if b >= div and b % div == 0:
                return f"{b // div}{unit}"
        return f"{b}B"

    boundaries = [0] + thresholds + [None]
    labels = {}
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if lo == 0 and hi is not None:
            labels[i] = f"<{fmt(hi)}"
        elif hi is None:
            labels[i] = f"≥{fmt(lo)}"
        else:
            labels[i] = f"{fmt(lo)}-{fmt(hi)}"
    return labels


# ─────────────────────────────────────────────
# OracleGeneral 読み込み
# ─────────────────────────────────────────────

def load_oracle_general(path: str, max_requests: int = None) -> pd.DataFrame:
    records = []
    with open(path, "rb") as f:
        raw = f.read()

    n = len(raw) // OG_RECORD_SIZE
    if max_requests:
        n = min(n, max_requests)

    for i in range(n):
        off = i * OG_RECORD_SIZE
        ts, obj_id, obj_size, next_vtime = struct.unpack_from(OG_FORMAT, raw, off)
        if obj_size > 0:
            records.append((i, ts, obj_id, obj_size, next_vtime))

    df = pd.DataFrame(records,
                      columns=["vtime", "timestamp", "obj_id", "obj_size", "next_access_vtime"])
    df["obj_id"] = df["obj_id"].astype(str)
    print(f"  読み込み: {len(df):,} リクエスト  "
          f"ユニーク={df['obj_id'].nunique():,}  "
          f"OHW率={( df['next_access_vtime'] == -1 ).mean():.3%}")
    return df


# ─────────────────────────────────────────────
# Reuse Distance の計算
# ─────────────────────────────────────────────

def compute_reuse_distances(df: pd.DataFrame) -> pd.DataFrame:
    """
    next_access_vtime から reuse distance（バーチャル時刻差）を計算する。

    reuse_dist = next_access_vtime - vtime
      - next_access_vtime == -1 の場合は np.inf（再利用なし）
      - 初回アクセスのみのオブジェクトも含まれる（cold miss）
    """
    df = df.copy()
    df["reuse_dist"] = df["next_access_vtime"] - df["vtime"]
    df.loc[df["next_access_vtime"] == -1, "reuse_dist"] = np.inf
    return df


# ─────────────────────────────────────────────
# 検証1: クラス内/クラス間分散比 (η²)
# ─────────────────────────────────────────────

def compute_eta_squared(df: pd.DataFrame, thresholds: list) -> dict:
    """
    η²（イータ二乗）= クラス間 SS / 全体 SS
    η² が大きい → サイズクラスが reuse distance をよく説明する
                  → 同サイズのreuse timeが似通うという仮定が有効
    η² が小さい → サイズクラスは reuse distance をほとんど説明しない
                  → 仮定が崩れている（今回のケース）

    有限 reuse distance のみで計算（OHW は除外）。
    """
    finite = df[np.isfinite(df["reuse_dist"])].copy()
    finite["size_class"] = finite["obj_size"].apply(
        lambda s: get_size_class(s, thresholds)
    )

    if len(finite) == 0:
        return {"eta_squared": np.nan, "n_finite": 0}

    overall_mean = finite["reuse_dist"].mean()
    ss_total = ((finite["reuse_dist"] - overall_mean) ** 2).sum()

    class_means = finite.groupby("size_class")["reuse_dist"].mean()
    class_counts = finite.groupby("size_class")["reuse_dist"].count()
    ss_between = sum(
        class_counts[ci] * (class_means[ci] - overall_mean) ** 2
        for ci in class_means.index
    )

    eta_sq = ss_between / ss_total if ss_total > 0 else 0.0

    # Kruskal-Wallis 検定（非パラメトリック）
    groups = [
        finite[finite["size_class"] == ci]["reuse_dist"].values
        for ci in sorted(finite["size_class"].unique())
        if len(finite[finite["size_class"] == ci]) > 1
    ]
    kw_stat, kw_pval = kruskal(*groups) if len(groups) >= 2 else (np.nan, np.nan)

    return {
        "eta_squared":  float(eta_sq),
        "n_finite":     int(len(finite)),
        "n_inf":        int((~np.isfinite(df["reuse_dist"])).sum()),
        "kw_statistic": float(kw_stat) if not np.isnan(kw_stat) else np.nan,
        "kw_pvalue":    float(kw_pval) if not np.isnan(kw_pval) else np.nan,
    }


# ─────────────────────────────────────────────
# 検証2 & 3: サイズクラス別 RD 統計と OHW 率
# ─────────────────────────────────────────────

def compute_class_rd_stats(df: pd.DataFrame, thresholds: list) -> pd.DataFrame:
    """
    サイズクラス別に reuse distance の統計を計算する。
    OHW 率（one-hit-wonder = next_access_vtime == -1）も含む。
    """
    df = df.copy()
    df["size_class"] = df["obj_size"].apply(lambda s: get_size_class(s, thresholds))
    class_labels = build_class_labels(thresholds)

    rows = []
    for ci in sorted(df["size_class"].unique()):
        sub = df[df["size_class"] == ci]
        finite_rd = sub.loc[np.isfinite(sub["reuse_dist"]), "reuse_dist"].values
        ohw_mask = sub["next_access_vtime"] == -1

        row = {
            "size_class":        ci,
            "size_class_label":  class_labels.get(ci, str(ci)),
            "n_requests":        len(sub),
            "ohw_frac":          float(ohw_mask.mean()),
            "ohw_count":         int(ohw_mask.sum()),
        }

        if len(finite_rd) > 0:
            row.update({
                "rd_mean":   float(np.mean(finite_rd)),
                "rd_median": float(np.median(finite_rd)),
                "rd_std":    float(np.std(finite_rd)),
                "rd_cv":     float(np.std(finite_rd) / np.mean(finite_rd))
                             if np.mean(finite_rd) > 0 else np.nan,
                "rd_p10":    float(np.percentile(finite_rd, 10)),
                "rd_p25":    float(np.percentile(finite_rd, 25)),
                "rd_p75":    float(np.percentile(finite_rd, 75)),
                "rd_p90":    float(np.percentile(finite_rd, 90)),
                "rd_p99":    float(np.percentile(finite_rd, 99)),
                "n_finite_rd": len(finite_rd),
            })
        else:
            for key in ["rd_mean","rd_median","rd_std","rd_cv",
                        "rd_p10","rd_p25","rd_p75","rd_p90","rd_p99","n_finite_rd"]:
                row[key] = np.nan

        rows.append(row)

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# 検証4: RD とサイズの Spearman 相関
# ─────────────────────────────────────────────

def compute_size_rd_correlation(df: pd.DataFrame) -> dict:
    """
    reuse distance とオブジェクトサイズの Spearman 相関を計算する。
    finite な reuse distance のみ使用。

    負の相関 → 大きいオブジェクトほど再利用が少ない = 仮説2の根拠
    相関なし → サイズと reuse distance は独立 = 仮定が崩れている原因
    """
    finite = df[np.isfinite(df["reuse_dist"])]
    if len(finite) < 10:
        return {"size_rd_rho": np.nan, "size_rd_pvalue": np.nan}

    # 大規模トレース対応: サンプリング
    if len(finite) > 500_000:
        sample = finite.sample(500_000, random_state=42)
    else:
        sample = finite

    rho, pval = spearmanr(sample["obj_size"], sample["reuse_dist"])
    return {
        "size_rd_rho":    float(rho),
        "size_rd_pvalue": float(pval),
    }


# ─────────────────────────────────────────────
# 分布の重なり計算 (Bhattacharyya 係数)
# ─────────────────────────────────────────────

def compute_distribution_overlap(
    df: pd.DataFrame, thresholds: list, n_bins: int = 100
) -> pd.DataFrame:
    """
    サイズクラス間の reuse distance 分布の重なりを計算する。

    Bhattacharyya 係数 BC(p, q) = Σ √(p_i * q_i)
      BC = 1: 完全に同じ分布
      BC = 0: 全く重ならない分布

    BC が高い → クラス間の分布形状が似ている
              → サイズ分割の根拠が「reuse time の均質性」では説明できない
    """
    df = df.copy()
    df["size_class"] = df["obj_size"].apply(lambda s: get_size_class(s, thresholds))
    class_labels = build_class_labels(thresholds)

    finite = df[np.isfinite(df["reuse_dist"])]
    n_classes = len(thresholds) + 1
    classes = sorted(finite["size_class"].unique())

    # 共通ビン
    rd_min = float(finite["reuse_dist"].quantile(0.01))
    rd_max = float(finite["reuse_dist"].quantile(0.99))
    bins = np.linspace(rd_min, rd_max, n_bins + 1)

    # 各クラスのヒストグラムを正規化
    histograms = {}
    for ci in classes:
        rd = finite[finite["size_class"] == ci]["reuse_dist"].values
        hist, _ = np.histogram(rd, bins=bins)
        total = hist.sum()
        histograms[ci] = hist / total if total > 0 else hist.astype(float)

    # Bhattacharyya 係数のペア行列
    overlap_rows = []
    for i in range(len(classes)):
        for j in range(i + 1, len(classes)):
            ci, cj = classes[i], classes[j]
            bc = float(np.sum(np.sqrt(histograms[ci] * histograms[cj])))
            overlap_rows.append({
                "class_a": class_labels.get(ci, str(ci)),
                "class_b": class_labels.get(cj, str(cj)),
                "bhattacharyya_coef": bc,
                "interpretation": (
                    "分布が非常に似ている（仮定有効）" if bc > 0.8 else
                    "中程度の重なり" if bc > 0.5 else
                    "分布が異なる（仮定無効）"
                ),
            })

    return pd.DataFrame(overlap_rows)


# ─────────────────────────────────────────────
# 可視化
# ─────────────────────────────────────────────

def plot_rd_distributions(
    df: pd.DataFrame,
    thresholds: list,
    trace_name: str,
    out_path: str,
    n_sample: int = 100_000,
):
    """
    サイズクラス別 reuse distance 分布を可視化する。
    4パネル構成:
      (A) RD 累積分布（CDF）- クラス間の差を最も見やすく示す
      (B) RD ヒストグラム（対数スケール）
      (C) OHW 率のクラス別棒グラフ
      (D) サイズ vs RD の散布図（サンプリング）
    """
    df = df.copy()
    df["size_class"] = df["obj_size"].apply(lambda s: get_size_class(s, thresholds))
    class_labels = build_class_labels(thresholds)
    n_classes = len(thresholds) + 1
    classes = sorted(df["size_class"].unique())
    colors = plt.cm.tab10(np.linspace(0, 1, max(n_classes, 2)))

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"{trace_name}  |  再利用距離（Reuse Distance）分析\n"
        f"仮説:「同サイズのオブジェクトは reuse distance が似通う」の検証",
        fontsize=12
    )

    # ── (A) RD の CDF ──
    ax = axes[0, 0]
    for i, ci in enumerate(classes):
        finite = df[(df["size_class"] == ci) & np.isfinite(df["reuse_dist"])]
        if len(finite) == 0:
            continue
        rd = np.sort(finite["reuse_dist"].values)
        cdf = np.arange(1, len(rd) + 1) / len(rd)
        ax.plot(rd, cdf, color=colors[i], linewidth=1.5,
                label=class_labels.get(ci, str(ci)))
    ax.set_xscale("log")
    ax.set_xlabel("Reuse Distance（仮想時刻差）")
    ax.set_ylabel("累積確率")
    ax.set_title("(A) Reuse Distance の CDF\n"
                 "曲線が重なる → クラス間で分布が似ている")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── (B) RD ヒストグラム（対数スケール） ──
    ax = axes[0, 1]
    finite_all = df[np.isfinite(df["reuse_dist"])]
    if len(finite_all) > 0:
        rd_min = float(np.percentile(finite_all["reuse_dist"], 1))
        rd_max = float(np.percentile(finite_all["reuse_dist"], 99))
        bins = np.logspace(np.log10(max(rd_min, 1)), np.log10(max(rd_max, 2)), 50)
        for i, ci in enumerate(classes):
            finite = finite_all[finite_all["size_class"] == ci]
            if len(finite) == 0:
                continue
            ax.hist(finite["reuse_dist"].values, bins=bins, alpha=0.5,
                    color=colors[i], label=class_labels.get(ci, str(ci)),
                    density=True)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Reuse Distance（仮想時刻差）")
    ax.set_ylabel("密度（対数）")
    ax.set_title("(B) RD ヒストグラム（対数スケール）\n"
                 "ピーク位置が異なる → クラス間で分布が異なる")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── (C) OHW 率 ──
    ax = axes[1, 0]
    ohw_rates = []
    class_names = []
    for ci in classes:
        sub = df[df["size_class"] == ci]
        ohw_rate = float((sub["next_access_vtime"] == -1).mean())
        ohw_rates.append(ohw_rate)
        class_names.append(class_labels.get(ci, str(ci)))
    bars = ax.bar(range(len(classes)), ohw_rates,
                  color=[colors[i] for i in range(len(classes))], alpha=0.8)
    ax.set_xticks(range(len(classes)))
    ax.set_xticklabels(class_names, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("One-Hit-Wonder 率")
    ax.set_title("(C) OHW 率（サイズクラス別）\n"
                 "大クラスの OHW が高い → 退避汚染の直接証拠（仮説1・2）")
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, ohw_rates):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.01,
                f"{val:.2%}", ha="center", va="bottom", fontsize=8)

    # ── (D) サイズ vs RD 散布図 ──
    ax = axes[1, 1]
    sample_size = min(n_sample, len(df))
    sample = df.sample(sample_size, random_state=42)
    finite_sample = sample[np.isfinite(sample["reuse_dist"])]

    if len(finite_sample) > 0:
        sc_colors = [colors[get_size_class(s, thresholds)]
                     for s in finite_sample["obj_size"]]
        ax.scatter(
            finite_sample["obj_size"],
            finite_sample["reuse_dist"],
            c=sc_colors, alpha=0.15, s=3,
        )
        # サイズクラス別の中央値ライン
        for i, ci in enumerate(classes):
            fc = finite_sample[
                finite_sample["obj_size"].apply(
                    lambda s: get_size_class(s, thresholds)
                ) == ci
            ]
            if len(fc) > 10:
                size_bins = np.percentile(fc["obj_size"], np.linspace(5, 95, 10))
                rd_medians = []
                for k in range(len(size_bins) - 1):
                    mask = (fc["obj_size"] >= size_bins[k]) & (fc["obj_size"] < size_bins[k + 1])
                    if mask.sum() > 5:
                        rd_medians.append((
                            float(np.mean(size_bins[k:k+2])),
                            float(fc.loc[mask, "reuse_dist"].median())
                        ))
                if rd_medians:
                    xs, ys = zip(*rd_medians)
                    ax.plot(xs, ys, color=colors[i], linewidth=2,
                            label=class_labels.get(ci, str(ci)))

        rho, pval = spearmanr(finite_sample["obj_size"], finite_sample["reuse_dist"])
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("オブジェクトサイズ（バイト）")
        ax.set_ylabel("Reuse Distance")
        ax.set_title(f"(D) サイズ vs Reuse Distance\n"
                     f"Spearman ρ={rho:.3f}  (p={pval:.3g})\n"
                     f"ρ<0 → 大きいほど再利用が少ない（仮説2の根拠）")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


# ─────────────────────────────────────────────
# 仮説検定テキストサマリー
# ─────────────────────────────────────────────

def write_hypothesis_report(
    trace_name: str,
    eta_result: dict,
    rd_stats: pd.DataFrame,
    overlap_df: pd.DataFrame,
    corr_result: dict,
    out_path: str,
):
    lines = ["=" * 68]
    lines.append(f"  Reuse Distance 仮説検定レポート: {trace_name}")
    lines.append("=" * 68)
    lines.append("")

    # ── 検証1: η² ──
    lines.append("【検証1】η²（サイズクラスが reuse distance を説明する割合）")
    lines.append("-" * 50)
    eta = eta_result.get("eta_squared", np.nan)
    if not np.isnan(eta):
        lines.append(f"  η² = {eta:.4f}")
        lines.append(f"  解釈: " + (
            "サイズクラスが RD を強く説明 → 仮定がある程度有効" if eta > 0.14 else
            "サイズクラスが RD をやや説明 → 仮定は部分的に有効" if eta > 0.06 else
            "サイズクラスは RD をほとんど説明しない → 仮定が崩れている ★"
        ))
        lines.append(f"  （参考: 0.01=小効果, 0.06=中効果, 0.14=大効果）")
        kw_p = eta_result.get("kw_pvalue", np.nan)
        if not np.isnan(kw_p):
            lines.append(f"  Kruskal-Wallis p = {kw_p:.4g}  "
                         + ("（クラス間に有意差あり）" if kw_p < 0.05 else "（有意差なし）"))
    else:
        lines.append("  データ不足")
    lines.append("")

    # ── 検証2: 分布の重なり ──
    lines.append("【検証2】Bhattacharyya 係数（クラス間の RD 分布の重なり）")
    lines.append("-" * 50)
    if overlap_df is not None and len(overlap_df) > 0:
        for _, row in overlap_df.iterrows():
            lines.append(f"  {row['class_a']} vs {row['class_b']}: "
                         f"BC={row['bhattacharyya_coef']:.3f}  {row['interpretation']}")
    else:
        lines.append("  データ不足")
    lines.append("")

    # ── 検証3: OHW 率 ──
    lines.append("【検証3】One-Hit-Wonder 率（サイズクラス別）")
    lines.append("-" * 50)
    if rd_stats is not None and len(rd_stats) > 0:
        for _, row in rd_stats.iterrows():
            lines.append(f"  {row['size_class_label']:<15}: "
                         f"OHW={row['ohw_frac']:.3%}  "
                         f"RD_median={row['rd_median']:.1f}  "
                         f"RD_cv={row['rd_cv']:.2f}")
    lines.append("")

    # ── 検証4: RD とサイズの相関 ──
    lines.append("【検証4】サイズ vs Reuse Distance の Spearman 相関")
    lines.append("-" * 50)
    rho = corr_result.get("size_rd_rho", np.nan)
    pval = corr_result.get("size_rd_pvalue", np.nan)
    if not np.isnan(rho):
        lines.append(f"  ρ = {rho:.4f}  (p = {pval:.4g})")
        lines.append(f"  解釈: " + (
            "強い負の相関 → 大きいオブジェクトほど再利用が少ない → 仮説2の強い根拠 ★" if rho < -0.3 else
            "弱い負の相関 → 傾向はあるが弱い" if rho < -0.1 else
            "相関なし → サイズと再利用頻度は独立"
        ))
    lines.append("")

    # ── 総合判定 ──
    lines.append("【総合判定】M2 が効く主因の候補")
    lines.append("-" * 50)
    if not np.isnan(eta) and eta < 0.06:
        lines.append("  ✗ 仮説「同サイズのreuse timeが似通う」は不成立")
        lines.append("    → 閾値パラメータと性能の相関が低い原因が説明できた")
        lines.append("")
    if not np.isnan(rho) and rho < -0.1:
        lines.append("  ★ 仮説2支持: サイズ-人気度の負の相関あり")
        lines.append("    → 大オブジェクトが小オブジェクトのキャッシュ枠を奪っている可能性")
    if rd_stats is not None and len(rd_stats) > 1:
        ohw_large = rd_stats[rd_stats["size_class"] == rd_stats["size_class"].max()]["ohw_frac"].values
        ohw_small = rd_stats[rd_stats["size_class"] == rd_stats["size_class"].min()]["ohw_frac"].values
        if len(ohw_large) > 0 and len(ohw_small) > 0 and ohw_large[0] > ohw_small[0] * 1.5:
            lines.append("  ★ 仮説1支持: 大クラスの OHW 率が高い")
            lines.append("    → 退避行列実験（eviction_matrix_sim.py）で退避干渉を定量化すること")

    lines.append("")
    lines.append("=" * 68)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  レポート保存: {out_path}")

    # コンソールにも出力
    print("\n" + "\n".join(lines))


# ─────────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────────

def run_single_trace(
    trace_path: str,
    thresholds: list,
    out_dir: str,
    max_requests: int = None,
) -> dict:
    trace_name = Path(trace_path).stem
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"Reuse Distance 分析: {trace_name}")
    print(f"{'='*60}")

    # 読み込み
    suffix = Path(trace_path).suffix.lower()
    if suffix in {".oraclegeneral", ".bin", ".lcs"} or (
        Path(trace_path).stat().st_size % OG_RECORD_SIZE == 0
    ):
        df = load_oracle_general(trace_path, max_requests)
    else:
        # フォールバック
        from eviction_matrix_sim import load_trace
        df = load_trace(trace_path, max_requests=max_requests)
        if "next_access_vtime" not in df.columns:
            print("  [エラー] next_access_vtime がありません。OracleGeneral形式を使用してください。")
            return {}

    # reuse distance 計算
    df = compute_reuse_distances(df)

    # 各検証を実行
    print("\n[検証1] η²（クラス内/クラス間分散比）...")
    eta_result = compute_eta_squared(df, thresholds)
    print(f"  η² = {eta_result['eta_squared']:.4f}")

    print("\n[検証2 & 3] サイズクラス別 RD 統計と OHW 率...")
    rd_stats = compute_class_rd_stats(df, thresholds)
    stats_path = os.path.join(out_dir, f"{trace_name}_rd_stats.csv")
    rd_stats.to_csv(stats_path, index=False, encoding="utf-8-sig")
    print(rd_stats[["size_class_label", "n_requests", "ohw_frac",
                     "rd_median", "rd_cv"]].to_string(index=False))

    print("\n[検証2] 分布の重なり（Bhattacharyya 係数）...")
    overlap_df = compute_distribution_overlap(df, thresholds)
    if overlap_df is not None and len(overlap_df) > 0:
        overlap_path = os.path.join(out_dir, f"{trace_name}_rd_overlap.csv")
        overlap_df.to_csv(overlap_path, index=False, encoding="utf-8-sig")
        print(overlap_df.to_string(index=False))

    print("\n[検証4] サイズ vs Reuse Distance 相関...")
    corr_result = compute_size_rd_correlation(df)
    print(f"  ρ = {corr_result['size_rd_rho']:.4f}  "
          f"(p = {corr_result['size_rd_pvalue']:.4g})")

    # 可視化
    print("\nグラフ生成...")
    plot_rd_distributions(
        df, thresholds, trace_name,
        os.path.join(out_dir, f"{trace_name}_rd_distribution.png")
    )

    # 仮説検定レポート
    write_hypothesis_report(
        trace_name, eta_result, rd_stats, overlap_df, corr_result,
        os.path.join(out_dir, f"{trace_name}_hypothesis_test.txt")
    )

    # η² の分散分解もCSVに
    variance_row = {"trace": trace_name, **eta_result, **corr_result}
    variance_df = pd.DataFrame([variance_row])
    var_path = os.path.join(out_dir, f"{trace_name}_rd_variance.csv")
    variance_df.to_csv(var_path, index=False, encoding="utf-8-sig")

    return {**eta_result, **corr_result, "trace": trace_name}


def main():
    parser = argparse.ArgumentParser(
        description="OracleGeneral トレースの Reuse Distance 分析"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace",     type=str, help="単一トレースファイル")
    group.add_argument("--trace-dir", type=str, help="トレースディレクトリ")

    parser.add_argument(
        "--thresholds", type=int, nargs="+",
        default=DEFAULT_THRESHOLDS,
        help="サイズクラス境界（バイト）"
    )
    parser.add_argument(
        "--out", type=str, default="./output/reuse_dist",
        help="出力ディレクトリ"
    )
    parser.add_argument(
        "--max-requests", type=int, default=None,
        help="読み込む最大リクエスト数（デバッグ用）"
    )

    args = parser.parse_args()

    if args.trace:
        trace_files = [args.trace]
    else:
        trace_dir = Path(args.trace_dir)
        trace_files = sorted(
            list(trace_dir.glob("*.oracleGeneral")) +
            list(trace_dir.glob("*.bin")) +
            list(trace_dir.glob("*.lcs")) +
            list(trace_dir.glob("*.csv"))
        )
        if not trace_files:
            print(f"エラー: {trace_dir} にトレースが見つかりません")
            sys.exit(1)

    all_results = []
    for tf in trace_files:
        result = run_single_trace(
            str(tf), args.thresholds, args.out, args.max_requests
        )
        if result:
            all_results.append(result)

    if len(all_results) > 1:
        summary_df = pd.DataFrame(all_results)
        summary_path = os.path.join(args.out, "ALL_TRACES_summary.csv")
        summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
        print(f"\n全トレース横断サマリー: {summary_path}")
        print(summary_df[["trace", "eta_squared", "size_rd_rho",
                           "kw_pvalue"]].to_string(index=False))


if __name__ == "__main__":
    main()
