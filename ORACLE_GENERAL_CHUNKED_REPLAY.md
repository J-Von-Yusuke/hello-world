# oracleGeneral トレースのチャンク化再生 — 前提知識と実装手順

CacheLib cachebench に、libCacheSim 形式(zstd 圧縮 oracleGeneral バイナリ)のトレースを
**piecewise 方式のチャンク化を伴って**再生するジェネレータを追加する。

- **対象**: DRAM + SSD(Navy BlockCache)ハイブリッド構成での SSD 負荷／WAF 測定
- **トレース**: KV / CDN / Block の 3 種類
- **前提**: BigHash 無効、BlockCache エンジンのみ
- **作成**: 2026-08-03 / 実装未着手

---

## 0. 結論(先に読む)

| 項目 | 内容 |
|---|---|
| 最大の制約 | CacheLib のアイテムは **4 MiB(`Slab::kSize`)を超えられない**。ハイブリッドでも DRAM 経由が必須なので、これが**キャッシュ全体の天井** |
| 超えたとき | `std::invalid_argument` が飛び、cachebench が **DBG ログで握り潰して null を返す**。警告なしの恒久ミスになる |
| 解決 | piecewise と同じく **オブジェクト全体を allocate しない**。固定長チャンクに分割して 1 レコード → N リクエストに展開する |
| 分割の閾値 | **4 MiB ではなく `cachePieceSize`(既定 64 KiB)**。理由は §2.2 |
| 変更箇所 | 新規ヘッダ 1 本 + `Stressor.cpp` に 2 行 + CMake に zstd リンク |

> **用語の注意**: 「4 MiB を超えないように分割する」は方向としては正しいが、
> 実際の分割基準は 4 MiB より **64 倍細かい 64 KiB** である。
> 4 MiB チャンクにすると 16 MiB リージョンに 4 個しか入らず、
> リージョン追い出しの粒度が粗くなりすぎて region-split の評価対象そのものが歪む(§5.1)。

---

# 第 1 部 前提知識

## 1.1 oracleGeneral 形式

libCacheSim の `oracleGeneralBin` リーダが読む固定長バイナリ。**24 バイト・パック済み・リトルエンディアン**。

```c
struct {           // オフセット
  uint32_t timestamp;          //  0  (4B) 秒
  uint64_t obj_id;             //  4  (8B) 変換時に振られた通し ID
  uint32_t obj_size;           // 12  (4B) バイト
  int64_t  next_access_vtime;  // 16  (8B) -1 または INT64_MAX = 再アクセスなし
};                 // 合計 24B
```

ファイル全体がこのレコードの単純な連結で、**区切り文字は存在しない**。
libCacheSim 側では `-1` と `INT64_MAX` の両方を「再アクセスなし」の番兵として扱い、
`MAX_REUSE_DISTANCE` に正規化している。

### 落とし穴 1: 構造体パディング

上の struct を C++ でそのまま宣言すると、多くの ABI で **32 バイト**になる
(`uint32_t` の後ろに 4B、`uint32_t obj_size` の後ろにも 4B の詰め物が入る)。
1 レコード目は偶然読めても 2 レコード目以降が全部ずれる。

```cpp
#pragma pack(push, 1)
struct OracleGeneralRecord { ... };
#pragma pack(pop)
static_assert(sizeof(OracleGeneralRecord) == 24, "record must be 24B packed");
```

`static_assert` は必須。ビルド時に落ちるので、実行時のデバッグを丸ごと回避できる。

### 落とし穴 2: 同一 obj_id でサイズが変わる

トレースによっては同じ `obj_id` が異なる `obj_size` で再出現する
(tencentBlock_11784 では約 27% のリクエストが該当)。両者の挙動:

- **libCacheSim**: `cache_find_base` はサイズ差を**完全に無視**する(更新するのは `next_access_vtime` と `freq` のみ)
- **CacheLib**: HIT 時は最初に挿入した古いサイズのアイテムをそのまま返す

どちらも「初回サイズを保持する」点で偶然一致するため比較は成立するが、
これは設計上の保証ではなく実装の副作用。**レポートに前提として明記すること**。

## 1.2 zstd ストリーミング解凍

トレースは zstd 圧縮されている(w01: 215M req ≒ 5.2 GB raw、tencentBlock: 51M req ≒ 1.2 GB raw)。
事前に全展開するとディスクを食うので、`ZSTD_decompressStream` でストリーミング解凍する。

libCacheSim の `zstdReader.c` が採る構成をそのまま踏襲する:

- 入力バッファ `ZSTD_DStreamInSize()`、出力バッファ `ZSTD_DStreamOutSize()`
- 出力バッファが尽きたら `fread` で入力を補充して `ZSTD_decompressStream` を再度回す
- **レコードが出力バッファ境界をまたぐケースの継ぎ足しが必須**(これが実装の核心)

`ZSTD_decompressStream` は出力 0 バイトで返ることがある(フレーム境界、入力不足)。
その場合はループを継続して入力を供給し続ける。物理 EOF でのみ終了と判定する。

