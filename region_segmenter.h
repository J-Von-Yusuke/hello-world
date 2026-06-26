/*
 * region_segmenter.h — サイズ分割の「閾値の個数 k と分割位置」をオンラインで確定する。
 *
 *   split_threshold.h(静的な分位点 count_p50/byte_p50 中点)は一般化不能と実証された
 *   (最適閾値は count_p50–byte_p50 帯の上にも下にも中にも現れ、分位点と無関係)。
 *   本ヘッダはそれを置換する: 分布形状を仮定せず、SHARDS で抽出した直近リクエストの
 *   小さな標本上で region 単位削除(大域 region-LRU, k 分割)を「実際に回して」コストを測り、
 *   k=1..KMAX の全分割を網羅して目的(MR or BMR=SSD書込)最小の分割を選ぶ。
 *   標本上の厳密最適 + SHARDS 誤差限界 → 分布に依らず near-optimal(実トレース 5 本で
 *   全トレース最適の 0.01–0.05pt 以内)。k は限界利得停止で自動決定。
 *
 * 使い方(LRU.c / region-split キャッシュへの組込み):
 *   // 閾値は「ホスト側の構造体」が所有する。そのポインタを init で渡す:
 *   uint64_t threshold[RSEG_KMAX - 1];   // ホスト構造体のメンバ(容量 >= RSEG_KMAX-1)
 *   int      threshold_num;              // 同上(=k-1, 0 なら無分割)
 *   rseg_t R;
 *   rseg_init(&R, cache_capacity_bytes, physical_region_bytes, 0.02,
 *             RSEG_OBJ_PARETO, 0.5,         // 目的と重み(両立なら PARETO+w。w:1=BMR寄り,0=MR寄り)
 *             threshold, &threshold_num);   // ★ 外部バッファを登録(以後ここへ書かれる)
 *   //   physical_region_bytes = 実機の消去ブロック(例 256MiB)。影シミュはこれに
 *   //   sample_rate を掛けた region サイズで「region 個数(=cache/region)を保存」する。
 *   // 各リクエストで(ヒット/ミス問わず):
 *   rseg_on_request(&R, obj_id, obj_size);     // 標本採取 + 周期再計算(→ threshold[]/num を更新)
 *   // 挿入時の配置(ホストが threshold[]/threshold_num を直接見てもよい):
 *   int pool = rseg_pool_of(&R, obj_size);     // 0..k-1。-1 は region 超(非キャッシュ)
 *   // 終了時: rseg_free(&R);
 *
 * 計算量: 採取 O(1)。再計算は RSEG_RECALC_OBS 観測ごと、標本(<=RSEG_BUF_CAP)を
 *   候補分割数(<=~1500)だけ再生 → 周期的バースト。メモリ ~数 MB(下記 #define で調整)。
 *
 * 注意: これは「閾値決定器」。region 単位削除本体(プール毎の region-LRU + 物理消去ブロック)
 *   は別途。本器が出す thresholds[] でプール分けすれば WAF=1 を保てる。
 */
#ifndef REGION_SEGMENTER_H
#define REGION_SEGMENTER_H

#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>

