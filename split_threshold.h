/*
 * split_threshold.h  —  統計だけでサイズ分割閾値を推定 (複数規則を比較可能)
 *
 * 3本の log2サイズ・バケット・ヒストグラムを退去(eviction)時に積む:
 *     count[b]      : 退去したオブジェクト「個数」(バケットb)        → count_p50
 *     all_bytes[b]  : 退去した全オブジェクトの「サイズ和」           → byte_p50
 *     cold_bytes[b] : そのうち OHW(在中アクセス<=1) のサイズ和        → cold_byte_p50
 *
 * これらから複数の閾値候補を同一データで比較できる:
 *     count_p50                      : 個数中央サイズ
 *     byte_p50                       : バイト中央サイズ
 *     cold_byte_p50                  : OHWだけのバイト中央サイズ
 *     mid = geomean(count_p50,byte_p50)   : 帯[count_p50,byte_p50]の中間(log中点)
 *     min_rule = min(byte_p50,cold_byte_p50)  : 本命(実トレースで最良+1pt以内)
 *
 * 集積 O(1)/eviction、計算 O(NBUCKET)、メモリ ~960B、依存なし(標準intのみ)。
 */
#ifndef SPLIT_THRESHOLD_H
#define SPLIT_THRESHOLD_H

#include <stdint.h>
#include <string.h>

#define SST_NBUCKET       48          /* 2^0(1B) .. 2^47(128TB) */
#define SST_DECAY_EVICTS  1000000ULL  /* この退去数ごとに半減(ドリフト追従) */
#define SST_RECALC_EVICTS 100000ULL   /* この退去数ごとに閾値再計算 */
#define SST_NOSPLIT_PCT   5           /* 大poolバイト割合がこれ未満(%)なら無分割 */

/* 採用する閾値規則 */
typedef enum {
  SST_RULE_MIN = 0,   /* min(byte_p50, cold_byte_p50)  ← 本命 */
  SST_RULE_MID,       /* geomean(count_p50, byte_p50)  ← 帯の中間(比較用) */
  SST_RULE_BYTE,      /* byte_p50 のみ                  ← 比較用 */
} sst_rule_t;

typedef struct {
  uint64_t count[SST_NBUCKET];       /* 個数 */
  uint64_t all_bytes[SST_NBUCKET];   /* 全体サイズ和 */
  uint64_t cold_bytes[SST_NBUCKET];  /* OHWサイズ和 */
  uint64_t n_evict;
  sst_rule_t rule;
  int64_t  threshold;                /* 現在の閾値; <0 は「無分割」 */
} split_thresh_t;

/* 候補一覧(比較用) */
typedef struct {
  int64_t count_p50, byte_p50, cold_byte_p50, mid, min_rule;
} sst_candidates_t;

/* floor(log2(sz)) (sz>=1) */
static inline int sst_bucket(uint64_t sz) {
  int b = 0;
  while (sz > 1 && b < SST_NBUCKET - 1) { sz >>= 1; b++; }
  return b;
}

static inline void sst_init(split_thresh_t *s, sst_rule_t rule) {
  memset(s, 0, sizeof(*s));
  s->rule = rule;
  s->threshold = -1;
}

/* 退去時: size と freq(在中アクセス回数)で3本に積む */
static inline void sst_on_evict(split_thresh_t *s, uint64_t obj_size, uint32_t freq) {
  int b = sst_bucket(obj_size);
  s->count[b]     += 1;
  s->all_bytes[b] += obj_size;
  if (freq <= 1) s->cold_bytes[b] += obj_size;   /* OHW = cold */
}

/* 累積が50%に達するバケット番号(重みは hist の中身: 個数 or バイト)。空なら -1 */
static inline int sst_hist_p50_bucket(const uint64_t *hist) {
  uint64_t tot = 0;
  for (int b = 0; b < SST_NBUCKET; b++) tot += hist[b];
  if (tot == 0) return -1;
  uint64_t cum = 0;
  for (int b = 0; b < SST_NBUCKET; b++) {
    cum += hist[b];
    if (cum * 2 >= tot) return b;
  }
  return SST_NBUCKET - 1;
}
/* 50%点の代表サイズ(2^bucket)。空なら -1 */
static inline int64_t sst_hist_p50(const uint64_t *hist) {
  int b = sst_hist_p50_bucket(hist);
  return (b < 0) ? -1 : (((int64_t)1) << b);
}

/* 全候補を計算(比較用) */
static inline void sst_get_candidates(const split_thresh_t *s, sst_candidates_t *o) {
  int cb = sst_hist_p50_bucket(s->count);      /* count_p50 のバケット */
  int bb = sst_hist_p50_bucket(s->all_bytes);  /* byte_p50  のバケット */
  o->count_p50     = (cb < 0) ? -1 : (((int64_t)1) << cb);
  o->byte_p50      = (bb < 0) ? -1 : (((int64_t)1) << bb);
  o->cold_byte_p50 = sst_hist_p50(s->cold_bytes);
  /* 帯の中間 = geomean(count_p50,byte_p50) = 2^((cb+bb)/2)。log空間の中点。 */
  o->mid = (cb < 0 || bb < 0) ? -1 : (((int64_t)1) << ((cb + bb + 1) / 2));
  /* 本命 = min(byte_p50, cold_byte_p50) */
  if (o->byte_p50 < 0) o->min_rule = -1;
  else if (o->cold_byte_p50 < 0 || o->byte_p50 < o->cold_byte_p50) o->min_rule = o->byte_p50;
  else o->min_rule = o->cold_byte_p50;
}

/* 無分割ゲート: 大pool(>S)のバイト割合が小さければ -1(無分割) を返す */
static inline int64_t sst_apply_gate(const split_thresh_t *s, int64_t S) {
  if (S < 0) return -1;
  uint64_t tot = 0, large = 0;
  for (int b = 0; b < SST_NBUCKET; b++) {
    tot += s->all_bytes[b];
    if ((((int64_t)1) << b) > S) large += s->all_bytes[b];
  }
  if (tot == 0 || large * 100 < tot * (uint64_t)SST_NOSPLIT_PCT) return -1;
  return S;
}

/* 採用ruleで閾値を再計算 */
static inline void sst_recompute(split_thresh_t *s) {
  sst_candidates_t c;
  sst_get_candidates(s, &c);
  int64_t S;
  switch (s->rule) {
    case SST_RULE_MID:  S = c.mid;      break;
    case SST_RULE_BYTE: S = c.byte_p50; break;
    case SST_RULE_MIN:
    default:            S = c.min_rule; break;
  }
  s->threshold = sst_apply_gate(s, S);
}

/* 毎 eviction 末尾: 周期再計算 + 指数減衰 */
static inline void sst_tick(split_thresh_t *s) {
  s->n_evict++;
  if (s->n_evict % SST_RECALC_EVICTS == 0) sst_recompute(s);
  if (s->n_evict % SST_DECAY_EVICTS == 0)
    for (int b = 0; b < SST_NBUCKET; b++) { s->count[b] >>= 1; s->all_bytes[b] >>= 1; s->cold_bytes[b] >>= 1; }
}

/* 配置: size>閾値 なら大pool(1)、無分割なら常に小pool(0) */
static inline int sst_pool_of(const split_thresh_t *s, uint64_t obj_size) {
  if (s->threshold < 0) return 0;
  return (obj_size > (uint64_t)s->threshold) ? 1 : 0;
}

#endif /* SPLIT_THRESHOLD_H */