## 1.3 CacheLib のアイテムサイズ上限 — 4 MiB の壁

```cpp
// cachelib/allocator/memory/Slab.h
static constexpr unsigned int kNumSlabBits = 22;
static constexpr size_t kSize = 1 << kNumSlabBits;   // 4,194,304 = 4 MiB
```

`MemoryAllocator` のドキュメントに *"provides allocation by any size up to Slab::kSize"* とあり、
`allocate()` は *"@throw std::invalid_argument if the poolId is invalid or **the size is invalid**"* と明記されている。
つまり**サイズ超過は nullptr ではなく例外**。

問題は cachebench 側の受け止め方:

```cpp
// cachelib/cachebench/cache/Cache-inl.h
try {
  handle = cache_->allocate(pid, key, CacheValue::getSize(size), ttlSecs);
  if (handle) { CacheValue::initialize(handle->getMemory()); }
} catch (const std::invalid_argument& e) {
  XLOGF(DBG, "Unable to allocate, reason: {}", e.what());   // ← DBG は既定で出ない
}
return handle;   // null が返るだけ
```

さらに `CacheStressor::setKey()` は:

```cpp
auto it = cache_->allocate(pid, key, size, ttlSecs);
if (it == nullptr) { ++stats.setFailure; return OpResultType::kSetFailure; }
```

`setFailure` は増えるが、**なぜ失敗したかは記録されない**。
結果として 4 MiB 超のオブジェクトは、エラーも警告も出さずに**永久にミスし続ける**。

### SSD 負荷試験にとってなぜ致命的か

本来最も多くのバイトを書き込むはずの上位テールが、**書き込みを一切発生させない**。
WAF の分子(実書込量)と分母(論理書込量)が同時に欠落するため、
**WAF は一見もっともらしい値のまま**になり、異常に気づく手がかりが残らない。

## 1.4 ハイブリッド構成での実効上限

| 層 | 上限 | 根拠 |
|---|---|---|
| DRAM | **4 MiB** | `Slab::kSize` |
| Navy BlockCache | 16 MiB − 32 B | `getMaxItemSize() = regionSize_ - sizeof(EntryDesc)`(既定 `regionSize` = 16 MiB) |
| Navy 理論値 | 約 33.5 MiB | `kMaxItemSize = kMinAllocAlignSize(512) * UINT16_MAX` |

**アイテムは必ず DRAM に割り当てられてから NVM へ落ちる**ため、
BlockCache 側がいくら大きくても **4 MiB が全体の天井**になる。
チャンクサイズを 4 MiB 超に設定することはできない。

### BigHash 無効構成での確認事項

- `navyBigHashSizePct` は**既定で 0(無効)**。明示設定は不要だが、意図を残すため `0` と書いておく
- 全アイテムが BlockCache へ行く。BlockCache の割り当ては **512 B アライン**なので、
  チャンク末尾の端数に最大 511 B のパディングが乗る(64 KiB チャンクなら 0.8% 未満)
- このパディングは WAF の系統誤差になるので、値を記録しておく

## 1.5 piecewise がこの壁を回避している仕組み

CDN トレースには数百 MiB のコンテンツが大量に含まれるが、piecewise は壊れない。
理由は `PieceWiseReqWrapper` のコンストラクタにある:

```cpp
if (fullContentSize < cachePieceSize) {
    sizes[0] = fullContentSize + responseHeaderSize;   // 丸ごと。ただし必ず < 64KiB
} else {
    cachePieces = std::make_unique<GenericPieces>(baseKey, cachePieceSize, ...);
    updatePieceKey(baseKey);
    pieceType = PieceType::Metadata;
    sizes[0] = metadataSize;      // 以降 header → body と片単位で更新
}
// body 片: rw.sizes[0] = rw.cachePieces->getSizeOfAPiece(nextPieceIndex);
```

**`fullObjectSize` が `cache_->allocate()` の引数になる経路が存在しない。**
トレースの `OBJECT_SIZE` 列は帳簿上の値として保持されるだけで、
実際に割り当てられるのは `sizes[0]` のみ。その上限は常に `cachePieceSize` に固定される。

```
32 KiB のオブジェクト  →  allocate ×1      各 32 KiB
512 KiB のオブジェクト →  allocate ×8      各 64 KiB
2 GiB のオブジェクト   →  allocate ×32768  各 64 KiB
                          ↑ どの行も 64 KiB で頭打ち = 4 MiB の壁に近づかない
```

関連する既定値も辻褄が合っている:

```cpp
uint64_t cachePieceSize{65536};   // 64 KiB
uint64_t maxCachePieces{32000};   // 32000 * 64KiB = 2 GiB / コンテンツ
```

`maxCachePieces` 超過分はキャッシュせず ingress(オリジン直通)として計上される。
`kCachePieceGroupSize = 16 MiB` はキー命名のグループ単位なので `cachePieceSize ≤ 16 MiB` が要求され、
実用上は自然に 4 MiB 未満に収まる。