/* ---- 調整パラメータ ---------------------------------------------------- */
#define RSEG_BUF_BITS     17                 /* 標本リングバッファ = 2^17 = 131072 リクエスト */
#define RSEG_BUF_CAP      (1u << RSEG_BUF_BITS)
#define RSEG_HT_CAP       (1u << (RSEG_BUF_BITS + 1)) /* dense 化ハッシュ表(2倍, 開番地法) */
#define RSEG_KMAX         4                  /* 最大プール数(=最大 KMAX-1 閾値) */
#define RSEG_NBUCKET      48                 /* log2 サイズバケット 2^0..2^47 */
#define RSEG_NREG_MIN     8                  /* 実効 region 数の下限(これ未満は粒度が粗すぎ→rate↑) */
#define RSEG_NREG_MAX     (1 << 20)          /* region 配列上限(=cache/region の上限) */
#define RSEG_REG_SLACK    512
#define RSEG_REGION_OBJ_FLOOR 16             /* 影 region が保証する最小オブジェクト数(退化防止) */
#define RSEG_RECALC_OBS   200000ULL          /* この標本観測数ごとに再計算 */
#define RSEG_MIN_OBS      20000u             /* 標本がこれ未満なら再計算を見送る */
#define RSEG_EPS_PT       0.003              /* 限界利得停止: 目的の絶対改善閾(pt) */
#define RSEG_EPS_REL      0.01               /* 同: 相対改善閾(base 比) */
#define RSEG_OBJ_MR       0                  /* 目的: ミス率のみ最小化 */
#define RSEG_OBJ_BMR      1                  /* 目的: バイトミス率のみ(=WAF=1 の SSD書込/req) */
#define RSEG_OBJ_PARETO   2                  /* 目的: 正規化ブレンド w*BMR+(1-w)*MR で両立(Pareto) */
#define RSEG_OBJ_MR_CAPPED_BMR 3             /* 目的: BMR<=無分割BMR*(1+tol) の制約下で MR 最小
                                              *       =SSD書込を増やさず命中率を上げる(WAF=1整合) */

typedef struct {
  /* 設定 */
  double    sample_rate;
  uint64_t  cache_capacity;        /* ホストキャッシュ容量[byte] */
  uint64_t  physical_region_bytes; /* 実機の消去ブロック(region)サイズ[byte] 例:256MiB */
  int       nreg;                  /* 実機 region 数 = cache_capacity/physical_region_bytes */
  uint8_t   reject_oversize;       /* 1: region 超のオブジェクトは非キャッシュ(既定1) */
  int       objective;             /* RSEG_OBJ_MR / _BMR / _PARETO / _MR_CAPPED_BMR */
  double    bmr_weight;            /* PARETO: BMR重み w∈[0,1](1=BMR寄り,0=MR寄り,0.5=均衡)。
                                    * MR_CAPPED_BMR: BMR 許容増加率 tol(0=厳密に書込を増やさない)。 */
  uint64_t  shards_thresh;         /* hash(id) < これ なら採取 */

  /* 標本リングバッファ(生 id+size) */
  uint64_t *buf_id;
  uint32_t *buf_sz;
  uint32_t  buf_head;           /* 次に書く位置 */
  uint32_t  buf_count;          /* 充填数(<=RSEG_BUF_CAP) */
  uint64_t  n_sampled;          /* 累積採取数(再計算トリガ) */

  /* 現在の決定は「外部(ホスト)構造体」に書き込む。init で登録したポインタを保持。
   *   out_thr  : 昇順の閾値バッファ(ホスト所有, 容量 >= RSEG_KMAX-1)
   *   out_nthr : 閾値数(=k-1, 0 なら無分割)。再計算ごとに上書き。 */
  uint64_t *out_thr;
  int      *out_nthr;
  uint64_t  n_recompute;
  /* 内省(ログ用): 直近再計算での実効 region 数と平均 obj サイズ。
   * last_eff_nreg < RSEG_NREG_MIN なら影が粗すぎ(=object≳region) → 判定の信頼が低い。 */
  int       last_eff_nreg;
  double    last_mean_obj;

  /* スクラッチ(init で確保) */
  uint64_t *ht_key; int32_t *ht_val;    /* id -> dense */
  uint32_t *dsize;                      /* dense -> 代表サイズ(最大) */
  int32_t  *reqd;                       /* バッファ順の dense 列 */
  int32_t  *slot_obj, *slot_next;       /* スロット連結(サイズ S) */
  uint8_t  *in_cache; int32_t *obj_region, *loc_slot;  /* dense 毎 */
  int32_t  *reg_head, *reg_tail, *reg_prev, *reg_next, *reg_nextfree;
  uint8_t  *reg_open;
  double   *reg_bytes;
  int32_t   open_reg[RSEG_KMAX];
} rseg_t;

