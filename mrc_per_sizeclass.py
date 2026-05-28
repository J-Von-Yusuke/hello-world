"""
mrc_per_sizeclass.py
====================
サイズクラス別 Miss Ratio Curve (MRC) の生成と最適キャッシュ割り当て分析

【libCacheSim は使わない理由】
  OracleGeneral 形式には next_access_vtime フィールドがある。
  これは「次のアクセスの仮想時刻」であり、
    reuse_distance = next_access_vtime - current_vtime
  と定義されるため、MRC は reuse_distance の CDF の補関数として
  シミュレーションなしに直接計算できる。

  libCacheSim の MRC 生成も内部的に同じ原理を使っている。
  サイズクラス別 MRC を libCacheSim で生成しようとすると、
  クラスごとにトレースをフィルタして cachesim を呼ぶ迂回が必要になり
  複雑な割にメリットがない。

  libCacheSim が実際に役立つ場面:
    - LRU / FIFO / S3-FIFO / ARC などの比較（ベースライン実験）
    - 複数キャッシュサイズでのヒット率の高速バッチ計算

【binning】
  1KiB〜8GiB を 2 の指数乗で 25 bin（cache_common.POW2_THRESHOLDS）。
  --thresholds オプションで上書き可能。

【使い方】
  python mrc_per_sizeclass.py \
      --trace ./traces/cdn.oracleGeneral \
      --out ./output/mrc_analysis

  # 複数トレース一括
  python mrc_per_sizeclass.py \
      --trace-dir ./traces \
      --out ./output/mrc_analysis

【出力】
  {out_dir}/{trace_name}_mrc_class{i}.csv    - 各クラスの MRC データ
  {out_dir}/{trace_name}_mrc_all.png         - 全クラスの MRC 重ね描き
  {out_dir}/{trace_name}_allocation_opt.csv  - 最適割り当てと均等割り当ての比較
  {out_dir}/{trace_name}_knee_summary.csv    - 各クラスの膝点サマリー
"""

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cache_common import (
    POW2_THRESHOLDS, N_BINS,
    get_size_class_vectorized,
    build_class_labels,
    load_trace,
    setup_matplotlib_font,
)
setup_matplotlib_font()


# ─────────────────────────────────────────────
# MRC の計算（next_access_vtime ベース）
# ─────────────────────────────────────────────

def compute_mrc_from_reuse_dist(
    df: pd.DataFrame,
    n_points: int = 200,
) -> pd.DataFrame:
    """
    next_access_vtime から MRC を直接計算する。

    手順:
      1. reuse_dist = next_access_vtime - vtime  (グローバル仮想時刻差)
      2. next_access_vtime == -1 → reuse_dist = ∞ (再アクセスなし)
      3. MRC(C) = P(reuse_dist > C) をキャッシュサイズ C の関数として計算

    キャッシュサイズの単位:
      - バイトベース: reuse_dist × 平均オブジェクトサイズ で近似

    注意: df["vtime"] はグローバルな仮想時刻（load_trace が付与）を使う。
          per-class フィルタ後も vtime を上書きしないこと。
    """
    if "next_access_vtime" not in df.columns or df["next_access_vtime"].isna().all():
        return None

    # グローバル vtime を使う（クラスフィルタ後も上書きしない）
    vtime  = df["vtime"].values.astype(np.int64)
    next_vt = df["next_access_vtime"].values.astype(np.int64)
    sizes  = df["obj_size"].values.astype(np.int64)

    # reuse distance（仮想時刻単位）
    rd_vtime = next_vt - vtime
    rd_vtime[next_vt == -1] = np.iinfo(np.int64).max  # ∞

    # バイト単位への変換: rd_vtime × 平均オブジェクトサイズ
    mean_size = float(sizes.mean())
    rd_bytes = rd_vtime.astype(float) * mean_size
    rd_bytes[next_vt == -1] = np.inf

    # MRC のキャッシュサイズ軸: 有限 reuse distance の 1〜99 パーセンタイル
    finite_rd = rd_bytes[np.isfinite(rd_bytes)]
    if len(finite_rd) == 0:
        return None

    cs_min = float(np.percentile(finite_rd, 1))
    cs_max = float(np.percentile(finite_rd, 99))
    if cs_min <= 0 or cs_max <= cs_min:
        return None
    cache_sizes = np.geomspace(max(cs_min, 1), cs_max, n_points)

    # miss_ratio(C) = P(reuse_dist > C)
    #              = (∞ の割合) + P(finite_rd > C) × (有限の割合)
    n_total = len(rd_bytes)
    n_inf   = int(np.sum(~np.isfinite(rd_bytes)))
    inf_frac = n_inf / n_total if n_total > 0 else 0.0
    finite_frac = 1.0 - inf_frac

    rows = []
    for cs in cache_sizes:
        miss_ratio = inf_frac + float(np.mean(finite_rd > cs)) * finite_frac
        rows.append({"cache_size_bytes": cs, "miss_ratio": miss_ratio})

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# MRC の「膝」検出（Kneedle アルゴリズム）
# ─────────────────────────────────────────────

