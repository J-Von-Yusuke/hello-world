#!/usr/bin/env bash
# verify.sh - check on this machine that the patched CacheLib features work.
#
# Runs cachebench once per "case" (feature set) on the device you give, stops
# each run after STOP bytes written to the device, and checks the counters
# that prove the feature ran (SR reclaim, PIDs, RUH Update, Trim, Nemo, ...).
# Stats are written on three axes per case: bytes.log / ops.log / time.log.
#
#   ./verify.sh -d /dev/nvme0n1 --yes                 # real SSD (destroys data)
#   ./verify.sh -d /dev/shm/navy.dev -c 4GiB -s 8GB   # quick check in RAM
#   ./verify.sh -d /dev/nvme0n1 --fdp on -T trace.oracleGeneral.zst
#   ./verify.sh -d ... -W cachelib:ssd_perf/kvcache_l2_wc
#
# Run ./verify.sh -h for all options.
set -uo pipefail

# ---------------------------------------------------------------- defaults
CL="$HOME/cachelib-ru"         # CacheLib checkout (install.sh target)
DEV=""                         # device or file
NVM=15GiB; DRAM=4GiB           # NVM cache size / DRAM cache size
STOP=30GB; WARM=15GiB          # byte axis: stop / warmup (device bytes)
PBYTES=1GiB                    # byte axis: progress interval
POPS=10000000                  # op axis: progress every N ops
PSEC=60                        # time axis: progress every N seconds
POLL=500                       # sampling period of the byte/op axes (ms)
MAXSEC=0                       # time axis: stop after N s (0 = no limit)
FDP=sim                        # sim | on | off
SIMH=8; SIMRU=256MiB           # simulated FDP: handles / RU size
SR=256MiB                      # SR size when --fdp off
BHPCT=20                       # % of NVM for BigHash
THREADS=16
WL=synthetic                   # synthetic | cachelib:<config> | <file.json>
TRACE=""; REPEAT=false         # oracleGeneral trace (.zst)
CASES="base sr sfifo noruh trim log nemo"
OUT="./verify-$(date +%Y%m%d-%H%M%S)"
YES=0; DRY=0; FRESH=0

usage() { cat <<'EOF'
usage: verify.sh -d DEVICE [options]

device and sizes
  -d, --device PATH      NVMe block device (/dev/nvme0n1) or a file (/dev/shm/x.dev)
  -c, --nvm SIZE         NVM cache size              (default 15GiB)
  -m, --dram SIZE        DRAM cache size             (default 4GiB)
      --bighash-pct N    % of NVM for BigHash        (default 20)
      --yes              do not ask before writing a block device
      --fresh            blkdiscard the block device before every case

stop / warmup / progress (sizes: 30GB = 30e9, 15GiB = 15*2^30, also K/M/T)
  -s, --stop SIZE        stop after SIZE device bytes   (default 30GB)
  -w, --warmup SIZE      WARMUP block at SIZE           (default 15GiB)
      --progress-bytes S byte-axis interval             (default 1GiB)
      --progress-ops N   op-axis interval               (default 10000000)
      --progress-sec N   time-axis interval             (default 60)
      --max-seconds N    also stop after N seconds      (default 0 = off)

FDP
      --fdp sim|on|off   sim: simulate PIDs/RU on any device (default)
                         on : real FDP SSD (io_uring + deviceEnableFDP)
                         off: no FDP (one SR size from --sr, PIDs unused by the device)
      --sim-handles N    simulated placement handles  (default 8)
      --sim-ru SIZE      simulated RU size            (default 256MiB)
      --sr SIZE          SR size; replaced by the RU size when the device
                         (or --fdp sim) reports one   (default 256MiB)

workload (pick one)
  -W synthetic           built-in mix 100B..200KB (default)
  -W cachelib:NAME       a config shipped with CacheLib, e.g.
                         cachelib:ssd_perf/kvcache_l2_wc  (list: --list-workloads)
  -W FILE.json           any cachebench config (its test_config is used)
  -T, --trace FILE.zst   oracleGeneral trace (zstd), replayed once
      --repeat-trace     replay the trace again when it ends
  -t, --threads N        worker threads (default 16; cachelib/file configs keep theirs)

run control
      --cases "LIST"     subset of: base sr sfifo noruh trim log nemo
  -o, --out DIR          output directory (default ./verify-DATE)
  -L, --cachelib DIR     CacheLib checkout (default ~/cachelib-ru)
  -n, --dry-run          write configs and print commands only
      --list-workloads   list the configs shipped with CacheLib

cases
  base   original CacheLib (Region mode, in-place BigHash): reference
  sr     SuperRegion per PID, size-based PIDs, II reinsertion, LRU
  sfifo  sr + SFIFO [2,1,1] + II hit segments + floors [0.1,0.3,0.5,0]
  noruh  sr with RUH Update off   (device GC becomes visible on FDP SSDs)
  trim   sr with Trim on reclaim
  log    sr + BigHash log mode (RU-unit erase)
  nemo   sr + Nemo BigHash (Set-Groups, async flush)
EOF
}

