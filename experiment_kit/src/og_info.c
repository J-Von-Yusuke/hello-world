/* og_info.c : oracleGeneral.zst の概要を 1 パスで JSON 1 行にする（実験キットの入口）。
 *   n 要求数 / uniq 物の数 / footprint 物の最初のサイズの和 / req_bytes / t_sec 経過秒 /
 *   sat_reqs, sat_bytes  size==UINT32_MAX の要求（埋め値の疑い）/ max_nonsat / size_change_reqs
 * record (24 B): u32 clock_time, u64 obj_id, u32 obj_size, i64 next_access_vtime
 * build: gcc -O2 -o og_info og_info.c -lzstd ; usage: og_info file.zst
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>
typedef unsigned long long ull;
typedef struct { uint64_t k; uint32_t first, last; } ent_t;   /* k = id+1, 0 = 空き */
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
int main(int argc, char **argv) {
  if (argc < 2) return 1;
  FILE *f = fopen(argv[1], "rb"); if (!f) { perror(argv[1]); return 1; }
  tab = calloc(cap, sizeof(ent_t));
  ZSTD_DCtx *d = ZSTD_createDCtx();
  size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4, nc = 0, r;
  uint8_t *in = malloc(ib), *out = malloc(ob), carry[24];
  ull n = 0, fp = 0, rb = 0, sat = 0, satb = 0, mx = 0, sc = 0, dropped = 0; uint32_t t0 = 0, tl = 0;
  const char *dv = getenv("SAT_DROP"); uint64_t drop = dv ? strtoull(dv, 0, 10) : 0;   /* この size の要求は無かったことにする（drop / delete 変種用） */
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc};
      if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi);
      if (ZSTD_isError(rc)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc)); return 2; }
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24) {
        uint32_t t, sz; uint64_t id;
        memcpy(&t, out + o, 4); memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4);
        if (drop && sz == drop) { dropped++; continue; }
        if (!n) t0 = t; tl = t; n++; rb += sz;
        if (sz == 0xFFFFFFFFu) { sat++; satb += sz; } else if (sz > mx) mx = sz;
        if (used * 10 >= cap * 7) grow();
        ent_t *e = find(id);
        if (!e->k) { e->k = id + 1; e->first = e->last = sz; used++; fp += sz; }
        else { if (e->last != sz) sc++; e->last = sz; }
      }
    }
  }
  printf("{\"n\":%llu,\"uniq\":%zu,\"footprint\":%llu,\"req_bytes\":%llu,\"t_sec\":%u,"
         "\"sat_reqs\":%llu,\"sat_bytes\":%llu,\"max_nonsat\":%llu,\"size_change_reqs\":%llu,\"dropped\":%llu}\n",
         n, used, fp, rb, tl - t0, sat, satb, mx, sc, dropped);
  return 0;
}
