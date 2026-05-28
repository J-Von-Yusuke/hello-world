"""
cache_common.py
===============
全スクリプト共通のユーティリティ

【binning 設計方針】
  1KiB (2^10) から 8GiB (2^33) まで 2 の指数乗ごとに区切る。
  → 24 本の閾値 → 25 個の bin

  bin 0  : size < 1KiB           (< 2^10)
  bin 1  : 1KiB  ≤ size < 2KiB  (2^10 – 2^11)
  bin 2  : 2KiB  ≤ size < 4KiB  (2^11 – 2^12)
  ...
  bin 23 : 4GiB  ≤ size < 8GiB  (2^32 – 2^33)
  bin 24 : 8GiB ≤ size           (≥ 2^33)

  利点:
    - 事前仮定なし。自然な境界をデータから発見できる
    - 隣接 bin を後から統合できる（4倍幅 bin = 2倍幅 bin × 2 を足すだけ）
    - キャッシュ研究の標準的な手法で他論文と比較しやすい

【使い方】
  from cache_common import POW2_THRESHOLDS, get_size_class, build_class_labels
  from cache_common import aggregate_matrix, aggregate_df_by_log2_group
"""

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────
# matplotlib 日本語フォントセットアップ
# ─────────────────────────────────────────────

def setup_matplotlib_font() -> bool:
    """
    日本語フォントを matplotlib に設定し、PDF/EPS 出力時のフォント埋め込みを有効化する。

    設定優先順:
      0. プロジェクト同梱フォント（fonts/*.ttf / fonts/*.otf）← 最優先
             python download_font.py を一度実行すると fonts/ に配置される
      1. japanize-matplotlib がインストール済みなら自動設定
             pip install japanize-matplotlib
      2. フォントマネージャに登録済みのシステムフォントを名前で探索
             Windows : Meiryo / BIZ UDGothic / Yu Gothic / MS Gothic
             macOS   : Hiragino Sans / Apple SD Gothic Neo
             Linux   : Noto Sans CJK JP / IPAexGothic / IPAGothic
      3. Windows フォントディレクトリのファイルを直接探索して登録

    フォント埋め込み設定（PNG では不要だが PDF/EPS 出力に備えて常に設定）:
      pdf.fonttype = 42  → TrueType フォントを PDF に埋め込む
      ps.fonttype  = 42  → PostScript でも同様

    戻り値:
      True  : 日本語フォントの設定に成功
      False : 日本語フォントが見つからなかった（文字化けの可能性あり）
    """
    import matplotlib
    import matplotlib.font_manager as fm
    import os
    import platform
    from pathlib import Path

    # PDF / EPS 出力へのフォント埋め込みは成否によらず常に設定
    matplotlib.rcParams["pdf.fonttype"] = 42
    matplotlib.rcParams["ps.fonttype"]  = 42

    def _try_font_file(fpath: str) -> bool:
        """フォントファイルを登録して font.family に設定する。成功したら True。"""
        try:
            fm.fontManager.addfont(fpath)
            prop = fm.FontProperties(fname=fpath)
            matplotlib.rcParams["font.family"] = prop.get_name()
            return True
        except Exception:
            return False

    # ── 方法0: プロジェクト同梱フォント（fonts/ ディレクトリ）────────
    # cache_common.py と同じ場所にある fonts/ を探す。
    # download_font.py を実行すると NotoSansJP-Regular.ttf が置かれる。
    _here = Path(__file__).parent
    _fonts_dir = _here / "fonts"
    if _fonts_dir.is_dir():
        for _fp in sorted(_fonts_dir.glob("*.ttf")) + sorted(_fonts_dir.glob("*.otf")):
            if _fp.stat().st_size > 200_000 and _try_font_file(str(_fp)):
                return True

    # ── 方法1: japanize-matplotlib ────────────────────────────────
    try:
        import japanize_matplotlib  # noqa: F401  # pip install japanize-matplotlib
        return True
    except ImportError:
        pass

    # ── 方法2: フォントマネージャに登録済みのシステムフォントを探索 ──
    system = platform.system()
    if system == "Windows":
        candidates = ["Meiryo", "BIZ UDGothic", "Yu Gothic", "MS Gothic", "MS PGothic"]
    elif system == "Darwin":
        candidates = ["Hiragino Sans", "Hiragino Kaku Gothic Pro", "Apple SD Gothic Neo", "Osaka"]
    else:
        candidates = ["Noto Sans CJK JP", "IPAexGothic", "IPAGothic",
                      "TakaoGothic", "VL Gothic", "Noto Sans JP"]

    available = {f.name for f in fm.fontManager.ttflist}
    for font_name in candidates:
        if font_name in available:
            matplotlib.rcParams["font.family"] = font_name
            return True

    # ── 方法3: フォントファイルを直接探索して登録 ──────────────────
    if system == "Windows":
        font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        font_files = [
            "meiryo.ttc", "meiryob.ttc",
            "BIZUDGothic-Regular.ttf",
            "YuGothR.ttc", "YuGothM.ttc", "YuGothB.ttc",
            "yugothic.ttc",
            "msgothic.ttc",
        ]
        for fname in font_files:
            fpath = os.path.join(font_dir, fname)
            if os.path.isfile(fpath) and _try_font_file(fpath):
                return True

    elif system == "Darwin":
        for font_dir in ["/System/Library/Fonts", "/Library/Fonts",
                         os.path.expanduser("~/Library/Fonts")]:
            for fname in ["ヒラギノ角ゴシック W3.ttc", "Hiragino Sans GB.ttc",
                          "HiraginoSans-W3.ttc"]:
                fpath = os.path.join(font_dir, fname)
                if os.path.isfile(fpath) and _try_font_file(fpath):
                    return True

    else:
        # ── Linux: 方法3a fc-list で日本語対応フォントを取得 ────────
        try:
            import subprocess
            res = subprocess.run(
                ["fc-list", ":lang=ja", "--format=%{file}\n"],
                capture_output=True, text=True, timeout=5,
            )
            for line in res.stdout.splitlines():
                fpath = line.strip()
                if fpath and os.path.isfile(fpath) and _try_font_file(fpath):
                    return True
        except Exception:
            pass

        # ── Linux: 方法3b フォントディレクトリを再帰スキャン ─────────
        # CJK/JP 系フォントファイル名のパターン（優先度順）
        _JP_PATTERNS = [
            # IPAfont
            "ipag.ttf", "ipagp.ttf", "ipam.ttf", "ipamp.ttf",
            "ipaexg.ttf", "ipaexm.ttf",
            # Noto CJK
            "NotoSansCJKjp-Regular.otf", "NotoSansCJK-Regular.ttc",
            "NotoSansJP-Regular.ttf", "NotoSansCJKjp-Regular.ttf",
            "NotoSerifCJKjp-Regular.otf",
            # Takao
            "TakaoGothic.ttf", "TakaoPGothic.ttf", "TakaoExGothic.ttf",
            # VL Gothic
            "VL-Gothic-Regular.ttf", "VL-PGothic-Regular.ttf",
            # Droid (フォールバック: CJK 含む)
            "DroidSansFallbackFull.ttf", "DroidSansFallback.ttf",
        ]
        _FONT_ROOTS = [
            "/usr/share/fonts",
            "/usr/local/share/fonts",
            os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts"),
        ]
        # まず名前が一致するものを優先
        for root in _FONT_ROOTS:
            if not os.path.isdir(root):
                continue
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if fname in _JP_PATTERNS:
                        fpath = os.path.join(dirpath, fname)
                        if _try_font_file(fpath):
                            return True
        # 次に名前パターンで広く検索
        _JP_KEYWORDS = ["cjk", "jp", "japanese", "ipa", "gothic", "mincho",
                        "takao", "vlgoth", "droid"]
        for root in _FONT_ROOTS:
            if not os.path.isdir(root):
                continue
            for dirpath, _, filenames in os.walk(root):
                for fname in filenames:
                    if not fname.lower().endswith((".ttf", ".otf", ".ttc")):
                        continue
                    fname_l = fname.lower()
                    if any(k in fname_l for k in _JP_KEYWORDS):
                        fpath = os.path.join(dirpath, fname)
                        size = os.path.getsize(fpath)
                        if size > 200_000 and _try_font_file(fpath):
                            return True

    print(
        "  [font] 日本語フォントが見つかりませんでした。\n"
        "         以下のいずれかを実行してください:\n"
        "           python download_font.py                      # フォントをダウンロードして同梱\n"
        "           pip install japanize-matplotlib              # パッケージとして追加\n"
        "           sudo apt install fonts-noto-cjk              # Ubuntu: Noto CJK\n"
        "           sudo apt install fonts-ipafont-gothic        # Ubuntu: IPAex ゴシック"
    )
    return False


