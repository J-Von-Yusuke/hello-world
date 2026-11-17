# 実験キット: トレース実験と合成負荷（別 PC 用）

改造した libCacheSim（RegionFlash）で、次の 2 つの実験を別の PC で再現し、**出典・種類ごとに傾向をまとめる**ためのキット。

| 実験 | 何をするか | 入力 |
|---|---|---|
| **validity**（CDN トレースの妥当性検証と同じ） | トレース 1 本ごとに、物単位 LRU・RegionFlash（分類なし/あり、再挿入、書き込み予算）を全量 1 回走らせ、**先頭 N 要求で測ったときの値**を全 N について得る。分類の効果が部分トレースで再現できるかを見る | `traces.tsv` に書いたトレース |
| **synth**（合成トレースによる評価） | 少数サイズの合成負荷を作り、理論（Che 型モデル）と実測（cachesim）を並べる | `synth_workloads.txt`（既定の 6 負荷つき） |

```
traces.tsv ─┐                       ┌→ work/results/<name>.<変種>.json（1 本の要約）
config.sh  ─┼→ run.sh validity ─────┤
            │                       └→ work/out/<name>/<変種>/…（cachesim の生の出力）
synth_workloads.txt → run.sh synth → work/synth/*.json
traces.tsv → run.sh che → work/che/report.html, che_summary.csv（Zipf の傾きと分類の利得。§6.4）
                                      ↓
                       run.sh aggregate → work/results/report.html, per_trace.csv, by_*.csv, synth_summary.csv
                                          work/results/figs/  スライド用の図（SVG・PNG）、説明（index.html・captions.md）、slides.pptx
```

## 1. 必要なもの

Linux（Windows なら WSL2 の Ubuntu）。`gcc cmake make git python3 libzstd-dev libglib2.0-dev`（Python は標準ライブラリだけ）。
スライド用の PNG と `slides.pptx` も欲しければ、SVG を PNG にする道具を 1 つと日本語フォントを入れる: `sudo apt install librsvg2-bin fonts-noto-cjk`（`rsvg-convert` の代わりに `cairosvg` / `inkscape` でもよい。LibreOffice は使わない：変換が止まることがあった）。無くても SVG と説明は出る。
メモリ: 同時実行数 `P` は既定 `auto` で、変種ごとに空きメモリとトレースの物の数から自動で決まる（§4.1）。ディスクは `work/` に、変換したトレースと出力（トレース 1 本あたり数百 MB〜数 GB）。

## 2. 手順

```bash
cd experiment_kit
chmod +x run.sh             # Windows から zip で持ってきた場合
./run.sh setup                 # 上流 libCacheSim を clone → regionflash.patch を当てて cachesim をビルド、C ツールをコンパイル
$EDITOR traces.tsv             # ★ トレースのパスを書く（§3）
$EDITOR config.sh              # SSD 容量などを必要なら直す（§4）
./run.sh check                 # パスが読めるか、名前が重複していないか
NREQ=2000000 ./run.sh validity # 試運転: 各トレースの先頭 200 万要求だけ（数分）。結果の形を確かめる
rm -rf work/out work/results   # 試運転の結果を消す
./run.sh validity              # 本番（途中で止めても、同じコマンドで続きから: 済んだ run は飛ばす）
./run.sh synth                 # 合成負荷
./run.sh aggregate             # 集計 → work/results/report.html
```

一部だけ: `./run.sh validity meta_nha wiki_cdn`。全部まとめて: `./run.sh all`。
clone 済みの libCacheSim を使うには、`config.sh` の `LCS_SRC` にそのパスを書きます。clone は省かれます。

- **未パッチの clone**: `setup` がそこへ `regionflash.patch` を当てます。当たるかを `git apply --check` で先に確かめ、当たらなければ何も変えずに止まります。
- **パッチ済みの clone**: そのまま使います。

パッチの土台は `93529a1` です。上流の最新(2026-11-09 の `4e3521a`)には当たりません。手元の clone が新しい場合は、どちらかで土台に合わせます。

```bash
git -C <clone> checkout 93529a1              # その clone を土台に戻す
git -C <clone> worktree add ../lcs-rf 93529a1   # 元の作業を残したまま別フォルダに土台を出し、LCS_SRC=../lcs-rf にする
```

`CACHESIM` に作成済みの cachesim を指定すれば、ビルドも省けます。

## 3. トレースのパスを書く場所: `traces.tsv`

タブ区切り。1 行 1 トレース。`#` で始まる行は無視。

| 列 | 意味 | 例 |
|---|---|---|
| `name` | 半角英数と `_`。結果のフォルダ名（重複不可） | `meta_nha` |
| `path` | トレースの絶対パス（キットを動かす PC 上の） | `/data/meta_rnha.oracleGeneral.zst` |
| `format` | libCacheSim が読める形式名。空 = `oracleGeneral` | `oracleGeneral` / `csv` / `twr` … |
| `params` | csv 等の読み方（`traceConv -t` の引数） | `time-col=1,obj-id-col=2,obj-size-col=3,obj-id-is-num=1,delimiter=,` |
| `category` | **種類**のまとめ単位 | `CDN` / `KV` / `Block` |
| `source` | **出典**（会社・データセット） | `Meta` / `Tencent` / `Wikimedia` / `Twitter` / `MSR` |
| `ssd` | この SSD 容量だけ個別指定。空 = `SSD_DEFAULT` | `240G`（固定）/ `30%`（フットプリント比） |
| `note` | メモ（集計に使わない） | |

- **`oracleGeneral.zst` 以外の形式は自動変換する**（`traceConv` → zstd。`work/traces/` に保存）。csv の場合は `params` が必須。
- 列は足せる（例: `region`）。`./run.sh aggregate --by category,source,region` で、その列でもまとめる。
- `category` と `source` は自由な文字列。「KV の Twitter」「Block の Tencent」のように**同じ種類に複数の出典**を入れれば、種類ごと・出典ごと・その組ごとの 3 通りで集計される（既定は `category`, `source`, `category/source`）。

