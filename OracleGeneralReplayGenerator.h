#pragma once

#include <zstd.h>

#include <cstdio>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <vector>

#include <folly/ThreadLocal.h>
#include <folly/logging/xlog.h>

#include "cachelib/cachebench/util/Exceptions.h"
#include "cachelib/cachebench/util/Request.h"
#include "cachelib/cachebench/workload/ReplayGeneratorBase.h"

namespace facebook {
namespace cachelib {
namespace cachebench {

// libCacheSim oracleGeneral: 24B fixed-size packed little-endian records
#pragma pack(push, 1)
struct OracleGeneralRecord {
  uint32_t timestamp;
  uint64_t objId;
  uint32_t objSize;
  int64_t nextAccessVtime; // -1 or INT64_MAX = no next access (unused here)
};
#pragma pack(pop)
static_assert(sizeof(OracleGeneralRecord) == 24, "record must be 24B packed");

// Streaming zstd reader that yields fixed-size records without ever
// materializing the decompressed trace on disk or in memory.
class ZstdRecordStream {
 public:
  explicit ZstdRecordStream(std::string path) : path_(std::move(path)) {
    open();
  }
  ~ZstdRecordStream() { closeAll(); }

  void rewind() { closeAll(); open(); }

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
      // records may straddle the output-buffer boundary
      size_t take = std::min(sizeof(rec) - got, outEnd_ - outPos_);
      std::memcpy(dst + got, outBuf_.data() + outPos_, take);
      outPos_ += take;
      got += take;
    }
    bytesOut_ += sizeof(rec);
    return true;
  }

  uint64_t bytesOut() const { return bytesOut_; }

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
        size_t rd = std::fread(inBuf_.data(), 1, inBuf_.size(), f_);
        if (rd == 0) {
          return false; // physical EOF
        }
        zin_ = {inBuf_.data(), rd, 0};
      }
      ZSTD_outBuffer zout{outBuf_.data(), outBuf_.size(), 0};
      size_t ret = ZSTD_decompressStream(zds_, &zout, &zin_);
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
  uint64_t bytesOut_{0};
};

class OracleGeneralReplayGenerator : public ReplayGeneratorBase {
 public:
  explicit OracleGeneralReplayGenerator(const StressorConfig& config)
      : ReplayGeneratorBase(config), stream_(config.traceFileName) {
    OracleGeneralRecord rec;
    for (uint64_t i = 0; i < fastForwardCount_; i++) {
      if (!stream_.readRecord(rec)) {
        throw std::invalid_argument("fastForwardCount exceeds trace length");
      }
    }
  }

  const Request& getReq(uint8_t,
                        std::mt19937_64&,
                        std::optional<uint64_t>) override {
    OracleGeneralRecord rec;
    {
      std::lock_guard<std::mutex> lg(lock_);
      if (!stream_.readRecord(rec)) {
        if (repeatTraceReplay_) {
          XLOG_EVERY_MS(INFO, 60'000)
              << "oracleGeneral EOF; restarting from beginning";
          stream_.rewind();
          stream_.readRecord(rec);
        } else {
          throw cachelib::cachebench::EndOfTrace("oracleGeneral EOF");
        }
      }
    }

    uint64_t n = ++nReads_;
    if (n <= 5) {
      XLOGF(INFO, "rec[{}] ts={} id={} sz={} nvt={}", n - 1, rec.timestamp,
            rec.objId, rec.objSize, rec.nextAccessVtime);
    }
    if (rec.objSize == 0) {
      ++nZeroSize_;
    }
    XLOG_EVERY_N(INFO, 1'000'000) << fmt::format(
        "replayed {} reqs, {} B decompressed, {} zero-size", n,
        stream_.bytesOut(), nZeroSize_.load());

    auto& ctx = *reqCtx_;
    ctx.setKey(fmt::format("{:020d}", rec.objId));
    ctx.sizes[0] = std::max<uint32_t>(rec.objSize, 1u);
    ctx.req.timestamp = rec.timestamp;
    return ctx.req;
  }

 private:
  // per-consumer-thread storage: Request keeps references into key/sizes
  struct ReqCtx {
    std::string key{"-"};
    std::vector<size_t> sizes{1};
    Request req{key, sizes.begin(), sizes.end(), OpType::kGet, 0};
    void setKey(std::string k) {
      key = std::move(k);
      req.key = key;
    }
  };

  ZstdRecordStream stream_;
  std::mutex lock_;
  std::atomic<uint64_t> nReads_{0};
  std::atomic<uint64_t> nZeroSize_{0};
  folly::ThreadLocal<ReqCtx> reqCtx_;
};

} // namespace cachebench
} // namespace cachelib
} // namespace facebook
