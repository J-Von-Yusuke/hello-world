#!/bin/bash
# che_sens.sh <trace name> [...]  — 11-26: Che モデル（og_che、物単位 LRU、DRAM なし）で、SSD 容量を変えたときの
#   「混ぜた LRU」と「大津の区切りで 2 クラスに分けた LRU（BMR 最小の配分）」の BMR を比べる。
#   分類の得が容量を増やすと消える（逆転する）かを、シミュレーションなしで予言できるかを見るため。
# 入力: traces.tsv（TRACES_TSV）の name と path、work/paper/<name>.json の config.ssd（既定の SSD 容量）、
#       work/indicator/<name>.json の no_filter.cut_log2（大津の区切り）。先に ./run.sh paper と ./run.sh indicator。
# 環境変数: CHE_X="0.33 1 2"（SSD の倍率）、SSD_BYTES（既定の SSD 容量を直接指定）、TAU（区切り [B] を直接指定）
# 出力: work/che_sens/<name>.json（og_che の JSON）と、画面の表。
set -e
cd "$(dirname "$0")"
K=$PWD; TSV=${TRACES_TSV:-$K/traces.tsv}; BIN=$K/work/bin/og_che; O=$K/work/che_sens; X=${CHE_X:-"0.33 1 2"}
[ $# -ge 1 ] || { echo "usage: ./che_sens.sh <trace name> [...]"; exit 1; }
[ -x "$BIN" ] || { echo "$BIN が無い（./run.sh setup）"; exit 1; }
[ -z "$NREQ" ] || echo "注意: og_che はトレース全体を読む（NREQ=$NREQ は効かない）"
echo "og_che sha1 $(sha1sum "$BIN" | cut -c1-10)"
mkdir -p "$O"
for n in "$@"; do
  tr=$(awk -F'\t' -v n="$n" '$1 == n { print $2; exit }' "$TSV")
  [ -n "$tr" ] && [ -f "$tr" ] || { echo "$n: traces.tsv に無いかファイルが無い（$tr）"; continue; }
  s=${SSD_BYTES:-$(python3 -c "import json,sys;print(int(json.load(open(sys.argv[1]))['config']['ssd']))" "$K/work/paper/$n.json" 2>/dev/null)}
  t=${TAU:-$(python3 -c "import json,sys;print(2**int(json.load(open(sys.argv[1]))['no_filter']['cut_log2']))" "$K/work/indicator/$n.json" 2>/dev/null)}
  [ -n "$s" ] || { echo "$n: work/paper/$n.json が無い（SSD_BYTES で指定も可）"; continue; }
  [ -n "$t" ] || { echo "$n: work/indicator/$n.json が無い（TAU で指定も可）"; continue; }
  caps=$(python3 -c "import sys;s=int(sys.argv[1]);print(','.join(str(int(s*float(x))) for x in sys.argv[2:]))" "$s" $X)
  echo "== $n  SSD=$s B  区切り τ=$t B  倍率 $X"
  "$BIN" -C "$caps" -t "$t" -w 0 "$tr" > "$O/$n.json"
  python3 - "$O/$n.json" "$s" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1])); S = float(sys.argv[2])
print(f"  zipf α {d['zipf_all']['alpha']}  初回ミス {d['compulsory_miss']:.4f}")
print("  SSD倍率   T[要求]     混ぜたBMR  分けたBMR  ΔBMR[pt]  小クラスの取り分")
for c in d["capacities"]:
    p = c["splits"][0]["part"]["bhr"]; m = 1 - c["lru"]["bhr"]; q = 1 - p["bhr"]
    print(f"  {c['C'] / S:6.2f}  {float(c['lru']['T']):10.4g}  {m:9.4f}  {q:9.4f}  {100 * (q - m):+8.3f}  {p['share'][0]:8.3f}")
print("  ΔBMR = 分けた − 混ぜた（負 = 分類が得）。DRAM と Region は入らないので、水準より倍率の間の向きを見る。")
EOF
done