## 4. 設定: `config.sh`

主なもの（環境変数でも上書きできる。例 `P=2 ./run.sh validity`）。

| 項目 | 既定 | 意味 |
|---|---|---|
| `SSD_DEFAULT` | `30%` | SSD 容量。`240G` なら固定、`30%` ならトレースのフットプリント（物の最初のサイズの和）に対する割合 |
| `DRAM_DIV` | `30` | DRAM = SSD ÷ 30（meta の 240 GiB に 8 GiB） |
| `REGION_N` | `392` | Region の数。R = SSD ÷ 392（meta: 626.5 MiB） |
| `HINTS` | none / kf4 / sf8 / st | 分類方法。`id:hint指定` を足せば、その分類の効果も集計される |
| `BUDGET_DWPD` | `3 1` | 書き込み予算つき構成（1 日あたりの SSD 全書き換え回数）。空なら走らせない |
| `SAT_VALUE` / `SAT_VARIANTS` / `SAT` | `4294967295` / `drop delete` / `auto` | サイズ `2^32−1`（= −1 を uint32 で書いたもの。GET 以外の操作の印と見る）の要求の扱い。`auto` = その要求があるトレースでは `SAT_VARIANTS` を走らせる（先頭が主結果）。`drop` = 無かったことにする、`delete` = その物を消す操作（要求に数えない）、`fix` = その物の他のサイズに置換（感度用）、`asis` = そのまま。`SAT="drop delete asis"` のように直接並べてもよい |
| `WRITE_MARKS_TB` | `1.5` | 「先頭から SSD に W TB 書いた時点」で効果を測る W（全受け入れ・分類なしの書き込み量で数える。TB = 10^12 B） |
| `REGION_BYTES` | 空 | Region の大きさをバイト数で固定（空 = SSD ÷ `REGION_N`） |
| `INTERVAL` | `10000` | 先頭 N 曲線の刻み（要求数）。1000 万要求未満のトレースは `1000` に |
| `P` | `auto` | 同時に走らせる cachesim の数。`auto` = §4.1 の見積りで自動。数字なら固定 |
| `NCPU` | CPU の総コア数 − 1 | 並列に使うプロセス数の上限。`P=auto` の上限、`P=auto` のときの tune-synth の並列数、`SYNTH_P` の既定値、ビルドの `make -j` に使う。`NCPU=8 ./run.sh …` で変えられる |
| `MEM_FRAC` | `0.8` | `auto` で使ってよい空きメモリ（`MemAvailable`）の割合 |
| `NREQ` | 空 | 数字を入れると各 run を先頭 NREQ 要求で打ち切る（試運転） |
| `SYNTH_N` / `SYNTH_SCALED` | 2000 万 / `0` | 合成トレースの長さ / 縮小 SSD 想定の重い負荷（2 億要求 × 多数、数時間）を走らせるか |

### 4.1 メモリと同時実行数（`P=auto`）

cachesim 1 本のピーク RSS は、次の 2 つの物の数でほぼ決まる。u = トレースの物の数（`og_info` の `uniq`）、x = min(1, (SSD+DRAM) ÷ フットプリント) = SSD に入る物の割合。

- **見た物すべて**: 分類（`kf4` `sf8`）の頻度表。1 個あたり約 70〜85 B。消えないので、トレースが長いほど増える。
- **いまキャッシュにある物**: 索引。Region 構成は 1 個あたり約 150 B。物単位 SSD（`obj_d*`）は約 310 B。

```
1 本 ≈ max( MEM_BASE + MEM_SEEN·u + MEM_RES·u·x ,  MEM_BASE + MEM_RES_OBJ·u·x ) × MEM_SAFETY
P    = floor( MemAvailable × MEM_FRAC ÷ 1 本 )   を 1〜NCPU（既定 = CPU の総コア数 − 1）に収める
```

係数の既定値(`config.sh`)は、少し多めに取ってある。

| 係数 | 既定値 |
|---|---|
| `MEM_BASE` | 400 MB |
| `MEM_SEEN` | 80 B |
| `MEM_RES` | 160 B |
| `MEM_RES_OBJ` | 320 B |
| `MEM_SAFETY` | 1.25 |

ハッシュ表は倍々に広がるので、その山の分として `MEM_SAFETY` を掛ける。係数は、次の全量の実測(WSL、Release ビルド)から決めた。

| トレース | u | x | none | kf4 / sf8+h1 | obj | 見積り(1 本) |
|---|---|---|---|---|---|---|
| Meta CDN nha（drop, SSD 240G） | 3,500 万 | 0.013 | 0.34 GiB | 2.63〜2.68 GiB | 0.46 GiB（1,600 万要求時点） | 3.8 GiB（31 GiB の PC で P=6） |
| w01（ブロック, SSD 30%） | 1.23 億 | 0.31 | 6.0 GiB | 14.3 GiB | 11.7 GiB | 19.0 GiB（同 P=1） |
| Tencent ブロック（SSD 30%、検証用。係数決めには使っていない） | 285 万 | 0.31 | — | 0.63 GiB（sf8） | 0.67 GiB（obj_d1） | 0.90 GiB |

- `MemAvailable` は変種ごとに読み直す。ほかの処理がメモリを使っていれば、その分だけ `P` が下がる。
- run ごとの実測ピークは `work/out/<name>/<変種>/<id>/rss`(KB)に残る。ログには「実測ピーク RSS の最大 … GB(見積り … GB)」と出る。実測が見積りに近い・超える場合は、`MEM_*` を上げる。
- 1 本の見積りが使えるメモリを超えると、警告を出して `P=1` で走らせる。
- 試運転(`NREQ`)でも u には全量の値を使うので、`P` は小さめ(安全側)になる。

## 5. 何を測るか（validity）

トレース 1 本・1 変種につき、**全量を 1 回流す**。LRU 系は未来に依存しないので、`interval` ごとの累積値（`P.interval.csv` の `cumulative` 行）の先頭 N 要求までが、先頭 N 要求だけを流した run と同じになる。1 回の run から全 N の値が得られる。