die() { echo "ERROR: $*" >&2; exit 2; }
need() { [ $# -ge 2 ] || die "$1 needs a value"; }

# "30GB" -> 30000000000, "15GiB" -> 16106127360, "1000" -> 1000
tobytes() {
  echo "$1" | awk '{
    s=$1; m=1
    if (match(s, /[KMGT]iB$/)) { u=substr(s,RSTART,1); s=substr(s,1,RSTART-1); m=2^(10*index("KMGT",u)) }
    else if (match(s, /[KMGT]B?$/)) { u=substr(s,RSTART,1); s=substr(s,1,RSTART-1); m=10^(3*index("KMGT",u)) }
    if (s !~ /^[0-9.]+$/) { print "x"; exit }
    printf "%.0f\n", s*m }'
}
bytes_or_die() { local b; b=$(tobytes "$1"); [ "$b" != x ] || die "bad size: $1"; echo "$b"; }
hum() { awk -v b="$1" 'BEGIN{ split("B KiB MiB GiB TiB",u," "); i=1; while (b>=1024 && i<5) { b/=1024; i++ } printf "%.1f%s", b, u[i] }'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -d|--device) need "$@"; DEV="$2"; shift ;;
    -c|--nvm) need "$@"; NVM="$2"; shift ;;
    -m|--dram) need "$@"; DRAM="$2"; shift ;;
    -s|--stop) need "$@"; STOP="$2"; shift ;;
    -w|--warmup) need "$@"; WARM="$2"; shift ;;
    --progress-bytes) need "$@"; PBYTES="$2"; shift ;;
    --progress-ops) need "$@"; POPS="$2"; shift ;;
    --progress-sec) need "$@"; PSEC="$2"; shift ;;
    --max-seconds) need "$@"; MAXSEC="$2"; shift ;;
    --fdp) need "$@"; FDP="$2"; shift ;;
    --sim-handles) need "$@"; SIMH="$2"; shift ;;
    --sim-ru) need "$@"; SIMRU="$2"; shift ;;
    --sr) need "$@"; SR="$2"; shift ;;
    --bighash-pct) need "$@"; BHPCT="$2"; shift ;;
    -t|--threads) need "$@"; THREADS="$2"; shift ;;
    -W|--workload) need "$@"; WL="$2"; shift ;;
    -T|--trace) need "$@"; TRACE="$2"; shift ;;
    --repeat-trace) REPEAT=true ;;
    --cases) need "$@"; CASES="$2"; shift ;;
    -o|--out) need "$@"; OUT="$2"; shift ;;
    -L|--cachelib) need "$@"; CL="$2"; shift ;;
    --yes) YES=1 ;;
    --fresh) FRESH=1 ;;
    -n|--dry-run) DRY=1 ;;
    --list-workloads)
      find "$CL/cachelib/cachebench/test_configs" -name '*.json' -path '*/config.json' 2>/dev/null |
        sed "s|$CL/cachelib/cachebench/test_configs/||; s|/config.json$||" | sort | sed 's/^/cachelib:/'
      exit 0 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see -h)" ;;
  esac
  shift
