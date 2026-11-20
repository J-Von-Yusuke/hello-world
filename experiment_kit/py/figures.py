#!/usr/bin/env python3
"""figures.py — 図を描く道具（標準ライブラリだけ）。paper_figs・paper_sens_figs・paper_robust_figs・tune_figs・agg_figs が使う。

  Fig（1600×900 の白地の SVG と凡例）、Ax（軸：数値 / カテゴリ、線・点・棒・積み上げ・横線・縦線）、heat（ヒートマップ）、span（軸の範囲）、
  method_label（RegionFlash の指定から図の名前）、to_png（SVG → PNG）、write_index（図と説明の index.html）、write_pptx（1 図 1 枚のスライド）。
  validity・synth の集計の図の一式は agg_figs.py（11-20 にここから分けた）。"""
import html, json, math, os, re, shutil, subprocess, zipfile

esc = html.escape
FONT = "'Noto Sans CJK JP','Noto Sans JP','Yu Gothic UI','Hiragino Sans',Meiryo,sans-serif"
INK, MUT, GRID = "#222222", "#666666", "#dddddd"
PAL = ["#3b7dbf", "#c9683a", "#3f9a64", "#8a5fb5", "#b8a02a", "#c0504a", "#2aa0a8", "#7d7d7d", "#d17fb0", "#5b6abf"]



def method_label(spec):
    """11-20 図の名前を RegionFlash の指定から、どの方式も同じ並びで作る：R｜閾値の決め方｜容量配分の決め方｜再挿入の決め方（＋篩など）。
    例：R=RU｜閾値 大津｜配分 名簿｜再挿入 n=cb、CacheLib：R=256M｜分類なし｜再挿入 n=1。(ja, en) を返す"""
    kv = {}
    for tok in spec.split(","):
        k, _, v = tok.partition("="); kv[k] = v
    rs = kv.get("region-size")
    r = f"R={int(rs) // 2**20}M" if rs else "R=RU"
    h, rp, re_, adm = kv.get("hint", "none"), kv.get("region-params", ""), kv.get("reinsert", ""), kv.get("admit", "")
    ja, en = [r], [r]
    if h == "none": ja.append("分類なし"); en.append("no class.")
    elif h.startswith("st_otsu"):
        v = "再利用の強さ" if "logreq" in h else "名簿" if "val=ghost" in h else "再利用率"
        ve = "reuse intensity" if "logreq" in h else "ghost" if "val=ghost" in h else "reuse rate"
        ja.append(f"閾値 大津（{v}）"); en.append(f"tau Otsu ({ve})")
    elif h.startswith("split_threshold"): ja.append("閾値 st"); en.append("tau st")
    elif h.startswith("fixed"): t = h.split("tau=")[-1].split("/")[0]; ja.append(f"閾値 固定 {t}"); en.append(f"tau fixed {t}")
    elif "key=dram" in h: ja.append("DRAM ヒット数で分類"); en.append("class. by DRAM hits")
    else: ja.append(h.split(":")[0]); en.append(h.split(":")[0])
    if h != "none":
        p = dict(x.partition("=")[::2] for x in rp.split("/") if x)
        if p.get("auto") == "2": a, b = "名簿", "ghost"
        elif p.get("auto") == "1":
            w = p.get("w", "0"); a, b = ("山登り（BHR）", "climb (BHR)") if float(w) == 0 else (f"山登り（BHR+{w}·OHR）", f"climb (BHR+{w}·OHR)")
            if p.get("gate"): a, b = a + "・gate", b + ", gate"
        elif "ff" in p: a, b = f"下限 {p['ff']} 固定", f"floor {p['ff']} fixed"
        else: a, b = "自然（下限なし）", "natural (no floor)"
        ja.append("配分 " + a); en.append("alloc. " + b)
    if not re_: ja.append("再挿入なし"); en.append("no rewrite")
    else:
        q = dict(x.partition("=")[::2] for x in re_.split(":", 1)[-1].split("/") if x)
        n = q.get("n", "1")
        if n == "cb":
            lam = float(q.get("lambda", "0")); base = q.get("base", "first")
            a = "n=cb" + ("（初回基準）" if base == "first" else "（全滞在基準）" if base == "all" else "") + (f" λ={lam:g}" if lam else "")
            b = "n=cb" + (" (first-write bar)" if base == "first" else " (all-stay bar)" if base == "all" else "") + (f" λ={lam:g}" if lam else "")
        elif n == "auto": a = b = "n=auto"
        else: a = b = f"n={n}"
        if q.get("mode") == "ii": a, b = a + "・別の口", b + ", own stream"
        if q.get("max_frac") == "1": a, b = a + "・上限なし", b + ", no cap"
        ja.append("再挿入 " + a); en.append("rewrite " + b)
    extra = []
    if adm.startswith("dram"): extra.append(("篩", "admission"))
    elif adm.startswith("sizemax"): extra.append(("受入 ≤256M", "admit ≤256M"))
    if kv.get("chunk") == "0": extra.append(("chunk=0", "chunk=0"))
    if kv.get("seed"): extra.append((f"種 {kv['seed']}", f"seed {kv['seed']}"))
    s_ja, s_en = "｜".join(ja), " | ".join(en)
    for a, b in extra: s_ja += "＋" + a; s_en += " + " + b
    if rs and int(rs) == 268435456 and h == "none": s_ja, s_en = "CacheLib：" + s_ja, "CacheLib: " + s_en   # R=256 MiB・分類なし = CacheLib の形
    return s_ja, s_en

