/*
 * og_trace_stats.c -- zstd-compressed oracleGeneral trace -> size-share statistics
 * (trace_characteristics.md  sec.2 / 4 / 5)
 *
 * record (24 B): u32 clock_time, u64 obj_id, u32 obj_size, i64 next_access_vtime
 *
 * Design
 *  - zstd cannot seek, so ONE producer decompresses the mmap'd file once, from byte 0
 *    to the end of the last window, and hands the chunk parts that fall inside a window to
 *    that window's consumer thread (open-addressing table id -> {count, first size};
 *    grows by doubling, MAP_POPULATE).  Wall time ~ one decompression pass + the last
 *    window's ingest tail.  Memory: chunks of windows not yet ingested stay queued.
 *  - N is read from the first frame header (single-frame file assumed; -F scans all frames).
 *  - Everything after the table is derived from a histogram over the request
 *    count n (lambda_hat = n/dur is a function of n alone):
 *        per n: c=#obj, S=sum s, Sy=sum s*log2 s, Syy=sum s*log2(s)^2
 *    so the byte-weighted Var(log s), Var(log lambda), Cov, the Che
 *    characteristic time (bisection) and the "contested" subset (0.1<=lambda*T<=10)
 *    cost O(#distinct n), not O(#objects).
 *  - Spearman rho(s, lambda_hat) uses mid-ranks of n (exact) and of log2(s) binned
 *    at 1/64 octave (approx. ties inside a bin).
 *
 * build: gcc -O3 -march=native -pthread og_trace_stats.c -o og_trace_stats -lzstd -lm
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#define ZSTD_STATIC_LINKING_ONLY   /* ZSTD_findDecompressedSize */
#include <zstd.h>

#define REC 24
#define CHUNK (REC * (1u << 17))
#define NB (1u << 20)   /* exact histogram for n < NB; rarer, hotter objects kept individually */
#define SB 2048         /* log2(s) bins, 1/64 octave */
#define NCHE 3
#define NV (2 + NCHE)   /* variants: all, n>=2, Che x NCHE */
static const double CHE_FRAC[NCHE] = {0.05, 0.10, 0.30};
static const char *VNAME[NV] = {"all", "n>=2", "contested@5%", "contested@10%", "contested@30%"};
static const char *WNAME[2] = {"byte-weighted", "request-weighted"};   /* weight of an object: s  |  n */
#define NT 8                                                           /* thresholds tau for the skew P_req/P_byte */
static const double TAU[NT] = {4096, 16384, 65536, 262144, 1048576, 16777216, 268435456, 2147483648.0};
static const char *TNAME[NT] = {"4KiB", "16KiB", "64KiB", "256KiB", "1MiB", "16MiB", "256MiB", "2GiB"};

typedef struct { uint64_t id; uint32_t cnt, sz; } slot_t;
typedef struct { double n, c, S, Sy, Syy, Y1, Y2; } cb_t;   /* per count n: #obj, sum s, s*y, s*y^2, y, y^2 (y=log2 s) */
typedef struct { double vs, vl, cov, sh, bytes; } mom_t;

typedef struct { uint8_t *data; int ref; } chunk_t;            /* decompressed chunk shared by the windows it overlaps */
typedef struct { chunk_t *ch; uint64_t lo, hi; } item_t;       /* records [lo,hi) of ch */

typedef struct {
  uint64_t start, len;
  /* result */
  uint64_t uniq, zero_obj, chg;
  double dur, foot, rho, che_u[NCHE];
  mom_t m[2][NV];
  double gini, pr[NT], pb[NT];   /* Lorenz bulge; P_req(tau), P_byte(tau) */
  double t_hash, t_post;
  /* working state */
  slot_t *T; uint64_t mask; int shift;
  uint64_t seen; uint32_t t0, t1;
  item_t *q; size_t qh, qt, qcap;                               /* FIFO, guarded by g_mu */
} win_t;

