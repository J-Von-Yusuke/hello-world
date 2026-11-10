/* synth_model.c — synth_gen と同じ指定の IRM 負荷に対する Che 型の予測。
 *
 *   synth_model [-R region] [-f f1,f2,..] [-g 0,0,1] [-S samples] size:nobj:share:alpha ...
 *     -f  容量 = 足跡 × f（既定 0.05,0.1,0.2,0.3）  -R  Region の大きさ（既定 容量/256）
 *     -g  サイズのクラス → 分類の対応を 1 つ足す（既定で「分けない」と「クラスごと」を出す）
 *
 * 時間の単位は要求 1 回。物 i の要求率 λ_i = share_k × r^-α / H_k（Σλ = 1）。
 * 出力（容量ごと）:
 *   lru         物単位 LRU を 1 つで共有（Che）  h = 1 − e^{−λT}、Σ s h = C
 *   opt         静的ナップサック（λ/s の順に詰める。IRM の上限）
 *   rfifo       Region FIFO：物は滞在 T 一定の FIFO、h = λT/(1+λT)。分類しても同じ
 *   各分類 g について
 *   part[g]     物単位 LRU を分類で静的に分割、OHR 最適（限界ヒット/バイトをそろえる）
 *   partB[g]    同じく BHR 最適
 *   rlru[g]     Region LRU、分類ごとの Region
 * Region LRU：Region は最後の touch（封じた時・SSD ヒット）から T 経つと追い出される（Che）。
 * touch の率 Λ は中の物の要求率の和。寿命 L = (e^{ΛT}−1)/Λ = ∫_0^T e^{Λt}dt。物 i が入った
 * Region の期待寿命 L_i = ∫_0^T e^{λ_i t} E[Π_j e^{λ_j t}] dt（同居の物 j は同じ分類の挿入の流れ
 * から独立に引き、R に収まらなくなったら封じる）。物から見ると滞在 L_i の FIFO なので
 * h_i = λ_i L_i / (1 + λ_i L_i)。挿入の流れ（ミス率 λ/(1+λL)）が L に依るので不動点で解く。 */
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define KMAX 8
#define G 160  // t の格子
typedef struct { int k; double c, lam, s; } bin_t;
static bin_t *B; static int NB, K, GR[KMAX];  // GR: クラス → 分類
static double SZ[KMAX], FP, TB;               // TB = Σ λ s

static int64_t parse_size(const char *s) {
  char *e; double v = strtod(s, &e);
  return (int64_t)(v * (*e == 'K' ? 1 << 10 : *e == 'M' ? 1 << 20 : *e == 'G' ? 1 << 30 : 1));
}
static double lse(double a, double b) {
  if (a == -INFINITY) return b;
  if (b == -INFINITY) return a;
  return a > b ? a + log1p(exp(b - a)) : b + log1p(exp(a - b));
}
static double lexpm1d(double d) {  // log((e^d − 1)/d)
  if (fabs(d) < 1e-8) return d / 2;
  if (d > 30) return d - log(d);
  return d > 0 ? log(expm1(d) / d) : log(expm1(d) / d);
}
static double sigm(double x) { return x > 0 ? 1 / (1 + exp(-x)) : exp(x) / (1 + exp(x)); }
#define IN(i, g) ((g) < 0 || GR[B[i].k] == (g))

