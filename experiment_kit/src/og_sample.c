/* og_sample.c : 物の ID のハッシュで 1/M の物だけを残した oracleGeneral を作る（SHARDS 型の空間サンプリング。
 * 縮小シミュレーションで、容量 C/M・Region R/M のキャッシュを回して設定を比べる用）。
 * 残した物の要求は全部残る（物ごとに全か無か）。size == UINT32_MAX の要求は落とす（drop と同じ）。
 * 出力は無圧縮の oracleGeneral（.bin）。next_access_vtime はそのまま（LRU 系は使わない）。
 * build: gcc -O2 -o og_sample og_sample.c -lzstd ; usage: og_sample in.zst out.bin M [seed] */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <zstd.h>
int main(int argc, char **argv) {
  if (argc < 4) { fprintf(stderr, "usage: og_sample in.zst out.bin M [seed]\n"); return 1; }
  uint64_t M = strtoull(argv[3], NULL, 10), seed = argc > 4 ? strtoull(argv[4], NULL, 10) : 1, kept = 0, all = 0;
  FILE *f = fopen(argv[1], "rb"), *fo = fopen(argv[2], "wb");
  if (!f || !fo || !M) { perror("open"); return 1; }
  ZSTD_DCtx *d = ZSTD_createDCtx(); size_t ib = ZSTD_DStreamInSize(), ob = (ZSTD_DStreamOutSize() / 24 + 1) * 24 * 4, nc = 0, r;
  uint8_t *in = malloc(ib), *out = malloc(ob), carry[24];
  while ((r = fread(in, 1, ib, f)) > 0) {
    ZSTD_inBuffer zi = {in, r, 0};
    while (zi.pos < zi.size) {
      ZSTD_outBuffer zo = {out, ob, nc}; if (nc) memcpy(out, carry, nc);
      if (ZSTD_isError(ZSTD_decompressStream(d, &zo, &zi))) return 2;
      size_t full = zo.pos / 24 * 24; nc = zo.pos - full; if (nc) memcpy(carry, out + full, nc);
      for (size_t o = 0; o < full; o += 24) {
        uint64_t id; uint32_t sz; memcpy(&id, out + o + 4, 8); memcpy(&sz, out + o + 12, 4); all++;
        uint64_t h = (id ^ seed) * 0x9E3779B97F4A7C15ull; h ^= h >> 29; h *= 0xBF58476D1CE4E5B9ull; h ^= h >> 32;
        if (sz == 0xFFFFFFFFu || h % M) continue;
        fwrite(out + o, 1, 24, fo); kept++;
      }
    }
  }
  fclose(fo);
  fprintf(stderr, "kept %llu of %llu requests (1/%llu of the objects)\n", (unsigned long long)kept, (unsigned long long)all, (unsigned long long)M);
  return 0;
}
