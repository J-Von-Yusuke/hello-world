/*
 * SplitThreshold.h — サイズ分割閾値のオンライン推定器 (CacheLib Navy 組み込み用)
 *
 * region_split_experiment/split_threshold.h (C版) の C++ 移植。
 * アルゴリズムは C 版と厳密に同一 (テストでビット単位の等価性を検証済み):
 *
 *   3本の log2 サイズ・バケット・ヒストグラム
 *     count[b]     : オブジェクト個数          → countP50   ┐ admission時に即集積
 *     allBytes[b]  : 全オブジェクトのサイズ和   → byteP50    ┘
 *     coldBytes[b] : OHW(在中アクセス<=1)サイズ和 → coldByteP50 (eviction時に集積)
 *
 *   mid      = geomean(countP50, byteP50)      … p50-p50 帯の中点
 *   minRule  = min(byteP50, coldByteP50)
 *   threshold = log空間で mid と minRule を λ∈[0,1] 補間 (λ=0→mid, λ=1→minRule)
 *   ＋無分割ゲート: 帯が狭い / 大poolのバイト割合が小さい → 無分割(threshold<0)
 *
 *   ドリフト追従: recalcEvery 観測ごとに再計算し、ヒストグラムを (emaDen-1)/emaDen
 *   に乗法減衰 (実効窓 ~ emaDen×recalcEvery 観測)。
 *
 * スレッド安全性: ホットパス (onAdmit/onEvict/poolOf) は relaxed atomic のみで
 * ロックフリー O(1)。再計算＋減衰は recalcEvery 回に1度 mutex 下で実行。
 * 減衰の load/store の間に他スレッドの増分が失われ得るが、確率的に
 * 高々「減衰1回あたり並行増分数」個で、統計量としては無視できる。
 *
 * 依存は C++17 標準ライブラリのみ (folly 不要) — CacheLib 外でも単体テスト可能。
 */
#pragma once

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>

namespace facebook {
namespace cachelib {
namespace navy {

class SplitThreshold {
 public:
  struct Config {
    // λ×256: 256=minRule(本命), 0=mid(p50-p50中点), 中間=log空間blend
    uint32_t lambdaX256{256};
    // countP50 と byteP50 がこの octave 未満しか離れない → 無分割
    uint32_t minBandOct{2};
    // 大pool(>閾値)のバイト割合がこの%未満なら無分割
    uint32_t noSplitPct{5};
    // この admission 観測数ごとに再計算＋EMA減衰
    uint64_t recalcEvery{100'000};
    // EMA: 再計算ごとに hist *= (emaDen-1)/emaDen
    uint32_t emaDen{16};
  };

  // 計測用スナップショット (<0 はデータ不足/無効)
  struct Candidates {
    int64_t countP50{-1};
    int64_t byteP50{-1};
    int64_t coldByteP50{-1};
    int64_t mid{-1};
    int64_t minRule{-1};
  };

  SplitThreshold() : SplitThreshold(Config{}) {}
  explicit SplitThreshold(Config config) : config_{config} {}

  SplitThreshold(const SplitThreshold&) = delete;
  SplitThreshold& operator=(const SplitThreshold&) = delete;

  // 集積点1: admission(ミス挿入)時に呼ぶ。O(1) ロックフリー
  // (recalcEvery 回に1度だけ再計算のため mutex を取る)。
  void onAdmit(uint64_t objSize) {
    const uint32_t b = bucketOf(objSize);
    count_[b].fetch_add(1, std::memory_order_relaxed);
    allBytes_[b].fetch_add(objSize, std::memory_order_relaxed);
    const uint64_t n = nObs_.fetch_add(1, std::memory_order_relaxed) + 1;
    if (n % config_.recalcEvery == 0) {
      recomputeAndDecay();
    }
  }

  // 集積点2: eviction時に呼ぶ。freq = 在中アクセス数(挿入自身を含む)。
  // freq<=1 (= 挿入後一度も再アクセスなし = OHW) のみ cold に積む。
  void onEvict(uint64_t objSize, uint32_t freq) {
    if (freq <= 1) {
      coldBytes_[bucketOf(objSize)].fetch_add(objSize,
                                              std::memory_order_relaxed);
    }
  }

  // 現在の採用閾値。負値は「無分割」。
  int64_t threshold() const {
    return threshold_.load(std::memory_order_relaxed);
  }

  // 配置先: size>閾値 → 大pool(1); 無分割 → 常に小pool(0)。
  uint32_t poolOf(uint64_t objSize) const {
    const int64_t t = threshold();
    return (t >= 0 && objSize > static_cast<uint64_t>(t)) ? 1 : 0;
  }

