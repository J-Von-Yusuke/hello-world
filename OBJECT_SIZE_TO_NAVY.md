# チャンク化オブジェクトの「元サイズ」と「個数」を Navy BlockCache に伝える

- **対象**: DRAM + SSD(Navy BlockCache)ハイブリッド構成でのサイズ別リージョン分割
- **前提**: BigHash 無効、BlockCache エンジンのみ。オブジェクトは replay 層で固定長チャンクに分割済み
- **関連**: `ORACLE_GENERAL_CHUNKED_REPLAY.md`(チャンク再生の設計)/ `SplitThreshold.h`(分割閾値)/ `OracleGeneralCache.h`(統計層)
- **図解版**: `ObjectSizeToNavy_guide.html`

---

# 第 1 部 何を解決するのか

## 1.1 満たすべき 2 条件

| | 条件 | これが無いと何が壊れるか |
|---|---|---|
| ① | あるキーを持つアイテムの**元のサイズ**が分かる | サイズ別リージョン分割そのものが成立しない。1 MiB チャンクは全部「1 MiB のオブジェクト」に見える |
| ② | チャンク化されたコンテンツを**重複なく数える**(N チャンクなら代表 1 つを 1 と数える) | 個数ヒストグラムが N 倍に膨張し、サイズ帯が `chunkSize` に潰れて分割閾値が読めなくなる |

## 1.2 問題の構造

CacheLib のアイテムは `Slab::kSize = 4 MiB` を超えられない。ハイブリッド構成でも必ず DRAM を
経由するため、これが全体の天井になる。そこで 1 レコードを固定長チャンクに展開してこの不変条件を
回避しているが、その代償として**オブジェクトという単位が Navy に届かなくなる**。

```
論理オブジェクト 1 GiB
  → ChunkSplitter が 1 MiB × 1024 個に分割
    → Navy から見えるのは互いに無関係な 1024 個のアイテム
       ① 元サイズが分からない(どの item も「1 MiB」にしか見えない)
       ② 個数が 1024 倍に膨張する
```

## 1.3 よくある誤解の訂正

### 誤解 1: 「4 MiB を超えるサイズは Navy に伝えられない」

4 MiB の上限は**1 アイテムのペイロードのバイト数**にかかるもので、**サイズという数値には無関係**。
1 GiB でも 4 GiB でも、それを表す整数は 4〜8 バイトである。天井にぶつかるのは
`value.size()` から論理サイズを**推定**しようとした場合だけ。明示的に運べば上限は無い。

`oracleGeneral` の `obj_size` は uint32 なので 4 B で足りる(最大 4,294,967,295)。
形式が表現できないサイズは Navy 以前にトレースに存在しない。

### 誤解 2: 「巨大オブジェクトは chained items で 1 個のアイテムとして保存される」

**使わない。** 本設計では 1 GiB のオブジェクトは**独立した 1024 個のアイテム**として保存され、
読み出しは 1024 回の GET とその再構成である。DRAM でも SSD でも「1 GiB のアイテム」は
どこにも存在しない。1 GiB という数字は replay 層の集計と、値ヘッダに書いた `origSize` という
注記の中にしかない。

| | チャンク化(本設計) | chained items(不使用) |
|---|---|---|
| キー | N 個 | 1 個 |
| lookup | N 回 | 1 回 |
| 退避 | チャンク単位(**部分残留あり**) | 鎖ごと丸ごと(原子的) |
| 再構成の責任 | アプリ(replay 層) | CacheLib 内部 |
| 実効的な上限 | 無制限 | Navy 経由で約 33.5 MiB |

chained items は「1 個のキーで、4 MiB を超える 1 つの塊を、全か無かで扱いたい」ための機構であり、
用途は (a) 既存アイテムへの追記、(b) 断片では意味を成さないデータ、(c) 巨大な割当クラスの回避。

**本設計で使わない理由は 2 つあり、2 つ目が本質的**:

1. Navy のエントリは `kMaxItemSize = 512 × UINT16_MAX ≈ 33.5 MiB` が上限なので、1 GiB は鎖でも入らない
2. 鎖にすると**部分残留が消える**。リージョン退避が巨大オブジェクトの一部だけを消す現象こそが測定対象であり、
   鎖は全部まとめて動くので測りたいものが構造的に発生しなくなる
   (`objFullHit%` / `objPartialHit%` と head/tail ヒット率差がその計器)