/* ---- 物単位 LRU（Che）。分類 g（g<0 なら全部）。mg = 限界 Σ w λ² e / Σ s λ e（w = 1 か s） ---- */
static void che(double T, int g, double *H, double *BH, double *C, double *mg, int byte) {
  double h = 0, bh = 0, c = 0, a = 0, b = 0;
  for (int i = 0; i < NB; i++) {
    if (!IN(i, g)) continue;
    double e = exp(-B[i].lam * T), x = B[i].c * B[i].lam;
    h += x * (1 - e), bh += x * B[i].s * (1 - e), c += B[i].c * B[i].s * (1 - e);
    a += x * B[i].lam * e * (byte ? B[i].s : 1), b += x * B[i].s * e;
  }
  if (H) *H = h;
  if (BH) *BH = bh / TB;
  if (C) *C = c;
  if (mg) *mg = a / b;
}
static double bisect(double lo, double hi, int g, double target, int what, int byte) {
  // what 0: 占有 C(T) = target（増加）、1: 限界 mg(T) = target（減少）
  for (int it = 0; it < 70; it++) {
    double m = (lo + hi) / 2, c, mg;
    che(exp(m), g, 0, 0, &c, &mg, byte);
    if (what ? mg > target : c < target) lo = m; else hi = m;
  }
  return exp((lo + hi) / 2);
}
static void part(double C, int byte, int ng, double *Cg, double *ohr, double *bhr) {
  double lo = -80, hi = 10, T[KMAX];
  for (int it = 0; it < 70; it++) {  // Σ_g C_g(T_g(μ)) = C
    double m = (lo + hi) / 2, c = 0, cg;
    for (int g = 0; g < ng; g++) T[g] = bisect(-10, 40, g, exp(m), 1, byte), che(T[g], g, 0, 0, &cg, 0, 0), c += cg;
    if (c > C) lo = m; else hi = m;
  }
  *ohr = *bhr = 0;
  for (int g = 0; g < ng; g++) { double a, b; che(T[g], g, &a, &b, &Cg[g], 0, 0); *ohr += a, *bhr += b; }
}

/* ---- Region（FIFO / LRU）。pres[i] = λL/(1+λL)、戻り値は占有バイト ---- */
static double *logL, *pres, *lm, R, *U;
static int S = 400, MF;
static double TFIX;  // 診断 TFIX=<T>：T を与えて解く（占有は C に合わせない）
static double CV;    // -c：追い出し年齢 T の変動係数（0 なら Che の T 一定、auto（<0）なら式から）
static double CVLAST;  // auto で最後に使った CV（出力用）
static int NOU;        // 診断 NOU=1：封じるまでの待ちを入れない（旧モデル）
/* 立ち上がりの隔離（-H <観測期間>）：空のキャッシュを埋める間は全部がミスなので、初回の要求の順に物が
 * Region に詰まる。最初の Region には熱い物が集まり、寿命 (e^{ΛT}−1)/Λ が観測期間 H を超えれば事実上不死になる。
 * その物は二度とミスせず（在籍率 1）、残りの物と容量で定常モデルを解く。pin[i] はビン i の不死の物の数。 */