done

# ---------------------------------------------------------------- checks
[ -n "$DEV" ] || { usage; die "-d DEVICE is required"; }
command -v python3 >/dev/null || die "python3 is required"
CB="$CL/build-cachelib/cachebench/cachebench"
[ -x "$CB" ] || [ "$DRY" = 1 ] || die "cachebench not found: $CB (use -L DIR)"
export LD_LIBRARY_PATH="$CL/opt/cachelib/lib:$CL/opt/cachelib/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

NVM_B=$(bytes_or_die "$NVM"); DRAM_B=$(bytes_or_die "$DRAM")
STOP_B=$(bytes_or_die "$STOP"); WARM_B=$(bytes_or_die "$WARM")
PB_B=$(bytes_or_die "$PBYTES"); SIMRU_B=$(bytes_or_die "$SIMRU"); SR_B=$(bytes_or_die "$SR")
[ "$WARM_B" -lt "$STOP_B" ] || die "warmup ($WARM) must be below stop ($STOP)"
case "$FDP" in sim|on|off) ;; *) die "--fdp must be sim, on or off" ;; esac
for c in $CASES; do
  case "$c" in base|sr|sfifo|noruh|trim|log|nemo) ;; *) die "unknown case: $c" ;; esac
done

# workload source
WL_KIND=synthetic; WL_FILE=""
if [ -n "$TRACE" ]; then
  [ -f "$TRACE" ] || die "trace not found: $TRACE"
  WL_KIND=oracle; WL_FILE=$(readlink -f "$TRACE")
elif [ "$WL" != synthetic ]; then
  if [ "${WL#cachelib:}" != "$WL" ]; then
    WL_FILE="$CL/cachelib/cachebench/test_configs/${WL#cachelib:}"
    [ -d "$WL_FILE" ] && WL_FILE="$WL_FILE/config.json"
  else
    WL_FILE="$WL"
  fi
  [ -f "$WL_FILE" ] || die "workload config not found: $WL_FILE (try --list-workloads)"
  WL_KIND=config; WL_FILE=$(readlink -f "$WL_FILE")
fi

# device
IS_BLK=0
if [ -b "$DEV" ]; then
  IS_BLK=1
  if lsblk -nro MOUNTPOINT "$DEV" 2>/dev/null | grep -q .; then
    die "$DEV (or one of its partitions) is mounted"
  fi
  DSIZE=$(blockdev --getsize64 "$DEV" 2>/dev/null || echo 0)
  [ "$DSIZE" -ge "$NVM_B" ] || die "$DEV is $(hum "$DSIZE"), smaller than --nvm $(hum "$NVM_B")"
  [ -w "$DEV" ] || [ "$DRY" = 1 ] || die "no write permission on $DEV (run with sudo)"
else
  [ "$FDP" != on ] || die "--fdp on needs an NVMe block device"
  [ "$FRESH" = 0 ] || die "--fresh needs a block device"
  DDIR=$(dirname "$DEV"); [ -d "$DDIR" ] || die "directory not found: $DDIR"
  AVAIL=$(df -B1 --output=avail "$DDIR" | tail -1)
  [ "$AVAIL" -ge "$NVM_B" ] || die "$DDIR has $(hum "$AVAIL") free, needs $(hum "$NVM_B")"
fi

