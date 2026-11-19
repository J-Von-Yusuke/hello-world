# 実験キット：RegionFlash の実験を別 PC で回す

改造した libCacheSim（RegionFlash）で、手元で確かめた実験を別の PC のトレースで再現し、**出典・種類ごとに傾向をまとめる**ためのキット。
RegionFlash の指定（`hint=`・`region-params=` など）の意味は [`params.html`](params.html) にまとめてある。

## 目次

1. [何ができるか](#1-何ができるか)
2. [必要なもの](#2-必要なもの)
3. [最短の手順](#3-最短の手順)
4. [トレースを書く：`traces.tsv`](#4-トレースを書くtracestsv)
5. [設定：`config.sh`](#5-設定configsh)
6. [実験ごとの説明](#6-実験ごとの説明)（validity・synth・tune・che・paper）
7. [制約と注意](#7-制約と注意)
8. [確かめたこと](#8-確かめたこと)
9. [付録：変更の履歴](#付録変更の履歴)

## 1. 何ができるか

| コマンド | 何をするか | 入力 | 出力 |
|---|---|---|---|
| `paper-final` | **論文の本番用の比較**：CacheLib と提案（`ru_auto`）と、比較に向いた方式だけ（§6.5.2） | `traces.tsv` | `work/paper/final/` |
| `paper` / `paper-ext` / `paper-robust` | 論文用の比較の全部・CacheLib の書き直しの閾値違い・頑健性（§6.5） | `traces.tsv` | `work/paper/…` |
| `validity` | トレース 1 本ごとに、分類なし / ありなどを全量 1 回回し、**先頭 N 要求で測った値**を全 N について出す（部分トレースで傾向が再現できるか）（§6.1） | `traces.tsv` | `work/out/`・`work/results/` |
| `synth` | 少数サイズの合成負荷で、理論（Che 型モデル）と実測（cachesim）を並べる（§6.2） | `synth_workloads.txt` | `work/synth/` |
| `tune` / `tune-synth` | 分類の閾値・容量の配分・書き直しの閾値を自動で決めたとき、手で選んだ最良にどこまで届くか（§6.3） | `traces.tsv` / 合成 | `work/tune/` |
| `che` | Zipf の傾きと分類の利得を、モデルの筋で説明できるか（§6.4） | `traces.tsv` | `work/che/` |
| `aggregate` | validity・synth・tune の結果を出典・種類ごとに集計し、スライド用の図を作る | `work/` の結果 | `work/results/` |

```
traces.tsv ─┬→ run.sh validity ──→ work/out/…, work/results/<name>.<変種>.json ─┐
config.sh  ─┤→ run.sh tune ──────→ work/tune/                                    ├→ run.sh aggregate → work/results/report.html, *.csv, figs/
            ├→ run.sh che ───────→ work/che/report.html                           │
            └→ run.sh paper* ────→ work/paper/{., ext, robust, final}/（図と表）    │
synth_workloads.txt → run.sh synth → work/synth/*.json ───────────────────────────┘
```

コマンドの一覧は `./run.sh`（引数なし）で出る。「準備」（setup・check・methods）・「実験」・「図と表だけ作り直す」（aggregate・*-figs・che-report）の 3 群。
どの実験も、済んだ run（`P.json` があるもの）は回し直さない。途中で止めても同じコマンドで続きから回る。消せばやり直す。

## 2. 必要なもの

- Linux（Windows なら WSL2 の Ubuntu）。`gcc cmake make git python3 libzstd-dev libglib2.0-dev`。Python は標準ライブラリだけで、3.10 以上。
- スライド用の PNG と `slides.pptx` も欲しければ、SVG を PNG にする道具を 1 つと日本語フォント：`sudo apt install librsvg2-bin fonts-noto-cjk`（`rsvg-convert` の代わりに `cairosvg` / `inkscape` でもよい。LibreOffice は変換が止まることがあったので使わない）。無くても SVG と説明は出る。
- メモリ：同時に回す本数 `P` は既定 `auto` で、空きメモリとトレースの物の数から自動で決まる（§5.3）。
- ディスク：`work/` に、変換したトレースと出力（トレース 1 本あたり数百 MB〜数 GB）。

## 3. 最短の手順

```bash
cd experiment_kit
chmod +x run.sh                  # Windows から zip で持ってきた場合
./run.sh setup                   # 上流 libCacheSim を clone → regionflash.patch を当てて cachesim をビルド、C ツールをコンパイル
$EDITOR traces.tsv               # ★ トレースのパスを書く（§4）
$EDITOR config.sh                # SSD 容量や回す方式を必要なら直す（§5）
./run.sh check                   # パスが読めるか、名前が重複していないか
NREQ=2000000 ./run.sh paper-final   # 試運転：各トレースの先頭 200 万要求だけ。結果の形を確かめる
rm -rf work/paper                # 試運転の結果を消す
./run.sh paper-final             # 論文の本番用の比較 → work/paper/final/index.html
./run.sh validity                # 部分トレースの妥当性（時間がかかる）
./run.sh synth                   # 合成負荷
./run.sh aggregate               # 集計 → work/results/report.html
```

一部のトレースだけ：`./run.sh paper-final meta_nha wiki_cdn`。`setup → validity → synth → aggregate` をまとめて：`./run.sh all`（tune・che・paper は含まない）。

### 3.1 libCacheSim の用意を変えるとき

- **clone 済みの libCacheSim を使う**：`config.sh` の `LCS_SRC` にそのパスを書く（clone を省く）。未パッチなら `setup` がそこへ `regionflash.patch` を当てる（当たるかを `git apply --check` で先に確かめ、当たらなければ何も変えずに止まる）。パッチ済みならそのまま使う。
- **パッチの土台は `93529a1`**。上流の最新（2026-11-09 の `4e3521a`）には当たらない。手元の clone が新しいときは、どちらかで土台に合わせる：

  ```bash
  git -C <clone> checkout 93529a1                 # その clone を土台に戻す
  git -C <clone> worktree add ../lcs-rf 93529a1   # 元の作業を残したまま別フォルダに土台を出し、LCS_SRC=../lcs-rf にする
  ```

- **作成済みの cachesim を使う**：`CACHESIM` にそのパスを書けばビルドも省ける。
- **キットを新しくしたとき**：`setup` は当てたパッチの印（`work/src/.rf_patch`）を `regionflash.patch` と照合し、違えば土台に戻して当て直し、cachesim を作り直す。各実験も始める前に同じ照合をする（印の無い古い `work/` でも自動で作り直す）。古い cachesim のままだと新しい構成は「reinsert does not have parameter target」などで FAIL する。`LCS_SRC` や `CACHESIM` を指定しているときは自動では作り直さない。

## 4. トレースを書く：`traces.tsv`

タブ区切り。1 行 1 トレース。`#` で始まる行は無視。列は 1 行目の見出しの名前で読む（順番は自由。無い列は空）。

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
| `amp_key` / `amp_size` | CacheLib cachebench の ampFactor（要求ごとに K 個の別キー）/ ampSizeFactor（サイズを F 倍）。tune・paper に効く（§6.5.5） | `4` / `2` |
| `marks` | paper の「書き込み量の印」の W（§6.5.5）。空 = `WRITE_MARKS_TB` | `1.5T,5T` |

- **`oracleGeneral.zst` 以外の形式は自動変換する**（`traceConv` → zstd。`work/traces/` に保存）。csv の場合は `params` が必須。
- 列は足せる（例：`region`）。`./run.sh aggregate --by category,source,region` で、その列でもまとめる。
- `category` と `source` は自由な文字列。同じ種類に複数の出典を入れれば、種類ごと・出典ごと・その組ごとの 3 通りで集計される（既定は `category`, `source`, `category/source`）。

## 5. 設定：`config.sh`

bash のファイル。どの項目も環境変数で上書きできる（例 `P=2 ./run.sh validity`）。

### 5.1 主な項目

| 項目 | 既定 | 意味 |
|---|---|---|
| `SSD_DEFAULT` | `30%` | SSD 容量。`240G` なら固定、`30%` ならトレースのフットプリント（物の最初のサイズの和）に対する割合 |
| `DRAM_DIV` | `30` | DRAM = SSD ÷ 30（240 GiB の SSD に 8 GiB） |
| `REGION_BYTES` | `656932864`（626.5 MiB） | Region の大きさ。既定 = 手元の FDP SSD の RU（Region = RU で DLWA = 1）。空にすると SSD ÷ `REGION_N` |
| `REGION_N` | `392` | `REGION_BYTES` が空のときの Region の数 |
| `CHUNK` | `64K` | 大きい物を片に分けて Region をまたいで書く幅（validity・tune・paper 共通） |
| `HINTS` | none / kf4 / sf8 / st | validity の分類方法。`id:hint指定` を足せば、その分類の効果も集計される |
| `BUDGET_DWPD` | `3 1` | validity の書き込み予算つき構成（1 日あたりの SSD 全書き換え回数）。空なら走らせない |
| `SAT_VALUE` / `SAT_VARIANTS` / `SAT` | `4294967295` / `drop delete` / `auto` | サイズ `2^32−1`（= −1 を uint32 で書いたもの。GET 以外の操作の印と見る）の要求の扱い。`auto` = その要求があるトレースでは `SAT_VARIANTS` を走らせる（先頭が主結果。tune・che・paper は先頭だけ）。`drop` = 無かったことにする、`delete` = その物を消す操作（要求に数えない）、`fix` = その物の他のサイズに置換（感度用）、`asis` = そのまま |
| `WRITE_MARKS_TB` | `1.5` | 「先頭から SSD に W TB 書いた時点」で効果を測る W（TB = 10^12 B） |
| `INTERVAL` | `10000` | 先頭 N 曲線・時間変化の刻み（要求数）。1000 万要求未満のトレースは `1000` に |
| `NREQ` | 空 | 数字を入れると各 run を先頭 NREQ 要求で打ち切る（試運転） |
| `P` | `auto` | 同時に走らせる cachesim の数。`auto` = §5.3 の見積りで自動。数字なら固定 |
| `NCPU` | CPU の総コア数 − 1 | 並列に使うプロセス数の上限（`P=auto`・tune-synth・`SYNTH_P`・`make -j`） |
| `MEM_FRAC` | `0.8` | `auto` で使ってよい空きメモリ（`MemAvailable`）の割合 |
| `SYNTH_N` / `SYNTH_SCALED` | 2000 万 / `0` | 合成トレースの長さ / 縮小 SSD 想定の重い負荷（2 億要求 × 多数、数時間）を走らせるか |
| `PAPER_*_METHODS` / `TUNE_METHODS` | §5.2 | paper 系・tune で回す方式 |

### 5.2 回す方式を選ぶ

**paper 系**：方式はすべて `PAPER_CATALOG` に 1 行ずつ（`"id|RegionFlash の指定|図の名前"`、名前は省略可）置いてあり、組ごとに id を並べて選ぶ。並べた順が図と表の順になる。

| 組 | 変数 | コマンド | 既定 |
|---|---|---|---|
| 本番用 | `PAPER_FINAL_METHODS` | `paper-final` | 10 方式（§6.5.2） |
| 全部 | `PAPER_METHODS` | `paper` | 検討してきた 18 方式（§6.5.1） |
| CacheLib の閾値違い | `PAPER_EXT_METHODS` | `paper-ext` | `r256_n0 r256_n2 r256_n3` |
| 頑健性 | `PAPER_ROBUST_METHODS` | `paper-robust` | 18 方式（§6.5.4） |

```bash
./run.sh methods                                               # カタログの全部と、組ごとにどれが選ばれているか
PAPER_METHODS="r256_base ru_auto ru_adm_auto" ./run.sh paper   # その場で選ぶ（config.sh を書き換えてもよい）
```

自分の方式を足すには、`config.sh` の末尾などに 1 行足して、その id を組に並べる：

```bash
PAPER_CATALOG+=("ru_fifo|hint=none,region-evict=fifo,reinsert=hits:n=1|R=RU＋Region FIFO")
PAPER_METHODS="$PAPER_METHODS ru_fifo"
```

- 指定の書き方は validity・tune と同じ（`params.html`）。SSD・DRAM・chunk はトレースごとに validity と同じ値が入り、`region-size` を書かなければ R = RU（`REGION_BYTES`）。
- 基準 `r256_base` は差の基準なので、paper・paper-final では選んでいなくても必ず回す。`all` と書くとカタログの全部（頑健性用の `_c0` などで終わる id は除く）。
- 選択から外した方式は、結果が残っていても図と表には出ない。実際に回した id・名前・指定は各出力先の `methods.tsv` に残る。
- カタログに無い id を選ぶと、何も回さずに止まる。
- 以前の指定も使える：`PAPER_GATE=1` で `ru_gate` を足す、`ROBUST_W0=0` で `ru_auto_w0_*` を外す。

**tune**：構成は `TUNE_JOBS`（`id|指定` の並び）。回すものを `TUNE_METHODS` で選ぶ（既定 `all`。基準 `none_h1` は必ず入る。例 `TUNE_METHODS="none_auto st_hc0.1_auto" ./run.sh tune meta_nha`）。

**validity**：`HINTS`・`WITH_REINSERT`・`BUDGET_DWPD`。**synth**：`synth_workloads.txt` の行。

### 5.3 メモリと同時実行数（`P=auto`）

cachesim 1 本のピーク RSS は、次の 2 つの物の数でほぼ決まる。u = トレースの物の数（`og_info` の `uniq`）、x = min(1, (SSD+DRAM) ÷ フットプリント) = SSD に入る物の割合。

- **見た物すべて**：分類（`kf4` `sf8`）の頻度表。1 個あたり約 70〜85 B。消えないので、トレースが長いほど増える。
- **いまキャッシュにある物**：索引。Region 構成は 1 個あたり約 150 B。物単位 SSD（`obj_d*`）は約 310 B。

```
1 本 ≈ max( MEM_BASE + MEM_SEEN·u + MEM_RES·u·x ,  MEM_BASE + MEM_RES_OBJ·u·x ) × MEM_SAFETY
P    = floor( MemAvailable × MEM_FRAC ÷ 1 本 )   を 1〜NCPU に収める
```

| 係数 | 既定値 |
|---|---|
| `MEM_BASE` | 400 MB |
| `MEM_SEEN` | 80 B |
| `MEM_RES` | 160 B |
| `MEM_RES_OBJ` | 320 B |
| `MEM_SAFETY` | 1.25（ハッシュ表の倍々拡張の山の分） |

係数は次の全量の実測（WSL、Release ビルド）から決め、少し多めに取ってある。

| トレース | u | x | none | kf4 / sf8+h1 | obj | 見積り（1 本） |
|---|---|---|---|---|---|---|
| Meta CDN nha（drop, SSD 240G） | 3,500 万 | 0.013 | 0.34 GiB | 2.63〜2.68 GiB | 0.46 GiB（1,600 万要求時点） | 3.8 GiB（31 GiB の PC で P=6） |
| w01（ブロック, SSD 30%） | 1.23 億 | 0.31 | 6.0 GiB | 14.3 GiB | 11.7 GiB | 19.0 GiB（同 P=1） |
| Tencent ブロック（SSD 30%、検証用） | 285 万 | 0.31 | — | 0.63 GiB（sf8） | 0.67 GiB（obj_d1） | 0.90 GiB |

- `MemAvailable` は変種ごとに読み直す。ほかの処理がメモリを使っていれば、その分だけ `P` が下がる。
- run ごとの実測ピークは `work/out/<name>/<変種>/<id>/rss`（KB）に残り、ログに「実測ピーク RSS の最大 … GB（見積り … GB）」と出る。実測が見積りに近い・超える場合は `MEM_*` を上げる。
- 1 本の見積りが使えるメモリを超えると、警告を出して `P=1` で走らせる。試運転（`NREQ`）でも u は全量の値を使うので、`P` は安全側になる。
- `hint=mini`（tune）の影の分と `hint=ghost` の名簿の分は見積りに入っていない。物の多いトレースでは `TUNE_P=3` のように小さく固定する。WSL でメモリを使い切ると WSL ごと落ちる。

## 6. 実験ごとの説明

### 6.1 validity：部分トレースで傾向が再現できるか

トレース 1 本・1 変種につき、**全量を 1 回流す**。LRU 系は未来に依存しないので、`interval` ごとの累積値（`P.interval.csv` の `cumulative` 行）の先頭 N 要求までが、先頭 N 要求だけを流した run と同じになる。1 回の run から全 N の値が得られる。

走らせる構成（トレース 1 本あたり cachesim 26 本 ＋ 物単位 LRU の曲線 1 本）：

| id | 構成 |
|---|---|
| `none` `kf4` `sf8` `st` | 全受け入れ。分類なし / 要求回数で 4 分類（`key=freq,k=4`）/ サイズ × 回数で 8 分類（`sizefreq,k=8,kf=4`）/ サイズだけで 2 分類（`split_threshold`。閾値は書いた物の大きさの分布からオンラインで決め直す） |
| `*_h1` | 上に再挿入（`reinsert=hits:n=1/max_frac=0.5`）を足したもの |
| `d3_*` `d1_*` | 書き込み予算つき（`admit=budget:dwpd=3` / `1`）。再挿入も同じ予算から引く |
| `obj_d3` `obj_d1` | 基準：Region なしの理想の SSD（物単位 LRU）。同じ予算・同じ DRAM |
| `lru.csv` | 物単位 LRU（SSD と同容量）の先頭 N 曲線、初回アクセスのバイト（強制ミス）、K(N)（128 KiB piece 数） |

固定の条件：DRAM は LRU、`inclusion=cachelib`、`region-evict=lru`、`seal-on-repartition=inactive`、`chunk` = `CHUNK`、`seed=1`。

**指標**（すべて「先頭 N 要求」の値。要求バイトに対する %）：

| 指標 | 定義 |
|---|---|
| BMR | `miss_bytes / req_bytes` |
| **効果** `eff` | `BMR(分類なし) − BMR(分類あり)` [pt]。**正 = 分類でミスが減った**。比べる相手は同じ条件（全受け入れ / 同じ予算、再挿入の有無）の分類なし |
| collateral | `BMR(none) − BMR(物単位 LRU)` [pt]。Region 単位で消すことによる余分なミス。予算つきは `BMR(d3_none) − BMR(obj_d3)` |
| DWPD（全受入） | `flash_region_write_bytes / SSD ÷ 経過日数` |
| conv（収束） | 以後ずっと `|eff(N') − eff(全量)| ≤ tol` となる最小の N ÷ 全要求数（tol = 0.1 pt と 0.02 pt）。**小さいほど短い部分トレースで傾向を再現できる** |
| 符号一致 | 先頭 1 / 3 / 10 / 31 % の時点で、効果の符号が全量と一致するか |
| 特徴 | `og_trace_stats` の 10 窓の平均：byte/request 重みの Var(log2 s)、サイズ・頻度の寄与、Spearman ρ(size, λ)、Lorenz の偏り、サイズ変化率、その他 |

**集計（`./run.sh aggregate`）**：

1. トレースごとに 1 つの JSON（`work/results/<name>.<変種>.json`）。主結果は `drop` → `delete` → `fix` → `asis` の順に最初にあるもの（`aggregate --variant NAME` で指定可）。
2. `traces.tsv` の `category` / `source`（と `--by` で指定した列）でグループ化。各指標について**トレース数・平均・中央値・最小・最大・効果が正 / 負のトレース数**（±0.02 pt を超えるもの）を `by_<列>.csv` に出す。`report.html` には文脈ごと（全受け入れ / 再挿入 / 予算 3・1 DWPD）の表にして載せる。
3. 図：トレースごとの効果、先頭 N 要求での効果の推移（全量に近づく様子）。
4. 特徴と効果のトレース間 Spearman 順位相関（トレースが 4 本以上のとき）。
5. 埋め値の扱いによる違い、効果を「SSD への書き込み量 [TB]」で測った図、「先頭 W TB 書き込んだ時点の効果」と全量の比較表。

**注意：平均は、トレース数が少ないうちは目安。** 同じ出典のトレースが 1 本ずつしか無いグループは、平均 = その 1 本の値。最小〜最大と正 / 負の数も見る。

**出力**：

| ファイル | 内容 |
|---|---|
| `work/results/report.html` | 全部まとめた図表（ブラウザで開く） |
| `work/results/per_trace.csv` | 1 トレース 1 行。特徴・LRU・Region・効果（`eff_*`）・収束（`conv01_*`）・符号一致（`sign31_*`）… |
| `work/results/by_category.csv` ほか | グループごとの統計（長い形式：group, metric, n, mean, median, min, max, n_positive, n_negative, traces） |
| `work/results/<name>.<変種>.json` | 1 本の詳細（先頭 N の曲線を含む） |
| `work/out/<name>/<変種>/<id>/` | cachesim の生の出力（`P.json` `P.interval.csv` `P.class.csv` `P.sizebin.csv`） |
| `work/results/figs/` | スライド用の図と説明（下） |

**スライド用の図（`work/results/figs/`）**：`./run.sh aggregate` で結果から全部作り直される（合成トレースの結果だけでも、その分は出る）。

| ファイル | 中身 |
|---|---|
| `NN_名前.svg` / `.png` | 図。1600×900（16:9）の白地、文字はスライドで読める大きさ。PNG は変換器があるとき |
| `index.html` | 全部の図を並べ、図ごとに**要点**（結果の数値から自動で書いた一言）・**読み方**・**定義**を付けたページ |
| `captions.md` | 説明だけのテキスト（スライドのノートや本文に貼る用） |
| `slides.pptx` | 1 図 1 枚のスライド。左に図、右に要点と読み方。PNG ができたときだけ作る |

| 区分 | 図 | 何が分かるか |
|---|---|---|
| 妥当性検証 | BMR の内訳 | 初回アクセス・容量不足・Region 化の余分（collateral）に分けたミス。分類で取り戻せる上限の目安 |
| | 分類の効果（条件ごと） | トレース × 分類の効果 [pt]。全受け入れ / 予算 3・1 DWPD × 再挿入の有無 |
| | 条件 × 分類の表（トレースごと） | どの条件でどの分類が効くかを色で一覧 |
| | 先頭 N 要求での効果の推移 | 部分トレースで全量の値に落ち着くか |
| | 書いた量で測った効果の推移 | 先頭 W TB の時点で全量と同じ向きか |
| | 先頭 W TB 時点と全量 | 印ごとの効果と全量をつないだ線。予算つきも含む |
| | 符号が定まる書き込み量 | 何 TB 書けば効果の向きが以後変わらないか |
| | 収束に要る割合 | ±0.1 / ±0.02 pt に収まる先頭の割合 |
| | collateral の推移 | Region 化の損を短いトレースで測れるか |
| | SSD 書き込み量 | 受け入れと再挿入の内訳、構成ごとの寿命の負担 |
| | 埋め値の扱いの比較 | drop / delete で結論が変わらないか |
| | グループごとの傾向 | 種類・出典ごとの効果の平均と最小〜最大 |
| | 特徴と効果の順位相関 | どんなトレースで分類が効くか |
| 合成トレース | 理論と実測 | Che 近似のモデルが実測にどこまで合うか（OHR・BHR） |
| | 負荷ごとの容量とヒット率 | 方式ごとの実測（実線）と理論（破線） |
| | サイズ分類の利得 | OHR は上がるが BHR は下がる、を実測と理論で |
| | 240 GiB 想定（`SYNTH_SCALED=1`） | BMR・その時間変化・書き込みの内訳 |

### 6.2 synth：合成負荷の理論と実測

`synth_workloads.txt` の 1 行 = 1 負荷（`名前 サイズ:物の数:要求の割合:Zipf指数 …`）。行を足せば負荷が増える。

| 出力 | 中身 |
|---|---|
| `work/synth/<負荷>.json` | 理論（`model`）と実測（`sim`）。Region LRU の静的分割も入る：`rpartO[1]` / `rpartB[1]`（クラスごとの部分トレースを RegionFlash で、容量はモデルの OHR 最適 / BMR 最適な配分）、`rfloorB[1]`（同じ BMR 最適な配分を全体のトレースで下限として与える）、`rautoB[1]`（下限の山登り、目的 BMR だけ） |
| `work/results/synth_summary.csv` | 理論と実測（OHR/BHR）の一覧（aggregate が作る） |

- モデルが 1 容量あたり Region LRU を 40 回ほど解くので、1 負荷 × 4 容量で 20〜30 分かかる。前の版の `<負荷>.json` があると飛ばすので、作り直すときは消してから回す。
- **DRAM あり**：行に `--dram <DRAM ÷ SSD>` を付ける（見本 `w1d`。CacheLib と同じ cachelib 方式、モデルは `synth_model -D`、`../docs/Che-sim/region_model_appendix.html` §P）。DRAM ありの行では物単位 LRU と部分トレースの静的分割は回さない。

### 6.3 tune：自動推定は手で選んだ最良に届くか

分類閾値・分類ごとの容量の配分・再挿入の閾値を**実行中に自動で決めたとき**に、手で選んだ最良（固定の格子）にどこまで届くか、目標（基準「分類なし＋再挿入 n=1」に対して OHR↑・BMR↓・SSD 書き込み↓）を満たすかを測る。計画と背景は `../docs/auto_tuning_plan.html`、理論は `../docs/auto_tuning_theory.md`（図つき版 `.html`）。

```bash
./run.sh tune-synth          # 合成の 95:5・3:1（答えが分かっている負荷）。6,000 万要求 × 2 負荷、32 コアで数分
./run.sh tune meta_nha       # traces.tsv のトレース（名前を省くと全部）。validity と同じ SSD・DRAM・Region
./run.sh tune-figs           # 図だけ作り直す（TUNE_W=1 ./run.sh tune-figs で目的の重みを変える）
```

| 自動で決めるもの | RegionFlash の指定 | 決め方 |
|---|---|---|
| 分類閾値（1〜3 個） | `hint=split_threshold` / `st_hr_refine:k_max=3` / `st_recursive:depth=2` | 書いた物の大きさの分布から SplitThreshold が決め直す |
| 分類閾値（目的から） | `hint=mini:M=64/w=0.1` | 物の 1/M を τ の候補ごとの影の Region LRU に流し、目的が最大の τ を採る。Meta で最良の固定 τ に −0.19pt（SplitThreshold は −0.96pt）。途中で τ を切り替えると効果が出きらない場合がある（w01） |
| 熱/冷の分類（名簿） | `hint=ghost:frac=0.3`（大きさとも分けるなら `/tau=<τ>`） | SSD から追い出した物の ID を名簿に残し、ミスして書く物が名簿に載っていれば別の分類へ。分類なしより BMR が Meta −0.23pt・Tencent −1.8pt・w01 −0.4pt、書き込み −0.6〜−4.7%。最良の固定 τ と重ねると Tencent −2.3pt・w01 −4.0pt（w01 で frac=1 なら名簿 約 1.6 GB） |
| 容量の配分（本命） | `region-evict=lru,region-params=auto=1/w=<重み>` | 大物の下限 ff を、窓ごとに測った目的 BHR + w·OHR が上がる向きへ 0.02 ずつ動かす（山登り）。w の掃引は `st_hc0`〜`st_hc1` |
| 容量の配分（比較用） | `region-evict=mv,region-params=w=<重み>` | 分類ごとの LRU 末尾のヒット密度が最も低い分類から追い出す。1 Region に物が多い分類では末尾が冷えず限界値を誤る（合成 95:5 で −2.6pt） |
| 再挿入の閾値 | `reinsert=hits:n=auto/target=0.5/eps=0.02` | 分類ごと・ヒット回数 h ごとに「書き直した物が次の滞在でまた当たった割合」を数え、target を越える最小の h を n にする |

分類は多くても 4 つ（閾値 3 個）まで：分類ごとに書き込み中の Region（FDP なら RU）が要り、書き込み先が散るほど各 Region の書き込み速度が落ちて開いたままの時間が延び、空きも増えるため。

**出力**：`work/tune/<名前>/<id>/P.json`（n=auto は `P.reauto.json` も）、1 トレースの要約 `work/tune/<名前>.json`、図 `work/tune/figs/`（交換曲線・書き込みの内訳・n=auto の学習・目標の達成・自動と最良の差・分類の数）。見本（400 万要求の試運転、数値は意味を持たない）：`../docs/kit_tune_figs_sample/`。

**集計**：`./run.sh aggregate` が tune の結果も出典・種類ごとにまとめる（validity の結果が無くても動く）。基準 = `none_h1`。差はどれも正 = 良くなった（OHR の向上 pt、BMR の減少 pt、SSD 書き込みの減少 %）。

| 出力（`work/results/`） | 中身 |
|---|---|
| `tune_per_trace.csv` | トレース × 構成ごとに 1 行：OHR・BMR・書き込み、基準との差、`goal`（3 つとも改善）、`gap`（目的の、固定の下限の格子の最良との差）、n=auto が選んだ閾値 |
| `tune_by_<列>.csv` | 出典・種類 × 構成 × 指標ごとに n・平均・中央値・最小・最大・目標を満たしたトレース数 |
| `tune_report.html`（`report.html` にも同じ節） | 上をまとめた表 |

- 構成は 36 個。全量の Meta nha で 1 本 70〜90 秒、P=4 で 15 分ほど。
- tune-synth の同時実行数は NCPU と空きメモリ ÷ 2.5 GB（1 本の実測 1.6〜1.9 GB）の小さい方。`TUNE_P` で固定もできる。

### 6.4 che：Zipf の傾きと分類の利得

「Zipf の傾きが小さいトレースでは分類で改善し、大きいトレースでは混ぜた方が良い」という観察を、モデル（`../docs/Che-sim/region_model_appendix.html` §N）の筋で説明できるかを、トレースごとに 4 項目で確かめる。

```bash
./run.sh che                 # traces.tsv の全部（tune を先に回してあれば、その none と split_threshold の τ を使う）
./run.sh che wiki_cdn        # 一部だけ
./run.sh che-report          # 表と図だけ作り直す
CHE_SIM=0 ./run.sh che       # RegionFlash を回さず、Che の予測だけ（数十秒/本）
```

| 項目 | 何を出すか | どこから |
|---|---|---|
| (1) 上限 | 物単位 LRU のミス率。分類で取り返せる OHR はこれより多くない | Che の予測（`og_che`）と、cachesim の素の `lru`（SSD と同じ容量、DRAM なし） |
| (2) 静的分割の予測 | 分けたときの OHR/BHR、OHR・BHR・J 最適な容量の取り分、Zipf の傾き | `og_che`：物ごとの（大きさ, 要求回数）から Che 近似（IRM）で解く。Region の効果は入らない |
| (3) 容量の取り分 | split_threshold の Region LRU で、分類ごとの生きている Region の割合の時系列と後半の平均 | RegionFlash の `P.interval.class.csv` |
| (4) 固定された Region | 終了時に残っている Region のうち、先頭 10%・50% に封じたものの割合。追い出した Region の寿命 | RegionFlash を `RF_EVLOG`・`RF_ENDDUMP` つきで回す |

出力は `work/che/`：`report.html`（図 6 枚と表）、`che_summary.csv`、`<name>.json`、`<name>/`（`che.json` と RegionFlash の生の出力）。

**読み方**：α が大きいトレースほど次の 3 つが揃えば、「分類が逆効果」をモデルの筋で説明できる。揃わなければ、独立参照モデルの外（人気の時間変化など）を疑う。「Che − 実測の OHR」が大きく負のトレースは時間局所性が強く、Che の数字は目安にとどまる。

1. 上限と、静的分割の予測利得が小さい。
2. Region LRU の自然な小物の取り分が、最適な取り分（対角線）より上にずれる。
3. 先頭 10% に封じて残った Region が増える。

**設定**：`CHE_TAUS`（Che で試す閾値。既定 `16K 64K 256K 1M 4M`、split_threshold の τ は自動で足す）、`CHE_W`（J の w）、`CHE_SIM`（RegionFlash を回すか）。

**注意**：
- SSD 容量は validity・tune と同じ。Che と素の LRU は SSD の容量だけで、DRAM は入れていない。
- Region が少ないと（例：小さいトレースで R = 626.5 MiB だと十数個）、取り分の時系列が粗くなる。100 個以上が目安。`REGION_BYTES= ./run.sh che` で SSD ÷ `REGION_N` にできる。
- Che の閾値は 1/4 オクターブの格子に丸める（出力の `taus_eff`）。2 の冪は丸めない。
- 傾き α は順位 r と要求回数 c の両対数の最小二乗（r = 10 から「2 回以上要求された物」の最後の順位まで、1 桁 20 点に間引く）。別の方法で測った傾きとは値がずれうる。
- 増幅つきのトレースでは、Che は容量を K·F で割って同じ答えを出す。素の LRU は回さない。
- `og_che` のメモリは物 1 個あたり約 50 B（w01：物 1.2 億個で 6.4 GB、18 秒）。

### 6.5 paper：論文用の比較

FDP SSD の RU（GC の単位、626.5 MiB）は、CacheLib の Region の上限（256 MiB）より大きい。Region を RU に合わせると DLWA = 1 になるが、Region が大きいぶん道連れと書き直し（アプリ層の GC）が増える。書き込み時の分類がその増加を抑えるかを測る。DLWA は実機と CacheLib で測るので、ここでは測らない。SSD・DRAM・chunk は validity と同じ、変種は埋め値を除いた主の変種。

```bash
./run.sh paper-final          # 本番用（§6.5.2）→ work/paper/final/
./run.sh paper                # 全部（§6.5.1）→ work/paper/
./run.sh paper-ext            # CacheLib の n=0・2・3（§6.5.3）→ work/paper/ext/
./run.sh paper-robust         # 頑健性（§6.5.4）→ work/paper/robust/
./run.sh paper-figs           # 図と表を全部作り直す（--final / --ext / --robust でその組だけ）。PAPER_LANG=en で英語
python3 py/paper_check.py work [トレース名..]   # 結果の確かめ（§6.5.5）
```

結果は組の間で共有する（同じ方式は回し直さない）。paper-final は paper と paper-ext の場所に回し、図と表だけを `final/` に出す。

#### 6.5.1 方式の一覧（`PAPER_CATALOG`）

| id | Region | 方式 |
|---|---|---|
| `r256_base`（**基準**） | 256 MiB（`PAPER_R256`） | 分類なし、再挿入 n=1（CacheLib 相当） |
| `ru_base` | RU | 分類なし、再挿入 n=1 |
| `ru_ff90_h3` / `r256_ff90_h3` | RU / 256 MiB | サイズ分類＋大物の下限 0.9＋n=3（手動。0.9 は合成負荷で見た値の決め打ち） |
| `ru_auto` / `r256_auto`（**提案**） | RU / 256 MiB | サイズ分類＋下限の山登り（目的 J = BHR + 0.1·OHR）＋n=auto |
| `ru_auto_w0` / `r256_auto_w0` | RU / 256 MiB | 山登りの目的を BMR だけに（`w=0`）。モデルでは BMR 最適な配分が大物に寄る（appendix §O） |
| `ru_none_auto` / `r256_none_auto` | RU / 256 MiB | 切り分け：分類なしで n=auto だけ。`*_auto` との差が「分類＋下限」、`*_base` との差が n=auto の効果 |
| `ru_auto_ii` | RU | 書き直した物を専用の書き込み口（専用の Region = 専用の RU）へ（`mode=ii`）。CacheLib の priority と同じ形 |
| `ru_dram_auto` / `ru_dram1_auto` | RU | 分類の鍵を **DRAM でのヒット数**に（`hint=key:key=dram`）。`dram` は動的（書いたバイトが半々になる境界を 10 万受け入れごとに決め直す）、`dram1` は固定（0 回 / 1 回以上） |
| `ru_st_nauto` / `ru_st_n0` | RU | **OHR 寄り**：サイズ分類だけで容量の配分は決めない（appendix §E.8・§O）。n=auto あり / なし |
| `ru_gate`（既定では回さない） | RU | 下限なしで始め、試しの下限で J が上がると分かったときだけ山登りへ（`gate=1`。`../docs/Che-sim/ru_auto_guide.html` §6）。合成負荷 6 条件では `ru_auto` より良かったのは 1 つだけ |
| `ru_adm_none_auto` / `ru_adm_auto` | RU | **受け入れ × 置き場所の 2×2**。受け入れの篩 = S3-FIFO の規則（`admit=dram:min=1/ghost=0.9`：DRAM で当たった物と、断られた後 SSD 容量 × 0.9 分の名簿にいる間にまた来た物だけ書く）。相方は `ru_none_auto`・`ru_auto`。置き場所の得が篩の後も残るか（直交性）を `table_orth.md / .csv` に出す |
| `r256_adm` | 256 MiB | CacheLib に同じ篩だけ足したもの（先行研究の手法をそのまま CacheLib に） |
| `ru_adm_dram2_auto` | RU | 篩と同じ手がかりで置き場所も分ける（DRAM 1 回以下 / 2 回以上） |
| `r256_n0` / `r256_n2` / `r256_n3` | 256 MiB | CacheLib の書き直しの閾値違い（§6.5.3） |
| `*_c0` `*_c0m` `*_f1` `*_s2` `*_s3` | — | 頑健性（§6.5.4） |

`paper` の既定（`PAPER_METHODS`）は `ru_gate`・`ru_adm_dram2_auto`・閾値違い・頑健性を除いた 18 方式。

#### 6.5.2 本番用の比較（`paper-final`）

上の方式は検討の途中で足してきたもので数が多い。論文の図と表に載せる組を、今までの計測から絞ったのが `PAPER_FINAL_METHODS`。交換関係の図（`fig_tradeoff*.svg`）も出す。

| 役割 | id | 何を示すか（数字は Meta nha 全量、CacheLib n=1 との ΔBMR / 書き込み比） |
|---|---|---|
| 基準 | `r256_base` | いまの CacheLib（R=256 MiB、分類なし、n=1） |
| CacheLib のつまみ | `r256_n0` / `r256_n2` / `r256_n3` | 書き直しの閾値を変えるだけで届く範囲（n=0：+0.13pt・0.89、n=3：+0.07pt・0.91）。提案がこの線より左下にあれば「書き直しを減らしただけ」ではない |
| Region を RU に | `ru_base` | DLWA = 1 にする代わりに道連れが増える。提案が取り戻す分の起点 |
| 切り分け | `ru_none_auto` | 分類なしで n=auto だけ（+0.03pt・0.89）。`ru_auto` との差（−0.14pt）が分類＋下限の効果 |
| 提案 | `ru_auto` | サイズ分類＋大の分類の下限の山登り＋n=auto（−0.11pt・0.86） |
| 別の鍵（先行研究の手がかり） | `ru_dram1_auto` | 置き場所を DRAM でのヒット数で分ける（RIPQ / Flashield の手がかり。−0.03pt・0.87）。大きさで分ける方が効く |
| 受け入れの先行研究 | `r256_adm` | CacheLib ＋ S3-FIFO 型の篩（+0.02pt・0.61） |
| 直交性 | `ru_adm_auto` | 提案＋同じ篩（−0.08pt・0.50）。置き場所の得は篩の上でも 7 割残る |

nha 以外のトレースではまだ確かめていない（数字は選び方の根拠で、結論ではない）。外した方式は検討用として `paper` に残る。

#### 6.5.3 CacheLib の書き直しの閾値違い（`paper-ext`）

`r256_n0`・`r256_n2`・`r256_n3`（n=1 は基準）。「書き直しの閾値だけ上げた CacheLib」と比べても分類の効果が残るかを見る。n=0 は書き込みの下限と、そのときの BMR の悪化を示す。`work/paper/ext/` の図と表には `work/paper/` の結果も読み込んで、基準・n=0〜3・分類なしの n=auto・提案・篩の方式を並べる（`work/paper/` の図と表は変えない）。`python3 py/paper_check.py work --ext` で ext の構成も並べて確かめる。

**交換関係の図**（ext と final）：`fig_tradeoff.svg`（全トレース）と `fig_tradeoff_<trace>.svg`。横 = SSD 書き込み（CacheLib n=1 = 1、0 から）、縦 = ΔBMR [pt]（下が良い）。CacheLib の n=0〜3 を線でつなぎ（書き直しを増やすほど書き込みが増えて BMR が下がる）、各方式を点で重ねる。点が線の左下なら、同じ BMR を少ない書き込みで出している。篩の点は書き込みが少なく左に来るので、縦の位置を n=0 の点と比べる（n=0 より上なら、篩は書き込みを減らす代わりに元のサーバからの転送を増やしている。S3-FIFO 論文の flash の実験はミス率と書き込みバイトしか示していないので、この図が帯域の側の答えになる）。

#### 6.5.4 頑健性（`paper-robust`）

結論が RegionFlash の作りや乱数に頼っていないかを見る（既定は 1 トレース 18 本）。背景は `../docs/regionflash_callgraph.html`。

| 条件 | 指定 | 確かめること |
|---|---|---|
| `_c0` | `chunk=0`（物を 1 Region の中に連続で置く） | 片を 1 つ失うと物全体のミスになる RegionFlash の扱いが、基準を不利にしていないか |
| `_c0m` | `chunk=0` ＋ 受け入れの上限を全構成で 256 MiB（`admit=sizemax`） | 同上を R の違う構成どうしで公平に。**`_c0` だけだと Region より大きい物は入らないので、R=RU の構成だけ 256〜626.5 MiB の物を受け入れて BMR が下がり書き込みが増える**（分類の効果ではない）。R の違う比較は `_c0m` で見る |
| `_f1` | 再挿入の上限なし（`max_frac=1`。既定は追い出す Region あたり R×0.5） | 候補の多い n=1 の基準が上限で損をしていないか |
| `_s2`・`_s3` | 乱数の種 2・3 | 差がゆれより大きいか |

- 条件ごとの一式（`work/paper/robust/<条件>/`）：paper と同じ図と表を、その条件で回した構成だけで作る（`r256_base_c0` → `r256_base` と読み替え）。カタログに `<構成名>_<条件>` の名前で足して `PAPER_ROBUST_METHODS` に並べれば、その構成も加わる。
- 条件をまたいだ図（`work/paper/robust/` 直下）：`fig_robust.svg` = 提案 − CacheLib n=1、`fig_robust_n3.svg` = 提案 − CacheLib n=3。左 = ΔBMR、右 = 書き込みの比、色 = 条件。数値は `table_robust.csv` / `.md`。
- どの条件でも「提案の ΔBMR ≤ 0（または n=3 より小さい）」と「書き込みの比 < 1」の向きが既定と同じなら、結論は作りや乱数に頼っていない。

#### 6.5.5 出力と確かめ方

出力（各組のフォルダ。`index.html` に図と表と要点）：

| ファイル | 中身 |
|---|---|
| `fig_main.svg` | 値そのものの 3 面：BMR [%]・SSD 書き込み量 [TB]・OHR [%]（参考）。**縦軸は 0 から**。横軸 = トレース（BMR・OHR は 2 本以上なら最後に平均） |
| `fig_rel.svg` | CacheLib 比の 3 面：ΔBMR [pt]（負が良い）・SSD 書き込み（CacheLib の合計 = 1、1 未満が良い）・ΔOHR [pt]。基準の棒は出さない |
| `fig_bmr` / `fig_write` / `fig_ohr` / `fig_dbmr` / `fig_nwrite` / `fig_dohr` | 上の 2 枚の 1 面ずつ。`.png` は変換器があれば |
| `table_main.tex` / `.md` / `.csv` | トレースごとに基準の BMR・書き込み [TB]（取得＋再書き込み）・OHR・アプリ層の書き込み増幅と、各構成の「ΔBMR / 書き込み（CacheLib = 1）/ ΔOHR」。CSV は全指標（`admit_TB`・`reinsert_TB`・`pad_TB`、`write_norm`） |
| `table_orth.md` / `.csv` | 受け入れ × 置き場所の 2×2（置き場所の ΔBMR を篩なし・ありで並べ、その差 = 相互作用） |
| `fig_tradeoff*.svg`（ext・final） | 交換関係の図（§6.5.3） |
| `fig_ops_<trace>.svg` | 時間変化。横 = 処理した要求数。BMR・OHR は区間ごと（約 `PAPER_BINS`=100 区間）、取得と再書き込みは累積 TB |
| `fig_wr_<trace>.svg` | 横 = その構成の累積 SSD 書き込み量。同じ量を書いた時点（寿命の消費が同じ時点）での比較 |
| `fig_marks_<trace>.svg` / `table_marks.md` / `.csv` | 書き込み量の印での結果（下） |
| `methods.tsv` | 回した方式（id・名前・指定） |

- **棒のラベル**：棒の上に値を書く。横書きが棒の幅に入らなければ縦書き、軸の外にはみ出すなら棒の中に白で。書き込みの棒は**取得（濃い）＋再書き込み（薄い）**に分けて積み、合計を棒の上、取得と再書き込みを各段の中に、小数第 2 位で書く（段に入らない数字だけ省く）。詰め物はごく小さいので表と CSV だけ。
- 面の題は軸の幅に収まるまで字を小さくし、それでも入らなければ 2 行に折る。
- 時間変化の図は各構成の `P.interval.csv`（刻み = `INTERVAL`）から作る。
- **書き込み量の印**：CacheLib（`r256_base`）の累積 SSD 書き込みが W に達した時点（= 先頭の部分トレース）の結果を取り出し、全量と比べる。W は `traces.tsv` の `marks` 列、空なら `WRITE_MARKS_TB`。図の線が W の時点で全量と同じ側にあれば、その長さの部分トレースで結論が言える。表では全量と差の向きが違うものに ✗。
- **増幅**：`traces.tsv` の `amp_key`（要求ごとに K 個の別キー。物・要求・フットプリントが K 倍）と `amp_size`（サイズを F 倍）を RegionFlash の `amp-key` / `amp-size` として tune・paper に渡す（`P.json` の `config` に残る）。要求数・区間・温め期間は増幅した後の要求で数える。validity には効かない。
- **確かめ方**：`python3 py/paper_check.py work [トレース名..]`。構成ごとに、Region・chunk・分類の閾値が意図どおりか、全要求を回したか、書き込み = 受け入れ＋再挿入＋詰め物 が成り立つか、基準からの書き込みの差の内訳、受け入れの差が BMR の差 × 要求バイトと合うか、n=auto が選んだ閾値と下限 ff を一覧にする。

## 7. 制約と注意

- `/bin/bash^M: bad interpreter` が出たら、ファイルの改行が Windows 形式（CRLF）になっている。キットのフォルダで次を実行して LF に戻す：`sed -i 's/\r$//' run.sh config.sh traces.tsv synth_workloads.txt py/*.py src/*.c`
- FAIL した構成（`P.json` が無いもの）は、同じコマンドをもう一度実行すればそれだけやり直す。理由はそのフォルダの `stdout`。
- 物の数の上限：`cdn_prefix_check` と `og_xform` は物が約 6,700 万個まで（超えると `NN overflow`。`src/*.c` を `-DNN=` 付きでコンパイルし直す）。`og_info` は無制限。
- 先頭 N 要求の値は **LRU 系の構成だけ**に成り立つ。未来を使う分類（`oracle_*` など）を `HINTS` に足すと、先頭 N の曲線は意味を持たない。
- 書き込み予算（DWPD）はトレースの時刻（秒）が基準。経過が 1 時間未満のトレースでは予算つき構成を走らせない。時刻が実時間でないトレース（合成など）では DWPD の意味が変わる。
- 変換したトレースは `work/traces/` に残る（容量を食う。消すと次回は作り直す）。
- 実行時間の実測（WSL）：nha（9,600 万要求）の全量で 1 本あたり none 56 秒、分類あり 70〜100 秒。w01（2.2 億要求、物が小さい）では 4〜7 分。

## 8. 確かめたこと

- 手元の 4 トレース（Block 3 本と CDN 1 本、`NREQ=2000000`）で `setup → validity → synth → aggregate` を通し、`report.html` と CSV が出ることを確認した。meta は埋め値を検出し、フットプリントが 19.97 TB（以前の測定の 20.0 TB と一致）。CSV を `traceConv` で変換する経路、`SYNTH_SCALED=1` の経路も 200 万要求で通した。**全長のトレースでの所要時間と結果の一致は未確認。**
- スライド用の図（2026-11-11）：meta nha の全量（drop / delete）、msr と tencent の先頭 500 万要求、合成 6 負荷（200 万要求）、縮小版（400 万要求）で 45 枚が出た。`slides.pptx` は LibreOffice で PDF に変換でき 45 枚がそろった（PowerPoint 本体では未確認）。合成トレースの結果だけのときは合成の図だけが出た。見本は `../docs/kit_figs_sample/`（数値は本番の結果ではない）。
- 2026-11-19：方式の選択（カタログ化）の前後で、paper・ext・robust の各方式の指定が文字列として一致（18・3・18）。自作の方式を足して nha の先頭 30 万要求で paper・paper-final・tune を回し、図と表まで出た。Python のファイルは 3.10 で通らない書き方を含まない（`tools/synth/py310_check.py`）。

## 付録：変更の履歴

- **11-19 受け入れの篩に ghost を足した**：初版（11-18）の `admit=dram:min=1` は ghost なしで、Flashield に近い厳しい篩だった。nha では OHR −5.7pt・BMR +0.09pt・書き込み 0.54 倍（CacheLib n=1 比）と S3-FIFO より不利に出た。ghost あり（`/ghost=0.9`）では OHR −2.9pt・BMR +0.02pt・書き込み 0.61 倍。置き場所の得は篩なし −0.14pt、ghost あり −0.10pt（7 割）、ghost なし −0.06pt。ghost なしと比べたいなら `/ghost=0.9` を外す。
- **11-19 人気が移り変わる合成負荷（`tools/synth/drift_adm.sh`）**：W1 の形で一巡 τ = 2,000 万 / 500 万要求。篩の後も SSD は入れ替わり続けた（追い出し 5,000〜16,000 回）。CacheLib 相当（BMR 41.1 / 41.7%）に対し、篩だけで −2.1 / −3.4pt（書き込み 2.4% / 7.2%）、篩＋置き場所の良い方で −4.0 / −4.7pt。篩と同じ手がかり（DRAM ヒット）で置くと大半が重なり（篩なし −5.8 / −5.4pt → 篩あり −1.8 / −0.4pt）、サイズで置くと τ によって向きが変わった（篩あり +1.0 / −1.0pt）。
- **11-18 受け入れの篩の合成負荷での注意**：W1 の形（2,000 万要求、DRAM = SSD/30）では、篩だけで SSD 書き込みが 0.4% に減り、BMR も 39.7% → 34.0%（篩なしでは追い出したバイトの 88% が一度も当たっていなかった）。篩の後は SSD がほとんど入れ替わらず（Region の追い出し 16 万回 → 740 回）、置き場所は何も変えなかった。**直交するかどうかは「篩を通った後も SSD があふれるか」で決まる**。実トレース（nha）では篩の後も入れ替わり、得は残った。
- **11-18 `mode=ii` と `n=auto` の組み合わせを修正**：`mode=ii` では書き直した物が専用の分類へ移るため、n=auto の学習が専用の分類にだけ数えられ、元の分類は n=1 のままだった。書き直しを決めた分類に数えるように直した（`ssd/rf_region.c`、物ごとの情報に 1 バイト `re_c`。物の情報の大きさは変わらない）。`mode=ii` も `class=reclassify` も使わない構成は結果がビット単位で変わらない。W1 の形（300 万要求）で、直す前の `ru_auto_ii` は書き直しが pi の 3.6 倍で BMR が 0.6pt 悪化、直した後は pi より BMR −0.11pt・書き込み −2.6%（1 点だけの確認）。
- **11-17 山登りの OHR の項を修正**：目的 J の OHR の項が SSD のヒットだけを数えていた（BHR の項は DRAM と SSD）。合計に直した（`evict/region_basic.c`・`region_policy.h`・`ssd/rf_region.c`）。`w=0` の結果はビット単位で変わらない。`w>0` の山登り（paper の `*_auto`、tune の `*hc*`・`st_hc0.1_auto`・`mini_hc0.1_auto`）は DRAM がある構成では前の版と別物になる。揃えて比べるなら、それらの結果のフォルダを消してから回す。
- **11-17 既存の結果に新しい構成を足すとき**：どの実験も `P.json` のある構成を飛ばすので、そのまま実行すれば足りない構成だけが走り、図と表は全構成で作り直される。
- **`REGION_BYTES` の既定を RU（656932864）にした**：validity と tune も Region = RU で走る。以前の「SSD ÷ 392」に戻すには `REGION_BYTES= ./run.sh …`。
