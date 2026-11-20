#!/bin/bash
# run.sh — 実験キットの入口（./run.sh <コマンド> [name..]）。詳しくは README.md、RegionFlash の指定の意味は params.html。
#   [name..] = traces.tsv の name（省くと全部）。結果は work/ の下で、済んだ run は回し直さない（消せばやり直す）。
#
# 準備
#   setup                  libCacheSim の取得・パッチ・cachesim のビルドと C ツールのコンパイル
#   check                  traces.tsv の検査（パスが読めるか、名前の重複）
#   methods                paper の方式のカタログと、組ごとに選んだもの（config.sh）
# 実験
#   validity [name..]      トレース実験（分類方法 HINTS × 再挿入 × 書き込み予算）  → work/out/
#   synth                  合成負荷                                              → work/synth/
#   tune [name..]          自動推定の実験（TUNE_JOBS のうち TUNE_METHODS）          → work/tune/
#   tune-synth             同じ実験を合成の 95:5・3:1 で（答えが分かっている負荷）    → work/tune/
#   che [name..]           Zipf の傾きと分類の利得（付録 §N.5）                     → work/che/
#   paper-final2 [name..]  論文の本番：CacheLib と現在の提案 ru_auto_v2 ほか（PAPER_FINAL2_METHODS）→ work/paper/final2/
#   paper-final [name..]   同じ組で提案を以前の ru_auto にしたもの（PAPER_FINAL_METHODS）     → work/paper/final/
#   paper-robust [name..]  頑健性の確認（PAPER_ROBUST_METHODS）                    → work/paper/robust/
#   paper [name..]         検討用の全部（PAPER_METHODS）                           → work/paper/
#   paper-ext [name..]     CacheLib の書き直しの閾値違い（PAPER_EXT_METHODS）        → work/paper/ext/
#   paper-sens [name..]    感度：SSD 容量・DRAM・RU を一つずつ変える（PAPER_SENS_METHODS × SENS_*）→ work/paper/sens/
#                          （final 系は paper・ext の場所に回して結果を共有し、図と表だけ final*/ に出す）
#   all                    setup → validity → synth → aggregate
# 図と表だけ作り直す（回さない）
#   aggregate [--by category,source]   validity・tune の集計（CSV と report.html）  → work/results/
#   tune-figs              tune の図                                              → work/tune/figs/
#   che-report             che の表と図                                            → work/che/report.html
#   paper-figs [--final2 | --final | --ext | --robust | --sens]  paper の図と表（引数なし = 結果のあるもの全部）
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
  local src=$LCS_SRC PT=$KIT/regionflash.patch sha stamp
  sha=$(sha256sum "$PT" | cut -c1-16)
  if [ -z "$src" ]; then   # 自分で clone して土台に合わせる
    src=$WORK/src
    [ -d "$src/.git" ] || git clone -q "$LCS_REPO" "$src" || die "clone 失敗"
    stamp=$(cat "$src/.rf_patch" 2>/dev/null)
    if [ -d "$src/libCacheSim/cache/eviction/flash" ] && [ "$stamp" != "$sha" ]; then
      # キットのパッチが新しくなった（または印の無い古い setup）: 土台に戻して当て直し、作り直す
      log "regionflash.patch が当てたものと違う → $src を $LCS_BASE に戻して当て直し、cachesim を作り直す"
      git -C "$src" reset -q --hard "$LCS_BASE" && git -C "$src" clean -qfd || die "$src を土台に戻せない"
      rm -f "$WORK/b/bin/cachesim" "$WORK/b/bin/traceConv"
    fi
    [ -d "$src/libCacheSim/cache/eviction/flash" ] || git -C "$src" checkout -q "$LCS_BASE" || die "checkout $LCS_BASE 失敗"
  elif [ -d "$src/libCacheSim/cache/eviction/flash" ] && [ "$(cat "$src/.rf_patch" 2>/dev/null)" != "$sha" ]; then
    log "注意: LCS_SRC=$src のパッチはキットの regionflash.patch と違う可能性がある（手元の clone なので自動では当て直さない）。新しい構成が FAIL するなら、その clone を $LCS_BASE に戻して ./run.sh setup をやり直す"
  fi
  if [ ! -d "$src/libCacheSim/cache/eviction/flash" ]; then   # 未パッチ（LCS_SRC の既存 clone も）なら当てる。ブランチは動かさない
    [ -d "$src/.git" ] || die "$src は git の clone ではない（パッチ済みのソースか、libCacheSim の clone を指定）"
    local head; head=$(git -C "$src" rev-parse --short HEAD)
    git -C "$src" apply --check --whitespace=nowarn "$PT" 2>"$WORK/apply.err" ||
      die "$src（HEAD $head）にパッチが当たらない: $WORK/apply.err。土台は $LCS_BASE → git -C $src checkout $LCS_BASE（変更中なら先に git stash）"
    git -C "$src" apply --whitespace=nowarn "$PT" || die "パッチの適用に失敗"
    echo "$sha" > "$src/.rf_patch"   # どのパッチを当てたかの印（キットを更新したら setup が当て直す）
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
  gcc -O3 -march=native -o "$B/og_che" "$s/og_che.c" -lzstd -lm &&
  gcc -O3 -march=native -o "$WORK/synth/synth_gen" "$s/synth_gen.c" -lm &&
  gcc -O3 -march=native -fopenmp -o "$WORK/synth/synth_model" "$s/synth_model.c" -lm || die "C ツールのコンパイル失敗（libzstd-dev が要る）"
  log "setup OK: $CACHESIM"
}

