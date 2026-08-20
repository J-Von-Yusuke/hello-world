# CacheValue.h を使った「元サイズ」「代表フラグ」伝達 — 変更手順書

- **対象読者**: CacheLib のコードを初めて触る人
- **確認したソース**: CacheLib **`v20240621`**(commit `c5c0d9b`)。
  行番号・構造ともこのタグで実機確認済み
- **注意**: `main` とは構造がかなり違う(`BlockCache::insert` の引数、`allocateForInsert` の有無、
  `numPriorities` の型、`ReinsertionRes` の名前など)。上流のドキュメントを見るときは注意すること
- **行番号について**: 行番号はこの commit のもの。バージョンが違うとずれるので、
  各手順に **grep 用のアンカー文字列**を併記した。行番号よりアンカーを信用すること
- **背景と、この方式を選んだ理由**: `OBJECT_SIZE_TO_NAVY.md`(棄却した案の比較あり)

---

## 0. この改造で何をするのか

チャンク化された巨大オブジェクトについて、SSD 側の Navy BlockCache に次の 2 つを教える。

| | 教えたいこと | 使い道 |
|---|---|---|
| ① | このチャンクの**親オブジェクトの元サイズ** | サイズ別にリージョンを分ける |
| ② | このチャンクが**代表かどうか** | オブジェクトを重複なく 1 個と数える |

やることは一言でいうと、**cachebench が既に全アイテムの先頭に付けているヘッダ
`CacheValue` に、フィールドを 2 つ足す**だけである。

---

## 1. なぜ `CacheValue.h` なのか

### 1.1 CacheValue とは何か

`cachelib/cachebench/cache/CacheValue.h` にある、**cachebench の全アイテム共通の値ヘッダ**である。
ファイル冒頭のコメントに `CacheValue is the schema for all items in cachebench` と書かれている。

アイテムのメモリは実際にはこうなっている。

```
[ CacheValue ヘッダ 24 B ][ 実際のデータ ]
  idx_            (8 B)
  version_        (8 B)
  consistencyNum_ (8 B)
```

`idx_` と `version_` はデストラクタ検査用、`consistencyNum_` は整合性検査用で、
**検査を使わない設定でも常に確保されている**。つまり「アイテムごとのメタデータを置く場所」が
既に用意されていて、しかもそこは誰でも読み書きできる。

### 1.2 3 つの理由

**理由 1: 新しいヘッダを発明しなくてよい。**
自前でヘッダを作ると「値のどこから読むか」というオフセット計算が必要になり、
CacheLib のシリアライズ形式に依存してしまう。CacheValue なら
`getMemoryAs<CacheValue>()` という既存のアクセサがそのまま使える。

**理由 2: 割当サイズが 1 バイトも増えない。**
`CacheValue::getSize()` の実装はこうなっている(`CacheValue.h:72-73`)。

```cpp
static size_t getSize(size_t size) {
  return std::max<size_t>(size, sizeof(CacheValue));
}
```

`size + sizeof(CacheValue)` ではなく **`max(size, sizeof(CacheValue))`** である。
つまりヘッダは要求サイズの**内側**に入る。ヘッダを 8 B 増やしても、
アイテムの割当サイズは変わらず、使えるペイロードが 8 B 減るだけである。
**アロケーションクラスの境界を跨ぐ心配が要らない。**

**理由 3: 値は Navy まで無変換で届く。**
DRAM から SSD へ落ちるとき、NvmCache はアイテムのメモリをそのまま blob にコピーする。
その blob の先頭が CacheValue である。だから BlockCache 側で blob の先頭を読めば、
書いた値がそのまま取り出せる。

### 1.3 ペイロードは絶対に壊れない

心配になる点なので先に潰しておく。値にデータを書く関数は
`Cache::setStringItem()`(`Cache.h:1325-1331`)で、中で使っているのは
`getMemory(handle)` である。そして `getMemory` の実装はこうなっている(`Cache.h:231` / `:236`)。

```cpp
const void* getMemory(const Item& item) const noexcept {
  return item.template getMemoryAs<CacheValue>()->getData();
}
```

`getData()` は**ヘッダの後ろ**を指す。したがってペイロードの書き込みが
CacheValue のフィールドを踏むことは構造上ありえない。

---

## 2. 変更の全体像

```
[Step 1] CacheValue.h      フィールドを 2 つ足す        … 器を作る
[Step 2] Cache.h           allocate で書けるようにする   … 書き口を作る
[Step 3] Request.h         フィールドを 2 つ足す        … 運搬箱を作る
[Step 4] CacheStressor.h   setKey で渡す                … 配管をつなぐ
[Step 5] 各 generator      値を詰める                   … 中身を入れる
[Step 6] BlockCache.cpp    挿入時に読んで分類           … 使い始める(分類)
[Step 7] BlockCache.cpp    代表だけ数える               … 使い始める(計数)
[Step 8] BlockCache.h      numPriorities を 3 にする    … ★ここで初めて挙動が変わる★
```

**重要な性質**: Step 7 まではベンチマークの結果が変わらない(Step 6 でクランプを入れるため)。
つまり **Step 1〜7 は「壊れていないこと」だけ確認しながら安全に進められる**。
挙動が変わるのは Step 8 だけなので、問題が起きたときの切り分けが容易になる。

