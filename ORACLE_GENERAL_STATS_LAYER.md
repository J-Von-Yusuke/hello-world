# oracleGeneral チャンク再生 — 統計層の分離と修正方針

対象: `OracleGeneralReplayGenerator.{h,cpp}`(Producer-Consumer + バッチ化 + チャンク分割版)
関連: [ORACLE_GENERAL_CHUNKED_REPLAY.md](./ORACLE_GENERAL_CHUNKED_REPLAY.md)(前提知識・チャンク層の設計)
成果物: [OracleGeneralCache.h](./OracleGeneralCache.h) / [OracleGeneralCache.cpp](./OracleGeneralCache.cpp)

---

## 0. 結論 — 変更点一覧

| # | 対象 | 種別 | 内容 |
|---|---|---|---|
| 1 | `OracleGeneralReplayGenerator.h` | **バグ修正** | コンパイルエラー 6 件 + キー生成の off-by-one 2 件 |
| 2 | `OracleGeneralCache.{h,cpp}` | **新規** | 統計層(`Stats` / `Adapter` / `ObjAccum` / チャンク境界関数) |
| 3 | `OracleGeneralReplayGenerator.h` | 改修 | バッチ要素を `chunkSize` → `objSize`、Adapter 保持、仮想関数 4 つ追加、統計メンバ削除 |
| 4 | `OracleGeneralReplayGenerator.cpp` | 改修 | **シャードを `objId` で決める**、`sizes[0]` を Adapter 由来にする |
| 5 | `cachebench/CMakeLists.txt` | 改修 | `.cpp` 2 本追加(+ zstd リンク要確認) |
| 6 | `runner/Stressor.cpp` | 改修 | `makeGenerator()` に分岐追加 |

**最重要は #4 の 2 点。** どちらも「測定対象そのものを壊す」種類の問題で、統計を足しても検出できない。

---

## 1. 現行ヘッダの修正必須箇所

### 1.1 コンパイルエラー

| 行 | 現状 | 修正 |
|---|---|---|
| 18 | `#include "cachelib/cachebench/util/Request"` | `.../Request.h` |
| 19 | `#include ".../workload/ReplayGeneratorBase"` | `.../ReplayGeneratorBase.h` |
| 48 | `std::nullpot` | `std::nullopt` |
| 101 | `Request req{key, sizes.begin(), sizes.end, ...}` | `sizes.end()` |
| 126 | `uint32_t c chunkIdx;` | `uint32_t c = chunkIdx;` |
| — | `AtomicCounter` の宣言なし | `#include "cachelib/common/AtomicCounter.h"` |

`getAllKeys()` は **override 不要**。`ReplayGeneratorBase` が `std::logic_error("no keys precomputed!")`
を投げる実装を持っているため、純粋仮想は解決済み(確認済み)。

### 1.2 通るが潜在的に誤る箇所 — キー生成の off-by-one

```cpp
void setKeyFromObjIdChunkAmp(uint64_t objId, uint32_t chunkIdx, uint32_t ampIdx){
    key.resize(31);
    char* p = key.data();
    uint32_t x = ampIdx;
    for(int i = 3; i > 0; i--){ ... }     // ← p[0] が書かれない
    for(int i = 23; i > 4; i--){ ... }    // ← p[4] が書かれない
```

**症状**: `ampIdx` は上位 1 桁、`objId` は上位 1 桁が書き込まれない。
メンバ初期値 `"000000000000000000000000_000000"`(31 文字)がその位置を `'0'` で埋めているため、
**`ampFactor < 1000` かつ `objId < 10^19` の範囲では偶然正しく動く**。
境界を越えるとキーが衝突し、**黙ってヒット率が上がる**(エラーも警告も出ない)。

**修正**:

```cpp
    for(int i = 3; i >= 0; i--){ p[i] = '0' + (x % 10); x /= 10; }
    for(int i = 23; i >= 4; i--){ p[i] = '0' + (objId % 10); objId /= 10; }
```

