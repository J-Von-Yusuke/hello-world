# config.sh — 実験キットの設定（bash。ここを書き換える）。環境変数で上書きもできる: P=2 ./run.sh validity

# ---- 場所 -------------------------------------------------------------
WORK=${WORK:-$KIT/work}                    # 生成物の置き場（traces/ out/ results/ synth/ bin/ src/ b/）。容量に注意
LCS_REPO=${LCS_REPO:-https://github.com/1a1a11a/libCacheSim.git}
LCS_BASE=${LCS_BASE:-93529a1}              # パッチの土台（変えない）
LCS_SRC=${LCS_SRC:-}                       # 空 = LCS_REPO を WORK/src に clone し、LCS_BASE に checkout してパッチを当てる。
                                           # 既存の clone のパスを書くと、未パッチならそこへ当てる（ブランチは動かさない。
                                           # HEAD が LCS_BASE でないと当たらないことがある）。パッチ済みならそのまま使う
CACHESIM=${CACHESIM:-$WORK/b/bin/cachesim} # 作成済みの cachesim を使うなら、そのパス（setup のビルドは飛ばせる）
TRACECONV=${TRACECONV:-$WORK/b/bin/traceConv}

# ---- 資源 -------------------------------------------------------------
P=${P:-auto}                               # 同時に走らせる cachesim の数。auto = 空きメモリと 1 本の見積りから変種ごとに決める（数字なら固定）
# auto の見積り（実測で決めた。README の「メモリと同時実行数」）。u = 物の数、x = min(1, (SSD+DRAM)/フットプリント)
#   1 本 ≈ max(MEM_BASE + MEM_SEEN·u + MEM_RES·u·x,  MEM_BASE + MEM_RES_OBJ·u·x) × MEM_SAFETY   （Region 構成, 物単位 SSD 構成）
#   P = floor(MemAvailable × MEM_FRAC ÷ 1 本) を 1〜NCPU（既定 = 総コア数 − 1）に収める
MEM_BASE=${MEM_BASE:-400000000}            # 固定分 [B]（nha の none: 346 MB）
MEM_SEEN=${MEM_SEEN:-80}                   # 見た物 1 個あたり [B]（分類 hint の表。nha 65〜85, w01 68）
MEM_RES=${MEM_RES:-160}                    # Region 構成で SSD+DRAM に入っている物 1 個あたり [B]（w01 の none: 150）
MEM_RES_OBJ=${MEM_RES_OBJ:-320}            # 物単位 SSD 構成（obj_d*）で同じく [B]（w01 の obj: 310）
MEM_SAFETY=${MEM_SAFETY:-1.25}             # 見積りの余裕（ハッシュ表の倍々拡張の山など）
MEM_FRAC=${MEM_FRAC:-0.8}                  # 空きメモリ（/proc/meminfo の MemAvailable）のうち使ってよい割合
NCPU=${NCPU:-$(n=$(nproc); echo $(( n > 1 ? n - 1 : 1 )))}   # 並列に使うプロセス数の上限（既定 = CPU の総コア数 − 1。1 つは OS と操作用に残す）
SYNTH_P=${SYNTH_P:-$NCPU}                  # 合成負荷の並列数（1 本あたりは軽い）

# ---- トレース実験（validity）の既定構成 ----------------------------------
SSD_DEFAULT=${SSD_DEFAULT:-30%}            # SSD 容量: 240G（固定）/ 30%（フットプリントに対する割合）。traces.tsv の ssd 列で個別指定
DRAM_DIV=${DRAM_DIV:-30}                   # DRAM = SSD / DRAM_DIV        （meta: 240G の SSD に 8G）
REGION_N=${REGION_N:-392}                  # Region の数。R = SSD / REGION_N（MiB に切り捨て。meta: 626.5 MiB）
CHUNK=${CHUNK:-64K}                        # piece-wise 書き込みの幅（R より大きければ R に縮める）
PIECE=${PIECE:-131072}                     # K(N)（piece 数）の数え方。CacheLib は 128 KiB
INTERVAL=${INTERVAL:-10000}                # 先頭 N 要求の曲線の刻み（要求数）。小さいトレース（< 1000 万）は 1000 に
NREQ=${NREQ:-}                             # 空 = 全要求。数字 = 各 RegionFlash 実行を先頭 NREQ 要求で打ち切る（試運転用）
BASE_E="dram-algo=lru,inclusion=cachelib,region-evict=lru,seal-on-repartition=inactive,seed=1"
# 分類方法（id:hint 指定）。足すと、その分類の効果（none との BMR 差）も集計される
HINTS=("none:hint=none" "kf4:hint=key:key=freq/k=4" "sf8:hint=key:key=sizefreq/k=8/kf=4" "st:hint=split_threshold")
REINSERT="reinsert=hits:n=1/max_frac=0.5"  # 再挿入あり構成（+h1）の指定
WITH_REINSERT=${WITH_REINSERT:-1}          # 0 にすると再挿入あり構成を走らせない
BUDGET_DWPD=${BUDGET_DWPD:-"3 1"}          # 書き込み予算つきの構成（DWPD）。空にすると予算つきを走らせない
SAT_VALUE=${SAT_VALUE:-4294967295}         # サイズ -1（uint32 の 2^32-1）。GET でない操作の印と見る値
SAT_VARIANTS=${SAT_VARIANTS:-"drop delete"} # SAT_VALUE の要求があるトレースの扱い（先頭が主結果）:
                                           #   drop = 無かったことにする / delete = その物を消す操作（要求に数えない）/
                                           #   asis = そのまま / fix = その物の他のサイズに置換（og_xform。感度用）
SAT=${SAT:-auto}                           # auto = SAT_VALUE があれば上の SAT_VARIANTS、無ければ asis。変種を直接並べてもよい（例 "drop delete asis"）
REGION_BYTES=${REGION_BYTES-656932864}    # Region の大きさ [B]。既定 = 手元の FDP SSD の RU（626.5 MiB。Region = RU で DLWA = 1）。空 = SSD ÷ REGION_N
WRITE_MARKS_TB=${WRITE_MARKS_TB:-"1.5"}    # 「先頭 W TB 書き込んだ時点」で効果を測る W（TB = 10^12 B。全受け入れ・分類なしの SSD 書き込み量で数える）
STATS=${STATS:-1}                          # 1 = og_trace_stats（サイズ・頻度の分布統計）も取る

# ---- 自動推定の実験（tune）: 分類閾値 × 容量の配分 × 再挿入の閾値 ---------------------
# 1 行 = "id|RegionFlash の指定"。DRAM・Region・chunk は validity と同じ（tune_synth は縮小 SSD の固定値）。
# 分類は多くても 4 つ（閾値 3 個）まで: 分類ごとに開いた Region が 1 つ要り、書き込み先が散るほど
# 各 Region（FDP なら RU）の書き込み速度が落ちて開いたままの時間が延び、空きも増えるため。
ST2="hint=split_threshold"; ST3="hint=st_hr_refine:k_max=3"; ST4="hint=st_recursive:depth=2"
RE1="reinsert=hits:n=1"; RE3="reinsert=hits:n=3"; REA="reinsert=hits:n=auto/target=0.5"
TUNE_JOBS=(
  # 基準（既存の構成）と理想 SSD
  "none|hint=none,region-evict=lru"            "none_h1|hint=none,region-evict=lru,$RE1"
  "none_auto|hint=none,region-evict=lru,$REA"
  "obj|ssd-mode=object,ssd-algo=lru,hint=none"
  # A 容量の配分（分類 2、再挿入なし）: 固定の下限 ff、自動の hc（ff を測った目的 BHR + w·OHR で山登り。本命）、
  #   自動の mv（末尾のヒット密度で配分。1 Region に物が多い分類で限界値を誤るので比較用）
  "st_lru|$ST2,region-evict=lru" "st_lru_auto|$ST2,region-evict=lru,$REA"
  "st_ff50|$ST2,region-evict=lru,region-params=fc=1/ff=0.5" "st_ff70|$ST2,region-evict=lru,region-params=fc=1/ff=0.7"
  "st_ff80|$ST2,region-evict=lru,region-params=fc=1/ff=0.8" "st_ff90|$ST2,region-evict=lru,region-params=fc=1/ff=0.9"
  "st_ff95|$ST2,region-evict=lru,region-params=fc=1/ff=0.95"
  "st_hc0.03|$ST2,region-evict=lru,region-params=auto=1/w=0.03" "st_hc0.1|$ST2,region-evict=lru,region-params=auto=1/w=0.1"
  "st_hc0.3|$ST2,region-evict=lru,region-params=auto=1/w=0.3"   "st_hc1|$ST2,region-evict=lru,region-params=auto=1/w=1"
  "st_hc0.1_auto|$ST2,region-evict=lru,region-params=auto=1/w=0.1,$REA"
  "st_mv0.03|$ST2,region-evict=mv,region-params=w=0.03" "st_mv0.1|$ST2,region-evict=mv,region-params=w=0.1"
  "st_mv0.3|$ST2,region-evict=mv,region-params=w=0.3"   "st_mv1|$ST2,region-evict=mv,region-params=w=1"
  # B 再挿入の閾値（固定 n=1, 3 と自動）
  "st_ff90_h1|$ST2,region-evict=lru,region-params=fc=1/ff=0.9,$RE1" "st_ff90_h3|$ST2,region-evict=lru,region-params=fc=1/ff=0.9,$RE3"
  "st_ff90_auto|$ST2,region-evict=lru,region-params=fc=1/ff=0.9,$REA"
  "st_mv0.3_h1|$ST2,region-evict=mv,region-params=w=0.3,$RE1" "st_mv0.3_auto|$ST2,region-evict=mv,region-params=w=0.3,$REA"
  # D 分割閾値を縮小シミュレーションで選ぶ（hint=mini）と、それに下限の山登り・n=auto を重ねた全部自動
  "mini_lru|hint=mini,region-evict=lru" "mini_hc0.1_auto|hint=mini,region-evict=lru,region-params=auto=1/w=0.1,$REA"
  # C 分類の数（2 / 3 / 4）
  "k3_lru_auto|$ST3,region-evict=lru,$REA" "k4_lru_auto|$ST4,region-evict=lru,$REA"
  "k3_mv0.3_auto|$ST3,region-evict=mv,region-params=w=0.3,$REA" "k4_mv0.3_auto|$ST4,region-evict=mv,region-params=w=0.3,$REA"
)
TUNE_W=${TUNE_W:-0.3}                      # 図で「良い構成」を選ぶ目的 BHR + w·OHR の w（mv の w と同じ意味）
TUNE_SYNTH=${TUNE_SYNTH:-"r95to5:1K:4194304:0.95:0.9,512K:25600:0.05:0.9 r3to1:1K:4194304:0.75:0.9,512K:25600:0.25:0.9"}
TUNE_SYNTH_N=${TUNE_SYNTH_N:-60000000}     # 合成トレースの長さ（1/64 縮小 SSD: 3.75 GiB、R = 4 MiB、chunk 16 KiB、DRAM なし）

# ---- 合成負荷（synth）------------------------------------------------------
SYNTH_N=${SYNTH_N:-20000000}               # 合成トレースの要求数
SYNTH_SCALED=${SYNTH_SCALED:-0}            # 1 = 規模を縮めた SSD 想定の負荷（scaled.py / writes.py: 2 億要求 × 多数）も走らせる。数時間かかる
SYNTH_SCALED_N=${SYNTH_SCALED_N:-200000000}

# ---- 論文用の比較（paper）: Region を CacheLib の上限 256 MiB と RU（REGION_BYTES）で、方式 3 通り ----------
# 1 行 = "id|指定"。region-size は各行で上書きする（同じキーは後の値が使われる）。SSD・DRAM・chunk は validity と同じ。
PAPER_R256=${PAPER_R256:-268435456}        # CacheLib の Region の上限（256 MiB）
PAPER_JOBS=(
  "r256_base|region-size=$PAPER_R256,hint=none,region-evict=lru,reinsert=hits:n=1"
  "r256_ff90_h3|region-size=$PAPER_R256,hint=split_threshold,region-evict=lru,region-params=fc=1/ff=0.9,reinsert=hits:n=3"
  "r256_auto|region-size=$PAPER_R256,hint=split_threshold,region-evict=lru,region-params=auto=1/w=0.1,reinsert=hits:n=auto/target=0.5"
  "ru_base|hint=none,region-evict=lru,reinsert=hits:n=1"
  "ru_ff90_h3|hint=split_threshold,region-evict=lru,region-params=fc=1/ff=0.9,reinsert=hits:n=3"
  "ru_auto|hint=split_threshold,region-evict=lru,region-params=auto=1/w=0.1,reinsert=hits:n=auto/target=0.5"
  # 切り分け: 分類なしで再挿入の閾値の自動化（n=auto）だけ。*_auto との差が「分類＋下限」の効果
  "ru_none_auto|hint=none,region-evict=lru,reinsert=hits:n=auto/target=0.5"
  "r256_none_auto|region-size=$PAPER_R256,hint=none,region-evict=lru,reinsert=hits:n=auto/target=0.5"
)
# 追加の比較（./run.sh paper-ext → work/paper/ext/）: CacheLib（R=256MiB、分類なし）の再挿入なし（n=0）と n=3。
# 「書き直しの閾値だけ上げた CacheLib」と比べて、分類の効果が残るかを見る。既にある結果は回さない
PAPER_EXT_JOBS=(
  "r256_n0|region-size=$PAPER_R256,hint=none,region-evict=lru"
  "r256_n3|region-size=$PAPER_R256,hint=none,region-evict=lru,reinsert=hits:n=3"
)
