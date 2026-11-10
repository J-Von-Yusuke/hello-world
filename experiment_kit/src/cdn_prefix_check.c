/* cdn_prefix_check.c : cdn_partial_trace_validity.md の前提を oracleGeneral.zst の先頭からの1パスで実測する。
 * record (24 B): u32 clock_time, u64 obj_id, u32 obj_size, i64 next_access_vtime
 * 1パスで同時に出すもの（n = 先頭からの要求数）:
 *   K_pieces   = sum min(ceil(s/64KiB),32000)            （§8 の K(n)）
 *   req_bytes, cold_bytes（初回アクセスのバイト＝強制ミス）, uniq_objs, trace_sec
 *   lru_miss_bytes = 通常のオブジェクト単位 LRU（容量 CAP バイト、サイズ変化は不一致=ミス、CAP 超は非受入）のミスバイト
 *   lru_write_bytes = 同 LRU が受け入れたバイト（全受け入れの書き込み量に相当）
 * 先頭 P 要求のサイズ p50/p90/p99/mean（SCION 表1 照合用）も出す。
 * build: gcc -O2 -o cdn_prefix_check cdn_prefix_check.c -lzstd
 * usage: cdn_prefix_check file.zst [P=200000] [step=0: 既定のチェックポイント / >0: step の倍数の行すべて] [CAP=257698037760]
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <zstd.h>

#define HB 28                         /* 2^28 slot */
#ifndef NN
#define NN (1u << 26)   /* 最大ユニーク物数。足りなければ -DNN=... */
#endif
typedef unsigned long long ull;
static uint64_t *hk; static uint32_t *hv;
static uint32_t *prv, *nxt, *nsz; static uint8_t *res;   /* LRU 連結リスト（添字 1..）。0 = なし */
static uint32_t head, tail;                              /* head = MRU, tail = LRU */
static int cmpu(const void *a, const void *b) { uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b; return (x > y) - (x < y); }
static void unlink_(uint32_t i) { if (prv[i]) nxt[prv[i]] = nxt[i]; else head = nxt[i]; if (nxt[i]) prv[nxt[i]] = prv[i]; else tail = prv[i]; }
static void push_front(uint32_t i) { prv[i] = 0; nxt[i] = head; if (head) prv[head] = i; head = i; if (!tail) tail = i; }

