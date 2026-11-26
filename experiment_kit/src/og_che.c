/* og_che.c : 実トレースの物ごとの（大きさ, 要求回数）から、Che 近似（IRM）でサイズ分類の「上限」を予測する。
 *   付録 §N.5 の 1・2：物単位 LRU のミス率（分類で取り返せる OHR の上限）と、静的分割の利得、最適な容量の取り分。
 *   λ_i = 物 i の要求回数 ÷ 全要求数（時間の単位 = 要求 1 回）。Region の効果は入らない（物単位 LRU の分割）。
 *
 * usage: og_che [-C cap1,cap2,..] [-t tauset ...] [-w w1,w2,..] file.zst
 *   -C  容量 [B]（複数可）
 *   -t  分類の閾値の組（カンマ区切り、例 -t 65536 -t 65536,1048576）。大きさ s の分類 = s を超える τ の数（RegionFlash と同じ）。
 *       τ は 1/4 オクターブの格子に丸める（出力の taus_eff）
 *   -w  目的 J = BHR + w·OHR の w（既定 0.03,0.1,0.3,1）
 *   環境変数 SAT_DROP=<size>：その大きさの要求は無かったことにする（og_info と同じ。drop / delete 変種用）
 *   -a K  受け入れの篩（11-26）：トレース全体で K 回以上要求される物だけがキャッシュに入れる（K=2 = 一度きりの物を入れない。
 *         S3-FIFO 型の篩の 1 次の近似）。入れない物の要求はミスのまま数える。既定 1 = 篩なし
 *   -D  DRAM [B]（11-26）：SSD の前の物単位 LRU（全部の物を入れる、og_ind と同じ）。1 個なら全容量に、-C と同じ個数なら容量ごとに。
 *       SSD に届く率 r_i = λ_i e^{−λ_i T_D} で SSD の Che を解き、出力の ohr・bhr は DRAM のヒットを足した全体の値。既定 0 = DRAM なし（以前と同じ）
 * build: gcc -O3 -march=native -o og_che og_che.c -lzstd -lm
 * 出力: JSON 1 個（標準出力）
 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>

typedef unsigned long long ull;
typedef struct { uint64_t k; uint32_t s, c; } ent_t; /* k = id+1, 0 = 空き。s = 最初の大きさ、c = 要求回数 */
static ent_t *tab;
static size_t cap = 1 << 22, used;
static ent_t *find(uint64_t id) {
  uint64_t k = id + 1; size_t m = cap - 1, h = (k * 0x9E3779B97F4A7C15ull) >> 20 & m;
  while (tab[h].k && tab[h].k != k) h = (h + 1) & m;
  return &tab[h];
}
static void grow(void) {
  ent_t *old = tab; size_t oc = cap; cap *= 2; tab = calloc(cap, sizeof(ent_t));
  if (!tab) { fprintf(stderr, "og_che: メモリ不足\n"); exit(2); }
  for (size_t i = 0; i < oc; i++) if (old[i].k) *find(old[i].k - 1) = old[i];
  free(old);
}

/* ---- ビン：（大きさの 1/4 オクターブの格子 b, 要求回数の段 q）。q は 64 回までは回数そのもの、それより上は 1% 刻み ---- */
#define NB_S 200
#define QX 64
#define NQ 2400
static int sbucket(uint32_t s) { return s <= 1 ? 0 : 1 + (int)floor(4 * log2((double)s - 1)); } /* s ≤ 2^(m/4) ⟺ b ≤ m */
static int qkey(uint32_t c) { return c <= QX ? (int)c : QX + 1 + (int)floor(log((double)c / QX) / log(1.01)); }
typedef struct { int b; double n, c, s, lam, r; } bin_t; /* 物の数、回数の和、バイトの和、λ（平均回数 ÷ 全要求）、r = SSD に届く率（DRAM の後） */
static bin_t *B; static int NBIN; static double NREQ, LB; /* LB = Σ λ s（要求バイトの率） */
static int *CL; /* ビン → 分類（τ の組ごとに入れ直す） */
static double AMIN = 1; /* -a：物あたりの要求回数がこれ未満のビンはキャッシュに入れない */
#define ADM(i) (B[i].c >= AMIN * B[i].n - 1e-9)
static double DH, DBH, TD; /* DRAM のヒット（要求の割合・バイトの割合）と DRAM の T */
static void set_dram(double D) { /* DRAM の T_D を解いて、各ビンの SSD に届く率 r を入れる */
  double fp = 0; for (int i = 0; i < NBIN; i++) fp += B[i].s;
  TD = D <= 0 ? 0 : INFINITY;
  if (D > 0 && D < fp * (1 - 1e-12)) {
    double lo = -5, hi = log(1e4 * NREQ);
    for (int it = 0; it < 64; it++) {
      double m = (lo + hi) / 2, c = 0; for (int i = 0; i < NBIN; i++) c += B[i].s * (1 - exp(-B[i].lam * exp(m)));
      if (c < D) lo = m; else hi = m;
    }
    TD = exp((lo + hi) / 2);
  }
  DH = DBH = 0;
  for (int i = 0; i < NBIN; i++) {
    B[i].r = isinf(TD) ? 0 : B[i].lam * exp(-B[i].lam * TD);
    DH += B[i].n * (B[i].lam - B[i].r); DBH += B[i].s * (B[i].lam - B[i].r) / LB;
  }
}