### 変更するファイル一覧

| ファイル | 役割 |
|---|---|
| `cachelib/navy/common/ObjectInfo.h` | **新規**。読み取り対象の 8 B(navy が所有) |
| `cachelib/cachebench/cache/CacheValue.h` | ObjectInfo を先頭に埋め込む |
| `cachelib/cachebench/cache/Cache.h` | allocate に引数追加 |
| `cachelib/cachebench/util/Request.h` | Request にフィールド追加 |
| `cachelib/cachebench/runner/CacheStressor.h` | setKey で受け渡し |
| `cachelib/cachebench/workload/` の各 generator | 値を設定 |
| `cachelib/navy/block_cache/BlockCache.h` | 優先度の数 |
| `cachelib/navy/block_cache/BlockCache.cpp` | 分類と計数 |

---

## Step 1: CacheValue にフィールドを 2 つ足す

### 変更場所

`cachelib/cachebench/cache/CacheValue.h`、private セクション(`:79-86`)。

```bash
grep -n "consistencyNum_" cachelib/cachebench/cache/CacheValue.h
```

### 変更前

```cpp
 private:
  // for ItemDestructorCheck usage
  uint64_t idx_;        // an unique id for item in cache
  uint64_t version_{0}; // a version number for item due to in-place mutation
  // for ConsistencyCheck usage
  uint64_t consistencyNum_{0}; // a number to check for item validation
  // beginning of the byte array.
  unsigned char data_[0];
```

### 先に: 読み取り対象の 8 B は **navy 側**で定義する

`CacheValue` は cachebench にあり、**cachebench → navy** の依存が既にある。
したがって navy から `CacheValue.h` を include すると**循環参照になる**。
そこで読み取り対象の 8 B だけを navy 側に置き、cachebench がそれを埋め込む。
定義は 1 箇所だけなので二重管理にならない。

`cachelib/navy/common/ObjectInfo.h`(新規):

```cpp
#pragma once
#include <folly/Portability.h>
#include <cstdint>

namespace facebook::cachelib::navy {

// cachebench の CacheValue 先頭に埋め込まれる 8 B。
// BlockCache は blob 0 の先頭からこれを読む。
struct FOLLY_PACK_ATTR ObjectInfo {
  uint32_t origSize;  // 親オブジェクトの論理サイズ
  uint32_t flags;     // 上位 16 bit = カナリア, bit0 = representative

  static constexpr uint32_t kCanary     = 0xCA5A0000u;
  static constexpr uint32_t kCanaryMask = 0xFFFF0000u;
  static constexpr uint32_t kRep        = 0x00000001u;

  bool valid() const { return (flags & kCanaryMask) == kCanary; }
  bool isRep() const { return (flags & kRep) != 0; }
};
static_assert(sizeof(ObjectInfo) == 8, "ObjectInfo must be 8B");

} // namespace facebook::cachelib::navy
```

**カナリアを `flags` の上位 16 bit に入れるのが要点。**
`flags` には空きがあるので、**追加バイトゼロで自己検証**にできる。
レイアウトがずれたら `valid()` が false になり、カウンタで即座に分かる。

### 変更後(CacheValue.h)

**フィールドは末尾ではなく先頭に置くこと。**
末尾だと navy が `sizeof(CacheValue)` を知る必要が出てしまう。
先頭なら navy は「blob 0 の offset 0」を読むだけで済み、
**CacheValue の他の中身に一切依存しない。**

```cpp
#include "cachelib/navy/common/ObjectInfo.h"

class CACHELIB_PACKED_ATTR CacheValue {
 public:
  uint32_t getOrigSize() const { return objInfo_.origSize; }
  bool isRepresentative() const { return objInfo_.isRep(); }

  void setObjectInfo(uint32_t origSize, bool isRep) {
    objInfo_.origSize = origSize;
    objInfo_.flags = navy::ObjectInfo::kCanary |
                     (isRep ? navy::ObjectInfo::kRep : 0u);
  }
  // ... 既存のメソッドはそのまま ...

 private:
  // ★必ず先頭★ Navy が blob 0 の offset 0 から読むため、順序を変えないこと
  navy::ObjectInfo objInfo_{};
  // ---- 以下は既存 ----
  uint64_t idx_;
  uint64_t version_{0};
  uint64_t consistencyNum_{0};
  unsigned char data_[0];
};
```

### なぜこの形なのか

- `origSize` が uint32 なのは、`oracleGeneral` の `obj_size` が uint32 だから(最大 4 GiB)。
  トレース形式が表現できない値は存在しないので、これで十分
- 8 B が先頭に入るので、後続の `uint64_t` 3 本は offset 8 / 16 / 24 と 8 B 境界に揃い、
  `data_` も offset 32 で揃う。アラインメントが崩れない

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| `sizeof(CacheValue)` | 24 → **32** |
| アイテムの割当サイズ | **変わらない**(§1.2 の理由 2) |
| 使えるペイロード | 各アイテム **8 B 減る** |
| ベンチの結果 | **ほぼ変わらない**。32 B 未満のアイテムだけ割当が 24→32 に増える |
| 動作 | **何も変わらない**。まだ誰も書かないし読まない |

