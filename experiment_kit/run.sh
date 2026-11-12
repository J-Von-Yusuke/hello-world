#!/bin/bash
# run.sh — 実験キットの入口。使い方は README.md
#   ./run.sh setup              cachesim のビルド（パッチ適用）と C ツールのコンパイル
#   ./run.sh check              traces.tsv の検査（パスがあるか、名前の重複）
#   ./run.sh validity [name..]  トレース実験（CDN 妥当性検証と同じ）。name を省くと traces.tsv の全部
#   ./run.sh synth              合成トレースの評価
#   ./run.sh aggregate [--by category,source]   集計（CSV と report.html）
#   ./run.sh tune [name..]      自動推定の実験（分類閾値 × 容量の配分 × 再挿入の閾値。TUNE_JOBS）→ work/tune/
#   ./run.sh tune-synth         同じ実験を合成の 95:5・3:1 で（答えが分かっている負荷での確認）
#   ./run.sh tune-figs          tune の図だけ作り直す → work/tune/figs/
#   ./run.sh all                setup → validity → synth → aggregate
set -u
KIT=$(cd "$(dirname "$0")" && pwd)
. "$KIT/config.sh"
B=$WORK/bin
mkdir -p "$WORK"/{traces,out,results,synth,bin}
log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------- setup
setup() {
  for c in gcc cmake git python3 make; do command -v $c >/dev/null || die "$c が無い"; done
  local src=$LCS_SRC PT=$KIT/regionflash.patch
  if [ -z "$src" ]; then   # 自分で clone して土台に合わせる
    src=$WORK/src
    [ -d "$src/.git" ] || git clone -q "$LCS_REPO" "$src" || die "clone 失敗"
    [ -d "$src/libCacheSim/cache/eviction/flash" ] || git -C "$src" checkout -q "$LCS_BASE" || die "checkout $LCS_BASE 失敗"
  fi
  if [ ! -d "$src/libCacheSim/cache/eviction/flash" ]; then   # 未パッチ（LCS_SRC の既存 clone も）なら当てる。ブランチは動かさない
    [ -d "$src/.git" ] || die "$src は git の clone ではない（パッチ済みのソースか、libCacheSim の clone を指定）"
    local head; head=$(git -C "$src" rev-parse --short HEAD)
    git -C "$src" apply --check --whitespace=nowarn "$PT" 2>"$WORK/apply.err" ||
      die "$src（HEAD $head）にパッチが当たらない: $WORK/apply.err。土台は $LCS_BASE → git -C $src checkout $LCS_BASE（変更中なら先に git stash）"
    git -C "$src" apply --whitespace=nowarn "$PT" || die "パッチの適用に失敗"
    log "パッチを当てた: $src（HEAD $head）"
    [ "$(git -C "$src" rev-parse HEAD)" = "$(git -C "$src" rev-parse -q --verify "$LCS_BASE^{commit}")" ] ||
      log "注意: HEAD $head は土台 $LCS_BASE と違う。当たったが、結果を検証済みなのは $LCS_BASE の上だけ"
  fi
  if [ ! -x "$CACHESIM" ] || [ ! -x "$TRACECONV" ]; then
    mkdir -p "$WORK/b" && (cd "$WORK/b" && cmake -DCMAKE_BUILD_TYPE=Release "$src" > cmake.log 2>&1 &&
      make cachesim traceConv -j"$NCPU" > make.log 2>&1) || die "ビルド失敗: $WORK/b/make.log を見る"
  fi
  local s=$KIT/src
  gcc -O2 -o "$B/og_info" "$s/og_info.c" -lzstd &&
  gcc -O2 -o "$B/og_pack" "$s/og_pack.c" -lzstd &&
  gcc -O2 -o "$B/og_xform" "$s/og_xform.c" -lzstd &&
  gcc -O2 -o "$B/og_sat_profile" "$s/og_sat_profile.c" -lzstd &&
  gcc -O2 -o "$B/cdn_prefix_check" "$s/cdn_prefix_check.c" -lzstd &&
  gcc -O2 -o "$B/og_sat_ops" "$s/og_sat_ops.c" -lzstd &&
  gcc -O2 -o "$B/og_sample" "$s/og_sample.c" -lzstd &&
  gcc -O3 -march=native -pthread -o "$B/og_trace_stats" "$s/og_trace_stats.c" -lzstd -lm &&
  gcc -O3 -march=native -o "$WORK/synth/synth_gen" "$s/synth_gen.c" -lm &&
  gcc -O3 -march=native -o "$WORK/synth/synth_model" "$s/synth_model.c" -lm || die "C ツールのコンパイル失敗（libzstd-dev が要る）"
  log "setup OK: $CACHESIM"
}

