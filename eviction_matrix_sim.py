"""
eviction_matrix_sim.py
======================
クロスサイズ退避行列（Cross-Size Eviction Matrix）の測定

【ポリシー】
  LRU / FIFO / S3-FIFO / ARC の 4 ポリシーを同一トレースで実行し、
  各ポリシーでどのサイズクラスが何を退避させるかを記録する。

  ポリシー間で退避行列パターン（特に非対称性スコア）が一致すれば、
  クロスサイズ退避干渉はポリシー非依存のトレース本来の特性であり、
  EuroSys 論文の主張（M2 の効果はポリシーに依らない）を強化できる。

【ポリシーの概要】
  LRU     : Least Recently Used（OrderedDict ベース）
  FIFO    : First-In First-Out（deque ベース）
  S3-FIFO : Simple, Scalable FIFO (Zhang et al., SOSP 2023)
              小キュー S (10%) + メインキュー M (90%) + ゴースト G
              freq bit で 1 回だけセカンドチャンスを与える
  ARC     : Adaptive Replacement Cache (Megiddo & Modha, FAST 2003)
              T1/T2 (実キャッシュ) + B1/B2 (ゴースト) で p を適応調整

【binning 設計】
  1KiB〜8GiB を 2 の指数乗で 24 本の閾値に区切る（25 bin）。
  詳細は cache_common.py を参照。
  後処理で aggregate_matrix(n_merge=2) を呼ぶと 2/4 倍幅 bin に集約できる。

【使い方】
  python eviction_matrix_sim.py --trace ./traces/cdn.oracleGeneral --out ./out
  python eviction_matrix_sim.py --trace-dir ./traces --cache-sizes 0.01 0.05 0.1 \\
      --policies lru fifo s3fifo arc --out ./out

【出力】
  {out}/{trace}_{policy}_eviction_matrix_cs{pct}pct.csv    細粒度 (25×25)
  {out}/{trace}_{policy}_eviction_matrix_cs{pct}pct_x2.csv 2×集約 (13×13)
  {out}/{trace}_{policy}_eviction_matrix_cs{pct}pct_x4.csv 4×集約 (7×7)
  {out}/{trace}_lru_eviction_heatmap_cs{pct}pct.png        LRU 詳細図
  {out}/{trace}_policy_comparison_cs{pct}pct.png           4ポリシー比較図
  {out}/{trace}_size_popularity.csv
  {out}/{trace}_mechanism_summary.csv
  {out}/ALL_TRACES_mechanism_summary.csv
"""

import argparse
import os
import sys
from pathlib import Path
from collections import OrderedDict, deque

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from cache_common import (
    POW2_THRESHOLDS, N_BINS,
    get_size_class, get_size_class_vectorized,
    build_class_labels, aggregate_matrix, plot_eviction_heatmap,
    load_trace, iter_oracle_general,
    setup_matplotlib_font,
)
setup_matplotlib_font()


# ═════════════════════════════════════════════
# 共通ベースクラス
# ═════════════════════════════════════════════

class _BaseCache:
    """
    全ポリシー共通の統計カウンタ・退避行列インターフェース。

    退避行列の定義:
      eviction_matrix[ev_sc][ins_sc] += 1
        ev_sc  : 退避されたオブジェクトのサイズクラス
        ins_sc : その退避を引き起こした（挿入中の）オブジェクトのサイズクラス
    """

    POLICY = "base"

    def __init__(self, capacity_bytes: int, n_classes: int = N_BINS):
        self.capacity   = capacity_bytes
        self.n_classes  = n_classes
        self.used_bytes = 0        # サブクラスが任意に使用

        self.hits       = 0
        self.misses     = 0
        self.hit_bytes  = 0
        self.miss_bytes = 0

        self.eviction_matrix  = np.zeros((n_classes, n_classes), dtype=np.int64)
        self.class_hits       = np.zeros(n_classes, dtype=np.int64)
        self.class_misses     = np.zeros(n_classes, dtype=np.int64)
        self.class_bytes_hit  = np.zeros(n_classes, dtype=np.int64)
        self.class_bytes_miss = np.zeros(n_classes, dtype=np.int64)

    # ── 内部ヘルパー ──────────────────────────

    def _on_hit(self, size: int, sc: int):
        self.hits      += 1
        self.hit_bytes += size
        self.class_hits[sc]      += 1
        self.class_bytes_hit[sc] += size

    def _on_miss(self, size: int, sc: int):
        self.misses      += 1
        self.miss_bytes  += size
        self.class_misses[sc]      += 1
        self.class_bytes_miss[sc]  += size

    def _on_evict(self, ev_sc: int, ins_sc: int):
        self.eviction_matrix[ev_sc][ins_sc] += 1

    # ── 外部向け集計メソッド ──────────────────

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def byte_miss_rate(self) -> float:
        total = self.hit_bytes + self.miss_bytes
        return self.miss_bytes / total if total else 0.0

    def class_hit_rates(self) -> np.ndarray:
        total = self.class_hits + self.class_misses
        total = np.where(total == 0, 1, total)
        return self.class_hits / total

    def normalized_eviction_matrix(self) -> np.ndarray:
        """列（挿入クラス）の合計で正規化"""
        col_sums = self.eviction_matrix.sum(axis=0, keepdims=True)
        col_sums = np.where(col_sums == 0, 1, col_sums)
        return self.eviction_matrix / col_sums

    def asymmetry_score(self, mid_class: int = None) -> float:
        """
        「大クラスが小クラスを退避させる」割合。
        mid_class 以上のクラスが mid_class 未満を退避させるケースの比率。
        値が高い → 退避干渉の非対称性が強い（仮説1の根拠）。
        """
        if mid_class is None:
            mid_class = self.n_classes // 2
        em  = self.eviction_matrix
        lge = em[:mid_class, mid_class:].sum()
        tot = em.sum()
        return float(lge / tot) if tot > 0 else 0.0


