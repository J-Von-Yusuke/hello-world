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
 * 記録は <uint32 t, uint64 id, uint32 size, int64 next> の 24 B（packed）。
 *
 * メモリ（2026-11-19）：要求ごとに物の番号と次のアクセスを 4 B ずつだけ持ち、記録は書くときに組み立てる
 * （1 要求 8 B。前の版は 24 + 1 B で、2 億要求に 5 GB 要り、別 PC で確保に失敗した）。出力のバイト列は前の版と同じ。
 * 要求数と物の番号は 2^32 − 1 未満に限る。確保・書き込みに失敗したら、要る量を書いて止まる。 */
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define KMAX 8
#define NONE UINT32_MAX
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
static void *xmalloc(size_t b, const char *what) {
  void *p = malloc(b);
  if (!p) { fprintf(stderr, "synth_gen: メモリが足りない（%s に %.2f GB）\n", what, b / 1e9); exit(2); }
  return p;
}

static int K; static int64_t sz[KMAX]; static uint64_t off[KMAX + 1];
static int cls_of(uint32_t id) { int k = 0; while (k < K - 1 && id > off[k + 1]) k++; return k; }

/* 要求 i（idx[j] が i、j = 0..m−1）を、次のアクセス nx[j]（j の番号。NONE = 無し）とともに path へ書く */
static void write_recs(const char *path, int64_t m, const uint32_t *ids, const uint32_t *idx, const uint32_t *nx) {
  FILE *f = fopen(path, "wb");
  if (!f) { fprintf(stderr, "synth_gen: %s を開けない: %s\n", path, strerror(errno)); exit(1); }
  enum { B = 1 << 16 }; static rec_t buf[B];
  for (int64_t j = 0; j < m; j += B) {
    int64_t c = m - j < B ? m - j : B;
    for (int64_t q = 0; q < c; q++) {
      uint32_t i = idx ? idx[j + q] : (uint32_t)(j + q), id = ids[i];
      buf[q] = (rec_t){i, id, (uint32_t)sz[cls_of(id)], nx[j + q] == NONE ? -1 : (int64_t)nx[j + q]};
    }
    if (fwrite(buf, sizeof(rec_t), c, f) != (size_t)c) {
      fprintf(stderr, "synth_gen: %s に書けない（%.2f GB 要る）: %s\n", path, m * (double)sizeof(rec_t) / 1e9, strerror(errno)); exit(1);
    }
  }
  if (fclose(f)) { fprintf(stderr, "synth_gen: %s を閉じられない: %s\n", path, strerror(errno)); exit(1); }
}

int main(int argc, char **argv) {
  if (argc < 5) { fprintf(stderr, "usage: synth_gen out n_req seed size:nobj:share:alpha ... [-S]\n"); return 1; }
  int64_t n = atoll(argv[2]); rs = strtoull(argv[3], NULL, 10);
  int split = 0;
  int64_t per[KMAX]; uint64_t no[KMAX]; double sh[KMAX], al[KMAX], *cdf[KMAX], tot = 0;
  for (int a = 4; a < argc; a++) {
    if (!strcmp(argv[a], "-S")) { split = 1; continue; }
    char *p = argv[a], *t;
    sz[K] = parse_size(strtok(p, ":")); no[K] = strtoull(strtok(NULL, ":"), NULL, 10);
    sh[K] = atof(strtok(NULL, ":")); al[K] = atof(strtok(NULL, ":"));
    per[K] = (t = strtok(NULL, ":")) ? atoll(t) : 0;
    tot += sh[K]; off[K + 1] = off[K] + no[K] + (per[K] ? n / per[K] + 1 : 0); K++;  // 新しい物の分の番号も確保
  }
  if (n <= 0 || n >= NONE || off[K] >= NONE) { fprintf(stderr, "synth_gen: 要求数と物の番号は 2^32−1 未満に限る\n"); return 1; }
  for (int k = 0; k < K; k++) {  // クラス内 Zipf の累積分布
    cdf[k] = xmalloc(no[k] * sizeof(double), "Zipf の累積分布");
    double c = 0;
    for (uint64_t r = 0; r < no[k]; r++) cdf[k][r] = (c += 1.0 / __builtin_pow(r + 1.0, al[k]));
    for (uint64_t r = 0; r < no[k]; r++) cdf[k][r] /= c;
  }
  uint32_t *ids = xmalloc(n * sizeof(uint32_t), "物の番号の列"), *nx = xmalloc(n * sizeof(uint32_t), "次のアクセスの列");
  for (int64_t i = 0; i < n; i++) {
    double u = urand() * tot, c = 0; int k = 0;
    while (k < K - 1 && u >= (c += sh[k])) k++;
    double v = urand(); uint64_t lo = 0, hi = no[k] - 1;
    while (lo < hi) { uint64_t m = (lo + hi) / 2; if (cdf[k][m] < v) lo = m + 1; else hi = m; }
    uint64_t id = per[k] ? no[k] + (uint64_t)(i / per[k]) - lo : lo + 1;  // 老化なしは順位 = 番号
    ids[i] = (uint32_t)(off[k] + id);
  }
  uint32_t *last = xmalloc((off[K] + 1) * sizeof(uint32_t), "物ごとの最後のアクセス");
  for (uint64_t x = 0; x <= off[K]; x++) last[x] = NONE;
  for (int64_t i = n - 1; i >= 0; i--) nx[i] = last[ids[i]], last[ids[i]] = (uint32_t)i;
  write_recs(argv[1], n, ids, NULL, nx);
  if (split) {  // クラス k の要求だけを、時刻を保ったまま書く（次のアクセスはその列の中の番号）
    uint32_t *idx = xmalloc(n * sizeof(uint32_t), "クラスの要求の位置");
    for (int k = 0; k < K; k++) {
      int64_t m = 0;
      for (int64_t i = 0; i < n; i++) if (cls_of(ids[i]) == k) idx[m++] = (uint32_t)i;
      for (uint64_t x = 0; x <= off[K]; x++) last[x] = NONE;
      for (int64_t j = m - 1; j >= 0; j--) { uint32_t id = ids[idx[j]]; nx[j] = last[id]; last[id] = (uint32_t)j; }
      char path[4096]; snprintf(path, sizeof path, "%s.c%d", argv[1], k);
      write_recs(path, m, ids, idx, nx);
    }
  }
  double fp = 0;
  for (int k = 0; k < K; k++) fp += (double)sz[k] * no[k];
  printf("n_req %lld  classes %d  ids %llu  footprint(at any time) %.0f B\n", (long long)n, K, (unsigned long long)off[K], fp);
  for (int k = 0; k < K; k++) printf("class %d id offset %llu\n", k, (unsigned long long)off[k]);
  return 0;
}
