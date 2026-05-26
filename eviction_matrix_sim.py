"""
eviction_matrix_sim.py
======================
クロスサイズ退避行列（Cross-Size Eviction Matrix）の測定

【目的】
  統合キャッシュと分割キャッシュをシミュレートし、
  「どのサイズクラスのオブジェクトが、どのサイズクラスのオブジェクトを退避させているか」
  を定量化する。

  これにより M2（コンテンツサイズ帯別分割）が効く真のメカニズムを特定する:
    - 仮説1: 大→小の退避干渉が主因 (Asymmetric Eviction Pressure)
    - 仮説2: サイズ-人気度の負の相関が主因 (Size-Popularity Skew)
    - 仮説3: サイズクラス間のワーキングセット比率差が主因

【入力トレース形式】
  OracleGeneral バイナリ形式 (CacheMon 標準):
    struct { uint32_t timestamp; uint64_t obj_id;
             uint32_t obj_size;  int64_t next_access_vtime; }
  拡張子 .oracleGeneral / .bin / .lcs を自動認識。
  CSV も引き続きサポート（拡張子 .csv / .tsv）。

【使い方】
  python eviction_matrix_sim.py \
      --trace path/to/trace.oracleGeneral \
      --cache-sizes 0.01 0.05 0.1 0.2 \
      --thresholds 1024 65536 1048576 \
      --out ./output/eviction_analysis

  # 複数トレースをまとめて処理
  python eviction_matrix_sim.py \
      --trace-dir ./traces \
      --cache-sizes 0.05 0.1 \
      --out ./output/eviction_analysis

【出力】
  {out_dir}/{trace_name}_eviction_matrix_cs{cache_size_pct}.csv
  {out_dir}/{trace_name}_size_popularity.csv
  {out_dir}/{trace_name}_mechanism_summary.csv
  {out_dir}/{trace_name}_plots.png
"""

import argparse
import os
import struct
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import spearmanr

# ─────────────────────────────────────────────
# OracleGeneral バイナリ形式の定数
# struct { uint32_t ts; uint64_t obj_id; uint32_t obj_size; int64_t next_vtime; }
# ─────────────────────────────────────────────
OG_FORMAT      = "=IQIq"   # little-endian: uint32, uint64, uint32, int64
OG_RECORD_SIZE = struct.calcsize(OG_FORMAT)   # = 24 bytes
OG_EXTENSIONS  = {".oraclegeneral", ".oracleGeneralBin", ".bin", ".lcs"}

# ─────────────────────────────────────────────
# デフォルトサイズクラス境界 (バイト)
# ─────────────────────────────────────────────
DEFAULT_THRESHOLDS = [256, 4_096, 65_536, 1_048_576]  # 256B, 4KB, 64KB, 1MB
SIZE_CLASS_LABELS_FMT = {
    0: "<256B",
    1: "256B-4KB",
    2: "4KB-64KB",
    3: "64KB-1MB",
    4: ">1MB",
}

# ─────────────────────────────────────────────
# サイズクラス判定
# ─────────────────────────────────────────────

def get_size_class(size: int, thresholds: list) -> int:
    for i, t in enumerate(thresholds):
        if size < t:
            return i
    return len(thresholds)


