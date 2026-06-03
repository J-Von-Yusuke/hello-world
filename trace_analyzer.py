#!/usr/bin/env python3
"""
trace_analyzer.py  ―  キャッシュトレース分析 & RegionSplit 最適閾値候補推定ツール
====================================================================================

研究の理論的根拠 (research_summary.md):
  ΔBMR ≈ B_protect × (1-OHW_protect) × D_size × C_partition
  最適閾値候補: OHW 急上昇点 かつ Score(T) が最大となる閾値

対応トレース形式:
  --format oraclegeneral (デフォルト)
      binary: uint32 timestamp | uint64 obj_id | uint32 obj_size | int32 extra  (20 bytes/req)
  --format csv
      header 行あり: timestamp,obj_id,size[,追加列...]
      または header なし (--no-header): 列0=timestamp, 列1=obj_id, 列2=size

使用例:
  python trace_analyzer.py trace.bin
  python trace_analyzer.py trace.bin --format oraclegeneral --sample 2000000
  python trace_analyzer.py trace.csv --format csv --sep ','
  python trace_analyzer.py trace.bin --cache-ratio 0.10
"""

import sys
import os
import math
import struct
import argparse
import csv
from collections import defaultdict

# ─────────────────────────────────────────────
#  定数・ユーティリティ
# ─────────────────────────────────────────────
N_BINS = 30  # bins[i] = [2^i, 2^(i+1)) bytes

def bin_idx(size: int) -> int:
    """サイズ(bytes)をビンインデックスに変換"""
    if size <= 0:
        return 0
    return min(int(math.log2(max(size, 1))), N_BINS - 1)

def bin_label(i: int) -> str:
    lo = 2 ** i
    hi = 2 ** (i + 1)
    def fmt(b):
        if b >= 1 << 30: return f"{b >> 30}GiB"
        if b >= 1 << 20: return f"{b >> 20}MiB"
        if b >= 1 << 10: return f"{b >> 10}KiB"
        return f"{b}B"
    return f"{fmt(lo)}~{fmt(hi)}"

def bin_midpoint(i: int) -> float:
    """ビン内の代表サイズ（幾何平均）"""
    return math.sqrt(2**i * 2**(i+1))

def human_bytes(b: float) -> str:
    for unit, div in [("GiB", 1<<30), ("MiB", 1<<20), ("KiB", 1<<10)]:
        if b >= div:
            return f"{b/div:.2f}{unit}"
    return f"{int(b)}B"


# ─────────────────────────────────────────────
#  トレース読み込み
# ─────────────────────────────────────────────
ORACLE_RECORD = struct.Struct("=IQIi")   # timestamp(4) obj_id(8) size(4) extra(4) = 20bytes
ORACLE_SIZE   = ORACLE_RECORD.size       # 20

def iter_oraclegeneral(path: str):
    """oraclegeneral binary から (timestamp, obj_id, size) を yield"""
    with open(path, "rb") as f:
        while True:
            buf = f.read(ORACLE_SIZE)
            if len(buf) < ORACLE_SIZE:
                break
            ts, oid, sz, _ = ORACLE_RECORD.unpack(buf)
            yield ts, oid, sz

def iter_csv(path: str, sep: str = ",", has_header: bool = True,
             col_ts: int = 0, col_id: int = 1, col_sz: int = 2):
    """CSV から (timestamp, obj_id, size) を yield"""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=sep)
        if has_header:
            next(reader, None)
        for row in reader:
            if len(row) <= max(col_ts, col_id, col_sz):
                continue
            try:
                ts  = int(float(row[col_ts]))
                oid = int(row[col_id])
                sz  = int(float(row[col_sz]))
                yield ts, oid, sz
            except ValueError:
                continue


