/* og_sat_ops.c : size == SAT（既定 UINT32_MAX。サイズ -1 を uint32 で書いたもの）の要求が、どんな操作に見えるかを実測する。
 * 比べる相手は「SAT でない要求」。見るもの:
 *   - その物の最初の要求か（first）、最後の要求か（last: next_access_vtime が無い）
 *   - 同じ物の直前の要求からの間隔（要求数、時刻）。直前の要求が SAT だったか
 *   - 同じ物の次の要求までの間隔（要求数）
 *   - 1 つの物あたりの SAT 要求の数
 * 削除（PURGE/DELETE）なら: last になりやすい、直後の要求まで間隔があく、直前の要求の直後（数秒以内）に来る…などが目印。
 * record (24 B): u32 clock_time, u64 obj_id, u32 obj_size, i64 next_access_vtime。next_access_vtime は要求番号（無ければ -1）。
 * build: gcc -O2 -o og_sat_ops og_sat_ops.c -lzstd ; usage: og_sat_ops file.zst [sat=4294967295]
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>
typedef unsigned long long ull;
typedef struct { uint64_t k; uint32_t last_i, last_t, nsat, nreq; uint8_t last_sat; } ent_t;
static ent_t *tab; static size_t cap = 1 << 20, used;
static ent_t *find(uint64_t id) {
  uint64_t k = id + 1; size_t m = cap - 1, h = (k * 0x9E3779B97F4A7C15ull) >> 20 & m;
  while (tab[h].k && tab[h].k != k) h = (h + 1) & m;
  return &tab[h];
}
static void grow(void) {
  ent_t *old = tab; size_t oc = cap; cap *= 2; tab = calloc(cap, sizeof(ent_t));
  for (size_t i = 0; i < oc; i++) if (old[i].k) *find(old[i].k - 1) = old[i];
  free(old);
}
/* 区分別の集計 [0] = SAT でない  [1] = SAT */
static ull n[2], first[2], lastr[2], prev_sat[2], prev_same_t[2], prev_le5s[2], hist_prev[2][34], hist_next[2][34], hist_prevt[2][34];
static int lg(ull v) { int b = 0; while (v > 1 && b < 33) { v >>= 1; b++; } return b; }
int main(int argc, char **argv) {
  if (argc < 2) return 1;
  uint64_t SAT = argc > 2 ? strtoull(argv[2], 0, 10) : 0xFFFFFFFFull;
  FILE *f = fopen(argv[1], "rb"); if (!f) { perror(argv[1]); return 1; }
  tab = calloc(cap, sizeof(ent_t));
  ZSTD_DCtx *d = ZSTD_createDCtx();
  size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4, nc = 0, r;
  uint8_t *in = malloc(ib), *out = malloc(ob), carry[24]; ull i = 0;
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc};
      if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi);
      if (ZSTD_isError(rc)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc)); return 2; }
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24, i++) {
        uint32_t t, sz; uint64_t id; int64_t nx;
        memcpy(&t, out + o, 4); memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4); memcpy(&nx, out + o + 16, 8);
        int s = sz == SAT;
        if (used * 10 >= cap * 7) grow();
        ent_t *e = find(id); n[s]++;
        if (!e->k) { e->k = id + 1; used++; first[s]++; }
        else {
          prev_sat[s] += e->last_sat; hist_prev[s][lg(i - e->last_i)]++;
          ull dt = t - e->last_t; hist_prevt[s][dt == 0 ? 0 : lg(dt) + 1 > 33 ? 33 : lg(dt) + 1]++; if (dt == 0) prev_same_t[s]++; if (dt <= 5) prev_le5s[s]++;
        }
        if (nx < 0 || nx == INT64_MAX) lastr[s]++; else hist_next[s][lg((ull)nx > i ? nx - i : 1)]++;
        e->last_i = (uint32_t)i; e->last_t = t; e->last_sat = s; e->nreq++; e->nsat += s;
      }
    }
  }
  ull nobj_sat = 0, one = 0, two = 0, many = 0, only_sat = 0;
  for (size_t k = 0; k < cap; k++) if (tab[k].k && tab[k].nsat) {
    nobj_sat++; if (tab[k].nsat == 1) one++; else if (tab[k].nsat == 2) two++; else many++; if (tab[k].nsat == tab[k].nreq) only_sat++; }
  const char *nm[2] = {"SATでない", "SAT"};
  printf("{\"n\":%llu,\"uniq\":%zu,\"n_sat\":%llu,\"objs_with_sat\":%llu,\"objs_1sat\":%llu,\"objs_2sat\":%llu,\"objs_3plus_sat\":%llu,\"objs_only_sat\":%llu,\"rows\":[",
         i, used, n[1], nobj_sat, one, two, many, only_sat);
  for (int s = 0; s < 2; s++) {
    ull rep = n[s] - first[s];
    printf("%s{\"kind\":\"%s\",\"n\":%llu,\"first\":%.5f,\"last\":%.5f,\"prev_is_sat\":%.5f,\"prev_same_second\":%.5f,\"prev_within_5s\":%.5f}",
           s ? "," : "", nm[s], n[s], (double)first[s] / n[s], (double)lastr[s] / n[s], (double)prev_sat[s] / (rep ? rep : 1),
           (double)prev_same_t[s] / (rep ? rep : 1), (double)prev_le5s[s] / (rep ? rep : 1));
  }
  printf("],\"hist\":{");
  for (int s = 0; s < 2; s++) {
    printf("%s\"%s\":{\"prev_gap_req\":[", s ? "," : "", nm[s]);
    for (int b = 0; b < 34; b++) printf("%s%llu", b ? "," : "", hist_prev[s][b]);
    printf("],\"next_gap_req\":[");
    for (int b = 0; b < 34; b++) printf("%s%llu", b ? "," : "", hist_next[s][b]);
    printf("],\"prev_gap_sec\":[");
    for (int b = 0; b < 34; b++) printf("%s%llu", b ? "," : "", hist_prevt[s][b]);
    printf("]}");
  }
  printf("}}\n");
  return 0;
}