**併せて**: 初期値は p[24] が `'_'` だが書き込みは `'-'`。どちらかに統一する
(`setKey()` 経由で短い文字列が入ると `resize(31)` が `'\0'` で埋めるため、
初期値に依存した実装自体が脆い。上記修正で全桁を明示的に書けば依存が消える)。

### 1.3 `requestId` について

```cpp
Request req{key, sizes.begin(), sizes.end(), OpType::kGet, 0};
```

5 引数コンストラクタで `requestId` に値が入るため、`CacheStressor` の
`if (req.requestId) { wg_->notifyResult(*req.requestId, result); }` を通過する。
**統計を取るにはこの形が必須**(4 引数版だと `notifyResult` が一度も呼ばれない)。

ただし全リクエストで `0` 固定なので、`PieceWiseReplayGenerator` がやっている
`XCHECK_EQ(requestId, ...)` による順序整合チェックはできない。統計目的なら問題ない。

---

## 2. 層分割の方針

### 2.1 CacheLib の分割線

cachebench には同型の分割が **2 例**ある(独立 2 ソース一致 = 確認済み):

| Generator 側 | Cache 側 |
|---|---|
| `PieceWiseReplayGenerator.{h,cpp}` | `PieceWiseCache.{h,cpp}` |
| `BlockChunkReplayGenerator.{h,cpp}` | `BlockChunkCache.{h,cpp}` |

分割線は **「トレース形式とスレッド機構」** vs **「キャッシュとしての意味論と計測」**。

| | Generator | Cache |
|---|---|---|
| 担当 | CSV/バイナリのパース、`ampFactor_` 展開、`getShard()` 分配、`ProducerConsumerQueue`、producer スレッド | `*CacheStats` / `*ReqWrapper` / `*CacheAdapter` |
| 統計コード | **1 行も持たない** | 全部ここ |
| 依存 | → Cache に依存 | ← Generator を知らない(一方向) |

**Adapter の 2 メソッドが接点のすべて**:

| Adapter | Generator 側の使い方 |
|---|---|
| `recordNewReq(rw)` | `getReq()` 内。新しいトレース行に入った時だけ |
| `processReq(rw, result)` → `bool` | `notifyResult()` 内。**true なら `popFront()`**、false なら同じ front をもう一度返す |

`getReq()` はキューから pop しない。Adapter が「まだ終わっていない」と言う限り同じ要素が返り続ける。
これが **「1 トレース行 → 1+N オペ」の正体**で、Generator は `bool` しか見ていない。

### 2.2 oracleGeneral 版で真似ない点

**`*ReqWrapper` は作らない。**

`PieceWiseReqWrapper` は「1 行が `Metadata → Header → Body` と複数オペにまたがる
**状態機械の状態**」を運ぶ器(かつキューの要素型)。
oracleGeneral は **1 チャンク = 1 GET** で状態機械が存在しないため、形だけ真似ると
空の抽象が 1 枚増えるだけになる。

代わりに置くのは **集計状態** `OracleGeneralObjAccum` — 「N チャンクのうち何個ヒットしたか」を畳む器。

**統計の二重記録もしない。**

`PieceWiseCacheStats` は `InternalStats` を「全体」「ベンチマーク窓」「集約フィールド別」と
複数系統に**二重に書いて**いる。本実装は窓統計も post-warmup 統計も
**スナップショット差分**で出すため、記録側の書き込み経路は 1 本だけ。
ホットパスのコストは `AtomicCounter` の relaxed add のみで、指標を増やしても変わらない。

### 2.3 新ファイルの内容

`OracleGeneralCache.h`:

| 要素 | 役割 |
|---|---|
| `ogNumChunks(objSize, chunkSize)` | **チャンク境界の唯一の定義**。producer と consumer が共有 |
| `ogChunkBytes(objSize, idx, chunkSize)` | 同上。端数チャンクを含む |
| `SizeBucket` | log2 サイズ帯(`bucket b = [2^b, 2^(b+1))`、26 段) |
| `OracleGeneralObjAccum` | 1 オブジェクト分の集計状態。スレッドローカルに置く |
| `OracleGeneralCacheStats` | 計測本体 |
| `OracleGeneralCacheAdapter` | `recordNewObject` / `recordNewChunk` / `processResult` |

