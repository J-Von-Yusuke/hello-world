/*
 * split_threshold.h — 統計だけでサイズ分割閾値を推定 (複数規則を比較・2段構え)
 *
 * 3本の log2サイズ・バケット・ヒストグラム:
 *   count[b]      : オブジェクト「個数」        → count_p50   ┐ admission時に即集積
 *   all_bytes[b]  : 全オブジェクトの「サイズ和」 → byte_p50    ┘ (evict待ち不要)
 *   cold_bytes[b] : OHW(在中アクセス<=1)サイズ和 → cold_byte_p50 (eviction時に集積)
 *
 * 即時性: count_p50/byte_p50/mid は admission のみで決まるので「最初から」使える。
 *         cold_byte_p50 は eviction が溜まるほど精度が上がり、min/blendを後から効かせる。
 *
 * 2段構え(λ で連続切替):
 *   mid      = geomean(count_p50, byte_p50)        (帯[count_p50,byte_p50]の中点; 狭帯は無分割)
 *   min_rule = min(byte_p50, cold_byte_p50)         (実トレース最良+1pt以内; cold必要)
 *   threshold = log空間で mid と min_rule を λ∈[0,1] 補間   (λ=0→mid, λ=1→min_rule)
 *   ＋無分割ゲート: 帯が狭い or 大poolのバイト割合が小さい → 無分割
 *
 * ドリフト追従: 指数移動平均(周期的に乗法減衰)。集積 O(1), 再計算 O(NBUCKET), メモリ ~1.2KB。
 */
#ifndef SPLIT_THRESHOLD_H
#define SPLIT_THRESHOLD_H

#include <stdint.h>
#include <string.h>

#define SST_NBUCKET        48          /* 2^0(1B) .. 2^47 */
#define SST_RECALC_OBS     100000ULL   /* この観測数ごとに 再計算＋EMA減衰 */
/* EMA: 再計算ごとに hist *= (DEN-1)/DEN。実効窓 ~ DEN×RECALC_OBS 観測 */
#define SST_EMA_DEN        16          /* 15/16 ずつ残す(=実効窓 ~160万観測) */
#define SST_NOSPLIT_PCT    5           /* 大poolバイト割合がこれ未満(%)なら無分割 */
#define SST_MIN_BAND_OCT   2           /* count_p50とbyte_p50が この octave 未満しか離れない → 無分割 */
#define SST_LAMBDA_DEFAULT 256         /* λ×256。256=min_rule(本命), 0=mid, 中間=blend */

typedef struct {
  uint64_t count[SST_NBUCKET];
  uint64_t all_bytes[SST_NBUCKET];
  uint64_t cold_bytes[SST_NBUCKET];
  uint64_t n_obs;                      /* admission観測数(再計算/減衰トリガ) */
  int      lambda_x256;                /* 0..256: mid↔min_rule の補間係数(2段構え) */
  int      min_band_oct;               /* 狭帯ゲート閾(octave) */
  int      nosplit_pct;                /* 大poolゲート閾(%) */
  int64_t  threshold;                  /* 現在の閾値; <0 は「無分割」 */
} split_thresh_t;

typedef struct {                       /* 比較用: 全候補(<0=データ不足) */
  int64_t count_p50, byte_p50, cold_byte_p50, mid, min_rule;
} sst_candidates_t;

static inline int sst_bucket(uint64_t sz) {           /* floor(log2(sz)) */
  int b = 0; while (sz > 1 && b < SST_NBUCKET - 1) { sz >>= 1; b++; } return b;
}

static inline void sst_init(split_thresh_t *s, int lambda_x256) {
  memset(s, 0, sizeof(*s));
  s->lambda_x256 = lambda_x256;        /* 例: 256=min_rule, 0=mid, 128=中間blend */
  s->min_band_oct = SST_MIN_BAND_OCT;
  s->nosplit_pct  = SST_NOSPLIT_PCT;
  s->threshold = -1;
}

/* ── 集積点1: admission(ミス挿入)時。count/byte を即時に積む(evict待ち不要) ── */
static inline void sst_on_admit(split_thresh_t *s, uint64_t obj_size) {
  int b = sst_bucket(obj_size);
  s->count[b] += 1;
  s->all_bytes[b] += obj_size;
}
/* ── 集積点2: eviction時。最終 freq で OHW を確定し cold を積む ── */
static inline void sst_on_evict(split_thresh_t *s, uint64_t obj_size, uint32_t freq) {
  if (freq <= 1) s->cold_bytes[sst_bucket(obj_size)] += obj_size;
}