static const uint8_t *g_src; static size_t g_size;
static win_t *g_win; static int g_nwin, g_done;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_cv = PTHREAD_COND_INITIALIZER;
static double g_tdec;
static int g_verbose;

static double now(void) {
  struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
  return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static void thread_log(const char *who) {   /* -v: CPU split and page faults of the calling thread */
  if (!g_verbose) return;
  struct rusage ru; getrusage(RUSAGE_THREAD, &ru);
  fprintf(stderr, "[%s] user=%.2fs sys=%.2fs minflt=%ld\n", who, ru.ru_utime.tv_sec + ru.ru_utime.tv_usec * 1e-6,
          ru.ru_stime.tv_sec + ru.ru_stime.tv_usec * 1e-6, ru.ru_minflt);
}

/* ---------------------------------------------------------------- ingest */
#define HASH(id, sh) (((id) * 0x9E3779B97F4A7C15ull) >> (sh))
#define PF 16   /* prefetch distance (records) */

/* zeroed table.  At load 0.25-0.5 random ids touch every 4 KiB page anyway, so pre-fault it in one call:
 * the per-page fault traps dominated ingest (sys > user) under WSL2. */
static slot_t *tab_alloc(uint64_t n) {
  void *p = mmap(0, n * sizeof(slot_t), PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_POPULATE, -1, 0);
  if (p == MAP_FAILED) { perror("mmap table"); exit(1); }
  return p;
}

static void grow(win_t *w) {   /* double the table; keeps load <= 0.5 so memory follows #unique, not window length */
  uint64_t ns = (w->mask + 1) * 2; int sh = w->shift - 1;
  slot_t *N = tab_alloc(ns);
  for (uint64_t i = 0; i <= w->mask; i++) {
    if (!w->T[i].cnt) continue;
    uint64_t h = HASH(w->T[i].id, sh);
    while (N[h].cnt) h = (h + 1) & (ns - 1);
    N[h] = w->T[i];
  }
  munmap(w->T, (w->mask + 1) * sizeof(slot_t)); w->T = N; w->mask = ns - 1; w->shift = sh;
}

static void ingest(win_t *w, const uint8_t *p, uint64_t k) {
  slot_t *T = w->T; uint64_t mask = w->mask; int sh = w->shift;
  uint32_t t; memcpy(&t, p, 4);
  if (!w->seen) w->t0 = t;
  for (uint64_t i = 0; i < k; i++, p += REC) {
    uint64_t id; uint32_t sz; memcpy(&id, p + 4, 8); memcpy(&sz, p + 12, 4);
    if (i + PF < k) { uint64_t nid; memcpy(&nid, p + PF * REC + 4, 8); __builtin_prefetch(&T[HASH(nid, sh)]); }
    uint64_t h = HASH(id, sh);
    while (T[h].cnt && T[h].id != id) h = (h + 1) & mask;
    slot_t *s = &T[h];
    if (s->cnt) { w->chg += (sz != s->sz); s->cnt++; continue; }
    s->id = id; s->sz = sz; s->cnt = 1;
    if (++w->uniq * 2 > mask + 1) { grow(w); T = w->T; mask = w->mask; sh = w->shift; }
  }
  memcpy(&w->t1, p - REC, 4);
  w->seen += k;
}

/* ---------------------------------------------------------------- statistics */
/* weighted moments of (x=log2 n, y=log2 s) over bins with nlo<=n<=nhi; rw: object weight n (requests) instead of s (bytes).
 * .bytes = selected weight / total weight (named for the byte case) */
static mom_t moments(const cb_t *b, size_t m, double nlo, double nhi, int rw, double total) {
  double W = 0, sx = 0, sy = 0, sxx = 0, syy = 0, sxy = 0;
  for (size_t i = 0; i < m; i++) {
    if (b[i].n < nlo || b[i].n > nhi) continue;
    double x = log2(b[i].n), n = b[i].n;
    double w = rw ? n * b[i].c : b[i].S, wy = rw ? n * b[i].Y1 : b[i].Sy, wyy = rw ? n * b[i].Y2 : b[i].Syy;
    W += w; sx += w * x; sxx += w * x * x; sy += wy; syy += wyy; sxy += x * wy;
  }
  mom_t r = {NAN, NAN, NAN, NAN, W / total};
  if (W <= 0) return r;
  double mx = sx / W, my = sy / W;
  r.vl = sxx / W - mx * mx; r.vs = syy / W - my * my; r.cov = sxy / W - mx * my;
  double vv = r.vl + r.vs - 2 * r.cov;
  r.sh = vv > 0 ? (r.vs - r.cov) / vv : NAN;
  return r;
}

/* u = T/dur with  sum_i s_i (1-exp(-n_i u)) = C   (Che approximation) */
static double che_u(const cb_t *b, size_t m, double C) {
  double lo = -64, hi = 64;
  for (int it = 0; it < 64; it++) {
    double mid = 0.5 * (lo + hi), u = exp2(mid), g = 0;
    for (size_t i = 0; i < m; i++) g += b[i].S * -expm1(-b[i].n * u);
    if (g < C) lo = mid; else hi = mid;
  }
  return exp2(hi);
}

static int cmp_u32(const void *a, const void *b) {
  uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b; return (x > y) - (x < y);
}

static void post(win_t *w) {
  double t = now();
  const uint64_t nslot = w->mask + 1;
  cb_t *dense = calloc(NB, sizeof(cb_t));
  uint32_t *ov = malloc((w->len / NB + 16) * sizeof(uint32_t)); size_t nov = 0;   /* overflow counts */
  uint32_t *ovs = malloc((w->len / NB + 16) * sizeof(uint32_t));                  /* ... and sizes */
  uint64_t sh_[SB] = {0}; uint64_t M = 0; double foot = 0, reqs = 0;
  double rq[SB] = {0}, by[SB] = {0};   /* per 1/64-octave size bin: requests, object bytes (Lorenz curve) */
  for (uint64_t i = 0; i < nslot; i++) {
    const slot_t *s = &w->T[i]; if (!s->cnt) continue;
    if (!s->sz) { w->zero_obj++; continue; }
    double y = log2((double)s->sz), S = s->sz;
    if (s->cnt < NB) { cb_t *d = &dense[s->cnt]; d->c++; d->S += S; d->Sy += S * y; d->Syy += S * y * y; d->Y1 += y; d->Y2 += y * y; }
    else { ov[nov] = s->cnt; ovs[nov++] = s->sz; }
    int sb_ = (int)(y * 64); sh_[sb_]++; rq[sb_] += s->cnt; by[sb_] += S; M++; foot += S; reqs += s->cnt;
  }
  /* compact: n ascending (dense), then single overflow objects */
  cb_t *b = malloc((NB + nov) * sizeof(cb_t)); size_t m = 0;
  for (uint32_t n = 1; n < NB; n++) if (dense[n].c > 0) { b[m] = dense[n]; b[m++].n = n; }
  for (size_t i = 0; i < nov; i++) {
    double y = log2((double)ovs[i]), S = ovs[i];
    b[m++] = (cb_t){ov[i], 1, S, S * y, S * y * y, y, y * y};
  }
  w->foot = foot;
  w->dur = fmax((double)w->t1 - w->t0, 1.0);
  const double tot[2] = {foot, reqs};
  for (int rw = 0; rw < 2; rw++) {
    w->m[rw][0] = moments(b, m, 1, INFINITY, rw, tot[rw]);
    w->m[rw][1] = moments(b, m, 2, INFINITY, rw, tot[rw]);
  }
  for (int k = 0; k < NCHE; k++) {   /* contested set: n-range from the Che time (bytes decide the cache size) */
    double u = che_u(b, m, CHE_FRAC[k] * foot); w->che_u[k] = u;
    for (int rw = 0; rw < 2; rw++) w->m[rw][2 + k] = moments(b, m, 0.1 / u, 10 / u, rw, tot[rw]);
  }
  free(b);

  /* Lorenz curve of objects sorted by size: x = cumulative byte share, y = cumulative request share.
   * gini = 2*area(y over x) - 1 (0 = requests spread like bytes); skew(tau) = P_req/P_byte for s < tau */
  { double cb = 0, cr = 0, area = 0; int nt = 0;
    for (int i = 0; i < SB; i++) {
      for (; nt < NT && i == (int)(log2(TAU[nt]) * 64 + 0.5); nt++) { w->pr[nt] = cr / reqs; w->pb[nt] = cb / foot; }
      double nb = cb + by[i], nr = cr + rq[i];
      area += (nb - cb) / foot * (nr + cr) / 2 / reqs; cb = nb; cr = nr;
    }
    for (; nt < NT; nt++) { w->pr[nt] = 1; w->pb[nt] = 1; }
    w->gini = 2 * area - 1; }

  /* Spearman: mid-ranks of n (exact) and of 1/64-octave log2(s) bin */
  double *rs = malloc(SB * sizeof(double)); { double acc = 0;
    for (int i = 0; i < SB; i++) { rs[i] = acc + (sh_[i] + 1) / 2.0; acc += sh_[i]; } }
  double *rn = malloc(NB * sizeof(double)); double dtot; { double acc = 0;
    for (uint32_t n = 0; n < NB; n++) { rn[n] = acc + (dense[n].c + 1) / 2.0; acc += dense[n].c; } dtot = acc; }
  qsort(ov, nov, 4, cmp_u32);
  double sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
  for (uint64_t i = 0; i < nslot; i++) {
    const slot_t *s = &w->T[i]; if (!s->cnt || !s->sz) continue;
    double a;
    if (s->cnt < NB) a = rn[s->cnt];
    else { size_t lt = 0, eq = 0; for (size_t j = 0; j < nov; j++) { lt += ov[j] < s->cnt; eq += ov[j] == s->cnt; }
           a = dtot + lt + (eq + 1) / 2.0; }
    double bb = rs[(int)(log2((double)s->sz) * 64)];
    sa += a; sb += bb; saa += a * a; sbb += bb * bb; sab += a * bb;
  }
  double ca = sab / M - sa / M * sb / M, va = saa / M - (sa / M) * (sa / M), vb = sbb / M - (sb / M) * (sb / M);
  w->rho = ca / sqrt(va * vb);
  free(rs); free(rn); free(dense); free(ov); free(ovs);
  w->t_post = now() - t;
}

/* ---------------------------------------------------------------- stream */
/* hand the part of chunk [idx, idx+k) that lies in each window to that window's worker */
static void dispatch(const uint8_t *buf, uint64_t idx, uint64_t k) {
  uint64_t lo[g_nwin], hi[g_nwin]; int n = 0;
  for (int j = 0; j < g_nwin; j++) {
    lo[j] = g_win[j].start > idx ? g_win[j].start : idx;
    hi[j] = g_win[j].start + g_win[j].len < idx + k ? g_win[j].start + g_win[j].len : idx + k;
    n += lo[j] < hi[j];
  }
  if (!n) return;
  chunk_t *ch = malloc(sizeof *ch); ch->data = malloc(k * REC); ch->ref = n; memcpy(ch->data, buf, k * REC);
  pthread_mutex_lock(&g_mu);
  for (int j = 0; j < g_nwin; j++) {
    win_t *w = &g_win[j]; if (lo[j] >= hi[j]) continue;
    if (w->qt == w->qcap) { w->qcap = w->qcap ? 2 * w->qcap : 64; w->q = realloc(w->q, w->qcap * sizeof(item_t)); }
    w->q[w->qt++] = (item_t){ch, lo[j] - idx, hi[j] - idx};
  }
  pthread_cond_broadcast(&g_cv);
  pthread_mutex_unlock(&g_mu);
}

/* the only decompressor: stream the file once, up to record e, feeding windows (none -> just counts) */
static uint64_t produce(uint64_t e) {
  ZSTD_DCtx *d = ZSTD_createDCtx();
  ZSTD_DCtx_setParameter(d, ZSTD_d_windowLogMax, 31);
  uint8_t *buf = malloc(CHUNK);
  ZSTD_inBuffer in = {g_src, g_size, 0};
  uint64_t idx = 0; size_t rem = 0;
  double t_dec = 0;
  while (idx < e) {
    ZSTD_outBuffer o = {buf, CHUNK, rem};
    double t = now();
    do {
      size_t ip = in.pos, op = o.pos, r = ZSTD_decompressStream(d, &o, &in);
      if (ZSTD_isError(r)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(r)); exit(1); }
      if (in.pos == ip && o.pos == op) break;
    } while (o.pos < o.size);
    t_dec += now() - t;
    uint64_t k = o.pos / REC;
    if (!k) break;
    dispatch(buf, idx, k);
    rem = o.pos - k * REC; memmove(buf, buf + k * REC, rem); idx += k;
  }
  g_tdec = t_dec; thread_log("producer");
  pthread_mutex_lock(&g_mu); g_done = 1; pthread_cond_broadcast(&g_cv); pthread_mutex_unlock(&g_mu);
  free(buf); ZSTD_freeDCtx(d);
  return idx;
}