### 確認方法

ビルドが通ること。心配なら一時的に次を足して確認する。

```cpp
static_assert(sizeof(CacheValue) == 32, "CacheValue must be 32B");
```

---

## Step 2: `Cache::allocate` で書けるようにする

### 変更場所

`cachelib/cachebench/cache/Cache.h`、`Cache<Allocator>::allocate` の定義(`:899-913`)。

```bash
grep -n "CacheValue::initialize" cachelib/cachebench/cache/Cache.h
```

### 変更前

```cpp
template <typename Allocator>
typename Cache<Allocator>::WriteHandle Cache<Allocator>::allocate(
    PoolId pid, folly::StringPiece key, size_t size, uint32_t ttlSecs) {
  WriteHandle handle;
  try {
    handle = cache_->allocate(pid, key, CacheValue::getSize(size), ttlSecs);
    if (handle) {
      CacheValue::initialize(handle->getMemory());
    }
  } catch (const std::invalid_argument& e) {
    XLOGF(DBG, "Unable to allocate, reason: {}", e.what());
  }
  return handle;
}
```

### 変更後

```cpp
template <typename Allocator>
typename Cache<Allocator>::WriteHandle Cache<Allocator>::allocate(
    PoolId pid, folly::StringPiece key, size_t size, uint32_t ttlSecs,
    uint32_t origSize, bool isRep) {          // ← 引数を 2 つ追加
  WriteHandle handle;
  try {
    handle = cache_->allocate(pid, key, CacheValue::getSize(size), ttlSecs);
    if (handle) {
      CacheValue::initialize(handle->getMemory());
      auto* cv = handle->template getMemoryAs<CacheValue>();
      // origSize が 0 のときは「チャンク化していない」とみなし自分のサイズを入れる。
      // これで下流は常に非 0 を前提にできる。
      cv->setObjectInfo(origSize != 0 ? origSize : static_cast<uint32_t>(size),
                        isRep);
    }
  } catch (const std::invalid_argument& e) {
    XLOGF(DBG, "Unable to allocate, reason: {}", e.what());
  }
  return handle;
}
```

宣言側(`Cache.h:125`)にも**デフォルト引数**を付けておくと、
既存の呼び出し箇所を一切直さずに済む。

```bash
grep -n "WriteHandle allocate(PoolId pid" cachelib/cachebench/cache/Cache.h
```

```cpp
  WriteHandle allocate(PoolId pid,
                       folly::StringPiece key,
                       size_t size,
                       uint32_t ttlSecs = 0,
                       uint32_t origSize = 0,      // ← 追加
                       bool isRep = true);         // ← 追加
```

### なぜここなのか

`Cache<Allocator>::allocate` は **cachebench で値が実体化される唯一の入口**である。
`CacheValue::initialize()` を呼んでいるのがここだけなので、ここに書けば書き漏らしが起きない。

### なぜ `isRep` のデフォルトが `true` なのか

チャンク化していないアイテム(通常のワークロード)は、それ自体が 1 個のオブジェクトである。
デフォルトを `true` にしておけば「チャンク化していない = 全部が代表」となり、
既存のワークロードでも個数が正しく数えられる。

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| 既存の呼び出し箇所 | デフォルト引数なので**修正不要、ビルドが通る** |
| 書かれる値 | `origSize_` = そのアイテムのサイズ、`flags_` = representative |
| 動作 | **何も変わらない**。まだ誰も読まない |

---

## Step 3: `Request` にフィールドを 2 つ足す

### 変更場所

`cachelib/cachebench/util/Request.h`、`struct Request`(`:63`)の public メンバ(`:134-135` 付近)。

```bash
grep -n "std::vector<size_t>::iterator sizeBegin" cachelib/cachebench/util/Request.h
```

### 変更内容

```cpp
  // If not chained, the size is *sizeBegin
  std::vector<size_t>::iterator sizeBegin;
  std::vector<size_t>::iterator sizeEnd;

  // ---- 追加 ----
  // 親オブジェクトの論理サイズ。0 = チャンク化していない
  uint32_t origSize{0};
  // この piece を代表として数えるか
  bool isRep{true};
```

### なぜ Request なのか

generator(トレースを読む側)と stressor(キャッシュを叩く側)の間で情報を渡す唯一の箱が
`Request` だから。generator は親サイズを知っていて、stressor は値を書く場所を知っている。
両者をつなぐには Request を経由するしかない。

### 変更後の予測動作

まだ誰も設定しないのでデフォルト値のまま。**動作は変わらない。**

---

## Step 4: `setKey` で受け渡す

### 変更場所

`cachelib/cachebench/runner/CacheStressor.h`、`setKey()`(`:476-500`)。

**注意**: v20240621 の `setKey` は第 3 引数が `const std::string* key`(ポインタ)である。
`main` の `std::string_view` とは違うので、コピペで持ってこないこと。

```bash
grep -n "OpResultType setKey" cachelib/cachebench/runner/CacheStressor.h
```

### 変更前

```cpp
  OpResultType setKey(
      PoolId pid, ThroughputStats& stats, const std::string* key,
      size_t size, uint32_t ttlSecs,
      const std::unordered_map<std::string, std::string>& featureMap,
      const std::string& itemValue) {
    ...
    auto it = cache_->allocate(pid, *key, size, ttlSecs);
```

