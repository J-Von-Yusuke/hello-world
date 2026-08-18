# CacheValue.h を使った「元サイズ」「代表フラグ」伝達 — 変更手順書

- **対象読者**: CacheLib のコードを初めて触る人
- **確認したソース**: CacheLib `main` / commit `7c04a82`
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
`CacheValue::getSize()` の実装はこうなっている(`CacheValue.h:74-80`)。

```cpp
static uint32_t getSize(size_t size) {
  const auto cacheValueSize = std::max<size_t>(size, sizeof(CacheValue));
  ...
  return static_cast<uint32_t>(cacheValueSize);
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
`Cache::setStringItem()`(`Cache.h:1571-1578`)で、中で使っているのは
`getMemory(handle)` である。そして `getMemory` の実装はこうなっている(`Cache.h:250-256`)。

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
| `cachelib/cachebench/cache/CacheValue.h` | ヘッダにフィールド追加 |
| `cachelib/cachebench/cache/Cache.h` | allocate に引数追加 |
| `cachelib/cachebench/util/Request.h` | Request にフィールド追加 |
| `cachelib/cachebench/runner/CacheStressor.h` | setKey で受け渡し |
| `cachelib/cachebench/workload/` の各 generator | 値を設定 |
| `cachelib/navy/block_cache/BlockCache.h` | 優先度の数 |
| `cachelib/navy/block_cache/BlockCache.cpp` | 分類と計数 |

---

## Step 1: CacheValue にフィールドを 2 つ足す

### 変更場所

`cachelib/cachebench/cache/CacheValue.h`、private セクション(`:85-92` 付近)。

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

### 変更後

```cpp
 private:
  // for ItemDestructorCheck usage
  uint64_t idx_;        // an unique id for item in cache
  uint64_t version_{0}; // a version number for item due to in-place mutation
  // for ConsistencyCheck usage
  uint64_t consistencyNum_{0}; // a number to check for item validation
  // ---- ここから追加 ----
  // 親オブジェクトの論理サイズ。チャンク化されていない場合は自分自身のサイズ。
  // 0 は「未設定」を意味する。
  uint32_t origSize_{0};
  // bit0 = representative(この piece をオブジェクト 1 個として数える)
  uint32_t flags_{0};
  // ---- ここまで追加 ----
  // beginning of the byte array.
  unsigned char data_[0];
```

public セクションにアクセサも足す(`getConsistencyNum()` の隣あたり)。

```cpp
  static constexpr uint32_t kFlagRepresentative = 0x1;

  uint32_t getOrigSize() const { return origSize_; }
  bool isRepresentative() const { return (flags_ & kFlagRepresentative) != 0; }

  void setOrigSize(uint32_t s) { origSize_ = s; }
  void setRepresentative(bool rep) {
    flags_ = rep ? (flags_ | kFlagRepresentative)
                 : (flags_ & ~kFlagRepresentative);
  }
```

### なぜ uint32 を 2 つなのか

- `origSize_` が uint32 なのは、`oracleGeneral` の `obj_size` が uint32 だから(最大 4 GiB)。
  トレース形式が表現できない値は存在しないので、これで十分
- `flags_` を uint8 ではなく uint32 にしたのは**アラインメントのため**。
  uint32 + uint8 だと合計 29 B になり `data_` が中途半端な位置から始まる。
  uint32 を 2 つにすれば合計 32 B で 8 の倍数に揃い、ペイロードのアクセスが素直になる

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

`cachelib/cachebench/cache/Cache.h`、`Cache<Allocator>::allocate` の定義(`:1085-1098` 付近)。

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
      cv->setOrigSize(origSize != 0 ? origSize : static_cast<uint32_t>(size));
      cv->setRepresentative(isRep);
    }
  } catch (const std::invalid_argument& e) {
    XLOGF(DBG, "Unable to allocate, reason: {}", e.what());
  }
  return handle;
}
```

宣言側(`Cache.h:145` 付近)にも**デフォルト引数**を付けておくと、
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

`cachelib/cachebench/util/Request.h`、`struct Request` の public メンバ(`:187-198` 付近)。

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

`cachelib/cachebench/runner/CacheStressor.h`、`setKey()`(`:443-467`)。

```bash
grep -n "OpResultType setKey" cachelib/cachebench/runner/CacheStressor.h
```

### 変更前

```cpp
  OpResultType setKey(
      PoolId pid, ThroughputStats& stats, const std::string_view key,
      size_t size, uint32_t ttlSecs,
      const std::unordered_map<std::string, std::string>& featureMap,
      const std::string& itemValue) {
    ...
    auto it = cache_->allocate(pid, key, size, ttlSecs);
```

### 変更後