# ═════════════════════════════════════════════
# LRU
# ═════════════════════════════════════════════

class ByteAwareLRU(_BaseCache):
    """
    Least Recently Used キャッシュ。
    OrderedDict を使い O(1) でアクセス・退避を行う。
    """
    POLICY = "lru"

    def __init__(self, capacity_bytes: int, n_classes: int = N_BINS):
        super().__init__(capacity_bytes, n_classes)
        self._cache = OrderedDict()   # obj_id -> (size, sc)

    def access(self, obj_id: str, size: int, sc: int) -> bool:
        if obj_id in self._cache:
            self._cache.move_to_end(obj_id)
            self._on_hit(size, sc)
            return True

        self._on_miss(size, sc)

        while self.used_bytes + size > self.capacity and self._cache:
            _, (ev_size, ev_sc) = self._cache.popitem(last=False)
            self.used_bytes -= ev_size
            self._on_evict(ev_sc, sc)

        if size <= self.capacity:
            self._cache[obj_id] = (size, sc)
            self.used_bytes += size
        return False


# ═════════════════════════════════════════════
# FIFO
# ═════════════════════════════════════════════

class ByteAwareFIFO(_BaseCache):
    """
    First-In First-Out キャッシュ。
    ヒット時にも順序を変えない。deque + lookup dict で O(1)。
    """
    POLICY = "fifo"

    def __init__(self, capacity_bytes: int, n_classes: int = N_BINS):
        super().__init__(capacity_bytes, n_classes)
        self._queue  = deque()   # (obj_id, size, sc)  挿入順
        self._lookup = {}        # obj_id -> (size, sc)

    def access(self, obj_id: str, size: int, sc: int) -> bool:
        if obj_id in self._lookup:
            self._on_hit(size, sc)
            return True

        self._on_miss(size, sc)

        while self.used_bytes + size > self.capacity and self._queue:
            ev_id, ev_size, ev_sc = self._queue.popleft()
            if ev_id in self._lookup:
                del self._lookup[ev_id]
                self.used_bytes -= ev_size
                self._on_evict(ev_sc, sc)

        if size <= self.capacity:
            self._queue.append((obj_id, size, sc))
            self._lookup[obj_id] = (size, sc)
            self.used_bytes += size
        return False


# ═════════════════════════════════════════════
# S3-FIFO
# ═════════════════════════════════════════════

class ByteAwareS3FIFO(_BaseCache):
    """
    S3-FIFO: Simple, Scalable FIFO Queues for Cache Eviction
    (Yazhuo Zhang et al., SOSP 2023)

    アルゴリズム:
      1. 全オブジェクトは最初に S（小キュー、容量の SMALL_FRAC）に入る
      2. ヒット時: freq bit を 1 にする（上限 1）
      3. S からの退避:
           freq=1 → M（メインキュー）に昇格し freq=0 に
           freq=0 → 退避して G（ゴースト）に追加
      4. M からの退避:
           freq=1 → M の末尾に戻す（セカンドチャンス）、freq=0 に
           freq=0 → 退避
      5. 新オブジェクトが G にあれば M に直接挿入（S をバイパス）

    実装上の注意:
      - deque への遅延削除（lazy deletion）を用いて O(1) amortized を実現
      - ゴーストは MAX_GHOST 件に制限してメモリを抑制
    """
    POLICY     = "s3fifo"
    SMALL_FRAC = 0.1
    MAX_GHOST  = 200_000

    def __init__(self, capacity_bytes: int, n_classes: int = N_BINS):
        super().__init__(capacity_bytes, n_classes)

        self._cap_s = max(int(capacity_bytes * self.SMALL_FRAC), 1)
        self._cap_m = max(capacity_bytes - self._cap_s, 1)

        # キューには obj_id のみ格納。遅延削除で古いエントリをスキップ。
        self._q_S = deque()
        self._q_M = deque()

        # キャッシュ内オブジェクト情報: obj_id -> [size, sc, freq, tag]
        #   tag: "S" または "M"（遅延削除時のキュー帰属判定に使用）
        self._data: dict = {}

        # ゴースト（S から退避された obj_id）: OrderedDict で挿入順管理
        self._ghost: OrderedDict = OrderedDict()

        self._used_S = 0
        self._used_M = 0

    def access(self, obj_id: str, size: int, sc: int) -> bool:
        # ── ヒット ──
        if obj_id in self._data:
            self._data[obj_id][2] = min(self._data[obj_id][2] + 1, 1)
            self._on_hit(size, sc)
            return True

        # ── ミス ──
        self._on_miss(size, sc)

        if obj_id in self._ghost:
            # ゴーストヒット → M に直接挿入
            del self._ghost[obj_id]
            self._insert_to_M(obj_id, size, sc, sc)
        else:
            # S に挿入（容量不足なら S から退避/昇格を繰り返す）
            while self._used_S + size > self._cap_s:
                if not self._evict_from_S(sc):
                    break
            if size <= self._cap_s:
                self._data[obj_id] = [size, sc, 0, "S"]
                self._q_S.append(obj_id)
                self._used_S += size
            elif size <= self._cap_m:
                # S に入らないサイズは直接 M へ
                self._insert_to_M(obj_id, size, sc, sc)
            # size > cap_m はキャッシュ不可（no-op）
        return False

    def _evict_from_S(self, ins_sc: int) -> bool:
        """
        S の先頭から 1 件を処理する。
          freq=1 → M に昇格
          freq=0 → 退避してゴーストへ
        何らかの処理をした場合 True を返す。
        """
        while self._q_S:
            ev_id  = self._q_S.popleft()
            entry  = self._data.get(ev_id)
            if entry is None or entry[3] != "S":
                continue   # 遅延削除: すでに処理済みのエントリ

            ev_size, ev_sc, ev_freq, _ = entry
            del self._data[ev_id]
            self._used_S -= ev_size

            if ev_freq > 0:
                # M へ昇格（M が満杯なら M から退避）
                self._insert_to_M(ev_id, ev_size, ev_sc, ins_sc)
            else:
                # 退避 → ゴーストに追加
                self._on_evict(ev_sc, ins_sc)
                self._ghost[ev_id] = True
                if len(self._ghost) > self.MAX_GHOST:
                    self._ghost.popitem(last=False)
            return True
        return False

    def _evict_from_M(self, ins_sc: int) -> bool:
        """
        M の先頭から 1 件退避（セカンドチャンスあり）。
        freq=1 → freq=0 にして末尾へ（セカンドチャンス）
        freq=0 → 退避
        True = 退避成功。
        """
        # 無限ループ防止: M のサイズの 2 倍を上限にイテレーション
        limit   = len(self._q_M) * 2 + 2
        checked = 0
        while self._q_M and checked < limit:
            ev_id = self._q_M.popleft()
            checked += 1
            entry = self._data.get(ev_id)
            if entry is None or entry[3] != "M":
                continue

            ev_size, ev_sc, ev_freq, _ = entry
            if ev_freq > 0:
                # セカンドチャンス: freq=0 にして末尾へ戻す
                self._data[ev_id][2] = 0
                self._q_M.append(ev_id)
            else:
                del self._data[ev_id]
                self._used_M -= ev_size
                self._on_evict(ev_sc, ins_sc)
                return True
        return False

    def _insert_to_M(self, obj_id: str, size: int, sc: int, ins_sc: int):
        """M にオブジェクトを挿入する（必要なら退避）。"""
        while self._used_M + size > self._cap_m:
            if not self._evict_from_M(ins_sc):
                break
        if size <= self._cap_m:
            self._data[obj_id] = [size, sc, 0, "M"]
            self._q_M.append(obj_id)
            self._used_M += size


