/* og_sat_profile.c : size == UINT32_MAX (4294967295) の要求の正体を調べる。
 * 出力: (1) 全体/sat/非sat の要求数・バイト・ユニーク物数
 *       (2) sat 物の要求回数分布、sat と非 sat の両方の size で現れる物の数
 *       (3) トレースを 10 等分した各区間での sat 要求の割合（要求・バイト）
 *       (4) 非 sat のサイズ log2 ビン別の要求数・バイト
 * build: gcc -O2 -o og_sat_profile og_sat_profile.c -lzstd ; usage: og_sat_profile file.zst [total_reqs]
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
static uint64_t *hk; static uint32_t *hv, *cnt; static uint8_t *fl;   /* fl: 1=sat を見た 2=非 sat を見た */
int main(int argc, char **argv) {
  uint64_t TOT = argc > 2 ? strtoull(argv[2], 0, 10) : 96680668ull;
  FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
  hk = calloc((size_t)1 << HB, 8); hv = malloc(((size_t)1 << HB) * 4); cnt = calloc(NN, 4); fl = calloc(NN, 1);
  ZSTD_DCtx *d = ZSTD_createDCtx(); size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4;
  uint8_t *in = malloc(ib), *out = malloc(ob), *carry = malloc(24); size_t nc = 0, r;
  uint64_t n = 0, uniq = 0, rq[3] = {0}, by[3] = {0}, dq[10] = {0}, dsat[10] = {0}, dby[10] = {0}, dsby[10] = {0}, bq[40] = {0}, bb[40] = {0};
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc}; if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi); if (ZSTD_isError(rc)) return 2;
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24) {
        uint32_t sz; uint64_t id; memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4);
        int sat = (sz == 0xFFFFFFFFu); int dec = (int)(n * 10 / TOT); if (dec > 9) dec = 9; n++;
        uint64_t key = id + 1, h = (key * 0x9E3779B97F4A7C15ull) >> (64 - HB);
        while (hk[h] && hk[h] != key) h = (h + 1) & (((uint64_t)1 << HB) - 1);
        uint32_t i; if (!hk[h]) { hk[h] = key; i = hv[h] = ++uniq; } else i = hv[h];
        cnt[i]++; fl[i] |= sat ? 1 : 2;
        rq[sat]++; by[sat] += sz; dq[dec]++; dby[dec] += sz;
        if (sat) { dsat[dec]++; dsby[dec] += sz; }
        else { int b = 0; uint32_t x = sz; while (x > 1) { x >>= 1; b++; } bq[b]++; bb[b] += sz; }
      }
    }
  }
  uint64_t us = 0, ub = 0, mixed = 0, sat_once = 0, sat_ge2 = 0, sat_reqs_in_ge10 = 0, mx = 0, sum = 0; uint64_t hist[6] = {0};   /* 1,2,3-9,10-99,100-999,1000+ */
  for (uint32_t i = 1; i <= uniq; i++) {
    if (fl[i] & 1) { us++; if (fl[i] & 2) mixed++; uint32_t c = cnt[i]; sum += c; if (c > mx) mx = c;
      hist[c == 1 ? 0 : c == 2 ? 1 : c < 10 ? 2 : c < 100 ? 3 : c < 1000 ? 4 : 5]++; if (c >= 10) sat_reqs_in_ge10 += c; if (c == 1) sat_once++; else sat_ge2++; }
    if (fl[i] & 2) ub++;
  }
  printf("requests: total %llu, sat %llu (%.3f%%), nonsat %llu\n", (ull)n, (ull)rq[1], 100.0 * rq[1] / n, (ull)rq[0]);
  printf("bytes   : sat %.3e (%.2f%%), nonsat %.3e\n", (double)by[1], 100.0 * by[1] / (by[0] + by[1]), (double)by[0]);
  printf("objects : total %llu, with-sat %llu, with-nonsat %llu, BOTH %llu\n", (ull)uniq, (ull)us, (ull)ub, (ull)mixed);
  printf("sat objects by request count: 1:%llu 2:%llu 3-9:%llu 10-99:%llu 100-999:%llu 1000+:%llu  (max %llu, mean %.2f) ; sat reqs on objs with >=10 reqs: %.1f%%\n",
         (ull)hist[0], (ull)hist[1], (ull)hist[2], (ull)hist[3], (ull)hist[4], (ull)hist[5], (ull)mx, (double)sum / us, 100.0 * sat_reqs_in_ge10 / rq[1]);
  printf("per-decile sat share (reqs%% / bytes%%):");
  for (int k = 0; k < 10; k++) printf(" %.2f/%.1f", 100.0 * dsat[k] / dq[k], 100.0 * dsby[k] / dby[k]); printf("\n");
  printf("nonsat size log2 bins (bin: reqs%% of nonsat, bytes%% of nonsat):\n");
  for (int b = 0; b < 33; b++) if (bq[b]) printf("  2^%d: %.2f%% %.2f%%\n", b, 100.0 * bq[b] / rq[0], 100.0 * bb[b] / by[0]);
  return 0;
}
