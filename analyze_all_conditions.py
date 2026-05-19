"""
全条件 一括分析スクリプト（実CSVフォーマット対応版）
======================================================

ファイル名形式:
    {trace}-{param}-SIZEDIVIDE-{K}-{C}-{T}.csv
    例: mrsBlk-0.3-SIZEDIVIDE-8-0-9.csv
        T=9 が update_type=-1 を表す

ヘッダー列（条件はファイル名よりこちらを優先して読む）:
    head_1  : ごみデータ（無視）
    head_2  : 閾値個数 K
    head_3  : サイズ補正の有無 (0=なし / 1=あり)
    head_4  : 更新タイプ (-1=静的 / 0=逐次更新 / 1=WMA / 2=動的WMA)

update_type の意味:
    -1 : 初期分布を使い続ける（静的）
     0 : 取得した分布で毎回更新
     1 : WMA（加重移動平均）
     2 : 動的WMA（MAパラメータも動的更新）

列の意味（時刻 t の行）:
    miss_ratio            → hit_rate = 1 - miss_ratio
    miss_byte_ratio       → byte_miss_rate
    view_counts_now[_N]   → D_t   : 時刻tの実測サイズ分布
    view_counts_make[_N]  → D_{t-1}: 一つ前の時間の生の分布
    count[_N]             → MA_t  : 時刻tで更新したMA状態（t+1の閾値生成に使われる）
                             type-1: 初期分布固定（MA更新なし）
                             type0:  D_t（MAを使わず現在分布そのまま）
                             type1:  WMA: α*D_t + (1-α)*MA_{t-1}
                             type2:  動的WMA: αを適応的に更新
    count_make[_N]        → MA_{t-1}: 時刻tの閾値生成に使ったMA状態
                            = count_{t-1} の値（1ステップ前のMA状態）
    threshold_use[_N]     → 時刻tで使用した閾値（MA_{t-1}から生成）
    threshold_make[_N]    → 時刻t+1で使用する閾値（MA_tから生成済み）

時間的な因果関係:
    MA_{t-1} (count_make_t)
        ↓ 閾値生成
    threshold_use_t → キャッシュ動作 → hit_rate_t, byte_miss_rate_t
        ↑ 同時刻に観測
    D_t (view_counts_now_t)
        ↓ MA更新
    MA_t (count_t) → threshold_make_t（次時刻に使用）

分析する2種類のJSダイバージェンス:
    js_inherent  = JS(D_{t-1}, D_t)
                   = JS(view_counts_make, view_counts_now)
                   → トレース固有のドリフト量（全update_typeで同一）
    js_effective = JS(MA_{t-1}, D_t)
                   = JS(count_make, view_counts_now)
                   → 閾値生成入力と実際の分布のミスマッチ（手法ごとに異なる）
                   → 小さいほど「正確な分布で閾値を作れている」
                   type-1: 累積するため最大になる
                   type0:  js_inherentと同値（1ステップ遅れのみ）
                   type1,2: MAで平滑化→理論上はtype0以下

使い方:
    python analyze_all_conditions.py --dir ./results --out ./output

依存:
    pip install numpy pandas matplotlib scipy
"""

import argparse
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import entropy, pearsonr, spearmanr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import platform
_OS = platform.system()
if _OS == "Windows":
    matplotlib.rc("font", family="MS Gothic")
elif _OS == "Darwin":
    matplotlib.rc("font", family="Hiragino Sans")
else:
    from matplotlib import font_manager as _fm
    _jp = [f.name for f in _fm.fontManager.ttflist
           if any(k in f.name for k in ("Gothic","Mincho","Noto","IPAex"))]
    if _jp:
        matplotlib.rc("font", family=_jp[0])
matplotlib.rcParams["axes.unicode_minus"] = False

# ── 定数 ─────────────────────────────────────────────────────────────────────
EPSILON = 1e-10

# ファイル名中の update_type の変換（9 → -1）
UPDATE_TYPE_MAP = {9: -1, 0: 0, 1: 1, 2: 2}

UPDATE_TYPE_LABEL = {
    -1: "type-1: 静的",
     0: "type0:  逐次更新",
     1: "type1:  WMA",
     2: "type2:  動的WMA",
}
UPDATE_TYPE_COLOR = {-1: "#757575", 0: "#1976D2", 1: "#388E3C", 2: "#F57C00"}
UPDATE_TYPE_STYLE = {-1: ":",       0: "--",      1: "-",       2: "-."}
UPDATE_TYPE_MARKER= {-1: "s",       0: "^",       1: "o",       2: "D"}