走らせる構成（トレース 1 本あたり cachesim 26 本（RegionFlash 24 + 理想の SSD 2）+ 物単位 LRU の曲線 1 本）:

| id | 構成 |
|---|---|
| `none` `kf4` `sf8` `st` | 全受け入れ。分類なし / 要求回数で 4 分類（`key=freq,k=4`）/ サイズ × 回数で 8 分類（`sizefreq,k=8,kf=4`）/ サイズだけで 2 分類（`split_threshold`。閾値は SplitThreshold の規則 `util/sst.h` で、書いた物の大きさの分布からオンラインで決め直す） |
| `*_h1` | 上に再挿入（`reinsert=hits:n=1/max_frac=0.5`）を足したもの |
| `d3_*` `d1_*` | 書き込み予算つき（`admit=budget:dwpd=3` / `1`）。再挿入も同じ予算から引く |
| `obj_d3` `obj_d1` | 基準: Region なしの理想の SSD（物単位 LRU）。同じ予算・同じ DRAM |
| `lru.csv` | 物単位 LRU（SSD と同容量）の先頭 N 曲線、初回アクセスのバイト（強制ミス）、K(N)（128 KiB piece 数） |

固定の条件: DRAM は LRU、`inclusion=cachelib`、`region-evict=lru`、`seal-on-repartition=inactive`、`chunk=1M`、`seed=1`。

### 指標の定義（すべて「先頭 N 要求」の値。要求バイトに対する %）

| 指標 | 定義 |
|---|---|
| BMR | `miss_bytes / req_bytes` |
| **効果** `eff` | `BMR(分類なし) − BMR(分類あり)` [pt]。**正 = 分類でミスが減った**。比べる相手は同じ条件（全受け入れ / 同じ予算、再挿入の有無）の分類なし |
| collateral | `BMR(none) − BMR(物単位 LRU)` [pt]。Region 単位で消すことによる余分なミス。予算つきは `BMR(d3_none) − BMR(obj_d3)` |
| DWPD（全受入） | `flash_region_write_bytes / SSD ÷ 経過日数` |
| conv（収束） | 以後ずっと `|eff(N') − eff(全量)| ≤ tol` となる最小の N ÷ 全要求数（tol = 0.1 pt と 0.02 pt）。**小さいほど短い部分トレースで傾向を再現できる** |
| 符号一致 | 先頭 1 / 3 / 10 / 31 % の時点で、効果の符号が全量と一致するか |
| 特徴 | `og_trace_stats` の 10 窓の平均: byte/request 重みの Var(log2 s)、サイズ・頻度の寄与、Spearman ρ(size, λ)、Lorenz の偏り、サイズ変化率、その他 |

### 集計の方法（aggregate）

1. トレースごとに 1 つの JSON（`work/results/<name>.<変種>.json`）。主結果は `drop` → `delete` → `fix` → `asis` の順に最初にあるもの（`aggregate --variant NAME` で指定可）。
2. `traces.tsv` の `category` / `source`（と `--by` で指定した列）でグループ化。各指標について **トレース数・平均・中央値・最小・最大・効果が正のトレース数・負のトレース数**（±0.02 pt を超えるものを数える）を `by_<列>.csv` に出す。`report.html` には、文脈ごと（全受け入れ / 再挿入 / 予算 3・1 DWPD）の表にして載せる。
3. 図: トレースごとの効果（点の色 = category）、先頭 N 要求での効果の推移（全量に近づく様子）。
4. 特徴と効果のトレース間 Spearman 順位相関（トレースが 4 本以上のとき）。どの特徴が効果の大小を説明するかの手がかり。
5. 埋め値の扱いによる違い（変種ごとの BMR・collateral・効果の表）。さらに、効果を「SSD への書き込み量 [TB]」で測った図と、「先頭 W TB 書き込んだ時点の効果」と全量の効果の比較表。

**注意: 平均は、トレース数が少ないうちは目安。** 同じ出典のトレースが 1 本ずつしか無いグループは、平均 = その 1 本の値。群の傾向を言うには、最小〜最大と正/負の数も見る。

## 6. 出力ファイル

| ファイル | 内容 |
|---|---|
| `work/results/report.html` | 全部まとめた図表（ブラウザで開く） |
| `work/results/per_trace.csv` | 1 トレース 1 行。特徴・LRU・Region・効果（`eff_*`）・収束（`conv01_*`）・符号一致（`sign31_*`）…（表計算で自由に集計できる） |
| `work/results/by_category.csv` ほか | グループごとの統計（長い形式: group, metric, n, mean, median, min, max, n_positive, n_negative, traces） |
| `work/results/<name>.<変種>.json` | 1 本の詳細（先頭 N の曲線を含む） |
| `work/results/synth_summary.csv` | 合成負荷の理論と実測（OHR/BHR） |
| `work/synth/<負荷>.json` | 1 負荷の理論（`model`）と実測（`sim`）。11-17 から Region LRU の静的分割も入る：`rpartO[1]` / `rpartB[1]`（クラスごとの部分トレースを RegionFlash で、容量はモデルの OHR 最適 / BMR 最適な配分）、`rfloorB[1]`（同じ BMR 最適な配分を全体のトレースで下限として与える）、`rautoB[1]`（下限の山登り、目的 BMR だけ）。モデルが 1 容量あたり Region LRU を 40 回ほど解くので、1 負荷 × 4 容量で 20〜30 分かかる。前の版の `<負荷>.json` があると飛ばすので、消してから `./run.sh synth` |
| `work/out/<name>/<変種>/<id>/` | cachesim の生の出力（`P.json` `P.interval.csv` `P.class.csv` `P.sizebin.csv`） |
| `work/results/figs/` | **スライド用の図と説明**（§6.1） |

### 6.1 スライド用の図（`work/results/figs/`）

`./run.sh aggregate` を実行すると、結果から図がすべて作り直されます。トレースの結果が無く合成トレースの結果だけでも、その分の図は出ます。