# ═════════════════════════════════════════════
# ARC
# ═════════════════════════════════════════════

class ByteAwareARC(_BaseCache):
    """
    ARC: Adaptive Replacement Cache
    (Nimrod Megiddo & Dharmendra S. Modha, FAST 2003)

    4 つのリスト:
      T1: 最近 1 回アクセスのオブジェクト（LRU 管理）
      T2: 最近 2 回以上アクセスのオブジェクト（LRU 管理）
      B1: T1 から退避されたゴースト（キーのみ）
      B2: T2 から退避されたゴースト（キーのみ）

    適応パラメータ p（T1 の目標バイト容量）:
      B1 ヒット → p を増加（T1 に多く割く）
      B2 ヒット → p を減少（T2 に多く割く）
      delta = size × max(1, |B2| / |B1|)  or  max(1, |B1| / |B2|)

    可変サイズへの対応:
      - T1/T2 の容量はバイト単位で管理
      - p もバイト単位（原論文のカウントベースを拡張）
    """
    POLICY    = "arc"
    MAX_GHOST = 200_000   # B1/B2 各々の最大エントリ数

    def __init__(self, capacity_bytes: int, n_classes: int = N_BINS):
        super().__init__(capacity_bytes, n_classes)

        self._T1 = OrderedDict()   # obj_id -> (size, sc)  LRU 順
        self._T2 = OrderedDict()   # obj_id -> (size, sc)  LRU 順
        self._B1 = OrderedDict()   # obj_id -> size  ゴースト T1（LRU 順）
        self._B2 = OrderedDict()   # obj_id -> size  ゴースト T2（LRU 順）

        self._used_T1 = 0
        self._used_T2 = 0

        # p: T1 の目標バイト容量（0 で初期化、アクセスに応じて適応）
        self._p: float = 0.0

    # ── Replace ──────────────────────────────

    def _arc_replace(self, ins_sc: int, ins_size: int = 1):
        """
        ARC の Replace ルール:
          used_T1 >= p かつ T1 に要素があれば T1 の LRU 端を B1 へ退避。
          それ以外は T2 の LRU 端を B2 へ退避。
        """
        if self._T1 and (self._used_T1 >= max(self._p, ins_size) or not self._T2):
            ev_id, (ev_size, ev_sc) = self._T1.popitem(last=False)
            self._used_T1 -= ev_size
            self._B1[ev_id] = ev_size
            if len(self._B1) > self.MAX_GHOST:
                self._B1.popitem(last=False)
            self._on_evict(ev_sc, ins_sc)
        elif self._T2:
            ev_id, (ev_size, ev_sc) = self._T2.popitem(last=False)
            self._used_T2 -= ev_size
            self._B2[ev_id] = ev_size
            if len(self._B2) > self.MAX_GHOST:
                self._B2.popitem(last=False)
            self._on_evict(ev_sc, ins_sc)
        elif self._T1:
            # T2 が空のフォールバック
            ev_id, (ev_size, ev_sc) = self._T1.popitem(last=False)
            self._used_T1 -= ev_size
            self._B1[ev_id] = ev_size
            if len(self._B1) > self.MAX_GHOST:
                self._B1.popitem(last=False)
            self._on_evict(ev_sc, ins_sc)

    def access(self, obj_id: str, size: int, sc: int) -> bool:
        cap = self.capacity

        # ── Case 1: T1 ヒット → T2 MRU へ ──
        if obj_id in self._T1:
            old_size, old_sc = self._T1.pop(obj_id)
            self._used_T1 -= old_size
            self._T2[obj_id] = (size, sc)
            self._used_T2  += size
            self._on_hit(size, sc)
            return True

        # ── Case 2: T2 ヒット → T2 内 MRU へ ──
        if obj_id in self._T2:
            self._T2.move_to_end(obj_id)
            self._on_hit(size, sc)
            return True

        # ── ミス ──
        self._on_miss(size, sc)

        in_B1 = obj_id in self._B1
        in_B2 = obj_id in self._B2

        # ── Case 3: B1 ゴーストヒット → p を増加、T2 に挿入 ──
        if in_B1:
            self._B1.pop(obj_id)
            b1n = max(len(self._B1), 1)
            b2n = max(len(self._B2), 1)
            delta = max(size, size * b2n / b1n)
            self._p = min(self._p + delta, float(cap))

            while self._used_T1 + self._used_T2 + size > cap:
                self._arc_replace(sc, size)
            self._T2[obj_id] = (size, sc)
            self._used_T2   += size
            return False

        # ── Case 4: B2 ゴーストヒット → p を減少、T2 に挿入 ──
        if in_B2:
            self._B2.pop(obj_id)
            b1n = max(len(self._B1), 1)
            b2n = max(len(self._B2), 1)
            delta = max(size, size * b1n / b2n)
            self._p = max(self._p - delta, 0.0)

            while self._used_T1 + self._used_T2 + size > cap:
                self._arc_replace(sc, size)
            self._T2[obj_id] = (size, sc)
            self._used_T2   += size
            return False

        # ── Case 5: 完全ミス → T1 に挿入 ──
        while self._used_T1 + self._used_T2 + size > cap:
            self._arc_replace(sc, size)

        if size <= cap:
            self._T1[obj_id] = (size, sc)
            self._used_T1   += size
        return False