/* ---- 内部ユーティリティ ------------------------------------------------ */
static inline uint64_t rseg__mix(uint64_t x) {            /* splitmix64 */
  x += 0x9E3779B97F4A7C15ULL;
  x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
  x = (x ^ (x >> 27)) * 0x94D049BB133111EBULL;
  return x ^ (x >> 31);
}
static inline int rseg__bucket(uint64_t sz) {             /* floor(log2(sz)) */
  int b = 0; while (sz > 1 && b < RSEG_NBUCKET - 1) { sz >>= 1; b++; } return b;
}

/* threshold      : ホスト所有の閾値バッファ(uint64_t*, 容量 >= RSEG_KMAX-1)。決定はここへ書く。
 * threshold_num  : ホスト所有の閾値数(int*)。再計算ごとに *threshold_num = k-1 が書かれる。
 * いずれも NULL 不可(外部保存が本 init の目的)。 */
/* objective    : RSEG_OBJ_MR / _BMR / _PARETO / _MR_CAPPED_BMR
 * bmr_weight   : PARETO        → BMR重み w∈[0,1](0=MR最優先,1=BMR最優先,0.5=均衡)
 *                MR_CAPPED_BMR → BMR許容増加率 tol(0=書込を一切増やさず MR最小化, 0.02=2%まで許容)
 *                MR / BMR      → 未使用 */
static inline void rseg_init(rseg_t *R, uint64_t cache_capacity,
                             uint64_t physical_region_bytes,
                             double sample_rate, int objective, double bmr_weight,
                             uint64_t *threshold, int *threshold_num) {
  uint64_t nreg;
  memset(R, 0, sizeof(*R));
  R->sample_rate = sample_rate;
  R->cache_capacity = cache_capacity;
  R->physical_region_bytes = physical_region_bytes ? physical_region_bytes : 1;
  R->reject_oversize = 1;
  R->objective = objective;
  R->bmr_weight = (bmr_weight < 0.0) ? 0.0 : (bmr_weight > 1.0 ? 1.0 : bmr_weight);
  R->shards_thresh = (sample_rate >= 1.0) ? UINT64_MAX
                     : (uint64_t)(sample_rate * 1.8446744073709552e19); /* rate*2^64 */
  /* 実機 region 数。影シミュは region サイズ=physical*rate で「region 個数を保存」する。 */
  nreg = cache_capacity / R->physical_region_bytes;
  if (nreg < RSEG_NREG_MIN) nreg = RSEG_NREG_MIN;
  if (nreg > RSEG_NREG_MAX) nreg = RSEG_NREG_MAX;
  R->nreg = (int)nreg;
  /* 外部の閾値保存先を登録。初期状態=無分割。 */
  R->out_thr = threshold;
  R->out_nthr = threshold_num;
  if (R->out_nthr) *R->out_nthr = 0;
  R->buf_id = (uint64_t*)malloc(sizeof(uint64_t) * RSEG_BUF_CAP);
  R->buf_sz = (uint32_t*)malloc(sizeof(uint32_t) * RSEG_BUF_CAP);
  R->ht_key = (uint64_t*)malloc(sizeof(uint64_t) * RSEG_HT_CAP);
  R->ht_val = (int32_t*) malloc(sizeof(int32_t)  * RSEG_HT_CAP);
  R->dsize  = (uint32_t*)malloc(sizeof(uint32_t) * RSEG_BUF_CAP);
  R->reqd   = (int32_t*) malloc(sizeof(int32_t)  * RSEG_BUF_CAP);
  {
    uint32_t S = RSEG_BUF_CAP + 64;
    int32_t  Rn = R->nreg + RSEG_REG_SLACK;
    R->slot_obj  = (int32_t*)malloc(sizeof(int32_t) * S);
    R->slot_next = (int32_t*)malloc(sizeof(int32_t) * S);
    R->in_cache  = (uint8_t*)malloc(sizeof(uint8_t) * RSEG_BUF_CAP);
    R->obj_region= (int32_t*)malloc(sizeof(int32_t) * RSEG_BUF_CAP);
    R->loc_slot  = (int32_t*)malloc(sizeof(int32_t) * RSEG_BUF_CAP);
    R->reg_head  = (int32_t*)malloc(sizeof(int32_t) * Rn);
    R->reg_tail  = (int32_t*)malloc(sizeof(int32_t) * Rn);
    R->reg_prev  = (int32_t*)malloc(sizeof(int32_t) * Rn);
    R->reg_next  = (int32_t*)malloc(sizeof(int32_t) * Rn);
    R->reg_nextfree = (int32_t*)malloc(sizeof(int32_t) * Rn);
    R->reg_open  = (uint8_t*)malloc(sizeof(uint8_t) * Rn);
    R->reg_bytes = (double*) malloc(sizeof(double)  * Rn);
  }
}