/* one consumer per window: ingest items as they arrive, then reduce */
static void *worker(void *arg) {
  win_t *w = arg;
  uint64_t slots = 1u << 16;
  w->T = tab_alloc(slots); w->mask = slots - 1; w->shift = 64 - __builtin_ctzll(slots);
  while (w->seen < w->len) {
    pthread_mutex_lock(&g_mu);
    while (w->qh == w->qt && !g_done) pthread_cond_wait(&g_cv, &g_mu);
    int empty = w->qh == w->qt; item_t it = empty ? (item_t){0} : w->q[w->qh++];
    pthread_mutex_unlock(&g_mu);
    if (empty) break;
    double t = now();
    ingest(w, it.ch->data + it.lo * REC, it.hi - it.lo);
    w->t_hash += now() - t;
    if (!__sync_sub_and_fetch(&it.ch->ref, 1)) { free(it.ch->data); free(it.ch); }
  }
  if (w->seen != w->len) { fprintf(stderr, "window start=%lu: short read %lu/%lu\n", w->start, w->seen, w->len); exit(1); }
  thread_log("worker/ingest"); post(w); munmap(w->T, (w->mask + 1) * sizeof(slot_t)); w->T = NULL;
  if (g_verbose) fprintf(stderr, "[win] start=%lu uniq=%lu  hash=%.2fs post=%.2fs\n", w->start, w->uniq, w->t_hash, w->t_post);
  return NULL;
}