# ══════════════════════════════════════════════════════════════════════════════
# ファイル名パース
# ══════════════════════════════════════════════════════════════════════════════

FNAME_RE = re.compile(
    r"SIZEDIVIDE-(?P<k>\d+)-(?P<c>\d+)-(?P<t>\d+)",
    re.IGNORECASE
)

def parse_filename(path: str) -> dict | None:
    """
    ファイル名から K, size_correction, update_type を抽出する。
    mrsBlk-0.3-SIZEDIVIDE-8-0-9.csv → k=8, c=0, t=-1
    """
    m = FNAME_RE.search(Path(path).stem)
    if not m:
        return None
    t_raw = int(m.group("t"))
    return {
        "filepath":        path,
        "k":               int(m.group("k")),
        "size_correction": int(m.group("c")),
        "update_type":     UPDATE_TYPE_MAP.get(t_raw, t_raw),
    }


def collect_csvs(dir_path: str) -> pd.DataFrame:
    rows = []
    for f in sorted(Path(dir_path).glob("*.csv")):
        info = parse_filename(str(f))
        if info is None:
            print(f"  [スキップ] パターン不一致: {f.name}")
        else:
            rows.append(info)
    if not rows:
        raise ValueError(f"{dir_path} に SIZEDIVIDE-K-C-T 形式のCSVが見つかりません。")
    df = pd.DataFrame(rows)
    print(f"  {len(df)} 件検出  "
          f"K={sorted(df['k'].unique())}  "
          f"corr={sorted(df['size_correction'].unique())}  "
          f"type={sorted(df['update_type'].unique())}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 列検出ユーティリティ
# ══════════════════════════════════════════════════════════════════════════════

def find_series_cols(df: pd.DataFrame, base: str) -> list[str]:
    """
    base, base_1, base_2, ... の連番列を返す。
    base__1（アンダースコア2つ）も base_1 として扱う。
    """
    cols = df.columns.tolist()

    # 全列名を正規化して base_N → N のマッピングを作る
    normalized = {}
    for c in cols:
        # base そのまま → インデックス 0
        if c == base:
            normalized[0] = c
            continue
        # base_N または base__N （N は整数）
        m = re.fullmatch(re.escape(base) + r"_{1,2}(\d+)", c)
        if m:
            normalized[int(m.group(1))] = c

    # 連番順にソートして返す
    return [normalized[i] for i in sorted(normalized)]


def to_prob(vals: np.ndarray) -> np.ndarray:
    v = vals.astype(float) + EPSILON
    return v / v.sum()


def js_div(p: np.ndarray, q: np.ndarray) -> float:
    m = 0.5 * (p + q)
    return float(0.5 * entropy(p, m) + 0.5 * entropy(q, m))


# ══════════════════════════════════════════════════════════════════════════════
# 1ファイル読み込みと指標計算
# ══════════════════════════════════════════════════════════════════════════════

def extract_conditions_from_header(df: pd.DataFrame) -> dict | None:
    """
    head_2, head_3, head_4 列から実験条件を読み取る。
    head_2 = K（閾値個数）
    head_3 = size_correction（0/1）
    head_4 = update_type（-1, 0, 1, 2）
    ファイル内で定数のはずなので先頭行から取得する。
    """
    try:
        k   = int(df["head_2"].iloc[0])
        sc  = int(df["head_3"].iloc[0])
        ut  = int(df["head_4"].iloc[0])
        return {"k": k, "size_correction": sc, "update_type": ut}
    except Exception:
        return None


def load_one(filepath: str) -> tuple[pd.DataFrame, dict]:
    """
    1CSVを読み込み、JS指標を計算する。
    戻り値: (結果DataFrame, 条件dict {k, size_correction, update_type})
    """
    df = pd.read_csv(filepath)
    df.columns = df.columns.str.strip().str.lower()

    # ── 実験条件の取得（head列優先、なければファイル名） ──────────────────
    cond = extract_conditions_from_header(df)
    if cond is None:
        # head列がなければファイル名から取得
        cond_from_name = parse_filename(filepath)
        if cond_from_name is None:
            raise ValueError(
                f"head_2/3/4 列もSIZEDIVIDE形式ファイル名もありません: {Path(filepath).name}"
            )
        cond = {k: v for k, v in cond_from_name.items() if k != "filepath"}

    # ── 必須列チェック ────────────────────────────────────────────────────
    for col in ["miss_ratio", "miss_byte_ratio"]:
        if col not in df.columns:
            raise KeyError(f"列 '{col}' がありません: {Path(filepath).name}")

    # ── 分布列を検出 ──────────────────────────────────────────────────────
    now_cols  = find_series_cols(df, "view_counts_now")   # D_t
    make_cols = find_series_cols(df, "view_counts_make")  # D_{t-1}（前時刻の生分布）
    used_cols = find_series_cols(df, "count_make")        # MA_{t-1}（閾値生成に使ったMA）
    ma_cols   = find_series_cols(df, "count")             # MA_t（現在のMA状態・履歴）

    for name, cols in [("view_counts_now", now_cols),
                       ("view_counts_make", make_cols),
                       ("count_make", used_cols)]:
        if not cols:
            raise KeyError(f"'{name}' 列が見つかりません: {Path(filepath).name}")

    # ── 性能指標 ──────────────────────────────────────────────────────────
    df["hit_rate"]       = 1.0 - df["miss_ratio"]
    df["byte_miss_rate"] = df["miss_byte_ratio"]

    # ── JS ダイバージェンスを行ごとに計算 ────────────────────────────────
    now_arr  = df[now_cols].values.astype(float)
    make_arr = df[make_cols].values.astype(float)
    used_arr = df[used_cols].values.astype(float)

    js_inh, js_eff = [], []
    for i in range(len(df)):
        d_t    = to_prob(now_arr[i])
        d_prev = to_prob(make_arr[i])
        d_used = to_prob(used_arr[i])
        js_inh.append(js_div(d_prev, d_t))  # JS(D_{t-1}, D_t) … 全手法共通
        js_eff.append(js_div(d_used, d_t))  # JS(MA_{t-1}, D_t) … 手法ごとに異なる

    df["js_inherent"]  = js_inh
    df["js_effective"] = js_eff

    # ── MA収束指標: JS(MA_t, D_t)  ──────────────────────────────────────
    # MA_t = count列。MAが現時点の真の分布にどれだけ近いかを示す。
    # js_effectiveはMA_{t-1}とD_tのズレなので、
    # js_ma_current = JS(MA_t, D_t) は「更新後のMAの精度」を示す。
    if ma_cols:
        ma_arr = df[ma_cols].values.astype(float)
        df["js_ma_current"] = [
            js_div(to_prob(ma_arr[i]), to_prob(now_arr[i]))
            for i in range(len(df))
        ]
    else:
        df["js_ma_current"] = np.nan

    # ── 不要な分布列を削除（メモリ節約） ─────────────────────────────────
    drop = now_cols + make_cols + used_cols + (ma_cols or [])
    df = df.drop(columns=[c for c in drop if c in df.columns], errors="ignore")

    return df, cond


# ══════════════════════════════════════════════════════════════════════════════
# 全CSVを読み込んで master DataFrame を構築
# ══════════════════════════════════════════════════════════════════════════════

def build_master(meta: pd.DataFrame) -> pd.DataFrame:
    dfs = []
    for i, row in meta.iterrows():
        print(f"\r  {i+1}/{len(meta)}: {Path(row['filepath']).name}", end="", flush=True)
        try:
            df, cond = load_one(row["filepath"])
            # head列から取得した条件を優先、なければファイル名由来の値を使用
            df["k"]               = cond.get("k",               int(row["k"]))
            df["size_correction"] = cond.get("size_correction", int(row["size_correction"]))
            df["update_type"]     = cond.get("update_type",     int(row["update_type"]))
            dfs.append(df)
        except Exception as e:
            print(f"\n  [エラー] {row['filepath']}: {e}")
    print()
    return pd.concat(dfs, ignore_index=True)


def aggregate(master: pd.DataFrame) -> pd.DataFrame:
    recs = []
    for (k, sc, ut), g in master.groupby(["k","size_correction","update_type"]):
        r_hr,  p_hr  = (pearsonr( g["js_effective"], g["hit_rate"])       if len(g)>2 else (np.nan,np.nan))
        r_bmr, p_bmr = (pearsonr( g["js_effective"], g["byte_miss_rate"]) if len(g)>2 else (np.nan,np.nan))
        recs.append({
            "k": k, "size_correction": sc, "update_type": ut,
            "mean_HR":              g["hit_rate"].mean(),
            "mean_BMR":             g["byte_miss_rate"].mean(),
            "mean_js_inherent":     g["js_inherent"].mean(),
            "mean_js_effective":    g["js_effective"].mean(),   # JS(MA_{t-1}, D_t)
            "mean_js_ma_current":   g["js_ma_current"].mean() if "js_ma_current" in g else np.nan,
            "pearson_r_HR_eff":     r_hr,
            "pearson_p_HR_eff":     p_hr,
            "pearson_r_BMR_eff":    r_bmr,
            "n_windows":            len(g),
        })
    return pd.DataFrame(recs)


# ══════════════════════════════════════════════════════════════════════════════
# 図0（追加）: MA収束性の比較 ← count列で初めて可能になる分析
# ══════════════════════════════════════════════════════════════════════════════

def plot_ma_convergence(agg: pd.DataFrame, out_dir: str, trace: str):
    """
    MA状態（count列）が真の分布 D_t にどれだけ近いかを手法間で比較する。

    3指標を並べて比較:
      js_inherent    : JS(D_{t-1}, D_t)       … 生のドリフト量（全手法共通）
      js_effective   : JS(MA_{t-1}, D_t)      … 閾値生成入力のズレ（手法ごと）
      js_ma_current  : JS(MA_t, D_t)          … 更新直後のMAの精度（手法ごと）

    理論的な大小関係:
      type-1: js_effective >> js_inherent（累積）
              js_ma_current = js_effective（MAを更新しない）
      type0:  js_effective ≈ js_inherent（1ステップ遅れのみ）
              js_ma_current ≈ 0（current = D_t そのもの）
      type1:  js_effective < js_inherent（MAで平滑化）
              js_ma_current < js_effective（更新後はより近い）
      type2:  理論上 type1 以下
    """
    ut_list = sorted(agg["update_type"].unique())

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    metrics = [
        ("mean_js_inherent",   "JS(D_{t-1}, D_t)\n固有ドリフト（全手法共通）"),
        ("mean_js_effective",  "JS(MA_{t-1}, D_t)\n閾値生成入力のズレ"),
        ("mean_js_ma_current", "JS(MA_t, D_t)\n更新直後のMA精度"),
    ]

    for ax, (col, title) in zip(axes, metrics):
        vals, labels, colors = [], [], []
        for ut in ut_list:
            sub = agg[agg["update_type"] == ut]
            if sub.empty or col not in sub or sub[col].isna().all():
                continue
            vals.append(sub[col].mean())
            labels.append(UPDATE_TYPE_LABEL.get(ut, f"type{ut}"))
            colors.append(UPDATE_TYPE_COLOR.get(ut, "#333"))

        bars = ax.bar(range(len(vals)), vals, color=colors,
                      edgecolor="white", alpha=0.85)
        for bar, v in zip(bars, vals):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(vals)*0.01,
                    f"{v:.5f}", ha="center", va="bottom", fontsize=8)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=20, ha="right", fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel("平均 JS ダイバージェンス（小さいほど良い）", fontsize=8)
        ax.grid(True, alpha=0.3, axis="y")
        ax.set_ylim(bottom=0, top=max(vals) * 1.15 if vals else 1)

    plt.suptitle(f"[MA収束性] 3種のJSダイバージェンス比較  |  Trace: {trace}\n"
                 f"type-1 では js_effective が最大（累積ズレ）、"
                 f"type1・2では js_ma_current < js_effective（更新で近づく）",
                 fontsize=11)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{trace}_fig0_ma_convergence.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  保存: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# 図1（論文核心）: js_effective の手法間比較 ← 最も重要な図
