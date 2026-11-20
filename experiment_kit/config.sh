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
# auto の見積り（実測で決めた。README §5.3「メモリと同時実行数」）。u = 物の数、x = min(1, (SSD+DRAM)/フットプリント)
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
CHUNK_MODE=${CHUNK_MODE:-piece}            # 片の扱い。piece = CacheLib の piece-wise caching と同じ（失った片だけが消え、欠けた片だけを
                                           # 取り直す。11-19 から既定）/ object = 片を 1 つ失うと物全体を捨てる（11-19 までの結果）。
                                           # chunk=0 の構成と物単位 SSD（obj 系）は片が無いので常に object。モードが違う結果は回し直す
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
  "st_hc0|$ST2,region-evict=lru,region-params=auto=1/w=0"        "st_mv0|$ST2,region-evict=mv,region-params=w=0"   # w=0 = BMR だけ（11-17）
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
  # E 名簿（ghost）の分類：追い出してすぐ戻ってきた物を別の Region へ。frac = 名簿の長さ ÷ SSD にいる物の数
  #   （docs/Che-sim/ghost_class_check.html。大きさとも分けるなら hint=ghost:frac=0.3/tau=<τ>）
  "ghost0.1|hint=ghost:frac=0.1,region-evict=lru" "ghost0.3|hint=ghost:frac=0.3,region-evict=lru"
  "ghost1|hint=ghost:frac=1,region-evict=lru"
)
# tune・tune-synth で回す構成（上の id を空白区切りで。all = 全部）。図の基準 none_h1 は必ず入る。
# 例: TUNE_METHODS="none_auto st_hc0.1_auto mini_hc0.1_auto" ./run.sh tune meta_nha
TUNE_METHODS=${TUNE_METHODS:-all}
# ---- Zipf の傾きと分類の利得（che。付録 §N.5）--------------------------------------
CHE_TAUS=${CHE_TAUS:-"16K 64K 256K 1M 4M"}   # Che 近似で試す分類の閾値の組（空白区切り。1 組に複数の τ はカンマ: "64K,1M"）。tune の st_lru の τ は自動で足す
CHE_W=${CHE_W:-0.03,0.1,0.3,1}              # 目的 J = BHR + w·OHR の w（静的分割の最適を w ごとに出す）
CHE_SIM=${CHE_SIM:-1}                       # 1 = RegionFlash も回す（容量の取り分の時系列と、固定された Region の記録。tune の none/obj があれば使う）
TUNE_W=${TUNE_W:-0.3}                      # 図で「良い構成」を選ぶ目的 BHR + w·OHR の w（mv の w と同じ意味）
TUNE_SYNTH=${TUNE_SYNTH:-"r95to5:1K:4194304:0.95:0.9,512K:25600:0.05:0.9 r3to1:1K:4194304:0.75:0.9,512K:25600:0.25:0.9"}
TUNE_SYNTH_N=${TUNE_SYNTH_N:-60000000}     # 合成トレースの長さ（1/64 縮小 SSD: 3.75 GiB、R = 4 MiB、chunk 16 KiB、DRAM なし）

# ---- 合成負荷（synth）------------------------------------------------------
SYNTH_N=${SYNTH_N:-20000000}               # 合成トレースの要求数
SYNTH_SCALED=${SYNTH_SCALED:-0}            # 1 = 規模を縮めた SSD 想定の負荷（scaled.py / writes.py: 2 億要求 × 多数）も走らせる。数時間かかる
SYNTH_SCALED_N=${SYNTH_SCALED_N:-200000000}