## 1.6 cachebench のジェネレータ・インターフェース

```cpp
// GeneratorBase の純粋仮想 — サブクラスは必ず実装
virtual const Request& getReq(uint8_t poolId, std::mt19937_64& gen,
                              std::optional<uint64_t> lastRequestId) = 0;
virtual const std::vector<std::string>& getAllKeys() const = 0;

// 既定実装あり(任意で override)
virtual void notifyResult(uint64_t requestId, OpResultType result) {}
virtual void markFinish() {}
virtual void renderStats(...) const {}
```

`ReplayGeneratorBase` を継承すると、以下が自動的に設定から吸い上げられる:

```cpp
repeatTraceReplay_  ampFactor_  ampSizeFactor_
fastForwardCount_   preLoadReqs_  binaryFileName_
timestampFactor_    numShards_(= numThreads)  mode_
```

### notifyResult は条件付きで呼ばれる

```cpp
// CacheStressor.h
lastRequestId = req.requestId;
if (req.requestId) {                       // ← nullopt なら呼ばれない
  wg_->notifyResult(*req.requestId, result);
}
```

バイト単位ミス率を自前で集計するには **`Request` の `requestId` を必ず設定する**必要がある。
5 引数コンストラクタ `Request(key, begin, end, op, reqId)` を使えばよい。

### ジェネレータの登録

```cpp
// cachelib/cachebench/runner/Stressor.cpp :: makeGenerator()
if (config.generator == "piecewise-replay") { ... }
else if (config.generator == "replay") { ... }
else if (config.generator == "binary-replay") { ... }
// ↑ ここに 1 分岐追加するだけ
```

## 1.7 op を持たないトレースの写像

oracleGeneral には操作種別のフィールドがない(全て読み出し)。
cachebench の GET/SET/DEL モデルへは **`kGet` + `enableLookaside: true`** で写像する。

```cpp
// CacheStressor.h — kGet のミス時
if (it == nullptr) {
  ++stats.getMiss;
  result = OpResultType::kGetMiss;
  if (config_.enableLookaside) {
    slock = {};
    xlock = chainedItemAcquireUniqueLock(key);
    setKey(pid, stats, key, *(req.sizeBegin), req.ttlSecs, ...);   // 同サイズで挿入
  }
}
```

これは libCacheSim の insert-on-miss と等価。
なお lookaside 経路の `setKey()` の戻り値は破棄されるため、
`notifyResult` に渡る `result` は **`kGetMiss` のまま**である(ミスバイト集計はこれを使う)。

---

# 第 2 部 設計

## 2.1 不変条件

> **`cache_->allocate()` に渡る値は、常に `chunkSize` 以下である。**

この 1 行を守れば、4 MiB の壁に起因する障害クラスが**丸ごと消滅**する。
コンストラクタで `chunkSize ≤ 4 MiB − α` を検証すれば、
実行時の「サイズ超過による静かな失敗」は原理的に発生しなくなる。

## 2.2 チャンクサイズの選び方

**4 MiB 直下ではなく 64 KiB を既定とする。** 理由:

| 観点 | 4 MiB チャンク | 64 KiB チャンク |
|---|---|---|
| 16 MiB リージョンあたりのアイテム数 | **4 個** | 256 個 |
| リージョン追い出し 1 回の巻き添え | 極端に大きい | 制御可能 |
| 512 B アラインのパディング損失 | 0.01% | 0.8% 未満 |
| piecewise / CDN 実運用との整合 | なし | **既定値と一致** |

region-split 研究はリージョン内のアイテム構成が主題なので、
リージョンあたり 4 個では**評価対象そのものが成立しない**。

## 2.3 フィールド写像

| oracleGeneral | cachebench 側 | 備考 |
|---|---|---|
| `timestamp` (u32) | `req.timestamp`(`timestampFactor` で除算) | 時刻依存機能を使わない限り参考値 |
| `obj_id` (u64) | キーの前半 20 桁ゼロ埋め | 固定長にしてフットプリントを均一化 |
| (チャンク番号) | キーの後半 6 桁ゼロ埋め | 最大 999,999 チャンク = 64 KiB × 100万 ≒ 64 GiB/オブジェクト |
| `obj_size` (u32) | N 個の `sizes[0]` に分割(末尾は端数) | 0 は 1 にクランプ |
| (op なし) | 常に `kGet` + `enableLookaside: true` | insert-on-miss |
| `next_access_vtime` | **破棄** | Belady 系専用。cachebench に消費者なし |

キー長は `20 + 1 + 6 = 27` 文字で固定。アイテムの実割当は
`chunkSize + キー 27 B + アイテムヘッダ 32 B + アロケーションクラス丸め`。

## 2.4 piecewise との意図的な相違

piecewise は「**一度 SET に入ったら以降の片も全部 SET**」という連鎖挿入を行う
(オリジンから連続取得している状況の模擬)。

**本実装ではこれを採用しない。** チャンクごとに独立した GET + lookaside とする。

