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
                                      ↓
                       run.sh aggregate → work/results/report.html, per_trace.csv, by_*.csv, synth_summary.csv
```

## 1. 必要なもの

Linux（Windows なら WSL2 の Ubuntu）。`gcc cmake make git python3 libzstd-dev libglib2.0-dev`（Python は標準ライブラリだけ）。
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
すでにパッチ済みの libCacheSim があるなら、`config.sh` の `LCS_SRC` にそのパスを書くと clone を省く（`CACHESIM` に作成済みの cachesim を指定すればビルドも省く）。

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
| `HINTS` | none / kf4 / sf8 | 分類方法。`id:hint指定` を足せば、その分類の効果も集計される |
| `BUDGET_DWPD` | `3 1` | 書き込み予算つき構成（1 日あたりの SSD 全書き換え回数）。空なら走らせない |
| `SAT_VALUE` / `SAT_VARIANTS` / `SAT` | `4294967295` / `drop delete` / `auto` | サイズ `2^32−1`（= −1 を uint32 で書いたもの。GET 以外の操作の印と見る）の要求の扱い。`auto` = その要求があるトレースでは `SAT_VARIANTS` を走らせる（先頭が主結果）。`drop` = 無かったことにする、`delete` = その物を消す操作（要求に数えない）、`fix` = その物の他のサイズに置換（感度用）、`asis` = そのまま。`SAT="drop delete asis"` のように直接並べてもよい |
| `WRITE_MARKS_TB` | `1.5` | 「先頭から SSD に W TB 書いた時点」で効果を測る W（全受け入れ・分類なしの書き込み量で数える。TB = 10^12 B） |
| `REGION_BYTES` | 空 | Region の大きさをバイト数で固定（空 = SSD ÷ `REGION_N`） |
| `INTERVAL` | `10000` | 先頭 N 曲線の刻み（要求数）。1000 万要求未満のトレースは `1000` に |
| `P` | `auto` | 同時に走らせる cachesim の数。`auto` = §4.1 の見積りで自動。数字なら固定 |
| `MEM_FRAC` | `0.8` | `auto` で使ってよい空きメモリ（`MemAvailable`）の割合 |
| `NREQ` | 空 | 数字を入れると各 run を先頭 NREQ 要求で打ち切る（試運転） |
| `SYNTH_N` / `SYNTH_SCALED` | 2000 万 / `0` | 合成トレースの長さ / 縮小 SSD 想定の重い負荷（2 億要求 × 多数、数時間）を走らせるか |

### 4.1 メモリと同時実行数（`P=auto`）

cachesim 1 本のピーク RSS は、次の 2 つの物の数でほぼ決まる。u = トレースの物の数（`og_info` の `uniq`）、x = min(1, (SSD+DRAM) ÷ フットプリント) = SSD に入る物の割合。

- **見た物すべて**: 分類（`kf4` `sf8`）の頻度表。1 個あたり約 70〜85 B。消えないので、トレースが長いほど増える。
- **いまキャッシュにある物**: 索引。Region 構成は 1 個あたり約 150 B。物単位 SSD（`obj_d*`）は約 310 B。

```
1 本 ≈ max( MEM_BASE + MEM_SEEN·u + MEM_RES·u·x ,  MEM_BASE + MEM_RES_OBJ·u·x ) × MEM_SAFETY
P    = floor( MemAvailable × MEM_FRAC ÷ 1 本 )   を 1〜nproc に収める
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

走らせる構成（トレース 1 本あたり cachesim 20 本（RegionFlash 18 + 理想の SSD 2）+ 物単位 LRU の曲線 1 本）:

| id | 構成 |
|---|---|
| `none` `kf4` `sf8` | 全受け入れ。分類なし / 要求回数で 4 分類（`key=freq,k=4`）/ サイズ × 回数で 8 分類（`sizefreq,k=8,kf=4`） |
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
| `work/out/<name>/<変種>/<id>/` | cachesim の生の出力（`P.json` `P.interval.csv` `P.class.csv` `P.sizebin.csv`） |

## 7. 制約と注意

- 物の数の上限: `cdn_prefix_check` と `og_xform` は物が約 6,700 万個まで（超えると `NN overflow`。`src/*.c` を `-DNN=` 付きでコンパイルし直す）。`og_info` は無制限。
- 先頭 N 要求の値は **LRU 系の構成だけ**に成り立つ。未来を使う分類（`oracle_*` など）を `HINTS` に足すと、先頭 N の曲線は意味を持たない。
- 書き込み予算（DWPD）はトレースの時刻（`clock_time` の秒）が基準。経過が 1 時間未満のトレースでは予算つき構成を走らせない。
- 時刻が実時間でないトレース（要求番号が時刻の合成トレースなど）では DWPD・予算の意味が変わる。
- 変換したトレースと fix 変換したトレースは `work/traces/` に残る（容量を食う。済んだら消してよい。消すと次回は作り直す）。
- 実行時間の実測(WSL): nha(9,600 万要求)の全量で、1 本あたり none 56 秒、分類あり 70〜100 秒。w01(2.2 億要求、物が小さい)では 4〜7 分。試運転（`NREQ=2000000`）では 4 トレース × 20 構成が数分。

## 8. 確かめたこと（作成時、WSL）

手元の 4 トレース（Block 3 本と CDN 1 本、`NREQ=2000000`）で `setup → validity → synth → aggregate` を通し、`report.html` と CSV が出ることを確認した。meta は埋め値（size = 2^32−1）を検出して fix 変換し、フットプリントが 19.97 TB（以前の測定の 20.0 TB と一致）になった。CSV を `traceConv` で変換する経路、`SYNTH_SCALED=1` の経路も 200 万要求で通した。**全長のトレースでの所要時間と結果の一致は未確認。**
