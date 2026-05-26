"""
mechanism_analysis.py
=====================
K / sc / update_type がキャッシュ性能を左右するメカニズムの解明

Step 0  : K-/sc-/ut-sensitivity  -- パラメータが各窓でどれだけ性能に効くか
Step 1a : GMM成分数 vs 最適K      -- サイズ分布の自然クラスタ数との対応
Step 1b : Anti-mode数 vs 最適K   -- 分布の谷(valley)と最適閾値数の関係
Step 1c : バイト集中度 vs sc選好  -- sc決定則の導出
Step 2a : ラベルノイズ分析         -- score_margin 分布とノイズ割合
Step 2b : 非定常性分析             -- 前半->後半の精度変化・ラベル分布シフト
Step 2c : 相互情報量               -- 14特徴量とラベルの情報量
M1 vs M2: LRUベースライン vs 最良パラメータの改善量の特性化

使い方:
  python mechanism_analysis.py --dir ./results --out ./output
  python mechanism_analysis.py --dir ./results --out ./output --target mr

出力 (すべて out_dir 以下):
  mech_sensitivity.png / mech_sensitivity.csv
  mech_gmm_vs_k.png    / mech_gmm_vs_k.csv
  mech_sc_rule.png     / mech_sc_rule.csv
  mech_label_noise.png / mech_label_noise.csv
  mech_nonstationarity.png
  mech_mi.png          / mech_mi.csv
  mech_m1_vs_m2.png    / mech_m1_vs_m2.csv
  mech_summary.txt

依存: pip install scikit-learn numpy pandas matplotlib scipy
"""

import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.stats import entropy as scipy_entropy, spearmanr, pearsonr
from scipy.ndimage import gaussian_filter1d
from sklearn.mixture import GaussianMixture
from sklearn.feature_selection import mutual_info_classif
from sklearn.preprocessing import LabelEncoder
from sklearn.tree import DecisionTreeClassifier
from sklearn.model_selection import TimeSeriesSplit, cross_val_predict, LeaveOneGroupOut
from sklearn.metrics import accuracy_score

# ============================================================
# shared constants  (mirror of predict_optimal_params)
# ============================================================
EPSILON = 1e-10
UPDATE_TYPE_MAP = {9: -1, 0: 0, 1: 1, 2: 2}
UPDATE_TYPE_LABEL_EN = {-1: "Static", 0: "Periodic", 1: "WMA", 2: "DynWMA"}
FNAME_RE = re.compile(
    r"(?P<trace>.+?)-SIZEDIVIDE-(?P<k>\d+)-(?P<c>\d+)-(?P<t>\d+)",
    re.IGNORECASE,
)
WINDOW_FEATURE_NAMES = [
    "wt_entropy", "wt_norm_entropy",
    "wt_top1_conc", "wt_top3_conc",
    "wt_gini", "wt_n_peaks", "wt_peak_bin",
    "wt_js_vs_prev",
    "wt_n_req", "wt_req_byte",
    "wt_byte_concentration", "wt_byte_mean_size",
    "wt_lru_cum_mr", "wt_lru_inter_mr",
]

# ============================================================
# shared utilities  (self-contained copy)
# ============================================================

def parse_filename(path: str):
    m = FNAME_RE.search(Path(path).stem)
    if not m:
        return None
    t_raw = int(m.group("t"))
    return {
        "filepath":        path,
        "trace":           m.group("trace"),
        "k":               int(m.group("k")),
        "size_correction": int(m.group("c")),
        "update_type":     UPDATE_TYPE_MAP.get(t_raw, t_raw),
    }


def collect_csvs(dir_path: str) -> pd.DataFrame:
    rows = []
    for f in sorted(Path(dir_path).glob("*.csv")):
        info = parse_filename(str(f))
        if info:
            rows.append(info)
    if not rows:
        raise ValueError(f"SIZEDIVIDE CSV not found: {dir_path}")
    return pd.DataFrame(rows)


def find_series_cols(df: pd.DataFrame, base: str) -> list:
    norm = {}
    for c in df.columns:
        m = re.fullmatch(re.escape(base) + r"_{1,2}(\d+)", c)
        if m:
            norm[int(m.group(1))] = c
    return [norm[i] for i in sorted(norm)]


def to_prob(vals: np.ndarray) -> np.ndarray:
    v = vals.astype(float) + EPSILON
    return v / v.sum()


def js_div(p: np.ndarray, q: np.ndarray) -> float:
    m = 0.5 * (p + q)
    return float(0.5 * scipy_entropy(p, m) + 0.5 * scipy_entropy(q, m))


def gini_coeff(prob: np.ndarray) -> float:
    p = np.sort(prob.astype(float))
    n = len(p)
    if n == 0 or p.sum() == 0:
        return 0.0
    cumsum = np.cumsum(p)
    return float(1 - 2 * cumsum.sum() / (n * p.sum()) + 1 / n)


def count_peaks(prob: np.ndarray, threshold: float = 0.02) -> int:
    peaks = 0
    for i in range(1, len(prob) - 1):
        if prob[i] > prob[i - 1] and prob[i] > prob[i + 1] and prob[i] >= threshold:
            peaks += 1
    return max(peaks, 1)


def count_valleys(prob: np.ndarray) -> int:
    """Local minima (valleys) count -- between peaks."""
    valleys = 0
    for i in range(1, len(prob) - 1):
        if prob[i] < prob[i - 1] and prob[i] < prob[i + 1]:
            valleys += 1
    return valleys