理由: 独立 GET にしておくと、**トレースをオフラインで分割した結果と完全に等価**になる(§4.4)。
これにより libCacheSim との厳密照合が成立し、実装 A(ジェネレータ内分割)と
実装 B(オフライン分割)の一致をもって A の正しさを検証できる。

## 2.5 記録すべき系統誤差

比較レポートに必ず明記する:

1. **アイテムオーバーヘッド**: CacheLib の実割当 = `chunkSize + 27 + 32 + 丸め`。libCacheSim は `obj_size` のみ
2. **512 B アライン**: BlockCache 内のパディング(64 KiB チャンクで 0.8% 未満)
3. **サイズ変化**: 同一 `obj_id` のサイズ変化は初回値を採用(両者一致、ただし偶然)
4. **チャンク末尾**: 最終チャンクは端数サイズ。パディングせず実サイズで割り当てる

---

# 第 3 部 実装

## 3.1 変更箇所(3 箇所)

| ファイル | 変更 |
|---|---|
| `cachelib/cachebench/workload/OracleGeneralReplayGenerator.h` | **新規作成**(header-only、CMake のソースリスト変更不要) |
| `cachelib/cachebench/runner/Stressor.cpp` | `makeGenerator()` に 2 行 + include 1 行 |
| `cachelib/cachebench/CMakeLists.txt` | zstd リンク(必要時のみ) |

`Config.h` / `Config.cpp` は**無改修**。既存キー `traceFileName` / `cachePieceSize` /
`enableLookaside` / `repeatTraceReplay` / `fastForwardCount` を流用する。

zstd は CacheLib のルート CMake が既に `find_package(Zstd REQUIRED)` しているため、
新規の外部依存は発生しない。

## 3.2 新規ヘッダ

`cachelib/cachebench/workload/OracleGeneralReplayGenerator.h`