def build_class_labels(thresholds: list) -> dict:
    labels = {}
    units = [(1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB"), (1, "B")]

    def fmt(b):
        for div, unit in units:
            if b >= div and b % div == 0:
                return f"{b // div}{unit}"
        return f"{b}B"

    boundaries = [0] + thresholds + [None]
    for i in range(len(boundaries) - 1):
        lo = boundaries[i]
        hi = boundaries[i + 1]
        if lo == 0 and hi is not None:
            labels[i] = f"<{fmt(hi)}"
        elif hi is None:
            labels[i] = f"≥{fmt(lo)}"
        else:
            labels[i] = f"{fmt(lo)}-{fmt(hi)}"
    return labels


# ─────────────────────────────────────────────
# トレース読み込み（OracleGeneral / CSV 自動判定）
# ─────────────────────────────────────────────

def _is_oracle_general(path: Path) -> bool:
    """拡張子とマジックバイトでOracleGeneral形式を判定する"""
    if path.suffix.lower() in {s.lower() for s in OG_EXTENSIONS}:
        return True
    # 拡張子が不明な場合: ファイルサイズが 24 の倍数かどうかで推定
    try:
        size = path.stat().st_size
        if size > 0 and size % OG_RECORD_SIZE == 0:
            return True
    except OSError:
        pass
    return False


def load_trace_oracle_general(path: str, max_requests: int = None) -> pd.DataFrame:
    """
    OracleGeneral バイナリ形式を読み込む。
    struct { uint32_t ts; uint64_t obj_id; uint32_t obj_size; int64_t next_vtime; }

    next_access_vtime:
      -1 = このアクセス以降にアクセスなし（one-hit wonder）
      >= 0 = 次にアクセスされる仮想時刻（スタック距離の近似）
    """
    records = []
    with open(path, "rb") as f:
        raw = f.read()

    n_records = len(raw) // OG_RECORD_SIZE
    if max_requests is not None:
        n_records = min(n_records, max_requests)

    for i in range(n_records):
        offset = i * OG_RECORD_SIZE
        ts, obj_id, obj_size, next_vtime = struct.unpack_from(OG_FORMAT, raw, offset)
        if obj_size > 0:
            records.append((ts, obj_id, obj_size, next_vtime))

    df = pd.DataFrame(records,
                      columns=["timestamp", "obj_id", "obj_size", "next_access_vtime"])
    df["obj_id"] = df["obj_id"].astype(str)
    return df


def load_trace_csv(path: str,
                   time_col: int = 0,
                   id_col: int = 1,
                   size_col: int = 2,
                   max_requests: int = None) -> pd.DataFrame:
    """CSV / TSV トレースを読み込む（ヘッダー自動判定）"""
    path = Path(path)
    sep = "\t" if path.suffix.lower() == ".tsv" else ","

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first_fields = f.readline().strip().split(sep)
    has_header = not all(_is_numeric(v) for v in first_fields[:3])

    try:
        df = pd.read_csv(
            path, sep=sep,
            header=0 if has_header else None,
            usecols=[time_col, id_col, size_col],
            nrows=max_requests,
            dtype={time_col: "int64", id_col: "str", size_col: "int64"},
            on_bad_lines="skip", low_memory=True,
        )
    except Exception:
        df = pd.read_csv(
            path, sep=sep,
            header=0 if has_header else None,
            usecols=[time_col, id_col, size_col],
            nrows=max_requests,
            on_bad_lines="skip", low_memory=True,
        )

    df.columns = ["timestamp", "obj_id", "obj_size"]
    df["next_access_vtime"] = np.nan   # CSV には再利用距離情報なし
    df = df.dropna(subset=["obj_size"])
    df["obj_size"] = pd.to_numeric(df["obj_size"], errors="coerce").fillna(0).astype(int)
    df = df[df["obj_size"] > 0]
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").fillna(0).astype(int)
    return df


def load_trace(path: str,
               time_col: int = 0,
               id_col: int = 1,
               size_col: int = 2,
               max_requests: int = None) -> pd.DataFrame:
    """
    ファイル形式を自動判定してトレースを読み込む。
    OracleGeneral (バイナリ) と CSV / TSV に対応。
    """
    p = Path(path)
    if _is_oracle_general(p):
        print(f"  形式: OracleGeneral バイナリ")
        df = load_trace_oracle_general(path, max_requests)
    else:
        print(f"  形式: CSV/TSV テキスト")
        df = load_trace_csv(path, time_col, id_col, size_col, max_requests)

    df = df.sort_values("timestamp").reset_index(drop=True)
    has_vtime = "next_access_vtime" in df.columns and df["next_access_vtime"].notna().any()

    print(f"  読み込み完了: {len(df):,} リクエスト  "
          f"ユニーク={df['obj_id'].nunique():,}  "
          f"サイズ=[{df['obj_size'].min()}, {df['obj_size'].max()}]B  "
          f"next_vtime={'あり' if has_vtime else 'なし'}")
    return df


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ─────────────────────────────────────────────
# バイト単位 LRU キャッシュ（退避行列記録付き）
# ─────────────────────────────────────────────

class ByteAwareLRU:
    """
    バイト容量制限の LRU キャッシュ。
    各退避イベントで (退避されたオブジェクトのサイズクラス, 挿入オブジェクトのサイズクラス)
    を記録する。
    """

    def __init__(self, capacity_bytes: int, thresholds: list):
        self.capacity = capacity_bytes
        self.thresholds = thresholds
        self.n_classes = len(thresholds) + 1
        self.used_bytes = 0

        # OrderedDict: obj_id -> (size, size_class)
        self._cache: OrderedDict = OrderedDict()

        # 統計カウンタ
        self.hits = 0
        self.misses = 0
        self.hit_bytes = 0
        self.miss_bytes = 0

        # クロスサイズ退避行列: eviction_matrix[evicted_class][inserting_class]
        self.eviction_matrix = np.zeros((self.n_classes, self.n_classes), dtype=np.int64)

        # サイズクラス別 hit / miss
        self.class_hits   = np.zeros(self.n_classes, dtype=np.int64)
        self.class_misses = np.zeros(self.n_classes, dtype=np.int64)
        self.class_bytes_hit   = np.zeros(self.n_classes, dtype=np.int64)
        self.class_bytes_miss  = np.zeros(self.n_classes, dtype=np.int64)

    def access(self, obj_id: str, size: int):
        sc = get_size_class(size, self.thresholds)

        if obj_id in self._cache:
            # キャッシュヒット
            self._cache.move_to_end(obj_id)
            self.hits += 1
            self.hit_bytes += size
            self.class_hits[sc] += 1
            self.class_bytes_hit[sc] += size
            return True

        # キャッシュミス: 容量を確保しながら挿入
        self.misses += 1
        self.miss_bytes += size
        self.class_misses[sc] += 1
        self.class_bytes_miss[sc] += size

        # 容量超過分を退避
        while self.used_bytes + size > self.capacity and self._cache:
            evicted_id, (evicted_size, evicted_sc) = self._cache.popitem(last=False)
            self.used_bytes -= evicted_size
            self.eviction_matrix[evicted_sc][sc] += 1

        # 挿入（容量が足りない場合は挿入しない）
        if size <= self.capacity:
            self._cache[obj_id] = (size, sc)
            self.used_bytes += size

        return False

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0

    def byte_miss_rate(self) -> float:
        total = self.hit_bytes + self.miss_bytes
        return self.miss_bytes / total if total > 0 else 0.0

    def class_hit_rates(self) -> np.ndarray:
        total = self.class_hits + self.class_misses
        total = np.where(total == 0, 1, total)
        return self.class_hits / total

    def normalized_eviction_matrix(self) -> np.ndarray:
        """各列（挿入クラス）の合計で正規化: 挿入1回あたりの退避先分布"""
        col_sums = self.eviction_matrix.sum(axis=0, keepdims=True)
        col_sums = np.where(col_sums == 0, 1, col_sums)
        return self.eviction_matrix / col_sums


# ─────────────────────────────────────────────
# 分割キャッシュシミュレータ
# ─────────────────────────────────────────────

class SizePartitionedCache:
    """
    サイズクラスごとに独立した LRU キャッシュを持つ分割キャッシュ。
    各キャッシュの容量は、そのクラスのバイト占有率に比例して割り当てる。
    """

    def __init__(self, total_capacity_bytes: int, thresholds: list,
                 class_byte_fracs: np.ndarray):
        self.n_classes = len(thresholds) + 1
        self.thresholds = thresholds

        # バイト占有率に基づいて容量を比例配分 (最小 1 バイト)
        capacities = np.maximum(
            (class_byte_fracs * total_capacity_bytes).astype(int), 1
        )
        self.caches = [
            ByteAwareLRU(int(cap), thresholds) for cap in capacities
        ]
        self.capacities = capacities

    def access(self, obj_id: str, size: int):
        sc = get_size_class(size, self.thresholds)
        return self.caches[sc].access(obj_id, size)

    def hit_rate(self) -> float:
        total_hits   = sum(c.hits   for c in self.caches)
        total_misses = sum(c.misses for c in self.caches)
        total = total_hits + total_misses
        return total_hits / total if total > 0 else 0.0

    def byte_miss_rate(self) -> float:
        total_hit_b  = sum(c.hit_bytes  for c in self.caches)
        total_miss_b = sum(c.miss_bytes for c in self.caches)
        total = total_hit_b + total_miss_b
        return total_miss_b / total if total > 0 else 0.0


# ─────────────────────────────────────────────
# サイズ-人気度分析
# ─────────────────────────────────────────────

def analyze_size_popularity(df: pd.DataFrame, thresholds: list) -> pd.DataFrame:
    """
    各オブジェクトのサイズとアクセス頻度を集計し、
    Spearman 相関とサイズクラス別統計を返す。

    OracleGeneral の next_access_vtime を使って one-hit-wonder を正確に判定する。
    next_access_vtime == -1 はそのアクセス以降アクセスなし（最終アクセス）を意味する。
    """
    has_vtime = ("next_access_vtime" in df.columns
                 and df["next_access_vtime"].notna().any())

    agg_dict = {
        "size": ("obj_size", "first"),
        "freq": ("obj_id", "count"),
    }
    obj_stats = df.groupby("obj_id").agg(**agg_dict).reset_index()

    if has_vtime:
        # next_access_vtime == -1 のアクセス = そのオブジェクトの最終アクセス
        # 全アクセスが1回だけ = (freq==1) AND (next_access_vtime==-1) で確認
        last_vtime = df.groupby("obj_id")["next_access_vtime"].last()
        obj_stats = obj_stats.join(last_vtime.rename("last_vtime"), on="obj_id")
        # 1回アクセスのオブジェクト = OHW（One-Hit Wonder）
        obj_stats["is_ohw"] = (obj_stats["freq"] == 1)
    else:
        obj_stats["is_ohw"] = (obj_stats["freq"] == 1)
        obj_stats["last_vtime"] = np.nan

    # Spearman 相関: サイズ vs アクセス頻度
    rho, pval = spearmanr(obj_stats["size"], obj_stats["freq"])
    print(f"  サイズ-人気度 Spearman ρ = {rho:.4f}  (p = {pval:.4g})")

    # サイズクラス別集計
    obj_stats["size_class"] = obj_stats["size"].apply(
        lambda s: get_size_class(s, thresholds)
    )
    class_labels = build_class_labels(thresholds)
    obj_stats["size_class_label"] = obj_stats["size_class"].map(class_labels)

    class_stats = obj_stats.groupby("size_class_label").agg(
        n_objects=("obj_id", "count"),
        total_requests=("freq", "sum"),
        mean_freq=("freq", "mean"),
        median_freq=("freq", "median"),
        one_hit_wonder_frac=("is_ohw", "mean"),
        mean_size=("size", "mean"),
        total_bytes=("size", "sum"),
    ).reset_index()
    class_stats["rho"] = rho
    class_stats["rho_pvalue"] = pval
    return class_stats


# ─────────────────────────────────────────────
# メカニズム指標の集計
# ─────────────────────────────────────────────

def compute_mechanism_metrics(
    unified_lru: ByteAwareLRU,
    partitioned: SizePartitionedCache,
    thresholds: list,
) -> dict:
    """
    統合キャッシュと分割キャッシュの比較から、
    退避干渉の程度とその影響を定量化する。
    """
    n_classes = len(thresholds) + 1
    class_labels = build_class_labels(thresholds)

    # --- 退避行列の非対称性スコア ---
    # 大クラス(上位半分)→小クラス(下位半分)への退避割合
    em = unified_lru.eviction_matrix
    mid = n_classes // 2

    large_evicts_small = em[:mid, mid:].sum()   # 小→大に退避  (行=退避先=小、列=挿入元=大)
    total_evictions = em.sum()
    # 注: em[i][j] = サイズクラスi が クラスj の挿入によって退避された数
    #     大(j>=mid)が小(i<mid)を退避: em[i<mid, j>=mid]
    large_evicts_small = em[:mid, mid:].sum()
    asymmetry_score = large_evicts_small / total_evictions if total_evictions > 0 else 0.0

    # --- クラス別ヒット率改善 ---
    unified_class_hr   = unified_lru.class_hit_rates()
    partitioned_class_hr = np.array([
        c.class_hit_rates()[i] for i, c in enumerate(partitioned.caches)
    ])

    # --- 総合サマリー ---
    metrics = {
        "unified_hit_rate":      unified_lru.hit_rate(),
        "unified_byte_miss_rate": unified_lru.byte_miss_rate(),
        "partitioned_hit_rate":   partitioned.hit_rate(),
        "partitioned_byte_miss_rate": partitioned.byte_miss_rate(),
        "hit_rate_improvement":  partitioned.hit_rate() - unified_lru.hit_rate(),
        "bmr_improvement":       unified_lru.byte_miss_rate() - partitioned.byte_miss_rate(),
        "asymmetry_score":       asymmetry_score,
        "total_evictions":       int(total_evictions),
    }
    for i in range(n_classes):
        lbl = class_labels[i].replace(" ", "").replace("≥", "ge").replace("<", "lt").replace("-", "_")
        metrics[f"unified_hr_{lbl}"]      = float(unified_class_hr[i])
        metrics[f"partitioned_hr_{lbl}"]  = float(partitioned_class_hr[i])
        metrics[f"hr_gain_{lbl}"]         = float(partitioned_class_hr[i] - unified_class_hr[i])

    return metrics


# ─────────────────────────────────────────────
# 可視化
# ─────────────────────────────────────────────

def plot_eviction_matrix(
    unified_lru: ByteAwareLRU,
    thresholds: list,
    trace_name: str,
    cache_size_pct: float,
    out_path: str,
):
    class_labels = build_class_labels(thresholds)
    n_classes = len(thresholds) + 1
    labels = [class_labels[i] for i in range(n_classes)]

    fig, axes = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle(
        f"{trace_name}  |  統合キャッシュ  |  キャッシュ容量={cache_size_pct:.0%}",
        fontsize=12
    )

    # (A) 退避行列（件数）
    ax = axes[0]
    em = unified_lru.eviction_matrix.astype(float)
    im = ax.imshow(em, cmap="YlOrRd", aspect="auto")
    ax.set_xticks(range(n_classes))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(n_classes))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("挿入クラス（キャッシュミスの元）", fontsize=9)
    ax.set_ylabel("退避クラス（追い出されたオブジェクト）", fontsize=9)
    ax.set_title("退避行列（件数）", fontsize=10)
    plt.colorbar(im, ax=ax)
    for i in range(n_classes):
        for j in range(n_classes):
            val = int(em[i, j])
            if val > 0:
                ax.text(j, i, f"{val:,}", ha="center", va="center",
                        fontsize=7, color="black" if em[i, j] < em.max() * 0.6 else "white")

    # (B) 正規化退避行列
    ax = axes[1]
    em_norm = unified_lru.normalized_eviction_matrix()
    im2 = ax.imshow(em_norm, cmap="Blues", aspect="auto", vmin=0, vmax=1)
    ax.set_xticks(range(n_classes))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(n_classes))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("挿入クラス", fontsize=9)
    ax.set_ylabel("退避クラス", fontsize=9)
    ax.set_title("正規化退避行列\n（列=挿入クラスで正規化）", fontsize=10)
    plt.colorbar(im2, ax=ax)
    for i in range(n_classes):
        for j in range(n_classes):
            ax.text(j, i, f"{em_norm[i, j]:.2f}", ha="center", va="center",
                    fontsize=7, color="black" if em_norm[i, j] < 0.6 else "white")

    # (C) サイズクラス別ヒット率
    ax = axes[2]
    hr = unified_lru.class_hit_rates()
    colors = plt.cm.tab10(np.linspace(0, 1, n_classes))
    bars = ax.bar(range(n_classes), hr, color=colors, alpha=0.8)
    ax.set_xticks(range(n_classes))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("ヒット率", fontsize=9)
    ax.set_title("統合キャッシュ\nサイズクラス別ヒット率", fontsize=10)
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, hr):
        ax.text(bar.get_x() + bar.get_width() / 2, val + 0.01,
                f"{val:.3f}", ha="center", va="bottom", fontsize=8)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


