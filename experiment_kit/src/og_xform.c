/* og_xform.c : size == UINT32_MAX の要求を変換した oracleGeneral.zst を作る（感度分析用）。
 *   drop : その要求を除外
 *   fix  : その物の「sat 以外の最大サイズ」に置換（sat 以外のサイズが 1 つも無い物の要求は除外）
 * 2 パス（1 パス目で物ごとの最大 非sat サイズを集める）。next_access_vtime はそのまま（LRU 系の cachesim は使わない）。
 * 統計: sat と非 sat の両方を持つ物のうち、非 sat サイズが複数種ある物の数。
 * build: gcc -O2 -o og_xform og_xform.c -lzstd ; usage: og_xform in.zst out.zst drop|fix
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <zstd.h>
#define HB 27
#ifndef NN
#define NN (1u << 26)   /* 最大ユニーク物数。足りなければ -DNN=... */
#endif
typedef unsigned long long ull;
static uint64_t *hk; static uint32_t *hv, *mx, *first; static uint8_t *fl;   /* fl: 1=sat 2=非sat 4=非satサイズが複数 */

typedef void (*cb_t)(uint8_t *rec);
static void scan(const char *path, cb_t cb) {
  FILE *f = fopen(path, "rb"); if (!f) { perror(path); exit(1); }
  ZSTD_DCtx *d = ZSTD_createDCtx(); size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4;
  uint8_t *in = malloc(ib), *out = malloc(ob), *carry = malloc(24); size_t nc = 0, r;
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc}; if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi); if (ZSTD_isError(rc)) exit(2);
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24) cb(out + o);
    }
  }
  fclose(f);
}
static uint32_t idx(uint64_t id, uint32_t *uniq) {
  uint64_t key = id + 1, h = (key * 0x9E3779B97F4A7C15ull) >> (64 - HB);
  while (hk[h] && hk[h] != key) h = (h + 1) & (((uint64_t)1 << HB) - 1);
  if (!hk[h]) { hk[h] = key; hv[h] = ++*uniq; }
  return hv[h];
}
static uint32_t uniq = 0;
static void pass1(uint8_t *rec) {
  uint64_t id; uint32_t sz; memcpy(&id, rec + 4, 8); memcpy(&sz, rec + 12, 4);
  uint32_t i = idx(id, &uniq);
  if (sz == 0xFFFFFFFFu) { fl[i] |= 1; return; }
  if (fl[i] & 2) { if (sz != first[i]) fl[i] |= 4; } else { first[i] = sz; fl[i] |= 2; }
  if (sz > mx[i]) mx[i] = sz;
}
static ZSTD_CCtx *cc; static FILE *fo; static uint8_t *obuf; static size_t obsz; static int dropmode;
static uint64_t kept = 0, dropped = 0, fixed = 0;
static uint8_t *wb; static size_t wn, wcap = 24 * 65536;
static void flushw(int end) {
  ZSTD_inBuffer zi = {wb, wn, 0};
  ZSTD_EndDirective m = end ? ZSTD_e_end : ZSTD_e_continue; size_t rem;
  do { ZSTD_outBuffer zo = {obuf, obsz, 0}; rem = ZSTD_compressStream2(cc, &zo, &zi, m); if (ZSTD_isError(rem)) exit(3); fwrite(obuf, 1, zo.pos, fo); }
  while (end ? rem != 0 : zi.pos < zi.size);
  wn = 0;
}
static void pass2(uint8_t *rec) {
  uint64_t id; uint32_t sz; memcpy(&id, rec + 4, 8); memcpy(&sz, rec + 12, 4);
  if (sz == 0xFFFFFFFFu) {
    uint32_t i = idx(id, &uniq);
    if (dropmode || !(fl[i] & 2)) { dropped++; return; }
    sz = mx[i]; memcpy(rec + 12, &sz, 4); fixed++;
  }
  memcpy(wb + wn, rec, 24); wn += 24; kept++;
  if (wn == wcap) flushw(0);
}
int main(int argc, char **argv) {
  if (argc < 4) return 1; dropmode = !strcmp(argv[3], "drop");
  hk = calloc((size_t)1 << HB, 8); hv = malloc(((size_t)1 << HB) * 4); mx = calloc(NN, 4); first = calloc(NN, 4); fl = calloc(NN, 1);
  scan(argv[1], pass1);
  uint64_t both = 0, multi = 0, satobj = 0, satonly = 0;
  for (uint32_t i = 1; i <= uniq; i++) if (fl[i] & 1) { satobj++; if (fl[i] & 2) { both++; if (fl[i] & 4) multi++; } else satonly++; }
  fprintf(stderr, "sat objs %llu: both %llu (non-sat size varies: %llu), sat-only %llu\n", (ull)satobj, (ull)both, (ull)multi, (ull)satonly);
  cc = ZSTD_createCCtx(); ZSTD_CCtx_setParameter(cc, ZSTD_c_compressionLevel, 3); ZSTD_CCtx_setParameter(cc, ZSTD_c_nbWorkers, 4);
  fo = fopen(argv[2], "wb"); obsz = ZSTD_CStreamOutSize(); obuf = malloc(obsz); wb = malloc(wcap);
  scan(argv[1], pass2); flushw(1); fclose(fo);
  fprintf(stderr, "%s: kept %llu, dropped %llu, fixed %llu\n", argv[3], (ull)kept, (ull)dropped, (ull)fixed);
  return 0;
}
