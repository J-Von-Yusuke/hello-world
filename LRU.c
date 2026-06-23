//
//  a LRU module that supports different obj size
//
//
//  LRU.c
//  libCacheSim
//
//  Created by Juncheng on 12/4/18.
//  Copyright © 2018 Juncheng. All rights reserved.
//

#include "dataStructure/hashtable/hashtable.h"
#include "libCacheSim/evictionAlgo.h"

#ifdef __cplusplus
extern "C" {
#endif

// #define USE_BELADY

// ***********************************************************************
// ****                                                               ****
// ****                   function declarations                       ****
// ****                                                               ****
// ***********************************************************************

static void LRU_free(cache_t *cache);
static bool LRU_get(cache_t *cache, const request_t *req);
static cache_obj_t *LRU_find(cache_t *cache, const request_t *req,
                             bool update_cache);
static cache_obj_t *LRU_insert(cache_t *cache, const request_t *req);
static cache_obj_t *LRU_to_evict(cache_t *cache, const request_t *req);
static void LRU_evict(cache_t *cache, const request_t *req);
static bool LRU_remove(cache_t *cache, obj_id_t obj_id);
static void LRU_print_cache(const cache_t *cache);

// ***********************************************************************
// ****                                                               ****
// ****                   end user facing functions                   ****
// ****                                                               ****
// ****                       init, free, get                         ****
// ***********************************************************************
/**
 * @brief initialize a LRU cache
 *
 * @param ccache_params some common cache parameters
 * @param cache_specific_params LRU specific parameters, should be NULL
 */
cache_t *LRU_init(const common_cache_params_t ccache_params,
                  const char *cache_specific_params) {
  cache_t *cache =
      cache_struct_init("LRU", ccache_params, cache_specific_params);
  cache->cache_init = LRU_init;
  cache->cache_free = LRU_free;
  cache->get = LRU_get;
  cache->find = LRU_find;
  cache->insert = LRU_insert;
  cache->evict = LRU_evict;
  cache->remove = LRU_remove;
  cache->to_evict = LRU_to_evict;
  cache->get_occupied_byte = cache_get_occupied_byte_default;
  cache->can_insert = cache_can_insert_default;
  cache->get_n_obj = cache_get_n_obj_default;
  cache->print_cache = LRU_print_cache;

  if (ccache_params.consider_obj_metadata) {
    cache->obj_md_size = 8 * 2;
  } else {
    cache->obj_md_size = 0;
  }

#ifdef USE_BELADY
  snprintf(cache->cache_name, CACHE_NAME_ARRAY_LEN, "LRU_Belady");
#endif

  LRU_params_t *params = malloc(sizeof(LRU_params_t));
  params->q_head = NULL;
  params->q_tail = NULL;
  cache->eviction_params = params;

  return cache;
}

/**
 * free resources used by this cache
 *
 * @param cache
 */
static void LRU_free(cache_t *cache) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;
  free(params);
  cache_struct_free(cache);
}

/**
 * @brief this function is the user facing API
 * it performs the following logic
 *
 * ```
 * if obj in cache:
 *    update_metadata
 *    return true
 * else:
 *    if cache does not have enough space:
 *        evict until it has space to insert
 *    insert the object
 *    return false
 * ```
 *
 * @param cache
 * @param req
 * @return true if cache hit, false if cache miss
 */
static bool LRU_get(cache_t *cache, const request_t *req) {
  return cache_get_base(cache, req);
}

// ***********************************************************************
// ****                                                               ****
// ****       developer facing APIs (used by cache developer)         ****
// ****                                                               ****
// ***********************************************************************

/**
 * @brief check whether an object is in the cache
 *
 * @param cache
 * @param req
 * @param update_cache whether to update the cache,
 *  if true, the object is promoted
 *  and if the object is expired, it is removed from the cache
 * @return true on hit, false on miss
 */
static cache_obj_t *LRU_find(cache_t *cache, const request_t *req,
                             bool update_cache) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;
  cache_obj_t *cache_obj = cache_find_base(cache, req, update_cache);

  if (cache_obj && likely(update_cache)) {
    /* lru_head is the newest, move cur obj to lru_head */
#ifdef USE_BELADY
    if (req->next_access_vtime != INT64_MAX)
#endif
      move_obj_to_head(&params->q_head, &params->q_tail, cache_obj);
  }
  return cache_obj;
}