static inline void rseg_free(rseg_t *R) {
  free(R->buf_id); free(R->buf_sz); free(R->ht_key); free(R->ht_val);
  free(R->dsize); free(R->reqd); free(R->slot_obj); free(R->slot_next);
  free(R->in_cache); free(R->obj_region); free(R->loc_slot);
  free(R->reg_head); free(R->reg_tail); free(R->reg_prev); free(R->reg_next);
  free(R->reg_nextfree); free(R->reg_open); free(R->reg_bytes);
  memset(R, 0, sizeof(*R));
}

/* dense 化: バッファを id->0..D-1 に詰め、reqd[] と dsize[](最大サイズ) を作る。返り値 D。 */
static inline int rseg__densify(rseg_t *R) {
  uint32_t i, mask = RSEG_HT_CAP - 1;
  int32_t D = 0;
  for (i = 0; i < RSEG_HT_CAP; i++) R->ht_val[i] = -1;
  for (i = 0; i < R->buf_count; i++) {
    /* バッファは buf_head から時系列。head が次書込位置なので最古は buf_head(満杯時) */
    uint32_t pos = (R->buf_count < RSEG_BUF_CAP) ? i : (R->buf_head + i) & (RSEG_BUF_CAP - 1);
    uint64_t id = R->buf_id[pos]; uint32_t sz = R->buf_sz[pos];
    uint32_t h = (uint32_t)(rseg__mix(id) & mask);
    while (R->ht_val[h] != -1 && R->ht_key[h] != id) h = (h + 1) & mask;
    if (R->ht_val[h] == -1) { R->ht_key[h] = id; R->ht_val[h] = D; R->dsize[D] = sz; D++; }
    else if (sz > R->dsize[R->ht_val[h]]) R->dsize[R->ht_val[h]] = sz;
    R->reqd[i] = R->ht_val[h];
  }
  return (int)D;
}

