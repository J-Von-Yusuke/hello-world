/* og_ind.c — 2026-11-26: does the proposal (size classification + allocation) help this workload? Two indicators from the trace alone.
 *   (1) pressure  : how many times the SSD capacity is written over the trace (turnover), in a mixed object-level LRU (Che, IRM).
 *                   Below ~1–2 almost nothing is evicted, so no eviction-side policy can matter (w01 with the filter: 3.9 → 1.1).
 *   (2) spread    : how much of the per-size reuse spread one size cut separates — Otsu's score on the same size bins as st_otsu
 *                   ([2^b, 2^(b+1))), value v_b = ln(requests reaching the SSD / bytes written to the SSD), weights = bytes written,
 *                   computed for the mixed LRU (object level, so popular content is not hidden by co-located junk as in a Region run).
 * DRAM (-D) is a separate LRU in front (CacheLib inclusion): the SSD sees request rate λ·e^{−λ T_D}. The filter (-a K) admits only
 * content requested at least K times in the trace (K = 2: the one-hit filter, a first-order S3-FIFO model); its requests still count
 * as misses but it never writes or occupies. One pass over the trace; outputs JSON for K = 1 and the given K.
 * usage: og_ind -C ssd_bytes [-D dram_bytes] [-a K] [-n max_requests] file.zst   (-n: only the first requests, as cachesim -n)     env SAT_DROP=<size>: drop requests of that size (as og_info)
 * build: gcc -O3 -march=native -o og_ind og_ind.c -lzstd -lm */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>

typedef struct { uint64_t k; uint32_t s, c; } ent_t;
static ent_t *tab; static size_t cap = 1 << 22, used;
static ent_t *find(uint64_t id) {
  uint64_t k = id + 1; size_t m = cap - 1, h = (k * 0x9E3779B97F4A7C15ull) >> 20 & m;
  while (tab[h].k && tab[h].k != k) h = (h + 1) & m;
  return &tab[h];
}
static void grow(void) {
  ent_t *old = tab; size_t oc = cap; cap *= 2; tab = calloc(cap, sizeof(ent_t));
  if (!tab) { fprintf(stderr, "og_ind: out of memory\n"); exit(2); }
  for (size_t i = 0; i < oc; i++) if (old[i].k) *find(old[i].k - 1) = old[i];
  free(old);
}
/* bins: size octave o = floor(log2 s) (st_otsu's bins) x request-count key q (exact up to 64, then 1% steps) */
#define NO 48
#define QX 64
#define NQ 2400
static int qkey(uint32_t c) { return c <= QX ? (int)c : QX + 1 + (int)floor(log((double)c / QX) / log(1.01)); }
typedef struct { int o; double n, c, s, lam; } bin_t;   /* objects, requests, bytes, λ = requests per object / all requests */
static bin_t *B; static int NB; static double N;

/* occupancy of an LRU at characteristic time T for request rates r(i) (per bin, per object), over admitted bins */
static double occ(double T, const double *r, const char *adm) {
  double c = 0; for (int i = 0; i < NB; i++) if (adm[i]) c += B[i].s * (1 - exp(-r[i] * T)); return c;
}
static double T_of(double C, const double *r, const char *adm) {
  double full = 0; for (int i = 0; i < NB; i++) if (adm[i]) full += B[i].s;
  if (C >= full) return INFINITY;
  double lo = -10, hi = log(1e6 * N);
  for (int it = 0; it < 80; it++) { double m = (lo + hi) / 2; if (occ(exp(m), r, adm) < C) lo = m; else hi = m; }
  return exp((lo + hi) / 2);
}
static void otsu(const double *A, const double *Q, double *total, double *between, int *cut) {
  double W = 0, WV = 0, v[NO]; int ok[NO];
  for (int o = 0; o < NO; o++) { ok[o] = A[o] > 0 && Q[o] > 0; v[o] = ok[o] ? log(Q[o] / A[o]) : 0; if (ok[o]) W += A[o], WV += A[o] * v[o]; }
  *total = *between = 0; *cut = -1; if (W <= 0) return;
  double m = WV / W, s = 0, sv = 0;
  for (int o = 0; o < NO; o++) if (ok[o]) *total += A[o] * (v[o] - m) * (v[o] - m) / W;
  for (int k = 1; k < NO; k++) {   /* small = octaves < k (sizes < 2^k, st_otsu's tau = 2^k - 1) */
    if (ok[k - 1]) s += A[k - 1], sv += A[k - 1] * v[k - 1];
    double L = W - s; if (s <= 0 || L < 0.05 * W) continue;
    double d = sv / s - (WV - sv) / L, sc = (s / W) * (L / W) * d * d;
    if (sc > *between) *between = sc, *cut = k;
  }
}
static void report(const char *tag, double K, double C, double D, int first) {
  double *rd = malloc(sizeof(double) * NB), *r = malloc(sizeof(double) * NB); char *all = malloc(NB), *adm = malloc(NB);
  for (int i = 0; i < NB; i++) all[i] = 1, adm[i] = B[i].c >= K * B[i].n - 1e-9, rd[i] = B[i].lam;
  double TD = D > 0 ? T_of(D, rd, all) : 0;
  for (int i = 0; i < NB; i++) r[i] = B[i].lam * (isinf(TD) ? 0 : exp(-B[i].lam * TD));   /* rate reaching the SSD */
  double T = T_of(C, r, adm), A[NO] = {0}, Q[NO] = {0}, W = 0, req = 0, hitb = 0, rb = 0, dramb = 0;
  for (int i = 0; i < NB; i++) {
    double sl = B[i].s * B[i].lam; rb += sl; dramb += sl - B[i].s * r[i];
    if (!adm[i]) continue;
    double e = isinf(T) ? 0 : exp(-r[i] * T), q = B[i].s * r[i], a = q * e;   /* bytes requested at / written to the SSD, per request */
    Q[B[i].o] += q; A[B[i].o] += a; hitb += q - a;
    W += B[i].s * fmax(1.0, N * r[i] * e) / fmax(B[i].n, 1) * B[i].n;   /* bytes written over the trace (at least once per object) */
    req += q;
  }
  double tot, bet; int cut; otsu(A, Q, &tot, &bet, &cut);
  printf("%s\"%s\":{\"admit_min\":%g,\"T_dram\":%.6g,\"T_ssd\":%.6g,\"turnover\":%.6g,\"spread_total\":%.6g,\"spread_between\":%.6g,\"eta\":%.6g,"
         "\"cut_log2\":%d,\"model_bhr\":%.6g,\"model_dram_bhr\":%.6g,\"bins\":[", first ? "" : ",", tag, K, isinf(TD) ? -1 : TD, isinf(T) ? -1 : T,
         W / C, tot, bet, tot > 0 ? bet / tot : 0, cut, (dramb + hitb) / rb, dramb / rb);
  int f = 1;
  for (int o = 0; o < NO; o++) if (A[o] > 0 || Q[o] > 0) printf("%s[%d,%.6g,%.6g]", f ? "" : ",", o, A[o], Q[o]), f = 0;
  printf("]}");
  free(rd); free(r); free(all); free(adm);
}