/**
 * @brief insert an object into the cache,
 * update the hash table and cache metadata
 * this function assumes the cache has enough space
 * and eviction is not part of this function
 *
 * @param cache
 * @param req
 * @return the inserted object
 */
static cache_obj_t *LRU_insert(cache_t *cache, const request_t *req) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;

  cache_obj_t *obj = cache_insert_base(cache, req);
  prepend_obj_to_head(&params->q_head, &params->q_tail, obj);

  return obj;
}

/**
 * @brief find the object to be evicted
 * this function does not actually evict the object or update metadata
 * not all eviction algorithms support this function
 * because the eviction logic cannot be decoupled from finding eviction
 * candidate, so use assert(false) if you cannot support this function
 *
 * @param cache the cache
 * @return the object to be evicted
 */
static cache_obj_t *LRU_to_evict(cache_t *cache, const request_t *req) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;

  DEBUG_ASSERT(params->q_tail != NULL || cache->occupied_byte == 0);

  cache->to_evict_candidate_gen_vtime = cache->n_req;
  return params->q_tail;
}

/**
 * @brief evict an object from the cache
 * it needs to call cache_evict_base before returning
 * which updates some metadata such as n_obj, occupied size, and hash table
 *
 * @param cache
 * @param req not used
 */
static void LRU_evict(cache_t *cache, const request_t *req) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;
  cache_obj_t *obj_to_evict = params->q_tail;
  DEBUG_ASSERT(params->q_tail != NULL);

  // we can simply call remove_obj_from_list here, but for the best performance,
  // we chose to do it manually
  // remove_obj_from_list(&params->q_head, &params->q_tail, obj)

  params->q_tail = params->q_tail->queue.prev;
  if (likely(params->q_tail != NULL)) {
    params->q_tail->queue.next = NULL;
  } else {
    /* cache->n_obj has not been updated */
    DEBUG_ASSERT(cache->n_obj == 1);
    params->q_head = NULL;
  }

#if defined(TRACK_DEMOTION)
  if (cache->track_demotion)
    printf("%ld demote %ld %ld\n", cache->n_req, obj_to_evict->create_time,
           obj_to_evict->next_access_vtime);
#endif

  cache_evict_base(cache, obj_to_evict, true);
}

/**
 * @brief remove the given object from the cache
 * note that eviction should not call this function, but rather call
 * `cache_evict_base` because we track extra metadata during eviction
 *
 * and this function is different from eviction
 * because it is used to for user trigger
 * remove, and eviction is used by the cache to make space for new objects
 *
 * it needs to call cache_remove_obj_base before returning
 * which updates some metadata such as n_obj, occupied size, and hash table
 *
 * @param cache
 * @param obj
 */
static void LRU_remove_obj(cache_t *cache, cache_obj_t *obj) {
  assert(obj != NULL);

  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;

  remove_obj_from_list(&params->q_head, &params->q_tail, obj);
  cache_remove_obj_base(cache, obj, true);
}

/**
 * @brief remove an object from the cache
 * this is different from cache_evict because it is used to for user trigger
 * remove, and eviction is used by the cache to make space for new objects
 *
 * it needs to call cache_remove_obj_base before returning
 * which updates some metadata such as n_obj, occupied size, and hash table
 *
 * @param cache
 * @param obj_id
 * @return true if the object is removed, false if the object is not in the
 * cache
 */
static bool LRU_remove(cache_t *cache, obj_id_t obj_id) {
  cache_obj_t *obj = hashtable_find_obj_id(cache->hashtable, obj_id);
  if (obj == NULL) {
    return false;
  }
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;

  remove_obj_from_list(&params->q_head, &params->q_tail, obj);
  cache_remove_obj_base(cache, obj, true);

  return true;
}

static void LRU_print_cache(const cache_t *cache) {
  LRU_params_t *params = (LRU_params_t *)cache->eviction_params;
  cache_obj_t *cur = params->q_head;
  // print from the most recent to the least recent
  if (cur == NULL) {
    printf("empty\n");
    return;
  }
  while (cur != NULL) {
    printf("%lu->", (unsigned long)cur->obj_id);
    cur = cur->queue.next;
  }
  printf("END\n");
}

