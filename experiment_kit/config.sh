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

# ---- 実行の仕方 ---------------------------------------------------------
NREQ=${NREQ:-}                             # 空 = 全要求。数字 = 各 RegionFlash 実行を先頭 NREQ 要求で打ち切る（試運転用）
INTERVAL=${INTERVAL:-1000000}              # 先頭 N 要求の曲線・時間変化の刻み（要求数）。11-20 に 10000 → 100 万（Wiki・Tencent CDN で区間ごとの CSV が
                                           # 大きくなりすぎストレージを圧迫したため）。細かく見たいときや 1000 万要求未満のトレースは小さく（例 INTERVAL=10000）
# 済みの判定（11-20）: 各 run は <フォルダ>/cmd.txt（パッチの版＋cachesim の引数）が今と完全に一致するときだけ飛ばす。
#   まだの run でも、全く同じコマンドの済んだ run が work/ の別の場所にあれば、その結果を写す
ADOPT_OLD=${ADOPT_OLD:-0}                  # 1 = cmd.txt の無い 11-20 より前の結果を、前の判定（片の扱い・fill）が通れば今の指定のものと見なして使う
SIG_PATCH=${SIG_PATCH:-1}                  # 0 = パッチの版は照合しない（キットを更新しても結果が変わらないと分かっているとき）
GUARD_SEC=${GUARD_SEC:-5}                  # 止め方の見張りの間隔 [秒]：run.sh か work/running/<pid> が消えたら、その run.sh の子孫を全部止める（11-20）
# 図の組（11-24）：組の方式が多いと図が潰れるので、図は目的ごとの小さな組にも分けて <組の場所>/figs_<名前>/ に出す（表は全体の方
# にも全部載る）。1 つ = "名前:方式 方式 .."。基準 r256-none-nat-n1 は必ず入る。その組の選択に無い方式は飛ばす。空にすれば出さない
PAPER_FIGSETS=(
  "main:r256-none-nat-n0 r256-none-nat-n3 ru-none-nat-n1 ru-otsu-ghost-cb r256-none-nat-n1-adm ru-otsu-ghost-cb-adm"     # 主の結果
  "parts:ru-none-nat-n1 ru-otsu-nat-cb ru-otsu-ghost-n1 ru-otsu-ghost-cb ru-none-nat-cb"                               # Q3 部品の寄与
  "lambda:r256-none-nat-n0 r256-none-nat-n2 r256-none-nat-n3 ru-otsu-ghost-cb ru-otsu-ghost-cb.l01 ru-otsu-ghost-cb.l03 ru-otsu-ghost-cb.l1"   # Q4 書き込みとの交換
  "adm:ru-none-nat-cb ru-otsu-ghost-cb ru-none-nat-cb-adm ru-otsu-ghost-cb-adm r256-none-nat-n1-adm"                  # Q5 篩との 2×2
  "prior:ru-otsu-ghost-cb ru-st-climb-nauto ru-st-climb-n1 r256-st-climb-n1 ru-none-nat-nauto ru-dram.t1-climb-nauto ru-st-climb-nauto-adm ru-st-climb-n1-adm r256-st-climb-n1-adm r256-none-nat-cb.first ru-st-climb-cb.first"   # 以前の提案・先行研究の手がかり
)
# 説明用の組（11-25）：./run.sh paper-figs --user-method [場所] が、ここに並べた方式だけの図と表を work/paper/<場所>/（既定 users）に出す。
# 回さない（work/paper・work/paper/ext に済んだ結果を使う）。基準 r256-none-nat-n1 は必ず入る。並べた順が図の順。
# fig_summary.svg = トレースごとに方式を横棒で並べた 1 枚（左 ΔBMR、右 書き込みの比）。traces.tsv の群ごとの一式も groups/ に出る
PAPER_USER_METHODS=${PAPER_USER_METHODS:-"
  r256-none-nat-n3 r256-none-nat-n1-adm ru-none-nat-n1 ru-st-climb-nauto ru-otsu-ghost-cb ru-otsu-ghost-cb-adm"}
GROUP_FIGS=${GROUP_FIGS:-1}                # 1 = paper 系の図と表を、全体（work/paper/<組>/）に加えてトレースの群（traces.tsv の category × source）ごとにも
                                           #     work/paper/<組>/groups/<category>__<source>/ に出す（11-20）。run は共有なので回す量は変わらない

# ---- 1 本のトレースの SSD・DRAM・Region（validity・tune・paper 共通）と validity の構成 ----------
SSD_DEFAULT=${SSD_DEFAULT:-30%}            # SSD 容量: 240G（固定）/ 30%（フットプリントに対する割合）。traces.tsv の ssd 列で個別指定
DRAM_DIV=${DRAM_DIV:-30}                   # DRAM = SSD / DRAM_DIV        （meta: 240G の SSD に 8G）
REGION_N=${REGION_N:-392}                  # Region の数。R = SSD / REGION_N（MiB に切り捨て。meta: 626.5 MiB）
CHUNK=${CHUNK:-64K}                        # piece-wise 書き込みの幅（R より大きければ R に縮める）
CHUNK_MODE=${CHUNK_MODE:-piece}            # 片の扱い。piece = CacheLib の piece-wise caching と同じ（失った片だけが消え、欠けた片だけを
                                           # 取り直す。11-19 から既定）/ object = 片を 1 つ失うと物全体を捨てる（11-19 までの結果）。
                                           # chunk=0 の構成と物単位 SSD（obj 系）は片が無いので常に object。モードが違う結果は回し直す
PIECE=${PIECE:-131072}                     # K(N)（piece 数）の数え方。CacheLib は 128 KiB
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
#   1. ./run.sh paper-final2   本番：CacheLib と、現在の提案 ru-otsu-ghost-cb ほか比較に向いた方式    → work/paper/final2/
#   2. ./run.sh paper-final    同じ組で、提案を以前の ru-st-climb-nauto にしたもの（前の版との比較）        → work/paper/final/
#   3. ./run.sh paper-robust   頑健性（chunk・再挿入の上限・乱数の種）                            → work/paper/robust/
#   ./run.sh paper / paper-ext は検討用の全部と、CacheLib の書き直しの閾値違い（final 系はこの 2 つの場所に回して結果を共有する）
# 方式はすべて PAPER_CATALOG に名前を 1 つずつ置き、組ごとに名前を選ぶ（並べた順が図と表の順）。指定は名前から作る（下の「方式の名前の付け方」）。
# SSD・DRAM・chunk はトレースごとに validity と同じで、R=RU なら region-size = REGION_BYTES。指定の意味は params.html。
# 基準 r256-none-nat-n1（いまの CacheLib）は差の基準なので、選んでいなくても必ず回す。既にある結果は回さない（同じコマンドの run は写す）。
# 選んだ方式と中身は ./run.sh methods で確かめられる。環境変数でも選べる: PAPER_FINAL2_METHODS="r256-none-nat-n1 ru-otsu-ghost-cb" ./run.sh paper-final2
# （all = カタログの全部。ただし頑健性用の ~ を含む名前は除く）

# -- 方式の名前の付け方（11-20）--
# 名前 = 部品を - でつないだもの：  R-閾値-配分-再挿入[-adm][~変種..]
# 名前から指定を組み立てる（run.sh の id_spec）。部品の中身（設定）を変えたら必ず別の名前になるので、版の名前（v2 など）は使わない。
# 細かい設定は部品の後ろに .設定 で付ける（既定と違うものだけ、下の順で）。数は先頭が 0 なら小数（l01 = 0.1、w0 = 0、l1 = 1）。
#   R      r256 = Region 256 MiB（CacheLib の上限、PAPER_R256）   ru = Region = RU（REGION_BYTES）
#   閾値   none = 分類なし   st = SplitThreshold（数とバイトの中央値の対数の真ん中）
#          otsu = 大津の式・再利用の強さ（大きさのビンごとの log(要求バイト ÷ 書いたバイト)、累積、hold 4・band 2）  .cd<W> = 変化の検出（窓 W 回の再計算）
#          fix.<大きさ> = 固定（fix.256k、fix.32m）   dram.k<n> = DRAM のヒット数で n 分割（動的）   dram.t<n> = DRAM で n 回以上 / 未満
#   配分   nat = 下限なし（Region LRU の自然な配分）
#          ghost = 名簿で測る限界の BHR がそろう向きへ大の分類の下限を動かす（auto=2、OHR の重み 0、下限 0 から）。既定 move=4：
#                  傾きが強く負（平滑値 < −0.5）なら下限を毎窓半分に（負荷が変わって大の分類が要らなくなったとき速く下げる。11-20 夜）
#                  .m0 = 半分にしない（一定の歩幅だけ）  .m2 = .m0 ＋ 下限が効いていない間、傾きが強く正なら今の割合との差を毎窓半分詰める
#                  .m3 = .m2 ＋ .m4 の下げ方（.m2・.m3 は w01・Tencent で負ける。比較用）
#          climb = 下限の山登り（auto=1、前の窓と J = BHR + w·OHR を比べる。既定 w=0.1）  .w<w>、.gate = 下限が要るかを試してから
#          ff.<百分率> = 大の分類の下限を固定（ff.90 = 0.9）
#   再挿入 n0 = 書き直さない   n1〜n9 = その回数以上当たった物を書き直す（CacheLib は n1）
#          nauto = 書き直した物が次も当たる割合 ≥ 0.5 になる最小の n   .ii = 書き直した物を専用の書き込み口へ
#          cb = 損得：次の滞在の 1 バイトあたりのヒット ≥ 名簿の限界の価値 ＋ λ なら書き直す（既定 λ=0）  .l<λ>、.first = 比べる基準を初めて書いた物の値に
#   受け入れ adm = S3-FIFO 型の篩（DRAM で当たった物と、断った後 SSD 容量 × 0.9 分の名簿にいる間にまた来た物だけ書く。SSD が一度埋まるまでは篩わない）
#   変種   ~c0 = chunk=0（片に分けない）  ~c0m = chunk=0 ＋ 受け入れの上限 256 MiB  ~f1 = 再挿入の上限なし（max_frac=1）  ~s<n> = 乱数の種
# 例：CacheLib = r256-none-nat-n1、現在の提案 = ru-otsu-ghost-cb、篩つき = ru-otsu-ghost-cb-adm、その種 2 = ru-otsu-ghost-cb~s2。
# 自分の方式を足すには、名前を下のカタログに 1 行足して組に並べる（"名前|図に出す名前" と書けば図の名前を変えられる。書かなければ名前から作る）。
PAPER_R256=${PAPER_R256:-268435456}        # CacheLib の Region の上限（256 MiB）
# 下の数字は Meta nha 全量・chunk-mode=piece での CacheLib n=1（BMR 4.705%）との ΔBMR [pt]・NAND 書き込み比
# （R=256 MiB は DLWA 1.4 を掛ける）。w01・Tencent の数字は docs/Che-sim/summary_1120.html と tools/synth/results/move_table.json。

PAPER_CATALOG=(
  # 1. 基準と Region の大きさ
  r256-none-nat-n1                          # 基準 = いまの CacheLib
  ru-none-nat-n1                            # Region を RU にしただけ（−0.036・0.720）
  # 2. 現在の提案：閾値 = 大津（再利用の強さ）、配分 = 名簿（move=4）、書き直し = 損得（nha −0.112 / w01 −3.81 / Tencent −0.755 / msr −0.32）
  ru-otsu-ghost-cb
  ru-otsu-ghost-n1  r256-otsu-ghost-n1      # 書き直しだけ CacheLib と同じ n=1（R=RU / 256 MiB）
  ru-otsu-ghost-n1-adm                      # ＋篩（書き直し n=1）
  ru-otsu-ghost-cb-adm                      # ＋篩
  ru-otsu-ghost-cb.first                    # 書き直しの基準を初めて書いた物の値に（書き込みを減らす側）
  ru-otsu-nat-cb  ru-none-nat-cb  ru-none-nat-cb-adm   # 部品の切り分け（論文の Q3・Q5）：配分なし / 分類なし / 分類なし＋篩
  ru-otsu-ghost-cb.l01  ru-otsu-ghost-cb.l03  ru-otsu-ghost-cb.l1   # 書き込みの値段 λ（Q4。交換関係の図で λ の線）
  ru-otsu-ghost.m0-cb  ru-otsu-ghost.m2-cb  ru-otsu-ghost.m3-cb   # 配分の動かし方の比較（move=0：nha −0.128、ほかは move=4 と同じ。move=2 / 3：w01 −2.39・Tencent −0.09 / −0.19）
  # 3. 以前の提案（st の閾値 ＋ 下限の山登り）
  ru-st-climb-nauto  r256-st-climb-nauto    # 以前の ru-st-climb-nauto（+0.083・0.609。n=auto が書き直しを削りすぎ）
  ru-st-climb-n1  r256-st-climb-n1          # 書き直しを n=1 に（−0.062・0.708 / −0.093・0.971）
  ru-st-climb.w0-nauto  r256-st-climb.w0-nauto   # 山登りの目的を BHR だけに（appendix §O）
  ru-st-ff.90-n3  r256-st-ff.90-n3          # 手で調整（大の下限 0.9・n=3）
  ru-st-climb.gate-nauto                    # 下限が要るかを試してから山登り（既定では回さない）
  # 4. 部品の切り分け（以前の版）
  ru-none-nat-nauto  r256-none-nat-nauto    # 分類なしで n=auto だけ（+0.137）
  ru-st-nat-nauto  ru-st-nat-n0             # サイズ分類だけで容量の配分は決めない。n=auto / 書き直しなし
  ru-st-climb-nauto.ii                      # 書き直した物を専用の書き込み口へ（CacheLib の priority と同じ形）
  r256-none-nat-cb.first                    # CacheLib の書き直しだけ損得に（+0.011・0.989）
  r256-st-climb-cb.first  ru-st-climb-cb.first   # 以前の分類＋下限＋損得（−0.028・0.891 / −0.064・0.674）
  # 5. 先行研究の手がかり：置き場所の別の鍵（RIPQ / Flashield）と受け入れ（S3-FIFO 型の篩）
  ru-dram.k2-climb-nauto  ru-dram.t1-climb-nauto   # 分類の鍵 = DRAM でのヒット数（動的な 2 分割 / 0 回と 1 回以上）
  r256-none-nat-n1-adm                      # CacheLib ＋ 篩（−0.003・0.608）
  ru-none-nat-nauto-adm  ru-st-climb-nauto-adm     # 受け入れ × 置き場所の 2×2（以前の版。相方 ru-none-nat-nauto・ru-st-climb-nauto）
  ru-st-climb-n1-adm  r256-st-climb-n1-adm  # 篩＋以前の分類＋下限、n=1（−0.064・0.429 / −0.074・0.588）
  ru-st-climb-cb.first-adm                  # （−0.002・0.381）
  ru-dram.t2-climb-nauto-adm                # 篩と同じ手がかりで置き場所も（既定では回さない）
  # 6. CacheLib の書き直しの閾値違い（paper-ext。n=1 は r256-none-nat-n1）
  r256-none-nat-n0  r256-none-nat-n2  r256-none-nat-n3
  # 7. 頑健性（paper-robust）。~ の後が条件で、図はそれを外した名前で並べる（~c0・~c0m は実機の置き方ではない感度分析。README §6.5.4）
  r256-none-nat-n1~c0  r256-none-nat-n3~c0  ru-st-climb-nauto~c0
  r256-none-nat-n1~c0m  r256-none-nat-n3~c0m  ru-st-climb-nauto~c0m  ru-none-nat-n1~c0m
  r256-none-nat-n1~f1  r256-none-nat-n3~f1  ru-st-climb-nauto~f1
  r256-none-nat-n1~s2  ru-st-climb-nauto~s2  r256-none-nat-n1~s3  ru-st-climb-nauto~s3
  ru-otsu-ghost-cb~s2  ru-otsu-ghost-cb~s3  # 現在の提案の種（Q2 の誤差）
  ru-st-climb.w0-nauto~c0  ru-st-climb.w0-nauto~c0m  ru-st-climb.w0-nauto~f1  ru-st-climb.w0-nauto~s2  ru-st-climb.w0-nauto~s3
)

# -- 組（空白区切り。改行してもよい。並べた順が図と表の順）--
# 本番（paper-final2）: 基準・CacheLib のつまみ・現在の提案・以前の提案の要素・切り分け・先行研究の手がかり（README §6.5.2）、
# 2 行目 = 論文の問い（docs/Che-sim/paper_story.html §3）：Q3 部品の段階（ru-none-nat-n1 → ru-otsu-nat-cb → ru-otsu-ghost-n1 → ru-otsu-ghost-cb）、
# Q4 λ の線、Q5 篩との 2×2（ru-none-nat-cb・ru-otsu-ghost-cb × ru-none-nat-cb-adm・ru-otsu-ghost-cb-adm）。種は paper-robust
PAPER_FINAL2_METHODS=${PAPER_FINAL2_METHODS:-"
  r256-none-nat-n1 r256-none-nat-n0 r256-none-nat-n2 r256-none-nat-n3 r256-st-climb-n1 ru-st-climb-n1 ru-none-nat-n1 ru-none-nat-nauto
  ru-otsu-ghost-cb ru-dram.t1-climb-nauto r256-none-nat-n1-adm ru-st-climb-nauto-adm r256-st-climb-n1-adm ru-st-climb-n1-adm
  r256-none-nat-cb.first ru-st-climb-cb.first
  ru-otsu-nat-cb ru-otsu-ghost-n1 ru-none-nat-cb ru-none-nat-cb-adm ru-otsu-ghost-cb-adm ru-otsu-ghost-cb.l01 ru-otsu-ghost-cb.l03 ru-otsu-ghost-cb.l1"}
# 以前の本番（paper-final）: 上と同じ組で、提案を以前の ru-st-climb-nauto にしたもの。現在の提案と、その n=1 版も並べる
PAPER_FINAL_METHODS=${PAPER_FINAL_METHODS:-"
  r256-none-nat-n1 r256-none-nat-n0 r256-none-nat-n2 r256-none-nat-n3 r256-st-climb-n1 ru-st-climb-n1 ru-none-nat-n1 ru-none-nat-nauto
  ru-st-climb-nauto ru-dram.t1-climb-nauto r256-none-nat-n1-adm ru-st-climb-nauto-adm r256-st-climb-n1-adm ru-st-climb-n1-adm
  r256-none-nat-cb.first ru-st-climb-cb.first ru-otsu-ghost-n1 r256-otsu-ghost-n1 ru-otsu-ghost-n1-adm ru-otsu-ghost-cb"}
# 検討用の全部（paper）: カタログの 1〜5 から gate と dram.t2 の篩つきを除いたもの
PAPER_METHODS=${PAPER_METHODS:-"
  r256-none-nat-n1 ru-none-nat-n1
  ru-otsu-ghost-cb ru-otsu-ghost-n1 r256-otsu-ghost-n1 ru-otsu-ghost-n1-adm ru-otsu-ghost-cb.first ru-otsu-ghost.m0-cb ru-otsu-ghost.m2-cb ru-otsu-ghost.m3-cb
  ru-st-climb-nauto r256-st-climb-nauto ru-st-climb-n1 r256-st-climb-n1 ru-st-climb.w0-nauto r256-st-climb.w0-nauto ru-st-ff.90-n3 r256-st-ff.90-n3
  ru-none-nat-nauto r256-none-nat-nauto ru-st-nat-nauto ru-st-nat-n0 ru-st-climb-nauto.ii r256-none-nat-cb.first r256-st-climb-cb.first ru-st-climb-cb.first
  ru-dram.k2-climb-nauto ru-dram.t1-climb-nauto r256-none-nat-n1-adm ru-none-nat-nauto-adm ru-st-climb-nauto-adm ru-st-climb-n1-adm r256-st-climb-n1-adm ru-st-climb-cb.first-adm"}
PAPER_EXT_METHODS=${PAPER_EXT_METHODS:-"r256-none-nat-n0 r256-none-nat-n2 r256-none-nat-n3"}
PAPER_ROBUST_METHODS=${PAPER_ROBUST_METHODS:-"
  r256-none-nat-n1~c0 r256-none-nat-n3~c0 ru-st-climb-nauto~c0  r256-none-nat-n1~c0m r256-none-nat-n3~c0m ru-st-climb-nauto~c0m ru-none-nat-n1~c0m
  r256-none-nat-n1~f1 r256-none-nat-n3~f1 ru-st-climb-nauto~f1
  r256-none-nat-n1~s2 ru-st-climb-nauto~s2 r256-none-nat-n1~s3 ru-st-climb-nauto~s3
  ru-st-climb.w0-nauto~c0 ru-st-climb.w0-nauto~c0m ru-st-climb.w0-nauto~f1 ru-st-climb.w0-nauto~s2 ru-st-climb.w0-nauto~s3
  ru-otsu-ghost-cb~s2 ru-otsu-ghost-cb~s3"}
# 以前の指定の互換: PAPER_GATE=1 で gate を足す / ROBUST_W0=0 で climb.w0 の頑健性を外す
[ "${PAPER_GATE:-0}" = 1 ] && PAPER_METHODS="$PAPER_METHODS ru-st-climb.gate-nauto"
[ "${ROBUST_W0:-1}" = 0 ] && PAPER_ROBUST_METHODS=$(printf '%s\n' $PAPER_ROBUST_METHODS | grep -v '^ru-st-climb.w0-nauto~' | tr '\n' ' ')

# -- 感度（paper-sens、11-20）：SSD 容量・DRAM・RU（Region の大きさ）を一つずつ変え、ほかは既定のまま（README §6.5.6）--
# 既定の点は work/paper の結果を共有する（同じ方式は回し直さない）。変えた点は work/paper/sens/<点>/。
# RU の点では、region-size を自分で持つ構成（r256-none-nat-n1 など）は既定の点と同じ run なので回さず、図では既定の点の値を使う。
PAPER_SENS_METHODS=${PAPER_SENS_METHODS:-"r256-none-nat-n1 ru-none-nat-n1 ru-otsu-ghost-cb ru-otsu-ghost-n1 ru-none-nat-cb"}
SENS_SSD=${SENS_SSD-"0.33 2"}                                      # SSD 容量をトレースごとの既定（traces.tsv の ssd か SSD_DEFAULT）の何倍に
SENS_DRAM=${SENS_DRAM-"100 10"}                                    # DRAM = SSD ÷ この値（既定 DRAM_DIV = 30）
SENS_RU=${SENS_RU-"268435456 1342177280 2684354560"}               # Region = RU の大きさ [B]（256 MiB・1.25 GiB・2.5 GiB。既定 REGION_BYTES = 626.5 MiB）