# ─────────────────────────────────────────────
#  パス 1: 全リクエスト統計（ストリーミング処理）
# ─────────────────────────────────────────────
def pass1_global(iterator, max_reuse_sample: int):
    """
    全リクエストをストリーミングで処理し以下を収集する:
      - ビン別リクエスト数・バイト量
      - オブジェクト別アクセス回数（OHW 判定用）
      - 最初の max_reuse_sample 件の再利用間隔データ（アクセス数ベース）
    """
    bin_req   = [0] * N_BINS
    bin_bytes = [0] * N_BINS

    obj_count = defaultdict(int)   # obj_id -> アクセス回数
    obj_size  = {}                 # obj_id -> size（最後に見たサイズ）
    obj_last  = {}                 # obj_id -> 最後のアクセスインデックス（再利用間隔用）

    reuse_intervals = defaultdict(list)  # bin_idx -> [interval, ...]
    sample_done     = False
    total_req       = 0

    for idx, (ts, oid, sz) in enumerate(iterator):
        total_req += 1
        bi = bin_idx(sz)
        bin_req[bi]   += 1
        bin_bytes[bi] += sz

        obj_count[oid] += 1
        obj_size[oid]   = sz

        # 再利用間隔（サンプル範囲のみ）
        if not sample_done:
            if oid in obj_last:
                reuse_intervals[bi].append(idx - obj_last[oid])
            obj_last[oid] = idx
            if idx + 1 >= max_reuse_sample:
                sample_done = True

    return bin_req, bin_bytes, obj_count, obj_size, reuse_intervals, total_req


# ─────────────────────────────────────────────
#  パス 2: OHW 分布（ビン別）
# ─────────────────────────────────────────────
def compute_ohw_per_bin(obj_count: dict, obj_size: dict):
    """ビン別の OHW オブジェクト数・ユニークオブジェクト数を集計"""
    bin_unique = [0] * N_BINS
    bin_ohw    = [0] * N_BINS
    for oid, cnt in obj_count.items():
        bi = bin_idx(obj_size.get(oid, 0))
        bin_unique[bi] += 1
        if cnt == 1:
            bin_ohw[bi] += 1
    return bin_unique, bin_ohw