# ─────────────────────────────────────────────
# ポリシー名 → クラスのマッピング
# ─────────────────────────────────────────────

POLICIES: dict = {
    "lru":    ByteAwareLRU,
    "fifo":   ByteAwareFIFO,
    "s3fifo": ByteAwareS3FIFO,
    "arc":    ByteAwareARC,
}
POLICY_LABELS = {
    "lru":    "LRU",
    "fifo":   "FIFO",
    "s3fifo": "S3-FIFO",
    "arc":    "ARC",
}


# ═════════════════════════════════════════════
# 分割キャッシュ（サイズクラスごとに独立した LRU）
# ═════════════════════════════════════════════

class SizePartitionedCache:
    """
    各サイズクラスに独立した ByteAwareLRU を持つ分割キャッシュ。
    容量配分はバイト占有率に比例。
    """

    def __init__(self, total_bytes: int,
                 class_byte_fracs,
                 n_classes: int = N_BINS):
        capacities = [max(int(f * total_bytes), 1) for f in class_byte_fracs]
        self.caches = [ByteAwareLRU(int(c), n_classes) for c in capacities]

    def access(self, obj_id: str, size: int, sc: int) -> bool:
        return self.caches[sc].access(obj_id, size, sc)

    def hit_rate(self) -> float:
        h = sum(c.hits   for c in self.caches)
        m = sum(c.misses for c in self.caches)
        return h / (h + m) if (h + m) else 0.0

    def byte_miss_rate(self) -> float:
        hb = sum(c.hit_bytes  for c in self.caches)
        mb = sum(c.miss_bytes for c in self.caches)
        return mb / (hb + mb) if (hb + mb) else 0.0


# ─────────────────────────────────────────────
# サイズ-人気度分析
# ─────────────────────────────────────────────

def analyze_size_popularity(df):
    import pandas as pd
    from scipy.stats import spearmanr

    class_labels = build_class_labels()

    obj_stats = (
        df.groupby("obj_id")
        .agg(size=("obj_size", "first"),
             freq=("obj_id", "count"),
             sc=("size_class", "first"))
        .reset_index()
    )
    obj_stats["is_ohw"] = obj_stats["freq"] == 1

    rho, pval = spearmanr(obj_stats["size"], obj_stats["freq"])
    print(f"  サイズ-人気度 Spearman ρ = {rho:+.4f}  (p = {pval:.3g})")

    class_stats = obj_stats.groupby("sc").agg(
        size_class_label=("sc",
            lambda x: class_labels.get(x.iloc[0], str(x.iloc[0]))),
        n_objects=("obj_id", "count"),
        total_requests=("freq", "sum"),
        mean_freq=("freq", "mean"),
        median_freq=("freq", "median"),
        ohw_frac=("is_ohw", "mean"),
        mean_size=("size", "mean"),
    ).reset_index().rename(columns={"sc": "size_class"})

    class_stats["size_rho"]   = rho
    class_stats["size_rho_p"] = pval
    return class_stats


# ═════════════════════════════════════════════
# 可視化
# ═════════════════════════════════════════════