# ---------------------------------------------------------------- traces.tsv
# 1 行 = name path format params category source ssd note を \x1f 区切りで出す（空欄を保つ）
# 列は見出しの名前で読む（無い列は空。列の順番は自由。見出しの行が無ければ従来の 8 列の並び）。
# 出す順: name path format params category source ssd note amp_key amp_size marks
TSV_COLS="name path format params category source ssd note amp_key amp_size marks"
rows() { awk -F'\t' -v want="$TSV_COLS" 'BEGIN{OFS="\037"; n = split(want, W, " ")}
  /^#/ || NF == 0 {next}
  $1 == "name" {for (i = 1; i <= NF; i++) H[$i] = i; next}
  {if (!("name" in H)) {H["name"]=1; H["path"]=2; H["format"]=3; H["params"]=4; H["category"]=5; H["source"]=6; H["ssd"]=7; H["note"]=8}
   line = ""; for (j = 1; j <= n; j++) {v = (W[j] in H) ? $(H[W[j]]) : ""; line = line (j > 1 ? OFS : "") v}; print line}' "${TRACES_TSV:-$KIT/traces.tsv}"; }
each_trace() {  # each_trace <fn> [names..]   fn には name path format params category source ssd を渡す。増幅と書き込み量の印は TR_AMP_KEY / TR_AMP_SIZE / TR_MARKS で
  local fn=$1; shift; local want=" $* " name path fmt params cat src ssd note amp_key amp_size marks
  while IFS=$'\x1f' read -r name path fmt params cat src ssd note amp_key amp_size marks; do
    [ $# -gt 0 ] && [[ "$want" != *" $name "* ]] && continue
    TR_AMP_KEY=${amp_key:-1} TR_AMP_SIZE=${amp_size:-1} TR_MARKS=${marks:-} "$fn" "$name" "$path" "${fmt:-oracleGeneral}" "$params" "$cat" "$src" "$ssd"
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
cmode() {  # cmode <RegionFlash の指定>: その構成の片の扱い。最後の chunk= が 0（か無い）・物単位 SSD なら object、ほかは CHUNK_MODE
  local c; c=$(grep -o 'chunk=[^,]*' <<< "$1" | tail -1)
  if [ -z "$c" ] || [ "$c" = chunk=0 ] || [[ "$1" == *ssd-mode=object* ]]; then echo object; else echo "$CHUNK_MODE"; fi
}
with_cmode() {  # with_cmode <指定>: piece なら ,chunk-mode=piece を足した指定（object は何も足さない：11-19 以前と同じ指定）
  if [ "$(cmode "$1")" = piece ]; then echo "$1,chunk-mode=piece"; else echo "$1"; fi
}
# 済んだ run の印：P.json があり、同じ片の扱いで回したもの（"chunk_mode": "<mode>"。11-19 以前の P.json には無いので回し直す）
done_test() {  # 済みの印：P.json の片の扱いが同じ。admit=dram の構成は fill の規則（11-19）の版の結果であること
  local t="grep -qs 'chunk_mode.: .$(cmode "$2").' $1/P.json"
  [[ "$2" == *admit=dram* ]] && t="$t && grep -qs 'fill_admits' $1/P.json"
  echo "$t"; }
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
    echo "$(done_test $d "$2") || { mkdir -p $d && ${TM:+/usr/bin/time -f %M -o $d/rss} $CACHESIM $tr oracleGeneral RegionFlash $SSD ${NREQ:+-n $NREQ} $TP -o $d/lcs.txt -e \"$(with_cmode "$2"),out=$d/P\" > $d/stdout2>&1 || echo FAIL $d; }" >> "$J"
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
synth_tools() {  # 合成の道具を、キットの src/ の方が新しければコンパイルし直す（setup をやり直さなくても新しい版になる）
  local S=$WORK/synth s=$KIT/src
  [ "$S/synth_gen" -nt "$s/synth_gen.c" ] || { gcc -O3 -march=native -o "$S/synth_gen" "$s/synth_gen.c" -lm || die "synth_gen のコンパイル失敗"; log "synth_gen をコンパイルした"; }
  [ "$S/synth_model" -nt "$s/synth_model.c" ] || { gcc -O3 -march=native -fopenmp -o "$S/synth_model" "$s/synth_model.c" -lm || die "synth_model のコンパイル失敗"; log "synth_model をコンパイルした"; }
}
synth() {
  synth_tools
  local S=$WORK/synth; cp "$KIT"/py/synth_run.py "$KIT"/py/scaled.py "$KIT"/py/writes.py "$S/"
  export SYNTH_HOME=$S CACHESIM SYNTH_P
  mkdir -p "$S/o"
  grep -v '^\s*#' "${SYNTH_WORKLOADS:-$KIT/synth_workloads.txt}" | sed 's/#.*//' | awk 'NF' | while read -r line; do
    set -- $line
    [ -s "$S/$1.json" ] && { log "synth $1: 済（消せばやり直す）"; continue; }
    log "synth $line"; (cd "$S" && python3 synth_run.py "$@" --n "$SYNTH_N") | tee -a "$S/synth.log"
  done
  if [ "$SYNTH_SCALED" = 1 ]; then
    export SYNTH_SCALED_N CHUNK_MODE   # chunk=16K の構成：片の扱いが今の CHUNK_MODE と違う結果（11-19 以前 = object）は回し直す
    local f; for f in scaled writes; do
      grep -qs "\"chunk_mode\": \"$CHUNK_MODE\"" "$S/$f.json" && { log "synth $f: 済（消せばやり直す）"; continue; }
      log "synth $f.py（chunk-mode=$CHUNK_MODE）"
      (cd "$S" && python3 $f.py > $f.json.tmp) && mv "$S/$f.json.tmp" "$S/$f.json"
    done
  fi
  log "synth 完了: $S/*.json"
}

# ---------------------------------------------------------------- 自動推定（tune）
tune_run() {  # tune_run <出力名> <trace> <SSD> <DRAM> <R> <chunk> <-t 等> <warmup>  → 構成の一覧を全部走らせて要約
  # 構成の一覧は RUN_JOBS（配列の名前。既定 TUNE_JOBS）、出力先は RUN_ROOT（既定 $WORK/tune）。paper も同じ仕組みで走る
  local -n JOBS=${RUN_JOBS:-TUNE_JOBS}
  local ROOT=${RUN_ROOT:-$WORK/tune}
  local O=$ROOT/$1 tr=$2 SSD=$3 DRAM=$4 R=$5 CH=$6 TP=$7 WU=$8 J s id spec E
  mkdir -p "$O"; J=$O/jobs.txt; : > "$J"
  E="dram-size=$DRAM,chunk=$CH,dram-algo=lru,inclusion=cachelib,seal-on-repartition=inactive,seed=1,interval=$INTERVAL${WU:+,warmup-req=$WU}"
  [ "${TR_AMP_KEY:-1}" != 1 ] && E="$E,amp-key=$TR_AMP_KEY"     # traces.tsv の amp_key（CacheLib の ampFactor：要求ごとに K 個の別キー）
  [ "${TR_AMP_SIZE:-1}" != 1 ] && E="$E,amp-size=$TR_AMP_SIZE"  # traces.tsv の amp_size（CacheLib の ampSizeFactor：サイズを F 倍）
  for s in "${JOBS[@]}"; do
    id=${s%%|*}; spec=${s#*|}
    if [[ "$spec" == *ssd-mode=object* ]]; then spec="dram-size=$DRAM,dram-algo=lru,inclusion=cachelib,seed=1,interval=$INTERVAL${WU:+,warmup-req=$WU},$spec"
    elif [[ "$spec" == *region-size=* ]]; then spec="$E,$spec"   # 構成が Region の大きさを持つ（paper の 256 MiB など）
    else spec="$E,region-size=$R,$spec"; fi
    echo "$(done_test $O/$id "$spec") || { mkdir -p $O/$id && $CACHESIM $tr oracleGeneral RegionFlash $SSD ${NREQ:+-n $NREQ} $TP -e \"$(with_cmode "$spec"),out=$O/$id/P\" > $O/$id/stdout 2>&1 || echo FAIL $O/$id; }" >>"$J"
  done
  echo "{\"ssd\":$SSD,\"dram\":$DRAM,\"region\":$R,\"chunk\":$CH,\"warmup\":\"$WU\",\"nreq_limit\":\"$NREQ\"}" > "$O/config.json"
  local p=${TUNE_P:-}; [ -z "$p" ] && { [ "$P" = auto ] && p=$NCPU || p=$P; }
  log "$(basename "$ROOT") $1: ${#JOBS[@]} 構成を P=$p で"
  tr "\n" "\0" < "$J" | xargs -0 -P "$p" -I{} bash -c "{}"
  python3 "$KIT/py/tune_one.py" "$O" > "$ROOT/$1.json" || log "$1: 要約に失敗"
}
trace_cfg() {  # trace_cfg <name> <path> <fmt> <params> <ssd指定>: tune / che 共通。validity と同じ SSD・DRAM・Region で、主の変種（SAT_VARIANTS の先頭）
  # 結果は呼んだ側の local に入れる: tr var TP SENV fp SSD DRAM R CH u（呼ぶ側で local 宣言しておく）
  local name=$1 O=$WORK/out/$1
  tr=$WORK/traces/$1.zst
  prep_trace "$name" "$2" "$3" "$4"
  mkdir -p "$O"
  [ -s "$O/info.json" ] || "$B/og_info" "$tr" > "$O/info.json" || die "$name: og_info 失敗"
  # 変種は validity の主結果と同じ（SAT=auto なら埋め値があるとき SAT_VARIANTS の先頭、指定があればその先頭）
  var=asis TP="" SENV=""
  if [ "$SAT" = auto ]; then [ "$(jq1 "$O/info.json" sat_reqs)" -gt 0 ] && var=${SAT_VARIANTS%% *}; else var=${SAT%% *}; fi
  case $var in drop|delete) TP="-t $var-obj-size=$SAT_VALUE"; SENV="SAT_DROP=$SAT_VALUE SAT_OP=$var";; esac
  if [ "$var" = fix ]; then
    local tf=$WORK/traces/$name.fix.zst
    [ -s "$tf" ] || "$B/og_xform" "$tr" "$tf" fix > "$O/xform.txt" 2>&1 || die "$name: og_xform 失敗"
    tr=$tf
  fi
  # フットプリントは埋め値（size = SAT_VALUE）を除いて数えた、その変種の info.json（validity と共用）
  mkdir -p "$O/$var"
  [ -s "$O/$var/info.json" ] || env $SENV "$B/og_info" "$tr" > "$O/$var/info.json" || die "$name: og_info 失敗"
  fp=$(jq1 "$O/$var/info.json" footprint)
  fp=$(awk -v f="$fp" -v k="${TR_AMP_KEY:-1}" -v a="${TR_AMP_SIZE:-1}" 'BEGIN{printf "%.0f", f * k * a}')   # 増幅したトレースのフットプリント
  SSD=$(ssd_bytes "$5" "$fp"); [ -n "${SSD_MULT:-}" ] && SSD=$(awk -v s="$SSD" -v m="$SSD_MULT" 'BEGIN{printf "%.0f", s * m}')   # paper-sens
  DRAM=$((SSD / DRAM_DIV))
  R=${REGION_BYTES:-$((SSD / REGION_N / 1048576 * 1048576))}; [ $R -lt 1048576 ] && R=1048576
  CH=$(numfmt --from=iec "$CHUNK"); [ $CH -gt $R ] && CH=$R
  u=$(awk -v n="$(jq1 "$O/$var/info.json" uniq)" -v k="${TR_AMP_KEY:-1}" 'BEGIN{printf "%.0f", n * k}')   # amp_key 倍の物（メモリも K 倍）
}
tune_one() {  # name path fmt params category source ssd: validity と同じ SSD・DRAM・Region で、主の変種（SAT_VARIANTS の先頭）だけ
  local name=$1 path=$2 tr var TP SENV fp SSD DRAM R CH u
  [ -r "$path" ] || { log "$name: $path が読めない。飛ばす"; return; }
  trace_cfg "$name" "$path" "$3" "$4" "$7"
  log "tune $name/$var: footprint=$fp SSD=$SSD DRAM=$DRAM R=$R chunk=$CH amp_key=${TR_AMP_KEY:-1} amp_size=${TR_AMP_SIZE:-1} marks=${TR_MARKS:-}"
  local pe; pe=($(auto_p "$u" "$fp" "$SSD" "$DRAM"))   # validity と同じメモリの見積り（meta は P≤4）
  log "tune $name/$var: 物 $u、同時実行 P=${TUNE_P:-${pe[0]}}（1 本の見積り ${pe[1]} GB、空き ${pe[2]} GB）"
  TUNE_P=${TUNE_P:-${pe[0]}} tune_run "$name" "$tr" "$SSD" "$DRAM" "$R" "$CH" "$TP" ""
  python3 - "${RUN_ROOT:-$WORK/tune}/$name.json" "$5" "$6" "$var" "${TR_AMP_KEY:-1}" "${TR_AMP_SIZE:-1}" "${TR_MARKS:-}" <<'EOF'
import json, sys; d = json.load(open(sys.argv[1]))
d.update(category=sys.argv[2], source=sys.argv[3], variant=sys.argv[4], amp_key=float(sys.argv[5]), amp_size=float(sys.argv[6]), marks=sys.argv[7])
json.dump(d, open(sys.argv[1], "w"))
EOF
}
# ---------------------------------------------------------------- Zipf の傾きと分類の利得（che。付録 §N.5）
che_one() {  # name path fmt params category source ssd → work/che/<name>.json
  local name=$1 path=$2 tr var TP SENV fp SSD DRAM R CH u O=$WORK/che/$1 T=$WORK/tune/$1
  [ -r "$path" ] || { log "$name: $path が読めない。飛ばす"; return; }
  trace_cfg "$name" "$path" "$3" "$4" "$7"
  mkdir -p "$O"
  # (3)(4) RegionFlash：分類なし・split_threshold（Region の寿命と終了時の Region を記録）。tune に none があればそれを使う。
  # (1) の上限は libCacheSim の素の LRU（SSD と同じ容量、DRAM なし。Che の予測と同じ条件）
  if [ "$CHE_SIM" = 1 ]; then
    local n iv E J=$O/jobs.txt id spec d ev; n=$(jq1 "$WORK/out/$name/$var/info.json" n); iv=$(( n / 400 )); [ $iv -lt 10000 ] && iv=10000
    E="dram-size=$DRAM,chunk=$CH,dram-algo=lru,inclusion=cachelib,seal-on-repartition=inactive,seed=1,interval=$iv"
    [ "${TR_AMP_KEY:-1}" != 1 ] && E="$E,amp-key=$TR_AMP_KEY"; [ "${TR_AMP_SIZE:-1}" != 1 ] && E="$E,amp-size=$TR_AMP_SIZE"
    : > "$J"
    for id in none st_lru; do
      case $id in
        none) spec="$E,region-size=$R,hint=none,region-evict=lru";;
        st_lru) spec="$E,region-size=$R,hint=split_threshold,region-evict=lru";;
      esac
      [ "$id" = none ] && eval "$(done_test "$T/none" "$spec")" && continue   # 同じ片の扱いの tune の結果を使う（st_lru は記録が要るので回す）
      d=$O/$id ev=""; [ "$id" = st_lru ] && ev="RF_EVLOG=$d/evlog RF_ENDDUMP=$d/enddump"
      echo "$(done_test $d "$spec") || { mkdir -p $d && env $ev $CACHESIM $tr oracleGeneral RegionFlash $SSD ${NREQ:+-n $NREQ} $TP -e \"$(with_cmode "$spec"),out=$d/P\" > $d/stdout 2>&1 || echo FAIL $d; }" >> "$J"
    done
    # 増幅つきのトレースでは素の LRU は回さない（cachesim の lru は amp を知らない）
    [ "${TR_AMP_KEY:-1}" = 1 ] && [ "${TR_AMP_SIZE:-1}" = 1 ] &&
      echo "[ -s $O/lru/res ] || { mkdir -p $O/lru && $CACHESIM $tr oracleGeneral lru $SSD ${NREQ:+-n $NREQ} $TP > $O/lru/stdout 2>&1 && grep 'byte miss ratio' $O/lru/stdout | tail -1 > $O/lru/res || echo FAIL $O/lru; }" >> "$J"
    local pe; pe=($(auto_p "$u" "$fp" "$SSD" "$DRAM"))
    log "che $name/$var: RegionFlash $(wc -l < "$J") 本を P=${pe[0]} で（SSD=$SSD R=$R）"
    tr "\n" "\0" < "$J" | xargs -0 -P "${pe[0]}" -I{} bash -c "{}"
    echo "{\"ssd\":$SSD,\"dram\":$DRAM,\"region\":$R,\"chunk\":$CH,\"n\":$n,\"interval\":$iv,\"nreq_limit\":\"$NREQ\"}" > "$O/config.json"
  fi
  # (1)(2) Che 近似の予測（物単位 LRU と静的分割。Region は入らない）。分類の閾値は CHE_TAUS と、split_threshold が最後に使った τ
  local targs=() t st f; for t in $CHE_TAUS; do targs+=(-t "$(numfmt --from=iec ${t//,/ } | paste -sd,)"); done
  for f in "$O/st_lru/P.json" "$T/st_lru/P.json"; do
    st=$(python3 -c "import json; print(','.join(str(x) for x in json.load(open('$f'))['config'].get('taus') or []))" 2>/dev/null) && break
  done
  [ -n "$st" ] && [[ " ${targs[*]} " != *" $st "* ]] && targs+=(-t "$st")   # 同じ組は 1 度だけ
  # 増幅（amp_key K・amp_size F）は「容量 ÷ (K·F) の元のトレース」と同じ（λ が 1/K、物が K 倍・大きさが F 倍）
  local Cm; Cm=$(awk -v s="$SSD" -v k="${TR_AMP_KEY:-1}" -v a="${TR_AMP_SIZE:-1}" 'BEGIN{printf "%.0f", s / k / a}')
  [ -s "$O/che.json" ] || { log "che $name: og_che（容量 $Cm、閾値 ${targs[*]}）"; env $SENV "$B/og_che" -C "$Cm" -w "$CHE_W" "${targs[@]}" "$tr" > "$O/che.json" || die "$name: og_che 失敗"; }
  python3 "$KIT/py/che_one.py" "$O" "$T" "$name" "$5" "$6" "$var" "$TUNE_W" > "$WORK/che/$name.json" || log "che $name: 要約に失敗"
}
che_report() { python3 "$KIT/py/che_report.py" "$WORK/che" "$@"; }

tune_synth() {  # 合成の 95:5 と 3:1（1/64 縮小の SSD）。答え（固定の下限の最良）が分かっている負荷で、自動推定が届くかを見る
  local S=$WORK/synth w name spec av
  if [ -z "${TUNE_P:-}" ]; then   # 1 本 約 1.6〜1.9 GB（6,000 万要求の実測）→ 2.5 GB と見て、空きメモリでも抑える
    av=$(awk '/^MemAvailable:/{printf "%.0f", $2*1024}' /proc/meminfo)
    TUNE_P=$(awk -v a="$av" -v f="$MEM_FRAC" -v c="$NCPU" 'BEGIN{p=int(a*f/2.5e9); if (p<1) p=1; if (p>c) p=c; print p}')
    log "tune-synth: 同時実行 P=$TUNE_P（NCPU=$NCPU、空き $((av / 2**30)) GB ÷ 2.5 GB）"
  fi
  export TUNE_P
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
paper_refresh() {  # 11-20: 図の系列 = いまの config.sh の選択。結果のある組の methods.tsv を、回さずに作り直す（カタログに無い id があれば止まる）
  local g v r sub sel dir b
  for g in "PAPER_METHODS paper base" "PAPER_EXT_METHODS paper/ext -" "PAPER_FINAL_METHODS paper/final base" "PAPER_FINAL2_METHODS paper/final2 base" "PAPER_ROBUST_METHODS paper/robust -" "PAPER_SENS_METHODS paper/sens base"; do
    v=${g%% *}; r=${g#* }; sub=${r%% *}; b=${r##* }; sel=${!v}; dir=$WORK/$sub
    [ -d "$dir" ] && [ -n "$(ids_of "$sel")" ] || continue
    ( paper_pick "$sel" "$dir" "$b" ) 2>&1 | grep -v '方式（' >&2 || true
  done
}
paper_figs() {  # 引数なし = 全部（paper、あれば ext と robust）。--ext / --robust ならその部分だけ
  local robust_all=0
  paper_refresh
  if [ $# -eq 0 ]; then
    python3 "$KIT/py/paper_figs.py" "$WORK" || return
    ls "$WORK"/paper/ext/*.json >/dev/null 2>&1 && python3 "$KIT/py/paper_figs.py" "$WORK" --ext
    [ -s "$WORK/paper/final/methods.tsv" ] && python3 "$KIT/py/paper_figs.py" "$WORK" --final
    [ -s "$WORK/paper/final2/methods.tsv" ] && python3 "$KIT/py/paper_figs.py" "$WORK" --final2
    ls "$WORK"/paper/sens/*/*.json >/dev/null 2>&1 && python3 "$KIT/py/paper_sens_figs.py" "$WORK"
    ls "$WORK"/paper/robust/*.json >/dev/null 2>&1 && robust_all=1
  fi
  if [ $robust_all = 1 ] || [[ " $* " == *" --robust "* ]]; then   # 条件をまたいだ比較（robust/ 直下）と、条件ごとの一式（robust/<条件>/）
    python3 "$KIT/py/paper_robust_figs.py" "$WORK"
    local c; for c in $(ids_of "$PAPER_ROBUST_METHODS" | grep -oE '_(c0m|c0|f1|s[0-9]+)$' | tr -d _ | sort -u); do   # 選んだ方式の条件ごと
      python3 "$KIT/py/paper_figs.py" "$WORK" --cond "$c" || log "paper-figs --cond $c: その条件の結果が無いので飛ばした"; done
  elif [[ " $* " == *" --sens "* ]]; then python3 "$KIT/py/paper_sens_figs.py" "$WORK"
  elif [ $# -gt 0 ]; then python3 "$KIT/py/paper_figs.py" "$WORK" "$@"; fi
}

# ---------------------------------------------------------------- paper の方式（config.sh の PAPER_CATALOG と *_METHODS）
BASE_ID=r256_base   # 差の基準（paper では必ず回す）
cat_get() {  # cat_get <id> → "指定<TAB>図の名前"（カタログに無ければ失敗）
  local s rest
  for s in "${PAPER_CATALOG[@]}"; do
    [ "${s%%|*}" = "$1" ] || continue
    rest=${s#*|}
    if [[ "$rest" == *"|"* ]]; then printf '%s\t%s\n' "${rest%%|*}" "${rest#*|}"; else printf '%s\t\n' "$rest"; fi
    return 0
  done
  return 1
}
ids_of() {  # ids_of <選択>: all を展開し、重複を除いた id を並べた順に 1 行ずつ
  local id s
  for id in $1; do
    if [ "$id" = all ]; then
      for s in "${PAPER_CATALOG[@]}"; do s=${s%%|*}; [[ "$s" =~ _(c0|c0m|f1|s[0-9]+)$ ]] || echo "$s"; done
    else echo "$id"; fi
  done | awk '!seen[$0]++'
}
paper_pick() {  # paper_pick <選択> <出力先> <base|-> [methods.tsv の置き場]: PICKED（"id|指定"）を作り、methods.tsv（id・図の名前・指定）を残す
  local root=$2 out=${4:-$2/methods.tsv} ids id sl bad=""
  ids=$(ids_of "$1")
  if [ "$3" = base ] && ! grep -qx "$BASE_ID" <<< "$ids"; then
    log "基準 $BASE_ID を足した（差の基準なので必ず回す）"; ids=$(printf '%s\n%s' "$BASE_ID" "$ids")
  fi
  local tsv=""; PICKED=()
  for id in $ids; do
    sl=$(cat_get "$id") || { bad="$bad $id"; continue; }
    PICKED+=("$id|${sl%%$'\t'*}")
    tsv+=$(printf '%s\t%s\t%s' "$id" "${sl#*$'\t'}" "${sl%%$'\t'*}")$'\n'
  done
  [ -n "$bad" ] && die "PAPER_CATALOG に無い id:$bad（./run.sh methods で一覧）"   # 止まるときは前の methods.tsv を残す
  [ ${#PICKED[@]} -gt 0 ] || die "回す方式が 1 つも選ばれていない"
  mkdir -p "$root" "$(dirname "$out")"; printf '%s' "$tsv" > "$out"
  log "${root#$WORK/}: ${#PICKED[@]} 方式（${PICKED[*]%%|*}）"
}
paper_set() {  # paper_set <選択> <出力先> <base|-> <methods.tsv の置き場|""> [name..]: 選んだ方式を traces.tsv のトレースで回す（tune と同じ仕組み）
  local sel=$1 root=$2 b=$3 out=$4; shift 4
  [ -n "$(ids_of "$sel")" ] || [ "$b" = base ] || return 0   # 回す方式が無い組は飛ばす（paper-final の ext 側など）
  paper_pick "$sel" "$root" "$b" "$out"
  RUN_JOBS=PICKED RUN_ROOT=$root each_trace tune_one "$@"
}
tune_pick() {  # TUNE_JOBS から TUNE_METHODS（id の並び。all = 全部）を PICKED に。図の基準 none_h1 は必ず入れる
  local s id bad="" ids
  PICKED=()
  if [ "$(echo $TUNE_METHODS)" = all ]; then PICKED=("${TUNE_JOBS[@]}"); return; fi
  ids=$(printf '%s\n' none_h1 $TUNE_METHODS | awk '!seen[$0]++')
  for id in $ids; do
    for s in "${TUNE_JOBS[@]}"; do [ "${s%%|*}" = "$id" ] && { PICKED+=("$s"); continue 2; }; done
    bad="$bad $id"
  done
  [ -n "$bad" ] && die "TUNE_JOBS に無い id:$bad"
  log "tune: ${#PICKED[@]} 構成（${PICKED[*]%%|*}）"
}
paper_final() {  # 論文の本番用（PAPER_FINAL_METHODS）。結果は paper / paper-ext と共有し（済んだ run は回さない）、図と表は work/paper/final/
  # paper-final2: PF_METHODS = PAPER_FINAL2_METHODS、PF_DIR = final2（図と表は work/paper/final2/）
  local M=${PF_METHODS:-$PAPER_FINAL_METHODS} F=$WORK/paper/${PF_DIR:-final} all extset main ext id
  paper_pick "$M" "$F" base   # 先に全部を確かめ、<dir>/methods.tsv = 図と表の並び
  all=$(ids_of "$M"); extset=" $(ids_of "$PAPER_EXT_METHODS" | tr '\n' ' ') "
  main=$(for id in $all; do [[ $extset == *" $id "* ]] || echo "$id"; done)
  ext=$(for id in $all; do [[ $extset == *" $id "* ]] && echo "$id"; done)
  paper_set "$main" "$WORK/paper" base "$F/run_paper.tsv" "$@"        # paper の方式（work/paper/methods.tsv は変えない）
  paper_set "$ext" "$WORK/paper/ext" - "$F/run_ext.tsv" "$@"          # CacheLib の n=0・2・3 は paper-ext の場所へ
}
paper_sens() {  # 11-20: 感度。既定の点（work/paper と共有）と、SSD・DRAM・RU を一つずつ変えた点（work/paper/sens/<点>/）
  local S=$WORK/paper/sens x
  paper_pick "$PAPER_SENS_METHODS" "$S" base                                  # sens/methods.tsv = 図の並び
  paper_set "$PAPER_SENS_METHODS" "$WORK/paper" base "$S/run_paper.tsv" "$@"  # 既定の点
  for x in $SENS_SSD; do ( SSD_MULT=$x; sens_point "$S/ssd_x$x" 0 "$@" ) || return; done
  for x in $SENS_DRAM; do ( DRAM_DIV=$x; sens_point "$S/dram_$x" 0 "$@" ) || return; done
  for x in $SENS_RU; do ( REGION_BYTES=$x; sens_point "$S/ru_$((x / 1048576))M" 1 "$@" ) || return; done
}
sens_point() {  # sens_point <出力先> <1 = region-size を自分で持つ構成を外す（RU の点。既定の点と同じ run）> [name..]
  local o=$1 drop=$2 s k=(); shift 2
  paper_pick "$PAPER_SENS_METHODS" "$o" base
  [ "$drop" = 1 ] && { for s in "${PICKED[@]}"; do [[ $s == *region-size=* ]] || k+=("$s"); done; PICKED=("${k[@]}"); }
  [ ${#PICKED[@]} -gt 0 ] || return 0
  RUN_JOBS=PICKED RUN_ROOT=$o each_trace tune_one "$@"
}
methods() {  # カタログの全部と、どの組で選ばれているか（● = 選択）
  local P E R s id sl lb bad
  P=" $(ids_of "$PAPER_METHODS" | tr '\n' ' ') $BASE_ID "; E=" $(ids_of "$PAPER_EXT_METHODS" | tr '\n' ' ') "
  R=" $(ids_of "$PAPER_ROBUST_METHODS" | tr '\n' ' ') "; local Fi=" $(ids_of "$PAPER_FINAL_METHODS" | tr '\n' ' ') $BASE_ID "
  local F2=" $(ids_of "$PAPER_FINAL2_METHODS" | tr '\n' ' ') $BASE_ID " Se=" $(ids_of "$PAPER_SENS_METHODS" | tr '\n' ' ') $BASE_ID "
  printf '%-18s %-7s %-6s %-6s %-4s %-7s %-5s %s\n' id final2 final paper ext robust sens "RegionFlash の指定（| 図の名前）"
  for s in "${PAPER_CATALOG[@]}"; do
    id=${s%%|*}; sl=$(cat_get "$id"); lb=${sl#*$'\t'}
    printf '%-18s %-7s %-6s %-6s %-4s %-7s %-5s %s\n' "$id" "$([[ $F2 == *" $id "* ]] && echo yes)" "$([[ $Fi == *" $id "* ]] && echo yes)" \
      "$([[ $P == *" $id "* ]] && echo yes)" "$([[ $E == *" $id "* ]] && echo yes)" "$([[ $R == *" $id "* ]] && echo yes)" \
      "$([[ $Se == *" $id "* ]] && echo yes)" "${sl%%$'\t'*}${lb:+ | $lb}"
  done
  bad=$(for id in $(ids_of "$PAPER_METHODS $PAPER_EXT_METHODS $PAPER_ROBUST_METHODS $PAPER_FINAL_METHODS $PAPER_FINAL2_METHODS $PAPER_SENS_METHODS"); do cat_get "$id" >/dev/null || echo "$id"; done)
  [ -z "$bad" ] || echo "注意: カタログに無い id が選ばれている（選択から外すか、カタログに足す）:" $bad
}

aggregate() { TUNE_W=$TUNE_W python3 "$KIT/py/aggregate.py" "$KIT" "$WORK" "$@"; }
need_build() {  # キットのパッチが当てたものと違う（キットを更新した）なら setup をやり直す（古い cachesim は新しい構成を知らず FAIL する）
  [ -n "$LCS_SRC" ] || [ "$CACHESIM" != "$WORK/b/bin/cachesim" ] && return 0   # 手元の clone / 作成済みの cachesim を使うときは任せる
  [ "$(cat "$WORK/src/.rf_patch" 2>/dev/null)" = "$(sha256sum "$KIT/regionflash.patch" | cut -c1-16)" ] && [ -x "$CACHESIM" ] || setup
}

# ---------------------------------------------------------------- main
need_traces() { check >/dev/null || { check; die "traces.tsv を直す"; }; }
usage() { awk 'NR > 1 && /^set -u/ {exit} NR > 1 {sub(/^# ?/, ""); print}' "$0"; }
cmd=${1:-help}; [ $# -gt 0 ] && shift
case $cmd in
  # 準備
  setup)        setup ;;
  check)        check ;;
  methods)      methods ;;
  # 実験
  validity)     need_traces; need_build; each_trace validity_one "$@" ;;
  synth)        need_build; synth ;;
  tune)         need_traces; need_build; mkdir -p "$WORK/tune"; tune_pick; RUN_JOBS=PICKED each_trace tune_one "$@"; tune_figs ;;
  tune-synth)   need_build; synth_tools; mkdir -p "$WORK/tune"; tune_pick; RUN_JOBS=PICKED tune_synth; tune_figs ;;
  che)          need_traces; [ "$CHE_SIM" = 1 ] && need_build; [ -x "$B/og_che" ] || setup
                mkdir -p "$WORK/che"; each_trace che_one "$@"; che_report ;;
  paper)        need_traces; need_build; paper_set "$PAPER_METHODS" "$WORK/paper" base "" "$@"; paper_figs ;;
  paper-ext)    need_traces; need_build; paper_set "$PAPER_EXT_METHODS" "$WORK/paper/ext" - "" "$@"; paper_figs --ext ;;
  paper-final)  need_traces; need_build; paper_final "$@"; paper_figs --final ;;
  paper-final2) need_traces; need_build; PF_METHODS=$PAPER_FINAL2_METHODS PF_DIR=final2 paper_final "$@"; paper_figs --final2 ;;
  paper-sens)   need_traces; need_build; paper_sens "$@"; paper_figs --sens ;;
  paper-robust) need_traces; need_build; paper_set "$PAPER_ROBUST_METHODS" "$WORK/paper/robust" - "" "$@"
                python3 "$KIT/py/paper_check.py" "$WORK" --robust; paper_figs --robust ;;
  all)          setup && check && each_trace validity_one && synth && aggregate ;;
  # 図と表だけ
  aggregate)    aggregate "$@" ;;
  tune-figs)    tune_figs "$@" ;;
  che-report)   che_report "$@" ;;
  paper-figs)   paper_figs "$@" ;;
  help|-h|--help) usage ;;
  *)            usage; die "知らないコマンド: $cmd" ;;
esac