```cpp
  OpResultType setKey(
      PoolId pid, ThroughputStats& stats, const std::string_view key,
      size_t size, uint32_t ttlSecs,
      const std::unordered_map<std::string, std::string>& featureMap,
      const std::string& itemValue,
      uint32_t origSize = 0, bool isRep = true) {   // ← 追加
    ...
    auto it = cache_->allocate(pid, key, size, ttlSecs, origSize, isRep);
```

呼び出し元は 2 箇所だけなので、両方に `req.origSize, req.isRep` を足す。

| 行 | 文脈 | grep アンカー |
|---|---|---|
| `:309` | `OpType::kSet` / `kLoneSet` | `result = setKey(pid, stats, key,` |
| `:339` | `OpType::kGet` の**ミス時 lookaside 挿入** | `if (config_.enableLookaside) {` の直後 |

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

### 6.1 読み取りヘルパを用意する

Navy は cachebench を知らないので、**Navy 側に小さな読み取り関数を置く**。
`NvmItem` のレイアウトは `NvmItem.h:154-167` に明記されており、blob へのアクセスは
`getBlob(size_t)`(`:114`、public)が用意されている。

```bash
grep -n "Blob getBlob" cachelib/allocator/nvmcache/NvmItem.h
```

**blob 0 が parent** であることはソースで確認済み(`NvmCache.h:1196`、
`blobs.push_back(makeBlob(item))` が chained ループより前にある)。
だから `getBlob(0)` の先頭が CacheValue である。

**オフセットを自前で計算してはいけない。** `getBlob(0)` を使えば chained items があっても正しく動く。

### 6.2 分類を差し込む場所

`cachelib/navy/block_cache/BlockCache.cpp` の `allocateForInsert`(`:1278-1303`)。

```bash
grep -n "All newly inserted items are assigned with the lowest priority" \
     cachelib/navy/block_cache/BlockCache.cpp
```

### 変更前

```cpp
  // All newly inserted items are assigned with the lowest priority
  auto retVal = allocateImpl(hk, valueSize, kDefaultItemPriority, canWait);
```

### 変更後

```cpp
  uint16_t priority = kDefaultItemPriority;
  const auto* cv = probeCacheValue(value);   // 6.1 のヘルパ
  if (cv != nullptr && cv->getOrigSize() != 0) {
    priority = classOf(cv->getOrigSize());   // 0 / 1 / 2
  } else {
    headerMissCount_.inc();                  // 定常運転で 0 であるべき
  }
  // ★重要★ numPriorities_ を超える priority を渡してはならない
  if (numPriorities_ > 0) {
    priority = std::min<uint16_t>(priority, numPriorities_ - 1);
  } else {
    priority = kDefaultItemPriority;
  }
  auto retVal = allocateImpl(hk, valueSize, priority, canWait);
```

`allocateForInsert` は現在 `value` 本体を受け取っていない(引数は `hk` と `valueSize` だけ)。
**呼び出し元は `:293` の 1 箇所しかない**ので、引数に `BufferView value` を足すのが最小変更である。

```bash
grep -n "allocateForInsert(hk, value.size())" cachelib/navy/block_cache/BlockCache.cpp
```

### なぜクランプが要るのか

`Allocator::allocate` のコメント(`Allocator.h:98-101`)にこう書かれている。

> When allocating with a priority, the priority must NOT exceed the max priority
> which is (numPriorities - 1)

つまり `numPriorities = 1` のまま priority 2 を渡すと**壊れる**。
クランプを入れておけば、Step 8 で `numPriorities` を 3 にするまで
**全部 priority 0 に丸められ、挙動が今までと完全に同じになる**。
これが「Step 7 まで結果が変わらない」ことの仕掛けである。

### 変更後の予測動作

| 項目 | 予測 |
|---|---|
| `numPriorities = 1` のまま | priority が全部 0 にクランプされ、**ベンチ結果は変更前と一致する** |
| `headerMissCount_` | **0**。0 でなければ値が書けていないか読み位置が違う |

---

## Step 7: 代表だけ数える

### 7.1 挿入側

`BlockCache::insert`(`:282-306`)。`allocateForInsert` の成功後に足す。

```cpp
  if (cv != nullptr && cv->isRepresentative()) {
    splitThreshold_.onAdmit(cv->getOrigSize());
    repAdmitCount_.inc();
  }
```

### 7.2 退避側

`BlockCache::onRegionReclaim`(`:672-730`)の switch。
`hk` と `value` は `:709-711` で既に手元にある。

```bash
grep -n "AllocatorApiResult reinsertionRes = reinsertOrRemoveItem" \
     cachelib/navy/block_cache/BlockCache.cpp
```

```cpp
    switch (reinsertionRes) {
    case AllocatorApiResult::EVICTED:
    case AllocatorApiResult::EXPIRED: {
      evictionCount++;
      usedSizeBytes_.sub(decodeSizeHint(encodeSizeHint(entrySize)));
      const auto* cv = probeCacheValue(value);          // ← 追加
      if (cv != nullptr && cv->isRepresentative()) {    // ← 追加
        splitThreshold_.onEvict(cv->getOrigSize(), /* freq = */ ...);
        repEvictCount_.inc();
      }
      break;
    }
```