| ファイル | 中身 |
|---|---|
| `NN_名前.svg` | 図。1600×900（16:9）の白地で、文字はスライドで読める大きさ。PowerPoint に挿入しても拡大で劣化しない |
| `NN_名前.png` | 同じ図の画像(変換器があるとき) |
| `index.html` | 全部の図を並べ、図ごとに **要点**(結果の数値から自動で書いた一言)・**読み方**・**定義** を付けたページ |
| `captions.md` | 説明だけのテキスト(スライドのノートや本文に貼る用) |
| `slides.pptx` | 1 図 1 枚のスライド。左に図(SVG、古い PowerPoint では PNG)、右に要点と読み方。PNG ができたときだけ作る |

出る図は次のとおりです。トレースが 1 本でも作れる図は、その 1 本で作ります。

| 区分 | 図 | 何が分かるか |
|---|---|---|
| 妥当性検証 | BMR の内訳 | 初回アクセス・容量不足・Region 化の余分(collateral)に分けたミス。分類で取り戻せる上限の目安 |
| | 分類の効果(条件ごとに 1 枚) | トレース × 分類の効果 [pt]。全受け入れ / 予算 3・1 DWPD × 再挿入の有無 |
| | 条件 × 分類の表(トレースごと) | どの条件でどの分類が効くかを色で一覧 |
| | 先頭 N 要求での効果の推移(分類ごと) | 部分トレースで全量の値に落ち着くか |
| | 書いた量で測った効果の推移(分類ごと) | 先頭 W TB の時点(縦の破線)で全量と同じ向きか |
| | 先頭 W TB 時点と全量(トレースごと、全構成) | 印ごとの効果と全量をつないだ線。予算つきも含む |
| | 符号が定まる書き込み量(トレースごと) | 何 TB 書けば効果の向きが以後変わらないか。1.5 TB の線との比較 |
| | 収束に要る割合(トレースごと) | ±0.1 / ±0.02 pt に収まる先頭の割合 |
| | collateral の推移 | Region 化の損を短いトレースで測れるか |
| | SSD 書き込み量(トレースごと) | 受け入れと再挿入の内訳、構成ごとの寿命の負担 |
| | 埋め値の扱いの比較(変種が 2 つ以上のとき) | drop / delete で結論が変わらないか |
| | グループごとの傾向(`--by` の列ごと) | 種類・出典ごとの効果の平均と最小〜最大 |
| | 特徴と効果の順位相関(4 本以上のとき) | どんなトレースで分類が効くか |
| 合成トレース | 理論と実測 | Che 近似のモデルが実測にどこまで合うか(OHR・BHR) |
| | 負荷ごとの容量とヒット率 | 方式ごとの OHR・BHR の実測(実線)と理論(破線) |
| | サイズ分類の利得 | OHR は上がるが BHR は下がる、を実測と理論で |
| | 240 GiB 想定:BMR(`SYNTH_SCALED=1`) | 分類・再挿入の BMR と物単位 LRU、モデルの予測 |
| | 240 GiB 想定:BMR の時間変化 | 定常に入っているか |
| | 240 GiB 想定:書き込みの内訳 | 受け入れと再挿入、大物の容量の下限ごと |

## 6.2 自動推定の実験（tune）

分類閾値・分類ごとの容量の配分・再挿入の閾値を**実行中に自動で決めたとき**に、手で選んだ最良（固定の格子）にどこまで届くか、
目標（基準「分類なし＋再挿入 n=1」に対して OHR↑・BMR↓・SSD 書き込み↓）を満たすかを測る。計画と背景は `../docs/auto_tuning_plan.html`。

```bash
./run.sh tune-synth          # 合成の 95:5・3:1（答えが分かっている負荷）。6,000 万要求 × 2 負荷 × 25 構成、32 コアで数分
./run.sh tune meta_nha       # traces.tsv のトレース（名前を省くと全部）。validity と同じ SSD・DRAM・Region
./run.sh tune-figs           # 図だけ作り直す（TUNE_W=1 ./run.sh tune-figs で目的の重みを変える）
```

| 自動で決めるもの | RegionFlash の指定 | 決め方 |
|---|---|---|
| 分類閾値（1〜3 個） | `hint=split_threshold` / `st_hr_refine:k_max=3` / `st_recursive:depth=2` | 書いた物の大きさの分布から SplitThreshold が決め直す（既存） |
| 分類閾値（目的から） | `hint=mini:M=64/w=0.1` | 物の 1/M を τ の候補（各オクターブと分割なし）ごとの影の Region LRU に流し、目的が最大の τ を採る（縮小シミュレーション）。Meta で最良の固定 τ に −0.19pt（SplitThreshold は −0.96pt）。途中で τ を切り替えると効果が出きらない場合がある（w01）。影の分だけメモリが増える |
| 熱/冷の分類（名簿） | `hint=ghost:frac=0.3`（大きさとも分けるなら `/tau=<τ>`） | SSD から追い出した物の ID を名簿に残し（長さ = frac × SSD にいる物の数）、ミスして書く物が名簿に載っていれば別の分類（実績あり）の Region へ。同じ設定の全量トレースで、分類なしより BMR が Meta −0.23pt・Tencent −1.8pt・w01 −0.4pt、書き込み −0.6〜−4.7%。最良の固定 τ と重ねると Tencent −2.3pt・w01 −4.0pt。名簿の分だけメモリが増える（w01 で frac=1 なら約 4,100 万件・約 1.6 GB）。Tencent はブロックトレース |
| 容量の配分（本命） | `region-evict=lru,region-params=auto=1/w=<重み>` | 大物の下限 ff を、N 回の追い出しごとに測った目的 BHR + w·OHR が上がる向きへ 0.02 ずつ動かす（山登り）。出発点は大物が受け入れたバイトの割合 |
| 容量の配分（比較用） | `region-evict=mv,region-params=w=<重み>` | 分類ごとの LRU 末尾のヒット密度が最も低い分類から追い出す。1 Region に物が多い分類では末尾が冷えず限界値を誤る（合成 95:5 で −2.6pt） |
| 再挿入の閾値 | `reinsert=hits:n=auto/target=0.5/eps=0.02` | 分類ごと・ヒット回数 h ごとに「書き直した物が次の滞在でまた当たった割合」を数え、target を越える最小の h を n にする |