def extract_window_features(df, t):
    now_cols = find_series_cols(df, "view_counts_now")
    if not now_cols or t >= len(df):
        return None
    B = len(now_cols)
    bin_idx = np.arange(B)
    now_t = df[now_cols].iloc[t].values.astype(float)
    prob_t = to_prob(now_t)
    ent = float(scipy_entropy(prob_t))
    norm_ent = ent / np.log(B) if B > 1 else 0.0
    sp = np.sort(prob_t)[::-1]
    top1 = float(sp[0])
    top3 = float(sp[:3].sum())
    gini = gini_coeff(prob_t)
    n_pk = count_peaks(prob_t)
    pk_bin = int(np.argmax(prob_t))
    n_valleys = count_valleys(prob_t)
    if t > 0:
        now_prev = df[now_cols].iloc[t - 1].values.astype(float)
        js_prev = js_div(to_prob(now_prev), prob_t)
    else:
        js_prev = 0.0
    def _sc(col):
        if col in df.columns:
            return float(pd.to_numeric(df[col].iloc[t], errors="coerce"))
        return np.nan
    n_req = _sc("n_req")
    req_byte = _sc("req_byte")
    byte_w = np.power(2.0, bin_idx.astype(float))
    byte_d = prob_t * byte_w
    byte_dn = byte_d / (byte_d.sum() + EPSILON)
    byte_t3 = float(np.sort(byte_dn)[::-1][:3].sum())
    byte_ms = float(np.dot(bin_idx, byte_dn))
    lru_cum = _sc("miss_ratio_lru")
    lru_inter = _sc("inter_miss_ratio_lru")
    return {
        "wt_entropy":            ent,
        "wt_norm_entropy":       norm_ent,
        "wt_top1_conc":          top1,
        "wt_top3_conc":          top3,
        "wt_gini":               gini,
        "wt_n_peaks":            n_pk,
        "wt_peak_bin":           pk_bin,
        "wt_n_valleys":          n_valleys,
        "wt_js_vs_prev":         js_prev,
        "wt_n_req":              n_req,
        "wt_req_byte":           req_byte,
        "wt_byte_concentration": byte_t3,
        "wt_byte_mean_size":     byte_ms,
        "wt_lru_cum_mr":         lru_cum,
        "wt_lru_inter_mr":       lru_inter,
        "_raw_prob":             prob_t,
    }


# ============================================================
# Core data loader: perf_matrix + aligned_df
# ============================================================

def load_perf_matrix(meta: pd.DataFrame):
    """
    Returns:
      aligned_df   : per-window DataFrame with features + labels + score_margin
      perf_matrix  : {trace: {t: {(K,sc,ut): {"mr", "bmr", "lru_mr"}}}}
      ref_dfs      : {trace: reference_csv_DataFrame}  (for distribution analysis)
    """
    aligned_rows = []
    perf_matrix = {}
    ref_dfs = {}

    for trace_name, grp in meta.groupby("trace"):
        cond_dfs = {}
        for _, row in grp.iterrows():
            key = (int(row["k"]), int(row["size_correction"]), int(row["update_type"]))
            try:
                df = pd.read_csv(row["filepath"])
                df.columns = df.columns.str.strip().str.lower()
                cond_dfs[key] = df
            except Exception as e:
                print(f"  [load error] {row['filepath']}: {e}")
        if not cond_dfs:
            continue

        ref_df = next(
            (d for d in cond_dfs.values() if "miss_ratio_lru" in d.columns),
            list(cond_dfs.values())[0],
        )
        ref_dfs[trace_name] = ref_df
        T = len(ref_df)

        # normalisation ranges for combined score
        mr_all, bmr_all = [], []
        for d in cond_dfs.values():
            if "miss_ratio" in d.columns:
                mr_all.extend(pd.to_numeric(d["miss_ratio"], errors="coerce").dropna())
            if "miss_byte_ratio" in d.columns:
                bmr_all.extend(pd.to_numeric(d["miss_byte_ratio"], errors="coerce").dropna())
        mr_min  = float(min(mr_all))  if mr_all  else 0.0
        mr_max  = float(max(mr_all))  if mr_all  else 1.0
        bmr_min = float(min(bmr_all)) if bmr_all else 0.0
        bmr_max = float(max(bmr_all)) if bmr_all else 1.0
        mr_r    = mr_max  - mr_min  if mr_max  > mr_min  else 1.0
        bmr_r   = bmr_max - bmr_min if bmr_max > bmr_min else 1.0

        perf_matrix[trace_name] = {}

        for t in range(T):
            wf = extract_window_features(ref_df, t)
            if wf is None:
                continue
            raw_prob = wf.pop("_raw_prob", None)

            perf_t = {}
            for key, d in cond_dfs.items():
                if t >= len(d):
                    continue
                mr = float(pd.to_numeric(
                    d["miss_ratio"].iloc[t] if "miss_ratio" in d.columns else np.nan,
                    errors="coerce"))
                bmr = float(pd.to_numeric(
                    d["miss_byte_ratio"].iloc[t] if "miss_byte_ratio" in d.columns else np.nan,
                    errors="coerce"))
                lru = float(pd.to_numeric(
                    d["miss_ratio_lru"].iloc[t] if "miss_ratio_lru" in d.columns else np.nan,
                    errors="coerce"))
                perf_t[key] = {"mr": mr, "bmr": bmr, "lru_mr": lru}
            if not perf_t:
                continue

            perf_matrix[trace_name][t] = perf_t

            # best overall condition
            scores = {}
            for key, v in perf_t.items():
                if np.isnan(v["mr"]) or np.isnan(v["bmr"]):
                    continue
                scores[key] = (0.5*(v["mr"]-mr_min)/mr_r
                               + 0.5*(v["bmr"]-bmr_min)/bmr_r)
            if not scores:
                continue
            best_key = min(scores, key=scores.get)
            sorted_scores = sorted(scores.values())
            score_margin = float(sorted_scores[1] - sorted_scores[0]) \
                if len(sorted_scores) >= 2 else np.nan

            aligned_rows.append({
                "trace":               trace_name,
                "window":              t,
                "optimal_K":           best_key[0],
                "optimal_sc":          best_key[1],
                "optimal_ut":          best_key[2],
                "score_margin":        score_margin,
                **wf,
            })

    aligned_df = pd.DataFrame(aligned_rows)
    return aligned_df, perf_matrix, ref_dfs


# ============================================================
# Step 0: Sensitivity Analysis
# ============================================================

