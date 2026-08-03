#include "cachelib/cachebench/workload/OracleGeneralCache.h"

#include <folly/Format.h>

#include <stdexcept>

namespace facebook {
namespace cachelib {
namespace cachebench {

namespace {
double pct(uint64_t n, uint64_t d) {
  return d ? 100.0 * static_cast<double>(n) / static_cast<double>(d) : 0.0;
}

std::string hb(uint64_t bytes) {
  static const char* kUnit[] = {"B", "KB", "MB", "GB", "TB", "PB"};
  double v = static_cast<double>(bytes);
  int i = 0;
  while (v >= 1024.0 && i < 5) {
    v /= 1024.0;
    ++i;
  }
  return folly::sformat("{:.2f}{}", v, kUnit[i]);
}

std::string bucketLabel(uint32_t b) {
  if (b == 0) {
    return "[0,2)";
  }
  if (b == SizeBucket::kNum - 1) {
    return folly::sformat(">={}", hb(1ull << b));
  }
  return folly::sformat("[{},{})", hb(1ull << b), hb(1ull << (b + 1)));
}
} // namespace

// ------------------------------------------------------------------ Stats

OracleGeneralCacheStats::Snap OracleGeneralCacheStats::snapOf(const Counters& c) {
  return Snap{c.chunkGets.get(), c.chunkHits.get(), c.getBytes.get(),
              c.hitBytes.get(),  c.missBytes.get(), c.objGets.get(),
              c.objFullHits.get(), c.objPartialHits.get()};
}

void OracleGeneralCacheStats::recordChunk(uint32_t bucket,
                                          uint32_t chunkIdx,
                                          uint32_t bytes,
                                          bool isHit) {
  auto bump = [&](Counters& c) {
    c.chunkGets.inc();
    c.getBytes.add(bytes);
    if (isHit) {
      c.chunkHits.inc();
      c.hitBytes.add(bytes);
    } else {
      // ミス = オリジンから取得して挿入するバイト数。WAF の分母。
      c.missBytes.add(bytes);
    }
  };
  bump(all_);
  bump(bySize_[bucket]);

  if (chunkIdx == 0) {
    headGets_.inc();
    if (isHit) {
      headHits_.inc();
    }
  } else {
    tailGets_.inc();
    if (isHit) {
      tailHits_.inc();
    }
  }
}

void OracleGeneralCacheStats::recordObject(uint32_t bucket,
                                           uint32_t objSize,
                                           uint32_t nChunks,
                                           uint32_t hits) {
  auto bump = [&](Counters& c) {
    c.objGets.inc();
    if (hits == nChunks) {
      c.objFullHits.inc();
    } else if (hits > 0) {
      c.objPartialHits.inc();
    }
  };
  bump(all_);
  bump(bySize_[bucket]);

  if (objSize == 0) {
    nZeroSize_.inc();
  }
  if (nChunks > 1) {
    nSplitObjs_.inc();
  }
  uint64_t prev = maxObjSize_.load(std::memory_order_relaxed);
  while (objSize > prev && !maxObjSize_.compare_exchange_weak(
                               prev, objSize, std::memory_order_relaxed)) {
  }
}

void OracleGeneralCacheStats::setNvmCacheWarmedUp() {
  bool expected = false;
  if (warmedUp_.compare_exchange_strong(expected, true)) {
    warmup_ = snapOf(all_);
  }
}

void OracleGeneralCacheStats::renderStats(uint64_t elapsedTimeNs,
                                          std::ostream& out) const {
  const auto s = snapOf(all_);
  const double secs = static_cast<double>(elapsedTimeNs) / 1e9;

  out << "== OracleGeneral chunked replay ==" << std::endl;
  out << folly::sformat("Objects       : {:>18}   split(>1chunk) {} ({:.2f}%)",
                        s.objGets, nSplitObjs_.get(),
                        pct(nSplitObjs_.get(), s.objGets))
      << std::endl;
  out << folly::sformat("Chunks        : {:>18}   {:.3f} chunk/obj, {:.0f} chunk/s",
                        s.chunkGets,
                        s.objGets ? static_cast<double>(s.chunkGets) / s.objGets : 0.0,
                        secs > 0 ? s.chunkGets / secs : 0.0)
      << std::endl;
  out << folly::sformat("Chunk hit     : {:>17.2f}%   {}/{}",
                        pct(s.chunkHits, s.chunkGets), s.chunkHits, s.chunkGets)
      << std::endl;
  out << folly::sformat("Byte  hit     : {:>17.2f}%   {}/{}",
                        pct(s.hitBytes, s.getBytes), hb(s.hitBytes), hb(s.getBytes))
      << std::endl;
  out << folly::sformat("Object full   : {:>17.2f}%   partial {:.2f}%",
                        pct(s.objFullHits, s.objGets), pct(s.objPartialHits, s.objGets))
      << std::endl;
  out << folly::sformat(
             "Head vs Tail  : head {:.2f}%  tail {:.2f}%  delta {:+.2f}pp",
             pct(headHits_.get(), headGets_.get()),
             pct(tailHits_.get(), tailGets_.get()),
             pct(tailHits_.get(), tailGets_.get()) -
                 pct(headHits_.get(), headGets_.get()))
      << std::endl;
  out << folly::sformat("MISS BYTES    : {:>18}   ({} B)  <- WAF の分母",
                        hb(s.missBytes), s.missBytes)
      << std::endl;
  out << folly::sformat("Anomaly       : zeroSize {}  maxObjSize {}",
                        nZeroSize_.get(), hb(maxObjSize_.load()))
      << std::endl;

  if (warmedUp_.load(std::memory_order_relaxed)) {
    out << folly::sformat(
               "post-warmup   : chunkHit {:.2f}%  byteHit {:.2f}%  missBytes {}",
               pct(s.chunkHits - warmup_.chunkHits, s.chunkGets - warmup_.chunkGets),
               pct(s.hitBytes - warmup_.hitBytes, s.getBytes - warmup_.getBytes),
               hb(s.missBytes - warmup_.missBytes))
        << std::endl;
  }

  const auto e = objLatency_.estimate();
  out << folly::sformat(
             "Object latency: p50 {} p90 {} p99 {} p999 {} p100 {} (ns)",
             e.p50, e.p90, e.p99, e.p999, e.p100)
      << std::endl;

  out << "-- by object size --" << std::endl;
  out << folly::sformat("{:>16} {:>16} {:>10} {:>10} {:>12} {:>12}", "size range",
                        "objGets", "fullHit%", "byteHit%", "getBytes", "missBytes")
      << std::endl;
  for (uint32_t b = 0; b < bySize_.size(); b++) {
    const auto t = snapOf(bySize_[b]);
    if (t.objGets == 0) {
      continue;
    }
    out << folly::sformat("{:>16} {:>16} {:>9.2f}% {:>9.2f}% {:>12} {:>12}",
                          bucketLabel(b), t.objGets, pct(t.objFullHits, t.objGets),
                          pct(t.hitBytes, t.getBytes), hb(t.getBytes), hb(t.missBytes))
        << std::endl;
  }
}

void OracleGeneralCacheStats::renderStats(uint64_t,
                                          folly::UserCounters& counters) const {
  const auto s = snapOf(all_);
  // 比率は basis point (万分率) で出す。UserCounters が整数のため。
  counters["og_objects"] = s.objGets;
  counters["og_chunks"] = s.chunkGets;
  counters["og_chunk_hit_bp"] = static_cast<int64_t>(100 * pct(s.chunkHits, s.chunkGets));
  counters["og_byte_hit_bp"] = static_cast<int64_t>(100 * pct(s.hitBytes, s.getBytes));
  counters["og_obj_fullhit_bp"] =
      static_cast<int64_t>(100 * pct(s.objFullHits, s.objGets));
  counters["og_get_bytes"] = s.getBytes;
  counters["og_miss_bytes"] = s.missBytes; // WAF = navy_device_bytes_written / これ
}

void OracleGeneralCacheStats::renderWindowStats(double elapsedSecs,
                                                std::ostream& out) const {
  const auto s = snapOf(all_);
  const auto& p = lastWindow_;
  const uint64_t dChunk = s.chunkGets - p.chunkGets;
  out << folly::sformat(
             "  oracleGeneral: {:.0f} chunk/s, chunkHit {:.2f}%, byteHit {:.2f}%, "
             "missBytes +{}",
             elapsedSecs > 0 ? dChunk / elapsedSecs : 0.0,
             pct(s.chunkHits - p.chunkHits, dChunk),
             pct(s.hitBytes - p.hitBytes, s.getBytes - p.getBytes),
             hb(s.missBytes - p.missBytes))
      << std::endl;
  lastWindow_ = s;
}

// ---------------------------------------------------------------- Adapter

OracleGeneralCacheAdapter::OracleGeneralCacheAdapter(uint32_t chunkSize,
                                                     const std::string& missTraceFile)
    : chunkSize_(chunkSize) {
  if (chunkSize_ == 0) {
    throw std::invalid_argument("chunkSize must be > 0");
  }
  if (!missTraceFile.empty()) {
    missTrace_.open(missTraceFile, std::ios::out | std::ios::trunc);
    if (!missTrace_) {
      throw std::invalid_argument("cannot open missTraceFile: " + missTraceFile);
    }
    missTrace_ << "objId,objSize,chunkIdx,chunkBytes\n";
  }
}

void OracleGeneralCacheAdapter::recordNewObject(OracleGeneralObjAccum& acc,
                                                uint64_t objId,
                                                uint32_t objSize) {
  acc.objId = objId;
  acc.objSize = objSize;
  acc.nChunks = ogNumChunks(objSize, chunkSize_);
  acc.issued = 0;
  acc.hits = 0;
  acc.bucket = SizeBucket::of(objSize);
  acc.latency.emplace(stats_.latencyStats());
}

uint32_t OracleGeneralCacheAdapter::recordNewChunk(OracleGeneralObjAccum& acc,
                                                   uint32_t chunkIdx) {
  acc.curIdx = chunkIdx;
  acc.curBytes = ogChunkBytes(acc.objSize, chunkIdx, chunkSize_);
  return acc.curBytes;
}

bool OracleGeneralCacheAdapter::processResult(OracleGeneralObjAccum& acc,
                                              OpResultType result) {
  const bool isHit = (result == OpResultType::kGetHit);
  stats_.recordChunk(acc.bucket, acc.curIdx, acc.curBytes, isHit);
  acc.issued++;
  if (isHit) {
    acc.hits++;
  } else if (missTrace_.is_open()) {
    std::lock_guard<std::mutex> g(missTraceLock_);
    missTrace_ << acc.objId << ',' << acc.objSize << ',' << acc.curIdx << ','
               << acc.curBytes << '\n';
  }

  if (acc.issued < acc.nChunks) {
    return false;
  }
  stats_.recordObject(acc.bucket, acc.objSize, acc.nChunks, acc.hits);
  acc.latency.reset(); // ここで LatencyTracker のデストラクタが実測を記録する
  return true;
}

} // namespace cachebench
} // namespace cachelib
} // namespace facebook