# ══════════════════════════════════════════════════════════════════════════════

def plot_effective_mismatch(agg: pd.DataFrame, out_dir: str, trace: str):
    """
    同じ js_inherent（トレース固有ドリフト）に対して、
    各手法の js_effective（実効ミスマッチ）がどれだけ小さいかを比較する。

    期待: type-1 >> type0 > type1 > type2

    さらに: js_effective が小さい手法ほど性能（HR/BMR）が良いことを示す。
    """
    # 最良Kと補正を選定
    best_row = agg.loc[agg["mean_HR"].idxmax()]
    best_k, best_sc = int(best_row["k"]), int(best_row["size_correction"])

    ut_list = sorted(agg["update_type"].unique())

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    # ── (a) js_effective の箱ひげ図（手法間比較） ────────────────────────
    ax = axes[0]
    data_to_plot = []
    labels_plot  = []
    for ut in ut_list:
        sub = agg[(agg["update_type"]==ut) &
                  (agg["k"]==best_k) &
                  (agg["size_correction"]==best_sc)]
        if sub.empty:
            continue
        data_to_plot.append(sub["mean_js_effective"].values)
        labels_plot.append(UPDATE_TYPE_LABEL.get(ut, f"type{ut}"))

    bars = ax.bar(range(len(data_to_plot)),
                  [d[0] if len(d)==1 else np.mean(d) for d in data_to_plot],
                  color=[UPDATE_TYPE_COLOR.get(ut,"#333") for ut in ut_list[:len(data_to_plot)]],
                  edgecolor="white", alpha=0.85)
    ax.set_xticks(range(len(labels_plot)))
    ax.set_xticklabels(labels_plot, rotation=20, ha="right", fontsize=8)
    ax.set_ylabel("Mean JS_effective  (小さいほど良い)", fontsize=9)
    ax.set_title(f"(a) 実効ミスマッチ JS_effective\nK={best_k}, corr={best_sc}", fontsize=10)
    ax.grid(True, alpha=0.3, axis="y")

    # ── (b) js_effective vs HR（手法別散布図） ───────────────────────────
    ax = axes[1]
    for ut in ut_list:
        sub = agg[agg["update_type"]==ut].sort_values("k")
        if sub.empty:
            continue
        for sc in sorted(sub["size_correction"].unique()):
            ssub = sub[sub["size_correction"]==sc]
            ls   = "-" if sc==1 else "--"
            ax.scatter(ssub["mean_js_effective"], ssub["mean_HR"],
                       color=UPDATE_TYPE_COLOR.get(ut,"#333"),
                       marker=UPDATE_TYPE_MARKER.get(ut,"o"), s=40, alpha=0.7)
            ax.plot(ssub["mean_js_effective"], ssub["mean_HR"],
                    color=UPDATE_TYPE_COLOR.get(ut,"#333"),
                    linestyle=ls, linewidth=0.8, alpha=0.5)

    # 全体回帰線
    if len(agg) > 2:
        r, p = pearsonr(agg["mean_js_effective"], agg["mean_HR"])
        coeffs = np.polyfit(agg["mean_js_effective"], agg["mean_HR"], 1)
        x_line = np.linspace(agg["mean_js_effective"].min(),
                              agg["mean_js_effective"].max(), 100)
        ax.plot(x_line, np.polyval(coeffs, x_line),
                color="red", linestyle="--", linewidth=1.2,
                label=f"回帰線 r={r:+.3f}")
        ax.legend(fontsize=8)
    ax.set_xlabel("Mean JS_effective", fontsize=9)
    ax.set_ylabel("Mean Hit Rate", fontsize=9)
    ax.set_title("(b) JS_effective vs Hit Rate\n（全K・全補正・全手法）", fontsize=10)
    ax.grid(True, alpha=0.3)

    # ── (c) js_inherent vs js_effective（手法別） ───────────────────────
    ax = axes[2]
    ax.plot([0, agg["mean_js_inherent"].max()],
            [0, agg["mean_js_inherent"].max()],
            color="gray", linestyle=":", linewidth=1, label="js_eff = js_inh (type0相当)")
    for ut in ut_list:
        sub = agg[agg["update_type"]==ut]
        if sub.empty:
            continue
        ax.scatter(sub["mean_js_inherent"], sub["mean_js_effective"],
                   color=UPDATE_TYPE_COLOR.get(ut,"#333"),
                   label=UPDATE_TYPE_LABEL.get(ut,f"type{ut}"),
                   s=30, alpha=0.8)

    ax.set_xlabel("JS_inherent（トレース固有ドリフト）", fontsize=9)
    ax.set_ylabel("JS_effective（実効ミスマッチ）", fontsize=9)
    ax.set_title("(c) 固有ドリフト vs 実効ミスマッチ\n対角線下 = 手法がドリフトを吸収できている", fontsize=10)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.3)

    plt.suptitle(f"[核心図] 実効ミスマッチ分析  |  Trace: {trace}", fontsize=12)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{trace}_fig1_effective_mismatch.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  保存: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# 図2: RQ4 最適K（HR/BMR曲線）