# ---- 論文用の比較（paper 系）-------------------------------------------------------------
# 流れ（README §6.5）:
#   1. ./run.sh paper-final2   本番：CacheLib と、現在の提案 ru_auto_v2 ほか比較に向いた方式    → work/paper/final2/
#   2. ./run.sh paper-final    同じ組で、提案を以前の ru_auto にしたもの（前の版との比較）        → work/paper/final/
#   3. ./run.sh paper-robust   頑健性（chunk・再挿入の上限・乱数の種）                            → work/paper/robust/
#   ./run.sh paper / paper-ext は検討用の全部と、CacheLib の書き直しの閾値違い（final 系はこの 2 つの場所に回して結果を共有する）
# 方式はすべて PAPER_CATALOG に 1 行ずつ置き、組ごとに id を選ぶ（並べた順が図と表の順）。
# 1 行 = "id|RegionFlash の指定" か "id|RegionFlash の指定|図に出す名前"。SSD・DRAM・chunk はトレースごとに validity と同じで、
# region-size を書かなければ R = RU（REGION_BYTES）。指定の意味は params.html。
# 自分の方式を足すには: カタログに 1 行足し、その id を組に並べる（名前を書かないと図には id が出る）。
# 基準 r256_base（いまの CacheLib）は差の基準なので、選んでいなくても必ず回す。既にある結果は回さない。
# 選んだ方式と中身は ./run.sh methods で確かめられる。環境変数でも選べる: PAPER_FINAL2_METHODS="r256_base ru_auto_v2" ./run.sh paper-final2
# （all = カタログの全部。ただし頑健性用の _c0・_c0m・_f1・_s<数> で終わる id は除く）
# 下の数字は Meta nha 全量・chunk-mode=piece での CacheLib n=1（BMR 4.705%）との ΔBMR [pt]・NAND 書き込み比
# （R=256 MiB は DLWA 1.4 を掛ける）。w01・Tencent の数字は docs/Che-sim/summary_1120.html。

# -- 部品 --
PAPER_R256=${PAPER_R256:-268435456}        # CacheLib の Region の上限（256 MiB）
B256="region-size=$PAPER_R256,hint=none,region-evict=lru"                # CacheLib の形: 256 MiB の Region・分類なし・Region LRU
#   分類と容量の配分
V2="hint=st_otsu:val=logreq/bins=1/hold=4/band=2/ema_den=0,region-evict=lru,region-params=auto=2/w=0/ff=0/move=2"
#     現在の版（11-20）: 閾値 = 大きさのビンごとの「書いた 1 バイトが何回要求されたか」の対数を、大津の式で 2 組に分ける所（累積で数える）。
#     容量 = SSD を出た物の名簿で各分類の「容量を少し足したら増える BHR」を測り、そろう向きへ大の分類の下限を動かす
#     （下限 0 から、OHR の重みなし。move=2 = 下限が効いていない間だけ、傾きが強く正なら今の割合との差を毎窓半分詰める）
AUTO="hint=split_threshold,region-evict=lru,region-params=auto=1/w=0.1"   # 以前の版: st の閾値 ＋ 下限の山登り（前の窓と J = BHR + 0.1·OHR を比べる）
AUTO0="hint=split_threshold,region-evict=lru,region-params=auto=1/w=0"    # 同じで目的を BHR だけに
#   書き直し（追い出す物をもう一度書くか）。CacheLib は reinsert=hits:n=1（1 回以上当たったら書き直す）
CBM="reinsert=hits:n=cb/lambda=0/base=marg"   # 現在の版（11-20）: 損得で決める n=cb。次の滞在で 1 バイトあたり何回当たったかの実測が、名簿で測る場所の限界の価値以上なら書き直す
CB="reinsert=hits:n=cb/lambda=0/base=first"   # n=cb で、比べる基準を「初めて書いた物の値」に（より厳しく、書き込みを減らす）
RA="reinsert=hits:n=auto/target=0.5"          # 以前の版 n=auto: 書き直した物が次も当たる割合 ≥ 0.5 になる最小の n（決め打ちの 0.5。書き直しを減らしすぎた）
#   受け入れ
ADM="admit=dram:min=1/ghost=0.9"           # S3-FIFO 型の篩: DRAM で当たった物と、断った後 SSD 容量 × 0.9 分の名簿にいる間にまた来た物だけ書く。
                                           # SSD が最初に埋まるまでは篩わない（fill=1 既定、S3-FIFO と同じ。11-19 以前は /fill=0）