```cpp
#pragma once

#include <zstd.h>

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include <fmt/core.h>
#include <folly/ThreadLocal.h>
#include <folly/logging/xlog.h>

#include "cachelib/cachebench/util/Exceptions.h"
#include "cachelib/cachebench/util/Request.h"
#include "cachelib/cachebench/workload/ReplayGeneratorBase.h"

namespace facebook {
namespace cachelib {
namespace cachebench {

// ---------------------------------------------------------------- record
// libCacheSim oracleGeneral: 24B fixed-size packed little-endian records
#pragma pack(push, 1)
struct OracleGeneralRecord {
  uint32_t timestamp;
  uint64_t objId;
  uint32_t objSize;
  int64_t nextAccessVtime;  // -1 / INT64_MAX = no next access (unused here)
};
#pragma pack(pop)
static_assert(sizeof(OracleGeneralRecord) == 24, "record must be 24B packed");

// ---------------------------------------------------------------- zstd
// Streaming zstd reader yielding fixed-size records. Never materializes the
// decompressed trace on disk or in memory.
class ZstdRecordStream {
 public:
  explicit ZstdRecordStream(std::string path) : path_(std::move(path)) {
    open();
  }
  ~ZstdRecordStream() { closeAll(); }

  ZstdRecordStream(const ZstdRecordStream&) = delete;
  ZstdRecordStream& operator=(const ZstdRecordStream&) = delete;

  void rewind() {
    closeAll();
    open();
  }

  // false = clean EOF at a record boundary; throws on a truncated record
  bool readRecord(OracleGeneralRecord& rec) {
    size_t got = 0;
    auto* dst = reinterpret_cast<char*>(&rec);
    while (got < sizeof(rec)) {
      if (outPos_ == outEnd_ && !refill()) {
        if (got == 0) {
          return false;
        }
        throw std::runtime_error("oracleGeneral: truncated record at EOF");
      }
      // a record may straddle the output-buffer boundary
      const size_t take = std::min(sizeof(rec) - got, outEnd_ - outPos_);
      std::memcpy(dst + got, outBuf_.data() + outPos_, take);
      outPos_ += take;
      got += take;
    }
    return true;
  }

 private:
  void open() {
    f_ = std::fopen(path_.c_str(), "rb");
    if (f_ == nullptr) {
      throw std::invalid_argument("cannot open trace: " + path_);
    }
    zds_ = ZSTD_createDStream();
    ZSTD_initDStream(zds_);
    inBuf_.resize(ZSTD_DStreamInSize());
    outBuf_.resize(ZSTD_DStreamOutSize());
    zin_ = {inBuf_.data(), 0, 0};
    outPos_ = outEnd_ = 0;
  }

  void closeAll() {
    if (zds_ != nullptr) {
      ZSTD_freeDStream(zds_);
      zds_ = nullptr;
    }
    if (f_ != nullptr) {
      std::fclose(f_);
      f_ = nullptr;
    }
  }

  bool refill() {
    while (true) {
      if (zin_.pos == zin_.size) {
        const size_t rd = std::fread(inBuf_.data(), 1, inBuf_.size(), f_);
        if (rd == 0) {
          return false;  // physical EOF
        }
        zin_ = {inBuf_.data(), rd, 0};
      }
      ZSTD_outBuffer zout{outBuf_.data(), outBuf_.size(), 0};
      const size_t ret = ZSTD_decompressStream(zds_, &zout, &zin_);
      if (ZSTD_isError(ret)) {
        throw std::runtime_error(std::string("zstd: ") +
                                 ZSTD_getErrorName(ret));
      }
      if (zout.pos > 0) {
        outPos_ = 0;
        outEnd_ = zout.pos;
        return true;
      }
      // zero output: frame boundary or starved input -- keep feeding
    }
  }

  std::string path_;
  std::FILE* f_{nullptr};
  ZSTD_DStream* zds_{nullptr};
  std::vector<char> inBuf_, outBuf_;
  ZSTD_inBuffer zin_{};
  size_t outPos_{0}, outEnd_{0};
};

// ---------------------------------------------------------------- generator
class OracleGeneralReplayGenerator : public ReplayGeneratorBase {
 public:
  // DRAM slab (4MiB) is the ceiling for the whole hybrid cache; leave room
  // for the key (27B) and the item header (32B).
  static constexpr size_t kMaxChunkSize = 4ull * 1024 * 1024 - 1024;
  static constexpr uint32_t kMaxChunkIdx = 999999;  // key uses 6 digits

  explicit OracleGeneralReplayGenerator(const StressorConfig& config)
      : ReplayGeneratorBase(config),
        stream_(config.traceFileName),
        chunkSize_(config.cachePieceSize) {
    if (chunkSize_ == 0 || chunkSize_ > kMaxChunkSize) {
      throw std::invalid_argument(
          fmt::format("cachePieceSize must be in (0, {}]; got {}",
                      kMaxChunkSize, chunkSize_));
    }
    OracleGeneralRecord rec;
    for (uint64_t i = 0; i < fastForwardCount_; i++) {
      if (!stream_.readRecord(rec)) {
        throw std::invalid_argument("fastForwardCount exceeds trace length");
      }
    }
    XLOGF(INFO, "oracleGeneral replay: file={} chunkSize={} fastForward={}",
          config.traceFileName, chunkSize_, fastForwardCount_);
  }

  const Request& getReq(uint8_t,
                        std::mt19937_64&,
                        std::optional<uint64_t>) override {
    auto& ctx = *reqCtx_;
    if (ctx.remaining == 0) {
      nextRecord(ctx);
    }
    const size_t sz = std::min(ctx.remaining, chunkSize_);
    ctx.updateKey(fmt::format("{:020d}_{:06d}", ctx.baseId, ctx.chunkIdx));
    ctx.sizes[0] = sz;   // INVARIANT: always <= chunkSize_ <= 4MiB - 1KiB
    ctx.lastSize = sz;
    ctx.remaining -= sz;
    ++ctx.chunkIdx;
    nChunks_.fetch_add(1, std::memory_order_relaxed);
    return ctx.req;
  }

  void notifyResult(uint64_t, OpResultType result) override {
    if (result == OpResultType::kGetMiss) {
      auto& ctx = *reqCtx_;
      missBytes_.fetch_add(ctx.lastSize, std::memory_order_relaxed);
      nMissChunks_.fetch_add(1, std::memory_order_relaxed);
    }
  }

  const std::vector<std::string>& getAllKeys() const override {
    throw std::logic_error("OracleGeneralReplayGenerator has no keys");
  }

  void markFinish() override { logSummary("FINAL"); }

 private:
  struct ReqCtx {
    std::string key = std::string(27, '0');
    std::vector<size_t> sizes{1};
    // 5-arg ctor sets requestId -> notifyResult() will be invoked
    Request req{key, sizes.begin(), sizes.end(), OpType::kGet,
                reinterpret_cast<uint64_t>(this)};
    uint64_t baseId{0};
    size_t remaining{0};
    size_t lastSize{0};
    uint32_t chunkIdx{0};
    void updateKey(std::string k) {
      key = std::move(k);
      req.key = key;   // string_view must be re-pointed after assignment
    }
  };

  void nextRecord(ReqCtx& ctx) {
    OracleGeneralRecord rec;
    {
      std::lock_guard<std::mutex> lg(lock_);
      if (!stream_.readRecord(rec)) {
        if (!repeatTraceReplay_) {
          throw cachelib::cachebench::EndOfTrace("oracleGeneral EOF");
        }
        XLOG_EVERY_MS(INFO, 60'000) << "oracleGeneral EOF; restarting";
        stream_.rewind();
        if (!stream_.readRecord(rec)) {
          throw cachelib::cachebench::EndOfTrace("empty trace");
        }
      }
    }

    const size_t sz = std::max<uint32_t>(rec.objSize, 1u);
    if ((sz + chunkSize_ - 1) / chunkSize_ > kMaxChunkIdx) {
      throw std::runtime_error(
          fmt::format("object {} too large for 6-digit chunk index (size={})",
                      rec.objId, sz));
    }
    if (rec.objSize == 0) {
      nZeroSize_.fetch_add(1, std::memory_order_relaxed);
    }
    if (sz > chunkSize_) {
      nSplit_.fetch_add(1, std::memory_order_relaxed);
    }
    const uint64_t n = nRecords_.fetch_add(1, std::memory_order_relaxed);
    totalBytes_.fetch_add(sz, std::memory_order_relaxed);
    updateMax(maxObjSize_, sz);

    if (n < 5) {
      XLOGF(INFO, "rec[{}] ts={} id={} sz={} nvt={}", n, rec.timestamp,
            rec.objId, rec.objSize, rec.nextAccessVtime);
    }
    if (n % 1'000'000 == 0 && n > 0) {
      logSummary("PROGRESS");
    }

    ctx.baseId = rec.objId;
    ctx.remaining = sz;
    ctx.chunkIdx = 0;
    ctx.req.timestamp = rec.timestamp / std::max<uint64_t>(timestampFactor_, 1);
  }

  static void updateMax(std::atomic<uint64_t>& tgt, uint64_t v) {
    uint64_t cur = tgt.load(std::memory_order_relaxed);
    while (v > cur && !tgt.compare_exchange_weak(cur, v,
                                                 std::memory_order_relaxed)) {
    }
  }

  void logSummary(const char* tag) const {
    const uint64_t tb = totalBytes_.load(), mb = missBytes_.load();
    const uint64_t nc = nChunks_.load(), nm = nMissChunks_.load();
    XLOGF(INFO,
          "[{}] records={} chunks={} split={} zeroSize={} maxObjSize={} "
          "bytes={} byteMissRatio={:.4f} chunkMissRatio={:.4f}",
          tag, nRecords_.load(), nc, nSplit_.load(), nZeroSize_.load(),
          maxObjSize_.load(), tb,
          tb ? static_cast<double>(mb) / static_cast<double>(tb) : 0.0,
          nc ? static_cast<double>(nm) / static_cast<double>(nc) : 0.0);
  }

  ZstdRecordStream stream_;
  const size_t chunkSize_;
  std::mutex lock_;

  std::atomic<uint64_t> nRecords_{0}, nChunks_{0}, nMissChunks_{0};
  std::atomic<uint64_t> totalBytes_{0}, missBytes_{0};
  std::atomic<uint64_t> nZeroSize_{0}, nSplit_{0}, maxObjSize_{0};

  folly::ThreadLocal<ReqCtx> reqCtx_;
};

}  // namespace cachebench
}  // namespace cachelib
}  // namespace facebook
```