def dash_attr(pat):  # ' stroke-dasharray="…"'（f-string の式の中に \" を書くと Python 3.11 以前で構文エラーになるので関数に）
    return f' stroke-dasharray="{pat}"'


def tw(s, fs):  # 文字列の幅の目安（全角 1 em、半角 0.58 em）
    return sum(fs if ord(c) > 0x2E7F else fs * 0.58 for c in str(s))


def fmt(v, d=2, sign=False):
    if v is None or (isinstance(v, float) and math.isnan(v)): return "–"
    if abs(v) < 0.5 * 10 ** -d: return f"{0:.{d}f}"   # −0.000 と書かない
    return f"{v:+.{d}f}" if sign else f"{v:.{d}f}"


def nice_ticks(lo, hi, n=6):
    if hi <= lo: hi = lo + 1
    raw = (hi - lo) / n; p = 10 ** math.floor(math.log10(raw))
    step = next(p * m for m in (1, 2, 2.5, 5, 10) if raw <= p * m)
    t, out = math.ceil(lo / step - 1e-9) * step, []
    while t <= hi + step * 1e-9: out.append(round(t, 12)); t += step
    return out


def tick_label(v):
    if v == 0: return "0"
    a = abs(v)
    if a >= 1e4 or a < 1e-3: return f"{v:.0e}".replace("e+0", "e").replace("e-0", "e-")
    return f"{v:g}"