static double HOR, *pin, PINB[KMAX];
static int NOBJ, NREG, NPIN, *obin, *oreg;
static double *rlam, *rfill;
#define CE(i) (B[i].c - pin[i])  // 定常モデルで扱う物の数
typedef struct { double t; int b; } fr_t;
static int cmp_fr(const void *a, const void *b) { double x = ((fr_t *)a)->t - ((fr_t *)b)->t; return (x > 0) - (x < 0); }
static void coldstart(double C) {  // 初回の要求の順に、分類ごとの Region に詰める（容量が埋まるまで）
  static fr_t *o;
  if (!o) {
    for (int i = 0; i < NB; i++) NOBJ += (int)B[i].c;
    o = malloc(NOBJ * sizeof(fr_t)), obin = malloc(NOBJ * sizeof(int)), oreg = malloc(NOBJ * sizeof(int));
    rlam = malloc(NOBJ * sizeof(double)), rfill = malloc(NOBJ * sizeof(double));
    uint64_t z = 777;
    for (int i = 0, n = 0; i < NB; i++)
      for (int j = 0; j < (int)B[i].c; j++, n++) {
        z += 0x9E3779B97F4A7C15ull;
        uint64_t x = z; x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull; x = (x ^ (x >> 27)) * 0x94D049BB133111EBull; x ^= x >> 31;
        o[n] = (fr_t){-log(((x >> 11) + 0.5) * 0x1.0p-53) / B[i].lam, i};
      }
    qsort(o, NOBJ, sizeof(fr_t), cmp_fr);
  }
  int cur[KMAX], sealed = 0, cap = (int)(C / R);
  double ob[KMAX];
  for (int g = 0; g < KMAX; g++) cur[g] = -1, ob[g] = 0;
  NREG = 0;
  for (int n = 0; n < NOBJ; n++) {
    int i = o[n].b, g = GR[B[i].k];
    obin[n] = i, oreg[n] = -1;
    if (sealed >= cap) continue;
    if (cur[g] < 0 || ob[g] + B[i].s > R) {
      if (cur[g] >= 0) rfill[cur[g]] = ob[g], sealed++;
      cur[g] = NREG++, rlam[cur[g]] = 0, rfill[cur[g]] = 0, ob[g] = 0;
    }
    oreg[n] = cur[g], rlam[cur[g]] += B[i].lam, ob[g] += B[i].s;
  }
}
static int pins(double T) {  // 寿命が観測期間を超える初期 Region の物を不死にする。不死の Region の数を返す
  int np = 0;
  static char *pr;
  pr = realloc(pr, NREG + 1);
  for (int r = 0; r < NREG; r++) {
    double x = rlam[r] * T;  // log((e^x − 1)/Λ) > log H
    pr[r] = rfill[r] > 0 && x + log(-expm1(-x)) - log(rlam[r]) > log(HOR);
    np += pr[r];
  }
  memset(pin, 0, NB * sizeof(double)), memset(PINB, 0, sizeof PINB);
  for (int n = 0; n < NOBJ; n++)
    if (oreg[n] >= 0 && pr[oreg[n]]) {
      int i = obin[n];
      pin[i] += 1, PINB[B[i].k] += B[i].s * R / rfill[oreg[n]];  // 詰め残しも含めて Region 1 個 = R
    }
  return np;
}
static double region(double T, int lru, double *ohr, double *bhr, double *occ_k) {
  double lT = log(T), fe[KMAX];
  for (int k = 0; k < K; k++) fe[k] = (int)(R / SZ[k]) * SZ[k] / R;  // 単一サイズの Region の充填率
  for (int i = 0; i < NB; i++) logL[i] = lT;                          // FIFO の解（LRU の初期値）
  for (int it = 0; it < 60; it++) {  // FIFO も組成（詰め残し）と封じるまでの待ちのために回す
    double lM[KMAX][G + 1], lW[KMAX][G + 1], q[KMAX], res = 0, elk[KMAX], lU[KMAX], half[KMAX];
    static double LS[KMAX][4096];  // 揺らぎの補正用：同居の物の touch 率の和 Λ_rest の標本
    static double la[400000];      // 封じてからの寿命 log L（U を足す前）
    int nls[KMAX];
    for (int k = 0; k < K; k++) {  // log M_k(t) = log E_ins[e^{λt}]（挿入はミス率に比例）
      double lz = -INFINITY;
      for (int i = 0; i < NB; i++)
        if (B[i].k == k) {
          double x = log(B[i].lam) + logL[i];
          lm[i] = CE(i) > 0 ? log(CE(i) * B[i].lam) - (x > 0 ? x + log1p(exp(-x)) : log1p(exp(x))) : -INFINITY;
          lz = lse(lz, lm[i]);
        }
      double el = 0;  // 診断 MF=1：組成の揺らぎを消す（log M = t E_ins[λ]）
      if (lz == -INFINITY) {  // クラスの物が全部不死：挿入なし
        elk[k] = 0, q[k] = -INFINITY;
        for (int j = 0; j <= G; j++) lM[k][j] = 0;
        continue;
      }
      for (int i = 0; i < NB; i++) if (B[i].k == k) el += exp(lm[i] - lz) * B[i].lam;
      elk[k] = el;
      if (getenv("DBG")) fprintf(stderr, "T=%.0f it=%d k=%d E_ins[lam]=%.3e n*E=%.3e miss=%.4f\n", T, it, k, el,
                                 el * (int)(R / SZ[k]), exp(lz));
      for (int j = 0; j <= G && lru; j++) {
        double t = T * j / G, mx = -INFINITY, s = 0;
        if (MF) { lM[k][j] = t * el; continue; }
        for (int i = 0; i < NB; i++) if (B[i].k == k && lm[i] + B[i].lam * t > mx) mx = lm[i] + B[i].lam * t;
        for (int i = 0; i < NB; i++) if (B[i].k == k) s += exp(lm[i] + B[i].lam * t - mx);
        lM[k][j] = mx + log(s) - lz;
      }
      q[k] = lz;
    }
    for (int c = 0, u = 0; c < K; c++) {  // 物（クラス c）を置き、残りを同じ分類の挿入の流れで埋める
      int nn[KMAX], one = 1;
      double qq[KMAX], mx = -INFINITY, tot = 0, ef = 0, cnt = 0;
      for (int k = 0; k < K; k++) if (GR[k] == GR[c] && q[k] > mx) mx = q[k];
      for (int k = 0; k < K; k++) qq[k] = GR[k] == GR[c] && mx > -INFINITY ? exp(q[k] - mx) : k == c, tot += qq[k], one &= !qq[k] || k == c;
      for (int j = 0; j <= G; j++) lW[c][j] = -INFINITY;
      for (int s = 0; s < (one ? 1 : S); s++) {  // 単一サイズなら組成は決まる
        double fill = SZ[c];
        memset(nn, 0, sizeof nn);
        for (;;) {
          double x = U[u++ % (S * 4096)] * tot, a = 0; int k = 0;
          while (k < K - 1 && (qq[k] == 0 || x >= (a += qq[k]))) k++;
          if (fill + SZ[k] > R) break;
          fill += SZ[k], nn[k]++;
        }
        ef += fill / R;
        LS[c][s] = 0;
        for (int k = 0; k < K; k++) LS[c][s] += nn[k] * elk[k];
        nls[c] = s + 1;
        for (int k = 0; k < K; k++) cnt += nn[k];
        for (int j = 0; j <= G && lru; j++) {
          double g = 0;
          for (int k = 0; k < K; k++) g += nn[k] * lM[k][j];
          lW[c][j] = lse(lW[c][j], g);
        }
      }
      for (int j = 0; j <= G; j++) lW[c][j] -= log(one ? 1 : S);
      fe[c] = ef / (one ? 1 : S);
      half[c] = cnt / (one ? 1 : S) / 2;  // 物の後に入る物の数の平均（0〜n−1 で一様）
    }
    /* T の揺らぎの補正（CV > 0）：Region は無アクセス時間がその時点の閾値 Θ を超えると死ぬ。
     * Θ = T(1 + CV·z)、z ~ N(0,1)。寿命が Θ の変動の時間より長い Region では死亡率が時間平均で効き、
     * 1 回の無アクセス区間で死ぬ確率 p = E_Θ[e^{−ΛΘ}]、寿命 L(Λ) = (1/p − 1)/Λ。
     * （Θ が一定なら (e^{ΛT}−1)/Λ に戻る。）同居の物は平均場（クラスごとの E_ins[λ]）で数え、
     * 混在の Region の個数の揺らぎは標本で残す。L(Λ) は log Λ の格子で引く。 */
    enum { GF = 600, NQ = 33 };
    static double lf[GF + 1];
    const double x0 = log(1e-10), x1 = log(10.0);
    double cv = lru ? CV : 0;
    if (cv < 0) {  /* -c auto：追い出しは最後の touch の古い順＝待ち行列。到着（最後の touch）はポアソン、
                    * 退去（追い出し）は書いたバイトで決まりほぼ規則的。T が伸びると到着が減る復元力
                    * −a'(T) = Σ_i (ミス率_i × λ_i) の OU 過程になり、Var(T) = 1 / (2 Σ_i m_i λ_i)。 */
      double fl = 0;
      for (int k = 0; k < K; k++) fl += exp(q[k]) * elk[k];
      cv = fmin(0.5, 1 / (T * sqrt(2 * fl)));
      CVLAST = cv;
    }
    if (cv > 0) {
      double zq[NQ], lwq[NQ], wz = 0;
      for (int q2 = 0; q2 < NQ; q2++) zq[q2] = -4 + 8.0 * q2 / (NQ - 1), wz += exp(-zq[q2] * zq[q2] / 2);
      for (int q2 = 0; q2 < NQ; q2++) lwq[q2] = -zq[q2] * zq[q2] / 2 - log(wz);
      for (int g = 0; g <= GF; g++) {
        double Lm = exp(x0 + (x1 - x0) * g / GF), lp = -INFINITY;
        for (int q2 = 0; q2 < NQ; q2++) lp = lse(lp, lwq[q2] - Lm * T * fmax(1e-3, 1 + cv * zq[q2]));
        lf[g] = log(-expm1(lp)) - lp - log(Lm);
      }
    }
    for (int i = 0; i < NB; i++) {  // log L_i = log ∫ e^{λ_i t + lW(t)} dt（区間ごとに指数で厳密）
      double a = -INFINITY, dt = T / G, *w = lW[B[i].k];
      if (!lru) a = lT;  // Region FIFO：封じてから T で一定
      else if (cv > 0) {
        int c = B[i].k;
        for (int s = 0; s < nls[c]; s++) {
          double y = (log(B[i].lam + LS[c][s]) - x0) / (x1 - x0) * GF;
          int g = y < 0 ? 0 : y >= GF ? GF - 1 : (int)y;
          double fr = fmin(fmax(y - g, 0), 1);
          a = lse(a, lf[g] + fr * (lf[g + 1] - lf[g]));
        }
        a -= log(nls[c]);
      } else
        for (int j = 0; j < G; j++) {
          double g0 = B[i].lam * dt * j + w[j], g1 = B[i].lam * dt * (j + 1) + w[j + 1];
          a = lse(a, g0 + log(dt) + lexpm1d(g1 - g0));
        }
      la[i] = a;
    }
    /* 封じるまでの待ち U：書き込み中の Region は追い出されず、Region の時計（最後の touch）は封じた時から
     * 始まるので、滞在 = U ＋ 封じてからの寿命。U = (後に入る物の数) ÷ 分類の挿入率で、挿入率が U に依る
     * （U が長いほどミスが減る）ので、U · rate(U) = half を同じ反復の中で解く（左辺は U について増加）。 */
    for (int c = 0; c < K; c++) {
      double lo = -10, hi = 60;
      for (int it2 = 0; it2 < 60 && !NOU; it2++) {
        double m = (lo + hi) / 2, f = 0;
        for (int i = 0; i < NB; i++)
          if (GR[B[i].k] == GR[c]) {  // c · λU / (1 + λ(L + U))
            double x = log(B[i].lam), z = x + lse(la[i], m);
            f += CE(i) * exp(x + m - (z > 0 ? z + log1p(exp(-z)) : log1p(exp(z))));
          }
        if (f < half[c]) lo = m; else hi = m;
      }
      lU[c] = NOU ? -INFINITY : (lo + hi) / 2;
    }
    for (int i = 0; i < NB; i++) {
      double a = lse(la[i], lU[B[i].k]);
      if (fabs(a - logL[i]) > res) res = fabs(a - logL[i]);
      logL[i] = it ? 0.5 * logL[i] + 0.5 * a : a;
    }
    if (getenv("DBG"))
      fprintf(stderr, "  it=%d lU0=%.2f logL[first]=%.2f logL[last]=%.2f res=%.3g\n", it, lU[0], logL[0], logL[NB - 1], res);
    if (res < 1e-4) break;
  }
  double occ = 0;
  *ohr = *bhr = 0;
  for (int k = 0; k < K; k++) occ_k[k] = PINB[k], occ += PINB[k];  // 不死の Region
  for (int i = 0; i < NB; i++) {
    double p = sigm(log(B[i].lam) + logL[i]);
    double o = CE(i) * B[i].s * p / fe[B[i].k];  // Region の詰め残しも容量を食う
    pres[i] = (pin[i] + CE(i) * p) / B[i].c;     // 不死の物は在籍率 1
    occ += o, occ_k[B[i].k] += o;
    *ohr += B[i].c * B[i].lam * pres[i], *bhr += B[i].c * B[i].lam * B[i].s * pres[i] / TB;
  }
  return occ;
}
static double region_solve(double C, int lru, double *ohr, double *bhr, double *occ_k) {
  double lo, hi;
  memset(pin, 0, NB * sizeof(double)), memset(PINB, 0, sizeof PINB), NPIN = 0;
  if (lru && HOR > 0) coldstart(C);
  for (int outer = 0, last = -1; outer < 8; outer++) {  // T → 不死の集合 → T … が止まるまで
    lo = -5, hi = 40;
    if (TFIX && lru) lo = hi = log(TFIX);
    for (int it = 0; it < 36 && lo < hi; it++) {
      double m = (lo + hi) / 2;
      if (region(exp(m), lru, ohr, bhr, occ_k) < C) lo = m; else hi = m;
    }
    if (!(lru && HOR > 0)) break;
    int np = pins(exp((lo + hi) / 2));
    if (np == last) break;
    last = NPIN = np;
  }
  region(exp((lo + hi) / 2), lru, ohr, bhr, occ_k);
  if (lru && getenv("DUMP"))  // 診断：ビンごとの (クラス, 物の数, λ, 在籍率)
    for (int i = 0; i < NB; i++) fprintf(stderr, "B %d %g %.4e %.5f\n", B[i].k, B[i].c, B[i].lam, pres[i]);
  return exp((lo + hi) / 2);
}
static void put_hk(const char *nm, double o, double b, double T, const double *ck, double C, const double *sh) {
  printf("  %-9s ohr=%.4f bhr=%.4f T=%.0f Ck=", nm, o, b, T);
  for (int k = 0; k < K; k++) printf("%.4f%s", ck[k] / C, k < K - 1 ? "," : "");
  printf(" hk=");
  for (int k = 0; k < K; k++) {
    double a = 0;
    for (int i = 0; i < NB; i++) if (B[i].k == k) a += B[i].c * B[i].lam * pres[i];
    printf("%.4f%s", a / sh[k], k < K - 1 ? "," : "");
  }
  if (CV < 0 && !strncmp(nm, "rlru", 4)) printf(" cv=%.4f", CVLAST);
  if (HOR > 0 && !strncmp(nm, "rlru", 4)) printf(" pinned=%d", NPIN);
  printf("\n");
}