/* 分類 k（k < 0 なら全部）の、T での 容量・ヒット・ヒットバイト と 限界値の分子・分母 */
static void che(double T, int k, double *C, double *H, double *BH, double wo, double wb, double *mnum, double *mden) {
  double c = 0, h = 0, bh = 0, a = 0, d = 0;
  for (int i = 0; i < NBIN; i++) {
    if ((k >= 0 && CL[i] != k) || !ADM(i)) continue;
    double l = B[i].r, e = exp(-l * T), p = 1 - e;
    c += B[i].s * p; h += B[i].n * l * p; bh += B[i].s * l * p;
    a += l * l * e * (wo * B[i].n + wb * B[i].s / LB); d += l * e * B[i].s;
  }
  if (C) *C = c;
  if (H) *H = h;
  if (BH) *BH = bh / LB;
  if (mnum) *mnum = a;
  if (mden) *mden = d;
}
static double occ(double T, int k) { double c; che(T, k, &c, 0, 0, 0, 0, 0, 0); return c; }
static double full(int k) { double s = 0; for (int i = 0; i < NBIN; i++) if ((k < 0 || CL[i] == k) && ADM(i)) s += B[i].s; return s; }
static double T_of_C(double C, int k) { /* 占有 = C となる T（log T の二分法） */
  if (C <= 0) return 0;
  if (C >= full(k) * (1 - 1e-12)) return INFINITY;
  double lo = -5, hi = log(1e4 * NREQ);
  for (int it = 0; it < 64; it++) { double m = (lo + hi) / 2; if (occ(exp(m), k) < C) lo = m; else hi = m; }
  return exp((lo + hi) / 2);
}
static double marg(double T, int k, double wo, double wb) {
  double a, d; che(T, k, 0, 0, 0, wo, wb, &a, &d); return d > 0 ? a / d : 0;
}
/* 静的分割（KKT）：全分類の限界値 = μ。μ を二分し、分類ごとに marg(T_k) = μ を解く（分類の中で単調でなければ近似） */
static void part(double C, int K, double wo, double wb, double *Tk, double *Ck) {
  double lo = -80, hi = 5;
  for (int it = 0; it < 60; it++) {
    double m = (lo + hi) / 2, mu = exp(m), tot = 0;
    for (int k = 0; k < K; k++) {
      double tl = -5, th = log(1e4 * NREQ);
      if (marg(0, k, wo, wb) <= mu) { Tk[k] = 0; Ck[k] = 0; continue; }
      if (marg(exp(th), k, wo, wb) > mu) { Tk[k] = INFINITY; Ck[k] = full(k); tot += Ck[k]; continue; }
      for (int j = 0; j < 50; j++) { double x = (tl + th) / 2; if (marg(exp(x), k, wo, wb) > mu) tl = x; else th = x; }
      Tk[k] = exp((tl + th) / 2); Ck[k] = occ(Tk[k], k); tot += Ck[k];
    }
    if (tot > C) lo = m; else hi = m;
  }
}
static void eval_alloc(int K, const double *Tk, double *ohr, double *bhr) {
  *ohr = *bhr = 0;
  for (int k = 0; k < K; k++) {
    double h, bh;
    if (isinf(Tk[k])) { /* 全部在籍 */
      h = bh = 0; for (int i = 0; i < NBIN; i++) if (CL[i] == k && ADM(i)) h += B[i].n * B[i].r, bh += B[i].s * B[i].r / LB;
    } else che(Tk[k], k, 0, &h, &bh, 0, 0, 0, 0);
    *ohr += h; *bhr += bh;
  }
  *ohr += DH; *bhr += DBH;
}
static int cmp_d(const void *a, const void *b) { double x = *(const double *)a, y = *(const double *)b; return (x < y) - (x > y); }
static void knap(double C, int byte, double *ohr, double *bhr) { /* λ/s（OHR）または λ（BHR）の大きい順に詰める（IRM の上限） */
  double *key = malloc(sizeof(double) * 2 * NBIN), left = C;
  for (int i = 0; i < NBIN; i++) key[2 * i] = !ADM(i) ? -1 : byte ? B[i].r : B[i].r * B[i].n / (B[i].s > 0 ? B[i].s : 1e-9), key[2 * i + 1] = i;
  qsort(key, NBIN, 2 * sizeof(double), cmp_d);
  *ohr = *bhr = 0;
  for (int j = 0; j < NBIN && left > 0; j++) {
    if (key[2 * j] < 0) break;
    bin_t *q = &B[(int)key[2 * j + 1]]; double f = q->s > 0 ? fmin(1, left / q->s) : 1;
    *ohr += f * q->n * q->r; *bhr += f * q->s * q->r / LB; left -= f * q->s;
  }
  free(key);
  *ohr += DH; *bhr += DBH;
}