### 実装メモ

- **producer スレッド構成を採らない理由**: `KVReplayGenerator` は生成スレッド +
  シャード別キューを持つが、研究用途は `numThreads: 1` の厳密再生なので、
  mutex + `folly::ThreadLocal` の直接読みが最小コードで、かつ順序が完全に決定的になる。
  `numThreads > 1` でも壊れないが、スレッド間の順序は OS スケジューラ依存になる。
- **チャンク状態はスレッドローカル**なので、同一オブジェクトのチャンクは
  同じスレッドに留まる(piecewise のスティッキーシャーディングと同じ性質)。
- **`req.key` の再設定**: `Request::key` は `std::string_view`。`key` を代入し直したら
  必ず `req.key = key;` で貼り直す(`KVReplayGenerator::ReqWrapper::updateKey` と同じ作法)。
- **`sizes` は絶対にリサイズしない**。`Request` が `sizes.begin()/end()` の
  イテレータを保持しているため。
- `markFinish()` / `renderStats()` のシグネチャは CacheLib のバージョンで揺れうる。
  ビルドが通らなければ `override` を外し、デストラクタで `logSummary("FINAL")` を呼ぶ形に落とす。

## 3.3 Stressor.cpp

```cpp
#include "cachelib/cachebench/workload/OracleGeneralReplayGenerator.h"

// makeGenerator() 内、"binary-replay" 分岐の後に
} else if (config.generator == "oracle-general-replay") {
  return std::make_unique<OracleGeneralReplayGenerator>(config);
```

## 3.4 CMake(必要時のみ)

リンクエラーが出た場合のみ、`cachelib/cachebench/CMakeLists.txt` の
`target_link_libraries(cachelib_cachebench PUBLIC ...)` に zstd を追加する。
変数名は `cachelib/cmake/FindZstd.cmake` を参照。folly が推移的にリンクしていれば不要。

## 3.5 設定 JSON

```json
{
  "cache_config": {
    "cacheSizeMB": 8192,
    "nvmCacheSizeMB": 131072,
    "nvmCachePaths": ["/dev/nvme0n1"],
    "navyBigHashSizePct": 0,
    "navyBlockSize": 4096,
    "navyRegionSizeMB": 16,
    "poolRebalanceIntervalSec": 0
  },
  "test_config": {
    "generator": "oracle-general-replay",
    "traceFileName": "/data/w01.oracleGeneral.bin.zst",
    "cachePieceSize": 65536,
    "enableLookaside": true,
    "numThreads": 1,
    "numOps": 100000000000,
    "repeatTraceReplay": false,
    "timestampFactor": 1,
    "replayGeneratorConfig": { "fastForwardCount": 0 }
  }
}
```