# ---------------------------------------------------------------- traces.tsv
# 1 行 = name path format params category source ssd note を \x1f 区切りで出す（空欄を保つ）
rows() { awk -F'\t' 'BEGIN{OFS="\037"} /^#/||NF==0||$1=="name"{next} {$1=$1; print}' "${TRACES_TSV:-$KIT/traces.tsv}"; }
each_trace() {  # each_trace <fn> [names..]   fn には name path format params category source ssd を渡す
  local fn=$1; shift; local want=" $* " name path fmt params cat src ssd note
  while IFS=$'\x1f' read -r name path fmt params cat src ssd note; do
    [ $# -gt 0 ] && [[ "$want" != *" $name "* ]] && continue
    "$fn" "$name" "$path" "${fmt:-oracleGeneral}" "$params" "$cat" "$src" "$ssd"
  done < <(rows)
}
check_one() { [ -r "$2" ] && echo "OK   $1  $2  [$5 / $6]" || { echo "NONE $1  $2  (読めない)"; BAD=1; }; }
check() { BAD=0; each_trace check_one; local d; d=$(rows | cut -d$'\x1f' -f1 | sort | uniq -d); [ -n "$d" ] && { echo "名前が重複: $d"; BAD=1; }
          [ -z "$(rows)" ] && { echo "traces.tsv にトレースが 1 本も書かれていない"; BAD=1; }; return $BAD; }

# ---------------------------------------------------------------- 1 本のトレース
prep_trace() {  # 作業用の oracleGeneral.zst を $WORK/traces/<name>.zst に用意する
  local name=$1 path=$2 fmt=$3 params=$4 out=$WORK/traces/$1.zst
  [ -s "$out" ] && return
  if [ "$fmt" = oracleGeneral ] && [[ "$path" == *.zst ]]; then ln -sf "$path" "$out"; return; fi
  local raw=$WORK/traces/$name.raw
  if [ "$fmt" = oracleGeneral ]; then cp "$path" "$raw"
  else "$TRACECONV" "$path" "$fmt" ${params:+-t "$params"} --output-format oracleGeneral -o "$raw" || die "$name: 変換失敗"
       [ -s "$raw" ] || raw=$raw.oracleGeneral; fi
  "$B/og_pack" "$raw" "$out" && rm -f "$raw" "$raw.reverse"
}
ssd_bytes() {  # ssd_bytes <指定> <footprint>
  local s=${1:-$SSD_DEFAULT}
  if [[ "$s" == *% ]]; then awk -v f="$2" -v p="${s%\%}" 'BEGIN{printf "%d", f*p/100}'; else numfmt --from=iec "$s"; fi
}
jq1() { python3 -c "import json,sys; print(json.load(open('$1'))['$2'])"; }
auto_p() {  # auto_p <物の数> <footprint> <SSD> <DRAM>  →  "P 1本の見積りGB 空きGB"（P が数字ならそのまま）
  local av; av=$(awk '/^MemAvailable:/{printf "%.0f", $2*1024}' /proc/meminfo)
  awk -v u="$1" -v f="$2" -v s="$3" -v d="$4" -v av="$av" -v c="$NCPU" -v P="$P" -v fr="$MEM_FRAC" \
      -v b="$MEM_BASE" -v a="$MEM_SEEN" -v r="$MEM_RES" -v ro="$MEM_RES_OBJ" -v sf="$MEM_SAFETY" 'BEGIN{
    x = f > 0 ? (s + d) / f : 1; if (x > 1) x = 1          # SSD+DRAM に入る物の割合（容量 ÷ フットプリント）
    e = b + a * u + r * u * x; o = b + ro * u * x; if (o > e) e = o; e *= sf   # 重い方（Region+分類 / 物単位 SSD）
    if (P == "auto") { p = int(av * fr / e); if (p < 1) p = 1; if (p > c) p = c } else p = P
    printf "%d %.1f %.1f\n", p, e / 2^30, av / 2^30 }'
}