  // 計測用: 全候補閾値のスナップショット。O(kNumBuckets)。
  Candidates getCandidates() const {
    Candidates c;
    const int32_t cb = p50Bucket(count_);
    const int32_t bb = p50Bucket(allBytes_);
    const int32_t kb = p50Bucket(coldBytes_);
    if (cb >= 0) c.countP50 = int64_t{1} << cb;
    if (bb >= 0) c.byteP50 = int64_t{1} << bb;
    if (kb >= 0) c.coldByteP50 = int64_t{1} << kb;
    if (cb >= 0 && bb >= 0) c.mid = int64_t{1} << ((cb + bb + 1) / 2);
    if (bb >= 0) {
      c.minRule = (kb >= 0 && c.coldByteP50 < c.byteP50) ? c.coldByteP50
                                                         : c.byteP50;
    }
    return c;
  }

  // CacheLib の CounterVisitor 互換 (visitor(name, double) で呼ばれる)。
  // BlockCache::getCounters() から転送すれば cachelib の統計に載る。
  template <typename Visitor>
  void getCounters(const Visitor& visitor) const {
    const Candidates c = getCandidates();
    visitor("navy_bc_split_threshold", static_cast<double>(threshold()));
    visitor("navy_bc_split_count_p50", static_cast<double>(c.countP50));
    visitor("navy_bc_split_byte_p50", static_cast<double>(c.byteP50));
    visitor("navy_bc_split_cold_byte_p50",
            static_cast<double>(c.coldByteP50));
    visitor("navy_bc_split_obs",
            static_cast<double>(nObs_.load(std::memory_order_relaxed)));
    visitor("navy_bc_split_recomputes",
            static_cast<double>(nRecomputes_.load(std::memory_order_relaxed)));
  }

 private:
  static constexpr uint32_t kNumBuckets = 48; // 2^0(1B) .. 2^47

  using Hist = std::array<std::atomic<uint64_t>, kNumBuckets>;

  static uint32_t bucketOf(uint64_t size) { // floor(log2(size))
    uint32_t b = 0;
    while (size > 1 && b < kNumBuckets - 1) {
      size >>= 1;
      b++;
    }
    return b;
  }

  // 累積50%に達するバケット番号。全て空なら -1。
  static int32_t p50Bucket(const Hist& h) {
    uint64_t tot = 0;
    for (const auto& v : h) {
      tot += v.load(std::memory_order_relaxed);
    }
    if (tot == 0) {
      return -1;
    }
    uint64_t cum = 0;
    for (uint32_t b = 0; b < kNumBuckets; b++) {
      cum += h[b].load(std::memory_order_relaxed);
      if (cum * 2 >= tot) {
        return static_cast<int32_t>(b);
      }
    }
    return kNumBuckets - 1;
  }

  // 大pool(>s)のバイト割合が noSplitPct% 未満なら -1(無分割)。
  int64_t applyPoolGate(int64_t s) const {
    uint64_t tot = 0;
    uint64_t large = 0;
    for (uint32_t b = 0; b < kNumBuckets; b++) {
      const uint64_t v = allBytes_[b].load(std::memory_order_relaxed);
      tot += v;
      if ((int64_t{1} << b) > s) {
        large += v;
      }
    }
    if (tot == 0 || large * 100 < tot * config_.noSplitPct) {
      return -1;
    }
    return s;
  }

  // 採用閾値の再計算: 狭帯ゲート → λ補間 → 大poolゲート。続けて EMA 減衰。
  void recomputeAndDecay() {
    std::lock_guard<std::mutex> guard{recomputeMutex_};
    int64_t th = -1;
    const int32_t cb = p50Bucket(count_);
    const int32_t bb = p50Bucket(allBytes_);
    if (cb >= 0 && bb >= 0 &&
        bb - cb >= static_cast<int32_t>(config_.minBandOct)) {
      const int32_t midB = (cb + bb + 1) / 2;
      const int32_t kb = p50Bucket(coldBytes_);
      const int32_t minB = (kb >= 0 && kb < bb) ? kb : bb;
      int32_t sB =
          midB +
          ((minB - midB) * static_cast<int32_t>(config_.lambdaX256) + 128) /
              256;
      if (sB < 0) sB = 0;
      if (sB >= static_cast<int32_t>(kNumBuckets)) sB = kNumBuckets - 1;
      th = applyPoolGate(int64_t{1} << sB);
    }
    threshold_.store(th, std::memory_order_relaxed);
    nRecomputes_.fetch_add(1, std::memory_order_relaxed);

    const uint64_t den = config_.emaDen;
    for (Hist* h : {&count_, &allBytes_, &coldBytes_}) {
      for (auto& v : *h) {
        v.store(v.load(std::memory_order_relaxed) * (den - 1) / den,
                std::memory_order_relaxed);
      }
    }
  }

  const Config config_;
  Hist count_{};
  Hist allBytes_{};
  Hist coldBytes_{};
  std::atomic<uint64_t> nObs_{0};
  std::atomic<uint64_t> nRecomputes_{0};
  std::atomic<int64_t> threshold_{-1};
  std::mutex recomputeMutex_;
};

} // namespace navy
} // namespace cachelib
} // namespace facebook