/* Zipf の傾き：順位 r と回数 c の両対数の最小二乗。r = 10 から「2 回以上」の最後の順位まで、1 桁 20 点に間引く */
static void zipf(int k, double *alpha, double *r_hi, double *r2) {
  static double hc[NQ]; /* 回数の段ごとの物の数 */
  memset(hc, 0, sizeof hc);
  double cq[NQ]; for (int q = 0; q < NQ; q++) cq[q] = 0;
  for (int i = 0; i < NBIN; i++) if (k < 0 || CL[i] == k) {
    int q = qkey((uint32_t)fmax(1, round(B[i].c / B[i].n)));
    hc[q] += B[i].n; cq[q] += B[i].c;
  }
  double xs[2000], ys[2000]; int np = 0; double rank = 0, rmax2 = 0;
  for (int q = NQ - 1; q >= 2; q--) if (hc[q] > 0) { rank += hc[q]; rmax2 = rank; }
  *r_hi = rmax2; *alpha = NAN; *r2 = NAN;
  if (rmax2 < 100) return;
  rank = 0;
  double nxt = 10;
  for (int q = NQ - 1; q >= 2 && np < 2000; q--) {
    if (hc[q] <= 0) continue;
    double r0 = rank; rank += hc[q];
    double c = cq[q] / hc[q];
    while (nxt <= rank && nxt <= rmax2) { if (nxt > r0) { xs[np] = log(nxt); ys[np] = log(c); np++; } nxt *= pow(10, 0.05); }
  }
  if (np < 5) return;
  double mx = 0, my = 0, sxx = 0, sxy = 0, syy = 0;
  for (int i = 0; i < np; i++) mx += xs[i], my += ys[i];
  mx /= np; my /= np;
  for (int i = 0; i < np; i++) sxx += (xs[i] - mx) * (xs[i] - mx), sxy += (xs[i] - mx) * (ys[i] - my), syy += (ys[i] - my) * (ys[i] - my);
  *alpha = -sxy / sxx; *r2 = sxy * sxy / (sxx * syy);
}

static const char *num(double x) { /* JSON の数（NaN・∞ は null） */
  static char b[8][32]; static int i; i = (i + 1) % 8;
  if (isnan(x) || isinf(x)) return "null";
  snprintf(b[i], 32, "%.6g", x); return b[i];
}

