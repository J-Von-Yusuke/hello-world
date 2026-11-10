# 別の Ubuntu へ反映する手順

CacheLib **v20240621**(commit `c5c0d9b`)用のパッチです。`main` には当たりません。

## 1. 新しく入れる場合

```bash
scp -r deploy/ user@host:~/cachelib-ru-deploy
ssh user@host
cd ~/cachelib-ru-deploy && chmod +x install.sh && ./install.sh ~/cachelib-ru
```

clone → submodule(全履歴)→ パッチ適用 → `contrib/build.sh -j -T`(依存込み 30〜90 分)。

## 2. 既に v20240621 をビルド済みの場合

```bash
./install.sh --existing /path/to/CacheLib
```

パッチを当てて `cachebench` だけを再ビルドします(数分)。既に当たっていれば何もしません。HEAD が `c5c0d9b` でなければ止まります。

## 3. 動作確認(メモリ上のデバイス。SSD に負荷をかけない)

```bash
export LD_LIBRARY_PATH=~/cachelib-ru/opt/cachelib/lib:~/cachelib-ru/opt/cachelib/lib64
~/cachelib-ru/build-cachelib/cachebench/cachebench \
  --json_test_config sample_ru.json --stop_ops 2000000 \
  --progress_ops 1000000 --progress_ops_stats_file /tmp/op.log
```

`sample_ru.json` は `/dev/shm/navy.dev` を使います。実 SSD で測るときは `nvmCachePaths` を書き換えてください。

## 3b. 機能がすべて動くかの確認(`verify.sh`)

ケースごとに cachebench を 1 回ずつ走らせます。デバイスに STOP バイト書いたら止め、各機能が動いた証拠になるカウンタ(SR reclaim、PID、RUH Update、Trim、Nemo など)を PASS / FAIL で判定します。統計は 3 軸で出ます(`bytes.log` / `ops.log` / `time.log`)。

```bash
# 実 NVMe: NVM 15GiB、30GB 書いたら停止、15GiB でウォームアップ(既定値)
sudo ./verify.sh -L ~/cachelib-ru -d /dev/nvme0n1

# FDP SSD を実際に使う / 自分の oracleGeneral トレース / CacheLib 付属の workload
sudo ./verify.sh -d /dev/nvme0n1 --fdp on
sudo ./verify.sh -d /dev/nvme0n1 -T /data/trace.oracleGeneral.zst --repeat-trace
sudo ./verify.sh -d /dev/nvme0n1 -W cachelib:ssd_perf/kvcache_l2_wc   # 一覧: --list-workloads

# SSD を使わずに、メモリ上で短く確認
./verify.sh -d /dev/shm/navy.dev -c 2GiB -m 256MiB -s 3GB -w 1GiB --sim-ru 64MiB
```

| ケース | 中身 | 主な判定 |
|---|---|---|
| base | 元の CacheLib(比較の基準) | 正常終了、3 軸のログ |
| sr | PID ごとの SR、サイズで PID、II 再挿入、LRU | `sr_reclaims`、2 つ以上の PID、`ruh_updates` |
| sfifo | sr + SFIFO + II のヒット段 + 容量下限 | SFIFO+下限の選択器が有効 |
| noruh | sr で RUH Update off | `ruh_updates = 0` |
| trim | sr で Trim on | `device_trims > 0` |
| log / nemo | sr + BigHash log / Nemo | `log_ru_evictions` / `nemo_flushes` |

- 書き込みの総量は「ケース数 × STOP」です(既定 7 × 30GB)。`--cases "base sr"` で絞れます。
- ブロックデバイスは中身を全部上書きします。始める前にデバイスパスの入力を求めます(`--yes` で省略)。マウント中なら止まります。
- `--fresh` を付けると、各ケースの前に `blkdiscard` でデバイスを空にします。前のケースの GC を持ち越さないためです。
- nvme-cli があると、`nvme fdp stats` の前後差からケースごとの WAF(media / host)を出します(取れない機種では出ません)。
- 結果は `OUT/summary.tsv`(hit 率、NVM hit、byteMiss、書込バイト、WAF)、詳細は `OUT/<case>/`。
- `-n` を付けると、設定 JSON とコマンドを作るだけで、実行はしません。

## 4. FDP SSD で使うとき

NavyConfig の FDP を有効にし(cachebench: `"deviceEnableFDP": true`、io_uring が必要)、
デバイスを `nvmCachePaths` に指定します。起動ログの `FDP: N placement handles ... RU X bytes`
で読み出せた値を確認してください。

- PID: handle 0 = BigHash、1〜N−1 = BlockCache(自動)
- SR の大きさ = RU(`navySuperRegionFromDevice`、既定 true)
- RUH Update: `navyRuhUpdate`(既定 true。false にするとデバイス GC を観察できる)
- Trim: `navyTrimOnReclaim`(既定 false)

FDP SSD がない環境では `navySimFdpPlacementHandles` / `navySimFdpRuSizeMB` で模擬できます。
ioctl 部分は実機では未確認です。

詳しい機能の説明は `../ARCHITECTURE.html`。