# ─────────────────────────────────────────────
#  閾値スコアリング
# ─────────────────────────────────────────────
def score_thresholds(bin_stats: list):
    """
    各ビン境界を閾値候補として Score を計算する。

    Score(T) = B_protect(T) × (1-OHW_protect(T)) × D_size(T) × C_partition(T)

    戻り値: List of dicts, sorted by score descending
    """
    total_bytes = sum(b["byte_count"] for b in bin_stats if b["req_count"] > 0)
    if total_bytes == 0:
        return []

    active = [b for b in bin_stats if b["req_count"] > 0]
    if len(active) < 2:
        return []

    results = []

    for t_idx in range(1, N_BINS):
        # 閾値 = 2^t_idx bytes
        threshold = 2 ** t_idx

        small = [b for b in active if b["bin_hi"] <= threshold]
        large = [b for b in active if b["bin_lo"] >= threshold]

        if not small or not large:
            continue

        # ── 小プール側 ──────────────────────────────
        B_small = sum(b["byte_count"] for b in small) / total_bytes
        total_small_bytes = sum(b["byte_count"] for b in small)

        # バイト加重 OHW 率（小プール）
        if total_small_bytes > 0:
            ohw_small = sum(b["byte_count"] * b["ohw_rate"] for b in small) / total_small_bytes
        else:
            ohw_small = 0.0

        # 保護価値スコア
        protect_value = B_small * (1.0 - ohw_small)

        # ── 大プール側 ──────────────────────────────
        B_large = sum(b["byte_count"] for b in large) / total_bytes
        total_large_bytes = sum(b["byte_count"] for b in large)

        if total_large_bytes > 0:
            ohw_large = sum(b["byte_count"] * b["ohw_rate"] for b in large) / total_large_bytes
        else:
            ohw_large = 0.0

        # ── サイズ比（変位係数 D_size）──────────────
        req_small = sum(b["req_count"] for b in small)
        req_large = sum(b["req_count"] for b in large)

        mean_sz_small = total_small_bytes / req_small if req_small > 0 else 1
        mean_sz_large = total_large_bytes / req_large if req_large > 0 else 1
        D_size = mean_sz_large / mean_sz_small

        # ── プール内競合リスク (C_partition) ─────────
        # 大プール内の OHW 分散が小さく、かつ OHW が低い場合にペナルティ
        if len(large) >= 2:
            ohw_large_vals = [b["ohw_rate"] for b in large]
            ohw_variance   = max(ohw_large_vals) - min(ohw_large_vals)
            intra_competition_risk = (ohw_variance < 0.15) and (ohw_large < 0.40)
        else:
            intra_competition_risk = False
        C_partition = 0.15 if intra_competition_risk else 1.0

        # ── OHW 勾配 ─────────────────────────────────
        last_small_ohw  = small[-1]["ohw_rate"]
        first_large_ohw = large[0]["ohw_rate"]
        ohw_gradient    = first_large_ohw - last_small_ohw

        # ── スコア ─────────────────────────────────
        score = protect_value * D_size * C_partition

        # ── ΔBMR 推定値 ─────────────────────────────
        # 実験キャリブレーション: k ≈ 0.07 (%pt per %)
        # 注: キャッシュサイズへの依存性があり目安値
        k = 0.07
        delta_bmr_est = -k * protect_value * D_size * C_partition

        results.append({
            "threshold_bytes"  : threshold,
            "threshold_label"  : human_bytes(threshold),
            "score"            : score,
            "protect_value"    : protect_value,      # B × (1-OHW_small)
            "B_small_pct"      : B_small * 100,
            "ohw_small"        : ohw_small,
            "B_large_pct"      : B_large * 100,
            "ohw_large"        : ohw_large,
            "D_size"           : D_size,
            "C_partition"      : C_partition,
            "ohw_gradient"     : ohw_gradient,
            "intra_risk"       : intra_competition_risk,
            "delta_bmr_est"    : delta_bmr_est,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# ─────────────────────────────────────────────
#  ワークロード類型判定
# ─────────────────────────────────────────────
def classify_workload(bin_stats: list, global_ohw: float, top_thresholds: list):
    """
    実験結果に基づくワークロード類型を推定する。
    類型: block_cache / cdn_inverted / uniform_low_ohw / small_heavy / neutral
    """
    active = [b for b in bin_stats if b["req_count"] > 0]
    if not active:
        return "unknown", []

    # OHW の傾向（小→大で増加 / 減少 / 均一）
    ohw_vals = [b["ohw_rate"] for b in active]
    sizes    = [b["bin_mid"] for b in active]

    # 最小・最大サイズ帯の OHW 差
    ohw_small_bins = [b["ohw_rate"] for b in active[:len(active)//2]]
    ohw_large_bins = [b["ohw_rate"] for b in active[len(active)//2:]]
    avg_ohw_small  = sum(ohw_small_bins) / len(ohw_small_bins) if ohw_small_bins else 0
    avg_ohw_large  = sum(ohw_large_bins) / len(ohw_large_bins) if ohw_large_bins else 0
    ohw_diff       = avg_ohw_large - avg_ohw_small   # 正=大サイズほどOHW高

    ohw_variance_all = max(ohw_vals) - min(ohw_vals) if ohw_vals else 0

    # バイト分布の重心（大サイズ寄りか小サイズ寄りか）
    total_bytes = sum(b["byte_count"] for b in active)
    byte_weighted_log2_size = sum(b["byte_count"] * math.log2(max(b["bin_mid"], 1))
                                  for b in active) / max(total_bytes, 1)
    # 大サイズ寄り: >= 20 (≈ 1MB)

    notes = []

    if ohw_variance_all < 0.15 and global_ohw < 0.35:
        cls = "uniform_low_ohw"
        notes.append("全帯域でOHWが均一に低い → RegionSplitは中立〜有害")
        notes.append("Tencent Block Cache 型（均一高再利用性）に類似")
    elif ohw_diff >= 0.20 and byte_weighted_log2_size >= 18:  # >= 256KB
        cls = "block_cache"
        notes.append("大サイズほどOHW高 かつ 大バイト帯に分布 → RegionSplit 有効")
        notes.append("Block Cache DS3 型に類似（閾値スコア上位を参照）")
    elif ohw_diff <= -0.20:
        cls = "cdn_inverted"
        notes.append("大サイズほどOHW低（価値ある）→ RegionSplit は中立〜有害")
        notes.append("Meta CDN 型に類似")
    elif byte_weighted_log2_size < 14:  # < 16KB
        cls = "small_heavy"
        notes.append("小サイズコンテンツが支配的 → 帯域間干渉が弱く RegionSplit 効果は限定的")
    else:
        cls = "neutral"
        notes.append("明確な類型に該当しない → 閾値スコアを参考に個別判断が必要")

    # 最良スコアの閾値候補からの ΔBMR 推定
    if top_thresholds:
        best = top_thresholds[0]
        notes.append(f"最良閾値候補 {best['threshold_label']} での ΔBMR 推定: "
                     f"{best['delta_bmr_est']:+.2f}%pt（目安値, 実際はキャッシュサイズに依存）")

    return cls, notes


# ─────────────────────────────────────────────
#  出力
# ─────────────────────────────────────────────
SEP = "─" * 80

def print_section(title):
    print(f"\n{'═'*80}")
    print(f"  {title}")
    print(f"{'═'*80}")

def print_size_distribution(bin_stats, total_req, total_bytes):
    print_section("1. サイズ分布")
    print(f"  総リクエスト数: {total_req:,}  総バイト量: {human_bytes(total_bytes)}")
    print()
    hdr = f"  {'サイズ帯':<18} {'Req数':>10} {'Req%':>7} {'Byte量':>12} {'Byte%':>7} {'OHW率':>7} {'B×(1-OHW)':>11}"
    print(hdr)
    print("  " + "─" * 78)
    for b in bin_stats:
        if b["req_count"] == 0:
            continue
        b_1r = b["byte_share"] * (1.0 - b["ohw_rate"])
        print(f"  {b['label']:<18} {b['req_count']:>10,} {b['req_share']*100:>6.2f}%"
              f" {human_bytes(b['byte_count']):>12} {b['byte_share']*100:>6.2f}%"
              f" {b['ohw_rate']*100:>6.1f}%  {b_1r*100:>9.2f}%")

def print_ohw_analysis(bin_stats, global_ohw, total_unique):
    print_section("2. OHW（One-hit Wonder）分析")
    print(f"  全体 OHW 率: {global_ohw*100:.2f}%  ユニークオブジェクト数: {total_unique:,}")
    print()
    print(f"  {'サイズ帯':<18} {'ユニーク数':>12} {'OHW数':>10} {'OHW率':>8}  OHW傾向")
    print("  " + "─" * 70)
    prev_ohw = None
    for b in bin_stats:
        if b["unique_count"] == 0:
            continue
        arrow = ""
        if prev_ohw is not None:
            diff = b["ohw_rate"] - prev_ohw
            arrow = " ↑" if diff > 0.05 else (" ↓" if diff < -0.05 else "  →")
        print(f"  {b['label']:<18} {b['unique_count']:>12,} {b['ohw_count']:>10,}"
              f" {b['ohw_rate']*100:>7.1f}%  {arrow}")
        prev_ohw = b["ohw_rate"]

def print_reuse_analysis(bin_stats, total_req):
    print_section("3. 再利用間隔分析（先頭サンプル）")
    print(f"  ※ 再利用間隔 = 同一オブジェクトへの連続アクセス間のリクエスト数（スタック距離近似）")
    print()
    print(f"  {'サイズ帯':<18} {'再利用件数':>10} {'中央値':>10} {'p75':>10} {'p95':>10}")
    print("  " + "─" * 68)
    for b in bin_stats:
        if not b["reuse_intervals"]:
            continue
        ivs = sorted(b["reuse_intervals"])
        n   = len(ivs)
        med = ivs[n // 2]
        p75 = ivs[int(n * 0.75)]
        p95 = ivs[int(n * 0.95)]
        print(f"  {b['label']:<18} {n:>10,} {med:>10,} {p75:>10,} {p95:>10,}")
    print()
    print("  ※ p95 > キャッシュ収容可能オブジェクト数 の帯域は LRU でも高ミス率になりやすい")

def print_threshold_candidates(candidates, top_n=5):
    print_section("4. 最適閾値候補（RegionSplit BMR 改善スコア順）")
    print("  スコア = B_protect×(1-OHW_protect) × D_size × C_partition")
    print("  ΔBMR推定 = -0.07 × スコア（目安; 実キャッシュ比率で変動）")
    print()
    hdr = (f"  {'閾値':<10} {'スコア':>8} {'B_small%':>9} {'OHW小':>8}"
           f" {'D_size':>8} {'OHW勾配':>9} {'ΔBMR推定':>10}  備考")
    print(hdr)
    print("  " + "─" * 90)
    shown = 0
    for c in candidates:
        if shown >= top_n:
            break
        note = ""
        if c["intra_risk"]:
            note = "[大プール内競合リスク]"
        if c["ohw_gradient"] > 0.20:
            note += " [OHW急上昇]"
        print(f"  {c['threshold_label']:<10} {c['score']:>8.3f}"
              f" {c['B_small_pct']:>8.1f}% {c['ohw_small']*100:>7.1f}%"
              f" {c['D_size']:>8.1f} {c['ohw_gradient']*100:>8.1f}%"
              f" {c['delta_bmr_est']:>9.3f}%pt  {note}")
        shown += 1

    # BMR が悪化する可能性があるものも表示
    harmful = [c for c in candidates if c["intra_risk"] and c["score"] < 0.5]
    if harmful:
        print()
        print("  ⚠ 以下の閾値は大プール内競合リスクがあり BMR が悪化する可能性:")
        for c in harmful[:3]:
            print(f"     {c['threshold_label']}: C_partition={c['C_partition']:.2f},"
                  f" 大プール OHW={c['ohw_large']*100:.1f}%")

def print_workload_summary(wl_class, notes, bin_stats):
    print_section("5. ワークロード類型と推奨")
    class_labels = {
        "block_cache"    : "Block Cache型（大サイズ高OHW）→ RegionSplit 有効",
        "cdn_inverted"   : "CDN型（大サイズ低OHW）→ RegionSplit 中立〜有害",
        "uniform_low_ohw": "均一低OHW型 → RegionSplit 中立〜有害",
        "small_heavy"    : "小サイズ特化型 → RegionSplit 効果限定",
        "neutral"        : "中間型 → 閾値スコアで個別判断",
        "unknown"        : "不明",
    }
    print(f"  推定類型: {class_labels.get(wl_class, wl_class)}")
    print()
    for note in notes:
        print(f"  • {note}")
    print()
    print("  ──────────────────────────────────────────────────")
    print("  ⚠ ΔBMR 推定値はキャッシュサイズ比率に依存します:")
    print("     キャッシュが大きいほど最適閾値は右シフト（大きい値）します。")
    print("     実際のキャッシュ比率に合わせてシミュレーションで確認してください。")


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────
def build_bin_stats(bin_req, bin_bytes, bin_unique, bin_ohw, reuse_intervals,
                    total_req, total_bytes):
    stats = []
    for i in range(N_BINS):
        req   = bin_req[i]
        byt   = bin_bytes[i]
        uniq  = bin_unique[i]
        ohw_n = bin_ohw[i]
        stats.append({
            "bin_idx"      : i,
            "label"        : bin_label(i),
            "bin_lo"       : 2 ** i,
            "bin_hi"       : 2 ** (i + 1),
            "bin_mid"      : bin_midpoint(i),
            "req_count"    : req,
            "req_share"    : req / total_req if total_req else 0,
            "byte_count"   : byt,
            "byte_share"   : byt / total_bytes if total_bytes else 0,
            "unique_count" : uniq,
            "ohw_count"    : ohw_n,
            "ohw_rate"     : ohw_n / uniq if uniq else 0.0,
            "reuse_intervals": reuse_intervals.get(i, []),
        })
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="キャッシュトレース分析 & RegionSplit 最適閾値候補推定",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("trace", help="トレースファイルパス")
    parser.add_argument("--format", choices=["oraclegeneral", "csv"],
                        default="oraclegeneral",
                        help="トレース形式 (デフォルト: oraclegeneral)")
    parser.add_argument("--sep", default=",",
                        help="CSV 区切り文字 (デフォルト: ',')")
    parser.add_argument("--no-header", action="store_true",
                        help="CSV にヘッダー行がない場合に指定")
    parser.add_argument("--col-ts",  type=int, default=0, help="CSV: timestamp 列インデックス")
    parser.add_argument("--col-id",  type=int, default=1, help="CSV: obj_id 列インデックス")
    parser.add_argument("--col-sz",  type=int, default=2, help="CSV: size 列インデックス")
    parser.add_argument("--sample", type=int, default=2_000_000,
                        help="再利用間隔を計算するリクエスト数上限 (デフォルト: 2,000,000)")
    parser.add_argument("--top", type=int, default=5,
                        help="表示する閾値候補数 (デフォルト: 5)")
    parser.add_argument("--cache-ratio", type=float, default=None,
                        help="キャッシュ容量/ワークロード総バイト (例: 0.10). "
                             "指定するとキャッシュ収容可能オブジェクト数の目安を表示")
    args = parser.parse_args()

    if not os.path.exists(args.trace):
        print(f"[ERROR] ファイルが見つかりません: {args.trace}", file=sys.stderr)
        sys.exit(1)

    # ── トレース読み込み ────────────────────────
    print(f"[INFO] トレース読み込み中: {args.trace}")
    if args.format == "oraclegeneral":
        iterator = iter_oraclegeneral(args.trace)
    else:
        iterator = iter_csv(args.trace, sep=args.sep,
                            has_header=not args.no_header,
                            col_ts=args.col_ts, col_id=args.col_id, col_sz=args.col_sz)

    bin_req, bin_bytes, obj_count, obj_size, reuse_intervals, total_req = \
        pass1_global(iterator, args.sample)

    total_bytes  = sum(bin_bytes)
    total_unique = len(obj_count)
    total_ohw    = sum(1 for c in obj_count.values() if c == 1)
    global_ohw   = total_ohw / total_unique if total_unique else 0

    print(f"[INFO] 読み込み完了: {total_req:,} リクエスト, {total_unique:,} ユニークオブジェクト")

    # ── OHW 帯域別集計 ──────────────────────────
    bin_unique, bin_ohw = compute_ohw_per_bin(obj_count, obj_size)

    # ── ビン統計構築 ────────────────────────────
    bin_stats = build_bin_stats(
        bin_req, bin_bytes, bin_unique, bin_ohw,
        reuse_intervals, total_req, total_bytes)

    # ── 閾値スコアリング ─────────────────────────
    candidates = score_thresholds(bin_stats)

    # ── ワークロード類型 ─────────────────────────
    wl_class, notes = classify_workload(bin_stats, global_ohw, candidates[:3])

    # ── キャッシュ容量目安 ──────────────────────
    if args.cache_ratio is not None:
        cache_bytes = total_bytes * args.cache_ratio
        mean_sz = total_bytes / total_req if total_req else 1
        notes.append(f"キャッシュ容量目安 (ratio={args.cache_ratio:.2f}): "
                      f"{human_bytes(cache_bytes)}, "
                      f"収容可能オブジェクト数目安: {int(cache_bytes/mean_sz):,}")

    # ── 出力 ──────────────────────────────────
    print("\n" + "═" * 80)
    print(f"  キャッシュトレース分析レポート: {os.path.basename(args.trace)}")
    print("═" * 80)

    print_size_distribution(bin_stats, total_req, total_bytes)
    print_ohw_analysis(bin_stats, global_ohw, total_unique)
    print_reuse_analysis(bin_stats, total_req)
    print_threshold_candidates(candidates, top_n=args.top)
    print_workload_summary(wl_class, notes, bin_stats)

    print("\n" + "═" * 80)
    print("  分析完了")
    print("═" * 80 + "\n")


if __name__ == "__main__":
    main()
