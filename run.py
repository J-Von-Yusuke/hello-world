"""
トレース取得〜ヒストグラム変換〜分布安定性分析 一括実行スクリプト
================================================================

使い方:
    # URLからダウンロードして分析
    python run_analysis.py

    # すでにダウンロード済みの .zst ファイルがある場合
    python run_analysis.py --file w02.oracleGeneral.bin.zst

依存:
    pip install zstandard numpy pandas matplotlib scipy
"""

import argparse
import io
import os
import struct
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import entropy
import matplotlib
matplotlib.use("Agg")           # GUI なし環境でも動作
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ── 日本語フォント設定（Windows / Mac / Linux 自動判別）────────────────────────
import platform
_OS = platform.system()
if _OS == "Windows":
    matplotlib.rc("font", family="MS Gothic")
elif _OS == "Darwin":
    matplotlib.rc("font", family="Hiragino Sans")
else:
    # Linux: 利用可能な日本語フォントを探す
    from matplotlib import font_manager as _fm
    _jp_fonts = [f.name for f in _fm.fontManager.ttflist
                 if any(k in f.name for k in ("Gothic", "Mincho", "Noto", "IPAex"))]
    if _jp_fonts:
        matplotlib.rc("font", family=_jp_fonts[0])
    else:
        # 日本語フォントがない場合は英語ラベルにフォールバック
        _JP_FALLBACK = True
    print(f"  フォント: {matplotlib.rcParams['font.family']}")

matplotlib.rcParams["axes.unicode_minus"] = False   # マイナス記号の文字化け防止


# ── 定数 ─────────────────────────────────────────────────────────────────────
DEFAULT_URL    = ("https://cache-datasets.s3.amazonaws.com/"
                  "cache_dataset_oracleGeneral/2015_cloudphysics/"
                  "w02.oracleGeneral.bin.zst")
RECORD_FORMAT  = "<IQIq"
RECORD_SIZE    = struct.calcsize(RECORD_FORMAT)   # 24 bytes
NUM_BUCKETS    = 21
WINDOW_SECS    = 3600

STABLE_THRESH  = 0.05
MODERATE_THRESH = 0.20
EPSILON        = 1e-10

assert RECORD_SIZE == 24


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1: ダウンロード
# ══════════════════════════════════════════════════════════════════════════════

def download_file(url: str, dest: str) -> str:
    if os.path.exists(dest):
        print(f"[STEP1] キャッシュを使用: {dest}")
        return dest
    print(f"[STEP1] ダウンロード中: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded/1e6:.1f} / {total/1e6:.1f} MB"
                          f"  ({downloaded/total*100:.1f}%)", end="", flush=True)
    print(f"\n  完了: {os.path.getsize(dest)/1e6:.1f} MB  -> {dest}")
    return dest


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2: バイナリ → ヒストグラム CSV
# ══════════════════════════════════════════════════════════════════════════════

def size_to_bucket(size: int) -> int:
    if size <= 0:
        return 0
    return min(size.bit_length() - 1, 20)


def open_stream(filepath: str):
    suffix = Path(filepath).suffix.lower()
    if suffix in (".zst", ".zstd"):
        try:
            import zstandard as zstd
        except ImportError:
            sys.exit("エラー: pip install zstandard が必要です。")
        return zstd.ZstdDecompressor().stream_reader(open(filepath, "rb"))
    return open(filepath, "rb")