int main(int argc, char **argv) {
  if (argc < 2) return 1;
  uint64_t P = argc > 2 ? strtoull(argv[2], 0, 10) : 200000;
  uint64_t step = argc > 3 ? strtoull(argv[3], 0, 10) : 0;
  uint64_t CAP = argc > 4 ? strtoull(argv[4], 0, 10) : 257698037760ull;
  uint64_t SATV = getenv("SAT_DROP") ? strtoull(getenv("SAT_DROP"), 0, 10) : 0;   /* この size の要求は数えない */
  int SATDEL = getenv("SAT_OP") && !strcmp(getenv("SAT_OP"), "delete");        /* 数えない代わりに、その物を LRU から消す */
  uint64_t PIECE = getenv("PIECE") ? strtoull(getenv("PIECE"), 0, 10) : 65536;   /* K(n) の piece サイズ。CacheLib は 131072 */
  static const uint64_t cp[] = {30000, 32000, 100000, 200000, 303587, 1000000, 3000000, 10000000, 30000000, 0};
  FILE *f = fopen(argv[1], "rb"); if (!f) { perror("open"); return 1; }
  hk = calloc((size_t)1 << HB, 8); hv = malloc(((size_t)1 << HB) * 4);
  prv = calloc(NN, 4); nxt = calloc(NN, 4); nsz = calloc(NN, 4); res = calloc(NN, 1);
  ZSTD_DCtx *d = ZSTD_createDCtx();
  size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4;
  uint8_t *in = malloc(ib), *out = malloc(ob), *carry = malloc(24); size_t nc = 0;
  uint32_t *psz = malloc(P * 4); uint64_t np = 0;
  uint64_t n = 0, K = 0, uniq = 0, bytes = 0, cold = 0, smax = 0, used = 0, lmiss = 0, lwr = 0, big = 0, bigb = 0, sat = 0, scb = 0, scn = 0; uint32_t t0 = 0, tl = 0; int ci = 0;
  printf("n,K_pieces,req_bytes,cold_bytes,uniq_objs,trace_sec,lru_miss_bytes,lru_write_bytes\n");
  size_t r;
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc};
      if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi);
      if (ZSTD_isError(rc)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc)); return 2; }
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24) {
        uint32_t t, sz; uint64_t id; memcpy(&t, out + o, 4); memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4);
        if (SATV && sz == SATV) {
          if (SATDEL) { uint64_t k = id + 1, h = (k * 0x9E3779B97F4A7C15ull) >> (64 - HB);
            while (hk[h] && hk[h] != k) h = (h + 1) & (((uint64_t)1 << HB) - 1);
            if (hk[h]) { uint32_t i = hv[h]; if (res[i]) { unlink_(i); used -= nsz[i]; res[i] = 0; } } }
          continue;
        }
        if (n == 0) t0 = t; tl = t; n++;
        uint64_t k = (sz + PIECE - 1) / PIECE; if (k > 32000) k = 32000; if (k == 0) k = 1; K += k; bytes += sz; if (sz > smax) smax = sz;
        if (sz >= 2147483648u) { big++; bigb += sz; if (sz == 0xFFFFFFFFu) sat++; }
        uint64_t key = id + 1, h = (key * 0x9E3779B97F4A7C15ull) >> (64 - HB);
        while (hk[h] && hk[h] != key) h = (h + 1) & (((uint64_t)1 << HB) - 1);
        uint32_t i;
        if (!hk[h]) { hk[h] = key; i = hv[h] = ++uniq; if (uniq >= NN) { fprintf(stderr, "NN overflow\n"); return 3; } cold += sz; }
        else i = hv[h];
        /* object-LRU */
        if (res[i] && nsz[i] == sz) { unlink_(i); push_front(i); }
        else {
          lmiss += sz; if (res[i]) { scb += sz; scn++; }
          if (res[i]) { unlink_(i); used -= nsz[i]; res[i] = 0; }
          if (sz <= CAP) {
            while (used + sz > CAP) { uint32_t v = tail; unlink_(v); used -= nsz[v]; res[v] = 0; }
            nsz[i] = sz; res[i] = 1; used += sz; lwr += sz; push_front(i);
          }
        }
        if (np < P) psz[np++] = sz;
        if ((step && n % step == 0) || (!step && cp[ci] && n == cp[ci])) {
          printf("%llu,%llu,%llu,%llu,%llu,%u,%llu,%llu\n", (ull)n, (ull)K, (ull)bytes, (ull)cold, (ull)uniq, tl - t0, (ull)lmiss, (ull)lwr); ci++;
        }
        if (n == P) {
          qsort(psz, np, 4, cmpu); double s = 0; for (uint64_t i2 = 0; i2 < np; i2++) s += psz[i2];
          printf("# first %llu req size: p50=%u p90=%u p99=%u max=%u mean=%.1f\n", (ull)P, psz[np / 2], psz[np * 9 / 10], psz[np * 99 / 100], psz[np - 1], s / np);
        }
      }
    }
  }
  printf("%llu,%llu,%llu,%llu,%llu,%u,%llu,%llu\n", (ull)n, (ull)K, (ull)bytes, (ull)cold, (ull)uniq, tl - t0, (ull)lmiss, (ull)lwr);
  printf("# SIZECHANGE: LRU misses on resident objects whose size differs: %llu reqs, %.3e B = %.2f%% of req bytes, %.2f%% of LRU miss bytes\n", (ull)scn, (double)scb, 100.0 * scb / bytes, 100.0 * scb / lmiss);
  printf("# FULL: max size %llu B, requests >=2GiB: %llu (%.4f%%), their bytes %.2f%% of req bytes, ==UINT32_MAX: %llu, CAP=%llu\n", (ull)smax, (ull)big, 100.0 * big / n, 100.0 * bigb / bytes, (ull)sat, (ull)CAP);
  return 0;
}
