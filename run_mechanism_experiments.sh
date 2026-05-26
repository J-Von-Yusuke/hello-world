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
# =============================================================================
set -euo pipefail

TRACE_DIR="${1:?使い方: $0 <トレースディレクトリ> [出力ディレクトリ]}"
OUT_DIR="${2:-./output/mechanism}"

# トレース拡張子 (OracleGeneral バイナリ形式)
TRACE_EXT="oracleGeneral"

# キャッシュ容量: WSS の 1%, 5%, 10%, 20%, 30%
CACHE_SIZES="0.01 0.05 0.1 0.2 0.3"

echo "============================================"
echo " メカニズム解明実験"
echo " トレースDir : $TRACE_DIR"
echo " 出力Dir     : $OUT_DIR"
echo " binning     : 1KiB〜8GiB (2の指数乗 25bin, cache_common.POW2_THRESHOLDS)"
echo "============================================"

mkdir -p "$OUT_DIR"

# ─── 実験0: Reuse Distance 分析（最優先: 元仮説の検証）───
echo ""
echo "[実験0] Reuse Distance 分析（OracleGeneral の next_access_vtime を使用）"
python3 reuse_distance_analysis.py \
    --trace-dir "$TRACE_DIR" \
    --out "$OUT_DIR/reuse_dist"

# ─── 実験1: クロスサイズ退避行列 ───
echo ""
echo "[実験1] クロスサイズ退避行列"
python3 eviction_matrix_sim.py \
    --trace-dir "$TRACE_DIR" \
    --cache-sizes $CACHE_SIZES \
    --out "$OUT_DIR/eviction_matrix"

# ─── 実験2: サイズクラス別 MRC ───
echo ""
echo "[実験2] サイズクラス別 MRC（next_vtime ベース高精度 MRC）"
for trace_file in "$TRACE_DIR"/*."$TRACE_EXT" "$TRACE_DIR"/*.csv; do
    [ -f "$trace_file" ] || continue
    echo "  処理中: $trace_file"

    python3 mrc_per_sizeclass.py \
        --trace "$trace_file" \
        --out "$OUT_DIR/mrc" \
        --capacity-fracs 0.05 0.1 0.2 0.3 0.5
done

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
echo "     → ρ < -0.1: 大きいオブジェクトほど再利用が少ない = 仮説2の根拠"
echo ""
echo "  実験1: $OUT_DIR/eviction_matrix/ALL_TRACES_mechanism_summary.csv を確認"
echo "     → asymmetry_score が高い = 仮説1（退避干渉）が主因"
echo ""
echo "  実験2: $OUT_DIR/mrc/*_mrc_all.png を確認"
echo "     → クラス間で MRC の膝位置が大きく異なる = 仮説3（WSS比率差）が主因"