/* 標本キャッシュ上で大域 region-LRU(k 分割)を再生し miss/miss_bytes/n/rbytes を返す。 */
static inline void rseg__sim(rseg_t *R, const int64_t *thr, int K, int D,
                             double scap, double region_bytes,
                             double *o_miss, double *o_mbytes,
                             double *o_n, double *o_rbytes) {
  uint32_t S = RSEG_BUF_CAP + 64;
  int32_t  Rn = R->nreg + RSEG_REG_SLACK;
  uint32_t s; int32_t r;
  int32_t free_slot = 0, reg_free = 0, lru_head = -1, lru_tail = -1;
  double total = 0.0, miss = 0.0, mb = 0.0, nc = 0.0, rb = 0.0;
  uint32_t t;
  for (s = 0; s < S - 1; s++) R->slot_next[s] = (int32_t)(s + 1);
  R->slot_next[S - 1] = -1;
  for (r = 0; r < Rn - 1; r++) R->reg_nextfree[r] = r + 1;
  R->reg_nextfree[Rn - 1] = -1;
  for (r = 0; r < (int32_t)D; r++) { R->in_cache[r] = 0; R->obj_region[r] = -1; R->loc_slot[r] = -1; }
  for (r = 0; r < K; r++) R->open_reg[r] = -1;

  for (t = 0; t < R->buf_count; t++) {
    int32_t o = R->reqd[t];
    double sz = (double)R->dsize[o];
    int c = 0, j;
    for (j = 0; j < K - 1; j++) { if (sz > (double)thr[j]) c++; else break; }
    nc += 1.0; rb += sz;
    if (R->in_cache[o] == 1) {                 /* ヒット: 封緘済み region を MRU へ */
      int32_t rr = R->obj_region[o];
      if (R->reg_open[rr] == 0) {
        int32_t p = R->reg_prev[rr], n = R->reg_next[rr];
        if (p != -1) R->reg_next[p] = n; else lru_head = n;
        if (n != -1) R->reg_prev[n] = p; else lru_tail = p;
        R->reg_prev[rr] = -1; R->reg_next[rr] = lru_head;
        if (lru_head != -1) R->reg_prev[lru_head] = rr;
        lru_head = rr; if (lru_tail == -1) lru_tail = rr;
      }
      continue;
    }
    miss += 1.0; mb += sz;
    {
      int32_t rr = R->open_reg[c];
      if (rr == -1) {
        rr = reg_free; reg_free = R->reg_nextfree[rr];
        R->reg_open[rr] = 1; R->reg_head[rr] = -1; R->reg_tail[rr] = -1; R->reg_bytes[rr] = 0.0;
        R->open_reg[c] = rr;
      }
      s = (uint32_t)free_slot; free_slot = R->slot_next[s];
      R->slot_obj[s] = o; R->slot_next[s] = -1;
      if (R->reg_tail[rr] == -1) R->reg_head[rr] = (int32_t)s;
      else R->slot_next[R->reg_tail[rr]] = (int32_t)s;
      R->reg_tail[rr] = (int32_t)s;
      R->loc_slot[o] = (int32_t)s; R->obj_region[o] = rr; R->in_cache[o] = 1;
      R->reg_bytes[rr] += sz; total += sz;
      if (R->reg_bytes[rr] >= region_bytes) {  /* 封緘 -> MRU */
        R->reg_open[rr] = 0; R->reg_prev[rr] = -1; R->reg_next[rr] = lru_head;
        if (lru_head != -1) R->reg_prev[lru_head] = rr;
        lru_head = rr; if (lru_tail == -1) lru_tail = rr;
        R->open_reg[c] = -1;
      }
    }
    while (total > scap) {                      /* 大域 LRU 削除: tail region 丸ごと */
      int32_t v = lru_tail, p, s2;
      if (v == -1) break;
      p = R->reg_prev[v]; lru_tail = p;
      if (p != -1) R->reg_next[p] = -1; else lru_head = -1;
      s2 = R->reg_head[v];
      while (s2 != -1) {
        int32_t o2 = R->slot_obj[s2], nx = R->slot_next[s2];
        if (R->obj_region[o2] == v && R->loc_slot[o2] == s2) {
          R->in_cache[o2] = 0; R->obj_region[o2] = -1; R->loc_slot[o2] = -1;
        }
        R->slot_next[s2] = free_slot; free_slot = s2; s2 = nx;
      }
      total -= R->reg_bytes[v];
      R->reg_nextfree[v] = reg_free; reg_free = v;
    }
  }
  *o_miss = miss; *o_mbytes = mb; *o_n = nc; *o_rbytes = rb;
}

/* 1 分割を影シミュし MR と BMR を両方返す。 */
static inline void rseg__sim_mrbmr(rseg_t *R, const int64_t *thr, int K, int D,
                                   double scap, double rbytes, double *mr, double *bmr) {
  double mi, mb, n, rb;
  rseg__sim(R, thr, K, D, scap, rbytes, &mi, &mb, &n, &rb);
  *mr  = (n  > 0) ? mi / n  : 1.0;
  *bmr = (rb > 0) ? mb / rb : 1.0;
}

/* スカラー化した目的値(小さいほど良)。PARETO は k=1 基準(mr1,bmr1)で正規化してブレンド。
 *   MR    : mr
 *   BMR   : bmr
 *   PARETO: w*(bmr/bmr1) + (1-w)*(mr/mr1)   (各項は無分割で 1 → 重みが「相対改善」を均す) */