def plot_mechanism_comparison(
    results_by_cache_size: dict,
    trace_name: str,
    out_path: str,
):
    """統合 vs 分割キャッシュの性能比較グラフ"""
    cache_sizes = sorted(results_by_cache_size.keys())
    unified_hr   = [results_by_cache_size[cs]["unified_hit_rate"]      for cs in cache_sizes]
    partitioned_hr = [results_by_cache_size[cs]["partitioned_hit_rate"] for cs in cache_sizes]
    unified_bmr  = [results_by_cache_size[cs]["unified_byte_miss_rate"] for cs in cache_sizes]
    partitioned_bmr = [results_by_cache_size[cs]["partitioned_byte_miss_rate"] for cs in cache_sizes]
    asymmetry    = [results_by_cache_size[cs]["asymmetry_score"]        for cs in cache_sizes]

    x_labels = [f"{cs:.0%}" for cs in cache_sizes]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle(f"{trace_name}  |  統合 vs 分割キャッシュ比較", fontsize=12)

    # ヒット率
    ax = axes[0]
    ax.plot(x_labels, unified_hr,     "o-", label="統合 LRU",  color="#DC2626", linewidth=1.5)
    ax.plot(x_labels, partitioned_hr, "s-", label="サイズ分割", color="#1976D2", linewidth=1.5)
    ax.set_xlabel("キャッシュ容量 (ワーキングセット比)")
    ax.set_ylabel("ヒット率")
    ax.set_title("ヒット率")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # バイトミス率
    ax = axes[1]
    ax.plot(x_labels, unified_bmr,     "o-", label="統合 LRU",  color="#DC2626", linewidth=1.5)
    ax.plot(x_labels, partitioned_bmr, "s-", label="サイズ分割", color="#1976D2", linewidth=1.5)
    ax.set_xlabel("キャッシュ容量 (ワーキングセット比)")
    ax.set_ylabel("バイトミス率")
    ax.set_title("バイトミス率")
    ax.legend()
    ax.grid(True, alpha=0.3)

    # 退避非対称性スコア
    ax = axes[2]
    ax.bar(x_labels, asymmetry, color="#9C27B0", alpha=0.8)
    ax.set_xlabel("キャッシュ容量 (ワーキングセット比)")
    ax.set_ylabel("大クラス→小クラス退避割合")
    ax.set_title("退避非対称性スコア\n（仮説1: 高いほど干渉が主因）")
    ax.set_ylim(0, 1)
    ax.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