def plot_results(unified,
                 partitioned,
                 sp_stats,
                 trace_name: str,
                 cache_size_pct: float,
                 out_path: str):
    """
    LRU ベースの詳細 5 パネル図。
      (A) 退避行列（件数）
      (B) 退避行列（正規化）
      (C) 4 倍幅 bin 集約行列
      (D) クラス別ヒット率：統合 LRU vs 分割 LRU
      (E) OHW 率
    """
    import matplotlib.pyplot as plt

    class_labels = build_class_labels()
    n = unified.n_classes

    fig = plt.figure(figsize=(22, 14))
    fig.suptitle(
        f"{trace_name}  |  LRU  キャッシュ容量 {cache_size_pct:.0%} × WSS\n"
        f"退避行列 (25×25 fine-grained, 2の指数乗 bin)",
        fontsize=11
    )
    gs = fig.add_gridspec(3, 3, hspace=0.45, wspace=0.35)

    # ── (A) 退避行列 生カウント ──
    ax = fig.add_subplot(gs[0:2, 0])
    im = plot_eviction_heatmap(
        ax, unified.eviction_matrix,
        title="(A) 退避行列（件数）",
        normalize_cols=False, cmap="YlOrRd", label_every=2
    )
    plt.colorbar(im, ax=ax, fraction=0.046)

    # ── (B) 退避行列 正規化 ──
    ax = fig.add_subplot(gs[0:2, 1])
    im2 = plot_eviction_heatmap(
        ax, unified.eviction_matrix,
        title="(B) 退避行列（正規化）\n列=挿入クラスで正規化",
        normalize_cols=True, cmap="Blues", label_every=2
    )
    plt.colorbar(im2, ax=ax, fraction=0.046)

    # ── (C) 4 倍幅集約 ──
    ax = fig.add_subplot(gs[0:2, 2])
    em_x4 = aggregate_matrix(unified.eviction_matrix, n_merge=4)
    coarse_labels = build_class_labels([2 ** i for i in range(10, 34, 4)])
    col_sums = em_x4.sum(axis=0, keepdims=True)
    col_sums = np.where(col_sums == 0, 1, col_sums)
    em_x4_norm = em_x4 / col_sums
    im3 = ax.imshow(em_x4_norm, cmap="Blues", aspect="auto", vmin=0, vmax=1)
    nc  = em_x4.shape[0]
    cl  = [coarse_labels.get(i, str(i)) for i in range(nc)]
    ax.set_xticks(range(nc)); ax.set_xticklabels(cl, rotation=45, ha="right", fontsize=7)
    ax.set_yticks(range(nc)); ax.set_yticklabels(cl, fontsize=7)
    ax.set_title("(C) 退避行列（4倍幅 bin に集約）\n隣接 bin を統合した粗粒度ビュー", fontsize=9)
    ax.set_xlabel("挿入クラス", fontsize=8)
    ax.set_ylabel("退避クラス", fontsize=8)
    plt.colorbar(im3, ax=ax, fraction=0.046)
    for i in range(nc):
        for j in range(nc):
            ax.text(j, i, f"{em_x4_norm[i,j]:.2f}",
                    ha="center", va="center", fontsize=6,
                    color="white" if em_x4_norm[i, j] > 0.6 else "black")

    # ── (D) クラス別ヒット率：統合 vs 分割 ──
    ax = fig.add_subplot(gs[2, 0:2])
    unified_hr    = unified.class_hit_rates()
    partitioned_hr = np.array([
        c.class_hit_rates()[i] for i, c in enumerate(partitioned.caches)
    ])
    active = [i for i in range(n)
              if (unified.class_hits[i] + unified.class_misses[i]) > 0]
    x   = np.arange(len(active))
    w   = 0.35
    lbls = [class_labels.get(i, str(i)) for i in active]
    ax.bar(x - w/2, unified_hr[active],     w, label="統合 LRU",   color="#DC2626", alpha=0.8)
    ax.bar(x + w/2, partitioned_hr[active], w, label="サイズ分割", color="#1976D2", alpha=0.8)
    ax.set_xticks(x); ax.set_xticklabels(lbls, rotation=45, ha="right", fontsize=7)
    ax.set_ylabel("ヒット率"); ax.set_ylim(0, 1)
    ax.set_title("(D) サイズクラス別ヒット率：統合 LRU vs 分割 LRU", fontsize=9)
    ax.legend(fontsize=8); ax.grid(True, alpha=0.3, axis="y")

    # ── (E) OHW 率 ──
    ax = fig.add_subplot(gs[2, 2])
    if sp_stats is not None and "ohw_frac" in sp_stats.columns:
        active_sc = sp_stats[sp_stats["size_class"].isin(active)]
        ax.bar(range(len(active_sc)),
               active_sc["ohw_frac"].values,
               color="#9C27B0", alpha=0.8)
        ax.set_xticks(range(len(active_sc)))
        ax.set_xticklabels(active_sc["size_class_label"].values,
                           rotation=45, ha="right", fontsize=7)
        ax.set_ylabel("One-Hit-Wonder 率")
        ax.set_ylim(0, 1)
        ax.set_title("(E) OHW 率（サイズクラス別）\n高い = 退避汚染の主因候補", fontsize=9)
        ax.grid(True, alpha=0.3, axis="y")

    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


