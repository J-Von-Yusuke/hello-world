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
#   P = floor(MemAvailable × MEM_FRAC ÷ 1 本) を 1〜nproc に収める
MEM_BASE=${MEM_BASE:-400000000}            # 固定分 [B]（nha の none: 346 MB）
MEM_SEEN=${MEM_SEEN:-80}                   # 見た物 1 個あたり [B]（分類 hint の表。nha 65〜85, w01 68）
MEM_RES=${MEM_RES:-160}                    # Region 構成で SSD+DRAM に入っている物 1 個あたり [B]（w01 の none: 150）
MEM_RES_OBJ=${MEM_RES_OBJ:-320}            # 物単位 SSD 構成（obj_d*）で同じく [B]（w01 の obj: 310）
MEM_SAFETY=${MEM_SAFETY:-1.25}             # 見積りの余裕（ハッシュ表の倍々拡張の山など）
MEM_FRAC=${MEM_FRAC:-0.8}                  # 空きメモリ（/proc/meminfo の MemAvailable）のうち使ってよい割合
SYNTH_P=${SYNTH_P:-8}                      # 合成負荷の並列数（1 本あたりは軽い）

# ---- トレース実験（validity）の既定構成 ----------------------------------
SSD_DEFAULT=${SSD_DEFAULT:-30%}            # SSD 容量: 240G（固定）/ 30%（フットプリントに対する割合）。traces.tsv の ssd 列で個別指定
DRAM_DIV=${DRAM_DIV:-30}                   # DRAM = SSD / DRAM_DIV        （meta: 240G の SSD に 8G）
REGION_N=${REGION_N:-392}                  # Region の数。R = SSD / REGION_N（MiB に切り捨て。meta: 626.5 MiB）
CHUNK=${CHUNK:-1M}                         # piece-wise 書き込みの幅（R より大きければ R に縮める）
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
REGION_BYTES=${REGION_BYTES:-}             # 空 = SSD ÷ REGION_N。バイト数を書くと Region の大きさをそれに固定（meta の過去の測定は 656932864）
WRITE_MARKS_TB=${WRITE_MARKS_TB:-"1.5"}    # 「先頭 W TB 書き込んだ時点」で効果を測る W（TB = 10^12 B。全受け入れ・分類なしの SSD 書き込み量で数える）
STATS=${STATS:-1}                          # 1 = og_trace_stats（サイズ・頻度の分布統計）も取る

# ---- 合成負荷（synth）------------------------------------------------------
SYNTH_N=${SYNTH_N:-20000000}               # 合成トレースの要求数
SYNTH_SCALED=${SYNTH_SCALED:-0}            # 1 = 規模を縮めた SSD 想定の負荷（scaled.py / writes.py: 2 億要求 × 多数）も走らせる。数時間かかる
SYNTH_SCALED_N=${SYNTH_SCALED_N:-200000000}
