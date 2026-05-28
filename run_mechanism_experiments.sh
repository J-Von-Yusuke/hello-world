#!/usr/bin/env bash
# =============================================================================
# run_mechanism_experiments.sh
# メカニズム解明実験の一括実行スクリプト
#
# 使い方:
#   bash run_mechanism_experiments.sh <トレースディレクトリ> [出力ディレクトリ]
#
# 例:
#   bash run_mechanism_experiments.sh ./traces ./output/mechanism
#
# 【大規模トレース向けチューニング】
#   下記の変数を環境変数で上書きできる。
#
#   SAMPLE_STRIDE=1    : 全件処理（小〜中規模トレース向け）
#   SAMPLE_STRIDE=10   : 1/10 サンプリング（数 GiB 程度）
#   SAMPLE_STRIDE=50   : 1/50 サンプリング（〜10 GiB 程度）
#   SAMPLE_STRIDE=100  : 1/100 サンプリング（34 GiB 超の大規模向け）
#
#   MAX_REQUESTS_RD    : reuse_distance_analysis の先頭読み込み件数
#                        5_000_000〜10_000_000 が統計的に十分（RD 分布の収束は速い）
#   MAX_REQUESTS_MRC   : mrc_per_sizeclass の先頭読み込み件数
#                        10_000_000 程度で MRC 形状が安定する
#
#   JOBS               : eviction_matrix_sim のトレース並列数
#                        CPU コア数以下に設定（メモリに余裕がある場合は増やす）
# =============================================================================
set -euo pipefail

TRACE_DIR="${1:?使い方: $0 <トレースディレクトリ> [出力ディレクトリ]}"
OUT_DIR="${2:-./output/mechanism}"

# ─── チューニングパラメータ ───────────────────────────────────────
# 34 GiB 超の大規模トレースには SAMPLE_STRIDE=20〜100 を推奨
SAMPLE_STRIDE="${SAMPLE_STRIDE:-20}"

# Reuse Distance 分析: 先頭 N 件に限定（RD 絶対値は保たれる）
MAX_REQUESTS_RD="${MAX_REQUESTS_RD:-5000000}"

# MRC 分析: 先頭 N 件に限定（MRC の形状収束に十分な量）
MAX_REQUESTS_MRC="${MAX_REQUESTS_MRC:-10000000}"

# eviction_matrix_sim の並列処理数（トレースファイルが複数ある場合に有効）
JOBS="${JOBS:-2}"

# キャッシュ容量: WSS の 1%, 5%, 10%, 20%, 30%
CACHE_SIZES="0.01 0.05 0.1 0.2 0.3"
# ────────────────────────────────────────────────────────────────────

echo "============================================"
echo " メカニズム解明実験"
echo " トレースDir   : $TRACE_DIR"
echo " 出力Dir       : $OUT_DIR"
echo " binning       : 1KiB〜8GiB (2の指数乗 25bin)"
echo " sample-stride : 1/${SAMPLE_STRIDE}（eviction_matrix_sim のみ）"
echo " max-req RD    : ${MAX_REQUESTS_RD} 件"
echo " max-req MRC   : ${MAX_REQUESTS_MRC} 件"
echo " 並列ジョブ数  : ${JOBS}"
echo "============================================"

mkdir -p "$OUT_DIR"

# ─── 実験0: Reuse Distance 分析（最優先: 元仮説の検証）────────────
echo ""
echo "[実験0] Reuse Distance 分析（next_access_vtime ベース）"
echo "  先頭 ${MAX_REQUESTS_RD} 件を使用（RD 絶対値は保たれる）"
python3 reuse_distance_analysis.py \
    --trace-dir "$TRACE_DIR" \
    --max-requests "$MAX_REQUESTS_RD" \
    --out "$OUT_DIR/reuse_dist"

# ─── 実験1: クロスサイズ退避行列 ────────────────────────────────
echo ""
echo "[実験1] クロスサイズ退避行列"
echo "  サンプリング: 1/${SAMPLE_STRIDE}、並列: ${JOBS} プロセス"
python3 eviction_matrix_sim.py \
    --trace-dir "$TRACE_DIR" \
    --cache-sizes $CACHE_SIZES \
    --sample-stride "$SAMPLE_STRIDE" \
    --jobs "$JOBS" \
    --out "$OUT_DIR/eviction_matrix"

# ─── 実験2: サイズクラス別 MRC ──────────────────────────────────
echo ""
echo "[実験2] サイズクラス別 MRC（next_vtime ベース高精度 MRC）"
echo "  先頭 ${MAX_REQUESTS_MRC} 件を使用"
python3 mrc_per_sizeclass.py \
    --trace-dir "$TRACE_DIR" \
    --max-requests "$MAX_REQUESTS_MRC" \
    --out "$OUT_DIR/mrc" \
    --capacity-fracs 0.05 0.1 0.2 0.3 0.5

echo ""
echo "============================================"
echo " 全実験完了"
echo " 出力: $OUT_DIR"
echo "============================================"
echo ""
echo "【結果の読み方】"
echo ""
echo "  実験0: $OUT_DIR/reuse_dist/*_hypothesis_test.txt を確認"
echo "     → η² < 0.06: 同サイズのreuse time仮定は崩れている（★ 論文のポイント）"
echo "     → ρ < -0.1 : 大きいオブジェクトほど再利用が少ない = 仮説2の根拠"
echo ""
echo "  実験1: $OUT_DIR/eviction_matrix/ALL_TRACES_mechanism_summary.csv を確認"
echo "     → asymmetry_score が高い = 仮説1（退避干渉）が主因"
echo "     ※ sample-stride=${SAMPLE_STRIDE} のため絶対ヒット率は参考値。"
echo "       非対称スコアの相対比較・ポリシー間比較は有効。"
echo ""
echo "  実験2: $OUT_DIR/mrc/*_mrc_all.png を確認"
echo "     → クラス間で MRC の膝位置が大きく異なる = 仮説3（WSS比率差）が主因"
echo ""
echo "【再実行時のサンプリング調整例】"
echo "  SAMPLE_STRIDE=1 bash $0 $TRACE_DIR $OUT_DIR            # フルトレース（低速・高精度）"
echo "  SAMPLE_STRIDE=100 JOBS=4 bash $0 $TRACE_DIR $OUT_DIR   # 高速（大規模向け）"