int main(int argc, char **argv) {
  double C = 0, D = 0, K = 2; unsigned long long nmax = 0; const char *path = 0;
  for (int a = 1; a < argc; a++) {
    if (!strcmp(argv[a], "-C") && a + 1 < argc) C = atof(argv[++a]);
    else if (!strcmp(argv[a], "-D") && a + 1 < argc) D = atof(argv[++a]);
    else if (!strcmp(argv[a], "-a") && a + 1 < argc) K = atof(argv[++a]);
    else if (!strcmp(argv[a], "-n") && a + 1 < argc) nmax = strtoull(argv[++a], 0, 10);
    else path = argv[a];
  }
  if (!path || C <= 0) { fprintf(stderr, "usage: og_ind -C ssd_bytes [-D dram_bytes] [-a K] [-n max_requests] file.zst\n"); return 1; }
  FILE *f = fopen(path, "rb"); if (!f) { perror(path); return 1; }
  tab = calloc(cap, sizeof(ent_t));
  ZSTD_DCtx *d = ZSTD_createDCtx();
  size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4, nc = 0, rr;
  uint8_t *in = malloc(ib), *out = malloc(ob), carry[24];
  unsigned long long n = 0, dropped = 0;
  const char *dv = getenv("SAT_DROP"); uint64_t drop = dv ? strtoull(dv, 0, 10) : 0;
  while ((!nmax || n < nmax) && (rr = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, rr, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc};
      if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi);
      if (ZSTD_isError(rc)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc)); return 2; }
      size_t fl = zo.pos / 24 * 24; nc = zo.pos - fl; if (nc) memcpy(carry, out + fl, nc);
      for (size_t o = 0; o < fl && (!nmax || n < nmax); o += 24) {
        uint32_t sz; uint64_t id; memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4);
        if (drop && sz == drop) { dropped++; continue; }
        n++;
        if (used * 10 >= cap * 7) grow();
        ent_t *e = find(id);
        if (!e->k) { e->k = id + 1; e->s = sz; e->c = 0; used++; }
        if (e->c < UINT32_MAX) e->c++;
      }
    }
  }
  fclose(f);
  if (!n) { fprintf(stderr, "og_ind: no requests\n"); return 1; }
  N = (double)n;
  double *gn = calloc((size_t)NO * NQ, sizeof(double)), *gc = calloc((size_t)NO * NQ, sizeof(double)), *gs = calloc((size_t)NO * NQ, sizeof(double));
  double fp = 0, one = 0, onefp = 0;
  for (size_t i = 0; i < cap; i++) if (tab[i].k) {
    int o = tab[i].s ? (int)floor(log2((double)tab[i].s)) : 0, q = qkey(tab[i].c); if (o >= NO) o = NO - 1; if (q >= NQ) q = NQ - 1;
    size_t j = (size_t)o * NQ + q; gn[j] += 1; gc[j] += tab[i].c; gs[j] += tab[i].s; fp += tab[i].s;
    if (tab[i].c == 1) one += 1, onefp += tab[i].s;
  }
  free(tab);
  B = malloc(sizeof(bin_t) * (size_t)NO * NQ); NB = 0;
  for (int o = 0; o < NO; o++) for (int q = 0; q < NQ; q++) {
    size_t j = (size_t)o * NQ + q; if (!gn[j]) continue;
    B[NB++] = (bin_t){o, gn[j], gc[j], gs[j], gc[j] / gn[j] / N};
  }
  printf("{\"n\":%llu,\"dropped\":%llu,\"uniq\":%zu,\"footprint\":%.0f,\"one_hit_frac\":%.6f,\"one_hit_bytes_frac\":%.6f,\"ssd\":%.0f,\"dram\":%.0f,\"C_over_fp\":%.6g,",
         n, dropped, used, fp, one / used, onefp / fp, C, D, C / fp);
  report("no_filter", 1, C, D, 1); report("filter", K, C, D, 0);
  printf("}\n");
  return 0;
}
