#!/usr/bin/env python3
"""
threshold_predictor.py  —  RegionSplit 最適閾値予測器 v3.0
====================================================================
入力: ビン別サイズ分布データ (JSON または Python dict)
出力: 推奨閾値 T, 判定経路, 判定根拠

【3経路アルゴリズム】
  Path A: OHWジャンプ  — r < 0.70 の後に r >= 0.75 のビンを検出
  Path B: バルク隔離   — 単一ビンがバイトの >80% を占有
  Path C: CDF交差点    — F_req(T) + F_byte(T) >= 1.0 の最初のビン

使用例:
  python threshold_predictor.py data.json
  python threshold_predictor.py data.json --cache-ratio 0.1
  python threshold_predictor.py data.json --json   # JSON出力
"""

import json
import sys
import argparse
from typing import Optional

# ─── 定数 ────────────────────────────────────────────────────────
OHW_HIGH       = 0.75   # OHWジャンプ: これ以上をジャンプ後とみなす
OHW_LOW        = 0.70   # OHWジャンプ: これ未満を「低OHWゾーン」とみなす
BULK_THRESHOLD = 0.80   # バルク隔離: 単一ビンのバイト占有率上限
CDF_CROSSOVER  = 1.0    # CDF交差点: F_req+F_byte の交差閾値
SIG_REQ_MIN    = 0.005  # 有意ビン: 最低リクエスト割合 (0.5%)
SIG_BYTE_MIN   = 0.005  # 有意ビン: 最低バイト割合    (0.5%)