構成は `config.sh` の `TUNE_JOBS`（`id|指定` の並び）。出力は `work/tune/<名前>/<id>/P.json`（n=auto は `P.reauto.json` も）、
1 トレースの要約 `work/tune/<名前>.json`、図 `work/tune/figs/`（交換曲線・書き込みの内訳・n=auto の学習・目標の達成・自動と最良の差・分類の数）。
見本（400 万要求の試運転。数値は意味を持たない）: `../docs/kit_tune_figs_sample/`。

**集計**：`./run.sh aggregate` が tune の結果も出典・種類ごとにまとめる（`py/tune_aggregate.py`。validity の結果が無くても動く）。基準 = 分類なし＋再挿入 n=1（`none_h1`）。差はどれも正 = 良くなった（OHR の向上 pt、BMR の減少 pt、SSD 書き込みの減少 %）。

| 出力（`work/results/`） | 中身 |
|---|---|
| `tune_per_trace.csv` | トレース × 構成ごとに 1 行：OHR・BMR・書き込み、基準との差、`goal`（3 つとも改善）、`gap`（目的の、固定の下限の格子の最良との差）、n=auto が選んだ閾値 |
| `tune_by_<列>.csv` | 出典・種類（`--by` の列）× 構成 × 指標ごとに n・平均・中央値・最小・最大・目標を満たしたトレース数 |
| `tune_report.html`（`report.html` にも同じ節） | 上をまとめた表 |

理論的根拠（再挿入の閾値・容量の配分）: `../docs/auto_tuning_theory.md`（同じ内容の図つき版 `.html`）。

注意:
- **キットを新しくしたとき**：`setup` は当てたパッチの印（`work/src/.rf_patch`）を `regionflash.patch` と照合し、違えば土台に戻して当て直し、cachesim を作り直す。validity / synth / tune / tune-synth も始める前に同じ照合をする。以前の版（印の無い setup）で作った `work/` の上に新しいキットを置いた場合も自動で作り直す。古い cachesim のままだと、新しい構成（`n=auto`・`auto=1`・`mv`・`mini`）は「reinsert does not have parameter target」などで FAIL する。`LCS_SRC` や `CACHESIM` を指定しているときは自動では作り直さない。
- FAIL した構成（`P.json` が無いもの）は、同じコマンドをもう一度実行すればそれだけやり直す。
- tune-synth の同時実行数は NCPU と空きメモリ ÷ 2.5 GB（1 本の実測 1.6〜1.9 GB）の小さい方。`TUNE_P` で固定もできる。
- `./run.sh all` には tune を含めない（時間がかかるので別に実行する）。
- 同時実行数は validity と同じメモリの見積り（§4.1）で決まるが、`hint=mini` の影の分（物の約 (候補数)/M 倍の表）は見積りに入っていない。物の多いトレース（例 w01：1 本 約 5.8 GB）では `TUNE_P=3` のように小さく固定する。WSL でメモリを使い切ると WSL ごと落ちる。
- 構成は 31 個（`TUNE_JOBS`）。全量の Meta nha で 1 本 70〜90 秒、P=4 で 10 分ほど。

分類は多くても 4 つ（閾値 3 個）まで: 分類ごとに書き込み中の Region（FDP なら RU）が要り、書き込み先が散るほど各 Region の書き込み速度が落ちて開いたままの時間が延び、空きも増えるため。

## 6.4 Zipf の傾きと分類の利得（che）

「Zipf の傾きが小さいトレースでは分類で改善し、大きいトレースでは混ぜた方が良い」という観察を、モデル（`docs/Che-sim/region_model_appendix.html` §N）の筋で説明できるかを、トレースごとに 4 項目で確かめる。

```bash
./run.sh che                 # traces.tsv の全部（tune を先に回してあれば、その none と split_threshold の τ を使う）
./run.sh che wiki_cdn        # 一部だけ
./run.sh che-report          # 表と図だけ作り直す
CHE_SIM=0 ./run.sh che       # RegionFlash を回さず、Che の予測だけ（数十秒/本）
```

| 項目 | 何を出すか | どこから |
|---|---|---|
| (1) 上限 | 物単位 LRU のミス率。分類で取り返せる OHR はこれより多くない | Che の予測（`og_che`）と、cachesim の素の `lru`（SSD と同じ容量、DRAM なし） |
| (2) 静的分割の予測 | 分けたときの OHR/BHR、OHR・BHR・J 最適な容量の取り分、Zipf の傾き（全体・分類ごと） | `og_che`：物ごとの（大きさ, 要求回数）から λ = 回数 ÷ 全要求として Che 近似（IRM）で解く。Region の効果は入らない |
| (3) 容量の取り分 | split_threshold の Region LRU で、分類ごとの生きている Region の割合の時系列と後半の平均 | RegionFlash の `P.interval.class.csv`（封じた − 追い出した） |
| (4) 固定された Region | 終了時に残っている Region のうち、トレースの先頭 10%・50% に封じたものの割合。追い出した Region の寿命（トレース長に対する分位） | RegionFlash を `RF_EVLOG`・`RF_ENDDUMP` つきで回す |

出力は `work/che/`：`report.html`（図 6 枚と表）、`che_summary.csv`（1 行 1 トレース）、`<name>.json`（全部）、`<name>/`（`che.json` と RegionFlash の生の出力）。

**report.html の読み方**（ページの冒頭にも書いてある）。α が大きいトレースほど、次の 3 つが揃えば「分類が逆効果」をモデルの筋で説明できる。

1. 上限（素の LRU のミス率）と、静的分割の予測利得が小さい。
2. Region LRU の自然な小物の取り分が、最適な取り分（対角線）より上にずれる。
3. 先頭 10% に封じて残った Region が増える。