### 7.3 再挿入の優先度 — ここは必ず直す

`reinsertOrRemoveItem` の中(`:1208-1215`)。

### 変更前

```cpp
  // Priority of an re-inserted item is determined by its past accesses
  // since the time it was last (re)inserted.
  uint16_t priority =
      numPriorities_ == 0
          ? kDefaultItemPriority
          : std::min<uint16_t>(lr.currentHits(), numPriorities_ - 1);
```

### 変更後

```cpp
  // サイズクラスを優先度に流用しているため、アクセス回数による昇格は行わない。
  // 再挿入されるアイテムは、今いるリージョンのクラスに留める。
  uint16_t priority =
      numPriorities_ == 0
          ? kDefaultItemPriority
          : regionManager_.getRegion(currAddr.rid()).getPriority();
```

`Region::getPriority()` は `Region.h:118` にある。

### なぜこれを直さないといけないのか

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

`cachelib/navy/block_cache/BlockCache.h:93`。

```bash
grep -n "allocatorsPerPriority" cachelib/navy/block_cache/BlockCache.h
```

```cpp
    std::vector<uint32_t> allocatorsPerPriority{1};
```

`numPriorities_` はこの vector の**要素数**から決まる(`BlockCache.cpp:218-219`)。
なので要素を 3 つにすれば優先度が 3 段になる。

```cpp
    std::vector<uint32_t> allocatorsPerPriority{1, 1, 1};   // クラス 0 / 1 / 2
```

設定経由で渡すなら `cachelib/allocator/nvmcache/NavyConfig.h:474` のセッターを使う。

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
| **chained items 関連** | cachebench で鎖ができるのは `OpType::kAddChained`(`CacheStressor.h:357-391`)だけで、`sizeBegin + 1 != sizeEnd` が前提。チャンク再生は `sizes[0]` しか使わないので鎖は発生しない。仮に発生しても `getBlob(0)` を使っていれば正しく動く |

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

## 付録 A: 確認済みの事実と出典

すべて CacheLib commit `7c04a82` のソースで確認済み。

| 事実 | 出典 |
|---|---|
| CacheValue は全アイテム共通の値ヘッダ | `cachebench/cache/CacheValue.h:26-45` |
| `getSize()` は `max(size, sizeof(CacheValue))` を返す | `cachebench/cache/CacheValue.h:74-80` |
| 値の実体化は `Cache::allocate` の 1 箇所 | `cachebench/cache/Cache.h:1085-1098` |
| ペイロード書き込みはヘッダの後ろから | `cachebench/cache/Cache.h:250-256`・`:1571-1578` |
| kSet と lookaside が同じ `setKey` を通る | `cachebench/runner/CacheStressor.h:309`・`:339` → `:443` |
| 鎖は `kAddChained` でのみ発生 | `cachebench/runner/CacheStressor.h:357-391` |
| blob 0 は parent | `allocator/nvmcache/NvmCache.h:1194-1204` |
| NvmItem のレイアウトと `getBlob` | `allocator/nvmcache/NvmItem.h:109`・`:114`・`:154-167` |
| 挿入は最低優先度に固定されている | `navy/block_cache/BlockCache.cpp:1286-1287` |
| 再挿入優先度はアクセス回数で決まる | `navy/block_cache/BlockCache.cpp:1208-1215` |
| priority は `numPriorities - 1` を超えてはならない | `navy/block_cache/Allocator.h:98-101` |
| `numPriorities_` は vector の要素数 | `navy/block_cache/BlockCache.cpp:218-219`・`BlockCache.h:93` |
| Region に優先度の getter/setter がある | `navy/block_cache/Region.h:112`・`:118` |
| 退避処理の位置 | `navy/block_cache/BlockCache.cpp:672-730` |

## 付録 B: 実装時に判断が必要な点

- **`probeCacheValue()` の置き場所**: Navy から `NvmItem` を参照すると、
  navy が allocator 層に依存することになる。依存を避けたい場合は
  「blob 0 の先頭を返す関数」を `BlockCacheConfig` 経由で cachebench 側から注入する。
  研究用プロトタイプとして直接参照でも通るが、上流に出すなら注入方式にすること
- **`splitThreshold_.onEvict()` に渡す `freq`(在中アクセス数)**:
  Navy のどの値から取るか。`lr.currentHits()` が候補
- **`classOf()` の閾値**: トレースごとに違う。`OBJECT_SIZE_TO_NAVY.md` 第 6 部を参照
  (meta_rnha なら 32 MiB、tencentBlock なら 32 KiB 付近)