def parse_to_histogram(filepath: str, csv_out: str) -> pd.DataFrame:
    if os.path.exists(csv_out):
        print(f"[STEP2] キャッシュを使用: {csv_out}")
        return pd.read_csv(csv_out)

    print(f"[STEP2] バイナリ解析中: {filepath}")
    stream      = open_stream(filepath)
    win_start   = None
    counts      = np.zeros(NUM_BUCKETS, dtype=np.int64)
    windows     = []
    n_rec       = 0

    with stream:
        while True:
            raw = stream.read(RECORD_SIZE)
            if len(raw) < RECORD_SIZE:
                break
            ts, obj_id, obj_size, _ = struct.unpack(RECORD_FORMAT, raw)
            n_rec += 1

            if win_start is None:
                win_start = ts - (ts % WINDOW_SECS)

            while ts >= win_start + WINDOW_SECS:
                windows.append((win_start, counts.copy()))
                counts[:] = 0
                win_start += WINDOW_SECS

            counts[size_to_bucket(obj_size)] += 1

            if n_rec % 5_000_000 == 0:
                print(f"  {n_rec:,} レコード  ({len(windows)} 窓完了)", flush=True)

    if counts.any():
        windows.append((win_start, counts.copy()))

    print(f"  合計: {n_rec:,} レコード  {len(windows)} 時間窓")

    rows = []
    for i, (t_start, cnt) in enumerate(windows):
        row = {"window": i, "time_start": t_start}
        for b in range(NUM_BUCKETS):
            row[f"bucket_{b}"] = int(cnt[b])
        rows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(csv_out, index=False, encoding="utf-8-sig")
    print(f"  CSV 保存: {csv_out}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3: 分布安定性分析
# ══════════════════════════════════════════════════════════════════════════════

def js_div(p: np.ndarray, q: np.ndarray) -> float:
    m = 0.5 * (p + q)
    return float(0.5 * entropy(p, m) + 0.5 * entropy(q, m))


def to_prob(counts: np.ndarray) -> np.ndarray:
    c = counts.astype(float) + EPSILON
    return c / c.sum()


def analyze_stability(df: pd.DataFrame, name: str) -> dict:
    bucket_cols = [f"bucket_{b}" for b in range(NUM_BUCKETS)]
    hist = df[bucket_cols].values.astype(float)
    T    = len(hist)

    probs   = np.array([to_prob(hist[i]) for i in range(T)])
    js_list = [js_div(probs[i], probs[i+1]) for i in range(T-1)]
    js_arr  = np.array(js_list)

    mean_js = float(np.mean(js_arr))
    stability = ("安定" if mean_js < STABLE_THRESH
                 else "中程度" if mean_js < MODERATE_THRESH
                 else "高変化")

    return {
        "name":      name,
        "windows":   T,
        "mean_JS":   round(mean_js, 5),
        "max_JS":    round(float(np.max(js_arr)), 5),
        "std_JS":    round(float(np.std(js_arr)), 5),
        "p95_JS":    round(float(np.percentile(js_arr, 95)), 5),
        "stability": stability,
        "_js":       js_arr,
        "_hist":     hist,
        "_probs":    probs,
    }


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: グラフ出力
# ══════════════════════════════════════════════════════════════════════════════

STABILITY_COLOR = {"安定": "#4CAF50", "中程度": "#FF9800", "高変化": "#F44336"}


def plot_js_timeseries(r: dict, out_dir: str):
    """JS ダイバージェンス時系列"""
    js   = r["_js"]
    col  = STABILITY_COLOR[r["stability"]]
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(js, color=col, linewidth=0.8, alpha=0.9)
    ax.axhline(r["mean_JS"], color="gray", linestyle="--", linewidth=1,
               label=f"mean = {r['mean_JS']:.5f}")
    ax.axhline(STABLE_THRESH,    color="#4CAF50", linestyle=":", linewidth=1,
               label=f"stable threshold ({STABLE_THRESH})")
    ax.axhline(MODERATE_THRESH,  color="#FF9800", linestyle=":", linewidth=1,
               label=f"moderate threshold ({MODERATE_THRESH})")
    ax.set_title(f"{r['name']}  |  [{r['stability']}]  "
                 f"mean JS={r['mean_JS']:.5f}  max JS={r['max_JS']:.5f}",
                 fontsize=12)
    ax.set_xlabel("Time window index (1 window = 1 hour)")
    ax.set_ylabel("JS Divergence")
    ax.legend(fontsize=9)
    ax.set_ylim(bottom=0)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{r['name']}_js_timeseries.png")
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  グラフ保存: {path}")


def plot_size_dist_heatmap(r: dict, out_dir: str):
    """時間×サイズバケット ヒートマップ（分布の時間変化を可視化）"""
    probs = r["_probs"]                       # shape (T, 21)
    T     = probs.shape[0]

    fig, ax = plt.subplots(figsize=(14, 5))
    im = ax.imshow(probs.T, aspect="auto", origin="lower",
                   cmap="YlOrRd", interpolation="nearest")
    plt.colorbar(im, ax=ax, label="Probability")
    ax.set_xlabel("Time window index (1 window = 1 hour)")
    ax.set_ylabel("Size bucket (2^k bytes)")
    ax.set_yticks(range(NUM_BUCKETS))
    ax.set_yticklabels([f"2^{k}" for k in range(NUM_BUCKETS)], fontsize=7)
    ax.set_title(f"{r['name']}  |  Content size distribution over time\n"
                 f"[{r['stability']}]  mean JS={r['mean_JS']:.5f}",
                 fontsize=11)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{r['name']}_heatmap.png")
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  グラフ保存: {path}")


def plot_overall_size_dist(r: dict, out_dir: str):
    """全窓合算のコンテンツサイズ分布棒グラフ"""
    hist  = r["_hist"].sum(axis=0)
    prob  = to_prob(hist)
    x     = range(NUM_BUCKETS)
    labels = [f"2^{k}" for k in range(NUM_BUCKETS)]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.bar(x, prob, color="#2196F3", edgecolor="white", linewidth=0.4)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=60, fontsize=8)
    ax.set_xlabel("Content size bucket (bytes)")
    ax.set_ylabel("Probability")
    ax.set_title(f"{r['name']}  |  Overall content size distribution (all windows)")
    plt.tight_layout()
    path = os.path.join(out_dir, f"{r['name']}_size_dist.png")
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"  グラフ保存: {path}")