def analyze_sensitivity(perf_matrix, aligned_df, out_dir, label):
    """
    Per-window sensitivity:
      K_sensitivity  = max_K(best_MR_for_K) - min_K(best_MR_for_K)
      sc_sensitivity = |MR(best_with_sc=0) - MR(best_with_sc=1)|
      ut_sensitivity = range over update_type
    """
    print("  [Step 0] Sensitivity analysis...")
    rows = []
    for trace_name, tw_dict in perf_matrix.items():
        adf = aligned_df[aligned_df["trace"] == trace_name]
        for t, cond_dict in tw_dict.items():
            # per-K best MR
            k_vals = sorted(set(k for k, _, _ in cond_dict.keys()))
            if len(k_vals) < 2:
                continue
            best_mr_per_k = {}
            for k in k_vals:
                mrs = [v["mr"] for (kk, sc, ut), v in cond_dict.items()
                       if kk == k and not np.isnan(v["mr"])]
                if mrs:
                    best_mr_per_k[k] = min(mrs)
            if len(best_mr_per_k) < 2:
                continue
            k_sens = max(best_mr_per_k.values()) - min(best_mr_per_k.values())

            # sc sensitivity
            mrs_sc0 = [v["mr"] for (k, sc, ut), v in cond_dict.items()
                       if sc == 0 and not np.isnan(v["mr"])]
            mrs_sc1 = [v["mr"] for (k, sc, ut), v in cond_dict.items()
                       if sc == 1 and not np.isnan(v["mr"])]
            sc_sens = abs(min(mrs_sc0) - min(mrs_sc1)) \
                if mrs_sc0 and mrs_sc1 else np.nan

            # ut sensitivity
            ut_vals = sorted(set(ut for _, _, ut in cond_dict.keys()))
            best_mr_per_ut = {}
            for ut in ut_vals:
                mrs = [v["mr"] for (k, sc, uu), v in cond_dict.items()
                       if uu == ut and not np.isnan(v["mr"])]
                if mrs:
                    best_mr_per_ut[ut] = min(mrs)
            ut_sens = (max(best_mr_per_ut.values()) - min(best_mr_per_ut.values())
                       if len(best_mr_per_ut) >= 2 else np.nan)

            # LRU vs best
            lru_mr = next((v["lru_mr"] for v in cond_dict.values()
                           if not np.isnan(v.get("lru_mr", np.nan))), np.nan)
            best_mr = min((v["mr"] for v in cond_dict.values()
                           if not np.isnan(v["mr"])), default=np.nan)
            m1_benefit = float(lru_mr - best_mr) if not np.isnan(lru_mr) else np.nan

            # look up window features
            row_feat = adf[adf["window"] == t]
            feat_vals = {}
            if not row_feat.empty:
                for fn in WINDOW_FEATURE_NAMES:
                    if fn in row_feat.columns:
                        feat_vals[fn] = float(row_feat.iloc[0][fn])

            rows.append({
                "trace":         trace_name,
                "window":        t,
                "k_sensitivity": k_sens,
                "sc_sensitivity": sc_sens,
                "ut_sensitivity": ut_sens,
                "m1_benefit":    m1_benefit,
                **feat_vals,
            })

    sens_df = pd.DataFrame(rows)
    if sens_df.empty:
        print("    [skip] No sensitivity data.")
        return sens_df

    csv_path = os.path.join(out_dir, f"{label}_mech_sensitivity.csv")
    sens_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(f"    Saved: {csv_path}")

    _plot_sensitivity(sens_df, out_dir, label)
    return sens_df


