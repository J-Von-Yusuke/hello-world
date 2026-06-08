#!/usr/bin/env python3
"""
threshold_predictor.py  —  RegionSplit 最適閾値予測器 v3.2
====================================================================
入力: ビン別サイズ分布データ (JSON)。複数ファイルを一括処理可能。
出力: 推奨閾値 T, 判定経路, 判定根拠 (text / json / csv)

【4経路アルゴリズム】優先順位: A → B → D → C
  Path A: OHWジャンプ  — r < 0.70 の後に r >= 0.75 のビンを検出
  Path B: バルク隔離   — 単一ビンがバイトの >80% を占有
  Path D: OHW V字谷   — r が前後ビンより >=0.05 低い谷 かつ N_b が前後平均の 1.5x 以上
  Path C: CDF交差点    — F_req(T) + F_byte(T) >= 1.0 の最初のビン

使用例:
  # 単一ファイル・テキスト出力
  python threshold_predictor.py files/metaKV.measurement.json

  # 複数ファイル一括・CSV 出力
  python threshold_predictor.py files/*.measurement.json --format csv

  # スコープ外ビン除外 + JSON 出力
  python threshold_predictor.py files/metaCDN.measurement.json --max-bin 27 --format json

  # 結果をファイルに保存
  python threshold_predictor.py files/*.measurement.json --format csv -o results.csv
"""

import csv
import io
import json
import os
import sys
import argparse
from pathlib import Path

# ─── 定数 ────────────────────────────────────────────────────────
OHW_HIGH       = 0.75   # OHWジャンプ: これ以上をジャンプ後とみなす
OHW_LOW        = 0.70   # OHWジャンプ: これ未満を「低OHWゾーン」とみなす
BULK_THRESHOLD = 0.80   # バルク隔離: 単一ビンのバイト占有率上限
CDF_CROSSOVER  = 1.0    # CDF交差点: F_req+F_byte の交差閾値
SIG_REQ_MIN    = 0.005  # 有意ビン: 最低リクエスト割合 (0.5%)
SIG_BYTE_MIN   = 0.005  # 有意ビン: 最低バイト割合    (0.5%)
VDIP_R_DROP    = 0.05   # V字谷: r が前後ビンより最低この分だけ低い
VDIP_N_RATIO   = 1.5    # V字谷: N_b が前後ビン平均の最低この倍以上

# CSV 出力列定義 (順序を固定)
CSV_FIELDS = [
    "workload",
    "cache_ratio",
    "max_bin",
    "threshold_bytes",
    "threshold_human",
    "path",
    "signal_bin_idx",
    "signal_bin_label",
    "signal_bin_r",
    "signal_bin_req_pct",
    "signal_bin_byte_pct",
    "signal_bin_n_ratio",
    "explanation",
]