def human_bytes(n: int) -> str:
    """バイト数を人間可読文字列に変換"""
    if n is None:
        return "N/A"
    for unit, threshold in [("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)]:
        if n >= threshold:
            v = n / threshold
            return f"{int(v)}{'.' + str(int((v - int(v)) * 10)) if v != int(v) else ''}{unit}"
    return f"{n}B"


def predict_threshold(
    bin_stats: list[dict],
    cache_ratio: float = 0.30,
) -> dict:
    """
    RegionSplit 最適閾値を3経路アルゴリズムで予測する。

    Parameters
    ----------
    bin_stats : list[dict]
        ビン別統計。各要素は以下のキーを持つ:
          - bin_idx  : int   ビンインデックス (2^bin_idx <= size < 2^(bin_idx+1))
          - req_count: int   リクエスト数 (または割合。byte_countと単位を揃える)
          - byte_count:int   バイト数
          - ohw_rate : float OHW率 (0〜1)
          - rho      : float 非OHWオブジェクトの平均再利用回数 (省略可)
          - mean_size: float ビン内オブジェクト平均サイズ (省略可)
        ※ bin_lo (ビン下端バイト) が含まれていれば優先して使用
    cache_ratio : float
        キャッシュ比率 (0.0〜1.0)。小キャッシュ(<= 0.15)では閾値を半減する。

    Returns
    -------
    dict with keys:
      threshold_bytes : int | None   推奨閾値 (バイト)
      threshold_human : str          人間可読閾値
      path            : str          判定経路 ("ohw_jump" / "bulk" / "cdf_cross" / "neutral")
      explanation     : str          判定根拠 (日本語)
      signal_bin      : dict | None  判定の根拠となったビン情報
      advice          : str          運用アドバイス
    """
    # 1. アクティブビンを抽出しソート
    active = sorted(
        [b for b in bin_stats if b.get("req_count", 0) > 0 or b.get("byte_count", 0) > 0],
        key=lambda x: x.get("bin_idx", 0),
    )
    if not active:
        return _result(None, "neutral", "入力データが空です", None, "データを確認してください")

    total_req   = sum(b.get("req_count",  0) for b in active) or 1
    total_bytes = sum(b.get("byte_count", 0) for b in active) or 1

    # 各ビンに割合を付与
    for b in active:
        b["_rs"] = b.get("req_count",  0) / total_req
        b["_bs"] = b.get("byte_count", 0) / total_bytes
        b["_lo"] = _bin_lo(b)

    # 有意ビンフィルタ (req or byte が閾値以上)
    sig = [b for b in active if b["_rs"] >= SIG_REQ_MIN or b["_bs"] >= SIG_BYTE_MIN]
    if not sig:
        sig = active  # フォールバック: 全ビンを使用

    # ──────────────────────────────────────────────────────────────
    # Path A: OHWジャンプ検出
    #   低OHWゾーン (r < 0.70) を確認した後、r >= 0.75 のビンが出現
    # ──────────────────────────────────────────────────────────────
    seen_low_ohw = False
    for b in sig:
        r = b.get("ohw_rate", 0.0)
        if r < OHW_LOW:
            seen_low_ohw = True
        elif seen_low_ohw and r >= OHW_HIGH:
            T = b["_lo"]
            if cache_ratio <= 0.15:
                T //= 2  # 小キャッシュでは1段下へ
            expl = (
                f"OHWジャンプ: ビン{b.get('bin_idx','?')} ({b.get('label', human_bytes(b['_lo']))}付近) で "
                f"r={r:.2f} に急上昇 (低OHWゾーン確認済み) → 閾値を {human_bytes(T)} に設定"
            )
            advice = _advice_ohw_jump(b, cache_ratio)
            return _result(T, "ohw_jump", expl, b, advice)

    # ──────────────────────────────────────────────────────────────
    # Path B: バルク隔離
    #   単一ビンがバイトの 80% 超を占有
    # ──────────────────────────────────────────────────────────────
    max_b = max(active, key=lambda x: x["_bs"])
    if max_b["_bs"] > BULK_THRESHOLD:
        T = max_b["_lo"]
        expl = (
            f"バルク隔離: ビン{max_b.get('bin_idx','?')} ({max_b.get('label', human_bytes(T))}) が "
            f"バイトの {max_b['_bs']*100:.0f}% を占有 → このビン ({human_bytes(T)}〜) を大プールへ隔離"
        )
        advice = _advice_bulk(max_b)
        return _result(T, "bulk", expl, max_b, advice)

    # ──────────────────────────────────────────────────────────────
    # Path C: CDF交差点
    #   累積 (F_req + F_byte) が 1.0 を最初に超えるビン
    # ──────────────────────────────────────────────────────────────
    cr = cb = 0.0
    for b in active:
        cr += b["_rs"]
        cb += b["_bs"]
        if cr + cb >= CDF_CROSSOVER:
            T = b["_lo"]
            if cache_ratio <= 0.15:
                T //= 2
            expl = (
                f"CDF交差点: ビン{b.get('bin_idx','?')} ({b.get('label', human_bytes(T))}) で "
                f"F_req+F_byte={cr+cb:.2f} が {CDF_CROSSOVER} を超過 → 閾値を {human_bytes(T)} に設定"
            )
            advice = _advice_cdf(b, cache_ratio)
            return _result(T, "cdf_cross", expl, b, advice)

    # ──────────────────────────────────────────────────────────────
    # Neutral: 有効な候補なし
    # ──────────────────────────────────────────────────────────────
    return _result(
        None, "neutral",
        "全ビンでOHW率・バイト占有・CDF構造が均一。RegionSplitの効果は限定的。",
        None,
        "LRUベースラインとの性能差は小さい見込み。分割なし構成を推奨。",
    )


def _bin_lo(b: dict) -> int:
    """ビン下端バイトを取得"""
    if "bin_lo" in b:
        return int(b["bin_lo"])
    idx = b.get("bin_idx", 0)
    return 1 << idx


def _result(T, path, expl, sig_bin, advice) -> dict:
    return {
        "threshold_bytes": T,
        "threshold_human": human_bytes(T) if T is not None else "N/A",
        "path":            path,
        "explanation":     expl,
        "signal_bin":      sig_bin,
        "advice":          advice,
    }


def _advice_ohw_jump(b: dict, cache_ratio: float) -> str:
    r = b.get("ohw_rate", 0)
    base = (
        f"OHWジャンプ型: 閾値以下の低OHWビン群を小プールで保護し、"
        f"高OHWビン (r={r:.2f}) を大プールへ分離します。"
    )
    if cache_ratio <= 0.15:
        return base + " キャッシュが小さいため閾値を1段下げました。実測で確認してください。"
    if r >= 0.90:
        return base + " OHW率が非常に高く、分離効果は大きいと期待されます。"
    return base + " OHW率が中程度なので、±1ビン (×2または÷2) も試してください。"


def _advice_bulk(b: dict) -> str:
    pct = b.get("_bs", 0) * 100
    return (
        f"バルク隔離型: 巨大オブジェクト ({human_bytes(b['_lo'])}〜) が"
        f"バイトの {pct:.0f}% を占有します。実験範囲外の場合は、"
        f"利用可能な最大閾値を設定し実効的に分離してください。"
    )


def _advice_cdf(b: dict, cache_ratio: float) -> str:
    base = "CDF交差型: リクエスト密度とバイト密度の重心が異なるトレースです。"
    if cache_ratio <= 0.15:
        return base + " 小キャッシュのため閾値を1段下げました。"
    return base + " OHWジャンプが見られない均一OHWトレースに多いパターンです。"


# ─── CLI ────────────────────────────────────────────────────────
def _load_bin_stats(path: str) -> list[dict]:
    """JSONファイルからビン統計を読み込む。複数フォーマットに対応。"""
    with open(path) as f:
        data = json.load(f)

    # フォーマット1: {"bins": [...]}
    if isinstance(data, dict) and "bins" in data:
        raw = data["bins"]
    # フォーマット2: measurement JSON {"workload":..., "_measurement":{"bins_detail":[...]}}
    elif isinstance(data, dict) and "_measurement" in data:
        raw = data["_measurement"].get("bins_detail", [])
    # フォーマット3: [{"bin": N, ...}] (リスト直接)
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError(f"未対応のJSONフォーマット: {list(data.keys())}")

    bins = []
    for b in raw:
        # measurement JSON フォーマット変換
        entry = {
            "bin_idx":   b.get("bin", b.get("bin_idx", 0)),
            "req_count": b.get("N_b", b.get("req_count", 0)),
            "byte_count":b.get("byte_count", 0),
            "ohw_rate":  b.get("r",   b.get("ohw_rate", 0.0)),
            "rho":       b.get("rho", 1.0),
            "mean_size": b.get("mean_size", 0),
        }
        # bin_lo を計算
        idx = entry["bin_idx"]
        entry["bin_lo"] = 1 << idx
        entry["label"]  = human_bytes(1 << idx)

        # byte_count がない場合、N_b と mean_size から推定
        if entry["byte_count"] == 0 and entry["mean_size"] > 0:
            entry["byte_count"] = int(entry["req_count"] * entry["mean_size"])

        bins.append(entry)

    return bins


def _print_result(r: dict, verbose: bool = True) -> None:
    path_labels = {
        "ohw_jump":  "Path A: OHWジャンプ",
        "bulk":      "Path B: バルク隔離",
        "cdf_cross": "Path C: CDF交差点",
        "neutral":   "Neutral: 効果限定的",
    }
    print()
    print("═" * 60)
    print(f"  推奨閾値 T = {r['threshold_human']}")
    print(f"  判定経路  = {path_labels.get(r['path'], r['path'])}")
    print("═" * 60)
    print(f"\n  判定根拠:\n    {r['explanation']}")
    print(f"\n  アドバイス:\n    {r['advice']}")
    if verbose and r["signal_bin"]:
        b = r["signal_bin"]
        print(f"\n  シグナルビン詳細:")
        print(f"    ビン     : {b.get('label', b.get('bin_idx'))}")
        print(f"    OHW率    : {b.get('ohw_rate', 0):.3f}")
        print(f"    Req 割合 : {b.get('_rs', 0)*100:.2f}%")
        print(f"    Byte 割合: {b.get('_bs', 0)*100:.2f}%")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="RegionSplit 閾値予測器 v3.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", help="ビン統計 JSON ファイル")
    parser.add_argument(
        "--cache-ratio", type=float, default=0.30, metavar="R",
        help="キャッシュ比率 (デフォルト: 0.30)"
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出力")
    parser.add_argument("-q", "--quiet", action="store_true", help="簡潔出力")
    args = parser.parse_args()

    try:
        bin_stats = _load_bin_stats(args.input)
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    result = predict_threshold(bin_stats, cache_ratio=args.cache_ratio)

    if args.json:
        out = {k: v for k, v in result.items() if k != "signal_bin"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_result(result, verbose=not args.quiet)


if __name__ == "__main__":
    main()