static inline double rseg__blend(const rseg_t *R, double mr, double bmr,
                                 double mr1, double bmr1) {
  double a, b;
  if (R->objective == RSEG_OBJ_MR)  return mr;
  if (R->objective == RSEG_OBJ_BMR) return bmr;
  if (R->objective == RSEG_OBJ_MR_CAPPED_BMR) {
    /* 制約: BMR <= 無分割BMR*(1+tol)。違反は実行不可(DBL_MAX)。制約内は MR を最小化。 */
    double cap = bmr1 * (1.0 + R->bmr_weight);     /* bmr_weight を許容増加率 tol に流用 */
    return (bmr > cap) ? DBL_MAX : mr;
  }
  /* PARETO: k=1 基準で正規化したブレンド */
  a = (mr1  > 0.0) ? mr  / mr1  : mr;
  b = (bmr1 > 0.0) ? bmr / bmr1 : bmr;
  return R->bmr_weight * b + (1.0 - R->bmr_weight) * a;
}

/* 標本上で k=1..KMAX の全分割を網羅し、限界利得停止で (k, thresholds) を決定。 */
static inline void rseg_recompute(rseg_t *R) {
  int D, b, i, j, k, ncand = 0, occ_min = -1, occ_max = -1;
  int64_t cand[RSEG_NBUCKET];
  uint8_t occ[RSEG_NBUCKET];
  double scap, region_bytes, mean_obj = 0.0, region_floor;
  double mr1 = 1.0, bmr1 = 1.0, mr, bmr;
  double bestval[RSEG_KMAX + 1];
  int64_t bestthr[RSEG_KMAX + 1][RSEG_KMAX - 1];
  int kstar;

  if (R->buf_count < RSEG_MIN_OBS) return;
  D = rseg__densify(R);
  if (D < 2) return;

  /* 占有 log2 バケット */
  memset(occ, 0, sizeof(occ));
  for (i = 0; i < D; i++) { int bb = rseg__bucket(R->dsize[i]); occ[bb] = 1; }
  for (b = 0; b < RSEG_NBUCKET; b++) if (occ[b]) { if (occ_min < 0) occ_min = b; occ_max = b; }
  /* 候補境界 = occ_min < b <= occ_max なる 2^b */
  for (b = occ_min + 1; b <= occ_max; b++)
    if (b > 0 && b < RSEG_NBUCKET) cand[ncand++] = ((int64_t)1) << b;

  /* 平均オブジェクトサイズ。影 region がこれより小さいと「1 region<数 obj」で
   * region 構造が消失し object-LRU に退化 → そうならない下限(フロア)を設ける。 */
  for (i = 0; i < D; i++) mean_obj += (double)R->dsize[i];
  mean_obj = (D > 0) ? mean_obj / (double)D : 1.0;

  scap = (double)R->cache_capacity * R->sample_rate;         /* SHARDS 容量スケール */
  region_bytes = (double)R->physical_region_bytes * R->sample_rate; /* 既定: region個数を保存 */
  region_floor = (double)RSEG_REGION_OBJ_FLOOR * mean_obj;    /* >=この数の obj/region を保証 */
  if (region_bytes < region_floor) region_bytes = region_floor;
  if (region_bytes < 1.0) region_bytes = 1.0;
  /* 実効 region 数(=scap/region)。小さすぎる(<RSEG_NREG_MIN)と標本が粗く信頼低 → 要 rate↑ */
  R->last_mean_obj = mean_obj;
  R->last_eff_nreg = (region_bytes > 0.0) ? (int)(scap / region_bytes) : 0;

  for (k = 0; k <= RSEG_KMAX; k++) bestval[k] = DBL_MAX;

  /* k=1: 無分割。PARETO 正規化の基準 (mr1,bmr1) もここで確定。 */
  rseg__sim_mrbmr(R, cand, 1, D, scap, region_bytes, &mr1, &bmr1);
  bestval[1] = rseg__blend(R, mr1, bmr1, mr1, bmr1);
  /* k=2: 単一閾値 */
  for (i = 0; i < ncand; i++) {
    int64_t t1[1]; double v; t1[0] = cand[i];
    rseg__sim_mrbmr(R, t1, 2, D, scap, region_bytes, &mr, &bmr);
    v = rseg__blend(R, mr, bmr, mr1, bmr1);
    if (v < bestval[2]) { bestval[2] = v; bestthr[2][0] = t1[0]; }
  }
  /* k=3: 2 閾値 */
  for (i = 0; i < ncand; i++) for (j = i + 1; j < ncand; j++) {
    int64_t t2[2]; double v; t2[0] = cand[i]; t2[1] = cand[j];
    rseg__sim_mrbmr(R, t2, 3, D, scap, region_bytes, &mr, &bmr);
    v = rseg__blend(R, mr, bmr, mr1, bmr1);
    if (v < bestval[3]) { bestval[3] = v; bestthr[3][0] = t2[0]; bestthr[3][1] = t2[1]; }
  }
  /* k=4: 3 閾値 */
  for (i = 0; i < ncand; i++) for (j = i + 1; j < ncand; j++) { int m;
    for (m = j + 1; m < ncand; m++) {
      int64_t t3[3]; double v; t3[0] = cand[i]; t3[1] = cand[j]; t3[2] = cand[m];
      rseg__sim_mrbmr(R, t3, 4, D, scap, region_bytes, &mr, &bmr);
      v = rseg__blend(R, mr, bmr, mr1, bmr1);
      if (v < bestval[4]) { bestval[4] = v; bestthr[4][0]=t3[0]; bestthr[4][1]=t3[1]; bestthr[4][2]=t3[2]; }
    }
  }

  /* 限界利得停止 */
  kstar = 1;
  for (k = 2; k <= RSEG_KMAX; k++) {
    double gain;
    if (bestval[k] == DBL_MAX) break;
    gain = bestval[k - 1] - bestval[k];
    if (gain >= RSEG_EPS_PT && gain >= RSEG_EPS_REL * bestval[1]) kstar = k; else break;
  }
  /* 決定を外部(ホスト)構造体へ書き込む */
  if (R->out_nthr && R->out_thr) {
    *R->out_nthr = kstar - 1;
    for (k = 0; k < kstar - 1; k++) R->out_thr[k] = (uint64_t)bestthr[kstar][k];
  }
  R->n_recompute++;
}