PAPER_CATALOG=(
  # 1. 基準と Region の大きさ
  "r256_base|$B256,reinsert=hits:n=1"                     # 基準 = いまの CacheLib
  "ru_base|hint=none,region-evict=lru,reinsert=hits:n=1" # Region を RU にしただけ（−0.036・0.720）
  # 2. 現在の提案（V2）
  "ru_auto_v2|$V2,$CBM"                                  # 閾値・容量の配分・書き直しを全部自動（nha −0.132・0.695 / w01 −3.81 / Tencent −0.73）
  "ru_v2_n1|$V2,reinsert=hits:n=1"                       # 同じ分類と配分で、書き直しは CacheLib と同じ n=1
  "r256_v2_n1|region-size=$PAPER_R256,$V2,reinsert=hits:n=1"   # 同、R=256 MiB
  "ru_adm_v2_n1|$V2,reinsert=hits:n=1,$ADM"              # ＋篩（w01 では大きく効き、Tencent・ブロックのトレースでは逆効果）
  "ru_v2_cb|$V2,$CB"                                     # 書き直しを初めて書いた物の基準で（書き込みを減らす側）
  "ru_none_v2|hint=none,region-evict=lru,$CBM"          # 分類なしで書き直しだけ n=cb（11-20。ru_auto_v2 との差 = 分類＋配分の効果。paper-sens）
  "ru_adm_v2|$V2,$CBM,$ADM"                             # 現在の提案 ＋ 篩（11-20。既定ではどの組にも入れていない）
  # 3. 以前の提案（st の閾値 ＋ 下限の山登り）
  "ru_auto|$AUTO,$RA"                                     # 以前の ru_auto（+0.083・0.609。n=auto が書き直しを削りすぎ）
  "r256_auto|region-size=$PAPER_R256,$AUTO,$RA"
  "ru_auto_n1|$AUTO,reinsert=hits:n=1"                    # 書き直しを n=1 に（−0.062・0.708）
  "r256_auto_n1|region-size=$PAPER_R256,$AUTO,reinsert=hits:n=1"   # 同、R=256 MiB（−0.093・0.971）
  "ru_auto_w0|$AUTO0,$RA"                                 # 山登りの目的を BHR だけに（appendix §O）
  "r256_auto_w0|region-size=$PAPER_R256,$AUTO0,$RA"
  "ru_ff90_h3|hint=split_threshold,region-evict=lru,region-params=fc=1/ff=0.9,reinsert=hits:n=3"   # 手で調整（大の下限 0.9・n=3）
  "r256_ff90_h3|region-size=$PAPER_R256,hint=split_threshold,region-evict=lru,region-params=fc=1/ff=0.9,reinsert=hits:n=3"
  "ru_gate|hint=split_threshold,region-evict=lru,region-params=auto=1/w=0.1/gate=1,$RA"   # 下限が要るかを試してから山登り（既定では回さない）
  # 4. 部品の切り分け
  "ru_none_auto|hint=none,region-evict=lru,$RA"           # 分類なしで n=auto だけ（+0.137）
  "r256_none_auto|region-size=$PAPER_R256,hint=none,region-evict=lru,$RA"
  "ru_st_nauto|hint=split_threshold,region-evict=lru,$RA" # サイズ分類だけで容量の配分は決めない（OHR 寄り）
  "ru_st_n0|hint=split_threshold,region-evict=lru"        # 同、書き直しなし
  "ru_auto_ii|$AUTO,$RA/mode=ii"                          # 書き直した物を専用の書き込み口へ（CacheLib の priority と同じ形）
  "r256_base_cb|$B256,$CB"                                # CacheLib の書き直しだけ n=cb に（+0.011・0.989。n=1 がほぼ損得の答え）
  "r256_auto_cb|region-size=$PAPER_R256,$AUTO,$CB"        # 以前の分類＋下限＋n=cb（−0.028・0.891）
  "ru_auto_cb|$AUTO,$CB"                                  # 同、R=RU（−0.064・0.674）
  # 5. 先行研究の手がかり：置き場所の別の鍵（RIPQ / Flashield）と受け入れ（S3-FIFO 型の篩）
  "ru_dram_auto|hint=key:key=dram/k=2,region-evict=lru,region-params=auto=1/w=0.1,$RA"   # 分類の鍵 = DRAM でのヒット数（動的な 2 分割）
  "ru_dram1_auto|hint=key:key=dram/thr=1,region-evict=lru,region-params=auto=1/w=0.1,$RA" # 同（DRAM で 0 回 / 1 回以上）
  "r256_adm|$B256,reinsert=hits:n=1,$ADM"                 # CacheLib ＋ 篩（−0.003・0.608）
  "ru_adm_none_auto|hint=none,region-evict=lru,$RA,$ADM"  # 受け入れ × 置き場所の 2×2（相方は ru_none_auto・ru_auto）
  "ru_adm_auto|$AUTO,$RA,$ADM"                            # （+0.112・0.354）
  "ru_adm_auto_n1|$AUTO,reinsert=hits:n=1,$ADM"           # 篩＋以前の分類＋下限、n=1（−0.064・0.429）
  "r256_adm_auto_n1|region-size=$PAPER_R256,$AUTO,reinsert=hits:n=1,$ADM"   # 同、R=256 MiB（−0.074・0.588）
  "ru_adm_auto_cb|$AUTO,$CB,$ADM"                         # 篩＋以前の分類＋下限＋n=cb（−0.002・0.381）
  "ru_adm_dram2_auto|hint=key:key=dram/thr=2,region-evict=lru,region-params=auto=1/w=0.1,$RA,$ADM"   # 篩と同じ手がかりで置き場所も（既定では回さない）
  # 6. CacheLib の書き直しの閾値違い（paper-ext。n=1 は r256_base）
  "r256_n0|$B256"  "r256_n2|$B256,reinsert=hits:n=2"  "r256_n3|$B256,reinsert=hits:n=3"
  # 7. 頑健性（paper-robust）。id の末尾が条件で、図はそれを外した名前で並べる:
  #   _c0  chunk=0（片に分けず 1 Region の中に連続で置く。Region より大きい物は入らない）。片を 1 つ失うと物ごと消える
  #        RegionFlash の扱いを外した感度分析。実機の置き方ではない（CacheLib の piece-wise caching は物を片に分けて独立の項目にする）
  #   _c0m chunk=0 ＋ 受け入れの上限を全構成 256 MiB に（どの構成も同じ物の集まりにする。nha では 256 MiB を超える物が
  #        リクエストバイトの 44% あり、それを全構成でミスにするので、言えるのは 256 MiB 以下の物の中での傾向だけ）
  #   _f1  再挿入の上限なし（max_frac=1）   _s2 / _s3  乱数の種
  "r256_base_c0|$B256,reinsert=hits:n=1,chunk=0"  "r256_n3_c0|$B256,reinsert=hits:n=3,chunk=0"  "ru_auto_c0|$AUTO,$RA,chunk=0"
  "r256_base_c0m|$B256,reinsert=hits:n=1,chunk=0,admit=sizemax:max=$PAPER_R256"  "r256_n3_c0m|$B256,reinsert=hits:n=3,chunk=0,admit=sizemax:max=$PAPER_R256"
  "ru_auto_c0m|$AUTO,$RA,chunk=0,admit=sizemax:max=$PAPER_R256"
  "ru_base_c0m|hint=none,region-evict=lru,reinsert=hits:n=1,chunk=0,admit=sizemax:max=$PAPER_R256"   # R=RU にしただけ（ru_base）の得が、片の扱い（chunk）によるものかを見る
  "r256_base_f1|$B256,reinsert=hits:n=1/max_frac=1"  "r256_n3_f1|$B256,reinsert=hits:n=3/max_frac=1"  "ru_auto_f1|$AUTO,$RA/max_frac=1"
  "r256_base_s2|$B256,reinsert=hits:n=1,seed=2"  "ru_auto_s2|$AUTO,$RA,seed=2"
  "r256_base_s3|$B256,reinsert=hits:n=1,seed=3"  "ru_auto_s3|$AUTO,$RA,seed=3"
  "ru_auto_w0_c0|$AUTO0,$RA,chunk=0"  "ru_auto_w0_c0m|$AUTO0,$RA,chunk=0,admit=sizemax:max=$PAPER_R256"
  "ru_auto_w0_f1|$AUTO0,$RA/max_frac=1"  "ru_auto_w0_s2|$AUTO0,$RA,seed=2"  "ru_auto_w0_s3|$AUTO0,$RA,seed=3"
)