variant_run() {  # variant_run <name> <var: asis|fix|drop|delete> <trace.zst> <ssd指定>
  local name=$1 var=$2 tr=$3 ssdspec=$4 O=$WORK/out/$1/$2
  mkdir -p "$O"
  # drop / delete: size == SAT_VALUE の要求を、drop = 無かったことに、delete = その物を消す操作に（cachesim の -t と同じ意味）
  local TP="" SENV=""
  case $var in drop|delete) TP="-t $var-obj-size=$SAT_VALUE"; SENV="SAT_DROP=$SAT_VALUE SAT_OP=$var";; esac
  [ -s "$O/info.json" ] || env $SENV "$B/og_info" "$tr" > "$O/info.json" || die "$name: og_info 失敗"
  local fp n sec; fp=$(jq1 "$O/info.json" footprint); n=$(jq1 "$O/info.json" n); sec=$(jq1 "$O/info.json" t_sec)
  local SSD DRAM R CH; SSD=$(ssd_bytes "$ssdspec" "$fp"); DRAM=$((SSD / DRAM_DIV)); R=${REGION_BYTES:-$((SSD / REGION_N / 1048576 * 1048576))}
  [ $R -lt 1048576 ] && R=1048576
  CH=$(numfmt --from=iec "$CHUNK"); [ $CH -gt $R ] && CH=$R
  echo "{\"ssd\":$SSD,\"dram\":$DRAM,\"region\":$R,\"chunk\":$CH,\"piece\":$PIECE,\"interval\":$INTERVAL,\"nreq_limit\":\"${NREQ}\"}" > "$O/config.json"
  log "$name/$var: n=$n footprint=$fp SSD=$SSD DRAM=$DRAM R=$R"
  # (a) 通常の物単位 LRU（SSD と同じ容量）の先頭 N 要求の曲線と、初回アクセス（強制ミス）
  [ -s "$O/lru.csv" ] || env $SENV PIECE=$PIECE "$B/cdn_prefix_check" "$tr" 200000 "$INTERVAL" "$SSD" > "$O/lru.csv" || die "$name: cdn_prefix_check 失敗"
  # (b) RegionFlash の全量 1 回の run（区間ごとの累積値から先頭 N 要求の値が全部取れる）
  local J=$O/jobs.txt TM=""; : > "$J"
  [ -x /usr/bin/time ] && TM=1   # 各 run のピーク RSS を <id>/rss に残す（見積り MEM_* の検証用）
  job() {  # job <id> <eviction params> （完成済みはスキップ）
    local d=$O/$1
    echo "[ -f $d/P.json ] || { mkdir -p $d && ${TM:+/usr/bin/time -f %M -o $d/rss} $CACHESIM $tr oracleGeneral RegionFlash $SSD ${NREQ:+-n $NREQ} $TP -o $d/lcs.txt -e \"$2,out=$d/P\" > $d/stdout 2>&1 || echo FAIL $d; }" >> "$J"
  }
  local E0="dram-size=$DRAM,region-size=$R,chunk=$CH,$BASE_E,interval=$INTERVAL" h hid hs v bud
  for bud in "" $BUDGET_DWPD; do
    [ -n "$bud" ] && [ "$sec" -lt 3600 ] && continue   # 予算は「1 日あたり」なので、1 時間未満のトレースでは意味がない
    for h in "${HINTS[@]}"; do
      hid=${h%%:*}; hs=${h#*:}
      for v in "" h1; do
        [ "$WITH_REINSERT" = 0 ] && [ -n "$v" ] && continue
        job "${bud:+d${bud}_}$hid${v:+_$v}" "$E0,$hs${v:+,$REINSERT}${bud:+,admit=budget:dwpd=$bud}"
      done
    done
    # 基準: 理想の SSD（Region なしの物単位 LRU）。同じ予算・同じ DRAM
    [ -n "$bud" ] && job "obj_d$bud" "dram-size=$DRAM,ssd-mode=object,ssd-algo=lru,dram-algo=lru,inclusion=cachelib,seed=1,interval=$INTERVAL,hint=none,admit=budget:dwpd=$bud"
  done
  local pe; pe=($(auto_p "$(jq1 "$O/info.json" uniq)" "$fp" "$SSD" "$DRAM"))
  log "$name/$var: 同時実行 P=${pe[0]}（1 本の見積り ${pe[1]} GB、空き ${pe[2]} GB、MEM_FRAC=$MEM_FRAC）"
  [ "$P" = auto ] && awk -v e="${pe[1]}" -v a="${pe[2]}" -v fr="$MEM_FRAC" 'BEGIN{exit !(e > a*fr)}' &&
    log "$name/$var: 警告: 1 本の見積りが使えるメモリを超える。P=1 で走らせるが OOM の恐れ"
  tr "\n" "\0" < "$J" | xargs -0 -P "${pe[0]}" -I{} bash -c "{}"
  local mx; mx=$(cat "$O"/*/rss 2>/dev/null | grep -E '^[0-9]+$' | sort -n | tail -1)
  [ -n "$mx" ] && log "$name/$var: 実測ピーク RSS の最大 $(awk -v k="$mx" 'BEGIN{printf "%.2f", k/2^20}') GB（見積り ${pe[1]} GB）"
  # (c) 結果の要約
  WRITE_MARKS_TB="$WRITE_MARKS_TB" python3 "$KIT/py/trace_one.py" "$name" "$var" "$O" || log "$name/$var: 要約に失敗"
}

validity_one() {  # name path fmt params category source ssd
  local name=$1 path=$2 fmt=$3 params=$4 ssd=$7 tr=$WORK/traces/$1.zst
  [ -r "$path" ] || { log "$name: $path が読めない。飛ばす"; return; }
  prep_trace "$name" "$path" "$fmt" "$params"
  local O=$WORK/out/$name; mkdir -p "$O"
  [ -s "$O/info.json" ] || "$B/og_info" "$tr" > "$O/info.json" || die "$name: og_info 失敗"
  [ -s "$O/sat.txt" ] || "$B/og_sat_profile" "$tr" "$(jq1 "$O/info.json" n)" > "$O/sat.txt" 2>&1
  local sat; sat=$(jq1 "$O/info.json" sat_reqs); local vars
  if [ "$SAT" = auto ]; then [ "$sat" -gt 0 ] && vars=$SAT_VARIANTS || vars=asis; else vars=$SAT; fi   # auto: 埋め値があれば SAT_VARIANTS（先頭が主結果）
  if [ "$STATS" = 1 ] && [ ! -s "$O/stats.csv" ]; then
    local L=$(( $(jq1 "$O/info.json" n) / 7 )); [ $L -gt 10000000 ] && L=10000000
    "$B/og_trace_stats" -w 10 -L "$L" -o "$O/stats.csv" "$tr" > "$O/stats.md" 2>"$O/stats.err" || log "$name: og_trace_stats 失敗（統計なしで続ける）"
  fi
  local var t
  for var in $vars; do
    t=$tr   # asis / drop / delete は元のトレースをそのまま使う（drop / delete は cachesim の -t で処理）
    if [ "$var" = fix ]; then
      t=$WORK/traces/$name.fix.zst
      [ -s "$t" ] || "$B/og_xform" "$tr" "$t" fix > "$O/xform.txt" 2>&1 || die "$name: og_xform 失敗"
    fi
    variant_run "$name" "$var" "$t" "$ssd"
  done
}

# ---------------------------------------------------------------- 合成負荷
synth() {
  local S=$WORK/synth; cp "$KIT"/py/synth_run.py "$KIT"/py/scaled.py "$KIT"/py/writes.py "$S/"
  export SYNTH_HOME=$S CACHESIM SYNTH_P
  mkdir -p "$S/o"
  grep -v '^\s*#' "${SYNTH_WORKLOADS:-$KIT/synth_workloads.txt}" | sed 's/#.*//' | awk 'NF' | while read -r line; do
    set -- $line
    [ -s "$S/$1.json" ] && { log "synth $1: 済（消せばやり直す）"; continue; }
    log "synth $line"; (cd "$S" && python3 synth_run.py "$@" --n "$SYNTH_N") | tee -a "$S/synth.log"
  done
  if [ "$SYNTH_SCALED" = 1 ]; then
    export SYNTH_SCALED_N
    [ -s "$S/scaled.json" ] || (cd "$S" && python3 scaled.py > scaled.json)
    [ -s "$S/writes.json" ] || (cd "$S" && python3 writes.py > writes.json)
  fi
  log "synth 完了: $S/*.json"
}

# ---------------------------------------------------------------- 自動推定（tune）
tune_run() {  # tune_run <出力名> <trace> <SSD> <DRAM> <R> <chunk> <-t 等> <warmup>  → TUNE_JOBS を全部走らせて要約
  local O=$WORK/tune/$1 tr=$2 SSD=$3 DRAM=$4 R=$5 CH=$6 TP=$7 WU=$8 J s id spec E
  mkdir -p "$O"; J=$O/jobs.txt; : > "$J"
  E="dram-size=$DRAM,region-size=$R,chunk=$CH,dram-algo=lru,inclusion=cachelib,seal-on-repartition=inactive,seed=1,interval=$INTERVAL${WU:+,warmup-req=$WU}"
  for s in "${TUNE_JOBS[@]}"; do
    id=${s%%|*}; spec=${s#*|}
    [[ "$spec" == *ssd-mode=object* ]] && spec="dram-size=$DRAM,dram-algo=lru,inclusion=cachelib,seed=1,interval=$INTERVAL${WU:+,warmup-req=$WU},$spec" || spec="$E,$spec"
    echo "[ -f $O/$id/P.json ] || { mkdir -p $O/$id && $CACHESIM $tr oracleGeneral RegionFlash $SSD ${NREQ:+-n $NREQ} $TP -e \"$spec,out=$O/$id/P\" > $O/$id/stdout 2>&1 || echo FAIL $O/$id; }" >> "$J"
  done
  echo "{\"ssd\":$SSD,\"dram\":$DRAM,\"region\":$R,\"chunk\":$CH,\"warmup\":\"$WU\",\"nreq_limit\":\"$NREQ\"}" > "$O/config.json"
  local p=${TUNE_P:-}; [ -z "$p" ] && { [ "$P" = auto ] && p=$NCPU || p=$P; }
  log "tune $1: ${#TUNE_JOBS[@]} 構成を P=$p で"
  tr "\n" "\0" < "$J" | xargs -0 -P "$p" -I{} bash -c "{}"
  python3 "$KIT/py/tune_one.py" "$O" > "$WORK/tune/$1.json" || log "tune $1: 要約に失敗"
}
tune_one() {  # name path fmt params category source ssd: validity と同じ SSD・DRAM・Region で、主の変種（SAT_VARIANTS の先頭）だけ
  local name=$1 path=$2 fmt=$3 params=$4 ssd=$7 tr=$WORK/traces/$1.zst
  [ -r "$path" ] || { log "$name: $path が読めない。飛ばす"; return; }
  prep_trace "$name" "$path" "$fmt" "$params"
  local O=$WORK/out/$name; mkdir -p "$O"
  [ -s "$O/info.json" ] || "$B/og_info" "$tr" > "$O/info.json" || die "$name: og_info 失敗"
  local var=asis TP=""; [ "$(jq1 "$O/info.json" sat_reqs)" -gt 0 ] && var=${SAT_VARIANTS%% *}
  case $var in drop|delete) TP="-t $var-obj-size=$SAT_VALUE";; esac
  local fp SSD DRAM R CH; fp=$(jq1 "$O/info.json" footprint); SSD=$(ssd_bytes "$ssd" "$fp"); DRAM=$((SSD / DRAM_DIV))
  R=${REGION_BYTES:-$((SSD / REGION_N / 1048576 * 1048576))}; [ $R -lt 1048576 ] && R=1048576
  CH=$(numfmt --from=iec "$CHUNK"); [ $CH -gt $R ] && CH=$R
  local pe; pe=($(auto_p "$(jq1 "$O/info.json" uniq)" "$fp" "$SSD" "$DRAM"))   # validity と同じメモリの見積り（meta は P≤4）
  TUNE_P=${TUNE_P:-${pe[0]}} tune_run "$name" "$tr" "$SSD" "$DRAM" "$R" "$CH" "$TP" ""
  python3 - "$WORK/tune/$name.json" "$5" "$6" "$var" <<'EOF'
import json, sys; d = json.load(open(sys.argv[1])); d.update(category=sys.argv[2], source=sys.argv[3], variant=sys.argv[4]); json.dump(d, open(sys.argv[1], "w"))
EOF
}
tune_synth() {  # 合成の 95:5 と 3:1（1/64 縮小の SSD）。答え（固定の下限の最良）が分かっている負荷で、自動推定が届くかを見る
  local S=$WORK/synth w name spec
  for w in $TUNE_SYNTH; do
    name=${w%%:*}; spec=${w#*:}; spec=${spec//,/ }
    [ -s "$WORK/traces/syn_$name.zst" ] || { "$S/synth_gen" "$WORK/traces/syn_$name.raw" "$TUNE_SYNTH_N" 1 $spec >/dev/null &&
      "$B/og_pack" "$WORK/traces/syn_$name.raw" "$WORK/traces/syn_$name.zst" && rm -f "$WORK/traces/syn_$name.raw"; } || die "syn_$name: 生成失敗"
    tune_run "syn_$name" "$WORK/traces/syn_$name.zst" $((240 * 2**30 / 64)) 0 4194304 16384 "" $((TUNE_SYNTH_N / 4))
    python3 - "$WORK/tune/syn_$name.json" <<'EOF'
import json, sys; d = json.load(open(sys.argv[1])); d.update(category="synth", source="synth", variant="asis"); json.dump(d, open(sys.argv[1], "w"))
EOF
  done
}
tune_figs() { TUNE_W=$TUNE_W python3 "$KIT/py/tune_figs.py" "$WORK" "$@"; }

aggregate() { python3 "$KIT/py/aggregate.py" "$KIT" "$WORK" "$@"; }

# ---------------------------------------------------------------- main
cmd=${1:-help}; [ $# -gt 0 ] && shift
case $cmd in
  setup) setup ;;
  check) check ;;
  validity) check >/dev/null || { check; die "traces.tsv を直す"; }; [ -x "$B/og_info" ] || setup; each_trace validity_one "$@" ;;
  synth) [ -x "$WORK/synth/synth_gen" ] || setup; synth ;;
  aggregate) aggregate "$@" ;;
  tune) check >/dev/null || { check; die "traces.tsv を直す"; }; [ -x "$B/og_info" ] || setup; mkdir -p "$WORK/tune"; each_trace tune_one "$@"; tune_figs ;;
  tune-synth) [ -x "$WORK/synth/synth_gen" ] || setup; mkdir -p "$WORK/tune"; tune_synth; tune_figs ;;
  tune-figs) tune_figs "$@" ;;
  all) setup && check && each_trace validity_one && synth && aggregate ;;
  *) sed -n '2,9p' "$0" ;;
esac