/* ---- ホスト側 API ------------------------------------------------------ */
/* 各リクエストで呼ぶ: SHARDS 採取 + 周期再計算。 */
static inline void rseg_on_request(rseg_t *R, uint64_t obj_id, uint32_t obj_size) {
  /* region 超のオブジェクトは非キャッシュ → 標本にも含めない(影シミュと実機を一致させる) */
  if (R->reject_oversize && (uint64_t)obj_size > R->physical_region_bytes) return;
  if (rseg__mix(obj_id) >= R->shards_thresh) return;       /* 非採取 */
  R->buf_id[R->buf_head] = obj_id;
  R->buf_sz[R->buf_head] = obj_size ? obj_size : 1u;
  R->buf_head = (R->buf_head + 1) & (RSEG_BUF_CAP - 1);
  if (R->buf_count < RSEG_BUF_CAP) R->buf_count++;
  R->n_sampled++;
  if (R->n_sampled % RSEG_RECALC_OBS == 0) rseg_recompute(R);
}

/* 配置: size>thr[j] の個数 = プール番号(0..k-1)。無分割なら 0。閾値は外部構造体から読む。
 * region 超の巨大オブジェクトは -1(=非キャッシュ/バイパス)。呼び出し側で別処理すること。
 * ※ ホストは out_thr/out_nthr を直接見て自前で配置してもよい(本関数は等価な補助)。 */
static inline int rseg_pool_of(const rseg_t *R, uint32_t obj_size) {
  int c = 0, j, nthr = R->out_nthr ? *R->out_nthr : 0;
  if (R->reject_oversize && (uint64_t)obj_size > R->physical_region_bytes) return -1;
  for (j = 0; j < nthr; j++) { if ((uint64_t)obj_size > R->out_thr[j]) c++; else break; }
  return c;
}

/* 現在の閾値数(=k-1)を返す(ログ/デバッグ用)。閾値値そのものはホストの threshold[] を直接参照。 */
static inline int rseg_get_nthr(const rseg_t *R) {
  return R->out_nthr ? *R->out_nthr : 0;
}

#endif /* REGION_SEGMENTER_H */