揃わなければ、独立参照モデルの外（人気の時間変化など）を疑う。「Che − 実測の OHR」が大きく負のトレースは時間局所性が強く、Che の数字（予測利得・最適な取り分）は目安にとどまる。

**設定**（`config.sh`）：

- `CHE_TAUS`：Che で試す分類の閾値。既定 `16K 64K 256K 1M 4M`。split_threshold の τ は自動で足す。
- `CHE_W`：目的 J = BHR + w·OHR の w。
- `CHE_SIM`：RegionFlash を回すかどうか。

**注意**

- **SSD 容量**：validity・tune と同じ（`traces.tsv` の `ssd` 列）。Che と素の LRU は SSD の容量だけで、DRAM は入れていない。
- **Region の数**：Region が少ないと（例: 小さいトレースで R = 626.5 MiB だと十数個）、取り分の時系列と固定された Region の割合が粗くなる。100 個以上が目安。`REGION_BYTES= ./run.sh che` で SSD ÷ `REGION_N` にできる。
- **τ の丸め**：Che の閾値は 1/4 オクターブの格子に丸める（出力の `taus_eff`）。2 の冪は丸めない。
- **傾き α の求め方**：順位 r と要求回数 c の両対数の最小二乗。r = 10 から「2 回以上要求された物」の最後の順位までを、1 桁 20 点に間引いて当てる。別の方法で測った傾きとは値がずれうる。
- **増幅（amp_key・amp_size）つきのトレース**：Che は容量を K·F で割って同じ答えを出す。素の LRU は回さない。
- **og_che のメモリ**：物 1 個あたり約 50 B（表の倍々拡張の山）。w01（物 1.2 億個）で 6.4 GB、18 秒。

## 6.3 論文用の比較（paper）：Region を CacheLib の上限と RU で比べる

FDP SSD の RU（GC の単位、626.5 MiB）は、CacheLib の Region の上限（256 MiB）より大きい。Region を RU に合わせると DLWA = 1 になるが、Region が大きいぶん道連れと書き直し（アプリ層の GC）が増える。書き込み時のサイズ分類がその増加を抑えるかを、6 構成（`config.sh` の `PAPER_JOBS`）で測る。DLWA は実機と CacheLib で測るので、ここでは測らない。

| id | Region | 方式 |
|---|---|---|
| `r256_base`（**基準**） | 256 MiB（`PAPER_R256`） | 分類なし、再挿入 n=1（CacheLib 相当） |
| `r256_ff90_h3` / `r256_auto` | 256 MiB | サイズ分類＋大物の下限 0.9＋n=3（手動） / 下限の山登り＋n=auto（自動）。山登りの目的は J = BHR + 0.1·OHR（`region-params=auto=1/w=0.1`、BMR + 0.1·MR の最小化と同じ）。手動の 0.9 はコスト式から決めた値ではなく、合成負荷で見た値の決め打ち |
| `ru_base` | RU = `REGION_BYTES`（既定 656932864 = 626.5 MiB） | 分類なし、再挿入 n=1 |
| `ru_ff90_h3` / `ru_auto` | RU | 同上の手動 / 自動（目的も同じ J = BHR + 0.1·OHR） |
| `ru_none_auto` / `r256_none_auto`（切り分け） | RU / 256 MiB | 分類なしで再挿入の閾値の自動化（n=auto）だけ。`*_auto` との差が「分類＋下限」の効果、`*_base` との差が n=auto の効果 |
| `ru_auto_w0` / `r256_auto_w0`（11-17 追加） | RU / 256 MiB | `*_auto` と同じで、下限の山登りの目的を BMR だけにする（`w=0`。`*_auto` は BHR + 0.1·OHR）。Region 単位の静的分割のモデルでは BMR 最適な配分が大物に寄り、サイズ分類が混在より BMR で勝つ（`../docs/Che-sim/region_model_appendix.html` §O）。実トレースでも差が出るかを見る |

**11-17 に足した 2 構成だけ回す**：`./run.sh paper` は `P.json` のある構成を飛ばすので、そのまま実行すれば `*_auto_w0` だけが走り、図と表は全構成で作り直される。

**11-17 の修正（山登りの OHR の項）**：下限の山登り（`auto=1`）の目的 J の OHR の項が、SSD のヒットだけを数えていた（BHR の項は DRAM と SSD の両方）。DRAM と SSD のヒットの合計に直した（`evict/region_basic.c`・`region_policy.h`・`ssd/rf_region.c`）。`w=0`（`*_auto_w0`）の結果はビット単位で変わらない（確認済み）。`w>0` の `*_auto`（w=0.1）は、DRAM がある構成では前の版と別物になる。揃えて比べるなら、`work/paper/*/ru_auto` と `r256_auto` を消してから `./run.sh paper`（`setup` がパッチの違いを見つけて cachesim を作り直す）。tune の `*hc*` も同様。

**追加の比較（paper-ext）**：CacheLib（R=256 MiB、分類なし）の再挿入なし `r256_n0`・`r256_n2`・`r256_n3`（n=1 は基準 `r256_base`。`config.sh` の `PAPER_EXT_JOBS`）。「書き直しの閾値だけ上げた CacheLib」と比べても、分類の効果が残るかを見る。n=0 は書き込みの下限と、そのときの BMR の悪化を示す。

```bash
./run.sh paper-ext           # 足りない構成だけ回す（P.json がある構成は飛ばす）。結果・図・表は work/paper/ext/
./run.sh paper-figs --ext    # ext の図と表だけ作り直す
python3 py/paper_check.py work --ext   # ext の構成も並べて確かめる
```

`work/paper/ext/` には、**交換関係の図** `fig_tradeoff.svg`（全トレースを並べた 1 枚）と `fig_tradeoff_<trace>.svg`（1 面ずつ）も出す。横 = SSD 書き込み（CacheLib n=1 = 1、0 から）、縦 = ΔBMR [pt]（CacheLib n=1 との差、下が良い）。CacheLib の n=0〜3 を線でつなぎ（書き直しを増やすほど書き込みが増えて BMR が下がる交換関係）、提案（RU＋分類の自動・手動）と分類なしの n=auto を点で重ねる。点が線の左下なら、同じ BMR を少ない書き込みで出している。