def human_bytes(n: int) -> str:
    """バイト数を人間可読文字列に変換"""
    if n is None:
        return "N/A"
    for unit, threshold in [("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)]:
        if n >= threshold:
            v = n / threshold
            frac = int((v - int(v)) * 10)
            return f"{int(v)}{'.' + str(frac) if frac else ''}{unit}"
    return f"{n}B"


def predict_threshold(bin_stats: list, cache_ratio: float = 0.30) -> dict:
    """
    RegionSplit 最適閾値を4経路アルゴリズムで予測する。

    Returns: dict with keys
      threshold_bytes, threshold_human, path, explanation, signal_bin, advice
    """
    active = sorted(
        [b for b in bin_stats if b.get("req_count", 0) > 0 or b.get("byte_count", 0) > 0],
        key=lambda x: x.get("bin_idx", 0),
    )
    if not active:
        return _result(None, "neutral", "入力データが空です", None, "データを確認してください")

    total_req   = sum(b.get("req_count",  0) for b in active) or 1
    total_bytes = sum(b.get("byte_count", 0) for b in active) or 1

    for b in active:
        b["_rs"] = b.get("req_count",  0) / total_req
        b["_bs"] = b.get("byte_count", 0) / total_bytes
        b["_lo"] = _bin_lo(b)

    sig = [b for b in active if b["_rs"] >= SIG_REQ_MIN or b["_bs"] >= SIG_BYTE_MIN]
    if not sig:
        sig = active

    # Path A: OHWジャンプ
    seen_low_ohw = False
    for b in sig:
        r = b.get("ohw_rate", 0.0)
        if r < OHW_LOW:
            seen_low_ohw = True
        elif seen_low_ohw and r >= OHW_HIGH:
            T = b["_lo"]
            if cache_ratio <= 0.15:
                T //= 2
            expl = (
                f"OHWジャンプ: ビン{b.get('bin_idx','?')} ({b.get('label', human_bytes(b['_lo']))}付近) で "
                f"r={r:.2f} に急上昇 (低OHWゾーン確認済み) → 閾値を {human_bytes(T)} に設定"
            )
            return _result(T, "ohw_jump", expl, b, _advice_ohw_jump(b, cache_ratio))

    # Path B: バルク隔離
    max_b = max(active, key=lambda x: x["_bs"])
    if max_b["_bs"] > BULK_THRESHOLD:
        T = max_b["_lo"]
        expl = (
            f"バルク隔離: ビン{max_b.get('bin_idx','?')} ({max_b.get('label', human_bytes(T))}) が "
            f"バイトの {max_b['_bs']*100:.0f}% を占有 → このビン ({human_bytes(T)}〜) を大プールへ隔離"
        )
        return _result(T, "bulk", expl, max_b, _advice_bulk(max_b))

    # Path D: OHW V字谷
    for i in range(1, len(sig) - 1):
        b_prev, b_curr, b_next = sig[i - 1], sig[i], sig[i + 1]
        r_prev = b_prev.get("ohw_rate", 0.0)
        r_curr = b_curr.get("ohw_rate", 0.0)
        r_next = b_next.get("ohw_rate", 0.0)
        drop_left  = r_prev - r_curr
        drop_right = r_next - r_curr
        if drop_left >= VDIP_R_DROP and drop_right >= VDIP_R_DROP:
            n_curr     = b_curr.get("req_count", 0)
            n_neighbor = ((b_prev.get("req_count", 0) + b_next.get("req_count", 0)) / 2) or 1
            n_ratio    = n_curr / n_neighbor
            if n_ratio >= VDIP_N_RATIO:
                T = b_curr["_lo"]
                if cache_ratio <= 0.15:
                    T //= 2
                b_curr["_vdip_n_ratio"]    = n_ratio
                b_curr["_vdip_drop_left"]  = drop_left
                b_curr["_vdip_drop_right"] = drop_right
                expl = (
                    f"OHW V字谷: ビン{b_curr.get('bin_idx','?')} "
                    f"({b_curr.get('label', human_bytes(b_curr['_lo']))}) で "
                    f"r={r_curr:.3f} が前後比 -{drop_left:.3f}/+{drop_right:.3f} の谷、"
                    f"N_b が前後平均の {n_ratio:.1f}x → このビン ({human_bytes(T)}〜) を大プールへ保護"
                )
                return _result(T, "vdip", expl, b_curr,
                               _advice_vdip(b_curr, n_ratio, drop_left, drop_right))

    # Path C: CDF交差点
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
            return _result(T, "cdf_cross", expl, b, _advice_cdf(b, cache_ratio))

    # Neutral
    return _result(
        None, "neutral",
        "全ビンでOHW率・バイト占有・CDF構造が均一。RegionSplitの効果は限定的。",
        None,
        "LRUベースラインとの性能差は小さい見込み。分割なし構成を推奨。",
    )


# ─── ヘルパー ────────────────────────────────────────────────────
def _bin_lo(b: dict) -> int:
    if "bin_lo" in b:
        return int(b["bin_lo"])
    return 1 << b.get("bin_idx", 0)


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
    base = (f"OHWジャンプ型: 閾値以下の低OHWビン群を小プールで保護し、"
            f"高OHWビン (r={r:.2f}) を大プールへ分離します。")
    if cache_ratio <= 0.15:
        return base + " キャッシュが小さいため閾値を1段下げました。実測で確認してください。"
    if r >= 0.90:
        return base + " OHW率が非常に高く、分離効果は大きいと期待されます。"
    return base + " OHW率が中程度なので、±1ビン (×2または÷2) も試してください。"


def _advice_bulk(b: dict) -> str:
    pct = b.get("_bs", 0) * 100
    return (f"バルク隔離型: 巨大オブジェクト ({human_bytes(b['_lo'])}〜) が"
            f"バイトの {pct:.0f}% を占有します。実験範囲外の場合は、"
            f"利用可能な最大閾値を設定し実効的に分離してください。")


def _advice_vdip(b: dict, n_ratio: float, drop_left: float, drop_right: float) -> str:
    r = b.get("ohw_rate", 0)
    return (f"OHW V字谷型: ビン ({b.get('label', '?')}) はr={r:.3f}の局所最小値を持ち、"
            f"前後ビン比でN_bが{n_ratio:.1f}x集中しています (r谷幅: -{drop_left:.3f}/+{drop_right:.3f})。"
            f" このビンを小プールへ入れるとOHWバイトが大量流入しBMRが悪化します。"
            f" 閾値をこのビンの下端に設定し、大プールへ残してください。"
            f" ±1ビンの実測で谷の深さを確認することを推奨します。")


def _advice_cdf(b: dict, cache_ratio: float) -> str:
    base = "CDF交差型: リクエスト密度とバイト密度の重心が異なるトレースです。"
    if cache_ratio <= 0.15:
        return base + " 小キャッシュのため閾値を1段下げました。"
    return base + " OHWジャンプが見られない均一OHWトレースに多いパターンです。"


# ─── I/O ────────────────────────────────────────────────────────
def _load_bin_stats(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict) and "bins" in data:
        raw = data["bins"]
    elif isinstance(data, dict) and "_measurement" in data:
        raw = data["_measurement"].get("bins_detail", [])
    elif isinstance(data, list):
        raw = data
    else:
        raise ValueError(f"未対応のJSONフォーマット: {list(data.keys())}")

    bins = []
    for b in raw:
        entry = {
            "bin_idx":   b.get("bin", b.get("bin_idx", 0)),
            "req_count": b.get("N_b", b.get("req_count", 0)),
            "byte_count": b.get("byte_count", 0),
            "ohw_rate":  b.get("r",  b.get("ohw_rate", 0.0)),
            "rho":       b.get("rho", 1.0),
            "mean_size": b.get("mean_size", 0),
        }
        idx = entry["bin_idx"]
        entry["bin_lo"] = 1 << idx
        entry["label"]  = human_bytes(1 << idx)
        if entry["byte_count"] == 0 and entry["mean_size"] > 0:
            entry["byte_count"] = int(entry["req_count"] * entry["mean_size"])
        bins.append(entry)
    return bins


def _to_csv_row(workload: str, cache_ratio: float, max_bin, result: dict) -> dict:
    """予測結果を CSV 1行分の dict に変換する"""
    sb = result.get("signal_bin") or {}
    return {
        "workload":           workload,
        "cache_ratio":        f"{cache_ratio:.2f}",
        "max_bin":            str(max_bin) if max_bin is not None else "",
        "threshold_bytes":    str(result["threshold_bytes"]) if result["threshold_bytes"] is not None else "",
        "threshold_human":    result["threshold_human"],
        "path":               result["path"],
        "signal_bin_idx":     str(sb.get("bin_idx", "")),
        "signal_bin_label":   sb.get("label", ""),
        "signal_bin_r":       f"{sb.get('ohw_rate', ''):.3f}" if sb.get("ohw_rate") is not None and sb else "",
        "signal_bin_req_pct": f"{sb.get('_rs', 0)*100:.2f}" if sb else "",
        "signal_bin_byte_pct":f"{sb.get('_bs', 0)*100:.2f}" if sb else "",
        "signal_bin_n_ratio": f"{sb.get('_vdip_n_ratio', ''):.2f}" if sb.get("_vdip_n_ratio") else "",
        "explanation":        result["explanation"],
    }


def _to_json_record(workload: str, cache_ratio: float, max_bin, result: dict) -> dict:
    """予測結果を JSON 出力用 dict に変換する"""
    sb = result.get("signal_bin") or {}
    sig_out = None
    if sb:
        sig_out = {
            "bin_idx":   sb.get("bin_idx"),
            "label":     sb.get("label"),
            "ohw_rate":  sb.get("ohw_rate"),
            "req_pct":   round(sb.get("_rs", 0) * 100, 4),
            "byte_pct":  round(sb.get("_bs", 0) * 100, 4),
        }
        if sb.get("_vdip_n_ratio"):
            sig_out["vdip_n_ratio"]    = round(sb["_vdip_n_ratio"], 3)
            sig_out["vdip_drop_left"]  = round(sb["_vdip_drop_left"], 3)
            sig_out["vdip_drop_right"] = round(sb["_vdip_drop_right"], 3)
    return {
        "workload":        workload,
        "cache_ratio":     cache_ratio,
        "max_bin":         max_bin,
        "threshold_bytes": result["threshold_bytes"],
        "threshold_human": result["threshold_human"],
        "path":            result["path"],
        "signal_bin":      sig_out,
        "explanation":     result["explanation"],
        "advice":          result["advice"],
    }


PATH_LABELS = {
    "ohw_jump":  "Path A: OHWジャンプ",
    "bulk":      "Path B: バルク隔離",
    "vdip":      "Path D: OHW V字谷",
    "cdf_cross": "Path C: CDF交差点",
    "neutral":   "Neutral: 効果限定的",
}


def _print_text(workload: str, result: dict, verbose: bool = True) -> None:
    sb = result.get("signal_bin") or {}
    print()
    print(f"  ワークロード: {workload}")
    print("═" * 60)
    print(f"  推奨閾値 T = {result['threshold_human']}")
    print(f"  判定経路  = {PATH_LABELS.get(result['path'], result['path'])}")
    print("═" * 60)
    print(f"\n  判定根拠:\n    {result['explanation']}")
    print(f"\n  アドバイス:\n    {result['advice']}")
    if verbose and sb:
        print(f"\n  シグナルビン詳細:")
        print(f"    ビン     : {sb.get('label', sb.get('bin_idx'))}")
        print(f"    OHW率    : {sb.get('ohw_rate', 0):.3f}")
        print(f"    Req 割合 : {sb.get('_rs', 0)*100:.2f}%")
        print(f"    Byte 割合: {sb.get('_bs', 0)*100:.2f}%")
        if sb.get("_vdip_n_ratio"):
            print(f"    N_b 比   : {sb['_vdip_n_ratio']:.2f}x (V字谷)")
    print()


# ─── main ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="RegionSplit 閾値予測器 v3.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("input", nargs="+", help="ビン統計 JSON ファイル (複数可、glob 展開済み)")
    parser.add_argument(
        "--format", choices=["text", "json", "csv"], default="text",
        help="出力形式: text (デフォルト) / json / csv"
    )
    parser.add_argument(
        "--cache-ratio", type=float, default=0.30, metavar="R",
        help="キャッシュ比率 (デフォルト: 0.30)"
    )
    parser.add_argument(
        "--max-bin", type=int, default=None, metavar="N",
        help="分析対象の最大ビン番号 (例: 27 で bin27以下のみ使用)"
    )
    parser.add_argument(
        "-o", "--output", default=None, metavar="FILE",
        help="出力先ファイルパス (省略時は標準出力)"
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="text 形式でシグナルビン詳細を省略"
    )
    # 後方互換: --json は --format json の別名
    parser.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.json:
        args.format = "json"

    # 出力先の設定
    out_file = open(args.output, "w", encoding="utf-8", newline="") if args.output else None
    out_stream = out_file or sys.stdout

    # 各ファイルを処理
    records_json = []
    csv_rows     = []
    errors       = []

    for fpath in args.input:
        workload = Path(fpath).stem.replace(".measurement", "")
        try:
            bin_stats = _load_bin_stats(fpath)
        except Exception as e:
            errors.append(f"{fpath}: {e}")
            continue

        if args.max_bin is not None:
            bin_stats = [b for b in bin_stats if b.get("bin_idx", 0) <= args.max_bin]

        result = predict_threshold(bin_stats, cache_ratio=args.cache_ratio)

        if args.format == "text":
            _print_text(workload, result, verbose=not args.quiet)
        elif args.format == "json":
            records_json.append(_to_json_record(workload, args.cache_ratio, args.max_bin, result))
        elif args.format == "csv":
            csv_rows.append(_to_csv_row(workload, args.cache_ratio, args.max_bin, result))

    # JSON: 1ファイルなら object、複数ならリスト
    if args.format == "json":
        payload = records_json[0] if len(records_json) == 1 else records_json
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=out_stream)

    # CSV: ヘッダー + 全行
    elif args.format == "csv":
        writer = csv.DictWriter(out_stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(csv_rows)

    # エラー報告
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)

    if out_file:
        out_file.close()
        print(f"保存先: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