# ══════════════════════════════════════════════════════════════════════════════

def plot_k_curves(agg: pd.DataFrame, out_dir: str, trace: str):
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for sc in sorted(agg["size_correction"].unique()):
        ls = "-" if sc==1 else "--"
        sc_label = "補正あり" if sc else "補正なし"
        for ut in sorted(agg["update_type"].unique()):
            sub = agg[(agg["update_type"]==ut)&(agg["size_correction"]==sc)].sort_values("k")
            if sub.empty:
                continue
            color = UPDATE_TYPE_COLOR.get(ut,"#333")
            label = f"{UPDATE_TYPE_LABEL.get(ut,f'type{ut}')} {sc_label}"
            axes[0].plot(sub["k"], sub["mean_HR"],
                         color=color, linestyle=ls, linewidth=1.5,
                         marker="o", markersize=4, label=label, alpha=0.85)
            axes[1].plot(sub["k"], sub["mean_BMR"],
                         color=color, linestyle=ls, linewidth=1.5,
                         marker="o", markersize=4, label=label, alpha=0.85)

    for ax, title in [(axes[0],"Mean Hit Rate"), (axes[1],"Mean Byte Miss Rate")]:
        ax.set_xlabel("Threshold Count K", fontsize=10)
        ax.set_ylabel(title, fontsize=10)
        ax.set_title(title, fontsize=11)
        ax.set_xticks(range(1, 9))
        ax.legend(fontsize=7, ncol=2)
        ax.grid(True, alpha=0.3)

    plt.suptitle(f"[RQ4] 最適閾値数 K  |  Trace: {trace}", fontsize=12)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{trace}_fig2_optimal_k.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  保存: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# 図3: RQ3 サイズ補正の効果