def print_size_dist_summary(r: dict):
    """バケット別カウントをターミナルに表示"""
    hist  = r["_hist"].sum(axis=0)
    total = hist.sum()
    print(f"\n─── コンテンツサイズ分布サマリー ({r['name']}) ──────────────────")
    print(f"  {'Bucket':<8} {'Size range':<22} {'Requests':>12} {'Ratio':>8}")
    print("  " + "-" * 52)
    for b in range(NUM_BUCKETS):
        lo  = 2 ** b
        hi  = "∞  " if b == 20 else f"{2**(b+1):<8}"
        cnt = int(hist[b])
        pct = cnt / total * 100 if total > 0 else 0
        print(f"  2^{b:<5}  [{lo:>8}, {hi})  {cnt:>12,}  {pct:>7.2f}%")
    print(f"  {'Total':<32}  {int(total):>12,}  100.00%")


# ══════════════════════════════════════════════════════════════════════════════
# メイン
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="トレース一括分析スクリプト")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--url",  default=DEFAULT_URL,
                     help="ダウンロードURL（デフォルト: w02.oracleGeneral.bin.zst）")
    src.add_argument("--file", help="ローカルの .bin.zst / .bin ファイル")
    parser.add_argument("--out_dir", default="./analysis_output",
                        help="出力ディレクトリ（デフォルト: ./analysis_output）")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    # ── STEP 1: ファイル取得 ─────────────────────────────────────────────────
    if args.file:
        bin_path = args.file
        trace_name = Path(bin_path).name.split(".")[0]
    else:
        fname      = Path(args.url).name                          # w02.oracleGeneral.bin.zst
        trace_name = fname.split(".")[0]                          # w02
        bin_path   = os.path.join(args.out_dir, fname)
        download_file(args.url, bin_path)

    # ── STEP 2: ヒストグラム CSV 生成 ─────────────────────────────────────────
    csv_path = os.path.join(args.out_dir, f"{trace_name}_histogram.csv")
    df = parse_to_histogram(bin_path, csv_path)

    # ── STEP 3: 安定性分析 ────────────────────────────────────────────────────
    print(f"\n[STEP3] 分布安定性分析: {trace_name}")
    result = analyze_stability(df, trace_name)

    print_size_dist_summary(result)

    print(f"\n─── 安定性スコア ({trace_name}) ─────────────────────────────────")
    mark = {"安定": "✅", "中程度": "⚠️ ", "高変化": "❌"}[result["stability"]]
    print(f"  {mark} 安定性グループ : {result['stability']}")
    print(f"  平均 JS距離       : {result['mean_JS']}")
    print(f"  最大 JS距離       : {result['max_JS']}")
    print(f"  標準偏差          : {result['std_JS']}")
    print(f"  95パーセンタイル  : {result['p95_JS']}")
    print(f"  時間窓数          : {result['windows']}")

    # ── STEP 4: グラフ出力 ────────────────────────────────────────────────────
    print(f"\n[STEP4] グラフ生成中...")
    plot_js_timeseries(result,      args.out_dir)
    plot_size_dist_heatmap(result,  args.out_dir)
    plot_overall_size_dist(result,  args.out_dir)

    # サマリー CSV
    summary = {k: v for k, v in result.items() if not k.startswith("_")}
    pd.DataFrame([summary]).to_csv(
        os.path.join(args.out_dir, f"{trace_name}_stability_summary.csv"),
        index=False, encoding="utf-8-sig"
    )

    print(f"\n✅ 完了。結果: {args.out_dir}/")
    print(f"   - {trace_name}_histogram.csv         (ヒストグラム時系列)")
    print(f"   - {trace_name}_js_timeseries.png     (JS距離の時系列グラフ)")
    print(f"   - {trace_name}_heatmap.png           (サイズ分布ヒートマップ)")
    print(f"   - {trace_name}_size_dist.png         (全体サイズ分布)")
    print(f"   - {trace_name}_stability_summary.csv (安定性スコア)")


if __name__ == "__main__":
    main()