int main(int argc, char **argv) {
  double caps[16], drs[16] = {0}; int ncap = 0, ndr = 0; char *tset[16]; int nt = 0; double ws[8] = {0.03, 0.1, 0.3, 1}; int nw = 4; const char *path = 0;
  for (int a = 1; a < argc; a++) {
    if (!strcmp(argv[a], "-C") && a + 1 < argc) { for (char *t = strtok(argv[++a], ","); t && ncap < 16; t = strtok(0, ",")) caps[ncap++] = atof(t); }
    else if (!strcmp(argv[a], "-t") && a + 1 < argc) { if (nt < 16) tset[nt++] = argv[++a]; else a++; }
    else if (!strcmp(argv[a], "-a") && a + 1 < argc) AMIN = atof(argv[++a]);
    else if (!strcmp(argv[a], "-D") && a + 1 < argc) { for (char *t = strtok(argv[++a], ","); t && ndr < 16; t = strtok(0, ",")) drs[ndr++] = atof(t); }
    else if (!strcmp(argv[a], "-w") && a + 1 < argc) { nw = 0; for (char *t = strtok(argv[++a], ","); t && nw < 8; t = strtok(0, ",")) ws[nw++] = atof(t); }
    else path = argv[a];
  }
  if (!path || !ncap || (ndr > 1 && ndr != ncap)) { fprintf(stderr, "usage: og_che -C cap[,cap..] [-D dram[,dram..]] [-t tau[,tau..]]... [-w w,..] [-a K] file.zst（-D は 1 個か -C と同じ個数）\n"); return 1; }
  FILE *f = fopen(path, "rb"); if (!f) { perror(path); return 1; }
  tab = calloc(cap, sizeof(ent_t));
  ZSTD_DCtx *d = ZSTD_createDCtx();
  size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4, nc = 0, r;
  uint8_t *in = malloc(ib), *out = malloc(ob), carry[24];
  ull n = 0, dropped = 0;
  const char *dv = getenv("SAT_DROP"); uint64_t drop = dv ? strtoull(dv, 0, 10) : 0;
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc};
      if (nc) memcpy(out, carry, nc);
      size_t rc = ZSTD_decompressStream(d, &zo, &zi);
      if (ZSTD_isError(rc)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc)); return 2; }
      size_t fl = zo.pos / 24 * 24; nc = zo.pos - fl; if (nc) memcpy(carry, out + fl, nc);
      for (size_t o = 0; o < fl; o += 24) {
        uint32_t sz; uint64_t id;
        memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4);
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
  if (!n) { fprintf(stderr, "og_che: 要求が 0\n"); return 1; }
  NREQ = (double)n;
  /* ビンに集める */
  double *gn = calloc((size_t)NB_S * NQ, sizeof(double)), *gc = calloc((size_t)NB_S * NQ, sizeof(double)), *gs = calloc((size_t)NB_S * NQ, sizeof(double));
  double fp = 0, one = 0;
  for (size_t i = 0; i < cap; i++) if (tab[i].k) {
    int b = sbucket(tab[i].s), q = qkey(tab[i].c); if (b >= NB_S) b = NB_S - 1; if (q >= NQ) q = NQ - 1;
    size_t j = (size_t)b * NQ + q; gn[j] += 1; gc[j] += tab[i].c; gs[j] += tab[i].s; fp += tab[i].s; one += tab[i].c == 1;
  }
  free(tab);
  B = malloc(sizeof(bin_t) * 200000); NBIN = 0; LB = 0;
  for (int b = 0; b < NB_S; b++) for (int q = 0; q < NQ; q++) {
    size_t j = (size_t)b * NQ + q; if (!gn[j]) continue;
    B[NBIN] = (bin_t){b, gn[j], gc[j], gs[j], gc[j] / gn[j] / NREQ, gc[j] / gn[j] / NREQ};
    LB += B[NBIN].lam * B[NBIN].s; NBIN++;
  }
  CL = calloc(NBIN, sizeof(int));
  printf("{\"admit_min\":%g,\"n\":%llu,\"dropped\":%llu,\"uniq\":%zu,\"footprint\":%.0f,\"one_hit_frac\":%.6f,\"compulsory_miss\":%.6f,\"bins\":%d,\"w\":[",
         AMIN, n, dropped, used, fp, one / used, used / NREQ, NBIN);
  for (int i = 0; i < nw; i++) printf("%s%g", i ? "," : "", ws[i]);
  {
    double a, rh, r2; zipf(-1, &a, &rh, &r2);
    printf("],\"zipf_all\":{\"alpha\":%s,\"r_hi\":%.0f,\"r2\":%s},\"capacities\":[", num(a), rh, num(r2));
  }
  for (int ci = 0; ci < ncap; ci++) {
    double Dr = ndr > 1 ? drs[ci] : drs[0]; set_dram(Dr);
    double C = caps[ci], T = T_of_C(C, -1), H, BH;
    che(T, -1, 0, &H, &BH, 0, 0, 0, 0); H += DH; BH += DBH;
    double ko, kb, ko2, kb2; knap(C, 0, &ko, &kb); knap(C, 1, &ko2, &kb2);
    printf("%s{\"C\":%.0f,\"C_over_fp\":%.5f,", ci ? "," : "", C, C / fp);
    if (Dr > 0) printf("\"D\":%.0f,\"dram\":{\"T\":%s,\"ohr\":%.5f,\"bhr\":%.5f},", Dr, num(TD), DH, DBH);
    printf("\"lru\":{\"T\":%s,\"ohr\":%.5f,\"bhr\":%.5f},\"knap_ohr\":{\"ohr\":%.5f,\"bhr\":%.5f},\"knap_bhr\":{\"ohr\":%.5f,\"bhr\":%.5f},\"splits\":[",
           num(T), H, BH, ko, kb, ko2, kb2);
    for (int ti = 0; ti < nt + 1; ti++) {
      /* ti == nt は「分けない」（1 分類）。τ を 1/4 オクターブの格子 m に丸める（s ≤ 2^(m/4) ⟺ b ≤ m） */
      int m4[16], K = 1; char buf[256];
      if (ti < nt) {
        strncpy(buf, tset[ti], 255); buf[255] = 0;
        for (char *t = strtok(buf, ","); t && K < 16; t = strtok(0, ",")) m4[K - 1] = (int)lround(4 * log2(atof(t))), K++;
        for (int i = 1; i < K - 1; i++) for (int j = i; j > 0 && m4[j - 1] > m4[j]; j--) { int x = m4[j]; m4[j] = m4[j - 1]; m4[j - 1] = x; }
      }
      for (int i = 0; i < NBIN; i++) { int c = 0; while (c < K - 1 && B[i].b > m4[c]) c++; CL[i] = c; }
      printf("%s{\"taus_eff\":[", ti ? "," : "");
      for (int i = 0; i < K - 1; i++) printf("%s%.0f", i ? "," : "", pow(2, m4[i] / 4.0));
      printf("],\"classes\":[");
      for (int k = 0; k < K; k++) {
        double no = 0, rq = 0, by = 0, rb = 0;
        for (int i = 0; i < NBIN; i++) if (CL[i] == k) no += B[i].n, rq += B[i].c, by += B[i].s, rb += B[i].lam * B[i].s;
        double a, rh, r2; zipf(k, &a, &rh, &r2);
        double ck, mo = marg(T, k, 1, 0), mb = marg(T, k, 0, 1); ck = occ(T, k);
        printf("%s{\"objects\":%.0f,\"req_share\":%.5f,\"byte_share\":%.5f,\"footprint\":%.0f,\"mean_size\":%.1f,\"alpha\":%s,\"alpha_r_hi\":%.0f,\"alpha_r2\":%s,"
               "\"lru_share\":%.5f,\"lru_marg_ohr\":%s,\"lru_marg_bhr\":%s}",
               k ? "," : "", no, rq / NREQ, rb / LB, by, no ? by / no : 0, num(a), rh, num(r2), ck / C, num(mo), num(mb));
      }
      printf("],\"part\":{");
      /* 目的：ohr（OHR 最適）、bhr（BHR 最適）、J_w = BHR + w·OHR */
      int nobj = 2 + nw;
      for (int oi = 0; oi < nobj; oi++) {
        double wo = oi == 0 ? 1 : oi == 1 ? 0 : ws[oi - 2], wb = oi == 0 ? 0 : 1, Tk[16], Ck[16], o, b;
        if (K == 1) { Tk[0] = T; Ck[0] = C; } else part(C, K, wo, wb, Tk, Ck);
        eval_alloc(K, Tk, &o, &b);
        if (oi < 2) printf("%s\"%s\":{", oi ? "," : "", oi ? "bhr" : "ohr"); else printf(",\"J%g\":{", ws[oi - 2]);
        printf("\"ohr\":%.5f,\"bhr\":%.5f,\"share\":[", o, b);
        double tot = 0; for (int k = 0; k < K; k++) tot += Ck[k];
        for (int k = 0; k < K; k++) printf("%s%.5f", k ? "," : "", tot > 0 ? Ck[k] / tot : 0);
        printf("]}");
      }
      printf("}}");
    }
    printf("]}");
  }
  printf("]}\n");
  return 0;
}