# ══════════════════════════════════════════════════════════════════════════════

def plot_size_correction(agg: pd.DataFrame, out_dir: str, trace: str):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    ut_list = sorted(agg["update_type"].unique())

    for ax, metric, title, hypothesis in [
        (axes[0], "mean_HR",  "Hit Rate",
         "仮説: 補正なし(0) ≥ 補正あり(1)"),
        (axes[1], "mean_BMR", "Byte Miss Rate",
         "仮説: 補正あり(1) ≤ 補正なし(0)"),
    ]:
        x = np.arange(len(ut_list))
        w = 0.35
        for i, sc in enumerate([0, 1]):
            vals = []
            for ut in ut_list:
                sub = agg[(agg["update_type"]==ut)&(agg["size_correction"]==sc)]
                vals.append(sub[metric].mean() if not sub.empty else 0)
            ax.bar(x + (i-0.5)*w, vals, width=w,
                   label=f"補正{'あり' if sc else 'なし'}",
                   color=["#90CAF9","#1976D2"][i], alpha=0.85, edgecolor="white")
        ax.set_xticks(x)
        ax.set_xticklabels([UPDATE_TYPE_LABEL.get(u,f"type{u}") for u in ut_list],
                            rotation=15, ha="right", fontsize=8)
        ax.set_title(f"{title}\n{hypothesis}", fontsize=10)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, axis="y")

    plt.suptitle(f"[RQ3] サイズ補正の効果  |  Trace: {trace}", fontsize=12)
    plt.tight_layout()
    path = os.path.join(out_dir, f"{trace}_fig3_size_correction.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  保存: {path}")


