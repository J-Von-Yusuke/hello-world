/* synth_model.c — synth_gen と同じ指定の IRM 負荷に対する Che 型の予測。
 *
 *   synth_model [-R region] [-f f1,f2,..] [-g 0,0,1] [-S samples] [-P steps] size:nobj:share:alpha ...
 *   build: gcc -O3 -march=native -fopenmp -o synth_model synth_model.c -lm（OMP_NUM_THREADS でスレッド数。-fopenmp なしでも動く）
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
 *   rlru[g]     Region LRU、分類ごとの Region（1 本の LRU を共有。配分は自然に決まる）
 *   rpartO/rpartJ/rpartB[g]  Region LRU を分類ごとに別々に持たせ（静的分割）、容量を
 *               OHR / BHR + 0.1·OHR / BHR が最大になるように配る。分類 g の曲線（容量 → ヒット）を
 *               容量の格子（-P 刻み、既定 20）で解き、動的計画法で配分を選ぶ（凹性を仮定しない）
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
#include <sys/time.h>
/* 計測（PROF=1 のとき標準エラーに）：region() の呼び出し・内側の反復の回数と、段階ごとの時間 */
static double wall(void) { struct timeval v; gettimeofday(&v, 0); return v.tv_sec + v.tv_usec * 1e-6; }
static double *WSL, WSLT; static int WSOK;  // ウォームスタート：前の解 log L と、そのときの log T
static long PR_CALL, PR_IT, PR_MAXED; static double PR_T[6], PR_RESMAX;  // PR_MAXED：ITMAX まで回って収束しなかった呼び出し  // 0 組成の標本 1 lM 2 寿命 la 3 離散 4 待ち U 5 その他

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
static int CSCAP;
/* 追い出しの離散性（既定。OLDR=1 で切る）：追い出しは Region 1 個分のバイトを書くたび（間隔 Δ = R ÷ 挿入バイト率）にしか起きない。
 * Region が少ないと Δ が T より長くなり、「無アクセスが T を超えた瞬間に死ぬ」（Che）が崩れる。長さ g の無アクセス区間が
 * 追い出しの時刻を年齢 T 以降に含む確率は E[min(1,(g−T)+/Δ)] = e^{−ΛT}(1−e^{−ΛΔ})/(ΛΔ) なので、
 * 寿命 ≈ (e^{ΛT}−1)/Λ · ΛΔ/(1−e^{−ΛΔ}) + Δ/2（Δ → 0 で Che の式に戻る） */
static int DISC, OPENR;
static double DLAST;  // 最後に解いた追い出しの間隔 Δ（pins が使う）
/* 立ち上がりの隔離（-H <観測期間>）：空のキャッシュを埋める間は全部がミスなので、初回の要求の順に物が
 * Region に詰まる。最初の Region には熱い物が集まり、寿命 (e^{ΛT}−1)/Λ が観測期間 H を超えれば事実上不死になる。
 * その物は二度とミスせず（在籍率 1）、残りの物と容量で定常モデルを解く。pin[i] はビン i の不死の物の数。 */
static double HOR, *pin, PINB[KMAX];
static int NOBJ, NREG, NPIN, *obin, *oreg;
static double *rlam, *rfill;
static int INIT = -1, ITS;  // 診断 SCAN：内側の反復の初期値（-1 = FIFO の解）、最後の反復の回数と残差
static double RES, DAMP = 0.5;  // DAMP：log ℓ の減衰（古い値を残す割合）
static int ITMAX = 300, FIXDAMP;  // 診断 FIXDAMP=1 ITMAX=60：11-17 以前の解き方（減衰 0.5 固定）
static int ONLY = -1;  // ≥ 0：分類 ONLY の物だけでキャッシュを解く（静的分割の 1 区画）
static double DOH, DBH, DK[KMAX];  // DRAM のヒット（-D、下の put_hk の前の説明）。SSD 側の各結果に足す
#define CE(i) (ONLY >= 0 && GR[B[i].k] != ONLY ? 0 : B[i].c - pin[i])  // 定常モデルで扱う物の数
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
    /* 容量が埋まった後も、初回の要求は必ずミスして初回の順に Region に入る（大きい物が数万要求で容量を埋めても、
     * 小物の初回は数百万要求続き、熱い小物の Region が不死になる）。CSCAP=1 で旧モデル（容量が埋まるまで） */
    if (CSCAP && sealed >= cap) continue;
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
    double x = rlam[r] * T, d = rlam[r] * DLAST;  // log((e^x − 1)/Λ) > log H（DISC なら離散の倍率 d/(1−e^{−d}) を掛ける）
    pr[r] = rfill[r] > 0 && x + log(-expm1(-x)) - log(rlam[r]) + (DISC && d > 1e-8 ? log(d / -expm1(-d)) : 0) > log(HOR);
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
/* Region の中身（組成）の厳密な分布（2026-11-19）：クラス c の物で始まる Region に、分類の挿入の流れ（確率 p_k）から
 * 物を 1 つずつ引き、入らない物を引いたら封じる。終わりの組成 n（クラス k が n_k 個、詰め f = s_c + Σ n_k s_k ≤ R）の確率は
 *   多項係数(n) · Π p_k^{n_k} · Σ_{k: f + s_k > R} p_k
 * （詰めは増えるだけなので、同じ物の集まりはどの順でも入る）。前は S 個の組成を固定の乱数で引いていた。引いた個数は p_k について
 * 階段状に変わるので、反復の写像が不連続になり（lW は組成の lse で大物の多い数個に支配され、段差が大きく出る）、
 * 分類なし（混在）の rlru[0] が周期解に落ちて収束せず、標本の偏り（S = 400 → 4000 で BHR が 0.2pt 動く）も残っていた。 */