int main(int argc, char **argv) {
  double fr[16] = {0.05, 0.1, 0.2, 0.3}, Rarg = 0, sh[KMAX], al[KMAX], tot = 0;
  int nf = 4, ngr = 2, grs[3][KMAX];
  uint64_t no[KMAX];
  MF = getenv("MF") != NULL, NOU = getenv("NOU") != NULL, TFIX = getenv("TFIX") ? atof(getenv("TFIX")) : 0;
  for (int a = 1; a < argc; a++) {
    if (!strcmp(argv[a], "-R")) Rarg = parse_size(argv[++a]);
    else if (!strcmp(argv[a], "-S")) S = atoi(argv[++a]);
    else if (!strcmp(argv[a], "-c")) CV = strcmp(argv[++a], "auto") ? atof(argv[a]) : -1;
    else if (!strcmp(argv[a], "-H")) HOR = atof(argv[++a]);
    else if (!strcmp(argv[a], "-f")) { nf = 0; for (char *t = strtok(argv[++a], ","); t; t = strtok(NULL, ",")) fr[nf++] = atof(t); }
    else if (!strcmp(argv[a], "-g")) { int k = 0; for (char *t = strtok(argv[++a], ","); t; t = strtok(NULL, ",")) grs[2][k++] = atoi(t); ngr = 3; }
    else { char *p = argv[a]; SZ[K] = parse_size(strtok(p, ":")); no[K] = strtoull(strtok(NULL, ":"), NULL, 10);
           sh[K] = atof(strtok(NULL, ":")); al[K] = atof(strtok(NULL, ":")); tot += sh[K]; FP += SZ[K] * no[K]; K++; }
  }
  for (int k = 0; k < K; k++) sh[k] /= tot, grs[0][k] = 0, grs[1][k] = k;
  B = malloc(sizeof(bin_t) * 400000);
  for (int k = 0; k < K; k++) {  // 順位を λ の比 ≤ 1.01 で束ねる
    double Hn = 0;
    for (uint64_t r = 1; r <= no[k]; r++) Hn += pow(r, -al[k]);
    for (uint64_t r = 1; r <= no[k];) {
      uint64_t e = r; double c = 0, l = 0;
      while (e <= no[k] && (e == r || pow((double)e / r, al[k]) <= 1.01)) c += 1, l += pow(e, -al[k]), e++;
      B[NB++] = (bin_t){k, c, sh[k] * l / c / Hn, SZ[k]};
      TB += sh[k] * l / Hn * SZ[k];
      r = e;
    }
  }
  logL = malloc(NB * sizeof(double)), pres = malloc(NB * sizeof(double)), lm = malloc(NB * sizeof(double));
  pin = calloc(NB, sizeof(double));
  U = malloc(sizeof(double) * S * 4096);
  uint64_t z = 12345;
  for (int i = 0; i < S * 4096; i++) z = z * 6364136223846793005ull + 1442695040888963407ull, U[i] = (z >> 11) * 0x1.0p-53;
  printf("# footprint %.0f  bins %d\n", FP, NB);
  for (int f = 0; f < nf; f++) {
    double C = FP * fr[f], H, BH, ck[KMAX], o, b, T;
    R = Rarg ? Rarg : C / 256;
    printf("f=%.3f C=%.0f R=%.0f\n", fr[f], C, R);
    T = bisect(-10, 40, -1, C, 0, 0);
    che(T, -1, &H, &BH, 0, 0, 0);
    for (int i = 0; i < NB; i++) pres[i] = 1 - exp(-B[i].lam * T);
    for (int k = 0; k < K; k++) GR[k] = k;
    for (int k = 0; k < K; k++) che(T, k, 0, 0, &ck[k], 0, 0);
    put_hk("lru", H, BH, T, ck, C, sh);
    {  // 静的ナップサック（λ/s の降順に詰める）
      int *ix = malloc(NB * sizeof(int));
      for (int i = 0; i < NB; i++) ix[i] = i;
      for (int i = 1; i < NB; i++) { int x = ix[i], j = i;
        while (j > 0 && B[ix[j - 1]].lam / B[ix[j - 1]].s < B[x].lam / B[x].s) ix[j] = ix[j - 1], j--; ix[j] = x; }
      double left = C, h = 0, bh = 0;
      for (int j = 0; j < NB && left > 0; j++) { bin_t *q = &B[ix[j]]; double t = fmin(1, left / (q->c * q->s));
        h += t * q->c * q->lam, bh += t * q->c * q->lam * q->s, left -= t * q->c * q->s; }
      printf("  opt       ohr=%.4f bhr=%.4f\n", h, bh / TB);
      free(ix);
    }
    T = region_solve(C, 0, &o, &b, ck);
    put_hk("rfifo", o, b, T, ck, C, sh);
    for (int v = 0; v < ngr; v++) {
      char nm[32]; int ng = 0;
      for (int k = 0; k < K; k++) GR[k] = grs[v][k], ng = GR[k] + 1 > ng ? GR[k] + 1 : ng;
      for (int byte = 0; v && byte < 2; byte++) {  // v = 0（分けない）の分割は lru と同じ
        double Cg[KMAX];
        part(C, byte, ng, Cg, &o, &b);
        snprintf(nm, sizeof nm, "%s[%d]", byte ? "partB" : "part", v);
        printf("  %-9s ohr=%.4f bhr=%.4f Cg=", nm, o, b);
        for (int g = 0; g < ng; g++) printf("%.0f%s", Cg[g], g < ng - 1 ? "," : "");
        printf("\n");
      }
      T = region_solve(C, 1, &o, &b, ck);
      snprintf(nm, sizeof nm, "rlru[%d]", v);
      put_hk(nm, o, b, T, ck, C, sh);
    }
    fflush(stdout);
  }
  return 0;
}