ヘッダ [split_threshold.h](region_split_experiment/split_threshold.h) を作りました。次に **LRU.c への組込み**（フック箇所）です。

## LRU.c への組込み（差分）

```c
// ① 先頭の include に追加
#include "split_threshold.h"

// ② LRU_params_t に推定器を1フィールド追加 (evictionAlgo.h の struct 定義へ)
//    typedef struct LRU_params { cache_obj_t *q_head, *q_tail;
//                                split_thresh_t sst;  /* ← 追加 */ } LRU_params_t;

// ③ LRU_init: 末尾の return 前で初期化
  sst_init(&params->sst);

// ④ LRU_find: ヒット時に freq を加算 (move_obj_to_head の直前に1行)
  if (cache_obj && likely(update_cache)) {
    cache_obj->misc.freq++;                 /* per-obj アクセス回数。freq欄が無ければ
                                               cache_obj_t に uint32_t freq; を追加 */
    move_obj_to_head(&params->q_head, &params->q_tail, cache_obj);
  }

// ⑤ LRU_insert: 挿入時に freq=1 (admission の1回)
  cache_obj_t *obj = cache_insert_base(cache, req);
  obj->misc.freq = 1;                        /* ← 追加 */
  prepend_obj_to_head(&params->q_head, &params->q_tail, obj);

// ⑥ LRU_evict: cache_evict_base の直前で統計集積 → tick
  cache_obj_t *obj_to_evict = params->q_tail;
  ...
  sst_on_evict(&params->sst, obj_to_evict->obj_size, obj_to_evict->misc.freq); /* ← 追加 */
  sst_tick(&params->sst);                                                       /* ← 追加 */
  cache_evict_base(cache, obj_to_evict, true);

// ⑦ 閾値を外(region/Navy層)から読む getter を追加
int64_t LRU_get_split_threshold(const cache_t *cache) {
  const LRU_params_t *p = (const LRU_params_t *)cache->eviction_params;
  return p->sst.threshold;                   /* <0 は「無分割」 */
}
```

## なぜこれで `min(byte_p50, cold_byte_p50)` になるか
- **集積点を eviction にする**のが要点：その瞬間にオブジェクトの生涯 `freq` が確定するので **OHW(freq≤1) を正しく判定**でき、`all_bytes`（全体）と `cold_bytes`（OHWのみ）を同一母集団で積めます。
- `sst_recompute` が両ヒストグラムの 50%点 = `byte_p50` と `cold_byte_p50` を求め、**min** を取り、**大poolバイト割合<5%なら無分割**ゲート。これは検証済みの規則そのものです。
- **減衰(半減)**で分布ドリフトに追従。更新 O(1)/退去、再計算 O(48)、メモリ ~640B。

## 使い方（libCacheSim上）
- **LRU.c 自体は分割しません**（object-LRU）。この実装は LRU を走らせながら **閾値を統計から算出して `LRU_get_split_threshold()` で公開**します。検証用途では、これをログ出力して「統計だけで出た閾値」が妥当か確認できます。
- **実際の分割**は region/Navy 層が `sst_pool_of(&sst, obj_size)` で配置先pool(0/1)を決め、**pool1(大)を FDP の別GC単位(=Region)へ**割り当てます。LRU.c はその閾値供給源。

## 注意点（正確に）
- **`freq` 欄**：libCacheSim の `cache_obj_t` に freq が無いビルドなら `uint32_t freq;` を1つ追加（`misc` がunionでqueueと衝突する場合は独立フィールドに）。
- **粒度はオクターブ(2のべき)**：閾値はバケット下端(2^b)で返るので、サイズ分割の境界(2のべき)に自然に一致。より細かくしたい場合はバケット内線形補間を追加。
- **「OHW=freq≤1」は在中アクセスでの近似**（真のOHWは将来情報）。退去時確定なので因果的・実装可能。再投入が多いとやや楽観的になり得る点は留意。
- これは**統計のみ規則(A)**。強い「大=冷」分布で最適を外す原理的限界（先述）が残るので、確実な最適が要る場面では候補数点の10%サンプリング・シャドウシミュを別途併用してください。

必要なら、この推定器を使う **region分割キャッシュ本体（2プール＋per-pool Region-LRU＋FDPストリーム割当）の C スケルトン**も書けます。

#ifdef __cplusplus
}
#endif
