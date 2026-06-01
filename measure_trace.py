#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_trace.py
================
zstd 圧縮された OracleGeneral 形式トレースから、Cache Temperature Simulator
（cache_temperature_simulator_4.html）のワークロード生成器が必要とする
全パラメータを一括計測し、生成器互換の config JSON として保存する。

計測するパラメータ（research report の Shape/Scale 二層）:
  Scale層:
    n                 ... ユニークオブジェクト数（フットプリント, サンプル時は推定値）
  Shape層（全体）:
    p_ohw             ... 全体 One-Hit-Wonder 率（unique 空間, 参照回数==1 の割合）
    zipf_alpha        ... 全体 Zipf 指数（rank-frequency log-log OLS）
    locality (L)      ... 時間的局所性（再利用距離分布から逆算, 0=IRM 〜 1=強局所性）
    arrival/hawkes_eta... 到着過程のバースト性（タイムスタンプ間隔の変動係数から推定）
  Shape層（サイズビン別, 30 ビン, ビン i = [2^i, 2^(i+1)) バイト）:
    q_b   ... リクエスト比率（Σ=1）
    r_b   ... ビン内 OHW 率
    a_b   ... ビン内 Zipf 指数
    rho_b ... 非OHWオブジェクト1個あたりの平均参照回数（= (N_b - m_b)/np_b）

OracleGeneral レコード形式（24 バイト/レコード, リトルエンディアン）:
    uint32 timestamp | uint64 obj_id | uint32 obj_size | int64 next_access_vtime
    （オフセット 0, 4, 12, 16; itemsize=24, パック）

サンプリング（大規模トレース対策）:
    --sample に「割合」または「データサイズ長」を指定できる。
      割合     : 0 < x <= 1            （例: 0.1 = 10%）
      サイズ長 : 3GiB / 500MiB / 1.5TB / 300000000（バイト）
    --method:
      spatial（既定: 割合指定時）
          オブジェクトIDのハッシュで一部オブジェクトのみ採用。
          採用オブジェクトの全アクセスを保持するため、各オブジェクトの
          参照回数・OHW・再利用強度が「無偏」に測れる（SHARDS と同種の空間サンプリング）。
          ストリーム全体を走査する。サイズ長指定時は rate = 目標 / 全体推定サイズ。
      prefix（既定: サイズ長指定時）
          展開後の先頭 N バイト（または先頭割合）だけを処理して打ち切る。
          走査量自体が減るため高速。時間窓サンプルとなる（定常なら代表性あり）。

出力:
    <out>.config.json      ... 生成器/ C版ツール互換（workload セクション）
    <out>.measurement.json ... 詳細レポート（再利用距離ヒストグラム, R², サンプリング情報など）
    <out>.bins.csv         ... ビン別サマリ（表計算用）

依存:
    pip install numpy zstandard

使用例:
    # 30GiB トレースから 3GiB 分（先頭, 高速）を計測
    python measure_trace.py trace.oracleGeneral.zst -o myresult --sample 3GiB

    # 全体の 10% を空間サンプリング（無偏・代表性重視）で計測
    python measure_trace.py trace.oracleGeneral.zst -o myresult --sample 0.1

    # サイズ長指定でも空間サンプリングしたい場合（全走査・要総サイズ推定）
    python measure_trace.py trace.zst -o r --sample 3GiB --method spatial --total-size 30GiB