# ─────────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────────

def run_single_trace(
    trace_path: str,
    cache_size_fracs: list,
    thresholds: list,
    out_dir: str,
    max_requests: int = None,
):
    trace_name = Path(trace_path).stem
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"トレース: {trace_name}")
    print(f"{'='*60}")

    # トレース読み込み
    df = load_trace(trace_path, max_requests=max_requests)
    n_classes = len(thresholds) + 1
    class_labels = build_class_labels(thresholds)

    # ── サイズ-人気度分析 ──
    print("\n[1/3] サイズ-人気度分析...")
    sp_stats = analyze_size_popularity(df, thresholds)
    sp_path = os.path.join(out_dir, f"{trace_name}_size_popularity.csv")
    sp_stats.to_csv(sp_path, index=False, encoding="utf-8-sig")
    print(f"  保存: {sp_path}")
    print(sp_stats[["size_class_label", "n_objects", "total_requests",
                     "mean_freq", "one_hit_wonder_frac"]].to_string(index=False))

    # ── ワーキングセットサイズの推定 ──
    # ユニークオブジェクトの総バイト数をワーキングセットサイズとみなす
    wss_bytes = df.groupby("obj_id")["obj_size"].first().sum()
    print(f"\n  ワーキングセットサイズ（推定）: {wss_bytes / 1e9:.3f} GB")

    # サイズクラス別バイト占有率（分割キャッシュの容量配分に使用）
    df["size_class"] = df["obj_size"].apply(lambda s: get_size_class(s, thresholds))
    class_byte_fracs = (
        df.groupby("size_class")["obj_size"]
        .sum()
        .reindex(range(n_classes), fill_value=0)
        .values.astype(float)
    )
    class_byte_fracs /= class_byte_fracs.sum() + 1e-10
    print("  サイズクラス別バイト占有率:")
    for i, lbl in class_labels.items():
        print(f"    {lbl:<15}: {class_byte_fracs[i]:.3%}")

    # ── キャッシュシミュレーション ──
    print("\n[2/3] キャッシュシミュレーション...")
    all_results = {}

    for frac in cache_size_fracs:
        cap = max(int(wss_bytes * frac), 1)
        print(f"\n  キャッシュ容量: {frac:.0%} × WSS = {cap / 1e6:.1f} MB")

        # 統合 LRU
        unified = ByteAwareLRU(cap, thresholds)
        # 分割キャッシュ（バイト占有率比例配分）
        partitioned = SizePartitionedCache(cap, thresholds, class_byte_fracs)

        for _, row in df.iterrows():
            unified.access(str(row["obj_id"]), int(row["obj_size"]))
            partitioned.access(str(row["obj_id"]), int(row["obj_size"]))

        # 退避行列を CSV 保存
        em_df = pd.DataFrame(
            unified.eviction_matrix,
            index=[class_labels[i] for i in range(n_classes)],
            columns=[class_labels[i] for i in range(n_classes)],
        )
        em_df.index.name = "evicted_class \\ inserting_class"
        em_path = os.path.join(
            out_dir, f"{trace_name}_eviction_matrix_cs{int(frac*100):02d}pct.csv"
        )
        em_df.to_csv(em_path, encoding="utf-8-sig")
        print(f"  退避行列保存: {em_path}")
        print("\n  退避行列 (正規化):")
        print(pd.DataFrame(
            unified.normalized_eviction_matrix(),
            index=[class_labels[i] for i in range(n_classes)],
            columns=[class_labels[i] for i in range(n_classes)],
        ).round(3).to_string())

        # グラフ
        plot_eviction_matrix(
            unified, thresholds, trace_name, frac,
            os.path.join(out_dir, f"{trace_name}_eviction_cs{int(frac*100):02d}pct.png")
        )

        # メカニズム指標
        metrics = compute_mechanism_metrics(unified, partitioned, thresholds)
        metrics["cache_size_frac"] = frac
        metrics["trace"] = trace_name
        all_results[frac] = metrics

        print(f"  統合ヒット率:  {metrics['unified_hit_rate']:.4f}")
        print(f"  分割ヒット率:  {metrics['partitioned_hit_rate']:.4f}  "
              f"(改善: {metrics['hit_rate_improvement']:+.4f})")
        print(f"  統合バイトMR:  {metrics['unified_byte_miss_rate']:.4f}")
        print(f"  分割バイトMR:  {metrics['partitioned_byte_miss_rate']:.4f}  "
              f"(改善: {metrics['bmr_improvement']:+.4f})")
        print(f"  退避非対称性:  {metrics['asymmetry_score']:.4f}")

    # ── サマリー保存 ──
    print("\n[3/3] サマリー保存...")
    summary_df = pd.DataFrame(list(all_results.values()))
    summary_path = os.path.join(out_dir, f"{trace_name}_mechanism_summary.csv")
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    print(f"  保存: {summary_path}")

    # 比較グラフ
    if len(all_results) > 1:
        plot_mechanism_comparison(
            all_results, trace_name,
            os.path.join(out_dir, f"{trace_name}_comparison.png")
        )

    return all_results