# ══════════════════════════════════════════════════════════════════════════════
# テキストサマリー
# ══════════════════════════════════════════════════════════════════════════════

def print_summary(agg: pd.DataFrame, trace: str):
    print(f"\n{'='*70}")
    print(f"  分析サマリー  |  Trace: {trace}")
    print(f"{'='*70}")

    # RQ1
    best = agg.loc[agg["mean_HR"].idxmax()]
    print(f"\n  ─── RQ1: M2全体での最良設定 ────────────────────────────────")
    print(f"  最高HR = {best['mean_HR']:.4f}  "
          f"(K={int(best['k'])}, 補正={'あり' if best['size_correction'] else 'なし'}, "
          f"更新type={int(best['update_type'])})")

    # RQ2: 実効ミスマッチの比較
    print(f"\n  ─── RQ2: 実効ミスマッチ JS_effective 比較 ──────────────────")
    print(f"  {'更新タイプ':<25} {'mean JS_eff':>12} {'mean HR':>9} {'mean BMR':>10}")
    print(f"  {'-'*58}")
    for ut in sorted(agg["update_type"].unique()):
        sub = agg[agg["update_type"]==ut]
        label = UPDATE_TYPE_LABEL.get(ut, f"type{ut}")
        print(f"  {label:<25} {sub['mean_js_effective'].mean():>12.5f}"
              f" {sub['mean_HR'].mean():>9.4f} {sub['mean_BMR'].mean():>10.4f}")

    # RQ2: ドリフトと性能の相関
    print(f"\n  ─── RQ2: JS_effective と性能の Pearson 相関 ────────────────")
    best_k  = int(agg.loc[agg["mean_HR"].idxmax(), "k"])
    best_sc = int(agg.loc[agg["mean_HR"].idxmax(), "size_correction"])
    for ut in sorted(agg["update_type"].unique()):
        sub = agg[(agg["update_type"]==ut)&(agg["k"]==best_k)&(agg["size_correction"]==best_sc)]
        if sub.empty or sub["pearson_r_HR_eff"].isna().all():
            continue
        r  = sub["pearson_r_HR_eff"].values[0]
        p  = sub["pearson_p_HR_eff"].values[0]
        verdict = ("✅ 有意・仮説通り" if p < 0.05 and r < 0
                   else "⚠️  有意差なし"  if p >= 0.05
                   else "❌ 有意・逆")
        label = UPDATE_TYPE_LABEL.get(ut, f"type{ut}")
        print(f"  {label:<25} r={r:+.3f}  p={p:.3f}  {verdict}")

    # RQ4: 最適K
    print(f"\n  ─── RQ4: 更新タイプ別 最適K（HR基準） ──────────────────────")
    for ut in sorted(agg["update_type"].unique()):
        sub = agg[agg["update_type"]==ut]
        br  = sub.loc[sub["mean_HR"].idxmax()]
        print(f"  {UPDATE_TYPE_LABEL.get(ut,f'type{ut}'):<25}"
              f" 最適K={int(br['k'])}  HR={br['mean_HR']:.4f}"
              f"  (補正={'あり' if br['size_correction'] else 'なし'})")

    print(f"\n{'='*70}")