static inline int sst_p50_bucket(const uint64_t *h) {  /* 累積50%のバケット番号; 空=-1 */
  uint64_t tot = 0; for (int b = 0; b < SST_NBUCKET; b++) tot += h[b];
  if (tot == 0) return -1;
  uint64_t cum = 0;
  for (int b = 0; b < SST_NBUCKET; b++) { cum += h[b]; if (cum * 2 >= tot) return b; }
  return SST_NBUCKET - 1;
}

static inline void sst_get_candidates(const split_thresh_t *s, sst_candidates_t *o) {
  int cb = sst_p50_bucket(s->count);
  int bb = sst_p50_bucket(s->all_bytes);
  int kb = sst_p50_bucket(s->cold_bytes);
  o->count_p50     = (cb < 0) ? -1 : (((int64_t)1) << cb);
  o->byte_p50      = (bb < 0) ? -1 : (((int64_t)1) << bb);
  o->cold_byte_p50 = (kb < 0) ? -1 : (((int64_t)1) << kb);
  o->mid           = (cb < 0 || bb < 0) ? -1 : (((int64_t)1) << ((cb + bb + 1) / 2));
  if (o->byte_p50 < 0) o->min_rule = -1;
  else if (o->cold_byte_p50 < 0 || o->byte_p50 < o->cold_byte_p50) o->min_rule = o->byte_p50;
  else o->min_rule = o->cold_byte_p50;
}

/* 大pool(>S)バイト割合が小さければ -1(無分割) */
static inline int64_t sst_apply_pool_gate(const split_thresh_t *s, int64_t S) {
  if (S < 0) return -1;
  uint64_t tot = 0, large = 0;
  for (int b = 0; b < SST_NBUCKET; b++) {
    tot += s->all_bytes[b];
    if ((((int64_t)1) << b) > S) large += s->all_bytes[b];
  }
  if (tot == 0 || large * 100 < tot * (uint64_t)s->nosplit_pct) return -1;
  return S;
}

/* 採用閾値を再計算: 狭帯ゲート → λで mid↔min_rule をlog補間 → 大poolゲート */
static inline void sst_recompute(split_thresh_t *s) {
  int cb = sst_p50_bucket(s->count);
  int bb = sst_p50_bucket(s->all_bytes);
  if (cb < 0 || bb < 0) { s->threshold = -1; return; }   /* データ不足 */

  /* 狭帯ゲート: count_p50 と byte_p50 が近い(=分離可能な裾が無い) → 無分割 */
  if (bb - cb < s->min_band_oct) { s->threshold = -1; return; }

  int mid_b = (cb + bb + 1) / 2;
  int kb = sst_p50_bucket(s->cold_bytes);
  int min_b = bb;                                         /* min_rule=byte_p50 (cold無ければ) */
  if (kb >= 0 && kb < bb) min_b = kb;                     /* min(byte_p50, cold_byte_p50) */

  /* log空間で mid_b と min_b を λ補間 (λ=0→mid, 256→min) */
  int S_b = mid_b + ((min_b - mid_b) * s->lambda_x256 + 128) / 256;
  if (S_b < 0) S_b = 0; if (S_b >= SST_NBUCKET) S_b = SST_NBUCKET - 1;

  s->threshold = sst_apply_pool_gate(s, ((int64_t)1) << S_b);
}

/* admission末尾で呼ぶ: 周期再計算 + EMA(乗法減衰) */
static inline void sst_tick(split_thresh_t *s) {
  if (++s->n_obs % SST_RECALC_OBS == 0) {
    sst_recompute(s);
    for (int b = 0; b < SST_NBUCKET; b++) {               /* EMA: (DEN-1)/DEN を残す */
      s->count[b]      = s->count[b]      * (SST_EMA_DEN - 1) / SST_EMA_DEN;
      s->all_bytes[b]  = s->all_bytes[b]  * (SST_EMA_DEN - 1) / SST_EMA_DEN;
      s->cold_bytes[b] = s->cold_bytes[b] * (SST_EMA_DEN - 1) / SST_EMA_DEN;
    }
  }
}

/* 配置: size>閾値 → 大pool(1); 無分割 → 常に小pool(0) */
static inline int sst_pool_of(const split_thresh_t *s, uint64_t obj_size) {
  if (s->threshold < 0) return 0;
  return (obj_size > (uint64_t)s->threshold) ? 1 : 0;
}

#endif /* SPLIT_THRESHOLD_H */