def _plot_sensitivity(sens_df, out_dir, label):
    traces = sens_df["trace"].unique()
    n_tr = len(traces)
    fig, axes = plt.subplots(2, max(n_tr, 1),
                             figsize=(max(6, 5*n_tr), 9), squeeze=False)

    for ci, trace_name in enumerate(traces):
        grp = sens_df[sens_df["trace"] == trace_name]

        # Top row: boxplot of sensitivities
        ax = axes[0, ci]
        data_box = [
            grp["k_sensitivity"].dropna().values,
            grp["sc_sensitivity"].dropna().values,
            grp["ut_sensitivity"].dropna().values,
        ]
        bp = ax.boxplot(data_box, patch_artist=True,
                        medianprops=dict(color="black", linewidth=1.5))
        colors = ["#1976D2", "#388E3C", "#F57C00"]
        for patch, color in zip(bp["boxes"], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
        ax.set_xticklabels(["K-sens", "sc-sens", "ut-sens"])
        ax.set_ylabel("MR range (higher = parameter matters more)")
        ax.set_title(f"Trace: {trace_name}\nParameter sensitivity distribution")
        ax.grid(True, alpha=0.3, axis="y")

        # report medians
        for i, (col, lbl) in enumerate([
            ("k_sensitivity","K"), ("sc_sensitivity","sc"), ("ut_sensitivity","ut")
        ]):
            med = float(np.nanmedian(grp[col]))
            ax.text(i+1, med, f" {med:.4f}", fontsize=8, va="center")

        # Bottom row: scatter k_sensitivity vs wt_js_vs_prev
        ax2 = axes[1, ci]
        if "wt_js_vs_prev" in grp.columns:
            xs = grp["wt_js_vs_prev"].values.astype(float)
            ys = grp["k_sensitivity"].values.astype(float)
            valid = ~(np.isnan(xs) | np.isnan(ys))
            ax2.scatter(xs[valid], ys[valid], alpha=0.35, s=8, color="#1976D2")
            if valid.sum() > 5:
                r, p = spearmanr(xs[valid], ys[valid])
                ax2.set_title(
                    f"Trace: {trace_name}\nK-sens vs distribution drift"
                    f"  (rho={r:.3f}, p={p:.3f})"
                )
            ax2.set_xlabel("wt_js_vs_prev (distribution drift)")
            ax2.set_ylabel("K-sensitivity (MR range)")
            ax2.grid(True, alpha=0.3)

    plt.suptitle(f"Parameter Sensitivity Analysis | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_sensitivity.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Step 1a: GMM / peaks vs optimal K
# ============================================================

def _gmm_bic_optimal(prob: np.ndarray, max_components: int = 6) -> int:
    """BIC-optimal GMM component count on the histogram."""
    B = len(prob)
    # Create pseudo-samples from histogram (capped at 500)
    total = max(int(prob.sum() * 1000), 100)
    total = min(total, 500)
    counts = np.round(prob / prob.sum() * total).astype(int)
    counts = np.maximum(counts, 0)
    X = np.concatenate([np.full(c, i) for i, c in enumerate(counts)
                        if c > 0]).reshape(-1, 1)
    if len(X) < 4:
        return 1
    best_bic = np.inf
    best_n = 1
    for n in range(1, min(max_components + 1, len(X) // 2 + 1)):
        try:
            gm = GaussianMixture(n_components=n, random_state=42, max_iter=200)
            gm.fit(X)
            bic = gm.bic(X)
            if bic < best_bic:
                best_bic = bic
                best_n = n
        except Exception:
            pass
    return best_n


def analyze_gmm_vs_k(ref_dfs, aligned_df, out_dir, label):
    """
    For each (trace, window t): compute
      - BIC-optimal GMM components on the size distribution
      - smoothed-peak count
      - smoothed-valley count
    then correlate with optimal_K.
    """
    print("  [Step 1a] GMM / peaks vs optimal K...")
    rows = []
    for trace_name, ref_df in ref_dfs.items():
        now_cols = find_series_cols(ref_df, "view_counts_now")
        if not now_cols:
            continue
        adf = aligned_df[aligned_df["trace"] == trace_name]
        T = len(ref_df)
        for t in range(T):
            if t >= len(ref_df):
                continue
            raw = ref_df[now_cols].iloc[t].values.astype(float)
            prob = to_prob(raw)
            # smoothed distribution
            smooth = gaussian_filter1d(prob, sigma=1.5)
            smooth = to_prob(smooth)
            n_peaks_smooth = count_peaks(smooth, threshold=0.01)
            n_valleys_smooth = count_valleys(smooth)
            gmm_n = _gmm_bic_optimal(prob, max_components=6)
            row_feat = adf[adf["window"] == t]
            opt_k = int(row_feat.iloc[0]["optimal_K"]) \
                if not row_feat.empty else np.nan
            rows.append({
                "trace":           trace_name,
                "window":          t,
                "gmm_n":           gmm_n,
                "n_peaks_smooth":  n_peaks_smooth,
                "n_valleys_smooth": n_valleys_smooth,
                "optimal_K":       opt_k,
            })

    df_gmm = pd.DataFrame(rows).dropna(subset=["optimal_K"])
    if df_gmm.empty:
        print("    [skip] No data.")
        return df_gmm

    csv_path = os.path.join(out_dir, f"{label}_mech_gmm_vs_k.csv")
    df_gmm.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # correlations
    corr_lines = []
    for trace_name in df_gmm["trace"].unique():
        sub = df_gmm[df_gmm["trace"] == trace_name]
        for col in ["gmm_n", "n_peaks_smooth", "n_valleys_smooth"]:
            xs = sub[col].values.astype(float)
            ys = sub["optimal_K"].values.astype(float)
            valid = ~(np.isnan(xs) | np.isnan(ys))
            if valid.sum() > 4:
                r, p = spearmanr(xs[valid], ys[valid])
                corr_lines.append(
                    f"  {trace_name:<20} {col:<22} rho={r:+.3f}  p={p:.4f}")

    print("\n".join(corr_lines))
    _plot_gmm_vs_k(df_gmm, out_dir, label)
    return df_gmm


def _plot_gmm_vs_k(df_gmm, out_dir, label):
    traces = df_gmm["trace"].unique()
    n_tr = len(traces)
    fig, axes = plt.subplots(2, max(n_tr, 1),
                             figsize=(max(6, 5*n_tr), 9), squeeze=False)

    for ci, trace_name in enumerate(traces):
        sub = df_gmm[df_gmm["trace"] == trace_name]

        ax = axes[0, ci]
        xs = sub["gmm_n"].values.astype(float)
        ys = sub["optimal_K"].values.astype(float)
        valid = ~(np.isnan(xs) | np.isnan(ys))
        if valid.sum() > 2:
            jitter = np.random.default_rng(0).uniform(-0.1, 0.1, valid.sum())
            ax.scatter(xs[valid] + jitter, ys[valid],
                       alpha=0.4, s=10, color="#9C27B0")
            r, p = spearmanr(xs[valid], ys[valid])
        else:
            r, p = 0, 1
        ax.set_xlabel("BIC-optimal GMM components")
        ax.set_ylabel("optimal K")
        ax.set_title(f"Trace: {trace_name}\nGMM components vs optimal K"
                     f"  (rho={r:.3f}, p={p:.3f})")
        ax.grid(True, alpha=0.3)

        ax2 = axes[1, ci]
        xs2 = sub["n_peaks_smooth"].values.astype(float)
        valid2 = ~(np.isnan(xs2) | np.isnan(ys))
        if valid2.sum() > 2:
            jitter2 = np.random.default_rng(1).uniform(-0.1, 0.1, valid2.sum())
            ax2.scatter(xs2[valid2] + jitter2, ys[valid2],
                        alpha=0.4, s=10, color="#0D9488")
            r2, p2 = spearmanr(xs2[valid2], ys[valid2])
        else:
            r2, p2 = 0, 1
        ax2.set_xlabel("Smoothed peak count")
        ax2.set_ylabel("optimal K")
        ax2.set_title(f"Trace: {trace_name}\nPeak count vs optimal K"
                      f"  (rho={r2:.3f}, p={p2:.3f})")
        ax2.grid(True, alpha=0.3)

    plt.suptitle(f"Distribution Structure vs Optimal K | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_gmm_vs_k.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Step 1c: sc decision rule
# ============================================================

def analyze_sc_decision_rule(aligned_df, perf_matrix, out_dir, label):
    """
    For each window t: sc_benefit = MR(best_with_sc=0) - MR(best_with_sc=1)
    Positive -> sc=1 is better.
    Correlate with wt_byte_concentration to find a decision threshold.
    """
    print("  [Step 1c] sc decision rule...")
    rows = []
    for trace_name, tw_dict in perf_matrix.items():
        adf = aligned_df[aligned_df["trace"] == trace_name]
        for t, cond_dict in tw_dict.items():
            mrs_sc0 = [v["mr"] for (k, sc, ut), v in cond_dict.items()
                       if sc == 0 and not np.isnan(v["mr"])]
            mrs_sc1 = [v["mr"] for (k, sc, ut), v in cond_dict.items()
                       if sc == 1 and not np.isnan(v["mr"])]
            if not mrs_sc0 or not mrs_sc1:
                continue
            sc_benefit = min(mrs_sc0) - min(mrs_sc1)
            row_feat = adf[adf["window"] == t]
            if row_feat.empty:
                continue
            byte_conc = float(row_feat.iloc[0].get("wt_byte_concentration", np.nan))
            rows.append({
                "trace":            trace_name,
                "window":           t,
                "sc_benefit":       sc_benefit,
                "sc_preferred":     1 if sc_benefit > 0 else 0,
                "byte_conc":        byte_conc,
            })

    df_sc = pd.DataFrame(rows)
    if df_sc.empty:
        print("    [skip]")
        return df_sc

    csv_path = os.path.join(out_dir, f"{label}_mech_sc_rule.csv")
    df_sc.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # find simple threshold on byte_conc
    thresholds = np.linspace(
        df_sc["byte_conc"].quantile(0.1),
        df_sc["byte_conc"].quantile(0.9), 50)
    best_acc, best_thr = 0, 0.5
    for thr in thresholds:
        pred = (df_sc["byte_conc"] >= thr).astype(int)
        acc = accuracy_score(df_sc["sc_preferred"], pred)
        if acc > best_acc:
            best_acc = acc
            best_thr = thr
    print(f"    sc=1 threshold: byte_conc >= {best_thr:.3f}  acc={best_acc:.1%}")

    _plot_sc_rule(df_sc, best_thr, best_acc, out_dir, label)
    return df_sc


def _plot_sc_rule(df_sc, best_thr, best_acc, out_dir, label):
    traces = df_sc["trace"].unique()
    n_tr = len(traces)
    fig, axes = plt.subplots(1, max(n_tr, 1),
                             figsize=(max(6, 5*n_tr), 5), squeeze=False)

    for ci, trace_name in enumerate(traces):
        sub = df_sc[df_sc["trace"] == trace_name]
        ax = axes[0, ci]
        xs = sub["byte_conc"].values.astype(float)
        ys = sub["sc_benefit"].values.astype(float)
        valid = ~(np.isnan(xs) | np.isnan(ys))
        colors = ["#388E3C" if b > 0 else "#F44336"
                  for b in sub["sc_benefit"].values]
        ax.scatter(xs[valid], ys[valid], c=[colors[i] for i in range(len(xs)) if valid[i]],
                   alpha=0.45, s=10)
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax.axvline(best_thr, color="#9C27B0", linewidth=1.2, linestyle="--",
                   label=f"threshold={best_thr:.3f} (acc={best_acc:.1%})")
        if valid.sum() > 4:
            r, p = spearmanr(xs[valid], ys[valid])
            ax.set_title(
                f"Trace: {trace_name}\n"
                f"byte_conc vs sc_benefit  (rho={r:.3f})\n"
                f"Green=sc=1 better, Red=sc=0 better"
            )
        ax.set_xlabel("wt_byte_concentration")
        ax.set_ylabel("sc_benefit = MR(sc=0) - MR(sc=1)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.suptitle(f"sc Decision Rule | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_sc_rule.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Step 2a: Label noise
# ============================================================

def analyze_label_noise(aligned_df, out_dir, label):
    """
    Distribution of score_margin per trace.
    Small margin = ambiguous label (multiple configs equally good).
    """
    print("  [Step 2a] Label noise analysis...")
    if "score_margin" not in aligned_df.columns:
        print("    [skip] No score_margin column.")
        return
    thresholds = [0.001, 0.005, 0.01, 0.05]
    noise_rows = []
    for trace_name in aligned_df["trace"].unique():
        sub = aligned_df[aligned_df["trace"] == trace_name]
        margins = sub["score_margin"].dropna().values
        row = {"trace": trace_name, "n_windows": len(margins)}
        for thr in thresholds:
            frac = float(np.mean(margins < thr))
            row[f"noise_frac_lt{thr}"] = frac
        row["margin_median"] = float(np.nanmedian(margins))
        row["margin_p25"]    = float(np.nanpercentile(margins, 25))
        noise_rows.append(row)

    noise_df = pd.DataFrame(noise_rows)
    csv_path = os.path.join(out_dir, f"{label}_mech_label_noise.csv")
    noise_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(noise_df.to_string(index=False))

    _plot_label_noise(aligned_df, thresholds, out_dir, label)
    return noise_df


def _plot_label_noise(aligned_df, thresholds, out_dir, label):
    traces = aligned_df["trace"].unique()
    n_tr = len(traces)
    fig, axes = plt.subplots(1, max(n_tr, 1),
                             figsize=(max(6, 5*n_tr), 5), squeeze=False)
    for ci, trace_name in enumerate(traces):
        sub = aligned_df[aligned_df["trace"] == trace_name]
        margins = sub["score_margin"].dropna().values
        ax = axes[0, ci]
        ax.hist(margins, bins=40, color="#9C27B0", alpha=0.7, edgecolor="white")
        for thr, col in zip(thresholds, ["#DC2626","#EA580C","#16A34A","#1976D2"]):
            frac = float(np.mean(margins < thr))
            ax.axvline(thr, color=col, linewidth=1.2, linestyle="--",
                       label=f"<{thr}: {frac:.0%}")
        ax.set_xlabel("score_margin")
        ax.set_ylabel("Count")
        ax.set_title(f"Trace: {trace_name}\nLabel noise distribution")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3, axis="y")
    plt.suptitle(f"Label Noise (score_margin) | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_label_noise.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Step 2b: Non-stationarity
# ============================================================

def analyze_nonstationarity(aligned_df, out_dir, label):
    """
    Compare optimal_K label distribution in first vs second half per trace.
    Also compute temporal autocorrelation of optimal_K sequence.
    """
    print("  [Step 2b] Non-stationarity analysis...")
    fn_used = [f for f in WINDOW_FEATURE_NAMES if f in aligned_df.columns]
    if not fn_used:
        print("    [skip] No features.")
        return

    le = LabelEncoder()
    rows = []
    acc_full_list, acc_fwd_list, trace_list = [], [], []

    for trace_name in aligned_df["trace"].unique():
        sub = aligned_df[aligned_df["trace"] == trace_name].sort_values("window")
        N = len(sub)
        if N < 20:
            continue
        X = sub[fn_used].values.astype(float)
        X = np.nan_to_num(X, nan=0.0)
        y = sub["optimal_K"].values.astype(str)
        y_enc = le.fit_transform(y)

        if len(np.unique(y_enc)) < 2:
            continue

        # temporal autocorrelation of optimal_K (lag-1)
        y_num = sub["optimal_K"].values.astype(float)
        if len(y_num) > 2:
            r_auto, _ = pearsonr(y_num[:-1], y_num[1:])
        else:
            r_auto = np.nan

        # label JS divergence first half vs second half
        half = N // 2
        y1 = y_enc[:half]
        y2 = y_enc[half:]
        classes = np.unique(y_enc)
        p1 = np.array([np.mean(y1 == c) for c in classes]) + EPSILON
        p2 = np.array([np.mean(y2 == c) for c in classes]) + EPSILON
        p1 /= p1.sum()
        p2 /= p2.sum()
        label_js = js_div(p1, p2)

        # forward accuracy: train on first half, test on second
        clf = DecisionTreeClassifier(max_depth=4, random_state=42)
        if len(np.unique(y_enc[:half])) < 2:
            acc_fwd = np.nan
        else:
            clf.fit(X[:half], y_enc[:half])
            preds = clf.predict(X[half:])
            acc_fwd = accuracy_score(y_enc[half:], preds)

        # full TimeSeriesSplit accuracy
        n_splits = min(5, max(2, N // 10))
        cv = TimeSeriesSplit(n_splits=n_splits)
        try:
            y_pred_cv = cross_val_predict(clf, X, y_enc, cv=cv)
            acc_full = accuracy_score(y_enc, y_pred_cv)
        except Exception:
            acc_full = np.nan

        rows.append({
            "trace":              trace_name,
            "n_windows":          N,
            "autocorr_K_lag1":    r_auto,
            "label_js_half":      label_js,
            "acc_timeseries_cv":  acc_full,
            "acc_forward":        acc_fwd,
            "acc_drop":           float(acc_full - acc_fwd)
                                  if not np.isnan(acc_full) and not np.isnan(acc_fwd)
                                  else np.nan,
        })
        acc_full_list.append(acc_full)
        acc_fwd_list.append(acc_fwd)
        trace_list.append(trace_name)

    nstat_df = pd.DataFrame(rows)
    csv_path = os.path.join(out_dir, f"{label}_mech_nonstationarity.csv")
    nstat_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    print(nstat_df.to_string(index=False))

    if trace_list:
        _plot_nonstationarity(aligned_df, nstat_df, out_dir, label)
    return nstat_df


def _plot_nonstationarity(aligned_df, nstat_df, out_dir, label):
    traces = nstat_df["trace"].values
    n_tr = len(traces)
    fig, axes = plt.subplots(2, max(n_tr, 1),
                             figsize=(max(6, 5*n_tr), 9), squeeze=False)

    for ci, trace_name in enumerate(traces):
        sub_a = aligned_df[aligned_df["trace"] == trace_name].sort_values("window")
        row = nstat_df[nstat_df["trace"] == trace_name].iloc[0]

        # top: optimal_K over time
        ax = axes[0, ci]
        ax.plot(sub_a["window"].values,
                sub_a["optimal_K"].values,
                color="#1976D2", linewidth=0.8, alpha=0.7)
        ax.axvline(len(sub_a) // 2, color="red", linestyle="--",
                   linewidth=1, label="half split")
        ax.set_title(
            f"Trace: {trace_name}\n"
            f"optimal_K over time  (autocorr={row['autocorr_K_lag1']:.3f})"
        )
        ax.set_xlabel("Window t")
        ax.set_ylabel("optimal K")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        # bottom: accuracy comparison
        ax2 = axes[1, ci]
        accs = [row["acc_timeseries_cv"], row["acc_forward"]]
        cols = ["#0D9488", "#EA580C"]
        lbls = ["TimeSeriesCV (full)", "Forward (1st->2nd half)"]
        bars = ax2.bar(lbls, accs, color=cols, alpha=0.8)
        for bar, val in zip(bars, accs):
            if not np.isnan(val):
                ax2.text(bar.get_x() + bar.get_width()/2, val + 0.01,
                         f"{val:.1%}", ha="center", fontsize=10)
        ax2.set_ylim(0, 1.1)
        ax2.set_ylabel("Accuracy")
        ax2.set_title(
            f"Trace: {trace_name}\n"
            f"Non-stationarity:  label_JS={row['label_js_half']:.4f}"
        )
        ax2.grid(True, alpha=0.3, axis="y")
        ax2.axhline(0.5, color="gray", linestyle=":", linewidth=0.8)

    plt.suptitle(f"Non-stationarity Analysis | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_nonstationarity.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Step 2c: Mutual Information
# ============================================================

def analyze_mutual_information(aligned_df, out_dir, label):
    """
    MI(feature_i, optimal_K), MI(feature_i, optimal_sc), MI(feature_i, optimal_ut)
    across all windows.
    """
    print("  [Step 2c] Mutual information analysis...")
    fn_used = [f for f in WINDOW_FEATURE_NAMES if f in aligned_df.columns]
    if not fn_used:
        print("    [skip] No features.")
        return

    X = aligned_df[fn_used].values.astype(float)
    X = np.nan_to_num(X, nan=0.0)
    results = {}
    for target_col in ["optimal_K", "optimal_sc", "optimal_ut"]:
        if target_col not in aligned_df.columns:
            continue
        y = LabelEncoder().fit_transform(aligned_df[target_col].astype(str))
        if len(np.unique(y)) < 2:
            continue
        mi = mutual_info_classif(X, y, discrete_features=False, random_state=42)
        results[target_col] = pd.Series(mi, index=fn_used)

    if not results:
        print("    [skip] No valid targets.")
        return

    mi_df = pd.DataFrame(results)
    csv_path = os.path.join(out_dir, f"{label}_mech_mi.csv")
    mi_df.to_csv(csv_path, encoding="utf-8-sig")

    _plot_mi(mi_df, out_dir, label)
    return mi_df


def _plot_mi(mi_df, out_dir, label):
    n_targets = len(mi_df.columns)
    fig, axes = plt.subplots(1, n_targets,
                             figsize=(max(6, 5*n_targets), 6), squeeze=False)
    colors_map = {
        "optimal_K":  "#1976D2",
        "optimal_sc": "#388E3C",
        "optimal_ut": "#F57C00",
    }
    for ci, target_col in enumerate(mi_df.columns):
        ax = axes[0, ci]
        vals = mi_df[target_col].sort_values(ascending=True)
        colors = [colors_map.get(target_col, "#555")] * len(vals)
        ax.barh(range(len(vals)), vals.values, color=colors, alpha=0.8)
        ax.set_yticks(range(len(vals)))
        ax.set_yticklabels(vals.index, fontsize=9)
        ax.set_xlabel("Mutual Information (bits)")
        ax.set_title(f"MI vs {target_col}")
        ax.grid(True, alpha=0.3, axis="x")
        # total MI
        total_mi = float(vals.sum())
        ax.text(0.98, 0.02, f"Total MI: {total_mi:.3f}",
                transform=ax.transAxes, ha="right", fontsize=9,
                bbox=dict(boxstyle="round,pad=0.3", fc="lightyellow"))

    plt.suptitle(f"Mutual Information: Features vs Targets | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_mi.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# M1 vs M2 benefit
# ============================================================

def analyze_m1_vs_m2_benefit(aligned_df, perf_matrix, out_dir, label):
    """
    benefit_mr(t) = MR_lru(t) - MR_best(t)
    Positive -> M2 (size partitioning) helps vs LRU baseline.
    Characterise which workload features predict large benefit.
    """
    print("  [M1 vs M2] Benefit characterisation...")
    fn_used = [f for f in WINDOW_FEATURE_NAMES if f in aligned_df.columns]
    rows = []
    for trace_name, tw_dict in perf_matrix.items():
        adf = aligned_df[aligned_df["trace"] == trace_name]
        for t, cond_dict in tw_dict.items():
            lru_mr = next((v["lru_mr"] for v in cond_dict.values()
                           if not np.isnan(v.get("lru_mr", np.nan))), np.nan)
            best_mr = min((v["mr"] for v in cond_dict.values()
                           if not np.isnan(v["mr"])), default=np.nan)
            if np.isnan(lru_mr) or np.isnan(best_mr):
                continue
            benefit_mr = float(lru_mr - best_mr)
            row_feat = adf[adf["window"] == t]
            if row_feat.empty:
                continue
            feat_vals = {fn: float(row_feat.iloc[0].get(fn, np.nan))
                         for fn in fn_used}
            rows.append({
                "trace":      trace_name,
                "window":     t,
                "lru_mr":     lru_mr,
                "best_mr":    best_mr,
                "benefit_mr": benefit_mr,
                **feat_vals,
            })

    ben_df = pd.DataFrame(rows)
    if ben_df.empty:
        print("    [skip] No LRU data available.")
        return ben_df

    csv_path = os.path.join(out_dir, f"{label}_mech_m1_vs_m2.csv")
    ben_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    # Spearman correlation of each feature with benefit
    corr_rows = []
    for fn in fn_used:
        xs = ben_df[fn].values.astype(float)
        ys = ben_df["benefit_mr"].values.astype(float)
        valid = ~(np.isnan(xs) | np.isnan(ys))
        if valid.sum() > 4:
            r, p = spearmanr(xs[valid], ys[valid])
            corr_rows.append({"feature": fn, "rho": r, "p_value": p})
    corr_df = pd.DataFrame(corr_rows).sort_values("rho", ascending=False)
    corr_csv = os.path.join(out_dir, f"{label}_mech_m1_vs_m2.csv")
    ben_df.to_csv(corr_csv, index=False, encoding="utf-8-sig")

    _plot_m1_vs_m2(ben_df, corr_df, out_dir, label)
    return ben_df


def _plot_m1_vs_m2(ben_df, corr_df, out_dir, label):
    traces = ben_df["trace"].unique()
    n_tr = len(traces)
    fig, axes = plt.subplots(2, max(n_tr, 1),
                             figsize=(max(7, 6*n_tr), 9), squeeze=False)

    for ci, trace_name in enumerate(traces):
        sub = ben_df[ben_df["trace"] == trace_name]

        ax = axes[0, ci]
        ws = sub["window"].values
        roll_ben = pd.Series(sub["benefit_mr"].values).rolling(20, min_periods=1).mean()
        ax.plot(ws, roll_ben.values, color="#7C3AED", linewidth=1.3,
                label="benefit_mr (rolling-20)")
        ax.fill_between(ws, 0, roll_ben.values,
                        where=roll_ben.values >= 0,
                        color="#7C3AED", alpha=0.25, label="M2 better")
        ax.fill_between(ws, 0, roll_ben.values,
                        where=roll_ben.values < 0,
                        color="#DC2626", alpha=0.25, label="LRU better")
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax.set_title(f"Trace: {trace_name}\nM2 benefit over time")
        ax.set_xlabel("Window t")
        ax.set_ylabel("MR_lru - MR_best")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        ax2 = axes[1, ci]
        # top feature for benefit
        if not corr_df.empty:
            best_fn = corr_df.iloc[0]["feature"]
            if best_fn in sub.columns:
                xs = sub[best_fn].values.astype(float)
                ys = sub["benefit_mr"].values.astype(float)
                valid = ~(np.isnan(xs) | np.isnan(ys))
                ax2.scatter(xs[valid], ys[valid], alpha=0.35, s=8, color="#7C3AED")
                if valid.sum() > 4:
                    r, p = spearmanr(xs[valid], ys[valid])
                    ax2.set_title(
                        f"Trace: {trace_name}\n"
                        f"{best_fn} vs benefit  (rho={r:.3f})"
                    )
                ax2.set_xlabel(best_fn)
                ax2.set_ylabel("M2 benefit_mr")
                ax2.axhline(0, color="gray", linestyle="--", linewidth=0.8)
                ax2.grid(True, alpha=0.3)

    plt.suptitle(f"M1 (LRU) vs M2 (size partition) Benefit | {label}", fontsize=11)
    plt.tight_layout()
    fpath = os.path.join(out_dir, f"{label}_mech_m1_vs_m2.png")
    plt.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"    Saved: {fpath}")


# ============================================================
# Summary text report
# ============================================================

def write_summary(sens_df, noise_df, nstat_df, mi_df, out_dir, label):
    lines = ["=" * 68]
    lines.append(f"  Mechanism Analysis Report  |  {label}")
    lines.append("=" * 68)

    if sens_df is not None and not sens_df.empty:
        lines.append("")
        lines.append("[Step 0] Parameter Sensitivity")
        lines.append("-" * 50)
        for trace_name in sens_df["trace"].unique():
            sub = sens_df[sens_df["trace"] == trace_name]
            km = float(np.nanmedian(sub["k_sensitivity"]))
            sm = float(np.nanmedian(sub["sc_sensitivity"]))
            um = float(np.nanmedian(sub["ut_sensitivity"]))
            bm = float(np.nanmedian(sub["m1_benefit"]))
            lines.append(f"  {trace_name}:")
            lines.append(f"    K-sens  median={km:.5f}")
            lines.append(f"    sc-sens median={sm:.5f}")
            lines.append(f"    ut-sens median={um:.5f}")
            lines.append(f"    M2 benefit (vs LRU) median={bm:.5f}")

    if noise_df is not None and not noise_df.empty:
        lines.append("")
        lines.append("[Step 2a] Label Noise")
        lines.append("-" * 50)
        for _, row in noise_df.iterrows():
            lines.append(f"  {row['trace']}:")
            for col in noise_df.columns:
                if col.startswith("noise_frac"):
                    lines.append(f"    {col}: {row[col]:.1%}")
            lines.append(f"    margin median: {row['margin_median']:.5f}")

    if nstat_df is not None and not nstat_df.empty:
        lines.append("")
        lines.append("[Step 2b] Non-stationarity")
        lines.append("-" * 50)
        for _, row in nstat_df.iterrows():
            lines.append(f"  {row['trace']}:")
            lines.append(f"    autocorr(K, lag1)={row['autocorr_K_lag1']:.3f}")
            lines.append(f"    label JS(half1||half2)={row['label_js_half']:.4f}")
            lines.append(f"    acc_TimeSeriesCV={row['acc_timeseries_cv']:.1%}")
            lines.append(f"    acc_forward={row['acc_forward']:.1%}")
            lines.append(f"    acc_drop={row['acc_drop']:+.1%}")

    if mi_df is not None and not mi_df.empty:
        lines.append("")
        lines.append("[Step 2c] Mutual Information (top-3 per target)")
        lines.append("-" * 50)
        for col in mi_df.columns:
            top3 = mi_df[col].nlargest(3)
            lines.append(f"  {col}:")
            for feat, val in top3.items():
                lines.append(f"    {feat:<28} MI={val:.4f}")

    lines.append("")
    lines.append("=" * 68)
    out_path = os.path.join(out_dir, f"{label}_mech_summary.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  Saved: {out_path}")


# ============================================================
# Main pipeline
# ============================================================

def run_mechanism_analysis(
    meta: pd.DataFrame,
    out_dir: str,
    label: str = "mech",
    target: str = "combined",
    w_mr: float = 0.5,
    w_bmr: float = 0.5,
):
    os.makedirs(out_dir, exist_ok=True)
    print(f"\n=== Mechanism Analysis: {label} ===")
    print(f"  Traces: {sorted(meta['trace'].unique())}")
    print(f"  Files:  {len(meta)}")

    print("\n[Loading performance matrix...]")
    aligned_df, perf_matrix, ref_dfs = load_perf_matrix(meta)
    if aligned_df.empty:
        print("  [ERROR] No valid windows. Check CSV format.")
        return

    print(f"  aligned windows: {len(aligned_df)}")

    sens_df   = analyze_sensitivity(perf_matrix, aligned_df, out_dir, label)
    gmm_df    = analyze_gmm_vs_k(ref_dfs, aligned_df, out_dir, label)
    sc_df     = analyze_sc_decision_rule(aligned_df, perf_matrix, out_dir, label)
    noise_df  = analyze_label_noise(aligned_df, out_dir, label)
    nstat_df  = analyze_nonstationarity(aligned_df, out_dir, label)
    mi_df     = analyze_mutual_information(aligned_df, out_dir, label)
    _         = analyze_m1_vs_m2_benefit(aligned_df, perf_matrix, out_dir, label)

    write_summary(sens_df, noise_df, nstat_df, mi_df, out_dir, label)
    print("\n=== Done ===")


def main():
    parser = argparse.ArgumentParser(
        description="Cache parameter mechanism analysis"
    )
    parser.add_argument("--dir",    required=True,
                        help="Directory containing SIZEDIVIDE CSV files")
    parser.add_argument("--out",    default="./output",
                        help="Output directory (default: ./output)")
    parser.add_argument("--label",  default="mech",
                        help="File name prefix (default: mech)")
    parser.add_argument("--target", default="combined",
                        choices=["mr", "bmr", "combined"],
                        help="Optimisation target (default: combined)")
    parser.add_argument("--w-mr",   type=float, default=0.5,
                        help="MR weight for combined score (default: 0.5)")
    parser.add_argument("--w-bmr",  type=float, default=0.5,
                        help="BMR weight for combined score (default: 0.5)")
    args = parser.parse_args()

    meta = collect_csvs(args.dir)
    run_mechanism_analysis(
        meta,
        out_dir=args.out,
        label=args.label,
        target=args.target,
        w_mr=args.w_mr,
        w_bmr=args.w_bmr,
    )


if __name__ == "__main__":
    main()