def plot_policy_comparison_heatmaps(
    caches: dict,
    trace_name: str,
    cache_size_pct: float,
    out_path: str,
):
    """
    4 ポリシーの正規化退避行列を 2×2 グリッドで並べる比較図。
    追加パネルとして非対称性スコアと全体ヒット率の棒グラフを表示。
    """
    import matplotlib.pyplot as plt

    pol_keys = list(caches.keys())
    n_pol    = len(pol_keys)
    if n_pol == 0:
        return

    grid_keys = pol_keys[:4]
    n_cols    = 2
    n_rows    = (len(grid_keys) + 1) // 2

    fig = plt.figure(figsize=(14, 5 * n_rows + 4))
    fig.suptitle(
        f"{trace_name}  |  ポリシー別 退避行列比較  |  容量 {cache_size_pct:.0%} × WSS\n"
        f"列（挿入クラス）で正規化。ポリシー間で非対称パターンが一致 → トレース本来の特性",
        fontsize=11
    )

    gs = fig.add_gridspec(n_rows + 1, n_cols, hspace=0.55, wspace=0.35)

    for idx, key in enumerate(grid_keys):
        row, col = divmod(idx, n_cols)
        ax  = fig.add_subplot(gs[row, col])
        pol = caches[key]
        asym = pol.asymmetry_score()
        im  = plot_eviction_heatmap(
            ax, pol.eviction_matrix,
            title=(f"({chr(65+idx)}) {POLICY_LABELS.get(key, key)}\n"
                   f"HR={pol.hit_rate():.4f}  asymmetry={asym:.4f}"),
            normalize_cols=True, cmap="Blues", label_every=2
        )
        plt.colorbar(im, ax=ax, fraction=0.046)

    ax_asym = fig.add_subplot(gs[n_rows, 0])
    labels    = [POLICY_LABELS.get(k, k) for k in pol_keys]
    asym_vals = [caches[k].asymmetry_score() for k in pol_keys]
    colors_bar = plt.cm.tab10(np.linspace(0, 1, max(len(pol_keys), 2)))
    bars = ax_asym.bar(range(len(pol_keys)), asym_vals,
                       color=colors_bar[:len(pol_keys)], alpha=0.85)
    ax_asym.set_xticks(range(len(pol_keys)))
    ax_asym.set_xticklabels(labels, fontsize=9)
    ax_asym.set_ylabel("非対称性スコア\n（大クラスが小クラスを退避させる割合）")
    ax_asym.set_ylim(0, max(asym_vals) * 1.3 + 0.01)
    ax_asym.set_title("ポリシー別 退避非対称性スコア\n"
                      "値が揃う → ポリシー非依存の特性", fontsize=9)
    ax_asym.grid(True, alpha=0.3, axis="y")
    for bar, val in zip(bars, asym_vals):
        ax_asym.text(bar.get_x() + bar.get_width() / 2, val + 0.002,
                     f"{val:.4f}", ha="center", va="bottom", fontsize=8)

    ax_hr = fig.add_subplot(gs[n_rows, 1])
    hr_vals = [caches[k].hit_rate() for k in pol_keys]
    ax_hr.bar(range(len(pol_keys)), hr_vals,
              color=colors_bar[:len(pol_keys)], alpha=0.85)
    ax_hr.set_xticks(range(len(pol_keys)))
    ax_hr.set_xticklabels(labels, fontsize=9)
    ax_hr.set_ylabel("ヒット率")
    ax_hr.set_ylim(0, min(max(hr_vals) * 1.2 + 0.01, 1.0))
    ax_hr.set_title("ポリシー別ヒット率", fontsize=9)
    ax_hr.grid(True, alpha=0.3, axis="y")
    for i, val in enumerate(hr_vals):
        ax_hr.text(i, val + 0.003, f"{val:.4f}",
                   ha="center", va="bottom", fontsize=8)

    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  グラフ保存: {out_path}")


# ═════════════════════════════════════════════
# シミュレーション実行
# ═════════════════════════════════════════════

def _scan_wss_streaming(trace_path: str,
                        sample_stride: int = 1,
                        max_requests: int = None):
    """
    Pass 1: トレースをストリーミングスキャンして WSS / クラス統計 / オブジェクト情報を収集する。

    Returns:
        wss_bytes       : ワーキングセットサイズ（バイト）
        class_byte_fracs: クラス別バイト占有率 (shape: N_BINS,)
        obj_info        : {obj_id_int: [first_size, sc, count]} 辞書
                          ※ユニークオブジェクト数が多いとメモリを使う。
                          --sample-stride で間引くと比例して削減できる。
        n_req           : 処理したリクエスト総数
    """
    class_byte_sums = np.zeros(N_BINS, dtype=np.float64)
    obj_info: dict = {}   # {oid_int: [first_size, sc, count]}
    n_req = 0

    _prog_interval = 5_000_000
    for ts, oid, sz, nv in iter_oracle_general(trace_path, sample_stride, max_requests):
        # サイズクラス: POW2_THRESHOLDS の高速パス (bit_length)
        bl = sz.bit_length()
        sc = max(0, min(bl - 10, N_BINS - 1)) if sz >= 1024 else 0

        class_byte_sums[sc] += sz
        if oid in obj_info:
            obj_info[oid][2] += 1
        else:
            obj_info[oid] = [sz, sc, 1]
        n_req += 1

        if n_req % _prog_interval == 0:
            print(f"    [Pass1] {n_req:,} req 処理済み "
                  f"ユニーク={len(obj_info):,}", end="\r", flush=True)

    print(f"    [Pass1] 完了: {n_req:,} req  ユニーク={len(obj_info):,}        ")

    wss_bytes = sum(v[0] for v in obj_info.values())
    total_bytes = class_byte_sums.sum()
    class_byte_fracs = class_byte_sums / (total_bytes + 1e-10)

    return wss_bytes, class_byte_fracs, obj_info, n_req


def _sp_stats_from_obj_info(obj_info: dict) -> "pd.DataFrame":
    """
    Pass 1 で収集した obj_info から analyze_size_popularity 相当の統計を計算する。
    obj_info = {oid: [first_size, sc, count]}
    """
    from scipy.stats import spearmanr

    class_labels = build_class_labels()

    if not obj_info:
        return pd.DataFrame()

    sizes  = np.array([v[0] for v in obj_info.values()], dtype=np.int64)
    scs    = np.array([v[1] for v in obj_info.values()], dtype=np.int32)
    counts = np.array([v[2] for v in obj_info.values()], dtype=np.int64)

    rho, pval = spearmanr(sizes, counts)
    print(f"  サイズ-人気度 Spearman ρ = {rho:+.4f}  (p = {pval:.3g})")

    rows = []
    for sc in range(N_BINS):
        mask = (scs == sc)
        if mask.sum() == 0:
            continue
        sc_counts = counts[mask]
        sc_sizes  = sizes[mask]
        total_req = int(sc_counts.sum())
        n_ohw     = int((sc_counts == 1).sum())
        rows.append({
            "size_class":       sc,
            "size_class_label": class_labels.get(sc, str(sc)),
            "n_objects":        int(mask.sum()),
            "total_requests":   total_req,
            "mean_freq":        float(sc_counts.mean()),
            "median_freq":      float(np.median(sc_counts)),
            "ohw_frac":         n_ohw / max(mask.sum(), 1),
            "mean_size":        float(sc_sizes.mean()),
            "size_rho":         rho,
            "size_rho_p":       pval,
        })

    return pd.DataFrame(rows)