### 変更後

```cpp
  OpResultType setKey(
      PoolId pid, ThroughputStats& stats, const std::string* key,
      size_t size, uint32_t ttlSecs,
      const std::unordered_map<std::string, std::string>& featureMap,
      const std::string& itemValue,
      uint32_t origSize = 0, bool isRep = true) {   // ← 追加
    ...
    auto it = cache_->allocate(pid, *key, size, ttlSecs, origSize, isRep);
```

呼び出し元は 2 箇所だけなので、両方に `req.origSize, req.isRep` を足す。

| 行 | 文脈 | grep アンカー |
|---|---|---|
| `:342` | `OpType::kSet` / `kLoneSet` | `result = setKey(pid, stats, key,` |
| `:372` | `OpType::kGet` の**ミス時 lookaside 挿入** | `if (config_.enableLookaside) {`(`:366`)の直後 |

### ここが設計上の要

**`enableLookaside` の真偽にかかわらず、挿入は必ず `setKey()` を通る。**

- `enableLookaside = true`(oracleGeneral 再生): kGet ミス → `:339` の `setKey`
- `enableLookaside = false`(PieceWise): 明示 kSet → `:309` の `setKey`

だから `setKey` に 1 回配管すれば両方に効く。**両対応のために分岐を書く必要はない。**

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| generator 未対応の段階 | `origSize = 0` が渡り、Step 2 の分岐で「自分のサイズ」が入る |
| 動作 | **何も変わらない** |

---

## Step 5: generator で値を詰める

### 変更場所

各 generator の、`Request` を組み立てている箇所。

| generator | `origSize` に入れる値 | `isRep` に入れる条件 |
|---|---|---|
| oracleGeneral チャンク再生 | `ChunkSplitter` が持つ**親レコードのサイズ** | `chunkIdx == 0` |
| PieceWise-Replay | `PieceWiseReqWrapper` の `fullObjectSize` | header ピースのとき |
| 既存の合成ワークロード | 触らない(0 のまま) | 触らない(true のまま) |

### なぜ代表を「キーの文字列」で判定しないのか

「キーの末尾 6 桁が 000000 なら代表」という判定も可能だが、そうすると
**BlockCache がキーの命名規則に依存してしまう**。
oracleGeneral 再生のキーは `<objId 20 桁>_<chunkIdx 6 桁>` だが、PieceWise のキーは
`|#|` 区切りで形式が違う。フラグにしておけば、**どの generator でも同じコードが動く。**

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| 値 | 1 GiB のオブジェクトの 1024 個のチャンク全部に `origSize_ = 1073741824` が入る。うち chunk 0 だけ `flags_` の bit0 が立つ |
| 動作 | **まだ何も変わらない**。書いたが読んでいない状態 |

### 確認方法

ここで一度、書けていることを確かめておくと後が楽になる。
`Cache::find()` で取ったハンドルから読むデバッグ出力を一時的に入れる。

```cpp
auto* cv = it->template getMemoryAs<CacheValue>();
XLOGF(INFO, "key={} origSize={} rep={}", key, cv->getOrigSize(),
      cv->isRepresentative());
```

同じオブジェクトの全チャンクで `origSize` が一致し、`rep` がちょうど 1 つだけ true になっていれば正しい。

---

## Step 6: Navy 側で読んで分類する

### 6.1 依存関係の確認(v20240621 で必ず読むこと)

| 向き | v20240621 での状況 |
|---|---|
| navy → allocator/nvmcache の **include** | **既にある**。`BlockCache.h:25` が `NavyConfig.h`、`navy/common/Device.h:22` や `navy/Factory.h:23` も同様。だから `NvmItem.h` を足しても新しい依存方向は生まれない |
| navy → cachebench | **不可**(循環する)。だから Step 1 で 8 B だけを navy 側の `ObjectInfo` として定義した |
| **リンク** | `cachelib_navy` は **`cachelib_common` にしかリンクしない**(`navy/CMakeLists.txt:58-60`)。一方 `NvmItem.cpp` は **`cachelib_allocator`** に入っている(`allocator/CMakeLists.txt:50`)。しかも `cachelib_allocator` が `cachelib_navy` にリンクする側である(`:63-64`) |

```bash
grep -n "target_link_libraries(cachelib_navy" -A 3 cachelib/navy/CMakeLists.txt
grep -n "nvmcache/NvmItem.cpp" cachelib/allocator/CMakeLists.txt
```

**ここから出る結論**: `NvmItem::getBlob()` は `NvmItem.cpp` にある**アウトオブライン関数**なので、
navy から呼ぶと `cachelib_navy` に未定義シンボルが残る。cachebench の実行ファイルは
`cachelib_allocator` 経由で最終的に解決するが、**navy 単体のテストがリンクできなくなる恐れがある**。

そこで **`getBlob()` は呼ばず、オフセットを navy 内でインライン計算する**。
`getNumBlobs()` は `NvmItem.h:87` で**インライン定義**されているので、これは呼んでよい。

> `main` ブランチでは `BlockCache.h` が `NvmItem.h` を include しており、
> `ReuseTimeReinsertionPolicy` という前例もあるが、**v20240621 にはどちらも無い**。

