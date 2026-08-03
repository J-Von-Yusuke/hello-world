/*
 * OracleGeneralCache.h
 *
 * oracleGeneral(zstd) チャンク再生の「キャッシュ意味論 + 計測」層。
 * トレース I/O とスレッド機構は OracleGeneralReplayGenerator.{h,cpp} が持つ。
 *
 * CacheLib 内の対応物:
 *   PieceWiseCache.h  : PieceWiseCacheStats / PieceWiseReqWrapper / PieceWiseCacheAdapter
 *   BlockChunkCache.h : BlockChunkCacheStats / BlockReqWrapper / BlockChunkCacheAdapter
 *
 * *ReqWrapper に相当するものは意図的に置いていない。あれは「1 トレース行が
 * Metadata->Header->Body と複数オペにまたがる状態機械の状態」を運ぶ器であり、
 * こちらは 1 チャンク = 1 GET で状態機械が存在しないため。代わりに
 * OracleGeneralObjAccum が「N チャンクのうち何個ヒットしたか」を畳む。
 */
#pragma once

#include <folly/Benchmark.h>
#include <folly/lang/Bits.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <fstream>
#include <mutex>
#include <optional>
#include <ostream>
#include <string>
#include <vector>

#include "cachelib/cachebench/util/Request.h"
#include "cachelib/common/AtomicCounter.h"
#include "cachelib/common/PercentileStats.h"

namespace facebook {
namespace cachelib {
namespace cachebench {

// ---------------------------------------------------------------------------
// チャンク境界の唯一の定義。producer(生成側)と consumer(集計側)がこれを共有する
// ことで「発行したバイト数」と「集計したバイト数」がずれない。
// ---------------------------------------------------------------------------
inline uint32_t ogNumChunks(uint32_t objSize, uint32_t chunkSize) {
  const uint64_t sz = std::max<uint32_t>(objSize, 1u);
  return static_cast<uint32_t>((sz + chunkSize - 1) / chunkSize);
}

inline uint32_t ogChunkBytes(uint32_t objSize, uint32_t idx, uint32_t chunkSize) {
  const uint64_t sz = std::max<uint32_t>(objSize, 1u);
  return static_cast<uint32_t>(
      std::min<uint64_t>(chunkSize, sz - static_cast<uint64_t>(idx) * chunkSize));
}

// ---------------------------------------------------------------------------
// サイズ帯。bucket b = [2^b, 2^(b+1)) バイト。最上位バケットは >= 2^(kNum-1)。
// size-aware split threshold を決めるための内訳の軸。
// ---------------------------------------------------------------------------
struct SizeBucket {
  static constexpr uint32_t kNum = 26; // 最上位は >= 32MiB

  static uint32_t of(uint64_t sz) {
    return sz < 2 ? 0
                  : std::min<uint32_t>(folly::findLastSet(sz) - 1, kNum - 1);
  }
};

// ---------------------------------------------------------------------------
// 1 オブジェクト(= N チャンク)分の集計状態。ワーカスレッドのローカルに置く。
// 同一オブジェクトのチャンクは同一シャードキューに載る前提(下記 注記 を参照)。
// ---------------------------------------------------------------------------
struct OracleGeneralObjAccum {
  uint64_t objId{0};
  uint32_t objSize{0};
  uint32_t nChunks{0};
  uint32_t issued{0}; // 結果が確定したチャンク数
  uint32_t hits{0};   // うちヒットしたチャンク数
  uint32_t curIdx{0};
  uint32_t curBytes{0};
  uint32_t bucket{0};
  std::optional<util::LatencyTracker> latency; // オブジェクト単位(N オペ)の実測
};

// ---------------------------------------------------------------------------
// 計測。ホットパスは AtomicCounter の relaxed add のみ。
// 窓統計と warm-up 後統計はどちらも「スナップショット差分」で出すので、
// 記録側に追加の書き込み経路を作らない。
// ---------------------------------------------------------------------------
class OracleGeneralCacheStats {
 public:
  OracleGeneralCacheStats() : bySize_(SizeBucket::kNum) {}

  void recordChunk(uint32_t bucket, uint32_t chunkIdx, uint32_t bytes, bool isHit);
  void recordObject(uint32_t bucket, uint32_t objSize, uint32_t nChunks, uint32_t hits);

  util::PercentileStats& latencyStats() const { return objLatency_; }

  void renderStats(uint64_t elapsedTimeNs, std::ostream& out) const;
  void renderStats(uint64_t elapsedTimeNs, folly::UserCounters& counters) const;
  void renderWindowStats(double elapsedSecs, std::ostream& out) const;
  void setNvmCacheWarmedUp();

 private:
  struct Counters {
    AtomicCounter chunkGets{0}, chunkHits{0};
    AtomicCounter getBytes{0}, hitBytes{0}, missBytes{0};
    AtomicCounter objGets{0}, objFullHits{0}, objPartialHits{0};
  };

  struct Snap {
    uint64_t chunkGets{0}, chunkHits{0};
    uint64_t getBytes{0}, hitBytes{0}, missBytes{0};
    uint64_t objGets{0}, objFullHits{0}, objPartialHits{0};
  };

  static Snap snapOf(const Counters& c);

  Counters all_;
  std::vector<Counters> bySize_;

  // 部分残留の検出: 先頭チャンクと後続チャンクのヒット率差
  AtomicCounter headGets_{0}, headHits_{0}, tailGets_{0}, tailHits_{0};

  AtomicCounter nZeroSize_{0}, nSplitObjs_{0};
  std::atomic<uint64_t> maxObjSize_{0};

  mutable util::PercentileStats objLatency_;
  mutable Snap lastWindow_{};

  Snap warmup_{};
  std::atomic<bool> warmedUp_{false};
};

// ---------------------------------------------------------------------------
// 遷移。PieceWiseCacheAdapter の recordNewReq()/processReq() に対応する。
// Generator はこの 3 メソッドしか呼ばない。
// ---------------------------------------------------------------------------
class OracleGeneralCacheAdapter {
 public:
  explicit OracleGeneralCacheAdapter(uint32_t chunkSize,
                                     const std::string& missTraceFile = "");

  // 新しいオブジェクトの先頭チャンクを発行する直前に 1 回
  void recordNewObject(OracleGeneralObjAccum& acc, uint64_t objId, uint32_t objSize);

  // 各チャンクを発行する直前。そのチャンクのバイト数を返す(= sizes[0] に入れる値)
  uint32_t recordNewChunk(OracleGeneralObjAccum& acc, uint32_t chunkIdx);

  // 結果受領。そのオブジェクトの全チャンクが揃ったら true
  bool processResult(OracleGeneralObjAccum& acc, OpResultType result);

  const OracleGeneralCacheStats& getStats() const { return stats_; }
  OracleGeneralCacheStats& getStats() { return stats_; }
  void setNvmCacheWarmedUp() { stats_.setNvmCacheWarmedUp(); }
  uint32_t getChunkSize() const { return chunkSize_; }

 private:
  const uint32_t chunkSize_;
  OracleGeneralCacheStats stats_;
  std::ofstream missTrace_;
  std::mutex missTraceLock_;
};

} // namespace cachebench
} // namespace cachelib
} // namespace facebook