def run_single_trace(trace_path: str,
                     cache_size_fracs: list,
                     out_dir: str,
                     policy_names: list = None,
                     max_requests: int = None,
                     sample_stride: int = 1) -> list:
    """
    1 トレースに対して複数ポリシー × 複数キャッシュサイズでシミュレーションを実行する。

    【高速化ポイント】
      Pass 1 : ストリーミングで WSS・サイズ人気度統計を取得（メモリ O(ユニーク数)）
      Pass 2 : 全 frac × 全 policy のキャッシュを同時に 1 パスで処理
               （従来: frac ごとに N パス → 1 パスに削減）
      sample_stride > 1 : N 件に 1 件だけ処理して実行時間を削減
    """
    import pandas as pd
    if policy_names is None:
        policy_names = list(POLICIES.keys())

    trace_name = Path(trace_path).stem
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n{'='*60}")
    print(f"退避行列シミュレーション: {trace_name}")
    print(f"ポリシー: {', '.join(POLICY_LABELS.get(p, p) for p in policy_names)}")
    if sample_stride > 1:
        print(f"サンプリング: 1/{sample_stride}（約 {100/sample_stride:.1f}%）")
    print(f"{'='*60}")

    # ──────────────────────────────────────────
    # Pass 1: WSS スキャン + サイズ人気度統計
    # ──────────────────────────────────────────
    print("\n[Pass 1] WSS スキャン...")
    wss_bytes, class_byte_fracs, obj_info, n_req_p1 = _scan_wss_streaming(
        trace_path, sample_stride, max_requests
    )
    print(f"  WSS: {wss_bytes / 1e9:.3f} GB  "
          f"(サンプリング後 {n_req_p1:,} req)")

    print("\nサイズ-人気度分析...")
    sp_stats = _sp_stats_from_obj_info(obj_info)
    if len(sp_stats) > 0:
        sp_path = os.path.join(out_dir, f"{trace_name}_size_popularity.csv")
        sp_stats.to_csv(sp_path, index=False, encoding="utf-8-sig")
        print(sp_stats[["size_class_label", "n_objects",
                         "total_requests", "ohw_frac"]].to_string(index=False))

    # Pass 1 のオブジェクト辞書は以降不要なので解放
    del obj_info

    # ──────────────────────────────────────────
    # Pass 2: 全 frac × 全 policy を同時シミュレーション
    # ──────────────────────────────────────────
    print("\n[Pass 2] キャッシュシミュレーション（全サイズ×全ポリシー同時実行）...")

    # キャッシュインスタンスを全サイズ・全ポリシー分まとめて生成
    active_caches: dict = {}          # {(frac, pol_name): cache}
    partitioned_caches: dict = {}     # {frac: SizePartitionedCache}
    cap_info: dict = {}               # {frac: cap_bytes}

    for frac in cache_size_fracs:
        cap = max(int(wss_bytes * frac), 1)
        cap_info[frac] = cap
        for pol_name in policy_names:
            if pol_name in POLICIES:
                active_caches[(frac, pol_name)] = POLICIES[pol_name](cap, N_BINS)
        partitioned_caches[frac] = SizePartitionedCache(cap, class_byte_fracs, N_BINS)

    n_caches = len(active_caches) + len(partitioned_caches)
    print(f"  キャッシュ数: {n_caches}  "
          f"({len(cache_size_fracs)} サイズ × "
          f"{len(policy_names)} ポリシー + 分割キャッシュ)")

    n_req   = 0
    _prog   = 2_000_000

    for ts, oid, sz, nv in iter_oracle_general(trace_path, sample_stride, max_requests):
        # サイズクラスを高速計算
        bl = sz.bit_length()
        sc = max(0, min(bl - 10, N_BINS - 1)) if sz >= 1024 else 0

        # 全キャッシュを同時更新（oid は int のまま渡す）
        for cache in active_caches.values():
            cache.access(oid, sz, sc)
        for part in partitioned_caches.values():
            part.access(oid, sz, sc)

        n_req += 1
        if n_req % _prog == 0:
            print(f"    {n_req:,} req 処理済み", end="\r", flush=True)

    print(f"    シミュレーション完了: {n_req:,} req 処理          ")

    # ──────────────────────────────────────────
    # 結果収集・保存（frac ごとに CSV / グラフを出力）
    # ──────────────────────────────────────────
    class_labels = build_class_labels()
    label_list   = [class_labels.get(i, str(i)) for i in range(N_BINS)]
    all_metrics  = []

    for frac in cache_size_fracs:
        cap        = cap_info[frac]
        partitioned = partitioned_caches[frac]
        frac_caches = {pol: active_caches[(frac, pol)]
                       for pol in policy_names
                       if (frac, pol) in active_caches}

        print(f"\n  容量 {frac:.0%} × WSS = {cap / 1e6:.1f} MB")

        # 退避行列を CSV 保存（1×, 2×集約, 4×集約）
        for pol_name, cache in frac_caches.items():
            for n_merge, suffix in [(1, ""), (2, "_x2"), (4, "_x4")]:
                if n_merge == 1:
                    em       = cache.eviction_matrix
                    row_lbls = col_lbls = label_list
                else:
                    em = aggregate_matrix(cache.eviction_matrix, n_merge=n_merge)
                    ct = [2 ** i for i in range(10, 34, n_merge)]
                    cl = build_class_labels(ct)
                    nc = em.shape[0]
                    row_lbls = col_lbls = [cl.get(i, str(i)) for i in range(nc)]

                em_df = pd.DataFrame(em, index=row_lbls, columns=col_lbls)
                em_df.index.name = "evicted \\ inserting"
                csv_path = os.path.join(
                    out_dir,
                    f"{trace_name}_{pol_name}_eviction_matrix"
                    f"_cs{int(frac*100):02d}pct{suffix}.csv"
                )
                em_df.to_csv(csv_path, encoding="utf-8-sig")

        # グラフ生成
        if "lru" in frac_caches:
            plot_results(
                frac_caches["lru"], partitioned, sp_stats,
                trace_name, frac,
                os.path.join(out_dir,
                             f"{trace_name}_lru_eviction_heatmap"
                             f"_cs{int(frac*100):02d}pct.png")
            )

        plot_policy_comparison_heatmaps(
            frac_caches, trace_name, frac,
            os.path.join(out_dir,
                         f"{trace_name}_policy_comparison"
                         f"_cs{int(frac*100):02d}pct.png")
        )

        # サマリー行を生成
        row = {
            "trace":           trace_name,
            "cache_size_frac": frac,
            "partitioned_lru_hit_rate":      partitioned.hit_rate(),
            "partitioned_lru_byte_miss_rate": partitioned.byte_miss_rate(),
        }
        for pol_name, cache in frac_caches.items():
            asym = cache.asymmetry_score()
            row[f"{pol_name}_hit_rate"]       = cache.hit_rate()
            row[f"{pol_name}_byte_miss_rate"]  = cache.byte_miss_rate()
            row[f"{pol_name}_asymmetry_score"] = asym
            row[f"{pol_name}_total_evictions"] = int(cache.eviction_matrix.sum())
            print(f"    {POLICY_LABELS.get(pol_name, pol_name):<8}: "
                  f"HR={cache.hit_rate():.4f}  "
                  f"BMR={cache.byte_miss_rate():.4f}  "
                  f"asym={asym:.4f}")

        lru_cache = frac_caches.get("lru")
        if lru_cache:
            row["hit_rate_improvement"] = partitioned.hit_rate() - lru_cache.hit_rate()
            row["bmr_improvement"]      = lru_cache.byte_miss_rate() - partitioned.byte_miss_rate()
        all_metrics.append(row)

    summary_df   = pd.DataFrame(all_metrics)
    summary_path = os.path.join(out_dir, f"{trace_name}_mechanism_summary.csv")
    summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
    return all_metrics