### 6.2 読み取りヘルパ

`cachelib/navy/block_cache/BlockCache.cpp` の無名 namespace に置く。

```cpp
#include "cachelib/allocator/nvmcache/NvmItem.h"   // ★v20240621 では自分で足す
#include "cachelib/navy/common/ObjectInfo.h"

namespace {

// NvmItem のレイアウト(NvmItem.h:147-159 のコメントに明記):
//   [ヘッダ sizeof(NvmItem)][BlobInfo × numBlobs][Blob データ...]
// BlobInfo は uint32 × 2 = 8 B(NvmItem.h:118-121, FOLLY_PACK_ATTR)。
// private なので sizeof が取れない。値は単体テストで固定する(§6.3)。
constexpr size_t kBlobInfoSize = 8;

// value は NvmCache が書いた NvmItem のバイト列そのもの。
// その blob 0 が parent アイテムのメモリで、先頭が ObjectInfo。
const ObjectInfo* probeObjectInfo(BufferView value) {
  // (1) NvmItem のヘッダが乗る長さがあるか
  if (value.size() < sizeof(cachelib::NvmItem)) {
    return nullptr;
  }
  const auto* nvmItem =
      reinterpret_cast<const cachelib::NvmItem*>(value.data());

  // (2) numBlobs の健全性。掛け算でオーバーフローさせないため割り算で判定する。
  //     getNumBlobs() はインライン(NvmItem.h:87)なのでリンク依存が出ない
  const size_t numBlobs = nvmItem->getNumBlobs();
  if (numBlobs == 0 || numBlobs > value.size() / kBlobInfoSize) {
    return nullptr;
  }

  // (3) blob 0 のデータ開始位置。ヘッダ + BlobInfo 配列の直後
  const size_t off = sizeof(cachelib::NvmItem) + numBlobs * kBlobInfoSize;
  if (off + sizeof(ObjectInfo) > value.size()) {
    return nullptr;
  }

  // (4) blob 0 = parent(NvmCache.h:942 が chained ループ :945 より前にある)
  const auto* oi = reinterpret_cast<const ObjectInfo*>(value.data() + off);

  // (5) カナリアで検証。false ならレイアウトがずれている
  return oi->valid() ? oi : nullptr;
}

} // namespace
```

#### ガードの順序に意味がある

(2) を掛け算ではなく**割り算**で書いているのは、壊れた `numBlobs`(例えば `SIZE_MAX`)で
`numBlobs * kBlobInfoSize` がオーバーフローし、(3) のチェックをすり抜けるのを防ぐため。
順番を入れ替えたり掛け算に直したりしないこと。

`reinterpret_cast` でオンディスク構造体を読むのは、同じファイル内に前例がある
(`onRegionReclaim` 内の `*reinterpret_cast<const EntryDesc*>(...)`)。
`NvmItem` は `FOLLY_PACK_ATTR` なのでアラインメント要求もない。

### 6.3 `kBlobInfoSize = 8` を単体テストで固定する

`BlobInfo` は private なので `sizeof` が取れない。ここだけが唯一の「レイアウトの決め打ち」なので、
**cachebench 側のテスト**(allocator と navy の両方にリンクできる)で固定する。

```cpp
TEST(ObjectInfoProbe, BlobZeroOffsetMatchesGetBlob) {
  std::string payload(64, 0x78);
  cachelib::Blob blob{static_cast<uint32_t>(payload.size()),
                      folly::StringPiece{payload}};
  auto item = std::unique_ptr<cachelib::NvmItem>(
      new (cachelib::NvmItem::estimateVariableSize(blob))
          cachelib::NvmItem(0, 0, 0, blob));

  const auto* base = reinterpret_cast<const uint8_t*>(item.get());
  const size_t off = sizeof(cachelib::NvmItem) + item->getNumBlobs() * 8;
  EXPECT_EQ(item->getBlob(0).data.data(),
            reinterpret_cast<const char*>(base + off));
}
```

これが通る限り、インライン計算は `getBlob(0)` と等価である。
CacheLib を上げたときにここが落ちれば、レイアウトが変わったと分かる。

### 6.4 分類を差し込む場所

**v20240621 には `allocateForInsert()` が無い。** 優先度は
`BlockCache::insert()` の中で直接決めている。`value` がその場にあるので、
**関数シグネチャの変更は一切要らない**(`main` より変更が小さい)。

```bash
grep -n "All newly inserted items are assigned with the lowest priority" \
     cachelib/navy/block_cache/BlockCache.cpp
```

#### 変更前(`BlockCache.cpp:168-180`)

```cpp
Status BlockCache::insert(HashedKey hk, BufferView value) {
  INJECT_PAUSE(pause_blockcache_insert_entry);

  uint32_t size = serializedSize(hk.key().size(), value.size());
  if (size > kMaxItemSize) {
    allocErrorCount_.inc();
    insertCount_.inc();
    return Status::Rejected;
  }

  // All newly inserted items are assigned with the lowest priority
  auto [desc, slotSize, addr] =
      allocator_.allocate(size, kDefaultItemPriority, true /* canWait */);
```

#### 変更後