実世界の CDN やブロブストアも巨大コンテンツは範囲リクエストで断片ごとに扱うため、
チャンク化はその挙動の再現でもある。

---

# 第 2 部 検討した 3 案と棄却の根拠

情報を Navy に届ける経路は原理的に 3 つしかない。**キー**・**外部の写像**・**値**である
(`insertAsync(HashedKey, BufferView)` 以外の入口が無いため)。

## 2.1 案 A: キーにサイズを埋める — 棄却

`<objId 20>_<chunkIdx 6>_<objSize 10>` の 38 字固定キーにする案。

**サイズは可変属性なので識別子に混ぜてはならない**という一点で破綻する。
同一 `objId` でサイズが変わったとき:

1. 全チャンクが別キーになり**偽ミス**
2. 旧チャンク集合が無効化されず残留し、SSD 占有とリージョン占有率が**二重計上**される
   → **測定対象である WAF とリージョン指標そのものを汚染する**

原理的にも、チャンク *k* の中身はバイト範囲 [*kC*, (*k*+1)*C*) でありサイズ変化後も不変なので、
重なるチャンクは本来ヒットすべきである。**チャンクの同一性は (objId, chunkIdx) だけで決まる。**

### 正規化で回避しようとした場合の改変量(全トレース実測)

「objId → サイズ」を関数にする前処理(初出サイズへの正規化)を入れれば案 A は成立するが、
その改変量を全 5 トレースで実測した結果が次表である。

| トレース | records | distinct | サイズ変動 object | 書き換わる request | **クラス境界を跨ぐ request** |
|---|---|---|---|---|---|
| w01 | 215,796,257 | 122,804,013 | 0% | 0% | **0%** |
| msr_usr_1 | 45,283,980 | 13,966,057 | 0% | 0% | **0%** |
| meta_rnha | 96,680,668 | 35,040,280 | 0.772% | 4.552% | **1.08%**(閾値 32 MiB) |
| block_traces_3 | 13,956,157 | 6,763,511 | 28.539% | 41.345% | **6.90%**(閾値 256 KiB) |
| tencentBlock_11784 | 51,008,148 | 2,849,037 | 31.106% | 33.269% | **10.30%**(閾値 32 KiB) |

右端が本質。**tencentBlock ではリクエストの 10.3% がサイズクラスの所属ごと変わる**。
サイズ別分割の効果を測る実験で、入力の 1 割の所属を前処理で動かすのでは評価が成立しない。

> 旧記録の「tencentBlock は 27%」は誤り。object 基準 31.1% / request 基準 33.3% が正しい。
> `ORACLE_GENERAL_CHUNKED_REPLAY.md` §落とし穴 2 の数値も要更新。

## 2.2 案 B: 外部のヒント表 — 棄却

キーは 27 字のまま据え置き、`objId → 元サイズ` の有界な直接マップ表を別に持ち、
BlockCache がキー先頭 20 桁から `objId` を parse して引く案。トレース改変は不要になる。

**棄却理由は 2 つ**:

1. **メモリを別途消費する**(実用的なサイズで 128 MB〜)
2. **item の lookup table と機能が重複している**。既にキーからアイテムを引く機構がある系に、
   キーから属性を引く機構をもう一つ足すのは構造として冗長