# ─────────────────────────────────────────────
# デフォルト閾値: 1KiB〜8GiB を 2 の指数乗で
# ─────────────────────────────────────────────
POW2_THRESHOLDS = [2 ** i for i in range(10, 34)]
# = [1024, 2048, 4096, ..., 4294967296, 8589934592]
# 24 本の閾値 → 25 bin

N_BINS = len(POW2_THRESHOLDS) + 1  # 25


# ─────────────────────────────────────────────
# サイズクラス判定
# ─────────────────────────────────────────────

def get_size_class(size: int, thresholds: list = None) -> int:
    """
    size が属する bin インデックスを返す。
    thresholds が None のときは POW2_THRESHOLDS を使う。

    2 の指数乗閾値に対しては bit_length() で O(1) 判定。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS

    # 高速パス: 閾値が 2 の指数乗であれば bit_length で計算
    if thresholds is POW2_THRESHOLDS or thresholds == POW2_THRESHOLDS:
        if size < thresholds[0]:          # < 1KiB
            return 0
        if size >= thresholds[-1]:        # ≥ 8GiB
            return len(thresholds)
        # size が属する bin = bit_length(size) - 10
        # 例: 1024 (2^10) → bit_length=11 → 11-10=1 → bin 1
        bl = size.bit_length()
        return bl - 10  # = log2(floor(size)) - 9

    # 汎用パス
    for i, t in enumerate(thresholds):
        if size < t:
            return i
    return len(thresholds)


def get_size_class_vectorized(sizes: np.ndarray,
                               thresholds: list = None) -> np.ndarray:
    """numpy 配列に対してサイズクラスを一括計算する（高速）"""
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    t = np.array(thresholds, dtype=np.int64)
    # searchsorted: sizes[i] が入る最初のインデックスを返す
    return np.searchsorted(t, sizes, side="right").astype(np.int32)


# ─────────────────────────────────────────────
# ラベル生成
# ─────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    """バイト数を人間が読みやすい文字列に変換する"""
    for div, unit in [(1 << 30, "GiB"), (1 << 20, "MiB"),
                       (1 << 10, "KiB"), (1, "B")]:
        if b >= div and b % div == 0:
            return f"{b // div}{unit}"
    return f"{b}B"


def build_class_labels(thresholds: list = None) -> dict:
    """
    {bin_index: label_string} の辞書を返す。

    例 (POW2_THRESHOLDS):
      {0: "<1KiB", 1: "1-2KiB", 2: "2-4KiB", ..., 24: "≥8GiB"}
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    boundaries = [0] + list(thresholds) + [None]
    labels = {}
    for i in range(len(boundaries) - 1):
        lo, hi = boundaries[i], boundaries[i + 1]
        if lo == 0 and hi is not None:
            labels[i] = f"<{_fmt_bytes(hi)}"
        elif hi is None:
            labels[i] = f"≥{_fmt_bytes(lo)}"
        else:
            lo_s, hi_s = _fmt_bytes(lo), _fmt_bytes(hi)
            # 単位が同じなら数値だけ省略して "1-2KiB" のように表示
            lo_val = lo_s.rstrip("BGiKMib")
            hi_val = hi_s.rstrip("BGiKMib")
            lo_unit = lo_s[len(lo_val):]
            hi_unit = hi_s[len(hi_val):]
            if lo_unit == hi_unit:
                labels[i] = f"{lo_val}-{hi_val}{hi_unit}"
            else:
                labels[i] = f"{lo_s}-{hi_s}"
    return labels