```bash
./cachebench --json_test_config oracle_replay.json --progress_stats_file=/tmp/oracle.log
```

- `numOps` は**スレッドあたりの上限**。チャンク化で op 数がレコード数より増えるため、
  十分大きく設定してトレース終端(`EndOfTrace`)で自然停止させる
- `navyBigHashSizePct: 0` は既定値と同じだが、意図を残すため明示する
- **CacheLib は Windows でビルドできない**。WSL2 (Ubuntu) 上で `./contrib/build.sh -j -T`

---

# 第 4 部 検証手順

## 4.1 仮説

新規のリスクは 3 点に局在する。それ以外(Stressor 以降)は無改修で正しく動く。

1. zstd ストリーミング解凍(バッファ境界をまたぐレコード)
2. 24 B レコードの解釈(パディング)
3. チャンク分割の境界(端数チャンク、チャンク数)

## 4.2 計測ログ(実装済み)

上記コードに以下を組み込み済み:

| 出力 | 何を検出するか |
|---|---|
| 冒頭 5 レコードのフィールドダンプ | パディング事故、エンディアン |
| 100 万レコードごとの `PROGRESS` | 進行状況、途中での異常 |
| `split=` | チャンク分割が発生した件数(0 なら分割層が働いていない) |
| `maxObjSize=` | トレースの最大オブジェクトサイズ(4 MiB 超の有無) |
| `zeroSize=` | サイズ 0 レコードの件数 |
| `byteMissRatio` / `chunkMissRatio` | 自前集計の指標(標準統計は個数のみ) |

## 4.3 事前確認 — トレースのサイズ分布

**CDN トレースは、既にチャンク化済みのものとオブジェクト単位のものが混在する。**
`cachePieceSize` を決める前に必ず確認する。

```bash
zstd -dc cdn.oracleGeneral.zst | python3 -c "
import sys,struct
d=sys.stdin.buffer.read(); n=len(d)//24
sz=[struct.unpack_from('<IQIq',d,i*24)[2] for i in range(n)]
tot=sum(sz); over=[s for s in sz if s>4*1024*1024]
print(f'reqs={n}  max={max(sz)}  mean={tot//n}')
print(f'>4MiB: {len(over)} reqs ({100*len(over)/n:.3f}%), {100*sum(over)/tot:.2f}% of bytes')
for p in (50,90,99,99.9): print(f'  p{p} = {sorted(sz)[int(n*p/100)]}')
"
```

- **最大が 64 KiB 前後で頭打ち** → 既にチャンク化済み。`cachePieceSize` を
  最大値より大きく取れば分割層は素通りし、二重分割を避けられる
- **数 MiB 〜 数百 MiB が存在** → オブジェクト単位。分割層が必須。
  `>4MiB` のバイト比率が「分割層がなければ静かに消えていた分量」

## 4.4 独立 2 ソース照合

`evidence-grading-rule` に従い、以下 2 系統が一致して初めて「確認済み」とする。

### (a) レコード数とバイト数

```bash
# 総レコード数
echo $(( $(zstd -dc trace.zst | wc -c) / 24 ))
# → cachebench の FINAL ログの records= と一致すること
# → libCacheSim のリクエスト数とも一致すること
```

### (b) A/B 等価性 — オフライン分割との突き合わせ

トレース自体を分割して**同一入力**を作り、両実装の結果が一致することを確認する。
これは同時に libCacheSim との厳密照合を可能にする。

```bash
zstd -dc cdn.oracleGeneral.zst | python3 chunk.py | zstd -12 - -o cdn.chunked.zst
```

分割済みトレースを `cachePieceSize` を十分大きく設定して(分割層を素通りさせて)再生し、
ジェネレータ内分割の結果と `byteMissRatio` / `chunkMissRatio` / `numNvmBytesWritten` が
一致すれば、分割層の実装が正しいことの独立検証になる。

> **注意**: 分割するとレコード数が変わるため **`next_access_vtime` は無効化される**
> (下のスクリプトは `-1` を書く)。libCacheSim 側で Belady 系オラクル方策を回すなら
> 再生成が必要(本再生では破棄しているので CacheLib 側は無影響)。
> また CDN トレースは展開後のサイズが数倍〜数十倍になるのでディスク容量を先に確認する。

## 4.5 4 MiB 壁の非発生確認

```bash
grep -c "setFailure" /tmp/oracle.log     # 0 であること
```

`chunkSize_ ≤ kMaxChunkSize` がコンストラクタで検証されているため、
サイズ超過による割り当て失敗は原理的に起きない。
それでも `setFailure` が出る場合はプール設定など別の原因なので、必ず追跡する。

---

# 第 5 部 実験設計上の注意

## 5.1 チャンクサイズは全アームで固定する

チャンクサイズはリージョン充填を直接決める:

- リージョン 16 MiB / チャンク 64 KiB → **256 アイテム/リージョン**(細粒度)
- リージョン 16 MiB / チャンク 1 MiB → **16 アイテム/リージョン**(粗粒度)

これは region-split 研究のパラメータそのもの。動かすと置換方式の差と交絡するため、
**全アームで同一値に固定**し、感度分析は独立した実験として分離する。

## 5.2 op 数のセマンティクスが変わる

チャンク化により **op 数 > レコード数** になる。

- `numOps` はレコード数ではなくチャンク数に対する上限として効く
- スループット(ops/sec)はチャンク単位。他方式と直接比較できない
- 実行時間の見積もりは「レコード数 × 平均チャンク数」で行う

## 5.3 トレース種別ごとの指針

| トレース | 典型サイズ | 分割層の効き方 | 備考 |
|---|---|---|---|
| **KV** | 数十 B 〜 数十 KB | ほぼ素通り(`split=0` に近い) | 64 KiB 以下が大半 |
| **Block**(tencentBlock) | 4 KB 〜 数百 KB | 一部で分割 | 27% が可変サイズ。§1.1 の注意を参照 |
| **CDN** | 数 KB 〜 数百 MB | **分割が主役** | §4.3 の事前確認が必須 |

`split=` の値がトレース種別ごとの期待と一致することを確認する。
KV で `split` が大きい、あるいは CDN で `split=0` なら、
トレースの中身か `cachePieceSize` の設定を疑う。

## 5.4 WAF の測り方

- **分子**(実書込量): cachebench 標準統計の `numNvmBytesWritten`
  (内部的には `navy_device_bytes_written` を引いている)
- **分母**(論理書込量): 自前で積む。ミスしたチャンクのバイト総和 = `missBytes_`
  (lookaside により、ミスしたチャンクがそのまま挿入されるため)
- 512 B アラインのパディングは分子側に含まれる系統誤差。値を記録しておく

---

# 付録 A: オフライン分割スクリプト

`chunk.py`

```python
import sys, struct

CHUNK, IDBITS = 65536, 20          # 1 obj_id あたり最大 1,048,576 チャンク
R = struct.Struct('<IQIq')         # 24B packed little-endian
inb, outb = sys.stdin.buffer, sys.stdout.buffer

while True:
    b = inb.read(R.size * 8192)
    if not b:
        break
    out = bytearray()
    for i in range(0, len(b) - R.size + 1, R.size):
        ts, oid, sz, _ = R.unpack_from(b, i)
        assert oid < (1 << (64 - IDBITS)), "obj_id too large for composite key"
        n = max(1, (sz + CHUNK - 1) // CHUNK)
        for c in range(n):
            csz = min(CHUNK, sz - c * CHUNK) if sz else 1
            out += R.pack(ts, (oid << IDBITS) | c, max(csz, 1), -1)
    outb.write(out)
```

合成 ID は `(obj_id << 20) | chunk_idx`。`obj_id` は libCacheSim の変換時に
振られた通し番号なので通常 2^27 程度に収まり、20 bit シフトしても溢れない
(`assert` で検証している)。

---

# 付録 B: 出典(すべて GitHub main ブランチの実装で確認済み)

| 主張 | 出典 |
|---|---|
| 24 B レコードのオフセットと番兵値 | libCacheSim `traceReader/customizedReader/oracle/oracleGeneralBin.h` |
| zstd ストリーミングの構成 | libCacheSim `traceReader/generalReader/zstdReader.c` |
| サイズ変化を無視する挙動 | libCacheSim `cache/cache.c` (`cache_find_base`) |
| `Slab::kSize = 1 << 22` | CacheLib `allocator/memory/Slab.h` |
| サイズ超過で `std::invalid_argument` | CacheLib `allocator/memory/MemoryAllocator.h`(`@throw` 記述) |
| DBG で握り潰す実装 | CacheLib `cachebench/cache/Cache-inl.h` |
| `setKey()` の失敗処理 | CacheLib `cachebench/runner/CacheStressor.h` |
| lookaside の挙動 | CacheLib `cachebench/runner/CacheStressor.h`(`kGet` の分岐) |
| `notifyResult` が条件付き | CacheLib `cachebench/runner/CacheStressor.h` |
| piecewise の不変条件 | CacheLib `cachebench/workload/PieceWiseCache.cpp`(`PieceWiseReqWrapper` ctor) |
| `cachePieceSize{65536}` / `maxCachePieces{32000}` | CacheLib `cachebench/util/Config.h` |
| BlockCache の上限 | CacheLib `navy/block_cache/BlockCache.h`(`getMaxItemSize`, `kMaxItemSize`) |
| BigHash 既定無効 / リージョン 16 MiB | CacheLib ドキュメント `Configure_HybridCache` |
| ジェネレータ登録 | CacheLib `cachebench/runner/Stressor.cpp`(`makeGenerator`) |
| `Request` の API | CacheLib `cachebench/util/Request.h` |
| `GeneratorBase` の純粋仮想 | CacheLib `cachebench/workload/GeneratorBase.h` |