class Fig:
    """1600×900 の白地の図。軸（Ax）を 1 つ以上置き、凡例は上端に横に並べる。"""
    def __init__(s, W=1600, H=900):
        s.W, s.H, s.e, s.legend = W, H, [], []

    def t(s, x, y, txt, fs=24, anchor="start", col=INK, weight="normal", rot=None):
        tr = f' transform="rotate({rot} {x:.1f} {y:.1f})"' if rot is not None else ""
        s.e.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{fs}" text-anchor="{anchor}" fill="{col}" font-weight="{weight}"{tr}>{esc(str(txt))}</text>')

    def add_legend(s, items):  # items = [(名前, 色, 種類 'line'|'box'|'dot'|'dash'|'hollow')]
        for it in items:
            if it[0] not in [l[0] for l in s.legend]: s.legend.append(it)

    def svg(s):
        L, x, y, fs = [], 40, 44, 24
        for name, col, kind in s.legend:
            w = 46 + tw(name, fs) + 34
            if x + w > s.W - 30: x, y = 40, y + 38
            if kind == "box": L.append(f'<rect x="{x}" y="{y-18}" width="30" height="22" fill="{col}"/>')
            elif kind in ("dot", "hollow"): L.append(f'<circle cx="{x+15}" cy="{y-7}" r="10" fill="{col if kind == "dot" else "#fff"}" stroke="{col}" stroke-width="3"/>')
            else: L.append(f'<line x1="{x}" x2="{x+34}" y1="{y-7}" y2="{y-7}" stroke="{col}" stroke-width="5"{dash_attr("9 6") if kind == "dash" else ""}/>')
            L.append(f'<text x="{x+44}" y="{y}" font-size="{fs}" fill="{INK}">{esc(name)}</text>'); x += w
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {s.W} {s.H}" width="{s.W}" height="{s.H}" font-family="{FONT}">'
                f'<rect width="{s.W}" height="{s.H}" fill="#ffffff"/>' + "".join(L) + "".join(s.e) + "</svg>")

    def legend_h(s):  # 凡例の行数から、軸の上端を決める
        x, rows = 40, 1
        for name, _, _ in s.legend:
            w = 46 + tw(name, 24) + 34
            if x + w > s.W - 30: x, rows = 40, rows + 1
            x += w
        return 40 + 38 * rows if s.legend else 40