# -- 組（空白区切り。改行してもよい）--
# 本番（paper-final2）: 基準・CacheLib のつまみ・現在の提案・以前の提案の要素・切り分け・先行研究の手がかり（README §6.5.2）
PAPER_FINAL2_METHODS=${PAPER_FINAL2_METHODS:-"
  r256_base r256_n0 r256_n2 r256_n3 r256_auto_n1 ru_auto_n1 ru_base ru_none_auto ru_auto_v2 ru_dram1_auto r256_adm ru_adm_auto
  r256_adm_auto_n1 ru_adm_auto_n1 r256_base_cb ru_auto_cb"}
# 以前の本番（paper-final）: 上と同じ組で、提案を以前の ru_auto にしたもの。11-20 に足した V2 の変種 3 つも並べる
PAPER_FINAL_METHODS=${PAPER_FINAL_METHODS:-"
  r256_base r256_n0 r256_n2 r256_n3 r256_auto_n1 ru_auto_n1 ru_base ru_none_auto ru_auto ru_dram1_auto r256_adm ru_adm_auto
  r256_adm_auto_n1 ru_adm_auto_n1 r256_base_cb ru_auto_cb ru_v2_n1 r256_v2_n1 ru_adm_v2_n1 ru_auto_v2"}
# 検討用の全部（paper）: カタログの 1〜5 から ru_gate と ru_adm_dram2_auto を除いた 31 方式
PAPER_METHODS=${PAPER_METHODS:-"
  r256_base ru_base
  ru_auto_v2 ru_v2_n1 r256_v2_n1 ru_adm_v2_n1 ru_v2_cb
  ru_auto r256_auto ru_auto_n1 r256_auto_n1 ru_auto_w0 r256_auto_w0 ru_ff90_h3 r256_ff90_h3
  ru_none_auto r256_none_auto ru_st_nauto ru_st_n0 ru_auto_ii r256_base_cb r256_auto_cb ru_auto_cb
  ru_dram_auto ru_dram1_auto r256_adm ru_adm_none_auto ru_adm_auto ru_adm_auto_n1 r256_adm_auto_n1 ru_adm_auto_cb"}
PAPER_EXT_METHODS=${PAPER_EXT_METHODS:-"r256_n0 r256_n2 r256_n3"}
PAPER_ROBUST_METHODS=${PAPER_ROBUST_METHODS:-"
  r256_base_c0 r256_n3_c0 ru_auto_c0  r256_base_c0m r256_n3_c0m ru_auto_c0m ru_base_c0m  r256_base_f1 r256_n3_f1 ru_auto_f1
  r256_base_s2 ru_auto_s2 r256_base_s3 ru_auto_s3
  ru_auto_w0_c0 ru_auto_w0_c0m ru_auto_w0_f1 ru_auto_w0_s2 ru_auto_w0_s3"}
# 以前の指定の互換: PAPER_GATE=1 で ru_gate を足す / ROBUST_W0=0 で ru_auto_w0_* を外す
[ "${PAPER_GATE:-0}" = 1 ] && PAPER_METHODS="$PAPER_METHODS ru_gate"
[ "${ROBUST_W0:-1}" = 0 ] && PAPER_ROBUST_METHODS=$(printf '%s\n' $PAPER_ROBUST_METHODS | grep -v '^ru_auto_w0_' | tr '\n' ' ')

# -- 感度（paper-sens、11-20）：SSD 容量・DRAM・RU（Region の大きさ）を一つずつ変え、ほかは既定のまま（README §6.5.6）--
# 既定の点は work/paper の結果を共有する（同じ方式は回し直さない）。変えた点は work/paper/sens/<点>/。
# RU の点では、region-size を自分で持つ構成（r256_base など）は既定の点と同じ run なので回さず、図では既定の点の値を使う。
PAPER_SENS_METHODS=${PAPER_SENS_METHODS:-"r256_base ru_base ru_auto_v2 ru_v2_n1 ru_none_v2"}
SENS_SSD=${SENS_SSD-"0.33 2"}                                      # SSD 容量をトレースごとの既定（traces.tsv の ssd か SSD_DEFAULT）の何倍に
SENS_DRAM=${SENS_DRAM-"100 10"}                                    # DRAM = SSD ÷ この値（既定 DRAM_DIV = 30）
SENS_RU=${SENS_RU-"268435456 1342177280 2684354560"}               # Region = RU の大きさ [B]（256 MiB・1.25 GiB・2.5 GiB。既定 REGION_BYTES = 626.5 MiB）