---

## 3. 具体的な修正差分

### 3.1 `OracleGeneralReplayGenerator.h`

**(a) バッチ要素のフィールド差し替え**

```diff
 struct OracleGeneralChunkReq {
     uint32_t timestamp{0};
     uint64_t objId{0};
     uint32_t chunkIdx{0};
-    uint32_t chunkSize{1};
+    uint32_t objSize{0};    // chunkSize は ogChunkBytes() で導出できる
 };
```

`chunkSize` は `(objSize, chunkIdx, chunkSize_)` から一意に決まるので冗長。
`objSize` を持たせると consumer がオブジェクト全体の文脈(総チャンク数・サイズ帯)を得られる。
in-memory の構造体サイズは変わらない。

**(b) 統計メンバの移動**

```diff
+#include "cachelib/cachebench/workload/OracleGeneralCache.h"
...
-    std::atomic<uint64_t> nSplit_{0};
-    std::atomic<uint64_t> maxObjSize_{0};
-    std::atomic<uint64_t> nZeroSize_{0};
+    OracleGeneralCacheAdapter cacheAdapter_;
```

**残すもの**(I/O 層の統計なので Generator が持つのが正しい):
`queueProducerWaitCounters_` / `queueConsumerWaitCounters_` / `nReads_` / `decompressedBytes_`

**(c) `ReqCtx` に集計器を追加**

```diff
 struct ReqCtx {
     std::string key{"0000000000000000000000000_000000"};
     std::vector<size_t> sizes{1};
     Request req{key, sizes.begin(), sizes.end(), OpType::kGet, 0};
     OracleGeneralReqBatch batch{};
     bool hasBatch{false};
+    OracleGeneralObjAccum accum{};
     ...
 };
```

**(d) 仮想関数 4 つを追加**

```cpp
void notifyResult(uint64_t, OpResultType result) override {
  cacheAdapter_.processResult(reqCtx_->accum, result);
}

void renderStats(uint64_t ns, std::ostream& out) const override {
  cacheAdapter_.getStats().renderStats(ns, out);
  out << folly::sformat("  io: reads {} decompressed {}B prodWait {} consWait {}",
                        nReads_.load(), decompressedBytes_.load(),
                        queueProducerWaitCounters_.get(),
                        queueConsumerWaitCounters_.get())
      << std::endl;
}

void renderStats(uint64_t ns, folly::UserCounters& c) const override {
  cacheAdapter_.getStats().renderStats(ns, c);
}

void renderWindowStats(double s, std::ostream& out) const override {
  cacheAdapter_.getStats().renderWindowStats(s, out);
}

void setNvmCacheWarmedUp(uint64_t) override { cacheAdapter_.setNvmCacheWarmedUp(); }
```

呼び出し鎖は確認済み:
`Runner::run()` → `Stressor::renderWorkloadGeneratorStats()` → `wg_->renderStats()`。
`renderWindowStats` は `CacheStressor` のコンストラクタで
`[wg = wg_.get()](double s){ wg->renderWindowStats(s, std::cout); }` として
ProgressTracker に登録される。

### 3.2 `OracleGeneralReplayGenerator.cpp`

#### (a) **シャードは `objId` で決める** ← 最重要

```cpp
const uint32_t n = ogNumChunks(rec.objSize, chunkSize_);
char base[24];                                   // amp(4) + objId(20)
formatBaseKey(base, rec.objId, currAmpIdx_);
const uint32_t shard = getShard(folly::StringPiece(base, sizeof(base)));
for (uint32_t i = 0; i < n; i++) {
  auto& b = pendingBatches_[shard];
  b.recs[b.size++] = {rec.timestamp, rec.objId, i, rec.objSize};
  if (b.size == kBatchSize) { flushBatchToShared(shard); }
}
```

**チャンクキーで分配してはいけない理由**(重要度順):

