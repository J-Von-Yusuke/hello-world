#include "split_threshold.h"
// LRU_params_t に: split_thresh_t sst;

// LRU_init:
  sst_init(&params->sst, SST_LAMBDA_DEFAULT);   // λ=256(min). 0=mid, 128=blend に変更可

// LRU_find (ヒット時):
  cache_obj->misc.freq++;                        // freq欄が無ければ cache_obj_t に追加

// LRU_insert (admission=即集積＋tick):
  cache_obj_t *obj = cache_insert_base(cache, req);
  obj->misc.freq = 1;
  sst_on_admit(&params->sst, req->obj_size);     // ← count/byte を即積む
  sst_tick(&params->sst);                        // ← 周期再計算＋EMA減衰
  prepend_obj_to_head(&params->q_head, &params->q_tail, obj);

// LRU_evict (cache_evict_base 直前):
  sst_on_evict(&params->sst, obj_to_evict->obj_size, obj_to_evict->misc.freq);  // ← cold

// getter:
int64_t LRU_get_split_threshold(const cache_t *c){
  return ((LRU_params_t*)c->eviction_params)->sst.threshold;  // <0=無分割
}
// 比較ログ用:
void LRU_log_candidates(const cache_t *c){
  sst_candidates_t k; sst_get_candidates(&((LRU_params_t*)c->eviction_params)->sst, &k);
  printf("count_p50=%ld byte_p50=%ld cold=%ld mid=%ld min=%ld\n",
         k.count_p50,k.byte_p50,k.cold_byte_p50,k.mid,k.min_rule);
}
