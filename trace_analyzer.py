#!/usr/bin/env python3
"""
trace_analyzer.py  ―  キャッシュトレース分析 & RegionSplit 最適閾値候補推定ツール (v2.1)
=======================================================================================

スコアリングの設計思想:
  閾値Tの良さは「位置の妥当性」×「改善幅の大きさ」で決まる。

  score_v2(T) = separation(T) × protect_value(T) × D_size(T) × C_partition(T)

  各因子の意味:
    separation(T):    閾値Tが「req-byte重なり帯の上端」として機能しているか (0〜1)
                      = joint_score最大ビンが小プール側に収まっている度合い
    protect_value(T): 小プールの実効バイト保護価値 = B_small × (1-OHW_small)
                      OHW重み付きバイト分率。大サイズOHWが高いとき実効分布は小サイズ寄り。
                      TをT'へ増やすと追加ビンのOHWが高ければprotect_valueは
                      増えにくい → 自然にピークが生まれ最適Tを示す。
    D_size(T):        汚染源が保護対象を押し出す圧力 = 大プール平均サイズ / 小プール平均サイズ
    C_partition(T):   大プール内競合リスク係数（均一低OHWが混在する場合に減衰）

  なぜ OHW勾配（ohw_gradient）を増幅器にしないか:
    実験(WL-06)でOHW勾配が5%pt程度でも ΔBMR=-0.976%pt を達成。
    実験(WL-01b)と同等の効果はOHW勾配ではなくB_protect×(1-r)とD_sizeで説明できる。
    OHW勾配はBMR改善の必要条件ではなく補助的な分類の手がかりにとどめる。

  CDN型の正しい処理:
    req分布とbyte分布が乖離 → joint_scoreが全ビンで低い →
    separationがどの閾値でも低い → score_v2 ≈ 0 → 「改善不可」を正しく出力。

補助指標:
  joint_score(bin) = req_share × eff_byte_share = req_share × byte_share × (1-OHW)
      req・byte・再利用性を同時に持つビンを特定。最高値ビンの上端が閾値候補。
  eff_byte_share(bin) = byte_share × (1-OHW)
      OHW重み付きバイト分布。大サイズほどOHW高ければ実効分布は小サイズ側にシフト。
  CDF crossover: F_req(T) + F_byte(T) ≈ 1 となる点（参考情報）

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
N_BINS = 30
_ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'   # zstandard フレームマジック

def _is_zstd(path: str) -> bool:
    with open(path, 'rb') as f:
        return f.read(4) == _ZSTD_MAGIC

def bin_idx(size: int) -> int:
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
    return math.sqrt(2**i * 2**(i+1))

def human_bytes(b: float) -> str:
    for unit, div in [("GiB", 1<<30), ("MiB", 1<<20), ("KiB", 1<<10)]:
        if b >= div:
            return f"{b/div:.2f}{unit}"
    return f"{int(b)}B"


# ─────────────────────────────────────────────
#  トレース読み込み
# ─────────────────────────────────────────────
ORACLE_RECORD = struct.Struct("=IQIi")
ORACLE_SIZE   = ORACLE_RECORD.size  # 20 bytes

def iter_oraclegeneral(path: str):
    """oraclegeneral バイナリイテレータ（平文 / zstd 圧縮の自動判別）"""
    if _is_zstd(path):
        try:
            import zstandard as _zstd
        except ImportError:
            print("[ERROR] zstd 圧縮ファイルには 'zstandard' が必要です: "
                  "pip install zstandard", file=sys.stderr)
            sys.exit(1)
        dctx = _zstd.ZstdDecompressor()
        with open(path, 'rb') as fh:
            with dctx.stream_reader(fh) as f:
                while True:
                    buf = f.read(ORACLE_SIZE)
                    if len(buf) < ORACLE_SIZE:
                        break
                    ts, oid, sz, _ = ORACLE_RECORD.unpack(buf)
                    yield ts, oid, sz
    else:
        with open(path, 'rb') as f:
            while True:
                buf = f.read(ORACLE_SIZE)
                if len(buf) < ORACLE_SIZE:
                    break
                ts, oid, sz, _ = ORACLE_RECORD.unpack(buf)
                yield ts, oid, sz

def iter_csv(path: str, sep: str = ",", has_header: bool = True,
             col_ts: int = 0, col_id: int = 1, col_sz: int = 2):
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
#  サンプリングユーティリティ
# ─────────────────────────────────────────────
def parse_ram_size(s: str) -> int:
    """'1GiB', '512MiB', '256KiB', '1GB' などをバイト数に変換"""
    for suffix, mult in [('gib', 1<<30), ('mib', 1<<20), ('kib', 1<<10),
                         ('gb', 10**9),  ('mb', 10**6),  ('kb', 10**3), ('b', 1)]:
        if s.lower().endswith(suffix):
            return int(float(s[:-len(suffix)]) * mult)
    return int(s)


def make_sampler(rate: float):
    """Knuth乗算ハッシュによる決定論的オブジェクトサンプラーを返す。
    rate >= 1.0 のとき None を返す（全件採用）。
    同じ obj_id は常に同じ判定になるので再利用間隔が破綻しない。"""
    if rate >= 1.0:
        return None
    threshold = int(rate * (1 << 32))
    def _accept(oid: int) -> bool:
        return ((oid * 2654435761) & 0xFFFFFFFF) < threshold
    return _accept


def quick_pass_unique_bytes(path: str, fmt: str, sep: str,
                             has_header: bool, col_ts: int,
                             col_id: int, col_sz: int):
    """全ユニークオブジェクトのサイズ合計と個数を返す軽量パス。
    --sample-ram のサンプリング率計算専用。"""
    obj_size = {}
    if fmt == 'oraclegeneral':
        it = iter_oraclegeneral(path)
    else:
        it = iter_csv(path, sep=sep, has_header=has_header,
                      col_ts=col_ts, col_id=col_id, col_sz=col_sz)
    for _, oid, sz in it:
        obj_size[oid] = sz
    total = sum(obj_size.values())
    return total, len(obj_size)


# ─────────────────────────────────────────────
#  パス 1: 全リクエスト統計（ストリーミング）
# ─────────────────────────────────────────────
def pass1_global(iterator, max_reuse_sample: int, sampler=None):
    """トレースをストリーミング集計する。
    sampler が指定されているとき、sampler(obj_id) が False のリクエストはスキップする。
    再利用間隔はサンプリング後のインデックスで計算するため整合性が保たれる。"""
    bin_req   = [0] * N_BINS
    bin_bytes = [0] * N_BINS
    obj_count = defaultdict(int)
    obj_size  = {}
    obj_last  = {}
    reuse_intervals = defaultdict(list)
    sample_done = False
    total_req   = 0          # サンプリング後の採用リクエスト数（再利用間隔の基準にも使う）

    for (ts, oid, sz) in iterator:
        if sampler is not None and not sampler(oid):
            continue
        bi = bin_idx(sz)
        bin_req[bi]   += 1
        bin_bytes[bi] += sz
        obj_count[oid] += 1
        obj_size[oid]   = sz
        if not sample_done:
            if oid in obj_last:
                reuse_intervals[bi].append(total_req - obj_last[oid])
            obj_last[oid] = total_req
            if total_req + 1 >= max_reuse_sample:
                sample_done = True
        total_req += 1

    return bin_req, bin_bytes, obj_count, obj_size, reuse_intervals, total_req


# ─────────────────────────────────────────────
#  パス 2: OHW 分布
# ─────────────────────────────────────────────
def compute_ohw_per_bin(obj_count: dict, obj_size: dict):
    bin_unique    = [0] * N_BINS
    bin_ohw       = [0] * N_BINS
    bin_ohw_bytes = [0] * N_BINS   # OHWオブジェクトのバイト量合計（1回分）
    for oid, cnt in obj_count.items():
        bi = bin_idx(obj_size.get(oid, 0))
        bin_unique[bi] += 1
        if cnt == 1:
            bin_ohw[bi]       += 1
            bin_ohw_bytes[bi] += obj_size.get(oid, 0)
    return bin_unique, bin_ohw, bin_ohw_bytes


# ─────────────────────────────────────────────
#  ビン統計構築
# ─────────────────────────────────────────────
def build_bin_stats(bin_req, bin_bytes, bin_unique, bin_ohw, bin_ohw_bytes,
                    reuse_intervals, total_req, total_bytes):
    stats = []
    for i in range(N_BINS):
        req  = bin_req[i]
        byt  = bin_bytes[i]
        uniq = bin_unique[i]
        ohwn = bin_ohw[i]
        ohw_rate      = ohwn / uniq if uniq else 0.0
        ohw_byt       = bin_ohw_bytes[i]
        stats.append({
            "bin_idx"        : i,
            "label"          : bin_label(i),
            "bin_lo"         : 2 ** i,
            "bin_hi"         : 2 ** (i + 1),
            "bin_mid"        : bin_midpoint(i),
            "req_count"      : req,
            "req_share"      : req / total_req if total_req else 0,
            "byte_count"     : byt,
            "byte_share"     : byt / total_bytes if total_bytes else 0,
            "eff_byte_share" : (byt / total_bytes if total_bytes else 0) * (1 - ohw_rate),
            "ohw_byte_count" : ohw_byt,
            "ohw_byte_share" : ohw_byt / total_bytes if total_bytes else 0,
            "unique_count"   : uniq,
            "ohw_count"      : ohwn,
            "ohw_rate"       : ohw_rate,
            "reuse_intervals": reuse_intervals.get(i, []),
        })
    return stats


# ─────────────────────────────────────────────
#  新スコアリング: req-byte 重なり指標
# ─────────────────────────────────────────────
def score_thresholds_v2(bin_stats: list):
    """
    v2 スコアリング（req-byte 重なり指標）

    joint_score(bin) = req_share × byte_share × (1-OHW)
        各ビンが「リクエスト・バイト・再利用性」を同時に持つ度合い。
        この値が高いビンの上端が最適閾値の第一候補。

    separation_score(T) = joint(max_bin<T) / (joint(max_bin<T) + joint_avg_above(T))
        閾値Tが「重なり帯の上端」として機能する度合い。

    CDF crossover:
        F_req(T) + F_byte(T) ≈ 1 となる点。
        req 累積が重なり帯を覆い尽くし、byte が大プールに移行する境界。
    """
    active = [b for b in bin_stats if b["req_count"] > 0]
    if len(active) < 2:
        return []

    total_req  = sum(b["req_count"]  for b in active)
    total_byte = sum(b["byte_count"] for b in active)
    total_eff  = sum(b["eff_byte_share"] * total_byte for b in active)  # 絶対値

    # joint_score per bin
    for b in active:
        b["joint_score"] = b["req_share"] * b["byte_share"] * (1.0 - b["ohw_rate"])

    max_joint = max(b["joint_score"] for b in active) or 1e-10

    results = []
    cum_req  = 0
    cum_byte = 0
    cum_eff  = 0

    for t_idx in range(1, N_BINS):
        threshold = 2 ** t_idx

        small = [b for b in active if b["bin_hi"] <= threshold]
        large = [b for b in active if b["bin_lo"] >= threshold]

        if not small or not large:
            continue

        cum_req  = sum(b["req_count"]  for b in small)
        cum_byte = sum(b["byte_count"] for b in small)
        cum_eff  = sum(b["eff_byte_share"] * total_byte for b in small)

        F_req  = cum_req  / total_req  if total_req  else 0
        F_byte = cum_byte / total_byte if total_byte else 0
        F_eff  = cum_eff  / total_eff  if total_eff  else 0

        # CDF 交差度: F_req(T) + F_byte(T) が 1 に近いほど均衡した交差点
        cdf_crossover_dist = abs((F_req + F_byte) - 1.0)

        # 重なり帯の上端かどうか
        max_joint_small = max((b["joint_score"] for b in small), default=0)
        sum_joint_large = sum(b["joint_score"] for b in large)

        separation = (max_joint_small / (max_joint_small + sum_joint_large + 1e-10))

        # 保護対象（小プール）の統計
        B_small   = F_byte
        small_req = cum_req
        small_byt = cum_byte
        ohw_small = (sum(b["byte_count"] * b["ohw_rate"] for b in small) / small_byt
                     if small_byt else 0)
        protect_value = B_small * (1.0 - ohw_small)

        # 大プール側
        large_byt = sum(b["byte_count"] for b in large)
        large_req = sum(b["req_count"]  for b in large)
        ohw_large = (sum(b["byte_count"] * b["ohw_rate"] for b in large) / large_byt
                     if large_byt else 0)

        # OHW 勾配
        ohw_gradient = ohw_large - ohw_small

        # サイズ比（変位係数）
        mean_sz_small = small_byt / small_req if small_req else 1
        mean_sz_large = large_byt / large_req if large_req else 1
        D_size = mean_sz_large / mean_sz_small

        # 大プール負荷率（pool_ratio=0.7 固定と仮定）
        pool_ratio = 0.7
        large_load_ratio = (1 - F_byte) / (1 - pool_ratio)  # 1-pool_ratio = 大プール割合

        # C_partition: 大プール内競合リスク
        if len(large) >= 2:
            ohw_large_vals = [b["ohw_rate"] for b in large]
            ohw_var        = max(ohw_large_vals) - min(ohw_large_vals)
            intra_risk     = (ohw_var < 0.15) and (ohw_large < 0.40) and (large_load_ratio > 1.5)
        else:
            intra_risk = False
        C_partition = 0.15 if intra_risk else 1.0

        # ── 統合スコア（v2.1）──────────────────────────
        # score_v1: 実験的に検証されたΔBMR推定の骨格
        #   = B_small × (1-OHW_small) × D_size × C_partition
        score_v1 = protect_value * D_size * C_partition

        # score_v2: 閾値位置の妥当性 × 改善幅推定
        #   = separation × score_v1
        # separationにより「重なり帯の上端」にある閾値が自然に高スコアになる。
        # OHW勾配ではなくprotect_valueを使うことでWL-06型（勾配弱・保護価値大）
        # を正しく評価できる。CDN型はseparation≈0のためscore_v2≈0になる。
        score_v2 = separation * score_v1

        # ΔBMR 推定（旧式・目安）
        delta_bmr_est = -0.07 * protect_value * D_size * C_partition

        results.append({
            "threshold_bytes"    : threshold,
            "threshold_label"    : human_bytes(threshold),
            "score_v2"           : score_v2,
            "score_v1"           : score_v1,
            "separation"         : separation,
            "max_joint_small"    : max_joint_small,
            "sum_joint_large"    : sum_joint_large,
            "F_req"              : F_req,
            "F_byte"             : F_byte,
            "F_eff_byte"         : F_eff,
            "cdf_crossover_dist" : cdf_crossover_dist,
            "ohw_gradient"       : ohw_gradient,
            "D_size"             : D_size,
            "C_partition"        : C_partition,
            "large_load_ratio"   : large_load_ratio,
            "intra_risk"         : intra_risk,
            "protect_value"      : protect_value,
            "B_small_pct"        : F_byte * 100,
            "ohw_small"          : ohw_small,
            "ohw_large"          : ohw_large,
            "delta_bmr_est"      : delta_bmr_est,
        })

    # OHWバイトスパイク検出: ビンごとの ohw_byte_share の増分が最大になる点
    # その直前（bin_lo）を「OHWバイト急増直前閾値」として候補にフラグ付け
    active_obs = [b for b in bin_stats if b["req_count"] > 0]
    ohw_byte_spike_bin = None
    max_delta = 0.0
    prev_obs = 0.0
    for b in active_obs:
        delta = b["ohw_byte_share"] - prev_obs
        if delta > max_delta:
            max_delta = delta
            ohw_byte_spike_bin = b
        prev_obs = b["ohw_byte_share"]
    spike_threshold = ohw_byte_spike_bin["bin_lo"] if ohw_byte_spike_bin else None

    for r in results:
        r["ohw_byte_spike"] = (spike_threshold is not None and
                               r["threshold_bytes"] == spike_threshold)
        r["ohw_byte_spike_threshold"] = spike_threshold

    results.sort(key=lambda x: x["score_v2"], reverse=True)
    return results


# ─────────────────────────────────────────────
#  ワークロード類型判定（更新版）
# ─────────────────────────────────────────────
def classify_workload(bin_stats, global_ohw, top_v2):
    active = [b for b in bin_stats if b["req_count"] > 0]
    if not active:
        return "unknown", []

    ohw_vals = [b["ohw_rate"] for b in active]
    ohw_var  = max(ohw_vals) - min(ohw_vals)

    # OHW 勾配（サイズ加重）
    avg_ohw_small = sum(b["ohw_rate"] for b in active[:len(active)//2]) / max(len(active)//2, 1)
    avg_ohw_large = sum(b["ohw_rate"] for b in active[len(active)//2:]) / max(len(active) - len(active)//2, 1)
    ohw_diff = avg_ohw_large - avg_ohw_small

    # req-byte 重なり評価
    max_joint  = max(b["joint_score"] for b in active) if active else 0
    joint_vals = [b["joint_score"] for b in active]
    joint_spread = max(joint_vals) - min(joint_vals) if joint_vals else 0

    # バイト重心（大サイズ寄りか）
    total_bytes = sum(b["byte_count"] for b in active)
    byte_weighted_log2 = (sum(b["byte_count"] * math.log2(max(b["bin_mid"], 1))
                              for b in active) / max(total_bytes, 1))

    notes = []

    if top_v2:
        best = top_v2[0]
        sep  = best["separation"]
        if sep > 0.65 and best["D_size"] > 2:
            if best["ohw_gradient"] > 0.15:
                cls = "block_cache_ohw_grad"
                notes.append("OHW急上昇型（Block Cache型）: 重なり帯の上端 = 最適閾値候補")
            else:
                cls = "block_cache_size_density"
                notes.append("サイズ密度差型（WL7型）: OHW勾配は弱いが重なり帯 + D_size が大きい")
            notes.append(f"最優先閾値候補: {best['threshold_label']}"
                         f"（separation={sep:.2f}, D_size={best['D_size']:.1f}）")
        elif ohw_diff <= -0.15:
            cls = "cdn_inverted"
            notes.append("CDN型（逆転OHW）: 大サイズほど再利用価値が高い → RegionSplit 中立〜有害")
        elif ohw_var < 0.15 and global_ohw < 0.35:
            cls = "uniform_low_ohw"
            notes.append("均一低OHW型: 廃棄コンテンツがサイズで識別不可 → 中立〜有害")
            if best["large_load_ratio"] > 1.5:
                notes.append("⚠ 大プール過負荷リスク: BMR悪化の可能性あり")
        elif joint_spread < 0.002:
            cls = "cdn_no_overlap"
            notes.append("req-byte 重なりなし（CDN型）: どの閾値でも joint_score が低い → 改善不可")
        else:
            cls = "neutral"
            notes.append("中間型: 閾値スコアを参考に個別判断")
    else:
        cls = "unknown"

    return cls, notes


# ─────────────────────────────────────────────
#  出力関数群
# ─────────────────────────────────────────────
SEP = "─" * 80

def print_section(title):
    print(f"\n{'═'*80}")
    print(f"  {title}")
    print(f"{'═'*80}")

def print_size_distribution(bin_stats, total_req, total_bytes):
    print_section("1. サイズ分布（req / byte / 実効byte / OHW）")
    print(f"  総リクエスト数: {total_req:,}  総バイト量: {human_bytes(total_bytes)}")
    print()
    hdr = (f"  {'サイズ帯':<18} {'Req数':>10} {'Req%':>6} {'Byte%':>7}"
           f" {'EffByte%':>9} {'OHW%':>7} {'joint':>8}")
    print(hdr)
    print("  " + "─" * 80)

    # CDF を計算して交差点を探す
    cum_req = cum_byte = 0
    prev_sum = None
    crossover_label = None

    active = [b for b in bin_stats if b["req_count"] > 0]
    total_r = sum(b["req_count"]  for b in active)
    total_b = sum(b["byte_count"] for b in active)

    for b in bin_stats:
        if b["req_count"] == 0:
            continue
        cum_req  += b["req_count"]
        cum_byte += b["byte_count"]
        F_r = cum_req  / total_r if total_r  else 0
        F_b = cum_byte / total_b if total_b  else 0
        cdf_sum = F_r + F_b

        # 交差点: cdf_sum が 1 を超える最初のビン
        cross_flag = ""
        if prev_sum is not None and prev_sum < 1.0 <= cdf_sum:
            cross_flag = " ◄ CDF交差点"
            crossover_label = b["label"]
        prev_sum = cdf_sum

        eff = b["eff_byte_share"] * 100
        joint = b["joint_score"] * 1000  # 1000倍して見やすく

        bar_j = "█" * min(int(joint * 20), 15)
        print(f"  {b['label']:<18} {b['req_count']:>10,} {b['req_share']*100:>5.2f}%"
              f" {b['byte_share']*100:>6.2f}% {eff:>8.2f}%"
              f" {b['ohw_rate']*100:>6.1f}%"
              f" {joint:>6.2f}‰ {bar_j}{cross_flag}")

    if crossover_label:
        print(f"\n  ★ CDF交差点（F_req + F_byte = 1）: {crossover_label} 付近")
        print(f"    → この帯域の上端が「リクエスト保護とバイト隔離のバランス点」")


def print_joint_analysis(bin_stats):
    print_section("2. req-byte 重なり分析")
    print("  joint_score(bin) = req% × byte% × (1-OHW%)  [×1000 で表示]")
    print("  この値が高いビンが「リクエスト・バイト・再利用性」を同時に持つ重なり帯。")
    print("  最高値ビンの上端が最優先の閾値候補となる。")
    print()

    active = [b for b in bin_stats if b["req_count"] > 0]
    if not active:
        return

    max_joint = max(b["joint_score"] for b in active) or 1e-10
    max_bin   = max(active, key=lambda b: b["joint_score"])

    print(f"  {'サイズ帯':<18} {'joint×1000':>11}  {'req%':>6}  {'byte%':>6}  {'EffByte%':>9}  グラフ")
    print("  " + "─" * 75)
    for b in active:
        j = b["joint_score"] * 1000
        bar_len = int(j / (max_joint * 1000) * 30)
        bar = "█" * bar_len
        star = " ★最高" if b is max_bin else ""
        print(f"  {b['label']:<18} {j:>10.3f}  "
              f"{b['req_share']*100:>5.2f}%  {b['byte_share']*100:>5.2f}%  "
              f"{b['eff_byte_share']*100:>8.2f}%  {bar}{star}")

    print(f"\n  最適閾値候補（joint_score最大ビンの上端）: {max_bin['bin_hi']:,} bytes"
          f" = {human_bytes(max_bin['bin_hi'])}")
    print(f"  根拠: {max_bin['label']} が重なり帯の核心（joint={max_joint*1000:.3f}‰）")


def print_ohw_analysis(bin_stats, global_ohw, total_unique):
    print_section("3. OHW 分析（サイズ帯別）")
    print(f"  全体 OHW率: {global_ohw*100:.2f}%  ユニークオブジェクト数: {total_unique:,}")
    print()
    print("  [3a] OHW率とOHWバイトシェア")
    print(f"  {'サイズ帯':<18} {'ユニーク':>10} {'OHW数':>9} {'OHW率':>8} {'OHWByte%':>10}  傾向")
    print("  " + "─" * 75)
    prev_ohw = None
    prev_ohw_bs = 0.0
    spike_label = None
    max_delta_obs = 0.0
    for b in bin_stats:
        if b["unique_count"] == 0:
            continue
        arrow = ""
        if prev_ohw is not None:
            d = b["ohw_rate"] - prev_ohw
            arrow = " ↑" if d > 0.05 else (" ↓" if d < -0.05 else "  →")
        delta_obs = b["ohw_byte_share"] - prev_ohw_bs
        spike_flag = ""
        if delta_obs > max_delta_obs:
            max_delta_obs = delta_obs
            spike_label = b["label"]
            spike_flag = " ◄ OHWバイト急増"
        print(f"  {b['label']:<18} {b['unique_count']:>10,} {b['ohw_count']:>9,}"
              f" {b['ohw_rate']*100:>7.1f}% {b['ohw_byte_share']*100:>9.2f}%  {arrow}{spike_flag}")
        prev_ohw    = b["ohw_rate"]
        prev_ohw_bs = b["ohw_byte_share"]

    if spike_label:
        active = [b for b in bin_stats if b["unique_count"] > 0]
        spike_bin = max(active, key=lambda b: b["ohw_byte_share"] - 0)  # ダミー初期値
        # 正確なスパイクビン（最大増分）を再検索
        prev2 = 0.0
        best_delta2 = 0.0
        for b in active:
            d = b["ohw_byte_share"] - prev2
            if d > best_delta2:
                best_delta2 = d
                spike_bin = b
            prev2 = b["ohw_byte_share"]
        print(f"\n  ★ OHWバイト急増点: {spike_bin['label']}")
        print(f"    → この帯域の直前（{human_bytes(spike_bin['bin_lo'])}）を閾値に置くと、")
        print(f"      OHWバイトが大プールに隔離され、実効的な小プール保護が最大化される可能性あり。")


def print_reuse_analysis(bin_stats):
    print_section("4. 再利用間隔分析（先頭サンプル）")
    print(f"  {'サイズ帯':<18} {'再利用件数':>10} {'中央値':>10} {'p75':>10} {'p95':>10}")
    print("  " + "─" * 65)
    for b in bin_stats:
        if not b["reuse_intervals"]:
            continue
        ivs = sorted(b["reuse_intervals"])
        n   = len(ivs)
        med = ivs[n // 2]
        p75 = ivs[int(n * 0.75)]
        p95 = ivs[int(n * 0.95)]
        print(f"  {b['label']:<18} {n:>10,} {med:>10,} {p75:>10,} {p95:>10,}")


def print_threshold_candidates(candidates, top_n=5):
    print_section("5. 閾値候補ランキング（v2: req-byte 重なり指標）")
    print("  v2スコア = separation × protect_value × D_size × C_partition")
    print("  protect_value = B_small × (1−OHW_small): 小プールの実効バイト保護価値")
    print("  separationは「重なり帯の上端として機能しているか」を測る（1に近いほど良い）")
    print()
    print(f"  {'閾値':>10} {'v2スコア':>9} {'separation':>11} {'D_size':>7}"
          f" {'OHW勾配':>8} {'負荷率':>7} {'ΔBMR推定':>9}  備考")
    print("  " + "─" * 100)
    shown = 0
    for c in candidates:
        if shown >= top_n:
            break
        note = []
        if c["intra_risk"]:
            note.append("[大プール過負荷]")
        if c["ohw_gradient"] > 0.15:
            note.append("[OHW急上昇]")
        if c["cdf_crossover_dist"] < 0.05:
            note.append("[CDF交差点]")
        if c.get("ohw_byte_spike"):
            note.append("[OHWバイト急増直前]")
        print(f"  {c['threshold_label']:>10} {c['score_v2']:>9.4f}"
              f" {c['separation']:>11.3f} {c['D_size']:>7.1f}"
              f" {c['ohw_gradient']*100:>7.1f}% {c['large_load_ratio']:>7.2f}"
              f" {c['delta_bmr_est']:>9.3f}%pt  {' '.join(note)}")
        shown += 1

    # ハーモニー表示: v1とv2の上位が一致しているかを確認
    top_v1_labels = sorted(candidates, key=lambda x: x["score_v1"], reverse=True)[:3]
    top_v2_labels = candidates[:3]
    agree = [c["threshold_label"] for c in top_v1_labels
             if any(c2["threshold_label"] == c["threshold_label"] for c2 in top_v2_labels)]
    if agree:
        print(f"\n  ✓ v1・v2スコアが一致する候補: {', '.join(agree)}"
              f" → 信頼度が高い閾値候補")
    else:
        print(f"\n  ⚠ v1・v2スコアの上位が乖離しています。v2（重なり指標）を優先してください。")


def print_workload_summary(wl_class, notes, candidates):
    print_section("6. ワークロード類型と推奨")
    class_map = {
        "block_cache_ohw_grad"    : "Block Cache型（OHW急上昇）→ RegionSplit 有効",
        "block_cache_size_density": "サイズ密度差型（WL7型）→ RegionSplit 有効（OHW均一でも）",
        "cdn_inverted"            : "CDN型（逆転OHW）→ RegionSplit 中立〜有害",
        "cdn_no_overlap"          : "req-byte 重なりなし → RegionSplit 改善不可",
        "uniform_low_ohw"         : "均一低OHW型 → 中立〜有害（大プール過負荷に要注意）",
        "neutral"                 : "中間型 → 閾値スコアで個別判断",
        "unknown"                 : "不明",
    }
    print(f"  推定類型: {class_map.get(wl_class, wl_class)}")
    print()
    for note in notes:
        print(f"  • {note}")

    print()
    print("  ─── 閾値選択の優先順位 ─────────────────────────────────────────")
    print("  1. CDF交差点付近（F_req + F_byte ≈ 1）がある場合 → 最優先候補")
    print("  2. joint_score が最大のビンの上端 → 重なり帯の上端")
    print("  3. v2スコア上位かつ separation > 0.7 → 隔離が明確")
    print()
    print("  ⚠ ΔBMR推定値はキャッシュ比率に依存。大きいほど最適閾値は右シフト。")
    print("  ⚠ 大プール負荷率 > 1.5 のとき大プール内競合リスク（C_partition↓）")


# ─────────────────────────────────────────────
#  メイン
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="キャッシュトレース分析 & RegionSplit 最適閾値候補推定 v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("trace",  help="トレースファイルパス")
    parser.add_argument("--format", choices=["oraclegeneral", "csv"],
                        default="oraclegeneral")
    parser.add_argument("--sep",       default=",")
    parser.add_argument("--no-header", action="store_true")
    parser.add_argument("--col-ts",    type=int, default=0)
    parser.add_argument("--col-id",    type=int, default=1)
    parser.add_argument("--col-sz",    type=int, default=2)
    parser.add_argument("--sample",    type=int, default=2_000_000,
                        help="再利用間隔サンプル上限")
    parser.add_argument("--top",       type=int, default=5,
                        help="表示する閾値候補数")
    parser.add_argument("--cache-ratio", type=float, default=None,
                        help="キャッシュ容量/ワークロードbyte (例: 0.10)")
    parser.add_argument("--sample-rate", type=float, default=None,
                        help="オブジェクトIDハッシュによるサンプリング率 (例: 0.1 = 10%%)")
    parser.add_argument("--sample-ram",  default=None,
                        help="サンプリング後ユニークオブジェクトのRAM目標量 (例: 1GiB, 512MiB)")
    args = parser.parse_args()

    if not os.path.exists(args.trace):
        print(f"[ERROR] ファイルが見つかりません: {args.trace}", file=sys.stderr)
        sys.exit(1)

    # ── サンプリング設定 ──────────────────────────────────────────────
    if args.sample_rate is not None and args.sample_ram is not None:
        print("[ERROR] --sample-rate と --sample-ram は同時に指定できません",
              file=sys.stderr)
        sys.exit(1)

    sampler = None
    sample_rate_used = None

    if args.sample_rate is not None:
        sample_rate_used = float(args.sample_rate)
        sampler = make_sampler(sample_rate_used)
        print(f"[INFO] サンプリング率: {sample_rate_used*100:.2f}%"
              f" (Knuth乗算ハッシュ、同一obj_id は常に同じ判定)")

    elif args.sample_ram is not None:
        target_bytes = parse_ram_size(args.sample_ram)
        print(f"[INFO] RAM目標: {human_bytes(target_bytes)}"
              f" → 第1パス: ユニークオブジェクトサイズ集計中...")
        total_uniq_bytes, n_uniq = quick_pass_unique_bytes(
            args.trace, args.format, args.sep, not args.no_header,
            args.col_ts, args.col_id, args.col_sz)
        sample_rate_used = min(1.0, target_bytes / total_uniq_bytes) if total_uniq_bytes else 1.0
        sampler = make_sampler(sample_rate_used)
        print(f"[INFO] ユニークオブジェクト: {n_uniq:,}個 /"
              f" {human_bytes(total_uniq_bytes)} (全体)")
        print(f"[INFO] 計算サンプリング率: {sample_rate_used*100:.2f}%"
              f"  推定採用RAM: {human_bytes(min(target_bytes, total_uniq_bytes))}")

    # ── イテレータ生成 ─────────────────────────────────────────────────
    compressed = _is_zstd(args.trace) if args.format == "oraclegeneral" else False
    print(f"[INFO] 読み込み中: {args.trace}"
          f"{'  (zstd圧縮)' if compressed else ''}")
    if args.format == "oraclegeneral":
        iterator = iter_oraclegeneral(args.trace)
    else:
        iterator = iter_csv(args.trace, sep=args.sep,
                            has_header=not args.no_header,
                            col_ts=args.col_ts, col_id=args.col_id, col_sz=args.col_sz)

    bin_req, bin_bytes, obj_count, obj_size, reuse_intervals, total_req = \
        pass1_global(iterator, args.sample, sampler)

    total_bytes  = sum(bin_bytes)
    total_unique = len(obj_count)
    total_ohw    = sum(1 for c in obj_count.values() if c == 1)
    global_ohw   = total_ohw / total_unique if total_unique else 0

    print(f"[INFO] 完了: {total_req:,} リクエスト, {total_unique:,} ユニークオブジェクト")

    bin_unique, bin_ohw, bin_ohw_bytes = compute_ohw_per_bin(obj_count, obj_size)
    bin_stats = build_bin_stats(
        bin_req, bin_bytes, bin_unique, bin_ohw, bin_ohw_bytes,
        reuse_intervals, total_req, total_bytes)

    # joint_score を先に付与（classify_workload が必要とする）
    total_r = sum(b["req_count"]  for b in bin_stats if b["req_count"] > 0)
    total_b = sum(b["byte_count"] for b in bin_stats if b["req_count"] > 0)
    for b in bin_stats:
        b["joint_score"] = b["req_share"] * b["byte_share"] * (1.0 - b["ohw_rate"])

    candidates = score_thresholds_v2(bin_stats)
    wl_class, notes = classify_workload(bin_stats, global_ohw, candidates[:3])

    if args.cache_ratio is not None:
        cache_bytes = total_bytes * args.cache_ratio
        mean_sz = total_bytes / total_req if total_req else 1
        notes.append(f"キャッシュ容量 (ratio={args.cache_ratio:.2f}): "
                      f"{human_bytes(cache_bytes)}, "
                      f"収容可能オブジェクト目安: {int(cache_bytes/mean_sz):,}")

    sample_info = (f"  サンプリング率: {sample_rate_used*100:.2f}%"
                   if sample_rate_used is not None and sample_rate_used < 1.0
                   else "")

    print("\n" + "═" * 80)
    print(f"  キャッシュトレース分析レポート v2.1: {os.path.basename(args.trace)}")
    if sample_info:
        print(sample_info)
    print("═" * 80)

    print_size_distribution(bin_stats, total_req, total_bytes)
    print_joint_analysis(bin_stats)
    print_ohw_analysis(bin_stats, global_ohw, total_unique)
    print_reuse_analysis(bin_stats)
    print_threshold_candidates(candidates, top_n=args.top)
    print_workload_summary(wl_class, notes, candidates)

    print("\n" + "═" * 80)
    print("  分析完了")
    print("═" * 80 + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