```cpp
  const ObjectInfo* oi = probeObjectInfo(value);

  uint16_t priority = kDefaultItemPriority;
  if (oi != nullptr && oi->origSize != 0) {
    priority = classOf(oi->origSize);        // 0 / 1 / 2
  } else {
    headerMissCount_.inc();                  // 定常運転で 0 であるべき
  }
  // ★重要★ numPriorities_ を超える priority を渡してはならない
  priority = (numPriorities_ == 0)
                 ? kDefaultItemPriority
                 : std::min<uint16_t>(priority, numPriorities_ - 1);

  auto [desc, slotSize, addr] =
      allocator_.allocate(size, priority, true /* canWait */);
```

`Allocator::allocate` は v20240621 では **3 引数**(`size, priority, canWait`)である
(`Allocator.h:103`)。`main` の 4 引数版(`distKey` 付き)とは違う。

#### なぜクランプが要るのか

`Allocator::allocate` のコメント(`Allocator.h:100-102`)にこう書かれている。

> When allocating with a priority, the priority must NOT exceed the max priority
> which is (numPriorities - 1) specified when constructing this allocator.

つまり `numPriorities = 1` のまま priority 2 を渡すと**壊れる**。
クランプを入れておけば、Step 8 で `numPriorities` を 3 にするまで
**全部 priority 0 に丸められ、挙動が今までと完全に同じになる**。
これが「Step 7 まで結果が変わらない」ことの仕掛けである。

#### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| `numPriorities = 1` のまま | priority が全部 0 にクランプされ、**ベンチ結果は変更前と一致する** |
| `headerMissCount_` | **0**。0 でなければ値が書けていないか読み位置が違う |

---

## Step 7: 代表だけ数える

### 7.1 挿入側(`BlockCache.cpp:168-` の `insert()` 内)

Step 6 で取った `oi` をそのまま使う。`OpenStatus::Ready` を確認した後に置く。

```cpp
  if (oi != nullptr && oi->isRep()) {
    splitThreshold_.onAdmit(oi->origSize);
    repAdmitCount_.inc();
  }
```

### 7.2 退避側(`BlockCache::onRegionReclaim`、`:395-`)

`hk` と `value` は `:424-426` で既に手元にある。
**v20240621 の判定は `AllocatorApiResult` ではなく `ReinsertionRes`** である。

```bash
grep -n "case ReinsertionRes::kEvicted" cachelib/navy/block_cache/BlockCache.cpp
```

`:441-450` の switch に足す。

```cpp
      switch (reinsertionRes) {
      case ReinsertionRes::kEvicted: {
        evictionCount++;
        usedSizeBytes_.sub(decodeSizeHint(encodeSizeHint(entrySize)));
        const ObjectInfo* oi = probeObjectInfo(value);   // ← 追加
        if (oi != nullptr && oi->isRep()) {              // ← 追加
          splitThreshold_.onEvict(oi->origSize, /* freq = */ ...);
          repEvictCount_.inc();
        }
        break;
      }
      case ReinsertionRes::kRemoved:
        holeCount_.sub(1);
        holeSizeTotal_.sub(decodeSizeHint(encodeSizeHint(entrySize)));
        break;
      case ReinsertionRes::kReinserted:
        break;
      }
```

`case` の中で変数を宣言するので、`{ }` でブロックを作ることを忘れないこと
(付けないとコンパイルエラーになる)。

### 7.3 再挿入の優先度 — ここは必ず直す

`reinsertOrRemoveItem`(`:523-524`)の中、`:552-556`。
v20240621 の引数は **4 つ**(`hk, value, entrySize, currAddr`)で、
`main` の `EntryDesc` 付き 5 引数版とは違う。

#### 変更前

```cpp
  // Priority of an re-inserted item is determined by its past accesses
  // since the time it was last (re)inserted.
  uint16_t priority =
      numPriorities_ == 0
          ? kDefaultItemPriority
          : std::min<uint16_t>(lr.currentHits(), numPriorities_ - 1);
```

#### 変更後

```cpp
  // サイズクラスを優先度に流用しているため、アクセス回数による昇格は行わない。
  // 再挿入されるアイテムは、今いるリージョンのクラスに留める。
  uint16_t priority =
      numPriorities_ == 0
          ? kDefaultItemPriority
          : regionManager_.getRegion(currAddr.rid()).getPriority();
```

`Region::getPriority()` は `Region.h:108` にある(`setPriority` は `:102`)。

#### なぜこれを直さないといけないのか

元の実装は**アクセス回数が多いアイテムを上位の優先度リージョンへ昇格させる**ものである。
優先度をサイズクラスに流用すると、この昇格は「よく使われる小さいオブジェクトを
大サイズ用リージョンへ移す」という**意味を成さない動作**になり、分別が崩れる。

なお、この変更により本来の reinsertion 優先度機能は使えなくなる。
両立させたい場合は `numPriorities = 3 × ホットネス段数` の 2 次元タグにする。

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| `repAdmitCount_` | オブジェクトの distinct 数と一致するはず。1024 チャンクのオブジェクトも 1 と数えられる |
| ベンチ結果 | **まだ変わらない**(priority はクランプで 0 のまま) |

---

## Step 8: `numPriorities` を 3 にする(ここで初めて挙動が変わる)

### 変更場所