1. **測定対象が壊れる。** チャンクキーで分配すると 1 オブジェクトのチャンクが複数スレッドに散り、
   他スレッドの書き込みと混ざって **別々のリージョンに配置される**。
   リージョン退避の WAF とヒット率が変わってしまい、region-split の効果が
   「配置の偶然」に埋もれる。
2. full-object hit の判定がスレッドローカルで完結する(ロック不要)。
3. オブジェクト内チャンクの時間局所性が保存され、実アクセスパターンに一致する。

`PieceWiseReplayGenerator` も `getShard(key)` を **baseKey** に対して 1 回だけ呼び、
全ピースを同一キューに載せている(構造上そうなっている)。

> 1 オブジェクトのチャンク数が `kBatchSize = 32` を超えるとバッチをまたぐが、
> 同一シャードキューへ順に載るため、消費側の `ReqCtx::accum` は途切れずに続く。

#### (b) `sizes[0]` を Adapter 由来にする

```cpp
const auto& c = ctx.batch.recs[ctx.batch.pos++];
if (c.chunkIdx == 0) {
  cacheAdapter_.recordNewObject(ctx.accum, c.objId, c.objSize);
}
ctx.sizes[0] = cacheAdapter_.recordNewChunk(ctx.accum, c.chunkIdx);
ctx.setKeyFromObjIdChunkAmp(c.objId, c.chunkIdx, ctx.batch.ampIdx);
ctx.req.timestamp = c.timestamp / std::max(timestampFactor_, 1u);
return ctx.req;
```

**発行バイト数と集計バイト数が構造的に一致する**(`ogChunkBytes()` が唯一の定義)。
ここがずれると WAF の分母が狂い、しかも誰も気づかない。

`chunkIdx == 0` が新オブジェクトの合図になる。producer が `idx=0..n-1` を順に発行するため。

### 3.3 `cachelib/cachebench/CMakeLists.txt`

明示リスト方式(glob ではない)。`./workload/BlockChunkCache.cpp` の隣に 2 行:

```cmake
  ./workload/OracleGeneralCache.cpp
  ./workload/OracleGeneralReplayGenerator.cpp
```

zstd のリンクは **要確認**。`cachelib/CMakeLists.txt` に `find_package(Zstd REQUIRED)` はあるが、
`cachelib_cachebench` へ推移的に届くかは未検証。未定義シンボルが出たら:

```cmake
target_link_libraries(cachelib_cachebench PUBLIC
  cachelib_interface cachelib_datatype cachelib_allocator gflags
  ${ZSTD_LIBRARIES})
```

### 3.4 `runner/Stressor.cpp`

```cpp
} else if (config.generator == "oracle-general-replay") {
  return std::make_unique<OracleGeneralReplayGenerator>(config);
```

---

## 4. 指標と研究上の対応

| 指標 | 答える問い |
|---|---|
| `missBytes` | **WAF の分母**。`navy_device_bytes_written / missBytes` = 実測 WAF。<br>Cache 側の値は generator から見えないので、**分母だけ出して外で割る** |
| `chunkHit%` vs `byteHit%` | 乖離 = サイズ偏り。乖離が大きいほど size-aware な扱いの余地がある |
| `objFullHit%` / `objPartialHit%` | **部分残留の総量**。partial が高い = オブジェクトの一部だけリージョン退避で消えている。<br>チャンク化の代償が数字で出る唯一の場所 |
| `head vs tail hit%` の delta | **部分残留の向き**。tail が有意に低ければ後続チャンクが先に落ちている<br>(= リージョン境界にまたがって配置されている)。region-split の直接の効果指標 |
| `-- by object size --` | **split threshold の決定入力**。どのサイズ帯で `fullHit%` が崩れ、<br>どこに `missBytes` が集中しているか |
| `post-warmup` | 冷キャッシュ期間の書き込みは WAF を押し上げる。`setNvmCacheWarmedUp()` 時点の<br>スナップショットとの差分で暖機後だけを見る |
| `objLatency` (p50/p90/p99) | **オブジェクト単位(N オペ)**の実測。cachebench 本体の per-op レイテンシとは別物 |

