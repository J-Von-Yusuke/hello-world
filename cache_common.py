"""
cache_common.py
===============
全スクリプト共通のユーティリティ

【binning 設計方針】
  1KiB (2^10) から 8GiB (2^33) まで 2 の指数乗ごとに区切る。
  → 24 本の閾値 → 25 個の bin

  bin 0  : size < 1KiB           (< 2^10)
  bin 1  : 1KiB  ≤ size < 2KiB  (2^10 – 2^11)
  bin 2  : 2KiB  ≤ size < 4KiB  (2^11 – 2^12)
  ...
  bin 23 : 4GiB  ≤ size < 8GiB  (2^32 – 2^33)
  bin 24 : 8GiB ≤ size           (≥ 2^33)

  利点:
    - 事前仮定なし。自然な境界をデータから発見できる
    - 隣接 bin を後から統合できる（4倍幅 bin = 2倍幅 bin × 2 を足すだけ）
    - キャッシュ研究の標準的な手法で他論文と比較しやすい

【使い方】
  from cache_common import POW2_THRESHOLDS, get_size_class, build_class_labels
  from cache_common import aggregate_matrix, aggregate_df_by_log2_group
"""

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────
# デフォルト閾値: 1KiB〜8GiB を 2 の指数乗で
# ─────────────────────────────────────────────
POW2_THRESHOLDS = [2 ** i for i in range(10, 34)]
# = [1024, 2048, 4096, ..., 4294967296, 8589934592]
# 24 本の閾値 → 25 bin

N_BINS = len(POW2_THRESHOLDS) + 1  # 25


# ─────────────────────────────────────────────
# サイズクラス判定
# ─────────────────────────────────────────────

def get_size_class(size: int, thresholds: list = None) -> int:
    """
    size が属する bin インデックスを返す。
    thresholds が None のときは POW2_THRESHOLDS を使う。

    2 の指数乗閾値に対しては bit_length() で O(1) 判定。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS

    # 高速パス: 閾値が 2 の指数乗であれば bit_length で計算
    if thresholds is POW2_THRESHOLDS or thresholds == POW2_THRESHOLDS:
        if size < thresholds[0]:          # < 1KiB
            return 0
        if size >= thresholds[-1]:        # ≥ 8GiB
            return len(thresholds)
        # size が属する bin = bit_length(size) - 10
        # 例: 1024 (2^10) → bit_length=11 → 11-10=1 → bin 1
        bl = size.bit_length()
        return bl - 10  # = log2(floor(size)) - 9

    # 汎用パス
    for i, t in enumerate(thresholds):
        if size < t:
            return i
    return len(thresholds)


def get_size_class_vectorized(sizes: np.ndarray,
                               thresholds: list = None) -> np.ndarray:
    """numpy 配列に対してサイズクラスを一括計算する（高速）"""
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    t = np.array(thresholds, dtype=np.int64)
    # searchsorted: sizes[i] が入る最初のインデックスを返す
    return np.searchsorted(t, sizes, side="right").astype(np.int32)


# ─────────────────────────────────────────────
# ラベル生成
# ─────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    """バイト数を人間が読みやすい文字列に変換する"""
    for div, unit in [(1 << 30, "GiB"), (1 << 20, "MiB"),
                       (1 << 10, "KiB"), (1, "B")]:
        if b >= div and b % div == 0:
            return f"{b // div}{unit}"
    return f"{b}B"


def build_class_labels(thresholds: list = None) -> dict:
    """
    {bin_index: label_string} の辞書を返す。

    例 (POW2_THRESHOLDS):
      {0: "<1KiB", 1: "1-2KiB", 2: "2-4KiB", ..., 24: "≥8GiB"}
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    boundaries = [0] + list(thresholds) + [None]
    labels = {}
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if lo == 0 and hi is not None:
            labels[i] = f"<{_fmt_bytes(hi)}"
        elif hi is None:
            labels[i] = f"≥{_fmt_bytes(lo)}"
        else:
            lo_s, hi_s = _fmt_bytes(lo), _fmt_bytes(hi)
            # 単位が同じなら数値だけ省略して "1-2KiB" のように表示
            lo_val = lo_s.rstrip("BGiKMib")
            hi_val = hi_s.rstrip("BGiKMib")
            lo_unit = lo_s[len(lo_val):]
            hi_unit = hi_s[len(hi_val):]
            if lo_unit == hi_unit:
                labels[i] = f"{lo_val}-{hi_val}{hi_unit}"
            else:
                labels[i] = f"{lo_s}-{hi_s}"
    return labels