また `work/paper/ext/` の図と表には、基準・n=0・n=3・分類なしの n=auto（`r256_none_auto`・`ru_none_auto`）・提案（`ru_ff90_h3`・`ru_auto`）を並べる（`work/paper/` の結果も読み込む。`work/paper/` の図と表は変えない）。

**頑健性の確認（paper-robust）**：結論が RegionFlash の作りや乱数に頼っていないかを見る（`PAPER_ROBUST_JOBS`、1 トレース 10 本、結果は `work/paper/robust/`）。背景は `../docs/regionflash_callgraph.html`（1 要求のコールグラフと、CacheLib との違いの一覧）。

| 条件 | 指定 | 確かめること |
|---|---|---|
| `_c0` | `chunk=0`（物を 1 Region の中に連続で置く） | 片を 1 つ失うと物全体のミスになる RegionFlash の扱いが、基準を不利にしていないか |
| `_c0m` | `chunk=0` ＋ 受け入れの上限を全構成で 256 MiB（`admit=sizemax`） | 同上を、R の違う構成どうしで公平に比べる。**`_c0` だけだと Region より大きい物は入らないので、R=RU の構成だけ 256〜626.5 MiB の物を受け入れ、BMR が下がって書き込みが増える**（分類の効果ではない）。R の違う比較は `_c0m` で見る |
| `_f1` | 再挿入の上限なし（`max_frac=1`、既定は追い出す Region あたり R×0.5） | 候補の多い n=1 の基準が上限で損をしていないか |
| `_s2`・`_s3` | 乱数の種 2・3 | 差がゆれより大きいか |

```bash
./run.sh paper-robust                       # 回して、paper_check.py --robust の表と、図・表（work/paper/robust/）を出す
./run.sh paper-figs --robust                # 図と表だけ作り直す（条件をまたいだ図と、条件ごとの一式の両方）
python3 py/paper_check.py work --robust     # 確認の表だけ出し直す
```

条件ごとの一式（`work/paper/robust/c0/`・`c0m/`・`f1/`・`s2/`・`s3/`）：`paper-figs` と同じ図と表（3 面まとめ・CacheLib 比・1 面ずつ・時間変化・`table_main`。CacheLib n=3 がある条件は交換関係の図も）を、その条件で回した構成だけで作る（`r256_base_c0` → `r256_base` のように読み替え）。`PAPER_ROBUST_JOBS` に `<構成名>_<条件>` の名前で足せば、その構成も加わる。

条件をまたいだ図（`work/paper/robust/` 直下）：`fig_robust.svg` = 提案（RU＋分類の自動）− CacheLib n=1、`fig_robust_n3.svg` = 提案 − CacheLib n=3（n=3 のある条件だけ）。どちらも左 = ΔBMR [pt]（0 = CacheLib、下が良い）、右 = 書き込みの比（CacheLib = 1、1 未満が良い）。横軸 = トレース（2 本以上なら平均も）、色 = 条件（既定・chunk=0・上限なし・種 2・種 3）。数値は `table_robust.csv` / `.md`。

どの条件でも「提案の ΔBMR ≤ 0（または CacheLib n=3 より小さい）」と「書き込みの比 < 1」の向きが既定と同じなら、結論は作りや乱数に頼っていない。

**書き込み量の印での結果（部分トレースで足りるか）**：`paper-figs` は各構成の `P.interval.csv` の累積の行から、CacheLib（`r256_base`）の累積 SSD 書き込みが W に達した時点（= 先頭の部分トレース）の結果を取り出し、全量と比べる。W は `traces.tsv` の `marks` 列（例 `1.5T,5T`）、空なら `WRITE_MARKS_TB`。

| 出力 | 中身 |
|---|---|
| `fig_marks_<trace>.svg` | 横 = CacheLib が SSD に書いた量（部分トレースの長さ、対数）、縦 = ΔBMR（CacheLib 比）と書き込みの比。赤い縦線 = 指定した W。線が右端（全量）の値に落ち着き、W の時点で全量と同じ側にあれば、その長さの部分トレースで結論が言える |
| `table_marks.md` / `.csv` | トレース × W（と全量）ごとに、先頭の割合（要求数）と各構成の「ΔBMR / 書き込みの比」。全量と差の向きが違うものに ✗ |

**増幅（CacheLib cachebench の ampFactor / ampSizeFactor）**：`traces.tsv` の `amp_key`（要求ごとに K 個の別キー。物の数・要求数・フットプリントが K 倍）と `amp_size`（物のサイズを F 倍）。RegionFlash の `amp-key=K` / `amp-size=F` として tune / paper に渡す（`RegionFlash_get` の入口で、サイズ変更の判定より前に適用。P.json の `config.amp_key` / `amp_size` に残る）。要求数・区間の長さ・温め期間は増幅した後の要求で数える。validity には効かない。

**結果の確かめ方**：`python3 py/paper_check.py work [トレース名..]`。構成ごとに、Region・chunk・分類の閾値が意図どおりか、全要求を回したか（`info.json` の要求数と一致）、書き込み = 受け入れ＋再挿入＋詰め物 が成り立つか、基準からの書き込みの差の内訳（受け入れ・再挿入・詰め物、TB）、受け入れの差が BMR の差 × 要求バイトと合うか、n=auto が選んだ閾値と下限 ff を一覧にする。

```bash
./run.sh paper               # traces.tsv の全部（名前を並べればそれだけ）。SSD・DRAM・chunk は validity と同じ。変種は埋め値を除いた主の変種
./run.sh paper-figs          # 図と表を全部作り直す：paper、結果があれば ext と robust（条件をまたいだ図＋条件ごとの一式）も。PAPER_LANG=en で英語
```