def find_knee_point(mrc_df: pd.DataFrame) -> float:
    """
    MRC の膝点をピタゴラス（最大垂直距離）法で検出する。
    Returns: 膝点に対応するキャッシュサイズ（バイト）
    """
    x = mrc_df["cache_size_bytes"].values.astype(float)
    y = mrc_df["miss_ratio"].values.astype(float)
    if len(x) < 3:
        return float(x[0]) if len(x) > 0 else 0.0

    x_n = (x - x.min()) / (x.max() - x.min() + 1e-12)
    y_n = (y - y.min()) / (y.max() - y.min() + 1e-12)

    start, end = np.array([x_n[0], y_n[0]]), np.array([x_n[-1], y_n[-1]])
    line     = end - start
    line_len = np.linalg.norm(line) + 1e-12

    dists = []
    for xi, yi in zip(x_n, y_n):
        pt       = np.array([xi, yi])
        proj_len = np.dot(pt - start, line) / line_len
        proj_pt  = start + proj_len * line / line_len
        dists.append(np.linalg.norm(pt - proj_pt))

    return float(x[int(np.argmax(dists))])


# ─────────────────────────────────────────────
# 最適割り当て分析
# ─────────────────────────────────────────────

def analyze_optimal_allocation(
    class_mrcs: dict,
    class_labels: dict,
    wss_bytes: int,
    capacity_fracs: list,
) -> pd.DataFrame:
    """
    総キャッシュ容量 C に対して2種類の割り当てを比較する:
      1. 均等割り当て: C / n_classes ずつ
      2. 膝点比例: 各クラスの MRC 膝点に比例（理論最適に近い）
    """
    n_classes  = len(class_mrcs)
    knee_bytes = {ci: find_knee_point(mrc) for ci, mrc in class_mrcs.items()}
    total_knee = sum(knee_bytes.values()) + 1e-10

    def interp_miss_ratio(mrc_df, cap):
        if cap <= 0 or mrc_df is None or len(mrc_df) == 0:
            return 1.0
        x = mrc_df["cache_size_bytes"].values
        y = mrc_df["miss_ratio"].values
        if cap >= x.max():
            return float(y[-1])
        return float(np.interp(cap, x, y))

    rows = []
    for frac in capacity_fracs:
        total_cap = int(wss_bytes * frac)

        alloc_equal = {ci: total_cap // n_classes          for ci in class_mrcs}
        alloc_knee  = {ci: int(total_cap * knee_bytes[ci] / total_knee)
                       for ci in class_mrcs}

        for alloc_name, alloc in [("equal", alloc_equal), ("knee_based", alloc_knee)]:
            for ci, mrc_df in class_mrcs.items():
                mr = interp_miss_ratio(mrc_df, alloc[ci])
                rows.append({
                    "cache_size_frac":  frac,
                    "total_cap_bytes":  total_cap,
                    "allocation_type":  alloc_name,
                    "size_class":       ci,
                    "size_class_label": class_labels.get(ci, str(ci)),
                    "allocated_bytes":  alloc[ci],
                    "knee_bytes":       knee_bytes[ci],
                    "miss_ratio":       mr,
                })

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# 可視化
# ─────────────────────────────────────────────

def plot_mrc_per_class(
    class_mrcs: dict,
    class_labels: dict,
    trace_name: str,
    wss_bytes: int,
    out_path: str,
):
    n_classes = len(class_mrcs)
    colors = plt.cm.tab10(np.linspace(0, 1, max(n_classes, 2)))

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle(
        f"{trace_name}  |  サイズクラス別 MRC（next_access_vtime から直接計算）",
        fontsize=11
    )

    # (A) 絶対キャッシュサイズ（バイト）
    ax = axes[0]
    for i, (ci, mrc_df) in enumerate(sorted(class_mrcs.items())):
        lbl  = class_labels.get(ci, str(ci))
        knee = find_knee_point(mrc_df)
        ax.plot(mrc_df["cache_size_bytes"] / 1e6, mrc_df["miss_ratio"],
                color=colors[i], linewidth=1.8, label=lbl)
        ax.axvline(knee / 1e6, color=colors[i], linestyle="--",
                   linewidth=0.8, alpha=0.7)
    ax.set_xlabel("キャッシュサイズ（MB）")
    ax.set_ylabel("ミス率")
    ax.set_title("(A) MRC（絶対サイズ）\n破線=膝点")
    ax.legend(fontsize=7, ncol=max(1, n_classes // 8))
    ax.grid(True, alpha=0.3)

    # (B) クラス WSS で正規化（膝点の位置の違いが見やすい）
    ax = axes[1]
    for i, (ci, mrc_df) in enumerate(sorted(class_mrcs.items())):
        lbl       = class_labels.get(ci, str(ci))
        class_wss = float(mrc_df["cache_size_bytes"].max())
        if class_wss > 0:
            x_norm = mrc_df["cache_size_bytes"] / class_wss
            ax.plot(x_norm, mrc_df["miss_ratio"],
                    color=colors[i], linewidth=1.8, label=lbl)
    ax.set_xlabel("キャッシュサイズ / クラス内 WSS")
    ax.set_ylabel("ミス率")
    ax.set_title("(B) MRC（クラス WSS 正規化）\n"
                 "膝点が右に寄るクラス→多くのキャッシュが必要")
    ax.legend(fontsize=7, ncol=max(1, n_classes // 8))
    ax.grid(True, alpha=0.3)

    # (C) 膝点比較棒グラフ
    ax = axes[2]
    classes   = sorted(class_mrcs.keys())
    knee_vals = [find_knee_point(class_mrcs[ci]) / 1e6 for ci in classes]
    lbls      = [class_labels.get(ci, str(ci)) for ci in classes]
    bars = ax.bar(range(len(classes)), knee_vals,
                  color=[colors[i % 10] for i in range(len(classes))], alpha=0.8)
    ax.set_xticks(range(len(classes)))
    ax.set_xticklabels(lbls, rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("膝点（MB）")
    ax.set_title("(C) クラス別 MRC 膝点\n"
                 "膝点が大きく異なる → 統合キャッシュでは\n"
                 "どのクラスも最適化できない（仮説3の根拠）")
    ax.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, knee_vals):
        ax.text(bar.get_x() + bar.get_width() / 2, val,
                f"{val:.1f}M", ha="center", va="bottom", fontsize=7)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


def plot_allocation_comparison(
    alloc_df: pd.DataFrame,
    trace_name: str,
    out_path: str,
):
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle(f"{trace_name}  |  割り当て戦略の比較", fontsize=11)

    colors = {"equal": "#DC2626", "knee_based": "#1976D2"}
    labels = {"equal": "均等割り当て", "knee_based": "膝点比例割り当て（理論最適近似）"}

    # 左: 総合ミス率
    ax = axes[0]
    for atype in ["equal", "knee_based"]:
        sub = alloc_df[alloc_df["allocation_type"] == atype]
        agg = sub.groupby("cache_size_frac")["miss_ratio"].mean().reset_index()
        ax.plot(agg["cache_size_frac"] * 100, agg["miss_ratio"],
                "o-", color=colors[atype], linewidth=1.8, label=labels[atype])
    ax.set_xlabel("総キャッシュ容量（% of WSS）")
    ax.set_ylabel("平均ミス率（クラス間平均）")
    ax.set_title("割り当て戦略別ミス率")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # 右: クラス別割り当てバイト数の比較（代表的なキャッシュサイズで）
    ax = axes[1]
    mid_idx = len(alloc_df["cache_size_frac"].unique()) // 2
    mid_frac = alloc_df["cache_size_frac"].unique()[mid_idx]
    rep     = alloc_df[alloc_df["cache_size_frac"] == mid_frac]
    classes = sorted(rep["size_class"].unique())
    x       = np.arange(len(classes))
    width   = 0.35
    for k, atype in enumerate(["equal", "knee_based"]):
        sub  = rep[rep["allocation_type"] == atype].sort_values("size_class")
        vals = sub["allocated_bytes"].values / 1e6
        ax.bar(x + k * width, vals, width,
               color=colors[atype], alpha=0.8, label=labels[atype])
    class_labels_list = [
        rep[rep["size_class"] == ci]["size_class_label"].values[0]
        for ci in classes
    ]
    ax.set_xticks(x + width / 2)
    ax.set_xticklabels(class_labels_list, rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("割り当てバイト数（MB）")
    ax.set_title(f"クラス別割り当て比較\n（総容量 {mid_frac:.0%} × WSS）")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


# ─────────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────────

def run_mrc_analysis(
    trace_path: str,
    thresholds: list = None,
    out_dir: str = "./output/mrc_analysis",
    capacity_fracs: list = None,
    max_requests: int = None,
    n_mrc_points: int = 200,
    sample_stride: int = 1,
):
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    if capacity_fracs is None:
        capacity_fracs = [0.05, 0.1, 0.2, 0.3, 0.5]

    trace_name = Path(trace_path).stem
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"MRC 分析: {trace_name}")
    print(f"{'='*60}")
    if sample_stride > 1:
        print(f"  サンプリング: 1/{sample_stride}（MRC 形状はほぼ保たれるがキャッシュサイズ軸が縮小）")

    # load_trace が vtime / size_class を自動付与する
    df = load_trace(trace_path, max_requests, sample_stride=sample_stride)

    if "next_access_vtime" not in df.columns or (df["next_access_vtime"] == -2).all():
        print("  [エラー] next_access_vtime がありません。OracleGeneral 形式を使用してください。")
        return None, None

    # size_class が未付与の場合は付与（CSV フォールバック対応）
    if "size_class" not in df.columns:
        from cache_common import get_size_class_vectorized
        df["size_class"] = get_size_class_vectorized(df["obj_size"].values, thresholds)

    class_labels = build_class_labels(thresholds)

    # WSS（ユニークオブジェクトの総バイト数）
    wss_bytes = int(df.groupby("obj_id")["obj_size"].first().sum())
    print(f"  総 WSS: {wss_bytes / 1e9:.3f} GB")

    # ── クラス別 MRC 生成 ──
    print("\nMRC 生成（next_access_vtime ベース）...")
    class_mrcs = {}
    knee_rows  = []

    for ci in sorted(df["size_class"].unique()):
        # per-class フィルタ: vtime はグローバル値のままにする
        sub  = df[df["size_class"] == ci]
        lbl  = class_labels.get(ci, str(ci))
        class_wss = int(sub.groupby("obj_id")["obj_size"].first().sum())
        print(f"  {lbl}: {len(sub):,} リクエスト  WSS={class_wss/1e6:.1f} MB")

        mrc_df = compute_mrc_from_reuse_dist(sub, n_points=n_mrc_points)
        if mrc_df is None or len(mrc_df) == 0:
            print(f"    → スキップ（データ不足）")
            continue

        class_mrcs[ci] = mrc_df
        knee     = find_knee_point(mrc_df)
        knee_pct = knee / class_wss * 100 if class_wss > 0 else 0.0

        mrc_path = os.path.join(out_dir, f"{trace_name}_mrc_class{ci}.csv")
        mrc_df.to_csv(mrc_path, index=False, encoding="utf-8-sig")

        knee_rows.append({
            "size_class":       ci,
            "size_class_label": lbl,
            "n_requests":       len(sub),
            "class_wss_bytes":  class_wss,
            "knee_bytes":       knee,
            "knee_pct_of_wss":  knee_pct,
        })
        print(f"    膝点: {knee/1e6:.2f} MB ({knee_pct:.1f}% of class WSS)")

    if not class_mrcs:
        print("  [エラー] MRC が生成できませんでした")
        return None, None

    # サマリー保存
    knee_df  = pd.DataFrame(knee_rows)
    knee_path = os.path.join(out_dir, f"{trace_name}_knee_summary.csv")
    knee_df.to_csv(knee_path, index=False, encoding="utf-8-sig")

    # ── 可視化 ──
    plot_mrc_per_class(
        class_mrcs, class_labels, trace_name, wss_bytes,
        os.path.join(out_dir, f"{trace_name}_mrc_all.png")
    )

    # ── 最適割り当て分析 ──
    print("\n最適割り当て分析...")
    alloc_df  = analyze_optimal_allocation(
        class_mrcs, class_labels, wss_bytes, capacity_fracs
    )
    alloc_path = os.path.join(out_dir, f"{trace_name}_allocation_opt.csv")
    alloc_df.to_csv(alloc_path, index=False, encoding="utf-8-sig")

    plot_allocation_comparison(
        alloc_df, trace_name,
        os.path.join(out_dir, f"{trace_name}_allocation_comparison.png")
    )

    # ── 膝点の分散チェック（仮説3の根拠） ──
    if len(knee_rows) >= 2:
        knees = knee_df["knee_pct_of_wss"].values
        mean_k = knees.mean()
        if mean_k > 0:
            cv = knees.std() / mean_k
            print(f"\n  膝点の変動係数 CV = {cv:.3f}")
            print(f"  （CV が大きい → クラス間でワーキングセット需要が不均一 → 仮説3の根拠）")

    return class_mrcs, alloc_df


def main():
    parser = argparse.ArgumentParser(
        description="サイズクラス別 MRC 生成（next_access_vtime ベース）"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace",     type=str, help="単一トレースファイル")
    group.add_argument("--trace-dir", type=str, help="トレースディレクトリ")

    parser.add_argument(
        "--thresholds", type=int, nargs="+",
        default=None,
        help="サイズクラス境界（バイト）。省略時は 1KiB〜8GiB の 2 の指数乗（25 bin）"
    )
    parser.add_argument("--out", type=str, default="./output/mrc_analysis")
    parser.add_argument(
        "--capacity-fracs", type=float, nargs="+",
        default=[0.05, 0.1, 0.2, 0.3, 0.5]
    )
    parser.add_argument("--n-mrc-points", type=int, default=200)
    parser.add_argument("--max-requests",  type=int, default=None, metavar="M",
                        help="先頭 M 件だけ読み込む（推奨: 5_000_000〜10_000_000）")
    parser.add_argument("--sample-stride", type=int, default=1, metavar="N",
                        help="N 件に 1 件だけ読み込む（MRC 形状はほぼ保たれる）")

    args = parser.parse_args()

    if args.trace:
        trace_files = [args.trace]
    else:
        td = Path(args.trace_dir)
        trace_files = sorted(
            list(td.glob("*.oracleGeneral")) +
            list(td.glob("*.oracleGeneral.zst")) +
            list(td.glob("*.oracleGeneral.bin.zst")) +
            list(td.glob("*.bin")) +
            list(td.glob("*.lcs")) +
            list(td.glob("*.csv"))
        )
        if not trace_files:
            print(f"エラー: {td} にトレースが見つかりません")
            sys.exit(1)

    for tf in trace_files:
        run_mrc_analysis(
            str(tf),
            thresholds=args.thresholds,
            out_dir=args.out,
            capacity_fracs=args.capacity_fracs,
            max_requests=args.max_requests,
            n_mrc_points=args.n_mrc_points,
            sample_stride=args.sample_stride,
        )


if __name__ == "__main__":
    main()