加えて表は有限なので情報が失われうる。欠落率 ≈ 1 − exp(−*M*/*S*)、
*M* ≈ DRAM 容量 ÷ `chunkSize`、*S* = 表のスロット数。
DRAM 10 GiB / chunk 64 KiB・*S* = 2²⁴ で約 0.97%。

### 副産物: chunkIdx 下界(最終設計でも使う)

チャンク *k* が存在することは、親が少なくとも (*k*+1)×`chunkSize` あることを**証明する**。
閾値 *T*、チャンク *C* とすると、(*k*+1)·*C* ≥ *T* を満たすチャンクは
**ヘッダが読めなくても正しく大クラスに入る**。ヘッダ欠落時のフォールバックとして有効。

## 2.3 案 C: 値の先頭に埋める — 採用

情報をアイテム自身に同居させる。外部状態がゼロになり、値はリージョン内に保存されるので
**リクレイム時の退避処理でも再起動後でも同じヘッダを読み直せる**。

| | A: キー埋め込み | B: ヒント表 | **C: 値ヘッダ** |
|---|---|---|---|
| トレース改変 | 必要(10.3% の歪み) | 不要 | **不要** |
| 情報の欠落 | なし | 0.03%(実効) | **なし** |
| 追加メモリ | なし | 128 MB〜 | **なし** |
| 既存機構との重複 | なし | lookup table と重複 | **なし** |
| リクレイム退避時 | キーから読める | 表次第 | **値から読める** |
| 層の結合 | なし | なし | NvmItem のレイアウト 1 点 |

---

# 第 3 部 最終設計

## 3.1 値ヘッダ(8 バイト)

各チャンクの値の先頭に 8 バイト置く。

```
[ magic 3B | flags 1B ][ origSize 4B ][ payload ... ]
                  ^                ^
                  |                +-- 親オブジェクトの論理サイズ(全チャンクが同じ値)
                  +-- bit0 = representative(このピースだけを 1 と数える)
```

- **条件 ①** を `origSize` が担う
- **条件 ②** を rep フラグが担う

## 3.2 代表判定を「キーの規約」ではなく「フラグ」に置く理由

当初は「代表 = `chunkIdx` が 0」とキーを parse して判定する設計だった。これをフラグに移すと、
**BlockCache がキー形式に一切依存しなくなる**。

- `oracleGeneral` 再生の `<objId 20>_<chunkIdx 6>` でも
- PieceWise の `|#|` 区切りキーでも

同じコードがそのまま動く。どのピースを代表にするかは各 generator が決めればよい
(`oracleGeneral` なら chunk 0、PieceWise なら必ず存在する header ピース)。

## 3.3 BlockCache が行う 2 つの動作(これだけ)

| 動作 | 対象 | 内容 |
|---|---|---|
| **分類** | 全チャンク | ヘッダを読み、magic 一致なら `origSize` でクラスを決め、priority として `Allocator::allocate()` に渡す。全チャンクが同じ `origSize` を読むので、**巨大オブジェクトの全チャンクが同じクラスのリージョンに揃う** |
| **計数** | rep フラグの item のみ | insert で 1 回、リクレイム退避で 1 回だけ計上。**N チャンクのオブジェクトも 1 と数えられる**。退避時に読み直せるのは、値がリージョン内に保存されているため |

## 3.4 enableLookaside = false への対応

PieceWise-Replay を `enableLookaside = false` で回すと、挿入は kGet のミス経由ではなく
明示的な kSet で起きる。**ヘッダの書き込みを挿入経路ではなく「値を実体化する 1 箇所」に置く**
ことで、この違いを設計から消せる。どちらの経路も
`allocate()` → 値を書く → `insertOrReplace()` という同じ流れを通るからである。

```
kGet ミス → lookaside 挿入  ┐
                            ├→ 値を実体化する唯一の箇所(先頭 8 B を書く)
kSet による明示挿入         ┘        ↓
                              NvmCache が NvmItem でラップ
                                     ↓
                              Navy BlockCache が読む
```

## 3.5 chained items への対応

`allocateChainedItem()` を明示的に呼ばない限り鎖は発生しない(サイズ超過での自動分割は無い)。
cachebench で鎖ができるのは generator が `Request::sizes` に複数要素を積んだときだけで、
先頭が parent、2 個目以降が chained item になる。現在の generator は `sizes[0]` しか
設定しないため鎖は生じない。**「CacheLib で無効」ではなく「generator が要求していない」状態**である。

**鎖が生じても Navy のエントリは 1 個のまま。** NvmCache が parent と鎖全体を 1 つの
`NvmItem` に直列化するため、**条件 ② の計数は影響を受けない**。変わるのは `numBlobs` が
1 + 鎖長になることで実データ先頭のオフセットがずれる点だけ。

そこで**オフセットを固定値で渡すのをやめ、ヘッダを取り出す関数そのものを渡す**(§4.5)。
`getBlob(0)` が内部で `numBlobs` を勘定するので、鎖があってもなくても同じコードが
正しい位置を返す。**鎖を使う予定が無くても、こちらの方が堅牢である。**

---

# 第 4 部 実装(変更は 4 箇所)

## 4.1 `cachebench/util/Request.h` — フィールド 2 つ追加

```cpp
struct Request {
  // ... 既存 ...
  uint32_t origSize{0};    // 親オブジェクトの論理サイズ (0 = ヘッダを書かない)
  bool     isRep{false};   // このピースを代表として 1 と数える
};
```

## 4.2 Navy 側のヘッダ定義 — 「読むだけ」の構造体

```cpp
namespace facebook::cachelib::navy {

struct ValueHeader {                  // 8 B, little endian
  uint32_t magicAndFlags;             // 上位 24 bit = magic, 下位 8 bit = flags
  uint32_t origSize;

  static constexpr uint32_t kMagic = 0xCAC5E7;   // 24 bit
  static constexpr uint32_t kRep   = 0x01;       // flags bit0

  bool valid() const { return (magicAndFlags >> 8) == kMagic; }
  bool isRep() const { return (magicAndFlags & kRep) != 0; }
};
static_assert(sizeof(ValueHeader) == 8, "value header must be 8B");

} // namespace
```

## 4.3 `CacheStressor` — 値を実体化する 1 箇所

```cpp
auto handle = cache_->allocate(pid, *req.key, sz);
if (handle) {
  if (req.origSize != 0) {
    auto* h = reinterpret_cast<navy::ValueHeader*>(handle->getMemory());
    h->magicAndFlags = (navy::ValueHeader::kMagic << 8)
                     | (req.isRep ? navy::ValueHeader::kRep : 0u);
    h->origSize = req.origSize;
  }
  cache_->insertOrReplace(handle);
}
```

lookaside の insert-on-miss と kSet がこの経路を共有していることを実装で必ず確認する。
共有していなければ、両方から呼ぶ小さなヘルパに切り出す。

## 4.4 各 generator — 値を設定するだけ

| generator | `origSize` | `isRep` |
|---|---|---|
| `oracleGeneral` チャンク再生 | `ChunkSplitter` が保持する親サイズ | `chunkIdx == 0` |
| PieceWise-Replay | `PieceWiseReqWrapper` の `fullObjectSize` | header ピースのとき |

## 4.5 Navy — probe 関数とクラス判定

**navy を NvmItem に依存させないこと。** `BlockCacheConfig` に「ヘッダを取り出す関数」と
「magic」を渡し、BlockCache は**関数の型と 8 B 構造体**以上のことを知らない設計にする。
レイアウトの知識は cachebench 側の 1 箇所に閉じ込め、単体テストで固定する。

```cpp
// navy 側 — 関数の型と 8 B 構造体しか知らない
using HeaderProbe = folly::Function<const ValueHeader*(BufferView) const>;
// BlockCacheConfig::setHeaderProbe(HeaderProbe);

// cachebench 側 — レイアウトを知っているのでここだけで実装する
cfg.setHeaderProbe([](navy::BufferView v) -> const navy::ValueHeader* {
  const auto* ni = reinterpret_cast<const NvmItem*>(v.data());
  if (ni->getNumBlobs() == 0) { return nullptr; }
  auto blob = ni->getBlob(0);                       // parent は blob 0
  if (blob.data.size() < sizeof(navy::ValueHeader)) { return nullptr; }
  return reinterpret_cast<const navy::ValueHeader*>(blob.data.data());
});
```

書き込み側は **parent の割当の先頭**に置く。parent は常に存在し、常に blob 0 である。
`origSize` には鎖の断片長ではなく**論理オブジェクト全体のサイズ**を入れる。

```cpp
// BlockCache::insert / リクレイム退避処理
const auto* h = headerProbe_(value);
uint16_t priority;
if (h != nullptr && h->valid()) {
  priority = classOf(h->origSize);         // ← 条件 ①
  if (h->isRep()) {                        // ← 条件 ②
    splitThreshold_.onAdmit(h->origSize);
    nRepAdmit_.inc();
  }
} else {
  nHeaderMiss_.inc();                      // 定常運転で 0 でなければならない
  priority = classOf(std::max<uint64_t>(   // §2.2 の chunkIdx 下界フォールバック
      value.size(), lowerBoundFromChunkIdx));
}
allocator_.allocate(size, priority, ...);
```

`numPriorities = 3` にすると `Allocator` が優先度ごとに別の open region を保持し、
リージョン自身が自分の優先度を記録する。これが「リージョンに 1/2/3 を振る」機構にそのまま対応する。

**転用時の必須手当て**:

- リクレイム再挿入は現リージョンの優先度を維持する(昇格ロジックが残るとクラスを跨ぐ)
- 本来の reinsertion 優先度機能は使えなくなる。両立には `numPriorities = 3 × ホットネス段数` の 2 次元タグ

---

# 第 5 部 不変条件と計測

## 5.1 出すべきカウンタ

| カウンタ | 期待値 | ずれたときの意味 |
|---|---|---|
| `nHeaderMiss` | **0** | NvmItem レイアウト変更、または probe 関数が parent blob を見失った |
| `numBlobs != 1` の件数 | **0** | 意図せず chained items が発生している(§3.5) |
| `nRepAdmit` | distinct オブジェクト数と一致 | 代表フラグの設定漏れ、または重複計上 |
| クラス跨ぎオブジェクト数 | ほぼ 0 | 同一オブジェクトのチャンクが別クラスへ散った |
| 孤児チャンク数とバイト量 | 小さい | §5.3 の系統誤差の量 |
| クラス別リージョンあたり収容数 | — | 研究の測定対象そのもの |

> **magic 不一致は必ずフォールバック + カウンタに落とすこと。**
> CacheLib を更新して NvmItem のレイアウトが変わった瞬間にカウンタが立つので、
> 黙って誤分類し続ける事故が構造的に起きなくなる。
> これが値埋め込みの唯一の弱点(層の結合)に対する保険である。

## 5.2 前提として assert すること

- `numBlobs == 1`(= 鎖が発生していない)。§4.5 の probe 関数を使えば鎖があっても動くが、
  初期段階では 1 であることをカウンタで確認する
- **blob 0 が parent であること**を単体テストで往復確認する。CacheLib の鎖は新しい要素を
  先頭に繋ぐ連結リストなので、直列化時の並び順が挿入順と逆になる可能性がある。
  違っていた場合も probe 関数側だけ直せば済む構造にしておく
- 値の長さが最低 8 B あること。サイズ 0 → 1 B にクランプされるオブジェクトは 8 + 1 B になる
- `chunkSize = アロケーションクラスサイズ − 35(ヘッダ 8 + キー 27) − アイテムヘッダ長`
  に合わせ、割当クラス境界を跨がないこと

> 現行の `chunkSize = 65536` は `65536 + 27 + 32 = 65595` で既に 64 KiB クラスを超えており、
> **本改修と無関係に次のクラスへ落ちて浪費している可能性がある**。一度確認すること。

## 5.3 残る既知の系統誤差

**孤児チャンク**: オブジェクトのサイズが縮小した後に再投入されると、旧バージョンの末尾チャンクが
孤児として旧クラスのリージョンに残る。二度と参照されないので LRU で抜けるが、その間はリージョン
占有に乗る。これはキー設計や値ヘッダとは無関係な、**チャンク化そのものに固有の現象**である。
消そうとするのではなく、孤児チャンク数とバイト量をカウンタに出して報告する。

**代表チャンクによる近似**: 「rep の item が在中 = オブジェクトが在中」という計数なので、
部分残留(rep だけ退避されて他チャンクが残る、またはその逆)の間は数え方に近似が入る。
既存の `objFullHit%` / `objPartialHit%` と head/tail ヒット率差がそのまま測定器になるので、
バイアスの量は数字で示せる。

## 5.4 コスト

| chunkSize | 8 B ヘッダのオーバーヘッド |
|---|---|
| 1 MiB | 0.0008% |
| 64 KiB | 0.012% |
| 5 KiB | 0.16% |

全アームで共通なので、A/B 比較には影響しない。

---

# 第 6 部 トレース別の適用指針

## 6.1 閾値の設定

3 クラス設計(例: ≤5 KiB / 中間 / ≥30 MiB)を素朴に適用すると、
**大半のトレースで最上位クラスが空になる**。最大オブジェクトサイズは
tencentBlock 512 KiB、w01 1.00 MiB、msr_usr_1 512 KiB、block_traces_3 9 MiB である。

実測の最適分割点(tencentBlock ≈ 32 KiB、w01 ≈ 256 KiB)の帯に閾値を下げるか、
本物の大サイズコンテンツを含む CDN トレースを追加すること。

## 6.2 meta_rnha の右打ち切り(重要)

meta_rnha は唯一 GiB 級のサイズを含むが、**`obj_size` が uint32 で右打ち切りされている**。
全 96,680,668 レコードの実測:

| 項目 | 値 |
|---|---|
| 値 4294967295(uint32 飽和)のレコード | 839,205(0.868%) |
| 同・distinct object | 173,456(0.495%) |
| **飽和値が占める全 request bytes** | **80.3%** |
| **飽和値が占める footprint** | **97.4%** |
| 飽和値を除いた実最大 | 2,126,627,943 = 1.9806 GiB |
| **[2 GiB, 4 GiB) の実サイズ** | **0 件**(全て飽和値) |

**2 GiB に閾値を置くと、真のサイズが記録されなかったオブジェクトだけを隔離するクラスができる。**
しかもそれが全リクエストバイトの 80.3% を占めるため、BMR も WAF の分母も丸ごと人工物の上に乗る。

### このトレースで採るべき設定

| 項目 | 値 | 根拠 |
|---|---|---|
| 飽和レコード | **除外を宣言** | footprint が 695.5 TiB → **17.9 TiB** となり実験が成立する規模になる |
| 閾値 | **32 MiB** | ≥32 MiB が実バイトの 90.7%、distinct で 195,815 obj(0.56%)が 10.5 TiB(58.7%) |
| chunkSize | **1 MiB** | 64 KiB だと 686 億オペ(飽和除外でも 136 億)で非現実的。1 MiB なら約 9 億 |
| regionSize | **256 MiB** | entries/region = 256 を維持(64 KiB / 16 MiB と同じ比) |

飽和レコードの除外は真の最大オブジェクトを落とすため、**分割の効果を過小評価する方向 = 保守的**
であり、「打ち切られた観測を除いた保守的な設定でもこれだけ効いた」と書ける。

### 参考: [32, 64) MiB の強いモード

同帯 5,464,349 件のうち **3,702,020 件(68%)が厳密に 52,428,800 B = 50.000 MiB**。
これは打ち切りのような形式由来の人工物ではなく、システム側のセグメント長とみられる
**実在の構造**である。閾値の根拠として論文に書けるのはこちら。

## 6.3 飽和値を除いた実バイト分布(meta_rnha)

| サイズ帯 | 実バイトの割合 |
|---|---|
| ≤ 1 MiB | 0.9% |
| 1 – 2 MiB | 0.5% |
| 2 – 4 MiB | 1.0% |
| 4 – 8 MiB | 1.6% |
| 8 – 16 MiB | 2.5% |
| 16 – 32 MiB | 2.8% |
| **32 – 64 MiB** | **31.0%** |
| 64 – 128 MiB | 5.0% |
| 128 – 256 MiB | 10.5% |
| 256 – 512 MiB | 14.8% |
| 512 MiB – 1 GiB | 20.6% |
| 1 – 2 GiB | 8.8% |

---

# 第 7 部 着手順

1. **§4.2 と §4.5(ヘッダの定義と BlockCache の読み取り)を先に入れる。**
   generator は生サイズのまま動かし、`nHeaderMiss` が 0 になることを確認する。
   ここで最もリスクの高い層間結合を先に潰せる
2. §4.1・§4.3・§4.4(Request のフィールドと書き込み、generator 側の設定)を載せる
3. `nRepAdmit` が distinct オブジェクト数と一致することを確認する
4. `numPriorities = 3` にして、クラス別のリージョン統計を取り始める
5. **感度分析として正規化版(= 案 A 相当)も 1 本回す。**
   w01 と msr_usr_1 では両者が完全一致するはずなので**実装の正しさ検証を兼ねられる**。
   tencentBlock での差が「可変サイズの扱いが結論を動かすか」への直接の答えになる

---

# 付録 A: 可変サイズオブジェクトの扱い(仕様の確認)

論文の評価妥当性のため、**libCacheSim 互換ではなく CacheLib 仕様に合わせる**方針を採る。
CacheLib 仕様下での可変サイズの正しい挙動は次のとおり
(GET + lookaside に SET が無く、hit 時に request のサイズを参照する経路が存在しないため)。

| 時点 | トレース | 実際の動作 |
|---|---|---|
| t1 | 10 MiB | miss → 160 chunk を挿入。大クラスへ |
| t2 | 2 MiB(在中中) | **hit。トレースの 2 MiB は無視され、10 MiB のまま扱われる** |
| t3 | 2 MiB(退避後) | chunk 0 が miss → 新サイズで再投入。32 chunk を小クラスへ |

つまり**在中中はサイズ変化が無視され、退避後の再投入のときだけ反映される**。
本設計ではこれが自然に成立する — replay 層は chunk 0 の hit で返ってきた値から
親サイズを読めるため、外部の表を持たずに t2 の挙動を再現できる。

> **要照合**: `ORACLE_GENERAL_CHUNKED_REPLAY.md` §落とし穴 2 は「libCacheSim はサイズ差を
> 完全に無視する」と書くが、`region-split-traces` の実測は「per-request サイズで更新、
> 初回サイズに潰すと BMR が壊れた」である。実測を優先すべきで、設計書 §落とし穴 2 と
> §2.5-3 は誤りの可能性が高い。libCacheSim `cache_find_base` の `occupied_byte` 更新の
> 有無を原典で再確認すること。

---

# 付録 B: 根拠の格付け

## 確認済み(実測、2026-08-05〜07)

計測スクリプトは `cachelib_integration/probes/` に格納済み(付録 C)。

| 主張 | 根拠 |
|---|---|
| 全 5 トレースの可変サイズ改変量(§2.1 の表) | `probes/probe_varsize.py` で全件走査 |
| meta_rnha の uint32 右打ち切りと実最大 1.9806 GiB | `probes/probe_meta_rnha.py` / `probe_meta_rnha2.py` |
| meta_rnha の footprint 695.5 TiB → 除外後 17.9 TiB | `probes/probe_meta_rnha3.py` |
| 50.000 MiB のモード(同帯の 68%) | `probes/probe_meta_rnha2.py` |
| 64 KiB チャンク時の総オペ数 686 億 | `probes/probe_meta_rnha2.py` |

## 弱い根拠(ソース未確認 — 実バージョンで要確認)

手元(Windows・WSL Ubuntu)に CacheLib のチェックアウトが存在しないため、
以下は未検証である(2026-08-05 確認)。

| 主張 | 確認すべき場所 |
|---|---|
| 優先度ごとの open region 機構 | `navy/block_cache/Allocator.h` の priority 引数 / `Region.h::getPriority` / `RegionManager.h` の優先度別 open region 取得 / `BlockCache.cpp::insert` の優先度決定箇所 / `BlockCacheConfig::setNumPriorities` |
| chained items が opt-in であること | `allocator/CacheAllocator.h` の `allocateChainedItem` / `addChainedItem` / `viewAsChainedAllocs` |
| cachebench で鎖ができる条件 | 値実体化箇所の `req.sizes` ループ / `cachebench/util/Config.h` の `chainedItemDistribution` 系 |
| NvmItem の blob アクセス | `allocator/nvmcache/NvmItem.h` の `getNumBlobs()` / `getBlob(idx)` |
| lookaside と kSet が値実体化経路を共有すること | `cachebench/runner/CacheStressor.h` |

---

# 付録 C: 計測スクリプト

格納場所: `cachelib_integration/probes/`。
いずれも Windows 側 Python(`zstandard` + `numpy`)で動作し、全件走査に数秒〜75 秒。
トレースのパスはスクリプト冒頭の `BASE` / `PATH` 定数で指定する。

| スクリプト | 出力 |
|---|---|
| `probe_varsize.py` | 全トレースの「サイズ変動 object % / 書き換わる request % / クラス境界を跨ぐ request %」 |
| `probe_meta_rnha.py` | log2 バケット別の件数・バイト割合、≥1 GiB の厳密値ヒストグラム(飽和検出) |
| `probe_meta_rnha2.py` | 飽和を除く実最大、帯内のモード、distinct object 数、チャンク化時の総オペ数 |
| `probe_meta_rnha3.py` | distinct footprint(飽和込み / 除外)とサイズ帯別内訳 |

**新しいトレースを追加したときは、まず `probe_varsize.py` を流して
「クラス境界を跨ぐ request %」を見ること。** これが大きいトレースでは、
サイズを識別子側に持たせる設計が使えない。
同時に打ち切り検査(`probe_meta_rnha.py` 相当)も必ずかけること。