NCASES=$(echo $CASES | wc -w)
cat <<EOF
== plan
 cachebench : $CB
 device     : $DEV $( [ $IS_BLK = 1 ] && echo "(block device, $(hum "$DSIZE"))" || echo "(file)")
 NVM / DRAM : $(hum "$NVM_B") / $(hum "$DRAM_B"), BigHash ${BHPCT}%
 byte axis  : stop $(hum "$STOP_B")  warmup $(hum "$WARM_B")  every $(hum "$PB_B")  (device bytes)
 op / time  : every $POPS ops / every ${PSEC}s$( [ "$MAXSEC" != 0 ] && echo ", stop at ${MAXSEC}s")
 FDP        : $FDP$( [ "$FDP" = sim ] && echo " ($SIMH handles, RU $(hum "$SIMRU_B"))"), SR $(hum "$SR_B") unless the device gives an RU size
 workload   : $WL_KIND ${WL_FILE}
 cases      : $CASES
 total write: about $(hum $((STOP_B * NCASES))) to $DEV
 output     : $OUT
EOF

if [ "$IS_BLK" = 1 ] && [ "$YES" = 0 ] && [ "$DRY" = 0 ]; then
  echo
  echo "WARNING: all data on $DEV will be overwritten."
  read -r -p "Type the device path to continue: " ans
  [ "$ans" = "$DEV" ] || die "aborted"
fi

mkdir -p "$OUT" || die "cannot create $OUT"
OUT=$(readlink -f "$OUT")