class Ax:
    """軸。x は数値（線形/対数）またはカテゴリ（cats）。y は数値（線形/対数）。"""
    def __init__(s, f, box, xr=None, yr=(0, 1), xlog=False, ylog=False, cats=None, xlabel="", ylabel="", title=None,
                 yticks=True, ylab=True, rot=None):
        s.f, (s.x0, s.y0, s.x1, s.y1) = f, box
        s.xlog, s.ylog, s.cats = xlog, ylog, cats
        if xlog and xr is not None:   # 対数軸の範囲は正に（0 以下が混ざると log10 が math domain error で止まる）
            hi = xr[1] if xr[1] > 0 else 1.0; xr = (xr[0] if 0 < xr[0] < hi else hi * 1e-3, hi)
        if ylog:
            hi = yr[1] if yr[1] > 0 else 1.0; yr = (yr[0] if 0 < yr[0] < hi else hi * 1e-3, hi)
        s.xr, s.yr = xr, yr
        if title: s.title(title)
        # y 目盛と格子
        if ylog:
            ts = [10 ** e for e in range(math.floor(math.log10(yr[0])), math.ceil(math.log10(yr[1])) + 1) if yr[0] <= 10 ** e <= yr[1]]
        else: ts = [t for t in nice_ticks(*yr) if yr[0] - 1e-12 <= t <= yr[1] + 1e-12]
        for t in ts if yticks else []:
            y = s.Y(t)
            f.e.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1.5"/>')
            if ylab: f.t(s.x0 - 12, y + 8, tick_label(t), 22, "end", MUT)
        if not ylog and yr[0] < 0 < yr[1]:
            f.e.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{s.Y(0):.1f}" y2="{s.Y(0):.1f}" stroke="#555" stroke-width="2.5"/>')
        # x 目盛
        if cats is not None:
            n = len(cats); s.bw = (s.x1 - s.x0) / max(n, 1)
            longest = max((tw(c, 22) for c in cats), default=0)
            rot = rot if rot is not None else (0 if longest < s.bw - 8 else -28)
            for i, c in enumerate(cats):
                x = s.x0 + s.bw * (i + 0.5)
                if rot: f.t(x + 6, s.y1 + 30, c, 22, "end", INK, rot=rot)
                else: f.t(x, s.y1 + 32, c, 22, "middle", INK)
        else:
            if xlog:
                xt = [10 ** e for e in range(math.floor(math.log10(xr[0])), math.ceil(math.log10(xr[1])) + 1) if xr[0] * 0.999 <= 10 ** e <= xr[1] * 1.001]
            else: xt = [t for t in nice_ticks(*xr) if xr[0] - 1e-12 <= t <= xr[1] + 1e-12]
            for t in xt:
                x = s.X(t)
                f.e.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{s.y0}" y2="{s.y1}" stroke="{GRID}" stroke-width="1"/>')
                f.t(x, s.y1 + 32, tick_label(t), 22, "middle", MUT)
        f.e.append(f'<rect x="{s.x0}" y="{s.y0}" width="{s.x1-s.x0}" height="{s.y1-s.y0}" fill="none" stroke="#999" stroke-width="1.5"/>')
        if xlabel: f.t((s.x0 + s.x1) / 2, f.H - 18, xlabel, 26, "middle")
        if ylabel: f.t(s.x0 - 92, (s.y0 + s.y1) / 2, ylabel, 26, "middle", rot=-90)

    def title(s, txt, fs=26, fs_min=19):
        """軸の幅（＋左右 20）に収まるよう字を小さくし、それでも入らなければ 2 行に折る（面を横に並べた図で隣の題と重ならない）"""
        cx, W = (s.x0 + s.x1) / 2, s.x1 - s.x0 + 40
        while fs > fs_min and tw(txt, fs) > W: fs -= 1
        if tw(txt, fs) <= W:
            s.f.t(cx, s.y0 - 14, txt, fs, "middle", weight="bold"); return
        k = txt.find("（")                                     # 「題（補足）」は補足の前で折る
        if k <= 0: k = min((i for i, c in enumerate(txt) if c == " "), key=lambda i: abs(i - len(txt) / 2), default=len(txt) // 2)
        a, b = txt[:k].rstrip(), txt[k:].lstrip()
        fs = 26 if fs_min < 26 else fs_min                     # 2 行なら大きい字から測り直す
        while fs > 14 and max(tw(a, fs), tw(b, fs)) > W: fs -= 1
        s.f.t(cx, s.y0 - 18 - fs, a, fs, "middle", weight="bold")
        s.f.t(cx, s.y0 - 12, b, fs, "middle", weight="bold")

    def X(s, v):
        a, b = s.xr
        if s.xlog: v, a, b = math.log10(max(v, a)), math.log10(a), math.log10(b)   # 0 以下の値は左端に描く
        return s.x0 + (s.x1 - s.x0) * (v - a) / ((b - a) or 1)

    def Y(s, v):
        a, b = s.yr
        if s.ylog: v, a, b = math.log10(max(v, 1e-300)), math.log10(a), math.log10(b)
        v = min(max(v, a - (b - a) * 0.02), b + (b - a) * 0.02)
        return s.y1 - (s.y1 - s.y0) * (v - a) / ((b - a) or 1)

    def line(s, xs, ys, col, w=4, dash=False, marker=False, label=None, op=1.0, hollow=False):
        pts = [(s.X(x), s.Y(y)) for x, y in zip(xs, ys) if x is not None and y is not None]
        if len(pts) > 1:
            s.f.e.append(f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="none" stroke="{col}" stroke-width="{w}" '
                         f'stroke-linejoin="round" opacity="{op}"{dash_attr("12 8") if dash else ""}/>')
        if marker:
            for a, b in pts:
                s.f.e.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="8" fill="{"#fff" if hollow else col}" stroke="{col}" stroke-width="3"/>')
        if label and pts:
            a, b = pts[-1]; s.f.t(min(a + 10, s.x1 - 4), b + 8, label, 22, "start" if a + 10 < s.x1 - 60 else "end", col, "bold")

    def dot(s, x, y, col, r=10, hollow=False, title=None):
        s.f.e.append(f'<circle cx="{s.X(x) if s.cats is None else x:.1f}" cy="{s.Y(y):.1f}" r="{r}" fill="{"#fff" if hollow else col}" '
                     f'stroke="{col}" stroke-width="3"/>')

    def vline(s, x, col="#c0504a", label=None):
        X = s.X(x)
        s.f.e.append(f'<line x1="{X:.1f}" x2="{X:.1f}" y1="{s.y0}" y2="{s.y1}" stroke="{col}" stroke-width="3" stroke-dasharray="10 7"/>')
        if label: s.f.t(X + 8, s.y0 + 28, label, 22, "start", col, "bold")

    def hline(s, y, col="#c0504a", label=None, dash=True):
        Y = s.Y(y)
        s.f.e.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{Y:.1f}" y2="{Y:.1f}" stroke="{col}" stroke-width="3"{dash_attr("10 7") if dash else ""}/>')
        if label: s.f.t(s.x1 - 8, Y - 10, label, 22, "end", col, "bold")

    def bars(s, groups, labels=True, d=2, sign=True):
        """groups = [(名前, 色, [値 or None]*カテゴリ数, [(lo,hi)]|None)]。カテゴリごとに横に並べる。"""
        k = len(groups); inner = s.bw * 0.8; w = inner / max(k, 1)
        fs = 20 if w > 80 else 17
        show = labels and w >= tw(fmt(-1.0, d, sign), fs) + 6   # 隣の棒の数字と重ならない幅があるときだけ
        fv = min(17, w - 3)   # 横に入らないときは縦書き（字の高さが棒の幅に入れば）
        vert = labels and not show and fv >= 10
        for gi, (name, col, vals, err) in enumerate(groups):
            for i, v in enumerate(vals):
                if v is None or v != v: continue
                x = s.x0 + s.bw * i + s.bw * 0.1 + w * gi
                y0, y1 = s.Y(0 if not s.ylog else s.yr[0]), s.Y(v)
                s.f.e.append(f'<rect x="{x+2:.1f}" y="{min(y0, y1):.1f}" width="{max(w-4, 1):.1f}" height="{abs(y1-y0):.1f}" fill="{col}"/>')
                top, bot = y1, y1
                if err and err[i]:
                    lo, hi = err[i]; cx = x + w / 2; top, bot = min(y1, s.Y(hi)), max(y1, s.Y(lo))
                    s.f.e.append(f'<path d="M{cx:.1f},{s.Y(lo):.1f}V{s.Y(hi):.1f}M{cx-9:.1f},{s.Y(lo):.1f}h18M{cx-9:.1f},{s.Y(hi):.1f}h18" stroke="#333" stroke-width="3" fill="none"/>')
                if show: s.f.t(x + w / 2, top - 8 if v >= 0 else bot + fs + 4, fmt(v, d, sign), fs, "middle", INK)
                elif vert:  # 棒の外（上 / 下）に縦に。軸の外にはみ出すなら、棒が十分長ければ棒の中に白で
                    txt = fmt(v, d, sign); L = tw(txt, fv); cx = x + w / 2 + fv * 0.35
                    out = top - 6 - L >= s.y0 if v >= 0 else bot + 6 + L <= s.y1
                    if out or abs(y1 - y0) < L + 10:
                        s.f.t(cx, top - 6 if v >= 0 else bot + 6, txt, fv, "start" if v >= 0 else "end", INK, rot=-90)
                    else:
                        s.f.t(cx, top + 6 if v >= 0 else bot - 6, txt, fv, "end" if v >= 0 else "start", "#fff", "bold", rot=-90)

    def stack(s, layers, labels=True, d=2):
        """layers = [(名前, 色, [値]*カテゴリ数)] を下から積む（正の値）。"""
        base = [0.0] * len(s.cats); w = s.bw * 0.6
        for name, col, vals in layers:
            for i, v in enumerate(vals):
                if not v: continue
                x = s.x0 + s.bw * (i + 0.2)
                ya, yb = s.Y(base[i]), s.Y(base[i] + v)
                s.f.e.append(f'<rect x="{x:.1f}" y="{yb:.1f}" width="{w:.1f}" height="{ya-yb:.1f}" fill="{col}"/>')
                if labels and ya - yb > 26: s.f.t(x + w / 2, (ya + yb) / 2 + 8, fmt(v, d), 20, "middle", "#fff", "bold")
                base[i] += v
        return base


def heat(f, box, rows, cols, M, title=None, d=3, lim=None, note=None):
    """M[r][c] を色の濃さで。正 = 青、負 = 赤（0 = 白）。"""
    x0, y0, x1, y1 = box; rl = max((tw(r, 24) for r in rows), default=0) + 24; x0 += rl
    cw, ch = (x1 - x0) / max(len(cols), 1), (y1 - y0) / max(len(rows), 1)
    vals = [abs(v) for r in M for v in r if v is not None]; lim = lim or (max(vals) if vals else 1) or 1
    if title: f.t((x0 + x1) / 2, y0 - 60, title, 26, "middle", weight="bold")
    for j, c in enumerate(cols):
        f.t(x0 + cw * (j + 0.5), y0 - 16, c, 22 if tw(c, 22) < cw - 6 else 18, "middle")
    for i, r in enumerate(rows):
        f.t(x0 - 14, y0 + ch * (i + 0.5) + 8, r, 24, "end")
        for j in range(len(cols)):
            v = M[i][j]; x, y = x0 + cw * j, y0 + ch * i
            if v is None:
                f.e.append(f'<rect x="{x+2:.1f}" y="{y+2:.1f}" width="{cw-4:.1f}" height="{ch-4:.1f}" fill="#f3f3f3"/>'); continue
            a = min(abs(v) / lim, 1); c = (59, 125, 191) if v > 0 else (192, 80, 74)
            rgb = tuple(int(255 - (255 - k) * a) for k in c)
            f.e.append(f'<rect x="{x+2:.1f}" y="{y+2:.1f}" width="{cw-4:.1f}" height="{ch-4:.1f}" fill="rgb{rgb}"/>')
            f.t(x + cw / 2, y + ch / 2 + 9, fmt(v, d, True), 24 if cw > 110 else 19, "middle", "#fff" if a > 0.55 else INK, "bold")
    if note: f.t(x1, y1 + 40, note, 20, "end", MUT)


def span(vals, pad=0.08, zero=True, lo_min=None):
    v = [x for x in vals if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if zero: v = v + [0]
    if not v: return (0, 1)
    lo, hi = min(v), max(v); p = (hi - lo) * pad or 0.1
    lo, hi = lo - p, hi + p
    if lo_min is not None: lo = max(lo, lo_min)
    return (lo, hi)


# ================================================================ 図の本体

def to_png(out, slugs):  # rsvg-convert / cairosvg / inkscape のどれか。LibreOffice は使わない（止まることがあった）
    svgs = [f"{out}/{s}.svg" for s in slugs]
    if shutil.which("rsvg-convert"):
        for s in svgs: subprocess.run(["rsvg-convert", "-w", "1600", "-o", s[:-4] + ".png", s], capture_output=True)
    else:
        try:
            import cairosvg
            for s in svgs: cairosvg.svg2png(url=s, write_to=s[:-4] + ".png", output_width=1600)
        except ImportError:
            if shutil.which("inkscape"):
                for s in svgs: subprocess.run(["inkscape", s, "--export-type=png", f"--export-filename={s[:-4]}.png", "-w", "1600"], capture_output=True)
    return all(os.path.exists(s[:-4] + ".png") for s in svgs) and bool(svgs)


def write_index(out, figs, png):
    H = ["""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>実験の図</title><style>
:root{--bg:#f6f5f2;--fg:#22211e;--mut:#6b675f;--line:#d9d4c9;--card:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732;--card:#24221f}}
:root[data-theme="dark"]{--bg:#1b1a18;--fg:#ebe7df;--mut:#a39d92;--line:#3a3732;--card:#24221f}
body{background:var(--bg);color:var(--fg);font:15px/1.75 system-ui,"Hiragino Sans","Yu Gothic UI",sans-serif;margin:0}main{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:1.5rem}nav{columns:2 320px;font-size:13.5px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px}nav a{color:inherit}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:22px 0}h2{font-size:1.1rem;margin:.2em 0 .6em}
img{width:100%;height:auto;border:1px solid var(--line);border-radius:6px;background:#fff}dl{display:grid;grid-template-columns:5em 1fr;gap:4px 12px;margin:.8em 0 0}
dt{color:var(--mut);font-weight:600}dd{margin:0}.m{color:var(--mut);font-size:13px}</style></head><body><main>
<h1>実験の図（スライド用）</h1><p class="m">各図は 1600×900（16:9）。SVG はそのまま PowerPoint に挿入できる（拡大しても劣化しない）。""" +
         ("PNG と slides.pptx も同じフォルダにある。" if png else "PNG は作れなかった（rsvg-convert などが無い）。") + "</p><nav>"]
    H += [f'<div><a href="#{s}">{esc(s[:2])}. {esc(t)}</a></div>' for s, t, *_ in figs] + ["</nav>"]
    for s, t, svg, pt, rd, df in figs:
        H.append(f'<section id="{s}"><h2>{esc(s[:2])}. {esc(t)}</h2><img src="{s}.svg" alt="{esc(t)}" loading="lazy"><dl><dt>要点</dt><dd>{esc(pt)}</dd>'
                 f'<dt>読み方</dt><dd>{esc(rd)}</dd>' + (f"<dt>定義</dt><dd>{esc(df)}</dd>" if df else "") + f'<dt>ファイル</dt><dd class="m">{s}.svg{" / " + s + ".png" if png else ""}</dd></dl></section>')
    open(f"{out}/index.html", "w", encoding="utf-8").write("\n".join(H) + "</main></body></html>")
    open(f"{out}/captions.md", "w", encoding="utf-8").write("\n".join(
        f"## {s[:2]}. {t}\n\nファイル: {s}.svg\n\n- 要点: {pt}\n- 読み方: {rd}\n" + (f"- 定義: {df}\n" if df else "") for s, t, _, pt, rd, df in figs))


# ---------------------------------------------------------------- PowerPoint（標準ライブラリで最小の pptx を組む）
NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
EMU = 914400


def _txt(i, name, x, y, w, h, paras):
    """paras = [(文字, 大きさpt, 太字, 色)]"""
    ps = "".join(f'<a:p><a:pPr><a:spcAft><a:spcPts val="600"/></a:spcAft></a:pPr><a:r><a:rPr lang="ja-JP" sz="{int(sz*100)}" b="{1 if b else 0}">'
                 f'<a:solidFill><a:srgbClr val="{c}"/></a:solidFill><a:latin typeface="Yu Gothic UI"/><a:ea typeface="Yu Gothic UI"/></a:rPr>'
                 f'<a:t>{esc(t)}</a:t></a:r></a:p>' for t, sz, b, c in paras)
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{i}" name="{name}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{int(x*EMU)}" y="{int(y*EMU)}"/>'
            f'<a:ext cx="{int(w*EMU)}" cy="{int(h*EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            f'<p:txBody><a:bodyPr wrap="square" lIns="0" rIns="0"><a:normAutofit/></a:bodyPr><a:lstStyle/>{ps}</p:txBody></p:sp>')


def write_pptx(path, out, figs):
    sid = range(1, len(figs) + 1)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="png" ContentType="image/png"/><Default Extension="svg" ContentType="image/svg+xml"/>'
          '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
          '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>' +
          "".join(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' for i in sid) + "</Types>")
    rel = lambda items: ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
                         "".join(f'<Relationship Id="{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{t}" Target="{g}"/>' for i, t, g in items) + "</Relationships>")
    grp = '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
    pres = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:presentation {NS}><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
            '<p:sldIdLst>' + "".join(f'<p:sldId id="{255+i}" r:id="rId{i+2}"/>' for i in sid) + '</p:sldIdLst>'
            '<p:sldSz cx="12192000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
    master = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster {NS}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree>{grp}</p:spTree></p:cSld>'
              '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
              '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>')
    layout = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout {NS} type="blank" preserve="1"><p:cSld name="Blank"><p:spTree>{grp}</p:spTree></p:cSld>'
              '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>')
    clr = "".join(f'<a:{n}><a:srgbClr val="{v}"/></a:{n}>' for n, v in (("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "1F1F1F"), ("lt2", "EEECE1"), ("accent1", "3B7DBF"),
                  ("accent2", "C9683A"), ("accent3", "3F9A64"), ("accent4", "8A5FB5"), ("accent5", "B8A02A"), ("accent6", "C0504A"), ("hlink", "0563C1"), ("folHlink", "954F72")))
    fill3 = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>' * 3
    theme = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="kit"><a:themeElements>'
             f'<a:clrScheme name="kit">{clr}</a:clrScheme><a:fontScheme name="kit">'
             '<a:majorFont><a:latin typeface="Yu Gothic UI"/><a:ea typeface="Yu Gothic UI"/><a:cs typeface=""/></a:majorFont>'
             '<a:minorFont><a:latin typeface="Yu Gothic UI"/><a:ea typeface="Yu Gothic UI"/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
             f'<a:fmtScheme name="kit"><a:fillStyleLst>{fill3}</a:fillStyleLst><a:lnStyleLst>' + '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>' * 3 +
             '</a:lnStyleLst><a:effectStyleLst>' + '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3 + f'</a:effectStyleLst><a:bgFillStyleLst>{fill3}</a:bgFillStyleLst></a:fmtScheme>'
             '</a:themeElements></a:theme>')
    z = zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED)
    z.writestr("[Content_Types].xml", ct)
    z.writestr("_rels/.rels", rel([("rId1", "officeDocument", "ppt/presentation.xml")]))
    z.writestr("ppt/presentation.xml", pres)
    z.writestr("ppt/_rels/presentation.xml.rels", rel([("rId1", "slideMaster", "slideMasters/slideMaster1.xml")] + [(f"rId{i+1}", "slide", f"slides/slide{i}.xml") for i in sid] +
                                                      [(f"rId{len(figs)+2}", "theme", "theme/theme1.xml")]))
    z.writestr("ppt/slideMasters/slideMaster1.xml", master)
    z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", rel([("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"), ("rId2", "theme", "../theme/theme1.xml")]))
    z.writestr("ppt/slideLayouts/slideLayout1.xml", layout)
    z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", rel([("rId1", "slideMaster", "../slideMasters/slideMaster1.xml")]))
    z.writestr("ppt/theme/theme1.xml", theme)
    for i, (s, t, _, pt, rd, df) in zip(sid, figs):
        z.write(f"{out}/{s}.png", f"ppt/media/fig{i}.png"); z.write(f"{out}/{s}.svg", f"ppt/media/fig{i}.svg")
        W, Hh = 8.9, 8.9 * 9 / 16
        pic = (f'<p:pic><p:nvPicPr><p:cNvPr id="3" name="{esc(s)}"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
               '<p:blipFill><a:blip r:embed="rId2"><a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">'
               '<asvg:svgBlip xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" r:embed="rId3"/></a:ext></a:extLst></a:blip>'
               '<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
               f'<p:spPr><a:xfrm><a:off x="{int(0.3*EMU)}" y="{int(1.25*EMU)}"/><a:ext cx="{int(W*EMU)}" cy="{int(Hh*EMU)}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')
        body = (_txt(2, "title", 0.4, 0.3, 12.5, 0.85, [(t, 26, True, "222222")]) + pic +
                _txt(4, "point", 9.45, 1.25, 3.6, 5.9, [("要点", 14, True, "3B7DBF"), (pt, 12, False, "222222"), ("読み方", 14, True, "3B7DBF"), (rd, 11, False, "444444")]))
        z.writestr(f"ppt/slides/slide{i}.xml", f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld {NS}><p:cSld><p:spTree>{grp}{body}</p:spTree></p:cSld>'
                                                '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>')
        z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", rel([("rId1", "slideLayout", "../slideLayouts/slideLayout1.xml"),
                                                               ("rId2", "image", f"../media/fig{i}.png"), ("rId3", "image", f"../media/fig{i}.svg")]))
    z.close()