typedef struct { double lp, f, n, ls; } mix_t;
static mix_t *MX; static long NMX, CAPMX;
#define MIXCAP 4000000  // 組成の数がこれを超える負荷は従来の標本に戻す
static void mix_rec(int a, int na, const int *ks, const double *lpk, const double *el, double f, double lw, double n, double ls) {
  if (NMX > MIXCAP) return;
  if (a == na) {
    double st = 0;
    for (int b = 0; b < na; b++) if (f + SZ[ks[b]] > R) st += exp(lpk[b]);
    if (st <= 0) return;  // まだ入る物しか引けない：先へ続く（その組成は終わりにならない）
    if (NMX == CAPMX) CAPMX = CAPMX ? 2 * CAPMX : 4096, MX = realloc(MX, CAPMX * sizeof(mix_t));
    MX[NMX++] = (mix_t){lw + lgamma(n + 1) + log(st), f, n, ls};
    return;
  }
  for (int m = 0; f + m * SZ[ks[a]] <= R; m++)
    mix_rec(a + 1, na, ks, lpk, el, f + m * SZ[ks[a]], lw + m * lpk[a] - lgamma(m + 1), n + m, ls + m * el[ks[a]]);
}
static int cmp_mix(const void *a, const void *b) { double x = ((mix_t *)b)->lp - ((mix_t *)a)->lp; return (x > 0) - (x < 0); }
static int64_t gcd64(int64_t a, int64_t b) { while (b) { int64_t t = a % b; a = b, b = t; } return a; }
static double region(double T, int lru, double *ohr, double *bhr, double *occ_k) {
  double lT = log(T), fe[KMAX];
  for (int k = 0; k < K; k++) fe[k] = (int)(R / SZ[k]) * SZ[k] / R;  // 単一サイズの Region の充填率
  for (int i = 0; i < NB; i++) logL[i] = lT;                          // FIFO の解（LRU の初期値）
  /* ウォームスタート（2026-11-19）：同じ区間解き（region_solve）の中で前の T の解が収束していれば、それを
   * T の比だけずらして初期値にする（T を少し動かすたびに FIFO の解から解き直していた）。収束しなかった解は使わない */
  if (lru && INIT < 0 && WSOK && !getenv("NOWARM"))
    for (int i = 0; i < NB; i++) logL[i] = WSL[i] + lT - WSLT;
  if (lru && INIT >= 0) {  // 診断 SCAN：初期値を変える（0 = 上限 ℓ_max、1.. = T と上限の間の乱数）
    double nl = 0;
    for (int i = 0; i < NB; i++) nl = fmax(nl, B[i].lam * (int)(R / SZ[B[i].k]));
    uint64_t z = 977ull * INIT + 1;
    for (int i = 0; i < NB; i++) {
      z = z * 6364136223846793005ull + 1442695040888963407ull;
      logL[i] = lT + (INIT ? (z >> 11) * 0x1.0p-53 : 1) * (nl * T + log(-expm1(-nl * T)) - log(nl) - lT);
    }
  }
  double dmp = DAMP, rprev = INFINITY, w0 = wall(), w1;
  PR_CALL++;
  for (int it = 0; it < ITMAX; it++) {  // FIFO も組成（詰め残し）と封じるまでの待ちのために回す
    double lM[KMAX][G + 1], lW[KMAX][G + 1], q[KMAX], res = 0, elk[KMAX], lU[KMAX], half[KMAX];
    PR_IT++;
    static double LS[KMAX][4096], LW[KMAX][4096];  // 同居の物の touch 率の和 Λ_rest の値と、その log 確率（揺らぎ・離散の補正用）
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
#pragma omp parallel for schedule(static)  // 時刻の格子 j ごとに独立
      for (int j = 0; j <= (lru ? G : -1); j++) {
        double t = T * j / G, mx = -INFINITY, s = 0;
        if (MF) { lM[k][j] = t * el; continue; }
        for (int i = 0; i < NB; i++) if (B[i].k == k && lm[i] + B[i].lam * t > mx) mx = lm[i] + B[i].lam * t;
        for (int i = 0; i < NB; i++) if (B[i].k == k) s += exp(lm[i] + B[i].lam * t - mx);
        lM[k][j] = mx + log(s) - lz;
      }
      q[k] = lz;
    }
    w1 = wall(), PR_T[1] += w1 - w0, w0 = w1;
    for (int c = 0, u = 0; c < K; c++) {  // 物（クラス c）を置き、残りを同じ分類の挿入の流れで埋める
      int nn[KMAX], one = 1;
      double qq[KMAX], mx = -INFINITY, tot = 0, ef = 0, cnt = 0;
      for (int k = 0; k < K; k++) if (GR[k] == GR[c] && q[k] > mx) mx = q[k];
      for (int k = 0; k < K; k++) qq[k] = GR[k] == GR[c] && mx > -INFINITY ? exp(q[k] - mx) : k == c, tot += qq[k], one &= !qq[k] || k == c;
      for (int j = 0; j <= G; j++) lW[c][j] = -INFINITY;
      int ks[KMAX], na = 0; double lpk[KMAX];
      int64_t gu = 0;
      for (int k = 0; k < K; k++) if (qq[k] > 0) ks[na] = k, lpk[na++] = log(qq[k] / tot), gu = gcd64(gu, (int64_t)SZ[k]);
      NMX = 0;
      if (!getenv("MIXSAMPLE")) mix_rec(0, na, ks, lpk, elk, SZ[c], 0, 0, 0);  // 診断 MIXSAMPLE=1：従来の標本
      long M = gu > 0 ? (long)((R - SZ[c]) / gu) : 0;
      if (!getenv("MIXSAMPLE") && NMX > 0 && NMX <= MIXCAP && M < 50000000) {  // 厳密な組成
        double mass = 0;
        for (long x = 0; x < NMX; x++) { double w = exp(MX[x].lp); mass += w, ef += w * MX[x].f / R, cnt += w * MX[x].n; }
        if (fabs(mass - 1) > 1e-9) fprintf(stderr, "synth_model: Region の組成の確率の和が %.12f（1 のはず）\n", mass);
        fe[c] = ef / mass, half[c] = cnt / mass / 2;
        /* Λ_rest の分布：確率の大きい順に、残りが 1e-12 未満になるか 4096 個まで（離散・揺らぎの補正は lse で増幅されない） */
        qsort(MX, NMX, sizeof(mix_t), cmp_mix);
        double kept = 0; int ns = 0;
        while (ns < NMX && ns < 4096 && kept < mass * (1 - 1e-12)) kept += exp(MX[ns].lp), ns++;
        for (int s = 0; s < ns; s++) LS[c][s] = MX[s].ls, LW[c][s] = MX[s].lp - log(kept);
        nls[c] = ns;
        /* lW = log E[Π_k M_k^{n_k}]：詰め f からの残りの寄与 V(f) = Σ_k p_k (f + s_k > R ? 1 : M_k V(f + s_k)) を上から解く */
#pragma omp parallel for schedule(static)
        for (int j = 0; j <= (lru ? G : -1); j++) {
          double *v = malloc((M + 1) * sizeof(double));
          for (long m = M; m >= 0; m--) {
            double f = SZ[c] + (double)m * gu, a = -INFINITY;
            for (int b = 0; b < na; b++) {
              long m2 = m + (long)(SZ[ks[b]] / gu);
              a = lse(a, lpk[b] + (f + SZ[ks[b]] > R ? 0 : lM[ks[b]][j] + v[m2]));
            }
            v[m] = a;
          }
          lW[c][j] = v[0];
          free(v);
        }
        continue;
      }
      if (NMX > MIXCAP) fprintf(stderr, "synth_model: Region の組成が %d 個を超えるので標本 %d 個で近似する\n", MIXCAP, S);
      for (int s = 0; s < (one ? 1 : S); s++) {  // 単一サイズなら組成は決まる
        LW[c][s] = -log(one ? 1 : S);
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
    w1 = wall(), PR_T[0] += w1 - w0, w0 = w1;
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
#pragma omp parallel for schedule(static)  // ビン i ごとに独立
    for (int i = 0; i < NB; i++) {  // log L_i = log ∫ e^{λ_i t + lW(t)} dt（区間ごとに指数で厳密）
      double a = -INFINITY, dt = T / G, *w = lW[B[i].k];
      if (!lru) a = lT;  // Region FIFO：封じてから T で一定
      else if (cv > 0) {
        int c = B[i].k;
        for (int s = 0; s < nls[c]; s++) {
          double y = (log(B[i].lam + LS[c][s]) - x0) / (x1 - x0) * GF;
          int g = y < 0 ? 0 : y >= GF ? GF - 1 : (int)y;
          double fr = fmin(fmax(y - g, 0), 1);
          a = lse(a, LW[c][s] + lf[g] + fr * (lf[g + 1] - lf[g]));
        }
      } else
        for (int j = 0; j < G; j++) {
          double g0 = B[i].lam * dt * j + w[j], g1 = B[i].lam * dt * (j + 1) + w[j + 1];
          a = lse(a, g0 + log(dt) + lexpm1d(g1 - g0));
        }
      la[i] = a;
    }
    w1 = wall(), PR_T[2] += w1 - w0, w0 = w1;
    if (DISC && lru) {  // 追い出しの間隔 Δ = R ÷ 挿入バイト率（今の ℓ でのミス率）。Λ は同居の平均場の標本 LS
      double br = 0, D;
      for (int i = 0; i < NB; i++) br += CE(i) * B[i].s * B[i].lam * (1 - sigm(log(B[i].lam) + logL[i]));
      D = DLAST = br > R * 1e-15 ? R / br : 1e15;  // 二分法の端（ほぼ全部ヒット）でも有限に
#pragma omp parallel for schedule(static)
      for (int i = 0; i < NB; i++) {
        int c = B[i].k; double g = -INFINITY;  // log の平均 log E_s[x/(1 − e^{−x})]
        for (int s = 0; s < nls[c]; s++) {
          double x = (B[i].lam + LS[c][s]) * D;
          g = lse(g, LW[c][s] + (x < 1e-8 ? 0 : x > 30 ? log(x) : log(x / -expm1(-x))));
        }
        la[i] = lse(la[i] + g, log(D / 2));
      }
    }
    w1 = wall(), PR_T[3] += w1 - w0, w0 = w1;
    /* 封じるまでの待ち U：書き込み中の Region は追い出されず、Region の時計（最後の touch）は封じた時から
     * 始まるので、滞在 = U ＋ 封じてからの寿命。U = (後に入る物の数) ÷ 分類の挿入率で、挿入率が U に依る
     * （U が長いほどミスが減る）ので、U · rate(U) = half を同じ反復の中で解く（左辺は U について増加）。 */
    /* 解き方（2026-11-19）：前は [−10, 60] の二分法 60 回で、反復ごとに 60 × K 回の並列区間を作り、全体の半分の時間を
     * 使っていた。f(m) = Σ c·λU/(1 + λ(L + U))（m = log U）は m について増加で、微分も閉じた形
     * f'(m) = Σ term·(1 − σ(z)·σ(m − la))（z = log λ + lse(la, m)）なので、前の反復の U から始めるニュートン法にし、
     * 枠 [lo, hi] から出る一歩は二分法に替える（同じ根を、ほぼ機械精度まで数回で求める）。 */
    static double lUprev[KMAX]; static int lUok;
    if (it == 0) lUok = 0;
    for (int c = 0; c < K; c++) {
      double lo = -10, hi = 60, m = lUok && lUprev[c] > lo && lUprev[c] < hi ? lUprev[c] : 25;
      for (int it2 = 0; it2 < 100 && !NOU; it2++) {
        double f = 0, fp = 0;
#pragma omp parallel for schedule(static) reduction(+ : f, fp)
        for (int i = 0; i < NB; i++)
          if (GR[B[i].k] == GR[c]) {  // c · λU / (1 + λ(L + U))
            double x = log(B[i].lam), z = x + lse(la[i], m);
            double t = CE(i) * exp(x + m - (z > 0 ? z + log1p(exp(-z)) : log1p(exp(z))));
            f += t, fp += t * (1 - sigm(z) * sigm(m - la[i]));
          }
        if (f < half[c]) lo = m; else hi = m;
        double mn = fp > 0 ? m - (f - half[c]) / fp : NAN;
        if (!(mn > lo && mn < hi)) mn = (lo + hi) / 2;
        if (fabs(mn - m) < 1e-13 * fmax(1, fabs(m)) || hi - lo < 1e-13) { m = mn; break; }
        m = mn;
      }
      lU[c] = NOU ? -INFINITY : m;
      lUprev[c] = m;
    }
    lUok = 1;
    w1 = wall(), PR_T[4] += w1 - w0, w0 = w1;
    int iworst = 0;
    for (int i = 0; i < NB; i++) {
      double a = lse(la[i], lU[B[i].k]);
      if (fabs(a - logL[i]) > res) res = fabs(a - logL[i]), iworst = i;
      logL[i] = it ? dmp * logL[i] + (1 - dmp) * a : a;
    }
    if (getenv("DIAGIT")) {  // 計測用 DIAGIT=1：反復ごとの残差・減衰・最悪のビン（クラス、λ、在籍率）・U・Δ・占有・OHR
      double oc = 0, oh = 0;
      for (int i = 0; i < NB; i++) { double p = sigm(log(B[i].lam) + logL[i]); oc += CE(i) * B[i].s * p / fe[B[i].k]; oh += CE(i) * B[i].lam * p; }
      fprintf(stderr, "it=%d res=%.3e dmp=%.2f worst bin %d k=%d lam=%.3e p=%.6f lU=%.4f,%.4f D=%.4g occ=%.6e ohr=%.6f\n", it, res, dmp,
              iworst, B[iworst].k, B[iworst].lam, sigm(log(B[iworst].lam) + logL[iworst]), lU[0], lU[K - 1], DLAST, oc, oh);
    }
    if (getenv("DBG"))
      fprintf(stderr, "  it=%d lU0=%.2f logL[first]=%.2f logL[last]=%.2f res=%.3g\n", it, lU[0], logL[0], logL[NB - 1], res);
    /* 減衰の自動調整：写像 Φ の微分には大きな負の固有値がある（ℓ↑ → 熱い物のミス↓ → 流れが冷える → ℓ↓）。
     * 減衰 d の反復の倍率は d + (1−d)μ なので、μ < −(1+d)/(1−d) だと 2 周期で振動して収束しない（α ≥ 1.2 で
     * 0.5 では振動し、§N.4 の「跳び」に見えた）。残差が減らなくなったら d を上げる */
    if (it > 1 && res > 0.95 * rprev && !FIXDAMP) dmp = fmin(0.95, dmp + 0.15);
    rprev = res;
    ITS = it, RES = res;
    w1 = wall(), PR_T[5] += w1 - w0, w0 = w1;
    if (res < 1e-4) break;
  }
  if (ITS == ITMAX - 1 && RES >= 1e-4) PR_MAXED++, PR_RESMAX = fmax(PR_RESMAX, RES);
  if (lru) {  // 次の T の初期値に残す（収束したときだけ）
    WSOK = RES < 1e-4;
    if (WSOK) memcpy(WSL, logL, NB * sizeof(double)), WSLT = lT;
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
  if (OPENR) {  // 書き込み中の Region も N 個の枠を 1 つ取る（rf_region.c の rf_place）。中身は平均で半分なので、空きの R/2 を分類ごとに足す
    int seen[KMAX] = {0};
    for (int i = 0; i < NB; i++)
      if (CE(i) > 0 && !seen[GR[B[i].k]]) seen[GR[B[i].k]] = 1, occ += R / 2;
  }
  return occ;
}
static double region_solve(double C, int lru, double *ohr, double *bhr, double *occ_k) {
  double lo, hi;
  memset(pin, 0, NB * sizeof(double)), memset(PINB, 0, sizeof PINB), NPIN = 0;
  WSOK = 0;  // ウォームスタートは 1 つの区間解きの中だけ（区画・分類・容量が変わると別の問題）
  int hor = lru && HOR > 0 && ONLY < 0;  // 静的分割の区画では立ち上がりの隔離を入れない
  if (hor) coldstart(C);
  for (int outer = 0, last = -1; outer < 8; outer++) {  // T → 不死の集合 → T … が止まるまで
    lo = -5, hi = 40;
    if (TFIX && lru) lo = hi = log(TFIX);
    /* 占有 = C となる log T を、符号を保つ線形補間（イリノイ法）で詰める（2026-11-19）。前は 36 回の二分法で、
     * 1 回の区間解きに region() を 37 回呼んでいた。占有は T について増加でなめらかなので、同じ精度
     * （log T の幅 1e-10）まで 10 回前後で届く。補間が枠の外に出る・縮まないときは二分法の一歩にする。 */
    if (lo < hi) {
      double glo = region(exp(lo), lru, ohr, bhr, occ_k) - C, ghi = region(exp(hi), lru, ohr, bhr, occ_k) - C;
      if (glo >= 0) hi = lo;        // どの T でも占有が C 以上：下端（二分法でも下端に寄る）
      else if (ghi < 0) lo = hi;    // どの T でも足りない：上端
      for (int it = 0, side = 0; it < 80 && hi - lo > 1e-10; it++) {
        double m = (lo * ghi - hi * glo) / (ghi - glo);
        if (!(m > lo && m < hi) || it % 8 == 7) m = (lo + hi) / 2;  // 8 回に 1 回は二分（補間が片側に張り付くのを防ぐ）
        double g = region(exp(m), lru, ohr, bhr, occ_k) - C;
        if (g < 0) { lo = m, glo = g; if (side < 0) ghi /= 2; side = -1; }
        else       { hi = m, ghi = g; if (side > 0) glo /= 2; side = 1; }
        if (fabs(g) < 1e-12 * C) { lo = hi = m; break; }
      }
    }
    if (getenv("DIAG")) {  // 計測用 DIAG=1：外側の反復ごとに 不死の Region の数・T・占有/C・内側の反復と残差・括弧の両端の占有
      double gl = region(exp(lo), lru, ohr, bhr, occ_k) - C, gh = region(exp(hi), lru, ohr, bhr, occ_k) - C;
      double oc = region(exp((lo + hi) / 2), lru, ohr, bhr, occ_k);
      fprintf(stderr, "DIAG lru=%d outer=%d pinned=%d T=%.6g occ/C=%.6f its=%d res=%.1e  g(lo)/C=%.2e g(hi)/C=%.2e width=%.1e\n",
              lru, outer, NPIN, exp((lo + hi) / 2), oc / C, ITS, RES, gl / C, gh / C, hi - lo);
    }
    if (!hor) break;
    int np = pins(exp((lo + hi) / 2));
    if (np == last) break;
    last = NPIN = np;
  }
  region(exp((lo + hi) / 2), lru, ohr, bhr, occ_k);
  if (getenv("DIAG")) {
    double s = 0; for (int k = 0; k < K; k++) s += occ_k[k];
    fprintf(stderr, "DIAG final only=%d regions=%.1f pinned=%d sum(occ_k)/C=%.6f its=%d res=%.1e T=%.6g D=%.4g ohr=%.6f bhr=%.6f\n", ONLY,
            C / R, NPIN, s / C, ITS, RES, exp((lo + hi) / 2), DLAST, *ohr, *bhr);
  }
  if (lru && getenv("DUMP"))  // 診断：ビンごとの (クラス, 物の数, λ, 在籍率)
    for (int i = 0; i < NB; i++) fprintf(stderr, "B %d %g %.4e %.5f\n", B[i].k, B[i].c, B[i].lam, pres[i]);
  return exp((lo + hi) / 2);
}
/* Region LRU の静的分割：分類 g に容量 m·C/P（m = 0..P）を与えた区画を単独で解き、
 * 目的 a·BHR + b·OHR の和が最大になる (m_g)（Σ m_g = P）を動的計画法で選ぶ */
static int P = 20;
static void rpart(double C, int ng, int v, const double *nat) {
  static double oh[KMAX][201], bh[KMAX][201];
  double ck[KMAX];
  for (int g = 0; g < ng; g++) {
    double foot = 0, ho = 0, hb = 0;
    for (int i = 0; i < NB; i++)
      if (GR[B[i].k] == g) foot += B[i].c * B[i].s, ho += B[i].c * B[i].lam, hb += B[i].c * B[i].lam * B[i].s / TB;
    oh[g][0] = bh[g][0] = 0;
    ONLY = g;
    for (int m = 1; m <= P; m++)
      if (C * m / P >= foot) oh[g][m] = ho, bh[g][m] = hb;  // 区画が分類の全部を持てる
      else region_solve(C * m / P, 1, &oh[g][m], &bh[g][m], ck);
    ONLY = -1;
  }
  if (nat) {  /* 自然な配分（共有の Region LRU の取り分 nat[g]）が、ある w の目的 BHR + w·OHR の KKT 点かを調べる：
               * 分類ごとの限界値 μ_g(w) = d(bh + w·oh)/dC（曲線の中心差分）が全部等しくなる w があるか。
               * K = 2 では方程式 1 本・未知数 1 個で、ふつう解がある。K ≥ 3 では K−1 本に 1 個で、ふつう無い */
    double bw = 0, bs = INFINITY, mu[KMAX], mo[KMAX], mb[KMAX];
    for (int g = 0; g < ng; g++) {
      int j = (int)fmin(P - 1, fmax(1, round(nat[g] / C * P)));
      mo[g] = (oh[g][j + 1] - oh[g][j - 1]) / 2, mb[g] = (bh[g][j + 1] - bh[g][j - 1]) / 2;
    }
    for (int s = 0; s <= 400; s++) {  // w = 0 と 10^-3 〜 10^5
      double w = s ? pow(10, -3 + 8.0 * (s - 1) / 399) : 0, lo = INFINITY, hi = -INFINITY, sm = 0;
      for (int g = 0; g < ng; g++) mu[g] = mb[g] + w * mo[g], lo = fmin(lo, mu[g]), hi = fmax(hi, mu[g]), sm += mu[g] / ng;
      if (sm > 0 && (hi - lo) / sm < bs) bs = (hi - lo) / sm, bw = w;
    }
    printf("  kkt[%d]    w*=%.4g spread=%.3f mu(w*)=", v, bw, bs);
    for (int g = 0; g < ng; g++) printf("%.4g%s", mb[g] + bw * mo[g], g < ng - 1 ? "," : "\n");
  }
  const char *nm[3] = {"rpartO", "rpartJ", "rpartB"};
  const double wa[3] = {0, 1, 1}, wb[3] = {1, 0.1, 0};
  for (int o = 0; o < 3; o++) {
    double best[KMAX + 1][201];
    int ch[KMAX][201], m[KMAX];
    for (int r = 0; r <= P; r++) best[0][r] = r ? -INFINITY : 0;
    for (int g = 0; g < ng; g++)
      for (int r = 0; r <= P; r++) {
        best[g + 1][r] = -INFINITY;
        for (int x = 0; x <= r; x++) {
          double v = best[g][r - x] + wa[o] * bh[g][x] + wb[o] * oh[g][x];
          if (v > best[g + 1][r]) best[g + 1][r] = v, ch[g][r] = x;
        }
      }
    double so = 0, sb = 0;
    for (int g = ng - 1, r = P; g >= 0; r -= m[g], g--) m[g] = ch[g][r], so += oh[g][m[g]], sb += bh[g][m[g]];
    char t[32];
    snprintf(t, sizeof t, "%s[%d]", nm[o], v);
    printf("  %-9s ohr=%.4f bhr=%.4f Cg=", t, so + DOH, sb + DBH);
    for (int g = 0; g < ng; g++) printf("%.0f%s", C * m[g] / P, g < ng - 1 ? "," : "");
    printf("\n");
  }
}
/* DRAM（-D：容量 = SSD の容量 × 比）。inclusion=cachelib：どの要求の後もコンテンツは DRAM に入る（ミスは挿入、
 * SSD ヒットは昇格）ので、DRAM は SSD に依らない Che の LRU（特性時間 TD）で、DRAM ヒットは 1 − e^{−λ TD}。
 * SSD に届くのは DRAM ミスだけで、その率は a = λ e^{−λ TD}。SSD 側のモデル（Region の touch・挿入・在籍）は
 * λ を a に置き換えてそのまま解き、各行の ohr / bhr に DRAM のヒット DOH / DBH を足す（DK：分類ごと） */
static double DRAMF, TD, *LAM0;
static void put_hk(const char *nm, double o, double b, double T, const double *ck, double C, const double *sh) {
  printf("  %-9s ohr=%.4f bhr=%.4f T=%.0f Ck=", nm, o + DOH, b + DBH, T);
  for (int k = 0; k < K; k++) printf("%.4f%s", ck[k] / C, k < K - 1 ? "," : "");
  printf(" hk=");
  for (int k = 0; k < K; k++) {
    double a = DK[k];
    for (int i = 0; i < NB; i++) if (B[i].k == k) a += B[i].c * B[i].lam * pres[i];
    printf("%.4f%s", a / sh[k], k < K - 1 ? "," : "");
  }
  if (CV < 0 && !strncmp(nm, "rlru", 4)) printf(" cv=%.4f", CVLAST);
  if (HOR > 0 && !strncmp(nm, "rlru", 4)) printf(" pinned=%d", NPIN);
  if (DRAMF > 0) printf(" TD=%.0f dram_ohr=%.4f", TD, DOH);
  printf("\n");
}

int main(int argc, char **argv) {
  double fr[16] = {0.05, 0.1, 0.2, 0.3}, Rarg = 0, sh[KMAX], al[KMAX], tot = 0;
  int nf = 4, ngr = 2, grs[3][KMAX];
  uint64_t no[KMAX];
  if (getenv("DAMP")) DAMP = atof(getenv("DAMP"));
  if (getenv("ITMAX")) ITMAX = atoi(getenv("ITMAX"));
  FIXDAMP = getenv("FIXDAMP") != NULL;
  CSCAP = getenv("CSCAP") != NULL, DISC = OPENR = getenv("OLDR") == NULL;  // OLDR=1：11-17 以前の連続の Che（追い出しの離散性・書き込み中の枠なし）
  MF = getenv("MF") != NULL, NOU = getenv("NOU") != NULL, TFIX = getenv("TFIX") ? atof(getenv("TFIX")) : 0;
  for (int a = 1; a < argc; a++) {
    if (!strcmp(argv[a], "-R")) Rarg = parse_size(argv[++a]);
    else if (!strcmp(argv[a], "-S")) S = atoi(argv[++a]);
    else if (!strcmp(argv[a], "-c")) CV = strcmp(argv[++a], "auto") ? atof(argv[a]) : -1;
    else if (!strcmp(argv[a], "-H")) HOR = atof(argv[++a]);
    else if (!strcmp(argv[a], "-P")) P = atoi(argv[++a]) > 200 ? 200 : atoi(argv[a]);  // 0 = 静的分割を解かない
    else if (!strcmp(argv[a], "-D")) DRAMF = atof(argv[++a]);  // DRAM の容量 ÷ SSD の容量
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
  pin = calloc(NB, sizeof(double)), WSL = malloc(NB * sizeof(double));
  U = malloc(sizeof(double) * S * 4096);
  uint64_t z = 12345;
  for (int i = 0; i < S * 4096; i++) z = z * 6364136223846793005ull + 1442695040888963407ull, U[i] = (z >> 11) * 0x1.0p-53;
  printf("# footprint %.0f  bins %d\n", FP, NB);
  LAM0 = malloc(NB * sizeof(double));
  for (int i = 0; i < NB; i++) LAM0[i] = B[i].lam;
  for (int f = 0; f < nf; f++) {
    double C = FP * fr[f], H, BH, ck[KMAX], o, b, T;
    R = Rarg ? Rarg : C / 256;
    printf("f=%.3f C=%.0f R=%.0f\n", fr[f], C, R);
    if (getenv("SCAN")) {  // 診断 SCAN=T1,T2,..：クラスごとの分類で、T を固定して初期値 NST 通りから内側の不動点を解く
      int nst = getenv("NST") ? atoi(getenv("NST")) : 6;
      for (int k = 0; k < K; k++) GR[k] = getenv("SCAN0") ? 0 : k;  // SCAN0=1：分類なし（全部 1 つの流れ）
      char buf[4096]; strncpy(buf, getenv("SCAN"), sizeof buf - 1);
      for (char *t = strtok(buf, ","); t; t = strtok(NULL, ","))
        for (INIT = -1; INIT < nst; INIT++) {
          double occ = region(atof(t), 1, &o, &b, ck);
          printf("  scan T=%s init=%d occ/C=%.4f ohr=%.4f bhr=%.4f it=%d res=%.1e Ck/C=", t, INIT, occ / C, o, b, ITS, RES);
          for (int k = 0; k < K; k++) printf("%.4f%s", ck[k] / C, k < K - 1 ? "," : "\n");
        }
      INIT = -1;
      continue;
    }
    if (DRAMF > 0) {  // DRAM を解き、SSD に届く率 a = λ e^{−λ TD} に置き換える
      for (int i = 0; i < NB; i++) B[i].lam = LAM0[i];
      TD = bisect(-10, 40, -1, C * DRAMF, 0, 0);
      DOH = DBH = 0, memset(DK, 0, sizeof DK);
      for (int i = 0; i < NB; i++) {
        double e = exp(-LAM0[i] * TD), x = B[i].c * LAM0[i] * (1 - e);
        DOH += x, DBH += x * B[i].s / TB, DK[B[i].k] += x, B[i].lam = fmax(LAM0[i] * e, 1e-300);  // 熱い物は a → 0（log を避ける）
      }
      printf("  dram      ohr=%.4f bhr=%.4f TD=%.0f C_D=%.0f\n", DOH, DBH, TD, C * DRAMF);
    }
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
      printf("  opt       ohr=%.4f bhr=%.4f\n", h + DOH, bh / TB + DBH);
      free(ix);
    }
    double pw = wall(); long pc = PR_CALL, pi = PR_IT;
#define PROF_STEP(nm) if (getenv("PROF")) fprintf(stderr, "PROF f=%.3f %-8s %7.2f s  region() %5ld  iterations %7ld\n", fr[f], nm, \
                                                  wall() - pw, PR_CALL - pc, PR_IT - pi), pw = wall(), pc = PR_CALL, pi = PR_IT
    T = region_solve(C, 0, &o, &b, ck);
    put_hk("rfifo", o, b, T, ck, C, sh);
    PROF_STEP("rfifo");
    for (int v = 0; v < ngr; v++) {
      char nm[32]; int ng = 0;
      for (int k = 0; k < K; k++) GR[k] = grs[v][k], ng = GR[k] + 1 > ng ? GR[k] + 1 : ng;
      for (int byte = 0; v && byte < 2; byte++) {  // v = 0（分けない）の分割は lru と同じ
        double Cg[KMAX];
        part(C, byte, ng, Cg, &o, &b);
        snprintf(nm, sizeof nm, "%s[%d]", byte ? "partB" : "part", v);
        printf("  %-9s ohr=%.4f bhr=%.4f Cg=", nm, o + DOH, b + DBH);
        for (int g = 0; g < ng; g++) printf("%.0f%s", Cg[g], g < ng - 1 ? "," : "");
        printf("\n");
      }
      T = region_solve(C, 1, &o, &b, ck);
      snprintf(nm, sizeof nm, "rlru[%d]", v);
      put_hk(nm, o, b, T, ck, C, sh);
      PROF_STEP(nm);
      if (v && P > 0) {
        double nat[KMAX] = {0};  // 自然な配分を分類（グループ）ごとに
        for (int k = 0; k < K; k++) nat[GR[k]] += ck[k];
        rpart(C, ng, v, nat);
        PROF_STEP("rpart");
      }
    }
    fflush(stdout);
  }
  if (getenv("PROF"))
    fprintf(stderr, "PROF not converged (hit ITMAX): %ld, largest residual %.2e\n", PR_MAXED, PR_RESMAX),
    fprintf(stderr, "PROF total: region() %ld, iterations %ld | sample %.1f s, lM %.1f s, la %.1f s, disc %.1f s, U %.1f s, other %.1f s\n",
            PR_CALL, PR_IT, PR_T[0], PR_T[1], PR_T[2], PR_T[3], PR_T[4], PR_T[5]);
  return 0;
}