# ══════════════════════════════════════════════════════════════════════════════
# エントリーポイント
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="全条件 一括分析（SIZEDIVIDE形式）")
    parser.add_argument("--dir",   required=True, help="CSVが入ったディレクトリ")
    parser.add_argument("--trace", default="trace",
                        help="トレース名（グラフタイトル用）。省略時はディレクトリ名")
    parser.add_argument("--out",   default="./output", help="出力ディレクトリ")
    args = parser.parse_args()

    trace = args.trace if args.trace != "trace" else Path(args.dir).name
    os.makedirs(args.out, exist_ok=True)

    print("[1/4] CSVを検出中...")
    meta = collect_csvs(args.dir)

    print("\n[2/4] 全CSVを読み込み中...")
    master = build_master(meta)
    print(f"  合計 {len(master):,} 行（時間窓×条件）")

    print("\n[3/4] 条件別に集計中...")
    agg = aggregate(master)
    agg.to_csv(os.path.join(args.out, f"{trace}_agg.csv"),
               index=False, encoding="utf-8-sig")

    print("\n[4/4] グラフ生成中...")
    plot_ma_convergence(agg,     args.out, trace)   # 図0: MA収束性（count列が活きる）
    plot_effective_mismatch(agg, args.out, trace)   # 図1: 実効ミスマッチ（論文核心）
    plot_k_curves(agg,           args.out, trace)   # 図2: 最適K
    plot_size_correction(agg,    args.out, trace)   # 図3: サイズ補正効果

    print_summary(agg, trace)

    print(f"\n✅ 完了。出力: {args.out}/")
    print(f"   {trace}_fig0_ma_convergence.png      ← MA収束性（count列）")
    print(f"   {trace}_fig1_effective_mismatch.png  ← 論文の核心図")
    print(f"   {trace}_fig2_optimal_k.png           ← RQ4 最適K曲線")
    print(f"   {trace}_fig3_size_correction.png     ← RQ3 サイズ補正効果")
    print(f"   {trace}_agg.csv                      ← 集計テーブル（全条件）")


if __name__ == "__main__":
    main()
