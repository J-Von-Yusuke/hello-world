/* synth_gen.c — 少数のサイズだけを持つ IRM（独立参照）合成トレースを oracleGeneral で書く。
 *
 *   synth_gen <out.bin> <n_req> <seed> <size:nobj:share:alpha[:period]> [...] [-S]
 *     size  物の大きさ（K/M 可）   nobj  そのクラスの物の数
 *     share 要求に占める割合（和で正規化）   alpha  クラス内の Zipf 指数
 *     period 人気の老化：period 要求ごとに新しい物が 1 位に入り、既存の物は 1 位ずつ下がる（0・省略で不変）。
 *            時刻 i で順位 r の物の番号は (nobj + ⌊i/period⌋ − r + 1)。nobj × period 要求で一巡する。
 *     -S    クラス k の要求だけを <out.bin>.c<k> にも書く（静的分割 LRU の評価用）
 *
 * 要求ごとにクラスを share で引き、クラス内で Zipf 順位を引く。
 * 時刻は 1 要求 = 1 秒（--warmup-sec と Region の年齢の計測用）。next_access は要求番号（無ければ -1）。
 * 記録は <uint32 t, uint64 id, uint32 size, int64 next> の 24 B（packed）。 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define KMAX 8
#pragma pack(push, 1)
typedef struct { uint32_t t; uint64_t id; uint32_t sz; int64_t nxt; } rec_t;
#pragma pack(pop)

static uint64_t rs;  // splitmix64
static double urand(void) {
  uint64_t z = (rs += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull, z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return ((z ^ (z >> 31)) >> 11) * 0x1.0p-53;
}
static int64_t parse_size(const char *s) {
  char *e; double v = strtod(s, &e);
  return (int64_t)(v * (*e == 'K' ? 1 << 10 : *e == 'M' ? 1 << 20 : *e == 'G' ? 1 << 30 : 1));
}
static void write_rev(const char *path, rec_t *r, int64_t n, int64_t *last, uint64_t nobj) {
  for (uint64_t i = 0; i < nobj; i++) last[i] = -1;
  for (int64_t i = n - 1; i >= 0; i--) r[i].nxt = last[r[i].id - 1], last[r[i].id - 1] = i;
  FILE *f = fopen(path, "wb");
  if (!f || fwrite(r, sizeof(rec_t), n, f) != (size_t)n) { perror(path); exit(1); }
  fclose(f);
}

int main(int argc, char **argv) {
  if (argc < 5) { fprintf(stderr, "usage: synth_gen out n_req seed size:nobj:share:alpha ... [-S]\n"); return 1; }
  int64_t n = atoll(argv[2]); rs = strtoull(argv[3], NULL, 10);
  int K = 0, split = 0;
  int64_t sz[KMAX], per[KMAX]; uint64_t no[KMAX], off[KMAX + 1] = {0}; double sh[KMAX], al[KMAX], *cdf[KMAX], tot = 0;
  for (int a = 4; a < argc; a++) {
    if (!strcmp(argv[a], "-S")) { split = 1; continue; }
    char *p = argv[a], *t;
    sz[K] = parse_size(strtok(p, ":")); no[K] = strtoull(strtok(NULL, ":"), NULL, 10);
    sh[K] = atof(strtok(NULL, ":")); al[K] = atof(strtok(NULL, ":"));
    per[K] = (t = strtok(NULL, ":")) ? atoll(t) : 0;
    tot += sh[K]; off[K + 1] = off[K] + no[K] + (per[K] ? n / per[K] + 1 : 0); K++;  // 新しい物の分の番号も確保
  }
  for (int k = 0; k < K; k++) {  // クラス内 Zipf の累積分布
    cdf[k] = malloc(no[k] * sizeof(double));
    double c = 0;
    for (uint64_t r = 0; r < no[k]; r++) cdf[k][r] = (c += 1.0 / __builtin_pow(r + 1.0, al[k]));
    for (uint64_t r = 0; r < no[k]; r++) cdf[k][r] /= c;
  }
  rec_t *r = malloc(n * sizeof(rec_t));
  uint8_t *cls = malloc(n);
  for (int64_t i = 0; i < n; i++) {
    double u = urand() * tot, c = 0; int k = 0;
    while (k < K - 1 && u >= (c += sh[k])) k++;
    double v = urand(); uint64_t lo = 0, hi = no[k] - 1;
    while (lo < hi) { uint64_t m = (lo + hi) / 2; if (cdf[k][m] < v) lo = m + 1; else hi = m; }
    uint64_t id = per[k] ? no[k] + (uint64_t)(i / per[k]) - lo : lo + 1;  // 老化なしは順位 = 番号
    r[i] = (rec_t){(uint32_t)i, off[k] + id, (uint32_t)sz[k], -1};
    cls[i] = (uint8_t)k;
  }
  int64_t *last = malloc(off[K] * sizeof(int64_t));
  write_rev(argv[1], r, n, last, off[K]);
  if (split) {  // クラス k の要求だけを、時刻を保ったまま書く
    rec_t *q = malloc(n * sizeof(rec_t));
    for (int k = 0; k < K; k++) {
      int64_t m = 0;
      for (int64_t i = 0; i < n; i++) if (cls[i] == k) q[m++] = r[i];
      char path[4096]; snprintf(path, sizeof path, "%s.c%d", argv[1], k);
      write_rev(path, q, m, last, off[K]);
    }
  }
  double fp = 0;
  for (int k = 0; k < K; k++) fp += (double)sz[k] * no[k];
  printf("n_req %lld  classes %d  ids %llu  footprint(at any time) %.0f B\n", (long long)n, K, (unsigned long long)off[K], fp);
  for (int k = 0; k < K; k++) printf("class %d id offset %llu\n", k, (unsigned long long)off[k]);
  return 0;
}