# ---------------------------------------------------------------- config
# writes $OUT/$case/config.json; prints the directory cachebench must run in
gen_config() {
  CASE="$1" DEV="$DEV" NVM_B="$NVM_B" DRAM_B="$DRAM_B" FDP="$FDP" SIMH="$SIMH" \
  SIMRU_B="$SIMRU_B" SR_B="$SR_B" BHPCT="$BHPCT" THREADS="$THREADS" IS_BLK="$IS_BLK" \
  WL_KIND="$WL_KIND" WL_FILE="$WL_FILE" REPEAT="$REPEAT" OUTF="$2" python3 - <<'PY'
import json, os
e = os.environ
case, kind = e["CASE"], e["WL_KIND"]
MB = 1 << 20
nvm, dram = int(e["NVM_B"]), int(e["DRAM_B"])
huge = 10**12  # runs are stopped by the byte/time axes, not by numOps

cache, run_dir = {}, os.path.dirname(e["OUTF"])
if kind == "synthetic":
    sizes = [100, 800, 4000, 30000, 200000]
    probs = [0.3, 0.35, 0.3, 0.05]
    avg = sum(p * (a + b) / 2 for p, a, b in zip(probs, sizes, sizes[1:]))
    test = {"numOps": huge, "numThreads": int(e["THREADS"]),
            "numKeys": max(100000, int(3 * nvm / avg)),  # working set ~3x NVM
            "keySizeRange": [16, 32], "keySizeRangeProbability": [1.0],
            "valSizeRange": sizes, "valSizeRangeProbability": probs,
            "getRatio": 0.8, "setRatio": 0.2, "enableLookaside": True}
    cache["poolRebalanceIntervalSec"] = 0
elif kind == "oracle":
    test = {"generator": "oracle-general-replay", "traceFileName": e["WL_FILE"],
            "cachePieceSize": 65536, "enableLookaside": True,
            "numThreads": int(e["THREADS"]), "numOps": huge,
            "repeatTraceReplay": e["REPEAT"] == "true", "timestampFactor": 1,
            "replayGeneratorConfig": {"oracleTrackUnique": False}}
else:  # a cachebench config: keep its workload and pool layout
    src = json.load(open(e["WL_FILE"]))
    test = src["test_config"]
    test["numOps"] = huge
    test.setdefault("configPath", os.path.dirname(e["WL_FILE"]))
    run_dir = os.path.dirname(e["WL_FILE"])
    cache = src.get("cache_config", {})
    for k in list(cache):  # drop NVM settings of the source config
        if k.startswith("navy") or k.startswith("nvm") or k in ("deviceEnableFDP",):
            del cache[k]

cache.update({
    "cacheSizeMB": dram // MB, "nvmCacheSizeMB": nvm // MB,
    "nvmCachePaths": [e["DEV"]], "truncateNvmFile": e["IS_BLK"] != "1",
    "navyBigHashSizePct": int(e["BHPCT"]), "navySmallItemMaxSize": 2048,
    "navyHitsReinsertionThreshold": 1, "navyRegionSizeMB": 16,
    "printNvmCounters": True})

fdp = e["FDP"]
if fdp == "on":
    cache.update({"navyEnableIoUring": True, "deviceEnableFDP": True})
if case != "base":
    if fdp == "sim":
        cache.update({"navySimFdpPlacementHandles": int(e["SIMH"]),
                      "navySimFdpRuSizeMB": int(e["SIMRU_B"]) // MB})
    # SR > 0 switches SR mode on; a device (or simulated) RU size replaces it
    cache.update({"navySuperRegionSizeMB": int(e["SR_B"]) // MB,
                  "navyCleanSuperRegions": 2,
                  "navyPlacementSizeThresholds": [8192, 65536],
                  "navyReinsertPlacement": "ii"})
extra = {
    "sfifo": {"navySegmentedFifoSegmentRatio": [2, 1, 1],
              "navySfifoReinsertHitThresholds": [2],
              "navyPlacementCapacityFloors": [0.1, 0.3, 0.5, 0]},
    "noruh": {"navyRuhUpdate": False},
    "trim": {"navyTrimOnReclaim": True},
    "log": {"navyBigHashMode": "log"},
    "nemo": {"navyBigHashMode": "nemo"},
}.get(case, {})
cache.update(extra)
json.dump({"cache_config": cache, "test_config": test}, open(e["OUTF"], "w"), indent=2)
print(run_dir)
PY
}

# ---------------------------------------------------------------- checks
FINAL=""   # FINAL block of the byte axis
cnt() {    # cnt NAME -> numeric value in the FINAL block (empty if absent)
  printf '%s\n' "$FINAL" | awk -v k="$1" -F' +: +' '$1==k { gsub(/,/,"",$2); print $2+0; exit }'
}
P=0; F=0; W=0
pass() { P=$((P+1)); echo "   PASS  $*"; }
fail() { F=$((F+1)); echo "   FAIL  $*"; }
warn() { W=$((W+1)); echo "   WARN  $*"; }
expect() { # expect NAME OP VALUE [warn]
  local v; v=$(cnt "$1")
  if [ -z "$v" ]; then
    [ "${4:-}" = warn ] && warn "$1 missing" || fail "$1 missing"; return
  fi
  if awk -v a="$v" -v b="$3" -v op="$2" 'BEGIN{exit !((op=="gt"&&a>b)||(op=="eq"&&a==b)||(op=="ge"&&a>=b))}'; then
    pass "$1 = $v ($2 $3)"
  else
    [ "${4:-}" = warn ] && warn "$1 = $v (expected $2 $3)" || fail "$1 = $v (expected $2 $3)"
  fi
}

fdp_stats() { # best effort: FDP host/media bytes written (needs nvme-cli)
  [ "$IS_BLK" = 1 ] && command -v nvme >/dev/null || return 0
  nvme fdp stats "$DEV" -e 1 -o json > "$1" 2>/dev/null || rm -f "$1"
}
waf() { # waf BEFORE AFTER -> "WAF x.xx" from FDP statistics log
  [ -f "$1" ] && [ -f "$2" ] || return 0
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
def g(f):
    d = json.load(open(f)); d = {k.lower(): v for k, v in d.items()}
    return int(d.get("hbmw", 0)), int(d.get("mbmw", 0))
(h0, m0), (h1, m1) = g(sys.argv[1]), g(sys.argv[2])
if h1 > h0: print("WAF %.3f (host %d B, media %d B)" % ((m1 - m0) / (h1 - h0), h1 - h0, m1 - m0))
PY
}

# ---------------------------------------------------------------- run
SUMMARY="$OUT/summary.tsv"
printf 'case\tresult\tpass\tfail\twarn\telapsed_s\tops_M\tdevice_bytes\thit_pct_warm\tnvm_hit_pct_warm\tbyte_miss_pct\tfdp_waf\n' > "$SUMMARY"

for c in $CASES; do
  d="$OUT/$c"; mkdir -p "$d"
  echo; echo "== case $c"
  run_dir=$(gen_config "$c" "$d/config.json") || die "config generation failed"
  args=(--json_test_config "$d/config.json"
        --progress "$PSEC" --progress_stats_file "$d/time.log"
        --progress_bytes "$PB_B" --warmup_bytes "$WARM_B" --stop_bytes "$STOP_B"
        --byte_axis_device --progress_bytes_stats_file "$d/bytes.log"
        --progress_ops "$POPS" --progress_ops_stats_file "$d/ops.log"
        --progress_poll_ms "$POLL")
  [ "$MAXSEC" != 0 ] && args+=(--stop_seconds "$MAXSEC")
  echo "(cd $run_dir && $CB ${args[*]})" > "$d/cmd.sh"
  if [ "$DRY" = 1 ]; then cat "$d/cmd.sh"; continue; fi

  if [ "$FRESH" = 1 ]; then blkdiscard -f "$DEV" || die "blkdiscard failed"; fi
  fdp_stats "$d/fdp_before.json"
  t0=$(date +%s)
  (cd "$run_dir" && "$CB" "${args[@]}") > "$d/out.log" 2>&1
  rc=$?
  t1=$(date +%s)
  fdp_stats "$d/fdp_after.json"
  [ "$IS_BLK" = 1 ] || rm -f "$DEV"   # give the RAM / disk space back

  P=0; F=0; W=0
  FINAL=$(awk '/\[byte:FINAL\]/{f=1} f' "$d/bytes.log" 2>/dev/null)
  hdr=$(printf '%s\n' "$FINAL" | head -1)

  # --- every case: the run itself and the three stats axes
  [ "$rc" = 0 ] && pass "cachebench exit 0" || fail "cachebench exit $rc (see $d/out.log)"
  n=$(grep -ac 'checksum mismatch' "$d/out.log"); [ "$n" = 0 ] && pass "no checksum mismatch" || fail "$n checksum mismatches"
  [ -n "$FINAL" ] && pass "byte axis FINAL block" || fail "no FINAL block in bytes.log"
  grep -aq '\[byte:WARMUP\]' "$d/bytes.log" 2>/dev/null && pass "byte axis WARMUP at $(hum "$WARM_B")" || warn "byte axis WARMUP not reached"
  if grep -aq '\[byte:STOP\]' "$d/bytes.log" 2>/dev/null; then pass "byte axis STOP at $(hum "$STOP_B")"
  elif [ "$WL_KIND" = oracle ]; then warn "trace ended before STOP (use --repeat-trace)"
  else fail "byte axis STOP not reached"; fi
  grep -aq '\[op:' "$d/ops.log" 2>/dev/null && pass "op axis written (ops.log)" || warn "op axis has no block (fewer than $POPS ops?)"
  grep -aq '\[time:' "$d/time.log" 2>/dev/null && pass "time axis written (time.log)" || warn "time axis has no block (run shorter than ${PSEC}s?)"
  grep -aq 'GET bytes' "$d/out.log" && pass "byte-miss stats printed" || fail "no 'GET bytes' line"
  expect navy_device_write_errors eq 0
  expect navy_device_read_errors eq 0
  [ "$c" = nemo ] || expect navy_bh_checksum_errors eq 0 warn
  [ "$FDP" = on ] && { grep -a 'FDP' "$d/out.log" | grep -aim1 'handle' | sed 's/^.*\] /   info  /'; }

  # --- per feature
  bh_used=$(cnt navy_bh_inserts); bh_used=${bh_used:-0}
  if [ "$c" = base ]; then
    grep -aq 'SuperRegion mode' "$d/out.log" && fail "base ran in SuperRegion mode" || pass "Region mode (original)"
  else
    grep -aq 'SuperRegion mode' "$d/out.log" && pass "$(grep -ao 'SuperRegion mode:[^"]*' "$d/out.log" | head -1)" || fail "SuperRegion mode not enabled"
    expect navy_bc_sr_reclaims gt 0
    np=0; for p in 0 1 2 3 4 5 6 7; do v=$(cnt "navy_bc_placement_${p}_srs"); [ "${v:-0}" != 0 ] && np=$((np+1)); done
    [ "$np" -ge 2 ] && pass "$np PIDs own SRs" || fail "only $np PID owns SRs"
    expect navy_bc_reinsertions gt 0 warn
    if [ "$c" = noruh ]; then
      expect navy_device_ruh_updates eq 0
      expect navy_device_ruh_update_skipped gt 0
    else
      expect navy_device_ruh_updates gt 0
      expect navy_device_ruh_update_errors eq 0
    fi
  fi
  case "$c" in
    sfifo) grep -aq 'segmented FIFO selection' "$d/out.log" && pass "SFIFO + floors selector active" || fail "SFIFO + floors selector not active"
           expect navy_bc_floor_protects gt 0 warn ;;
    trim)  expect navy_device_trims gt 0; expect navy_device_trim_errors eq 0 ;;
    log)   if [ "$bh_used" = 0 ]; then warn "BigHash got no inserts (workload has no small objects)"
           else expect navy_bh_log_ru_evictions gt 0; fi ;;
    nemo)  if [ "$bh_used" = 0 ]; then warn "BigHash got no inserts (workload has no small objects)"
           else expect navy_bh_nemo_flushes gt 0; expect navy_bh_nemo_sg_evictions gt 0 warn; fi ;;
  esac

  w=$(waf "$d/fdp_before.json" "$d/fdp_after.json"); [ -n "$w" ] && echo "   info  FDP stats: $w"
  res=PASS; [ "$F" -gt 0 ] && res=FAIL
  echo "   -> $res ($P pass, $F fail, $W warn)  $((t1-t0))s"
  gv() { printf '%s\n' "$hdr" | grep -o "$1" | head -1 | grep -o '[0-9.]*' | head -1; }
  # hit ratios since WARMUP (the header line holds only the last interval)
  warm=$(printf '%s\n' "$FINAL" | awk '/Since Warmup/{f=1} /^== Ops Since/{f=0} f')
  hv() { printf '%s\n' "$warm" | grep -a -m1 "^$1 *:" | grep -o '[0-9.]*%' | tr -d %; }
  bm=$(grep -a 'GET bytes' "$d/out.log" | tail -1 | grep -o 'byteMiss: *[0-9.]*' | grep -o '[0-9.]*$')
  printf '%s\t%s\t%d\t%d\t%d\t%d\t%s\t%s\t%s\t%s\t%s\t%s\n' "$c" "$res" "$P" "$F" "$W" $((t1-t0)) \
    "$(gv 'ops=[0-9.]*M')" "$(gv 'nvmDeviceB=[0-9]*')" "$(hv 'Hit Ratio')" \
    "$(hv 'NVM Hit Ratio')" "${bm:-}" "$(echo "$w" | awk '{print $2}')" >> "$SUMMARY"
done

[ "$DRY" = 1 ] && { echo; echo "dry run: configs in $OUT"; exit 0; }
echo; echo "== summary ($SUMMARY)"
column -t -s $'\t' "$SUMMARY" 2>/dev/null || cat "$SUMMARY"
! grep -q $'\tFAIL\t' "$SUMMARY"