# ─────────────────────────────────────────────
# bin の集約ユーティリティ
# ─────────────────────────────────────────────

def aggregate_bins(n_merge: int, thresholds: list = None) -> list:
    """
    隣接 n_merge 本の bin を1つに統合した粗い閾値リストを返す。

    例: n_merge=2 → 2倍幅 (1-2KiB + 2-4KiB → 1-4KiB)
        n_merge=4 → 4倍幅

    戻り値: 集約後の閾値リスト
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    # 元の閾値を n_merge おきに間引く
    return thresholds[n_merge - 1::n_merge]


def aggregate_matrix(matrix: np.ndarray,
                     thresholds: list = None,
                     n_merge: int = 2) -> np.ndarray:
    """
    退避行列（n_bins × n_bins）を n_merge 本ごとに集約する。

    例: 25×25 の行列を n_merge=2 で集約 → 13×13 に縮小。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    n = matrix.shape[0]
    coarse_thresholds = aggregate_bins(n_merge, thresholds)
    n_coarse = len(coarse_thresholds) + 1

    result = np.zeros((n_coarse, n_coarse), dtype=matrix.dtype)
    for i_fine in range(n):
        i_coarse = min(i_fine // n_merge, n_coarse - 1)
        for j_fine in range(n):
            j_coarse = min(j_fine // n_merge, n_coarse - 1)
            result[i_coarse, j_coarse] += matrix[i_fine, j_fine]
    return result


def aggregate_df_by_merge(df: pd.DataFrame,
                          size_class_col: str = "size_class",
                          thresholds: list = None,
                          n_merge: int = 2) -> pd.DataFrame:
    """
    DataFrame のサイズクラス列を n_merge 本ごとに集約する。
    集約後のクラスインデックスと対応ラベルを追加した DataFrame を返す。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    coarse_thresholds = aggregate_bins(n_merge, thresholds)
    coarse_labels = build_class_labels(coarse_thresholds)

    df = df.copy()
    df["coarse_class"] = (df[size_class_col] // n_merge).clip(
        upper=len(coarse_thresholds)
    )
    df["coarse_label"] = df["coarse_class"].map(coarse_labels)
    return df


# ─────────────────────────────────────────────
# 退避行列の可視化ヘルパー
# ─────────────────────────────────────────────

def plot_eviction_heatmap(ax, matrix: np.ndarray,
                           thresholds: list = None,
                           title: str = "",
                           normalize_cols: bool = True,
                           cmap: str = "YlOrRd",
                           label_every: int = 2):
    """
    退避行列をヒートマップとして ax に描画する。

    normalize_cols=True  → 各列（挿入クラス）の合計で正規化
    label_every          → X/Y 軸ラベルを何本おきに表示するか（25 bin では 2 推奨）
    """
    import matplotlib.pyplot as plt

    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    labels = build_class_labels(thresholds)
    n = matrix.shape[0]
    label_list = [labels.get(i, str(i)) for i in range(n)]

    if normalize_cols:
        col_sums = matrix.sum(axis=0, keepdims=True)
        col_sums = np.where(col_sums == 0, 1, col_sums)
        plot_data = matrix / col_sums
        vmin, vmax = 0, 1
    else:
        plot_data = matrix.astype(float)
        vmin, vmax = None, None

    im = ax.imshow(plot_data, cmap=cmap, aspect="auto",
                   vmin=vmin, vmax=vmax, interpolation="nearest")

    # 軸ラベル（間引き表示）
    tick_pos  = list(range(0, n, label_every))
    tick_lbls = [label_list[i] for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_lbls, rotation=45, ha="right", fontsize=7)
    ax.set_yticks(tick_pos)
    ax.set_yticklabels(tick_lbls, fontsize=7)
    ax.set_xlabel("挿入クラス（キャッシュミスを起こしたオブジェクト）", fontsize=9)
    ax.set_ylabel("退避クラス（追い出されたオブジェクト）", fontsize=9)
    ax.set_title(title, fontsize=9)

    return im


# ─────────────────────────────────────────────
# OracleGeneral 読み込み（共通）
# ─────────────────────────────────────────────
import struct as _struct

OG_FORMAT      = "=IQIq"
OG_RECORD_SIZE = _struct.calcsize(OG_FORMAT)  # 24 bytes


def load_oracle_general(path: str,
                         max_requests: int = None) -> pd.DataFrame:
    """
    OracleGeneral バイナリ形式を読み込む。
    struct { uint32_t ts; uint64_t obj_id;
             uint32_t obj_size; int64_t next_access_vtime; }

    next_access_vtime == -1: このアクセス以降に再アクセスなし
    """
    with open(path, "rb") as f:
        raw = f.read()

    n = len(raw) // OG_RECORD_SIZE
    if max_requests:
        n = min(n, max_requests)

    ts_arr       = np.empty(n, dtype=np.uint32)
    obj_id_arr   = np.empty(n, dtype=np.uint64)
    size_arr     = np.empty(n, dtype=np.uint32)
    vtime_arr    = np.empty(n, dtype=np.int64)

    valid = 0
    for i in range(n):
        off = i * OG_RECORD_SIZE
        ts, oid, sz, nv = _struct.unpack_from(OG_FORMAT, raw, off)
        if sz > 0:
            ts_arr[valid]    = ts
            obj_id_arr[valid] = oid
            size_arr[valid]  = sz
            vtime_arr[valid] = nv
            valid += 1

    df = pd.DataFrame({
        "vtime":              np.arange(valid, dtype=np.int64),
        "timestamp":          ts_arr[:valid].astype(np.int64),
        "obj_id":             obj_id_arr[:valid].astype(str),
        "obj_size":           size_arr[:valid].astype(np.int64),
        "next_access_vtime":  vtime_arr[:valid],
    })

    # サイズクラスをここで一括付与（vectorized）
    df["size_class"] = get_size_class_vectorized(
        df["obj_size"].values, POW2_THRESHOLDS
    )

    ohw_frac = float((df["next_access_vtime"] == -1).mean())
    print(f"  読み込み完了: {len(df):,} req  "
          f"ユニーク={df['obj_id'].nunique():,}  "
          f"OHW={ohw_frac:.3%}  "
          f"サイズ=[{df['obj_size'].min()}, {df['obj_size'].max()}]B")
    return df


def load_trace(path: str, max_requests: int = None) -> pd.DataFrame:
    """
    ファイル形式を自動判定して読み込む。
    OracleGeneral (.oracleGeneral / .bin) と CSV に対応。
    """
    from pathlib import Path
    p = Path(path)
    suffix = p.suffix.lower()

    is_binary = (
        suffix in {".oraclegeneral", ".bin", ".lcs"}
        or (p.stat().st_size % OG_RECORD_SIZE == 0 and suffix not in {".csv", ".tsv", ".txt"})
    )

    if is_binary:
        print(f"  形式: OracleGeneral バイナリ ({p.name})")
        return load_oracle_general(path, max_requests)

    # CSV フォールバック
    print(f"  形式: CSV ({p.name})")
    sep = "\t" if suffix == ".tsv" else ","
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline().strip().split(sep)
    has_header = not all(_try_float(v) for v in first[:3])

    df = pd.read_csv(path, sep=sep,
                     header=0 if has_header else None,
                     names=None if has_header else ["timestamp", "obj_id", "obj_size"],
                     usecols=[0, 1, 2],
                     nrows=max_requests,
                     on_bad_lines="skip", low_memory=True)
    df.columns = ["timestamp", "obj_id", "obj_size"]
    df["obj_size"] = pd.to_numeric(df["obj_size"], errors="coerce").fillna(0).astype(int)
    df = df[df["obj_size"] > 0].reset_index(drop=True)
    df["vtime"] = df.index.astype(np.int64)
    df["next_access_vtime"] = np.int64(-2)   # CSV には情報なし
    df["size_class"] = get_size_class_vectorized(df["obj_size"].values)
    return df


def _try_float(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False