"""

import argparse
import json
import math
import os
import sys
import time

try:
    import numpy as np
except ImportError:
    sys.exit("エラー: numpy が必要です。  pip install numpy")

# ----------------------------------------------------------------------
# 定数
# ----------------------------------------------------------------------
RECORD_SIZE = 24
N_BINS = 30                     # ビン i = [2^i, 2^(i+1)) , i=0..29（生成器と一致）
READ_CHUNK = 32 * 1024 * 1024  # 展開後 32MiB ずつ処理

# OracleGeneral レコードの numpy 構造化 dtype（パック・LE）
REC_DTYPE = np.dtype({
    'names':   ['ts', 'id', 'sz', 'nv'],
    'formats': ['<u4', '<u8', '<u4', '<i8'],
    'offsets': [0, 4, 12, 16],
    'itemsize': RECORD_SIZE,
})


# ----------------------------------------------------------------------
# ユーティリティ
# ----------------------------------------------------------------------
def parse_size_or_fraction(spec):
    """'--sample' の値を解釈して (kind, value) を返す。
       kind='fraction' なら value は 0<f<=1、kind='bytes' なら value はバイト数。"""
    s = str(spec).strip()
    if not s:
        return None
    units = {
        'TIB': 1024**4, 'TB': 1000**4, 'T': 1024**4,
        'GIB': 1024**3, 'GB': 1000**3, 'G': 1024**3,
        'MIB': 1024**2, 'MB': 1000**2, 'M': 1024**2,
        'KIB': 1024,    'KB': 1000,    'K': 1024,
        'B': 1,
    }
    su = s.upper()
    for u in sorted(units, key=len, reverse=True):
        if su.endswith(u):
            num = float(su[:-len(u)])
            return ('bytes', int(num * units[u]))
    # 単位なし
    val = float(s)
    if val <= 1.0:
        return ('fraction', val)
    return ('bytes', int(val))


def human_bytes(n):
    n = float(n)
    for u in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if n < 1024 or u == 'TiB':
            return f"{n:.2f}{u}"
        n /= 1024


def hash_keep_threshold(rate):
    """空間サンプリング用のしきい値（32bit）。"""
    return int(max(0.0, min(1.0, rate)) * (1 << 32))


def obj_hash32(ids):
    """obj_id 配列 → 決定論的 32bit ハッシュ（numpy ベクトル化, splitmix 風）。"""
    h = ids.astype(np.uint64)
    h = h * np.uint64(0x9E3779B97F4A7C15)
    h ^= (h >> np.uint64(29))
    h = h * np.uint64(0xBF58476D1CE4E5B9)
    h ^= (h >> np.uint64(32))
    return (h & np.uint64(0xFFFFFFFF)).astype(np.uint64)


def size_to_bin(sz):
    """サイズ（バイト）→ ビン番号（floor(log2(sz)) を [0,29] にクランプ）。"""
    sz = np.maximum(sz.astype(np.int64), 1)
    b = np.floor(np.log2(sz)).astype(np.int64)
    return np.clip(b, 0, N_BINS - 1)


def ols_zipf_alpha(counts, max_pts=2000):
    """参照回数配列に rank-frequency log-log OLS を当てて (alpha, r2) を返す。
       生成器の estimateZipf と同じく freq=1（OHW）の点も含める。"""
    c = np.sort(np.asarray(counts, dtype=np.float64))[::-1]
    n = c.size
    if n < 5:
        return float('nan'), float('nan')
    step = max(1, n // max_pts)
    idx = np.arange(0, n, step)
    x = np.log(idx + 1.0)
    y = np.log(c[idx])
    mx, my = x.mean(), y.mean()
    sxx = np.sum((x - mx) ** 2)
    sxy = np.sum((x - mx) * (y - my))
    syy = np.sum((y - my) ** 2)
    if sxx <= 0 or syy <= 0:
        return float('nan'), float('nan')
    slope = sxy / sxx
    r2 = (sxy * sxy) / (sxx * syy)
    return float(-slope), float(r2)


# ----------------------------------------------------------------------
# ストリーム供給（zstd / raw）
# ----------------------------------------------------------------------
def iter_decompressed_chunks(path, is_zstd):
    """展開後バイト列を READ_CHUNK 単位で yield する。"""
    if is_zstd:
        try:
            import zstandard as zstd
        except ImportError:
            sys.exit("エラー: zstandard が必要です。  pip install zstandard")
        dctx = zstd.ZstdDecompressor()
        with open(path, 'rb') as fh:
            with dctx.stream_reader(fh) as reader:
                while True:
                    chunk = reader.read(READ_CHUNK)
                    if not chunk:
                        break
                    yield chunk
    else:
        with open(path, 'rb') as fh:
            while True:
                chunk = fh.read(READ_CHUNK)
                if not chunk:
                    break
                yield chunk


# ----------------------------------------------------------------------
# メイン計測
# ----------------------------------------------------------------------
def measure(path, args):
    is_zstd = args.zstd if args.zstd is not None else path.lower().endswith('.zst')

    # --- サンプリング設定の決定 ---
    sample = parse_size_or_fraction(args.sample) if args.sample else None
    method = args.method
    keep_threshold = None      # 空間サンプリング: 32bit ハッシュしきい値
    prefix_byte_limit = None   # prefix: 展開後バイト上限
    sample_rate = 1.0          # 実効サンプリング率（推定 true 値への外挿に使用）

    if sample is None:
        method = 'full'
    else:
        kind, val = sample
        # 既定メソッド: 割合→spatial, サイズ→prefix
        if method == 'auto':
            method = 'spatial' if kind == 'fraction' else 'prefix'

        if method == 'spatial':
            if kind == 'fraction':
                sample_rate = val
            else:  # bytes 目標 → rate = 目標 / 全体サイズ推定
                total = None
                if args.total_size:
                    tk, tv = parse_size_or_fraction(args.total_size)
                    total = tv if tk == 'bytes' else None
                if total is None:
                    comp = os.path.getsize(path)
                    total = int(comp * (args.est_ratio if is_zstd else 1.0))
                    print(f"[警告] spatial+サイズ指定: 全展開サイズ不明のため "
                          f"圧縮 {human_bytes(comp)} ×{args.est_ratio} ≈ {human_bytes(total)} と推定。"
                          f" 正確を期すなら --total-size を指定してください。", file=sys.stderr)
                sample_rate = max(1e-9, min(1.0, val / total))
            keep_threshold = hash_keep_threshold(sample_rate)
        elif method == 'prefix':
            if kind == 'bytes':
                prefix_byte_limit = val
            else:  # fraction → 全展開サイズ推定が必要
                total = None
                if args.total_size:
                    tk, tv = parse_size_or_fraction(args.total_size)
                    total = tv if tk == 'bytes' else None
                if total is None:
                    comp = os.path.getsize(path)
                    total = int(comp * (args.est_ratio if is_zstd else 1.0))
                    print(f"[警告] prefix+割合指定: 全展開サイズを "
                          f"{human_bytes(total)} と推定して先頭 {val*100:.1f}% を処理。",
                          file=sys.stderr)
                prefix_byte_limit = int(total * val)
            sample_rate = 1.0  # prefix は窓内のフル情報（外挿しない）

    print(f"[info] file={path}  zstd={is_zstd}  method={method}  "
          f"rate={sample_rate:.4g}  prefix_limit="
          f"{human_bytes(prefix_byte_limit) if prefix_byte_limit else '-'}", file=sys.stderr)

    # --- 集計状態 ---
    # オブジェクト辞書: id -> [count, last_global_idx, size, bin]
    objs = {}
    ird_hist = np.zeros(25, dtype=np.int64)   # log2 再利用距離ヒストグラム（0..24）
    sum_log2_ird = 0.0
    reuse_events = 0
    global_idx = 0          # 走査した全レコード数（spatial の真の時間軸）
    processed_bytes = 0     # 展開後処理バイト数（prefix 用）
    kept_records = 0

    # タイムスタンプ・バースト性（連続レコード間隔の CV）
    ia_sum = 0.0
    ia_sqsum = 0.0
    ia_cnt = 0
    last_ts = None

    leftover = b''
    t0 = time.time()
    stop = False

    for chunk in iter_decompressed_chunks(path, is_zstd):
        if leftover:
            chunk = leftover + chunk
        nrec = len(chunk) // RECORD_SIZE
        if nrec == 0:
            leftover = chunk
            continue
        # prefix 打ち切り: 残り予算ぶんのレコードに切り詰める（チャンク内で正確に停止）
        if prefix_byte_limit is not None:
            remain_rec = (prefix_byte_limit - processed_bytes) // RECORD_SIZE
            if remain_rec <= nrec:
                nrec = max(0, remain_rec)
                stop = True
                if nrec == 0:
                    break
        used = nrec * RECORD_SIZE
        leftover = chunk[used:]

        recs = np.frombuffer(chunk, dtype=REC_DTYPE, count=nrec)
        ids = recs['id']
        szs = recs['sz']
        tss = recs['ts']

        # --- バースト性: 連続レコードのタイムスタンプ間隔（全走査ベース）---
        if args.measure_burstiness:
            ts_f = tss.astype(np.float64)
            d = np.diff(ts_f)
            if last_ts is not None and nrec > 0:
                d0 = float(ts_f[0]) - last_ts
                if d0 < 0:
                    d0 = -d0
                ia_sum += d0; ia_sqsum += d0 * d0; ia_cnt += 1
            d = np.abs(d)
            ia_sum += float(d.sum())
            ia_sqsum += float(np.sum(d * d))
            ia_cnt += d.size
            last_ts = float(ts_f[-1])

        # --- 採用マスク ---
        if method == 'spatial':
            h = obj_hash32(ids)
            mask = (h < np.uint64(keep_threshold))
            sel = np.nonzero(mask)[0]
        else:  # prefix / full は全採用
            sel = np.arange(nrec)

        base = global_idx  # このチャンク先頭の真のグローバル index
        bins_sel = size_to_bin(szs[sel])
        ids_sel = ids[sel]
        szs_sel = szs[sel]

        # スカラループ（採用レコードのみ）: 参照回数・再利用距離を更新
        sel_list = sel.tolist()
        ids_list = ids_sel.tolist()
        szs_list = szs_sel.tolist()
        bins_list = bins_sel.tolist()
        for k in range(len(sel_list)):
            gidx = base + sel_list[k]
            oid = ids_list[k]
            e = objs.get(oid)
            if e is None:
                objs[oid] = [1, gidx, szs_list[k], bins_list[k]]
            else:
                # 再利用距離 = 真のグローバル index 差
                ird = gidx - e[1]
                if ird < 1:
                    ird = 1
                lb = ird.bit_length() - 1   # floor(log2)
                if lb > 24:
                    lb = 24
                ird_hist[lb] += 1
                sum_log2_ird += math.log2(ird)
                reuse_events += 1
                e[0] += 1
                e[1] = gidx

        kept_records += len(sel_list)
        global_idx += nrec
        processed_bytes += used

        # 進捗
        if global_idx % (READ_CHUNK // RECORD_SIZE * 4) < nrec:
            rate_mps = global_idx / max(1e-9, time.time() - t0) / 1e6
            print(f"\r[info] 走査 {global_idx:,} rec  採用 {kept_records:,}  "
                  f"unique {len(objs):,}  ({rate_mps:.1f}M rec/s)   ",
                  end='', file=sys.stderr)

        # prefix 打ち切り
        if prefix_byte_limit is not None and processed_bytes >= prefix_byte_limit:
            stop = True
        if stop:
            break

    print("", file=sys.stderr)
    elapsed = time.time() - t0
    print(f"[info] 走査完了: {global_idx:,} rec 処理, 採用 {kept_records:,}, "
          f"unique {len(objs):,}, {elapsed:.1f}s", file=sys.stderr)

    if not objs:
        sys.exit("エラー: 採用レコードが 0 件。サンプリング率や入力を確認してください。")

    return aggregate(objs, ird_hist, sum_log2_ird, reuse_events,
                     kept_records, global_idx, sample_rate, method,
                     (ia_sum, ia_sqsum, ia_cnt), args)


# ----------------------------------------------------------------------
# 集計 → パラメータ算出
# ----------------------------------------------------------------------
def aggregate(objs, ird_hist, sum_log2_ird, reuse_events,
              kept_records, scanned_records, sample_rate, method,
              burst, args):
    # オブジェクト配列化
    n_obj = len(objs)
    counts = np.empty(n_obj, dtype=np.int64)
    bins = np.empty(n_obj, dtype=np.int8)
    sizes = np.empty(n_obj, dtype=np.int64)
    for i, e in enumerate(objs.values()):
        counts[i] = e[0]
        sizes[i] = e[2]
        bins[i] = e[3]

    N_total = int(counts.sum())          # 採用リクエスト総数
    n_unique = n_obj                      # 採用ユニーク数
    ohw_mask = (counts == 1)
    m_total = int(ohw_mask.sum())
    p_ohw = m_total / n_unique

    # 全体 Zipf
    g_alpha, g_r2 = ols_zipf_alpha(counts)

    # --- ビン別 ---
    bins_out = []
    per_bin = []
    for b in range(N_BINS):
        sel = (bins == b)
        n_b = int(sel.sum())
        if n_b == 0:
            bins_out.append({'q': 0.0, 'r': 0.0, 'a': round(args.default_alpha, 4), 'rho': 1.0})
            per_bin.append(dict(bin=b, n_b=0, N_b=0, m_b=0, q=0, r=0, a=0, rho=0,
                                mean_size=0, r2=0))
            continue
        cb = counts[sel]
        N_b = int(cb.sum())
        m_b = int((cb == 1).sum())
        np_b = n_b - m_b
        q_b = N_b / N_total
        r_b = m_b / n_b
        rho_b = (N_b - m_b) / np_b if np_b > 0 else 1.0
        a_b, r2_b = ols_zipf_alpha(cb)
        if math.isnan(a_b):
            a_b = args.default_alpha
        mean_sz = float(sizes[sel].mean())
        bins_out.append({
            'q': round(q_b, 8),
            'r': round(r_b, 6),
            'a': round(max(0.1, min(2.5, a_b)), 4),
            'rho': round(max(1.0, rho_b), 4),
        })
        per_bin.append(dict(bin=b, n_b=n_b, N_b=N_b, m_b=m_b, np_b=np_b,
                            q=q_b, r=r_b, a=a_b, rho=rho_b,
                            mean_size=mean_sz, r2=r2_b))

    # --- 時間的局所性 L の逆算 ---
    # 生成器: hot オブジェクトの参照を幅 w=0.005^L のタイムライン窓に配置。
    #   IRM(w=1) での期待再利用距離 ≈ N/count。
    #   観測 IRD の幾何平均 / IRM 期待の幾何平均 ≈ w  →  L = log(w)/log(0.005)
    if reuse_events > 0:
        gm_measured_log2 = sum_log2_ird / reuse_events
        # IRM 期待（幾何平均）:
        #   オブジェクトが count 回ランダム配置されると再利用距離は平均 N/count の
        #   指数分布に近く、その幾何平均は (N/count)·e^{-γ}（γ=オイラー定数）。
        #   算術平均 N/count をそのまま使うと真の IRM でも w<1 になる偏りが出るため、
        #   log2 ベースラインから γ/ln2 を差し引いて補正する。
        GAMMA_OVER_LN2 = 0.8327462  # = 0.5772156649 / ln(2)
        hot = counts[counts >= 2]
        sum_log2_irm = float(np.sum((hot - 1) *
                                    (np.log2(N_total / hot) - GAMMA_OVER_LN2)))
        gm_irm_log2 = sum_log2_irm / reuse_events if reuse_events else 0.0
        # w = 2^(measured - irm)
        w_est = 2.0 ** (gm_measured_log2 - gm_irm_log2)
        w_est = max(1e-4, min(1.0, w_est))
        L_est = math.log(w_est) / math.log(0.005)
        L_est = max(0.0, min(1.0, L_est))
    else:
        w_est, L_est = 1.0, 0.0

    # --- バースト性 → arrival / hawkes_eta ---
    ia_sum, ia_sqsum, ia_cnt = burst
    arrival, hawkes_eta, burst_cv = 'poisson', 0.0, float('nan')
    if args.measure_burstiness and ia_cnt > 1:
        mean_ia = ia_sum / ia_cnt
        var_ia = max(0.0, ia_sqsum / ia_cnt - mean_ia * mean_ia)
        if mean_ia > 0:
            burst_cv = math.sqrt(var_ia) / mean_ia
            # Poisson(指数間隔) は CV≈1。CV>1 ほどバースト的。
            eta = (burst_cv - 1.0) / (burst_cv + 1.0)
            hawkes_eta = round(max(0.0, min(0.9, eta)), 3)
            arrival = 'hawkes' if hawkes_eta > 0.15 else 'poisson'

    # --- Scale 層: 真の n / N の推定（spatial のみ外挿）---
    if method == 'spatial' and sample_rate > 0:
        est_true_n = int(round(n_unique / sample_rate))
        est_true_N = int(round(N_total / sample_rate))
    else:
        est_true_n = n_unique
        est_true_N = N_total

    # IRD ヒストグラムのラベル
    ird_labels = []
    for b in range(len(ird_hist)):
        lo = 1 << b
        ird_labels.append(f"{lo//1048576}M" if lo >= 1048576 else
                          f"{lo//1024}K" if lo >= 1024 else str(lo))

    result = {
        'workload': {
            'n': est_true_n,
            'request_multiplier': 1,
            'p_ohw': round(p_ohw, 6),
            'zipf_alpha': round(g_alpha, 4) if not math.isnan(g_alpha) else args.default_alpha,
            'locality': round(L_est, 4),
            'arrival': arrival,
            'hawkes_eta': hawkes_eta,
            'bins': bins_out,
        },
        '_measurement': {
            'source_file': os.path.abspath(args.input),
            'sampling': {
                'method': method,
                'sample_spec': args.sample,
                'effective_rate': sample_rate,
                'scanned_records': scanned_records,
                'kept_records': kept_records,
            },
            'global': {
                'unique_objects_measured': n_unique,
                'unique_objects_estimated_true': est_true_n,
                'requests_measured': N_total,
                'requests_estimated_true': est_true_N,
                'ohw_objects': m_total,
                'ohw_rate': round(p_ohw, 6),
                'zipf_alpha': round(g_alpha, 4) if not math.isnan(g_alpha) else None,
                'zipf_r2': round(g_r2, 4) if not math.isnan(g_r2) else None,
                'mean_object_size': float(sizes.mean()),
            },
            'locality': {
                'L_estimated': round(L_est, 4),
                'window_fraction_w': round(w_est, 6),
                'reuse_events': reuse_events,
                'median_log2_ird': round(2.0 ** (sum_log2_ird / reuse_events), 1) if reuse_events else 0,
            },
            'burstiness': {
                'measured': bool(args.measure_burstiness),
                'interarrival_cv': round(burst_cv, 4) if not math.isnan(burst_cv) else None,
                'arrival': arrival,
                'hawkes_eta': hawkes_eta,
            },
            'reuse_distance_histogram': {
                'labels': ird_labels,
                'counts': ird_hist.tolist(),
            },
            'bins_detail': per_bin,
        },
    }
    return result


# ----------------------------------------------------------------------
# 出力
# ----------------------------------------------------------------------
def write_outputs(result, out_prefix):
    # 1) 生成器/C版互換 config（workload + 最小プレースホルダ）
    config = {
        'version': 1,
        'generated_by': 'measure_trace.py',
        'note': 'Measured from real trace. Adjust cache/algorithms before running.',
        'trace': {'file': '', 'format': 'oraclegeneral'},
        'cache': {'capacity_bytes': 67108864},
        'algorithms': ['lru', 'lfu', 'gdsf', 's3fifo'],
        'workload': result['workload'],
        'sweep': {'mode': 'none'},
        'output': {'csv': 'results.csv'},
    }
    with open(out_prefix + '.config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    # 2) 詳細レポート
    with open(out_prefix + '.measurement.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 3) ビン別 CSV
    with open(out_prefix + '.bins.csv', 'w', encoding='utf-8') as f:
        f.write("bin,size_lo,size_hi,n_b,N_b,m_b,np_b,q_b,r_b,alpha_b,rho_b,mean_size,r2\n")
        for d in result['_measurement']['bins_detail']:
            b = d['bin']
            f.write(f"{b},{1<<b},{1<<(b+1)},{d['n_b']},{d['N_b']},{d['m_b']},"
                    f"{d.get('np_b',0)},{d['q']:.8f},{d['r']:.6f},{d['a']:.4f},"
                    f"{d['rho']:.4f},{d['mean_size']:.1f},{d['r2']:.4f}\n")

    print(f"[出力] {out_prefix}.config.json  (生成器/C版に読み込み可)", file=sys.stderr)
    print(f"[出力] {out_prefix}.measurement.json  (詳細レポート)", file=sys.stderr)
    print(f"[出力] {out_prefix}.bins.csv  (ビン別サマリ)", file=sys.stderr)


def print_summary(result):
    w = result['workload']
    g = result['_measurement']['global']
    loc = result['_measurement']['locality']
    print("\n========== 計測結果サマリ ==========", file=sys.stderr)
    print(f"  ユニーク数 n (推定真値): {w['n']:,}", file=sys.stderr)
    print(f"  リクエスト数 N (測定/推定): {g['requests_measured']:,} / {g['requests_estimated_true']:,}", file=sys.stderr)
    print(f"  全体 OHW率 p:   {w['p_ohw']:.4f}", file=sys.stderr)
    print(f"  全体 Zipf α:    {w['zipf_alpha']:.3f}  (R²={g['zipf_r2']})", file=sys.stderr)
    print(f"  時間的局所性 L: {w['locality']:.3f}  (窓幅 w≈{loc['window_fraction_w']}, 中央IRD≈{loc['median_log2_ird']})", file=sys.stderr)
    print(f"  到着過程:       {w['arrival']}  (η={w['hawkes_eta']})", file=sys.stderr)
    print("====================================\n", file=sys.stderr)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="zstd圧縮OracleGeneralトレースから生成器パラメータを計測",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help='入力トレース（.zst 圧縮 or 生バイナリ）')
    ap.add_argument('-o', '--out', default='trace_measure',
                    help='出力ファイル接頭辞（既定: trace_measure）')
    ap.add_argument('--sample', default=None,
                    help='サンプリング指定: 割合(0.1) または サイズ長(3GiB/500MiB/...)')
    ap.add_argument('--method', choices=['auto', 'spatial', 'prefix', 'full'],
                    default='auto',
                    help='サンプリング法（auto: 割合→spatial, サイズ→prefix）')
    ap.add_argument('--total-size', default=None,
                    help='全展開サイズ（spatial+サイズ / prefix+割合 の換算用, 例 30GiB）')
    ap.add_argument('--est-ratio', type=float, default=3.5,
                    help='zstd 展開倍率の推定値（--total-size 未指定時, 既定 3.5）')
    ap.add_argument('--zstd', dest='zstd', action='store_true', default=None,
                    help='zstd として扱う（既定は拡張子 .zst で自動判定）')
    ap.add_argument('--raw', dest='zstd', action='store_false',
                    help='非圧縮の生バイナリとして扱う')
    ap.add_argument('--no-burstiness', dest='measure_burstiness',
                    action='store_false', default=True,
                    help='バースト性（タイムスタンプ間隔）計測を無効化')
    ap.add_argument('--default-alpha', type=float, default=0.9,
                    help='空ビン/フィット不能時の既定 Zipf α（既定 0.9）')
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"エラー: ファイルが見つかりません: {args.input}")

    result = measure(args.input, args)
    write_outputs(result, args.out)
    print_summary(result)


if __name__ == '__main__':
    main()