# ─────────────────────────────────────────────
# bin の集約ユーティリティ
# ─────────────────────────────────────────────

def aggregate_bins(n_merge: int, thresholds: list = None) -> list:
    """
    隣接 n_merge 本の bin を1つに統合した粗い閾値リストを返す。

    例: n_merge=2 → 2倍幅 (1-2KiB + 2-4KiB → 1-4KiB)
        n_merge=4 → 4倍幅

    戻り値: 集約後の閾値リスト
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    # 元の閾値を n_merge おきに間引く
    return thresholds[n_merge - 1::n_merge]


def aggregate_matrix(matrix: np.ndarray,
                     thresholds: list = None,
                     n_merge: int = 2) -> np.ndarray:
    """
    退避行列（n_bins × n_bins）を n_merge 本ごとに集約する。

    例: 25×25 の行列を n_merge=2 で集約 → 13×13 に縮小。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    n = matrix.shape[0]
    coarse_thresholds = aggregate_bins(n_merge, thresholds)
    n_coarse = len(coarse_thresholds) + 1

    result = np.zeros((n_coarse, n_coarse), dtype=matrix.dtype)
    for i_fine in range(n):
        i_coarse = min(i_fine // n_merge, n_coarse - 1)
        for j_fine in range(n):
            j_coarse = min(j_fine // n_merge, n_coarse - 1)
            result[i_coarse, j_coarse] += matrix[i_fine, j_fine]
    return result


def aggregate_df_by_merge(df: pd.DataFrame,
                          size_class_col: str = "size_class",
                          thresholds: list = None,
                          n_merge: int = 2) -> pd.DataFrame:
    """
    DataFrame のサイズクラス列を n_merge 本ごとに集約する。
    集約後のクラスインデックスと対応ラベルを追加した DataFrame を返す。
    """
    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    coarse_thresholds = aggregate_bins(n_merge, thresholds)
    coarse_labels = build_class_labels(coarse_thresholds)

    df = df.copy()
    df["coarse_class"] = (df[size_class_col] // n_merge).clip(
        upper=len(coarse_thresholds)
    )
    df["coarse_label"] = df["coarse_class"].map(coarse_labels)
    return df


# ─────────────────────────────────────────────
# 退避行列の可視化ヘルパー
# ─────────────────────────────────────────────

def plot_eviction_heatmap(ax, matrix: np.ndarray,
                           thresholds: list = None,
                           title: str = "",
                           normalize_cols: bool = True,
                           cmap: str = "YlOrRd",
                           label_every: int = 2):
    """
    退避行列をヒートマップとして ax に描画する。

    normalize_cols=True  → 各列（挿入クラス）の合計で正規化
    label_every          → X/Y 軸ラベルを何本おきに表示するか（25 bin では 2 推奨）
    """
    import matplotlib.pyplot as plt

    if thresholds is None:
        thresholds = POW2_THRESHOLDS
    labels = build_class_labels(thresholds)
    n = matrix.shape[0]
    label_list = [labels.get(i, str(i)) for i in range(n)]

    if normalize_cols:
        col_sums = matrix.sum(axis=0, keepdims=True)
        col_sums = np.where(col_sums == 0, 1, col_sums)
        plot_data = matrix / col_sums
        vmin, vmax = 0, 1
    else:
        plot_data = matrix.astype(float)
        vmin, vmax = None, None

    im = ax.imshow(plot_data, cmap=cmap, aspect="auto",
                   vmin=vmin, vmax=vmax, interpolation="nearest")

    # 軸ラベル（間引き表示）
    tick_pos  = list(range(0, n, label_every))
    tick_lbls = [label_list[i] for i in tick_pos]
    ax.set_xticks(tick_pos)
    ax.set_xticklabels(tick_lbls, rotation=45, ha="right", fontsize=7)
    ax.set_yticks(tick_pos)
    ax.set_yticklabels(tick_lbls, fontsize=7)
    ax.set_xlabel("挿入クラス（キャッシュミスを起こしたオブジェクト）", fontsize=9)
    ax.set_ylabel("退避クラス（追い出されたオブジェクト）", fontsize=9)
    ax.set_title(title, fontsize=9)

    return im


# ─────────────────────────────────────────────
# OracleGeneral 読み込み（共通）
# ─────────────────────────────────────────────
import struct as _struct

OG_FORMAT      = "=IQIq"
OG_RECORD_SIZE = _struct.calcsize(OG_FORMAT)  # 24 bytes


def iter_oracle_general(
    path: str,
    sample_stride: int = 1,
    max_requests: int = None,
):
    """
    OracleGeneral バイナリを 1 レコードずつストリーミングで読み出すジェネレータ。

    メモリ使用量: O(1)（読み込みバッファ分のみ）。
    zstd 圧縮ファイルも透過的に対応。

    Args:
        path          : ファイルパス（.zst / .oracleGeneral.bin.zst も可）
        sample_stride : N のとき、sz>0 レコード N 件に 1 件だけ yield する。
                        1 = 全件（デフォルト）、10 = 約 1/10 にダウンサンプリング。
                        eviction_matrix_sim 用途では 10〜100 が実用的。
        max_requests  : yield する最大件数。None = 全件。

    Yields:
        (ts: int, obj_id: int, obj_size: int, next_access_vtime: int)
        obj_id は uint64 をそのまま Python int として返す。
    """
    from pathlib import Path as _Path

    _rec      = _struct.Struct(OG_FORMAT)
    _rec_size = OG_RECORD_SIZE
    _CHUNK    = _rec_size * 65_536   # 約 1.5 MB / チャンク

    _path  = str(path)
    _is_zst = _Path(_path).suffix.lower() == ".zst"

    def _gen(fobj):
        """ファイルオブジェクトを読み進めるジェネレータ本体。"""
        seen    = 0   # sz > 0 レコードを見た件数（stride 判定用）
        yielded = 0   # 実際に yield した件数
        while True:
            buf = fobj.read(_CHUNK)
            if not buf:
                return
            n_recs = len(buf) // _rec_size
            for i in range(n_recs):
                if max_requests is not None and yielded >= max_requests:
                    return
                ts, oid, sz, nv = _rec.unpack_from(buf, i * _rec_size)
                if sz > 0:
                    if seen % sample_stride == 0:
                        yield ts, oid, sz, nv
                        yielded += 1
                    seen += 1

    if _is_zst:
        try:
            import zstandard as _zstd
        except ImportError:
            raise ImportError(
                "zstd 圧縮ファイルの読み込みには zstandard ライブラリが必要です:\n"
                "  pip install zstandard"
            )
        _dctx = _zstd.ZstdDecompressor()
        with open(_path, "rb") as _f:
            with _dctx.stream_reader(_f) as _reader:
                yield from _gen(_reader)
    else:
        with open(_path, "rb") as _f:
            yield from _gen(_f)


def load_oracle_general(path: str,
                         max_requests: int = None) -> pd.DataFrame:
    """
    OracleGeneral バイナリ形式を読み込む。
    struct { uint32_t ts; uint64_t obj_id;
             uint32_t obj_size; int64_t next_access_vtime; }

    next_access_vtime == -1: このアクセス以降に再アクセスなし

    ストリーミング読み込みのため、max_requests が小さい場合でも
    ファイル全体をメモリに展開しない。
    """
    _BLOCK = 1_000_000   # 1 ブロックあたりの事前確保レコード数

    # ブロック単位で numpy 配列に積み上げる
    buf_ts  = np.empty(_BLOCK, dtype=np.uint32)
    buf_oid = np.empty(_BLOCK, dtype=np.uint64)
    buf_sz  = np.empty(_BLOCK, dtype=np.uint32)
    buf_nv  = np.empty(_BLOCK, dtype=np.int64)
    arrs_ts, arrs_oid, arrs_sz, arrs_nv = [], [], [], []
    i = 0

    for ts, oid, sz, nv in iter_oracle_general(path, max_requests=max_requests):
        buf_ts[i]  = ts
        buf_oid[i] = oid
        buf_sz[i]  = sz
        buf_nv[i]  = nv
        i += 1
        if i == _BLOCK:
            arrs_ts.append(buf_ts.copy())
            arrs_oid.append(buf_oid.copy())
            arrs_sz.append(buf_sz.copy())
            arrs_nv.append(buf_nv.copy())
            i = 0

    # 端数ブロック
    if i > 0:
        arrs_ts.append(buf_ts[:i].copy())
        arrs_oid.append(buf_oid[:i].copy())
        arrs_sz.append(buf_sz[:i].copy())
        arrs_nv.append(buf_nv[:i].copy())

    # 空トレース対応
    if not arrs_ts:
        return pd.DataFrame(columns=[
            "vtime", "timestamp", "obj_id",
            "obj_size", "next_access_vtime", "size_class"
        ])

    ts_a  = np.concatenate(arrs_ts)
    oid_a = np.concatenate(arrs_oid)
    sz_a  = np.concatenate(arrs_sz)
    nv_a  = np.concatenate(arrs_nv)
    n = len(ts_a)

    df = pd.DataFrame({
        "vtime":             np.arange(n, dtype=np.int64),
        "timestamp":         ts_a.astype(np.int64),
        "obj_id":            oid_a.astype(np.int64),   # uint64 → int64（groupby 等で使用）
        "obj_size":          sz_a.astype(np.int64),
        "next_access_vtime": nv_a,
    })

    # サイズクラスをここで一括付与（vectorized）
    df["size_class"] = get_size_class_vectorized(
        df["obj_size"].values, POW2_THRESHOLDS
    )

    ohw_frac = float((df["next_access_vtime"] == -1).mean())
    print(f"  読み込み完了: {n:,} req  "
          f"ユニーク={df['obj_id'].nunique():,}  "
          f"OHW={ohw_frac:.3%}  "
          f"サイズ=[{df['obj_size'].min()}, {df['obj_size'].max()}]B")
    return df


def load_trace(path: str,
               max_requests: int = None,
               sample_stride: int = 1) -> pd.DataFrame:
    """
    ファイル形式を自動判定して読み込む。
    OracleGeneral バイナリ・zstd 圧縮バイナリ・CSV に対応。

    Args:
        path          : トレースファイルパス
        max_requests  : 読み込む最大リクエスト数（先頭から）
        sample_stride : N 件に 1 件だけ取得（OracleGeneral のみ有効）
                        注: stride > 1 のとき vtime が間引かれ、
                            next_access_vtime との差（RD）は縮小される。
                            クラス間の相対比較には有効。

    対応拡張子:
      .oracleGeneral                  非圧縮バイナリ
      .oracleGeneral.zst              zstd 圧縮バイナリ
      .oracleGeneral.bin.zst          zstd 圧縮バイナリ（bin 付き）
      .bin / .lcs                     非圧縮バイナリ
      .csv / .tsv / .txt              CSV/TSV テキスト
    """
    from pathlib import Path
    p = Path(path)
    name_lower = p.name.lower()

    # .zst サフィックスの検出（複合拡張子 .oracleGeneral.bin.zst などに対応）
    is_zst = name_lower.endswith(".zst")

    # .zst を除いた内側の拡張子を取得
    inner_suffix = Path(name_lower[:-4]).suffix if is_zst else p.suffix.lower()

    is_binary = (
        is_zst                                           # *.zst は必ずバイナリ扱い
        or inner_suffix in {".oraclegeneral", ".bin", ".lcs"}
        or (
            not is_zst
            and p.suffix.lower() not in {".csv", ".tsv", ".txt"}
            and p.stat().st_size % OG_RECORD_SIZE == 0
        )
    )

    if is_binary:
        label = f"OracleGeneral バイナリ{'(zstd圧縮)' if is_zst else ''}"
        print(f"  形式: {label} ({p.name})")
        # sample_stride は load_oracle_general → iter_oracle_general に伝達
        _BLOCK = 1_000_000
        buf_ts  = np.empty(_BLOCK, dtype=np.uint32)
        buf_oid = np.empty(_BLOCK, dtype=np.uint64)
        buf_sz  = np.empty(_BLOCK, dtype=np.uint32)
        buf_nv  = np.empty(_BLOCK, dtype=np.int64)
        arrs_ts, arrs_oid, arrs_sz, arrs_nv = [], [], [], []
        i = 0
        for ts, oid, sz, nv in iter_oracle_general(path, sample_stride, max_requests):
            buf_ts[i] = ts; buf_oid[i] = oid; buf_sz[i] = sz; buf_nv[i] = nv
            i += 1
            if i == _BLOCK:
                arrs_ts.append(buf_ts.copy()); arrs_oid.append(buf_oid.copy())
                arrs_sz.append(buf_sz.copy()); arrs_nv.append(buf_nv.copy())
                i = 0
        if i > 0:
            arrs_ts.append(buf_ts[:i].copy()); arrs_oid.append(buf_oid[:i].copy())
            arrs_sz.append(buf_sz[:i].copy()); arrs_nv.append(buf_nv[:i].copy())
        if not arrs_ts:
            return pd.DataFrame(columns=["vtime","timestamp","obj_id",
                                          "obj_size","next_access_vtime","size_class"])
        ts_a = np.concatenate(arrs_ts); oid_a = np.concatenate(arrs_oid)
        sz_a = np.concatenate(arrs_sz); nv_a  = np.concatenate(arrs_nv)
        n = len(ts_a)
        df = pd.DataFrame({
            "vtime":             np.arange(n, dtype=np.int64),
            "timestamp":         ts_a.astype(np.int64),
            "obj_id":            oid_a.astype(np.int64),
            "obj_size":          sz_a.astype(np.int64),
            "next_access_vtime": nv_a,
        })
        df["size_class"] = get_size_class_vectorized(df["obj_size"].values, POW2_THRESHOLDS)
        ohw_frac = float((df["next_access_vtime"] == -1).mean())
        print(f"  読み込み完了: {n:,} req  "
              f"ユニーク={df['obj_id'].nunique():,}  "
              f"OHW={ohw_frac:.3%}  "
              f"サイズ=[{df['obj_size'].min()}, {df['obj_size'].max()}]B")
        return df

    # CSV フォールバック
    print(f"  形式: CSV ({p.name})")
    sep = "\t" if p.suffix.lower() == ".tsv" else ","
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline().strip().split(sep)
    has_header = not all(_try_float(v) for v in first[:3])

    df = pd.read_csv(path, sep=sep,
                     header=0 if has_header else None,
                     names=None if has_header else ["timestamp", "obj_id", "obj_size"],
                     usecols=[0, 1, 2],
                     nrows=max_requests,
                     on_bad_lines="skip", low_memory=True)
    df.columns = ["timestamp", "obj_id", "obj_size"]
    df["obj_size"] = pd.to_numeric(df["obj_size"], errors="coerce").fillna(0).astype(int)
    df = df[df["obj_size"] > 0].reset_index(drop=True)
    df["vtime"] = df.index.astype(np.int64)
    df["next_access_vtime"] = np.int64(-2)   # CSV には情報なし
    df["size_class"] = get_size_class_vectorized(df["obj_size"].values)
    return df


def _try_float(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False