`cachelib/navy/block_cache/BlockCache.h:87`。
**v20240621 では単なる `uint16_t`** である
(`main` の `std::vector<uint32_t> allocatorsPerPriority` ではない)。

```bash
grep -n "numPriorities" cachelib/navy/block_cache/BlockCache.h
```

```cpp
    uint16_t numPriorities{1};      // ← 3 にする
```

`numPriorities_` はこれをそのまま受け取る(`BlockCache.cpp:117`)。

**注意**: v20240621 の `NavyConfig` には `numPriorities` のセッターが**無い**。
つまり JSON 設定からは変えられない。次のどちらかにする。

1. `BlockCache::Config` の既定値を 3 に変える(最小変更。実験用途ならこれで十分)
2. `NavyConfig` にセッターを足し、`NavySetup.cpp` で `BlockCache::Config` に流す
   (設定ファイルから切り替えたい場合)

### 変更後の予測動作

**ここで初めてリージョンがサイズクラスごとに分かれる。**

| 項目 | 予測 |
|---|---|
| リージョン | クラスごとに別々の open region が開く。1 つのリージョンには同じクラスのアイテムしか入らない |
| 巨大オブジェクト | 1024 チャンク全部が同じクラスのリージョン群に入る |
| WAF | リージョン単位の退避なので理論上 1 に近づく |
| ヒット率 | **変わる**(良くなるとは限らない。これが測定対象) |
| 小クラスのリージョン | 16 MiB に小さいアイテムが数千個入り、1 回の退避で数千オブジェクトが同時に消える |
| 大クラスのリージョン | チャンクサイズが固定なので**リージョン内断片化がほぼゼロ**になる |

この非対称性そのものが研究の測定対象なので、**クラス別のリージョンあたり収容オブジェクト数を
必ず記録すること。**

---

## 3. つまずきやすい点

| 症状 | 原因 | 対処 |
|---|---|---|
| `headerMissCount_` が 0 にならない | 値が書けていない / 読み位置が違う | Step 5 の確認方法でまず DRAM 側を確認する |
| `repAdmitCount_` が distinct 数より多い | 複数チャンクに rep が立っている | generator の `isRep` 条件を見直す |
| `repAdmitCount_` が極端に少ない | 代表チャンクだけ先に退避されている | 部分残留。既存の `objFullHit%` / `objPartialHit%` で量を確認 |
| クラッシュ、または挙動不審 | `numPriorities` を超える priority を渡した | Step 6 のクランプが入っているか確認 |
| 32 B 未満のアイテムでサイズが変 | `getSize()` が `max(size, 32)` を返すため | 仕様どおり。系統誤差として記録する |

---

## 4. 変更しなくてよい場所とその理由

| 場所 | 理由 |
|---|---|
| **キーの形式** | サイズはキーに入れない。サイズは可変属性なので識別子に混ぜると、サイズが変わったときに別オブジェクト扱いになり、偽ミスと二重占有が起きる |
| **トレースファイル** | 前処理でサイズを正規化する必要がない。tencentBlock ではリクエストの 10.3% がクラス所属ごと変わってしまう |
| **Navy の index** | キーからアイテムを引く機構は既にある。サイズ用の別表は作らない |
| **`NvmItem`** | `getBlob(0)` という公開アクセサがあるので、レイアウトを直接触る必要がない |
| **chained items 関連** | cachebench で鎖ができるのは `OpType::kAddChained`(`CacheStressor.h:390-415`)だけで、`req.sizeBegin + 1 != req.sizeEnd`(`:406`)が前提。チャンク再生は `sizes[0]` しか使わないので鎖は発生しない。仮に発生しても §6.2 の実装は `numBlobs` を勘定するので正しく動く |

---

## 5. 動作確認の順序

1. **Step 1 の後**: ビルドが通る。`sizeof(CacheValue) == 32`
2. **Step 2〜4 の後**: ベンチが以前と同じ結果を出す(数値が一致すること)
3. **Step 5 の後**: DRAM 側のデバッグ出力で、同一オブジェクトの全チャンクの `origSize` が一致し
   `rep` がちょうど 1 つ
4. **Step 6 の後**: `headerMissCount_ == 0`、かつ**ベンチ結果が Step 2 のときと一致**
   (クランプが効いているので変わらないのが正しい)
5. **Step 7 の後**: `repAdmitCount_` が distinct オブジェクト数と一致
6. **Step 8 の後**: リージョンがクラス別に分かれる。ここから測定を始める

**4 の「結果が変わらないこと」の確認が最も重要である。**
ここで変わってしまう場合、クランプが効いていないか、意図せず別の場所を壊している。

---

## 付録 A: 確認済みの事実と出典(すべて v20240621 / `c5c0d9b`)