# ═════════════════════════════════════════════
# エントリーポイント
# ═════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="クロスサイズ退避行列によるキャッシュメカニズム分析",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
【大規模トレース向けオプション】
  --sample-stride N   N 件に 1 件だけ処理（N=10 で 1/10、N=100 で 1/100）
                      退避行列の相対パターンはほぼ保たれる。
                      34GiB のトレースには --sample-stride 20〜100 が実用的。
  --max-requests M    先頭 M 件だけ読み込む（--sample-stride と併用可）
  --jobs J            トレースファイルを J プロセスで並列処理（複数ファイル時のみ有効）

【実行例】
  # 単一ファイル、1/10 サンプリング
  python eviction_matrix_sim.py --trace ./traces/cdn.oracleGeneral.zst \\
      --sample-stride 10 --out ./out

  # 複数ファイルを 4 並列
  python eviction_matrix_sim.py --trace-dir ./traces \\
      --sample-stride 20 --jobs 4 --out ./out
"""
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--trace",     type=str, help="単一トレースファイル")
    group.add_argument("--trace-dir", type=str, help="トレースディレクトリ（一括処理）")

    parser.add_argument(
        "--cache-sizes", type=float, nargs="+",
        default=[0.01, 0.05, 0.1, 0.2, 0.3],
        help="WSS に対するキャッシュサイズ比率（デフォルト: 0.01 0.05 0.1 0.2 0.3）",
    )
    parser.add_argument(
        "--policies", type=str, nargs="+",
        default=list(POLICIES.keys()),
        choices=list(POLICIES.keys()),
        help="実行するポリシー（デフォルト: 全ポリシー）",
    )
    parser.add_argument("--out", type=str, default="./output/eviction_matrix",
                        help="出力ディレクトリ")
    parser.add_argument(
        "--sample-stride", type=int, default=1, metavar="N",
        help="N 件に 1 件だけ処理（大規模トレース向け。デフォルト: 1=全件）",
    )
    parser.add_argument(
        "--max-requests", type=int, default=None, metavar="M",
        help="先頭 M 件だけ処理（--sample-stride と併用可）",
    )
    parser.add_argument(
        "--jobs", type=int, default=1, metavar="J",
        help="並列処理数（--trace-dir 使用時のみ有効。デフォルト: 1）",
    )

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
            print(f"エラー: {td} にトレースが見つかりません"); sys.exit(1)

    os.makedirs(args.out, exist_ok=True)

    def _run(tf_path: str) -> list:
        return run_single_trace(
            tf_path,
            cache_size_fracs=args.cache_sizes,
            out_dir=args.out,
            policy_names=args.policies,
            max_requests=args.max_requests,
            sample_stride=args.sample_stride,
        )

    all_results = []

    n_jobs = min(max(args.jobs, 1), len(trace_files))
    if n_jobs > 1:
        # 複数プロセス並列（multiprocessing）
        from multiprocessing import Pool
        print(f"\n{len(trace_files)} トレースを {n_jobs} プロセスで並列処理します")
        tf_strs = [str(tf) for tf in trace_files]
        with Pool(processes=n_jobs) as pool:
            for res in pool.imap_unordered(_run, tf_strs):
                all_results.extend(res)
    else:
        for tf in trace_files:
            all_results.extend(_run(str(tf)))

    if all_results:
        import pandas as pd
        agg      = pd.DataFrame(all_results)
        agg_path = os.path.join(args.out, "ALL_TRACES_mechanism_summary.csv")
        agg.to_csv(agg_path, index=False, encoding="utf-8-sig")
        print(f"\n全トレースサマリー: {agg_path}")


if __name__ == "__main__":
    main()