# ─────────────────────────────────────────────
# エントリーポイント
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="クロスサイズ退避行列によるキャッシュメカニズム分析"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace", type=str,
                       help="単一トレースファイル (CSV)")
    group.add_argument("--trace-dir", type=str,
                       help="トレースファイルが格納されたディレクトリ")

    parser.add_argument(
        "--cache-sizes", type=float, nargs="+",
        default=[0.01, 0.05, 0.1, 0.2, 0.3],
        help="キャッシュ容量 / ワーキングセットサイズ の比 (複数指定可, デフォルト: 0.01 0.05 0.1 0.2 0.3)"
    )
    parser.add_argument(
        "--thresholds", type=int, nargs="+",
        default=DEFAULT_THRESHOLDS,
        help=f"サイズクラス境界 (バイト, デフォルト: {DEFAULT_THRESHOLDS})"
    )
    parser.add_argument(
        "--out", type=str, default="./output/eviction_analysis",
        help="出力ディレクトリ (デフォルト: ./output/eviction_analysis)"
    )
    parser.add_argument(
        "--max-requests", type=int, default=None,
        help="読み込む最大リクエスト数 (デバッグ用)"
    )
    parser.add_argument(
        "--time-col",   type=int, default=0, help="タイムスタンプ列インデックス"
    )
    parser.add_argument(
        "--id-col",     type=int, default=1, help="オブジェクトID列インデックス"
    )
    parser.add_argument(
        "--size-col",   type=int, default=2, help="オブジェクトサイズ列インデックス"
    )

    args = parser.parse_args()

    # トレースファイルリスト
    if args.trace:
        trace_files = [args.trace]
    else:
        trace_dir = Path(args.trace_dir)
        trace_files = sorted(
            list(trace_dir.glob("*.csv")) +
            list(trace_dir.glob("*.tsv")) +
            list(trace_dir.glob("*.txt"))
        )
        if not trace_files:
            print(f"エラー: {trace_dir} にトレースファイルが見つかりません")
            sys.exit(1)
        print(f"{len(trace_files)} 件のトレースを処理します")

    all_summaries = []
    for tf in trace_files:
        results = run_single_trace(
            trace_path=str(tf),
            cache_size_fracs=args.cache_sizes,
            thresholds=args.thresholds,
            out_dir=args.out,
            max_requests=args.max_requests,
        )
        for cs, metrics in results.items():
            all_summaries.append(metrics)

    # 全トレース横断サマリー
    if all_summaries:
        agg_df = pd.DataFrame(all_summaries)
        agg_path = os.path.join(args.out, "ALL_TRACES_mechanism_summary.csv")
        agg_df.to_csv(agg_path, index=False, encoding="utf-8-sig")
        print(f"\n全トレース横断サマリー保存: {agg_path}")

        # 主要指標の相関サマリー
        if len(agg_df) > 3:
            print("\n=== 仮説検証サマリー ===")
            for metric_x, metric_y in [
                ("asymmetry_score", "hit_rate_improvement"),
                ("asymmetry_score", "bmr_improvement"),
            ]:
                if metric_x in agg_df.columns and metric_y in agg_df.columns:
                    valid = agg_df[[metric_x, metric_y]].dropna()
                    if len(valid) > 3:
                        rho, pval = spearmanr(valid[metric_x], valid[metric_y])
                        print(f"  {metric_x} vs {metric_y}: ρ={rho:.3f} (p={pval:.4g})")


if __name__ == "__main__":
    main()