| 事実 | 出典 |
|---|---|
| CacheValue は全アイテム共通の値ヘッダ | `cachebench/cache/CacheValue.h:43` |
| `getSize()` は `max(size, sizeof(CacheValue))` を返す | `cachebench/cache/CacheValue.h:72-73` |
| CacheValue の既存フィールドは 24 B | `cachebench/cache/CacheValue.h:79-86` |
| 値の実体化は `Cache::allocate` の 1 箇所 | `cachebench/cache/Cache.h:899-913`(`CacheValue::initialize` は `:906`) |
| 宣言側 | `cachebench/cache/Cache.h:125` |
| ペイロード書き込みはヘッダの後ろから | `cachebench/cache/Cache.h:231` / `:236`(`getData()`)・`:1325-1331`(`setStringItem`) |
| kSet と lookaside が同じ `setKey` を通る | `cachebench/runner/CacheStressor.h:342` と `:372` → `:476` |
| `setKey` の key 引数は `const std::string*` | `cachebench/runner/CacheStressor.h:478` |
| Request のサイズ反復子 | `cachebench/util/Request.h:63`(struct)・`:134-135` |
| `NvmItem` のレイアウト | `allocator/nvmcache/NvmItem.h:147-159`(コメント)・`:118-121`(`BlobInfo`) |
| `getNumBlobs()` はインライン / `getBlob()` はアウトオブライン | `allocator/nvmcache/NvmItem.h:87` / `:92` |
| blob 0 は parent | `allocator/nvmcache/NvmCache.h:942`(chained ループ `:945` より前) |
| value は NvmItem の先頭から始まる | `allocator/nvmcache/NvmCache.h:998`(`toIOBuf`)→ `:1015-1016`(`insertAsync`) |
| **`BlockCache::insert` は 2 引数** | `navy/block_cache/BlockCache.cpp:168` |
| **挿入は最低優先度に固定、`insert()` 内で決まる** | `navy/block_cache/BlockCache.cpp:178-180` |
| **`Allocator::allocate` は 3 引数** | `navy/block_cache/Allocator.h:103` |
| priority は `numPriorities - 1` を超えてはならない | `navy/block_cache/Allocator.h:100-102` |
| **`numPriorities` は `uint16_t` スカラー** | `navy/block_cache/BlockCache.h:87`(受け取りは `BlockCache.cpp:117`) |
| 退避処理の位置 | `navy/block_cache/BlockCache.cpp:395`(`onRegionReclaim`)、switch は `:441-450` |
| **退避判定は `ReinsertionRes`** | `navy/block_cache/BlockCache.cpp:441-450` |
| **`reinsertOrRemoveItem` は 4 引数** | `navy/block_cache/BlockCache.cpp:523-524` |
| 再挿入優先度はアクセス回数で決まる | `navy/block_cache/BlockCache.cpp:552-556` |
| Region に優先度の getter/setter がある | `navy/block_cache/Region.h:102` / `:108` |
| `cachelib_navy` は `cachelib_common` にしかリンクしない | `navy/CMakeLists.txt:58-60` |
| `NvmItem.cpp` は `cachelib_allocator` に入る | `allocator/CMakeLists.txt:50`、リンクは `:63-64` |

### `main` との主な違い(上流ドキュメントを見るときの注意)

| 項目 | v20240621(こちら) | main |
|---|---|---|
| `BlockCache::insert` | `(hk, value)` | `(hk, value, poolId, expiryTime, lastAccessTimeSecs)` |
| 挿入時の priority 決定 | `insert()` 内(**value がその場にある**) | `allocateForInsert()`(value を持たないので引数追加が必要) |
| `Allocator::allocate` | `(size, priority, canWait)` | `(size, priority, canWait, distKey)` |
| `numPriorities` | `uint16_t` スカラー | `std::vector<uint32_t> allocatorsPerPriority` |
| 退避結果の型 | `ReinsertionRes::kEvicted` | `AllocatorApiResult::EVICTED` |
| `reinsertOrRemoveItem` | 4 引数 | 5 引数(`EntryDesc` 付き) |
| `BlockCache.h` が `NvmItem.h` を include | **しない** | する |
| navy 内で NvmItem を読む前例 | **無い** | `ReuseTimeReinsertionPolicy` |

**v20240621 のほうが変更量は小さい。** `insert()` に `value` があるため、
関数シグネチャを変えずに済む。

## 付録 B: 実装時に判断が必要な点

**解決済みの論点**(記録として残す):

- 「navy が allocator に依存してよいか」→ **include は既存**
  (`BlockCache.h:25` が `NavyConfig.h`)。新しい依存方向は生まれない。
  ただし**リンクは別問題**で、`cachelib_navy` は `cachelib_common` にしかリンクしない。
  だからアウトオブラインの `getBlob()` は呼ばず、インライン計算にした(§6.1〜6.2)
- 「navy から `CacheValue.h` を読めるか」→ **不可**(cachebench → navy が既存で循環する)。
  8 B の `ObjectInfo` を navy 側に置いて解決した(Step 1)

**まだ判断が必要な点**:

- **`splitThreshold_.onEvict()` に渡す `freq`(在中アクセス数)**:
  Navy のどの値から取るか。`reinsertOrRemoveItem` 内の `lr.currentHits()` が候補だが、
  退避カウントは `onRegionReclaim` 側で行うため、値を渡す経路を決める必要がある
- **`headerMissCount_` / `repAdmitCount_` / `repEvictCount_` の登録**:
  `BlockCache::getCounters()` に足して cachebench の統計に出す
- **`classOf()` の閾値**: トレースごとに違う。`OBJECT_SIZE_TO_NAVY.md` 第 6 部を参照
  (meta_rnha なら 32 MiB、tencentBlock なら 32 KiB 付近)
