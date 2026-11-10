/* og_pack.c : 生の oracleGeneral（24 B 記録）を zstd（単一フレーム）にする。traceConv の出力用。
 * build: gcc -O2 -o og_pack og_pack.c -lzstd ; usage: og_pack in.raw out.zst [level=3]
 */
#include <stdio.h>
#include <stdlib.h>
#include <zstd.h>
int main(int argc, char **argv) {
  if (argc < 3) return 1;
  FILE *fi = fopen(argv[1], "rb"), *fo = fopen(argv[2], "wb");
  if (!fi || !fo) { perror("open"); return 1; }
  ZSTD_CCtx *c = ZSTD_createCCtx();
  ZSTD_CCtx_setParameter(c, ZSTD_c_compressionLevel, argc > 3 ? atoi(argv[3]) : 3);
  size_t ib = ZSTD_CStreamInSize(), ob = ZSTD_CStreamOutSize(), r;
  char *in = malloc(ib), *out = malloc(ob);
  for (int last = 0; !last;) {
    r = fread(in, 1, ib, fi); last = r < ib;
    ZSTD_inBuffer zi = {in, r, 0};
    for (int fin = 0; !fin;) {
      ZSTD_outBuffer zo = {out, ob, 0};
      size_t rem = ZSTD_compressStream2(c, &zo, &zi, last ? ZSTD_e_end : ZSTD_e_continue);
      if (ZSTD_isError(rem)) { fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rem)); return 2; }
      fwrite(out, 1, zo.pos, fo);
      fin = last ? rem == 0 : zi.pos == zi.size;
    }
  }
  return fclose(fo) != 0;
}