`renderStats(UserCounters&)` 側では比率を **basis point(万分率)** で出す
(`UserCounters` が整数のため)。スクリプトから拾う場合はこちら。

### 4.1 数え方の約束

- `objGets` は**完了したオブジェクトのみ**カウントする(`recordObject()` は最終チャンクの結果受領時に呼ぶ)。
  run 終了時に途中だったオブジェクトはスレッドあたり最大 1 個落ちる。無視できる。
- `getBytes` はチャンク単位の総和なので、上記の途中オブジェクト分だけ `objGets × 平均サイズ` とずれる。
- `ampFactor > 1` のとき `objGets` はトレースのリクエスト数の `ampFactor` 倍になる。

---

## 5. 検証手順

| # | 内容 | 判定 |
|---|---|---|
| 1 | 単体テスト: 全 `idx` について `Σ ogChunkBytes() == max(objSize,1)` | 完全一致 |
| 2 | 単体テスト: キー生成が `ampIdx=9999`, `objId=10^19` で衝突しないこと | 1.2 の修正確認 |
| 3 | 小トレース再生: `og_get_bytes` == `zstd -dc trace \| 全 objSize の総和 × ampFactor` | 独立 2 ソース一致 |
| 4 | `og_chunks` == `Σ ogNumChunks(objSize) × ampFactor` | 完全一致 |
| 5 | `og_miss_bytes` と `numNvmBytesWritten` から WAF 算出 | **region eviction で WAF ≈ 1** |
| 6 | `head vs tail delta` | 大きく負なら部分残留あり |
| 7 | シャード方式 A/B(objId 分配 vs チャンクキー分配)で WAF を比較 | 3.2(a) の主張の実測確認 |

**#5 が出れば計測系全体が正しく繋がった証拠**になる。WAF が 1 から大きく外れる場合、
分子(Navy の実測)か分母(自前集計)のどちらが誤っているかを #3 で切り分ける。

**#7 は未実施の主張の検証**。3.2(a) の理由 1 はメカニズムからの推論であり、
実測で差が出ることを確認するまでは弱い根拠として扱う。

---

## 6. 出典

| 事実 | 出典 |
|---|---|
| Cache 層 / Generator 層の分割が慣例 | `cachebench/workload/PieceWiseCache.h` および `BlockChunkCache.h`(同型 2 例) |
| `add_library` は明示リスト | `cachelib/cachebench/CMakeLists.txt` |
| `notifyResult` は `if (req.requestId)` ガード付き | `cachebench/runner/CacheStressor.h:406-408` |
| `renderStats` の呼び出し鎖 | `cachebench/runner/Runner.cpp` → `Stressor.h`(`renderWorkloadGeneratorStats`) |
| `renderWindowStats` は ProgressTracker 経由 | `CacheStressor.h:95-97` のラムダ |
| `setNvmCacheWarmedUp(timestamp)` の呼び出し | `CacheStressor.h:530`(`checkNvmCacheWarmedUp` 内) |
| `getAllKeys()` は基底が実装済み | `cachebench/workload/ReplayGeneratorBase.h`(`std::logic_error` を投げる) |
| `getShard()` は strict モードで SpookyHashV2 | 同上 |
| `AtomicCounter` の API(`inc`/`add`/`get`) | `cachelib/common/AtomicCounter.h` |
| `PercentileStats::Estimates` のフィールド | `cachelib/common/PercentileStats.h` |
| `getNvmBytesWritten()` = `navy_device_bytes_written` | `cachebench/cache/Cache.h` |

---

## 7. 未確認事項

1. **ビルド検証をしていない。** `folly::sformat` の書式指定、`util::LatencyTracker` を
   `std::optional` に入れたときのムーブ、`std::vector<Counters>`(`AtomicCounter` を含む)の
   コピー要件でエラーが出る可能性がある。
2. **zstd の推移的リンク**(3.3)。
3. **シャード方式の実測差**(5-#7)。
4. `PieceWiseReplayGenerator` が baseKey でシャードを決めていることは構造からの推論。
   `.cpp` の該当行を直接確認していない。ただし 3.2(a) の理由 1 はそれ自体で成立する。
