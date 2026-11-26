# 実験キット：RegionFlash の実験を別 PC で回す

改造した libCacheSim（RegionFlash）で、手元で確かめた実験を別の PC のトレースで再現し、**出典・種類ごとに傾向をまとめる**ためのキット。
RegionFlash の指定（`hint=`・`region-params=` など）の意味は [`params.html`](params.html) にまとめてある。

## 目次

1. [何ができるか](#1-何ができるか)
2. [必要なもの](#2-必要なもの)
3. [最短の手順](#3-最短の手順)（3.2 回し直す・使い回す・止める）
4. [トレースを書く：`traces.tsv`](#4-トレースを書くtracestsv)
5. [設定：`config.sh`](#5-設定configsh)
6. [実験ごとの説明](#6-実験ごとの説明)（validity・synth・tune・che・paper）
7. [制約と注意](#7-制約と注意)
8. [中身の地図](#8-中身の地図)（どのファイルが何をするか）
9. [確かめたこと](#9-確かめたこと)

変更の履歴は [`CHANGELOG.md`](CHANGELOG.md)。

## 1. 何ができるか

| コマンド | 何をするか | 入力 | 出力 |
|---|---|---|---|
| `paper-final2` | **論文の本番**：CacheLib と現在の提案 `ru-otsu-ghost-cb`（閾値・容量の配分・書き直しを全部自動）と、比較に向いた方式（§6.5.2） | `traces.tsv` | `work/paper/final2/` |
| `paper-final` | 同じ組で、提案を以前の `ru-st-climb-nauto` にしたもの（前の版との比較、§6.5.2） | `traces.tsv` | `work/paper/final/` |
| `paper-sens` | **感度**：SSD 容量・DRAM・RU（Region の大きさ）を一つずつ変えても、CacheLib との差の向きが同じか（§6.5.6） | `traces.tsv` | `work/paper/sens/` |
| `paper-robust` / `paper` / `paper-ext` | 頑健性・検討用の全部・CacheLib の書き直しの閾値違い（§6.5） | `traces.tsv` | `work/paper/…` |
| `validity` | トレース 1 本ごとに、分類なし / ありなどを全量 1 回回し、**先頭 N 要求で測った値**を全 N について出す（部分トレースで傾向が再現できるか）（§6.1） | `traces.tsv` | `work/out/`・`work/results/` |
| `synth` | 少数サイズの合成負荷で、理論（Che 型モデル）と実測（cachesim）を並べる（§6.2） | `synth_workloads.txt` | `work/synth/` |
| `tune` / `tune-synth` | 分類の閾値・容量の配分・書き直しの閾値を自動で決めたとき、手で選んだ最良にどこまで届くか（§6.3） | `traces.tsv` / 合成 | `work/tune/` |
| `che` | Zipf の傾きと分類の利得を、モデルの筋で説明できるか（§6.4） | `traces.tsv` | `work/che/` |
| `aggregate` | validity・synth・tune の結果を出典・種類ごとに集計し、スライド用の図を作る | `work/` の結果 | `work/results/` |
| `stop` | 回っている run.sh を全部止める（子の cachesim・python ごと。§3.2） | | |

```
traces.tsv ─┬→ run.sh validity ──→ work/out/…, work/results/<name>.<変種>.json ─┐
config.sh  ─┤→ run.sh tune ──────→ work/tune/                                    ├→ run.sh aggregate → work/results/report.html, *.csv, figs/
            ├→ run.sh che ───────→ work/che/report.html                           │
            └→ run.sh paper* ────→ work/paper/{final2, final, robust, ., ext}/（図と表）│
synth_workloads.txt → run.sh synth → work/synth/*.json ───────────────────────────┘
```

コマンドの一覧は `./run.sh`（引数なし）で出る。「準備」（setup・check・methods・stop）・「実験」・「図と表だけ作り直す」（aggregate・*-figs・che-report）の 3 群。済んだ run は回し直さず、同じコマンドの run は使い回す。止め方も含めて §3.2。

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
NREQ=2000000 ./run.sh paper-final2  # 試運転：各トレースの先頭 200 万要求だけ。結果の形を確かめる
rm -rf work/paper                # 試運転の結果を消す
./run.sh paper-final2            # 論文の本番（現在の提案 ru-otsu-ghost-cb）→ work/paper/final2/index.html
./run.sh paper-final             # 同じ組で以前の ru-st-climb-nauto（結果は共有するので、増える run は ru-st-climb-nauto と V2 の変種だけ）→ work/paper/final/
./run.sh paper-robust            # 頑健性 → work/paper/robust/
./run.sh paper-sens              # 感度（SSD 容量・DRAM・RU を一つずつ変える）→ work/paper/sens/
./run.sh validity                # 部分トレースの妥当性（時間がかかる）
./run.sh synth                   # 合成負荷
./run.sh aggregate               # 集計 → work/results/report.html
```

一部のトレースだけ：`./run.sh paper-final2 meta_nha wiki_cdn`。`setup → validity → synth → aggregate` をまとめて：`./run.sh all`（tune・che・paper は含まない）。

### 3.1 libCacheSim の用意を変えるとき

- **clone 済みの libCacheSim を使う**：`config.sh` の `LCS_SRC` にそのパスを書く（clone を省く）。未パッチなら `setup` がそこへ `regionflash.patch` を当てる（当たるかを `git apply --check` で先に確かめ、当たらなければ何も変えずに止まる）。パッチ済みならそのまま使う。
- **パッチの土台は `93529a1`**。上流の最新（2026-11-09 の `4e3521a`）には当たらない。手元の clone が新しいときは、どちらかで土台に合わせる：

  ```bash
  git -C <clone> checkout 93529a1                 # その clone を土台に戻す
  git -C <clone> worktree add ../lcs-rf 93529a1   # 元の作業を残したまま別フォルダに土台を出し、LCS_SRC=../lcs-rf にする
  ```

- **作成済みの cachesim を使う**：`CACHESIM` にそのパスを書けばビルドも省ける。
- **キットを新しくしたとき**：`setup` は当てたパッチの印（`work/src/.rf_patch`）を `regionflash.patch` と照合し、違えば土台に戻して当て直し、cachesim を作り直す。各実験も始める前に同じ照合をする（印の無い古い `work/` でも自動で作り直す）。古い cachesim のままだと新しい構成は「reinsert does not have parameter target」などで FAIL する。`LCS_SRC` や `CACHESIM` を指定しているときは自動では作り直さない。

### 3.2 回し直す・使い回す・止める

どの実験も、済んだ run は回し直さない。途中で止めても同じコマンドで続きから回る。消せばやり直す。

**済みの判定（11-20）**：各 run のフォルダの `cmd.txt` に、成功した run の**パッチの版と cachesim に渡した引数**（トレース・SSD・`NREQ`・`-t`・`-e` の指定全部。DRAM・Region・片の扱い・乱数の種もここに入る。出力先だけ除く）が残る。`P.json` があり、`cmd.txt` が今回しようとしているものと**完全に一致するときだけ**飛ばす。カタログの指定を書き換えた・`config.sh` の SSD や Region を変えた・試運転（`NREQ`）の結果が残っていた・キットを更新した、のどれでも、その run だけ回し直す（id が同じでも古い結果は使わない）。途中で失敗した run は `cmd.txt` を消してから回すので、次も回し直しになる。

- **同じコマンドの run は使い回す**：まだ済んでいない run でも、全く同じ印（パッチの版＋コマンド）の済んだ run が `work/` の別の場所（別の組・別の感度の点）にあれば、そのフォルダのファイルを写して回さない（ログに「… と同じ run なので結果を写した」）。例：paper-sens の RU 256 MiB の点の `ru-none-nat-n1`・`ru-otsu-ghost-n1` は、paper の `r256-none-nat-n1`・`r256-otsu-ghost-n1` と同じコマンドなので写すだけ。写すのは複製で、リンクにはしない（回し直しで上書きしたとき相手を壊さないため）。
- 11-20 より前の結果には `cmd.txt` が無いので、**既定では全部回し直しになる**。その前の結果を使い続けるなら `ADOPT_OLD=1 ./run.sh …`：`cmd.txt` の無い結果は、前の判定（片の扱いと篩の fill 規則）が通れば今の指定で回したものと見なして `cmd.txt` を付ける（指定を変えていないことは自分で確かめる）。
- `SIG_PATCH=0 ./run.sh …`：パッチの版は照合しない（キットを更新しても結果が変わらないと分かっているとき。11-20 の `P.knobs.csv` のように出力が増えただけの更新など）。

**止め方（誤って裏で回したときなど、11-20）**：実験のコマンド（validity・synth・tune・che・paper 系・setup・all）は、回っている間 `work/running/<pid>`（中身はコマンド）を置き、`GUARD_SEC`（既定 5）秒ごとに **この `run.sh` のファイル**と `work/running/<pid>` があるかを見る。どちらかが消えたら、その run.sh から生まれたプロセス（xargs・cachesim・python）を全部止めて終わる（止めている間に xargs が次の cachesim を起動しないよう、先に全部を一時停止してから終わらせる）。

```bash
nohup ./run.sh paper-final2 > p.log 2>&1 &   # 裏で回した
./run.sh stop                                # 回っている全部を止める（work/running/ を空にする）
rm work/running/<pid>                        # その 1 つだけ止める
rm run.sh                                    # run.sh を消しても止まる（後で zip から戻す）
```

止めた run は `cmd.txt` が付かないので、次に同じコマンドを回すと、その run だけやり直す。

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
| `CHUNK_MODE` | `piece` | 片の扱い。`piece` = CacheLib の piece-wise caching と同じ（失った片だけが消え、欠けた片だけを取り直す）。`object` = 片を 1 つ失うと物全体を捨てる（11-19 までの結果）。`chunk=0` の構成と物単位 SSD は常に `object`。**結果の `P.json` に記録された扱いが違えば、その run は回し直す**（以前の版の結果も回し直しになる）。§6.5.4 の注意 |
| `HINTS` | none / kf4 / sf8 / st | validity の分類方法。`id:hint指定` を足せば、その分類の効果も集計される |
| `BUDGET_DWPD` | `3 1` | validity の書き込み予算つき構成（1 日あたりの SSD 全書き換え回数）。空なら走らせない |
| `SAT_VALUE` / `SAT_VARIANTS` / `SAT` | `4294967295` / `drop delete` / `auto` | サイズ `2^32−1`（= −1 を uint32 で書いたもの。GET 以外の操作の印と見る）の要求の扱い。`auto` = その要求があるトレースでは `SAT_VARIANTS` を走らせる（先頭が主結果。tune・che・paper は先頭だけ）。`drop` = 無かったことにする、`delete` = その物を消す操作（要求に数えない）、`fix` = その物の他のサイズに置換（感度用）、`asis` = そのまま |
| `WRITE_MARKS_TB` | `1.5` | 「先頭から SSD に W TB 書いた時点」で効果を測る W（TB = 10^12 B） |
| `INTERVAL` | `1000000` | 先頭 N 曲線・時間変化・自分で決める値の動き（`P.knobs.csv`）の刻み（要求数）。11-20 に 10000 → 100 万（Wiki・Tencent CDN で区間ごとの CSV が大きくなりすぎたため）。1000 万要求未満のトレースや細かく見たいときは小さく（`INTERVAL=10000 ./run.sh …`）。書き込み予算つき（`dwpd`）の構成は区間の終わりごとに λ を動かすので、刻みが粗いと追従も粗くなる |
| `NREQ` | 空 | 数字を入れると各 run を先頭 NREQ 要求で打ち切る（試運転）。`cmd.txt` に入るので、本番では試運転の結果を使わず回し直す |
| `PAPER_USER_METHODS` | CacheLib n=3・CacheLib＋篩・RU のみ・以前の提案・提案・提案＋篩 | 説明用の組：`./run.sh paper-figs --user-method [場所]` が、ここに並べた方式（＋基準）だけの図と表と `fig_summary.svg` を `work/paper/<場所>/`（既定 `users`）に出す。回さない（§6.5.5） |
| `PAPER_FIGSETS` | main・parts・lambda・adm・prior | 図の組：方式が多くて図が潰れるので、目的ごとに選んだ方式（＋基準）だけの同じ一式を `<組>/figs_<名前>/` にも出す。1 つ = `"名前:方式 方式 .."`。色はどの図の組でも同じ（§6.5.5） |
| `GROUP_FIGS` | `1` | paper 系の図と表を、全体に加えてトレースの群（`category` × `source`）ごとにも `<組>/groups/<category>__<source>/` に出す（§6.5.5） |
| `GUARD_SEC` | `5` | 止め方の見張りの間隔 [秒]（§3.2） |
| `ADOPT_OLD` / `SIG_PATCH` | `0` / `1` | 済みの判定（§3.2）：`cmd.txt` の無い古い結果を使うか / パッチの版も照合するか |
| `P` | `auto` | 同時に走らせる cachesim の数。`auto` = §5.3 の見積りで自動。数字なら固定 |
| `NCPU` | CPU の総コア数 − 1 | 並列に使うプロセス数の上限（`P=auto`・tune-synth・`SYNTH_P`・`make -j`） |
| `MEM_FRAC` | `0.8` | `auto` で使ってよい空きメモリ（`MemAvailable`）の割合 |
| `SYNTH_N` / `SYNTH_SCALED` | 2000 万 / `0` | 合成トレースの長さ / 縮小 SSD 想定の重い負荷（2 億要求 × 多数、数時間）を走らせるか |
| `PAPER_*_METHODS` / `TUNE_METHODS` | §5.2 | paper 系・tune で回す方式 |

### 5.2 回す方式を選ぶ

**paper 系**：方式はすべて `PAPER_CATALOG` に名前を 1 つずつ置き、組ごとに名前を並べて選ぶ。並べた順が図と表の順になる。

**方式の名前（11-20）**：`R-閾値-配分-再挿入[-adm][~変種]` の部品の並び。指定は名前から組み立てる（`run.sh` の `id_spec`）ので、名前と中身はずれない。部品の中身を変えたら必ず別の名前になる（`v2` のような版の名前は使わない）。細かい設定は部品の後ろに `.設定` で付ける（既定と違うものだけ）。数は先頭が 0 なら小数（`l01` = 0.1、`w0` = 0）。部品と設定の一覧は `config.sh` の「方式の名前の付け方」。

| 部品 | 名前（既定） | 細かい設定の例 |
|---|---|---|
| R | `r256`（256 MiB）・`ru`（RU） | |
| 閾値 | `none`・`st`・`otsu`（大津・再利用の強さ・累積）・`fix`・`dram` | `otsu.cd10`、`fix.256k`、`dram.t1` |
| 配分 | `nat`（下限なし）・`ghost`（名簿、move=4）・`climb`（山登り w=0.1）・`ff` | `ghost.m0`・`ghost.m2`・`ghost.m3`、`climb.w0`、`ff.90` |
| 再挿入 | `n0`〜`n9`・`nauto`・`cb`（損得、λ=0） | `cb.l01`、`cb.first`、`nauto.ii` |
| 受け入れ | `adm`（S3-FIFO 型の篩） | |
| 変種 | `~c0`・`~c0m`・`~f1`・`~s<n>` | |

例：CacheLib = `r256-none-nat-n1`、現在の提案 = `ru-otsu-ghost-cb`、その篩つき = `ru-otsu-ghost-cb-adm`、種 2 = `ru-otsu-ghost-cb~s2`。11-20 より前の id（`r256_base`・`ru_auto_v2` など）の結果は、同じコマンドなら新しい名前の run に写される（§3.2）。古い名前のフォルダは消してよい。11-20 より前の `ru_auto_v2` は `move=2`（今の `ru-otsu-ghost.m2-cb`）で、今の `ru-otsu-ghost-cb`（move=4）とは別の run になる。

| 組 | 変数 | コマンド | 既定 |
|---|---|---|---|
| 本番 | `PAPER_FINAL2_METHODS` | `paper-final2` | 24 方式（現在の提案 `ru-otsu-ghost-cb` と論文の問い Q1〜Q5 の変種、§6.5.2） |
| 以前の本番 | `PAPER_FINAL_METHODS` | `paper-final` | 20 方式（以前の `ru-st-climb-nauto` と V2 の変種、§6.5.2） |
| 検討用の全部 | `PAPER_METHODS` | `paper` | 31 方式（§6.5.1） |
| CacheLib の閾値違い | `PAPER_EXT_METHODS` | `paper-ext` | `r256-none-nat-n0 r256-none-nat-n2 r256-none-nat-n3` |
| 頑健性 | `PAPER_ROBUST_METHODS` | `paper-robust` | 21 方式（§6.5.4） |
| 感度 | `PAPER_SENS_METHODS` | `paper-sens` | 5 方式 × 変えた点（`SENS_SSD`・`SENS_DRAM`・`SENS_RU`、§6.5.6） |

```bash
./run.sh methods                                               # カタログの全部と、組ごとにどれが選ばれているか
PAPER_FINAL2_METHODS="r256-none-nat-n1 ru-otsu-ghost-cb ru-otsu-ghost-n1-adm" ./run.sh paper-final2   # その場で選ぶ（config.sh を書き換えてもよい）
```

自分の方式を足すには、`config.sh` の末尾などに 1 行足して、その id を組に並べる：

```bash
PAPER_CATALOG+=("ru-otsu-ghost.m3-cb.l03|現在の提案・下限を速く下げる・λ=0.3")   # 名前だけ書けば指定は作られる（| の後は図の名前、省略可）
PAPER_METHODS="$PAPER_METHODS ru-otsu-ghost.m3-cb.l03"
```

- 指定の書き方は validity・tune と同じ（`params.html`）。SSD・DRAM・chunk はトレースごとに validity と同じ値が入り、`region-size` を書かなければ R = RU（`REGION_BYTES`）。
- 基準 `r256-none-nat-n1` は差の基準なので、どの組でも選んでいなくても必ず回す。`all` と書くとカタログの全部（頑健性用の `~c0` などで終わる id は除く）。
- **図と表の系列は、いまの `config.sh` の選択で決まる**：組で選んだ方式のうち、どれか 1 本のトレースで結果がある物は全部、表・棒グラフ・時間変化・交換関係の図の系列になる（`paper_figs.py` は方式の一覧を持たない。ext は paper の選択も並べる）。`./run.sh paper-figs` は描く前に、結果のある組の `methods.tsv` を今の選択から作り直す（回さない）。だから選択を変えて `paper-figs` だけ回せば、その方式だけの図になる。選択から外した方式は、結果が残っていても出ない。
- 図の名前は、カタログの 3 番目の欄（`"id|指定|名前"`）→ `paper_figs.py` の名前の表 → 指定から自動で作った名前（例「R=RU＋閾値＝再利用の強さ＋配分＝名簿＋n=cb（名簿）」）の順。色は名前の表に無ければ並び順で決まり、どの図でも同じ方式は同じ色。
- カタログに無い id を選ぶと、何も回さずに止まる。
- 以前の指定も使える：`PAPER_GATE=1` で `ru-st-climb.gate-nauto` を足す、`ROBUST_W0=0` で `ru-st-climb.w0-nauto~*` を外す。

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
- **`SYNTH_SCALED=1`（`scaled.py`・`writes.py`）**：トレース `work/synth/sc_r3to1.bin`・`sc_r95to5.bin`（`SYNTH_SCALED_N` 要求、2 億要求で 4.8 GB ずつ。ディスクに計 9.6 GB、作るときのメモリは約 1.7 GB）を作り、作る前にディスクの空きを確かめ、16＋48 本の cachesim を回す。トレースの長さが `SYNTH_SCALED_N` と合わなければ（前の回の残り・書きかけ）作り直す。cachesim の出力は `work/synth/o/<名前>.log` に残り、結果の JSON が書かれなければ、構成・終了コード・ログの末尾を出して止まる（強制終了ならメモリ不足の見込み。`SYNTH_P=4` などで並列を減らす）。片の扱いは `CHUNK_MODE` に従う（512K の物は 16K の片 32 個。11-19 から片のモード）。`scaled.json`・`writes.json` に記録した `chunk_mode` が今の `CHUNK_MODE` と違えば回し直す（それ以前の結果も回し直しになる）。 結果をやり直すときは `work/synth/scaled.json`・`writes.json` を消す。
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

### 6.6 indicator：提案が効くワークロードを事前に見分ける（11-26）

```
./run.sh indicator           # traces.tsv の全トレースで指標を計算し（回さない）、paper の結果と並べる → work/indicator/report.html
./run.sh indicator-report    # 表と図だけ作り直す
./run.sh indicator --sens    # paper-sens の点（SSD・DRAM・RU を一つずつ変えた点）でも → work/indicator/sens/report.html
```

**感度の点（`--sens`、11-26）**：paper-sens と同じ点（`SENS_SSD`・`SENS_DRAM`。RU の点は Region の大きさがモデルに入らないので既定の点の指標）で指標を計算し、`work/paper/sens/<点>/` の結果の分類の得（`ru-otsu-ghost-cb` − `ru-none-nat-cb`。どちらも `PAPER_SENS_METHODS` の既定に入っている）と並べる。トレースごとの図 `fig_sens_<trace>.svg` は、列 = SSD 容量・DRAM・RU、段 = 入れ替わり（実線＝予言、破線＝分類しない run の実測、赤の破線＝2 回）・大きさで分けられるばらつき・分類の得 ΔBMR・分類の得 ΔOHR。容量を増やして得が消える（逆転する）トレースで、入れ替わりやばらつきが同じ向きに下がっていれば、指標で事前に予言できたことになる。表は `table_indicator_sens.csv`。予言の入れ替わりは、Region 単位の追い出しで戻ってくる人気のコンテンツの書き直しや再挿入を数えないので、実測より小さく出る（Tencent で約 1/3）。水準より、点の間の向きを見る。

トレースだけから、paper と同じ SSD・DRAM の大きさ（と `NREQ`）で 2 つの指標を出す（`src/og_ind.c`。コンテンツ単位の Che 近似で、DRAM は前段の LRU）。篩なしと篩あり（2 回以上リクエストされるコンテンツだけ入れる近似）の両方。

- **入れ替わり**：SSD の容量が何回書かれるか（圧力）。2 回より小さいと追い出しがほとんど起きず、書き分けも配分も効かない。篩で効果が消えるのは、篩でここが落ちるトレース（この PC の w01：3.9 → 1.1 回）。
- **ばらつき**：大きさの段（大津と同じ [2^b, 2^(b+1))）ごとの ln（SSD に届くリクエスト ÷ 書いたバイト）を、大きさの線 1 本で分けたときの大津の得点（と、全体に対する割合 η、切れ目）。混ぜたキャッシュの run から測ると、人気のコンテンツが一度きりのコンテンツに押し出されて差が隠れるので、トレースから測る。

`report.html` は指標と、paper の結果の分類の得（`ru-otsu-ghost-cb` − `ru-none-nat-cb`、篩ありは両方に `-adm`。BMR の差とその基準の BMR に対する割合、OHR の差）を並べる（`fig_indicator.svg`・`table_indicator.csv`）。この 4 方式は paper-final2 の組に入っている。同じ大きさ・`NREQ`・og_ind で計算済みなら飛ばし、category・source だけ今の traces.tsv に書き直す。

限界：コンテンツ単位のモデルなので Region の巻き添えは入らない。巨大なコンテンツを分割して持つトレース（nha）や、容量が足跡のごく一部のトレースでは入れ替わりの水準が合わない（nha：予言 1,647 回、実測 198 回）。時間局所性も入らない。この PC の 4 トレースでは、入れ替わりが篩の後の効果の消え方を当て、ばらつきが w01・Tencent（効く）と msr（効かない）を分けた。境目は全トレースで決める（docs/Che-sim/workload_indicator.html）。

### 6.5 paper：論文用の比較

FDP SSD の RU（GC の単位、626.5 MiB）は、CacheLib の Region の上限（256 MiB）より大きい。Region を RU に合わせると DLWA = 1 になるが、Region が大きいぶん道連れと書き直し（アプリ層の GC）が増える。書き込み時の分類と容量の配分がその増加を抑え、さらに BMR を下げるかを測る。DLWA は実機と CacheLib で測るので、ここでは測らない（NAND 書き込みを比べるときは R=256 MiB 側に 1.4 を掛ける）。SSD・DRAM・chunk は validity と同じ、変種は埋め値を除いた主の変種。

**流れ**

```bash
./run.sh paper-final2         # 1. 本番：CacheLib と現在の提案 ru-otsu-ghost-cb ほか（§6.5.2）→ work/paper/final2/
./run.sh paper-final          # 2. 同じ組で提案を以前の ru-st-climb-nauto に（前の版との比較）→ work/paper/final/
./run.sh paper-robust         # 3. 頑健性（§6.5.4）→ work/paper/robust/
./run.sh paper                # 検討用の全部（§6.5.1）→ work/paper/
./run.sh paper-ext            # CacheLib の n=0・2・3 だけ（§6.5.3）→ work/paper/ext/
./run.sh paper-sens           # 感度：SSD 容量・DRAM・RU を一つずつ変える（§6.5.6）→ work/paper/sens/
./run.sh paper-figs           # 図と表を全部作り直す（--final2 / --final / --ext / --robust でその組だけ）。PAPER_LANG=en で英語
./run.sh paper-figs --user-method        # 説明用：PAPER_USER_METHODS の方式だけ → work/paper/users/（場所を変えるなら --user-method talk など）
python3 py/paper_check.py work [トレース名..]   # 結果の確かめ（§6.5.5）
```

結果は組の間で共有する（同じ方式は回し直さない）。final2・final は paper と paper-ext の場所に回し、図と表だけを `final2/`・`final/` に出す。だから 1 → 2 と回しても、2 で増える run は以前の `ru-st-climb-nauto` と V2 の変種だけ。

#### 6.5.1 方式の一覧（`PAPER_CATALOG`）

`config.sh` の並びと同じ 7 群。数字は Meta nha 全量・`chunk-mode=piece`、CacheLib n=1 との ΔBMR [pt]・NAND 書き込み比。

| 群 | id | Region | 方式 |
|---|---|---|---|
| 1 基準 | `r256-none-nat-n1`（**基準**） | 256 MiB（`PAPER_R256`） | 分類なし、再挿入 n=1（CacheLib 相当。BMR 4.705%） |
| | `ru-none-nat-n1` | RU | Region を RU にしただけ（−0.036・0.720） |
| 2 **現在の提案（V2）** | `ru-otsu-ghost-cb` | RU | **閾値・容量の配分・書き直しを全部自動**。閾値 ＝ 大きさのビン（2 のべき乗）ごとの「書いた 1 バイトが何回要求されたか」の対数を、大津の式で 2 組に分ける所（累積で数える、`hint=st_otsu:val=logreq/bins=1/hold=4/band=2/ema_den=0`）。容量 ＝ SSD を出た物の名簿で各分類の「容量を少し足したら増える BHR」を測り、そろう向きへ大の分類の下限を動かす（`region-params=auto=2/w=0/ff=0`。名前の `ghost`）。書き直し ＝ 次の滞在で 1 バイトあたり何回当たったかが、名簿の限界の価値以上なら書き直す（`reinsert=hits:n=cb/lambda=0/base=marg`）。nha −0.112、w01 −3.81、Tencent −0.755。理論は `../docs/Che-sim/ru_auto_v2_theory.html` |
| | `ru-otsu-ghost-n1` / `r256-otsu-ghost-n1` | RU / 256 MiB | 同じ分類と配分で、書き直しは CacheLib と同じ n=1 |
| | `ru-otsu-ghost-n1-adm` | RU | ＋ S3-FIFO 型の篩。w01 では書き込み 0.2 倍で BMR −3.2、Tencent・ブロックのトレースでは BMR が悪化 |
| | `ru-otsu-ghost-cb.first` | RU | 書き直しの基準を「初めて書いた物の値」に（より厳しく、書き込みを減らす側） |
| | `ru-none-nat-cb` / `ru-otsu-nat-cb` | RU | 部品の段階（Q3）：分類なしで n=cb だけ / 閾値だけで配分は自然なまま（下限なし）。`ru-none-nat-n1` → `ru-otsu-nat-cb` → `ru-otsu-ghost-n1` → `ru-otsu-ghost-cb` の順に部品が増える |
| | `ru-otsu-ghost-cb-adm` / `ru-none-nat-cb-adm` | RU | 篩との 2×2（Q5）：篩 ＋ 現在の提案 / 篩 ＋ 分類なし ＋ n=cb（相方 `ru-otsu-ghost-cb`・`ru-none-nat-cb`） |
| | `ru-otsu-ghost-cb.l01` / `_l03` / `_l1` | RU | 書き込みの値段 λ = 0.1・0.3・1（Q4）。交換関係の図で `ru-otsu-ghost-cb`（λ=0）とつないだ破線になる |
| 3 以前の提案 | `ru-st-climb-nauto` / `r256-st-climb-nauto` | RU / 256 MiB | st の閾値 ＋ 下限の山登り（前の窓と J = BHR + 0.1·OHR を比べる）＋ n=auto（+0.083・0.609。山登りは 1 歩の効果が窓ごとの揺れに埋もれて酔歩し、n=auto は書き直しを 9 → 0.8 TB に削りすぎた） |
| | `ru-st-climb-n1` / `r256-st-climb-n1` | RU / 256 MiB | 同じで書き直しを n=1 に（−0.062・0.708 / −0.093・0.971） |
| | `ru-st-climb.w0-nauto` / `r256-st-climb.w0-nauto` | RU / 256 MiB | 山登りの目的を BHR だけに（appendix §O） |
| | `ru-st-ff.90-n3` / `r256-st-ff.90-n3` | RU / 256 MiB | 手で調整（大の下限 0.9・n=3） |
| | `ru-st-climb.gate-nauto`（既定では回さない） | RU | 下限が要るかを試してから山登り（`../docs/Che-sim/ru_auto_guide.html` §6） |
| 4 部品の切り分け | `ru-none-nat-nauto` / `r256-none-nat-nauto` | RU / 256 MiB | 分類なしで n=auto だけ（+0.137） |
| | `ru-st-nat-nauto` / `ru-st-nat-n0` | RU | サイズ分類だけで容量の配分は決めない（OHR 寄り）。n=auto あり / なし |
| | `ru-st-climb-nauto.ii` | RU | 書き直した物を専用の書き込み口へ（`mode=ii`。CacheLib の priority と同じ形） |
| | `r256-none-nat-cb.first` / `r256-st-climb-cb.first` / `ru-st-climb-cb.first` | 256 MiB / RU | 書き直しだけ n=cb（初めて書いた物の基準）に。CacheLib に足すと +0.011・0.989（1 回当たった物は初めて書いた物とほぼ同じ価値で、答えはほぼ n=1） |
| 5 先行研究の手がかり | `ru-dram.k2-climb-nauto` / `ru-dram.t1-climb-nauto` | RU | 置き場所の鍵を **DRAM でのヒット数**に（RIPQ / Flashield。`dram` は動的、`dram1` は 0 回 / 1 回以上） |
| | `r256-none-nat-n1-adm` | 256 MiB | CacheLib ＋ S3-FIFO 型の篩（`admit=dram:min=1/ghost=0.9`：DRAM で当たった物と、断った後 SSD 容量 × 0.9 分の名簿にいる間にまた来た物だけ書く。−0.003・0.608） |
| | `ru-none-nat-nauto-adm` / `ru-st-climb-nauto-adm` | RU | **受け入れ × 置き場所の 2×2**（相方は `ru-none-nat-nauto`・`ru-st-climb-nauto`）。置き場所の得が篩の後も残るか（直交性）を `table_orth.md / .csv` に出す |
| | `ru-st-climb-n1-adm` / `r256-st-climb-n1-adm` / `ru-st-climb-cb.first-adm` | RU / 256 MiB | 篩 ＋ 以前の分類と下限（n=1 で −0.064・0.429 / −0.074・0.588） |
| | `ru-dram.t2-climb-nauto-adm`（既定では回さない） | RU | 篩と同じ手がかりで置き場所も分ける |
| 6 CacheLib のつまみ | `r256-none-nat-n0` / `r256-none-nat-n2` / `r256-none-nat-n3` | 256 MiB | CacheLib の書き直しの閾値違い（§6.5.3） |
| 7 頑健性 | `*~c0` `*~c0m` `*~f1` `*~s2` `*~s3` | — | §6.5.4 |

`paper` の既定（`PAPER_METHODS`）は群 1〜5 から `ru-st-climb.gate-nauto`・`ru-dram.t2-climb-nauto-adm` を除いた 31 方式。

#### 6.5.2 本番（`paper-final2`）と以前の本番（`paper-final`）

論文の図と表に載せる組。交換関係の図（`fig_tradeoff*.svg`）も出す。`paper-final2` が本番で、`paper-final` は提案だけを以前の `ru-st-climb-nauto` に戻した同じ組（前の版との比較用。11-20 に足した V2 の変種 3 つと `ru-otsu-ghost-cb` も並ぶ）。

| 役割 | id | 何を示すか（nha、CacheLib n=1 との ΔBMR [pt] / NAND 書き込み比） |
|---|---|---|
| 基準 | `r256-none-nat-n1` | いまの CacheLib（R=256 MiB、分類なし、n=1） |
| CacheLib のつまみ | `r256-none-nat-n0` / `r256-none-nat-n2` / `r256-none-nat-n3` | 書き直しの閾値を変えるだけで届く範囲。提案がこの線より左下にあれば「書き直しを減らしただけ」ではない |
| **現在の提案** | `ru-otsu-ghost-cb`（final2）／ `ru-st-climb-nauto`（final） | 全部自動。`ru-otsu-ghost-cb`：nha −0.112、w01 −3.81、Tencent −0.755、msr −0.32（move=4。NAND 比は回し直しの後に更新）（以前の `ru-st-climb-nauto` は +0.083 / −0.38 / +1.46）。乱数の種による揺れは 0.016pt 以内 |
| 以前の提案の中心 | `r256-st-climb-n1` / `ru-st-climb-n1` | 以前の分類＋下限に n=1。−0.093・0.971 / −0.062・0.708 |
| Region を RU に | `ru-none-nat-n1` | −0.036・0.720。BMR の得はほぼ無い（RU の利点は DLWA の側） |
| 切り分け | `ru-none-nat-nauto` | 分類なしで n=auto だけ（+0.137） |
| 別の鍵（先行研究の手がかり） | `ru-dram.t1-climb-nauto` | 置き場所を DRAM でのヒット数で分ける（RIPQ / Flashield） |
| 受け入れの先行研究 | `r256-none-nat-n1-adm` | CacheLib ＋ S3-FIFO 型の篩（−0.003・0.608） |
| 直交性 | `ru-st-climb-nauto-adm` | 以前の分類＋下限＋n=auto ＋ 同じ篩（+0.112・0.354） |
| 篩＋分類＋下限（n=1） | `r256-st-climb-n1-adm` / `ru-st-climb-n1-adm` | −0.074・0.588 / −0.064・0.429。BMR が CacheLib より良い構成の中で書き込み最少 |
| 書き直しを損得で | `r256-none-nat-cb.first` / `ru-st-climb-cb.first` | +0.011・0.989 / −0.064・0.674。損得で決めると答えはほぼ n=1 |
| **論文の問い（11-20、`../docs/Che-sim/paper_story.html` §3）** | `ru-otsu-nat-cb` `ru-otsu-ghost-n1` `ru-none-nat-cb` | Q3 部品の段階：`ru-none-nat-n1` → 閾値だけ → ＋配分（n=1）→ ＋n=cb（`ru-otsu-ghost-cb`） |
| | `ru-otsu-ghost-cb.l01` `_l03` `_l1` | Q4 書き込みとの交換：λ を上げると左（書き込み減）へ。`fig_tradeoff*.svg` で CacheLib の n=0〜3 の線と並ぶ破線 |
| | `ru-none-nat-cb-adm` `ru-otsu-ghost-cb-adm` | Q5 篩との 2×2（`ru-none-nat-cb`・`ru-otsu-ghost-cb` と組む） |

**確かめた範囲と弱点**（`../docs/Che-sim/summary_1120.html`）：`ru-otsu-ghost-cb` の数字は手元のスクリプトで回した nha・w01・Tencent（Region を nha と同じ比率に縮めた版）。設計の選択（閾値の量など）はこの 3 本を見て決めたので、検証用に回した msr（−0.32、以前の `ru-st-climb-nauto` の −1.05 に負け、名簿の向きが逆という未解決の問題）、block_traces_3（差が出ない）、大きさの分布が途中で変わる合成負荷（分けない CacheLib に負ける）では得が小さいか負けている。篩はトレースを選ぶ。`chunk-mode=object` で測った以前の数字は `CHANGELOG.md` にある。

#### 6.5.3 CacheLib の書き直しの閾値違い（`paper-ext`）

`r256-none-nat-n0`・`r256-none-nat-n2`・`r256-none-nat-n3`（n=1 は基準）。「書き直しの閾値だけ上げた CacheLib」と比べても分類の効果が残るかを見る。n=0 は書き込みの下限と、そのときの BMR の悪化を示す。`work/paper/ext/` の図と表には `work/paper/` の結果も読み込んで、基準・n=0〜3・分類なしの n=auto・提案・篩の方式を並べる（`work/paper/` の図と表は変えない）。`python3 py/paper_check.py work --ext` で ext の構成も並べて確かめる。

**交換関係の図**（ext と final）：`fig_tradeoff.svg`（全トレース）と `fig_tradeoff_<trace>.svg`。横 = SSD 書き込み（CacheLib n=1 = 1、0 から）、縦 = ΔBMR [pt]（下が良い）。CacheLib の n=0〜3 を線でつなぎ（書き直しを増やすほど書き込みが増えて BMR が下がる）、各方式を点で重ねる。点が線の左下なら、同じ BMR を少ない書き込みで出している。篩の点は書き込みが少なく左に来るので、縦の位置を n=0 の点と比べる（n=0 より上なら、篩は書き込みを減らす代わりに元のサーバからの転送を増やしている。S3-FIFO 論文の flash の実験はミス率と書き込みバイトしか示していないので、この図が帯域の側の答えになる）。

#### 6.5.4 頑健性（`paper-robust`）

結論が RegionFlash の作りや乱数に頼っていないかを見る（既定は 1 トレース 21 本。11-20 に現在の提案の種 2・3 `ru-otsu-ghost-cb~s2`・`_s3` を足し、`fig_robust_v2.svg` に出す）。背景は `../docs/regionflash_callgraph.html`。

| 条件 | 指定 | 確かめること |
|---|---|---|
| `~c0` | `chunk=0`（片に分けず、物を 1 Region の中に連続で置く。Region より大きい物は入らない） | 片を 1 つ失うと物全体のミスになる RegionFlash の扱いを外したとき、同じ R どうしの比較の向きが変わらないか。**実機の置き方ではない**（下の注意）。R の違う構成では、R=RU の方だけ 256〜626.5 MiB の物を置けるので、差に置ける物の違いが混ざる |
| `~c0m` | `chunk=0` ＋ 受け入れの上限を全構成で 256 MiB（`admit=sizemax`） | 全構成を同じ物の集まりにして、追い出し方・置き場所だけを比べる。**256 MiB を超える物は全構成でミスになる**（nha ではリクエストバイトの 44.3%、リクエスト数の 0.69%）ので、言えるのは 256 MiB 以下の物の中での傾向だけ。BMR の主結果の代わりにはならない |

**注意：片（chunk）の扱いと実機の違い**。CacheLib を CDN で使うときの piece-wise caching は、大きい物を一定の大きさの片に分け、片をそれぞれ独立の項目として保存する（Region の大きさは物の大きさの上限にならない）。RegionFlash の既定（`chunk=64K`）も片に分けて置くが、**片を 1 つ失うと物全体を捨て、ヒット・ミスも物単位で数える**（`ssd/rf_piece.c`）。実機では欠けた片のバイトだけを取り直すので、RegionFlash は大きい物の損を実機より大きく数えている。この損は、物がまたがる Region の数が多いほど（R が小さいほど）大きい。`~c0` / `~c0m` はこの扱いを外すが、実機とも違う置き方になる。11-19 から、実機と同じ数え方 `chunk-mode=piece`（`CHUNK_MODE`、既定）を足した。失った片だけが消え、リクエストは残っている片のバイトをヒット、欠けた片のバイトをミスに数え、DRAM から出るとき欠けた片だけを書く。片を本当に別のキーに展開したトレース（`tools/synth/og_piece.c`）と、nha の先頭で BMR が 0.01〜0.06pt 以内で一致することを確かめた（`chunk-mode=object` は +0.54pt ずれていた）。
| `~f1` | 再挿入の上限なし（`max_frac=1`。既定は追い出す Region あたり R×0.5） | 候補の多い n=1 の基準が上限で損をしていないか |
| `~s2`・`~s3` | 乱数の種 2・3 | 差がゆれより大きいか |

- 条件ごとの一式（`work/paper/robust/<条件>/`）：paper と同じ図と表を、その条件で回した構成だけで作る（`r256-none-nat-n1~c0` → `r256-none-nat-n1` と読み替え）。カタログに `<構成名>~<条件>` の名前で足して `PAPER_ROBUST_METHODS` に並べれば、その構成も加わる。
- 条件をまたいだ図（`work/paper/robust/` 直下）：`fig_robust.svg` = 提案 − CacheLib n=1、`fig_robust_n3.svg` = 提案 − CacheLib n=3（`_w0` = 目的を BHR だけにした提案）。`fig_robust_rubase.svg`（と `_n3_rubase`）= R=RU にしただけ（`ru-none-nat-n1`）− CacheLib、`fig_robust_vs_rubase.svg` = 提案 − `ru-none-nat-n1`（Region を RU にした効果を除いた、分類＋下限＋n=auto の効果）。左 = ΔBMR、右 = 書き込みの比、色 = 条件。数値は `table_robust.csv` / `.md`。
- **`ru-none-nat-n1~c0m`（11-19 追加）**：nha 系で `ru-none-nat-n1`（R=RU のみ）が CacheLib より BMR −0.11pt と提案より良く出た。既定の `chunk=64K` では片を 1 つ失うと物全体を失うので、物がまたがる Region の少ない大きい R ほど大きい物を失いにくく、この得の一部は RegionFlash の数え方によるものかもしれない（上の注意）。`~c0m` で得が残るかは見られるが、`~c0m` は 256 MiB を超える物（nha のバイトの 44%）を外した中での比較なので、**得が消えても「片の扱いのせい」とは言い切れない**（外した大きい物に得があった可能性が残る）。決着には、片を独立の項目として数える作り（実機の piece-wise caching と同じ）が要る。
- どの条件でも「提案の ΔBMR ≤ 0（または n=3 より小さい）」と「書き込みの比 < 1」の向きが既定と同じなら、結論は作りや乱数に頼っていない。

#### 6.5.5 出力と確かめ方

出力（各組のフォルダ。`index.html` に図と表と要点）：

**説明用の組（11-25）**：教授や読者に見せる図は、方式を絞らないと読めない。`config.sh` の `PAPER_USER_METHODS` に並べた方式（基準 `r256-none-nat-n1` は必ず入る）だけで、同じ一式（図・表・`index.html`）を `work/paper/<場所>/` に出す。
```
./run.sh paper-figs --user-method          # → work/paper/users/
./run.sh paper-figs --user-method talk     # → work/paper/talk/（場所は英数字と _ . -）
```
- **回さない**：`work/paper`・`work/paper/ext` に済んだ結果を、final と同じ読み方で使う。結果の無い方式は図に出ない（先に `./run.sh paper <名前>` などで回す）。
- **トレース**：traces.tsv のトレースのうち、結果のあるもの全部。群ごとの一式も `groups/<category>__<source>/` に出る。
- **`fig_summary.svg`**：説明用の 1 枚。トレースごとに方式を横棒で並べる。左は BMR の CacheLib との差（ポイント、負が良い）、右は SSD 書き込みの比（CacheLib = 1）。これは説明用の組と図の組（`figs_<名前>/`）でだけ描く。同じ形で、指標の組み合わせを変えた図も出す：
  - `fig_summary_main.svg`（BMR・書き込み・OHR の値）と `fig_summary_rel.svg`（ΔBMR・書き込みの比・ΔOHR）。`fig_main`・`fig_rel` と同じ並び。
  - 1 面ずつ：`fig_summary_bmr / _write / _ohr`（値）、`fig_summary_dbmr / _nwrite / _dohr`（CacheLib 比）。
  - 値と CacheLib 比の対：`fig_summary_bmr_pair`（BMR・ΔBMR）、`_ohr_pair`（OHR・ΔOHR）、`_write_pair`（書き込み・書き込みの比）。
  - 書き込みの面（write・nwrite）は `fig_write`・`fig_nwrite` と同じく、取得（濃い）＋再書き込み（薄い）に分けて積む（詰め物は描かず表に残す）。nwrite は CacheLib の書き込み全体 = 1 で割り、CacheLib 自身の内訳も出す。
  - 行は基準（CacheLib）＋方式。値の面は目盛りがトレースごと（桁が違うため）、差と比の面は全トレース共通。並べる指標は `paper_figs.py` の `SUMM_FIGS`。
- 説明用の組は図の組（`figs_<名前>/`）には分けない。場所を変えれば、別の組み合わせの組を並べて残せる（`PAPER_USER_METHODS="…" ./run.sh paper-figs --user-method talk2`）。

**図の組（11-24）**：final2 は 24 方式あり、1 枚に全部並べると凡例が図の半分を占め棒が潰れる。そこで `config.sh` の `PAPER_FIGSETS` に並べた図の組ごとに、その方式と基準だけの同じ一式（図・表・`index.html`）を `<組のフォルダ>/figs_<名前>/` に出す（例：`work/paper/final2/figs_main/`）。群ごとの一式の中にも同じく `figs_<名前>/` ができる。全方式の図と表（`table_main.csv` など）は今までどおり組のフォルダにある。組の選択に無い方式は飛ばし、1 つも無い図の組は出さない。色は組全体の並びで決めるので、どの図の組でも 1 方式 1 色。全方式の図も、凡例が 3 行を超えた分だけ図を縦に伸ばすので軸は潰れない。回し直しは要らない（`./run.sh paper-figs` で作り直せる）。

**トレースの群ごと（11-20）**：組のフォルダ（`work/paper/`・`final/`・`final2/`・`ext/`・`robust/<条件>/`）には、traces.tsv の全トレースをまとめた図と表が出る。それに加えて、traces.tsv の `category` × `source` の群ごとに、同じ一式（下の表のファイル全部と `index.html`）を `<組のフォルダ>/groups/<category>__<source>/` に出す（例：`work/paper/final2/groups/CDN__Meta/`、`.../KV__Twitter/`）。書き込み量や効き方は群でかなり違うので、群の中で比べるとき用。run は群をまたいで共有するので回す量は変わらず、`./run.sh paper-figs` だけでも作り直せる。出さないなら `GROUP_FIGS=0`。category・source が空のトレースは `-` の群になる。感度（paper-sens）と頑健性の条件をまたいだ図（`fig_robust*.svg`）は群に分けない（感度はトレースごとの図がある）。

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
| `fig_knobs_<trace>.svg` / `table_knobs.md` / `.csv` | **自分で決める値の動き**（11-20）。横 = 処理した要求数。左から：① 閾値 τ（log2 バイト）。② 容量配分：大の分類の下限 ff（太い線）と、大の分類が実際に SSD に持つ割合（同じ色の細い線）。細い線が太い線より上なら下限は効いておらず（自然な配分のほうが多い）、重なっていれば下限で配分を決めている。下限なしで分類する方式は細い線だけ。③ n=cb の書き直しの基準（名簿で測った端の密度、ヒット/バイト/滞在）と ④ 実質の n（その時点で書き直す最少のヒット数。CacheLib の n に当たる）は分類ごとに線を分ける（破線 = 分類 0（小）、実線 = 分類 1（大））。色 = 方式。表は最後の 4 分の 1 の区間の平均（どこに落ち着いたか）と、下限が初めてその ±0.05 に入った区間。元は各 run の `P.knobs.csv` |
| `fig_rewrite_<trace>_<方式>.svg` | **書き直しの閾値がなぜその値か**（11-20、n=cb の方式）。分類ごとの面に、今の滞在で当たった回数の段（1〜8 以上）ごとの「その段で書き直した物が次の滞在で 1 バイトあたり何回当たったか」の棒と、比べる相手（名簿で測った端の密度 ＋ λ）の赤い破線。線を越える段（濃い棒）は書き直し、越えない段（灰色）は書き直さない。越える最初の段が実質の n。白抜き = データ 32 件未満で学習中。各 run の `P.reauto.json` の最終値 |
| `methods.tsv` | 回した方式（id・名前・指定） |

- **棒のラベル**：棒の上に値を書く。横書きが棒の幅に入らなければ縦書き、軸の外にはみ出すなら棒の中に白で。書き込みの棒は**取得（濃い）＋再書き込み（薄い）**に分けて積み、合計を棒の上、取得と再書き込みを各段の中に、小数第 2 位で書く（段に入らない数字だけ省く）。詰め物はごく小さいので表と CSV だけ。
- 面の題は軸の幅に収まるまで字を小さくし、それでも入らなければ 2 行に折る。
- 時間変化の図は各構成の `P.interval.csv`（刻み = `INTERVAL`）から作る。
- **書き込み量の印**：CacheLib（`r256-none-nat-n1`）の累積 SSD 書き込みが W に達した時点（= 先頭の部分トレース）の結果を取り出し、全量と比べる。W は `traces.tsv` の `marks` 列、空なら `WRITE_MARKS_TB`。図の線が W の時点で全量と同じ側にあれば、その長さの部分トレースで結論が言える。表では全量と差の向きが違うものに ✗。
- **増幅**：`traces.tsv` の `amp_key`（要求ごとに K 個の別キー。物・要求・フットプリントが K 倍）と `amp_size`（サイズを F 倍）を RegionFlash の `amp-key` / `amp-size` として tune・paper に渡す（`P.json` の `config` に残る）。要求数・区間・温め期間は増幅した後の要求で数える。validity には効かない。
- **確かめ方**：`python3 py/paper_check.py work [トレース名..]`。構成ごとに、Region・chunk・分類の閾値が意図どおりか、全要求を回したか、書き込み = 受け入れ＋再挿入＋詰め物 が成り立つか、基準からの書き込みの差の内訳、受け入れの差が BMR の差 × 要求バイトと合うか、n=auto が選んだ閾値と下限 ff を一覧にする。

#### 6.5.6 感度（`paper-sens`）

キャッシュの容量・DRAM の大きさ・RU の大きさを変えても、CacheLib（`r256-none-nat-n1`）との差の向きが変わらないかを見る。全部の組み合わせは回さず、**一度に一つだけ変え、ほかは既定のまま**にする。

| 変えるもの | 変数（既定） | 意味 | 点の場所 |
|---|---|---|---|
| SSD 容量 | `SENS_SSD`（`0.33 2`） | トレースごとの既定の SSD（traces.tsv の `ssd` か `SSD_DEFAULT`）の何倍か。DRAM は SSD ÷ `DRAM_DIV` なので一緒に変わる | `sens/ssd_x<倍>/` |
| DRAM | `SENS_DRAM`（`100 10`） | DRAM = SSD ÷ この値（既定は 30） | `sens/dram_<値>/` |
| RU | `SENS_RU`（`268435456 1342177280 2684354560`） | Region = RU の大きさ [B]（256 MiB・1.25 GiB・2.5 GiB。既定は 626.5 MiB） | `sens/ru_<MiB>M/` |

- 方式は `PAPER_SENS_METHODS`（既定 `r256-none-nat-n1 ru-none-nat-n1 ru-otsu-ghost-cb ru-otsu-ghost-n1 ru-none-nat-cb`）。`ru-none-nat-cb` は分類なしで書き直しだけ n=cb（`ru-otsu-ghost-cb` との差が分類＋配分の効果）。
- **既定の点は `work/paper` の結果を共有**する（paper で回した方式は回し直さない）。変えた点だけが `work/paper/sens/<点>/` に増える。
- **RU の点では CacheLib を回さない**：`r256-none-nat-n1` は Region を 256 MiB に固定して持つので、RU を変えても既定の点と同じ run になる。図と表では既定の点の値と比べる。
- 出力（`work/paper/sens/`）：`fig_sens_<トレース>.svg`（上から ΔBMR [pt]・SSD 書き込みの比・ΔOHR [pt]。左から SSD 容量・DRAM・RU。縦の灰線 = 既定の点）、`table_sens.csv` / `.md`（ΔBMR・ΔOHR・書き込みの比）。
- **書き込みは DLWA を掛けていない**（アプリ層の書き込み）。CacheLib 側の DLWA は RU によって違う（実機で測ったのは R=256 MiB・RU 626.5 MiB の 1.4 だけ）。NAND 書き込みで比べるときは、RU ごとに実機の値か見積りを掛ける。
- 規模：トレース × 点 × 方式。既定ではトレースあたり 7 点 × 5 方式から、既定の点の共有分と RU の点の CacheLib を除いた約 30 run。重いトレースだけ外すなら `./run.sh paper-sens meta_nha wiki_cdn` のように名前を並べる。点を減らすなら `SENS_RU="1342177280" ./run.sh paper-sens`、空にすればその軸を回さない（`SENS_DRAM= ./run.sh paper-sens`）。
- 見たいこと：SSD が大きいほど端の価値が下がり分けた得は小さくなるはず。RU が大きいほど巻き添えが増え、書き直しと分類の効き目は増すはず。DRAM が大きいと SSD に来る要求が冷えるので、得の大きさが変わるか。

## 7. 制約と注意

- `/bin/bash^M: bad interpreter` が出たら、ファイルの改行が Windows 形式（CRLF）になっている。キットのフォルダで次を実行して LF に戻す：`sed -i 's/\r$//' run.sh config.sh traces.tsv synth_workloads.txt py/*.py src/*.c`
- FAIL した構成（`P.json` が無いもの）は、同じコマンドをもう一度実行すればそれだけやり直す。理由はそのフォルダの `stdout`。
- 物の数の上限：`cdn_prefix_check` と `og_xform` は物が約 6,700 万個まで（超えると `NN overflow`。`src/*.c` を `-DNN=` 付きでコンパイルし直す）。`og_info` は無制限。
- 先頭 N 要求の値は **LRU 系の構成だけ**に成り立つ。未来を使う分類（`oracle_*` など）を `HINTS` に足すと、先頭 N の曲線は意味を持たない。
- 書き込み予算（DWPD）はトレースの時刻（秒）が基準。経過が 1 時間未満のトレースでは予算つき構成を走らせない。時刻が実時間でないトレース（合成など）では DWPD の意味が変わる。
- 変換したトレースは `work/traces/` に残る（容量を食う。消すと次回は作り直す）。
- 実行時間の実測（WSL）：nha（9,600 万要求）の全量で 1 本あたり none 56 秒、分類あり 70〜100 秒。w01（2.2 億要求、物が小さい）では 4〜7 分。

## 8. 中身の地図

| ファイル | 役割 |
|---|---|
| `run.sh` | 入口。節の順：止め方 → setup → traces.tsv → 1 本のトレース（変換・大きさ・済みの判定・`job_line`）→ validity → synth → tune → che → paper の図 → paper の方式と組（`PAPER_SETS`）→ main |
| `config.sh` | 設定。場所 → 資源 → 実行の仕方 → 1 本のトレースの SSD・DRAM・Region と validity → tune → che → synth → paper（部品 → カタログ → 組 → 感度） |
| `py/figures.py` | 図を描く道具（`Fig`・`Ax`・`heat`・`span`・`method_label`・`to_png`・`write_index`・`write_pptx`）。下の図の作り手が全部使う |
| `py/paper_figs.py` | paper 系の図と表（主の表・棒・時間変化・交換関係・書き込み量の印・自分で決める値の動き・書き直しの閾値の理由）。`--group` で群ごと |
| `py/paper_sens_figs.py` / `paper_robust_figs.py` / `paper_check.py` | 感度・頑健性の図と表、結果の確かめ |
| `py/aggregate.py` → `agg_figs.py` | validity・synth・tune の集計（report.html・CSV）と、スライド用の図の一式 |
| `py/tune_one.py` / `tune_figs.py` / `tune_aggregate.py` | tune（と paper の run の要約）・その図と集計 |
| `py/trace_one.py` / `che_one.py` / `che_report.py` / `synth_run.py` / `scaled.py` / `writes.py` | validity・che・synth の 1 本ごとの要約と実行 |
| `src/*.c` | トレースの道具（og_info・og_pack・cdn_prefix_check など）と合成負荷・Che モデル |

## 9. 確かめたこと

- 手元の 4 トレース（Block 3 本と CDN 1 本、`NREQ=2000000`）で `setup → validity → synth → aggregate` を通し、`report.html` と CSV が出ることを確認した。meta は埋め値を検出し、フットプリントが 19.97 TB（以前の測定の 20.0 TB と一致）。CSV を `traceConv` で変換する経路、`SYNTH_SCALED=1` の経路も 200 万要求で通した。**全長のトレースでの所要時間と結果の一致は未確認。**
- スライド用の図（2026-11-11）：meta nha の全量（drop / delete）、msr と tencent の先頭 500 万要求、合成 6 負荷（200 万要求）、縮小版（400 万要求）で 45 枚が出た。`slides.pptx` は LibreOffice で PDF に変換でき 45 枚がそろった（PowerPoint 本体では未確認）。合成トレースの結果だけのときは合成の図だけが出た。見本は `../docs/kit_figs_sample/`（数値は本番の結果ではない）。
- 2026-11-19：方式の選択（カタログ化）の前後で、paper・ext・robust の各方式の指定が文字列として一致（18・3・18）。自作の方式を足して nha の先頭 30 万要求で paper・paper-final・tune を回し、図と表まで出た。Python のファイルは 3.10 で通らない書き方を含まない（`tools/synth/py310_check.py`）。