/* ---------------------------------------------------------------- main */
static uint64_t rng_state;
static uint64_t rng(void) {  /* splitmix64 */
  uint64_t z = (rng_state += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull; z = (z ^ (z >> 27)) * 0x94D049BB133111EBull; return z ^ (z >> 31);
}
static int cmp_start_desc(const void *a, const void *b) {
  uint64_t x = ((const win_t *)a)->start, y = ((const win_t *)b)->start; return (x < y) - (x > y);
}

static void mean_ci(const double *v, int n, double *mean, double *ci) {
  static const double tq[] = {12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179,
                              2.160, 2.145, 2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064,
                              2.060, 2.056, 2.052, 2.048, 2.045, 2.042};
  double s = 0, ss = 0; int k = 0;
  for (int i = 0; i < n; i++) if (!isnan(v[i])) { s += v[i]; k++; }
  *mean = k ? s / k : NAN; *ci = NAN;
  if (k < 2) return;
  for (int i = 0; i < n; i++) if (!isnan(v[i])) ss += (v[i] - *mean) * (v[i] - *mean);
  *ci = (k - 1 <= 30 ? tq[k - 2] : 1.96) * sqrt(ss / (k - 1) / k);
}

int main(int argc, char **argv) {
  int nwin = 10, full_scan = 0; uint64_t L = 10000000; rng_state = 1; const char *csv = NULL; int c;
  while ((c = getopt(argc, argv, "w:L:s:o:vF")) != -1) switch (c) {
    case 'F': full_scan = 1; break;
    case 'w': nwin = atoi(optarg); break;
    case 'L': L = strtoull(optarg, 0, 10); break;
    case 's': rng_state = strtoull(optarg, 0, 10); break;
    case 'o': csv = optarg; break;
    case 'v': g_verbose = 1; break;
    default: fprintf(stderr, "usage: %s [-w windows] [-L requests] [-s seed] [-o csv] [-v] [-F] trace.zst\n", argv[0]); return 2; }
  if (optind >= argc) { fprintf(stderr, "no input\n"); return 2; }
  int fd = open(argv[optind], O_RDONLY); struct stat st;
  if (fd < 0 || fstat(fd, &st)) { perror("open"); return 1; }
  g_size = st.st_size; g_src = mmap(0, g_size, PROT_READ, MAP_SHARED, fd, 0);
  if (g_src == MAP_FAILED) { perror("mmap"); return 1; }

  double t0 = now();
  /* fast path: size in the first frame header, assuming a single-frame file (zstd CLI default);
   * -F walks every frame instead (~1-2 s on a 1 GB file) */
  unsigned long long bytes = full_scan ? ZSTD_findDecompressedSize(g_src, g_size) : ZSTD_getFrameContentSize(g_src, g_size);
  int known = bytes != ZSTD_CONTENTSIZE_UNKNOWN && bytes != ZSTD_CONTENTSIZE_ERROR;
  uint64_t N = known ? bytes / REC : produce(UINT64_MAX);
  g_done = 0;
  fprintf(stderr, "[count] N=%lu requests (%s) %.2fs\n", N, known ? (full_scan ? "all frames" : "first frame header") : "counted by decompressing", now() - t0);
  if (L > N) { L = N; nwin = 1; }

  g_nwin = nwin; g_win = calloc(nwin, sizeof(win_t));
  for (int i = 0; i < nwin; i++) { g_win[i].start = nwin == 1 ? 0 : rng() % (N - L + 1); g_win[i].len = L; }
  qsort(g_win, nwin, sizeof(win_t), cmp_start_desc);
  pthread_t th[nwin];
  double t1 = now(), emax = 0;
  for (int i = 0; i < nwin; i++) { pthread_create(&th[i], NULL, worker, &g_win[i]); emax = fmax(emax, g_win[i].start + g_win[i].len); }
  produce((uint64_t)emax);
  for (int i = 0; i < nwin; i++) pthread_join(th[i], NULL);
  double wall = now() - t1;
  if (g_verbose) fprintf(stderr, "[produce] decompress=%.2fs  analysis wall=%.2fs\n", g_tdec, wall);

  /* per-window csv */
  if (csv) {
    FILE *f = fopen(csv, "w");
    fprintf(f, "start,len,dur,uniq,zero_size_obj,footprint,size_change,rho");
    for (int rw = 0; rw < 2; rw++) for (int v = 0; v < NV; v++)
      fprintf(f, ",%c:%s:var_s,%c:%s:var_lam,%c:%s:cov,%c:%s:share_size,%c:%s:wfrac", "br"[rw], VNAME[v], "br"[rw], VNAME[v], "br"[rw], VNAME[v], "br"[rw], VNAME[v], "br"[rw], VNAME[v]);
    for (int k = 0; k < NCHE; k++) fprintf(f, ",che_T/dur@%d%%", (int)(CHE_FRAC[k] * 100 + 0.5));
    fprintf(f, ",gini"); for (int t = 0; t < NT; t++) fprintf(f, ",P_req<%s,P_byte<%s", TNAME[t], TNAME[t]);
    fputc('\n', f);
    for (int i = 0; i < nwin; i++) { win_t *w = &g_win[i];
      fprintf(f, "%lu,%lu,%.0f,%lu,%lu,%.0f,%.6f,%.4f", w->start, w->len, w->dur, w->uniq, w->zero_obj, w->foot, (double)w->chg / w->len, w->rho);
      for (int rw = 0; rw < 2; rw++) for (int v = 0; v < NV; v++)
        fprintf(f, ",%.4f,%.4f,%.4f,%.4f,%.4f", w->m[rw][v].vs, w->m[rw][v].vl, w->m[rw][v].cov, w->m[rw][v].sh, w->m[rw][v].bytes);
      for (int k = 0; k < NCHE; k++) fprintf(f, ",%.3e", w->che_u[k]);
      fprintf(f, ",%.4f", w->gini); for (int t = 0; t < NT; t++) fprintf(f, ",%.5f,%.6f", w->pr[t], w->pb[t]);
      fputc('\n', f); }
    fclose(f);
  }

  /* summary (mean +- 95% CI over windows) */
  double v[nwin], mu, ci;
#define COL(expr) for (int i = 0; i < nwin; i++) { win_t *w = &g_win[i]; v[i] = (expr); } mean_ci(v, nwin, &mu, &ci)
  printf("# %s  N=%lu  windows=%d x %lu req  seed-windows  wall=%.2fs\n\n", argv[optind], N, nwin, L, wall);
  for (int rw = 0; rw < 2; rw++) {
    printf("## %s\n\n| variant | Var(log2 s) | Var(log2 lam) | Cov | size share (mean +- 95%%CI) | freq share | weight frac |\n|---|---:|---:|---:|---|---:|---:|\n", WNAME[rw]);
    for (int k = 0; k < NV; k++) {
      printf("| %s |", VNAME[k]);
      COL(w->m[rw][k].vs); printf(" %.2f |", mu);
      COL(w->m[rw][k].vl); printf(" %.2f |", mu);
      COL(w->m[rw][k].cov); printf(" %.2f |", mu);
      COL(w->m[rw][k].sh); printf(" %.3f +- %.3f |", mu, ci);
      COL(1 - w->m[rw][k].sh); printf(" %.3f |", mu);
      COL(w->m[rw][k].bytes); printf(" %.3f |\n", mu);
    }
    printf("\n");
  }
  printf("## skew of requests vs object bytes below tau (Lorenz)\n\n| tau | P_req | P_byte | skew = P_req/P_byte (+-95%%CI) |\n|---|---:|---:|---|\n");
  for (int t = 0; t < NT; t++) {
    printf("| %s |", TNAME[t]);
    COL(w->pr[t]); printf(" %.3f |", mu);
    COL(w->pb[t]); printf(" %.4f |", mu);
    COL(w->pb[t] > 0 ? w->pr[t] / w->pb[t] : NAN); printf(" %.2f +- %.2f |\n", mu, ci);
  }
  COL(w->gini); printf("\nLorenz bulge (gini-like) G: %.3f +- %.3f\n", mu, ci);
  COL(w->rho); printf("\nSpearman rho(s, lambda): %.3f +- %.3f\n", mu, ci);
  COL((double)w->chg / w->len); printf("size-change request fraction: %.4f +- %.4f\n", mu, ci);
  COL((double)w->uniq); printf("unique objects / window: %.0f\n", mu);
  COL(w->foot); printf("footprint / window (bytes): %.3e\n", mu);
  COL(w->dur); printf("window duration (s): %.0f\n", mu);
  for (int k = 0; k < NCHE; k++) { COL(w->che_u[k]); printf("Che T/dur @%d%%: %.3e\n", (int)(CHE_FRAC[k] * 100 + 0.5), mu); }
  return 0;
}