出力（`work/paper/`）は**図 1 枚（1 面ずつの版もある）と表 1 つ**（査読で見る量を抑える）。図は値そのものを並べ、基準（濃い灰）も含む。

| ファイル | 中身 |
|---|---|
| `fig_main.svg` | 値そのものの 3 面まとめ：BMR [%]（低いほど良い）・SSD 書き込み量 [TB]（低いほど良い。棒を **取得（ミスして受け入れたバイト、濃い）＋ 再書き込み（再挿入、薄い）** に分けて積む。詰め物はごく小さいので表と CSV のみ）・OHR [%]（参考）。**縦軸は 0 から**。横軸 = トレース（BMR・OHR は 2 本以上なら最後に平均） |
| `fig_rel.svg` | CacheLib（R=256 MiB、基準）比の 3 面まとめ：ΔBMR [pt]（構成 − CacheLib、負が良い）・SSD 書き込み（CacheLib の合計 = 1 に正規化、1 未満が良い、1.0 に線。同じく取得＋再書き込みに分けて積む）・ΔOHR [pt]（参考）。基準の棒は出さない |
| `fig_bmr` / `fig_write` / `fig_ohr` / `fig_dbmr` / `fig_nwrite` / `fig_dohr`（.svg） | 上の 2 枚の 1 面ずつ（1 つだけ載せるとき用）。`.png` は rsvg-convert などがあれば |
| `table_main.tex` / `.md` / `.csv` | トレースごとに、基準の BMR・書き込み [TB]・OHR・アプリ層の書き込み増幅（書いたバイト ÷ 受け入れたバイト）と、各構成の「ΔBMR [pt]（負が良い）/ 書き込み（CacheLib = 1）/ ΔOHR [pt]」。基準の書き込みは「合計（取得＋再書き込み）」で示す。CSV は全構成の全指標（`admit_TB`・`reinsert_TB`・`pad_TB`、`write_norm` = 正規化した書き込み） |
| `fig_ops_<trace>.svg`（補足） | 時間変化。横軸 = 処理した要求数。BMR・OHR は区間ごと（約 `PAPER_BINS`=100 区間にまとめる）、取得と再書き込みはそれぞれ累積 TB（4 面）。キャッシュが埋まるまでの期間と、その後の定常の差、再書き込みがいつから増えるかが見える |
| `fig_wr_<trace>.svg`（補足） | 横軸 = その構成の累積 SSD 書き込み量 [TB]。BMR・OHR はそこまでの累積。同じ量を書いた時点（SSD の寿命の消費が同じ時点）での比較 |
| `index.html` | 図と表と要点、補足の図 |

時間変化の図は各構成の `P.interval.csv`（区間の刻み = `INTERVAL`、既定 1 万要求）から作る。`./run.sh paper-figs` で作り直せる。

`REGION_BYTES` の既定を RU（656932864）にしたので、validity と tune も Region = RU で走る。以前の「SSD ÷ 392」に戻すには `REGION_BYTES= ./run.sh …`（空にする）。`CHUNK` の既定は 64 KiB（piece-wise 書き込みの幅。validity・tune・paper 共通）。

## 7. 制約と注意

- `/bin/bash^M: bad interpreter` が出たら、ファイルの改行が Windows 形式（CRLF）になっている。キットのフォルダで次を実行して LF に戻す（中身は変わらない）: `sed -i 's/\r$//' run.sh config.sh traces.tsv synth_workloads.txt py/*.py src/*.c`

- 物の数の上限: `cdn_prefix_check` と `og_xform` は物が約 6,700 万個まで（超えると `NN overflow`。`src/*.c` を `-DNN=` 付きでコンパイルし直す）。`og_info` は無制限。
- 先頭 N 要求の値は **LRU 系の構成だけ**に成り立つ。未来を使う分類（`oracle_*` など）を `HINTS` に足すと、先頭 N の曲線は意味を持たない。
- 書き込み予算（DWPD）はトレースの時刻（`clock_time` の秒）が基準。経過が 1 時間未満のトレースでは予算つき構成を走らせない。
- 時刻が実時間でないトレース（要求番号が時刻の合成トレースなど）では DWPD・予算の意味が変わる。
- 変換したトレースと fix 変換したトレースは `work/traces/` に残る（容量を食う。済んだら消してよい。消すと次回は作り直す）。
- 実行時間の実測(WSL): nha(9,600 万要求)の全量で、1 本あたり none 56 秒、分類あり 70〜100 秒。w01(2.2 億要求、物が小さい)では 4〜7 分。試運転（`NREQ=2000000`）では 4 トレース × 20 構成が数分（`st` を足す前の測定）。

## 8. 確かめたこと（作成時、WSL）

手元の 4 トレース（Block 3 本と CDN 1 本、`NREQ=2000000`）で `setup → validity → synth → aggregate` を通し、`report.html` と CSV が出ることを確認した。meta は埋め値（size = 2^32−1）を検出して fix 変換し、フットプリントが 19.97 TB（以前の測定の 20.0 TB と一致）になった。CSV を `traceConv` で変換する経路、`SYNTH_SCALED=1` の経路も 200 万要求で通した。**全長のトレースでの所要時間と結果の一致は未確認。**

スライド用の図(2026-11-11)は、次の結果で 45 枚が出ることを確認しました。

- meta nha の全量(drop / delete)
- msr と tencent の先頭 500 万要求
- 合成 6 負荷(200 万要求)
- 縮小版(400 万要求)

| 確認したこと | 結果 |
|---|---|
| PNG への変換(LibreOffice。2026-11-12 以降は使わない：変換が止まることがあった) | 1 回約 3 秒 |
| `slides.pptx` | LibreOffice で PDF に変換でき、45 枚がそろった |
| 合成トレースの結果だけのとき | 合成トレースの図だけが出た |

見本は `../docs/kit_figs_sample/` にあります。合成トレースは短い長さで回した見本なので、数値は本番の結果ではありません。PowerPoint 本体で開いて確かめたことは、まだありません。
