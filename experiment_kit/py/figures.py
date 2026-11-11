#!/usr/bin/env python3
"""figures.py — 集計結果から、スライド用の図（16:9 の SVG / PNG）と説明を一括で作る。aggregate.py の最後に呼ばれる。

  出力（work/results/figs/）:
    NN_名前.svg       図（1600×900、白地、大きな文字。PowerPoint に貼れる）
    NN_名前.png       同じ図の画像（変換器 rsvg-convert / cairosvg / inkscape / soffice のどれかがあれば）
    index.html        全部の図と説明（要点・読み方・定義）を並べたページ
    captions.md       説明だけのテキスト（スライドのノートに貼る用）
    slides.pptx       1 図 1 枚のスライド（左に図、右に要点と読み方）。PNG ができたときだけ
  標準ライブラリだけ。"""
import html, json, math, os, re, shutil, subprocess, zipfile

esc = html.escape
FONT = "'Noto Sans CJK JP','Noto Sans JP','Yu Gothic UI','Hiragino Sans',Meiryo,sans-serif"
INK, MUT, GRID = "#222222", "#666666", "#dddddd"
PAL = ["#3b7dbf", "#c9683a", "#3f9a64", "#8a5fb5", "#b8a02a", "#c0504a", "#2aa0a8", "#7d7d7d", "#d17fb0", "#5b6abf"]
HINT_COL = {"none": "#7d7d7d", "kf4": "#3b7dbf", "sf8": "#c9683a", "st": "#3f9a64"}
HINT_NAME = {"none": "分類なし", "kf4": "kf4（回数 4 分類）", "sf8": "sf8（サイズ×回数 8 分類）", "st": "st（SplitThreshold）"}
CTX_ORDER = ["全受け入れ", "全受け入れ + 再挿入", "予算 3 DWPD", "予算 3 DWPD + 再挿入", "予算 1 DWPD", "予算 1 DWPD + 再挿入"]


def hcol(h):
    return HINT_COL.setdefault(h, PAL[(len(HINT_COL) + 3) % len(PAL)])


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
            else: L.append(f'<line x1="{x}" x2="{x+34}" y1="{y-7}" y2="{y-7}" stroke="{col}" stroke-width="5"{" stroke-dasharray=\"9 6\"" if kind == "dash" else ""}/>')
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
        s.xr, s.yr = xr, yr
        if title: f.t((s.x0 + s.x1) / 2, s.y0 - 14, title, 26, "middle", weight="bold")
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

    def X(s, v):
        a, b = s.xr
        if s.xlog: v, a, b = math.log10(v), math.log10(a), math.log10(b)
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
                         f'stroke-linejoin="round" opacity="{op}"{" stroke-dasharray=\"12 8\"" if dash else ""}/>')
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
        s.f.e.append(f'<line x1="{s.x0}" x2="{s.x1}" y1="{Y:.1f}" y2="{Y:.1f}" stroke="{col}" stroke-width="3"{" stroke-dasharray=\"10 7\"" if dash else ""}/>')
        if label: s.f.t(s.x1 - 8, Y - 10, label, 22, "end", col, "bold")

    def bars(s, groups, labels=True, d=2, sign=True):
        """groups = [(名前, 色, [値 or None]*カテゴリ数, [(lo,hi)]|None)]。カテゴリごとに横に並べる。"""
        k = len(groups); inner = s.bw * 0.8; w = inner / max(k, 1)
        fs = 20 if w > 80 else 17
        show = labels and w >= tw(fmt(-1.0, d, sign), fs) + 6   # 隣の棒の数字と重ならない幅があるときだけ
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
def ctx(k):
    m = re.match(r"^(d\d+_)?(.+?)(_h1)?$", k)
    return ((f"予算 {m[1][1:-1]} DWPD" if m[1] else "全受け入れ") + (" + 再挿入" if m[3] else ""), m[2])


def build(R, rows, work, by_groups, spearman):
    """rows: aggregate.py の行（_j = 主結果の JSON、_v = 全変種）。by_groups: {列: {値: [行]}}。"""
    out = f"{R}/figs"; shutil.rmtree(out, ignore_errors=True); os.makedirs(out)
    figs = []   # (slug, 題, svg, 要点, 読み方, 定義)

    def add(slug, title, fig, point, read, define=""):
        figs.append((f"{len(figs)+1:02d}_{slug}", title, fig.svg(), point, read, define))

    if rows:   # トレース実験の図（validity の結果があるとき）
        effkeys = sorted({k for r in rows for k in r["_j"]["effect"]}, key=lambda k: (CTX_ORDER.index(ctx(k)[0]) if ctx(k)[0] in CTX_ORDER else 99, k))
        hints = [h for h in dict.fromkeys(ctx(k)[1] for k in effkeys)]
        ctxs = [c for c in CTX_ORDER if any(ctx(k)[0] == c for k in effkeys)]
        key = lambda c, h: next((k for k in effkeys if ctx(k) == (c, h)), None)
        names = [r["name"] for r in rows]; many = len(rows) > 1
        tcol = {r["name"]: PAL[i % len(PAL)] for i, r in enumerate(rows)}
        marks = sorted({float(w) for r in rows for w in r["_j"].get("at_writes", {})})
        WM = 1.5 if 1.5 in marks else (marks[0] if marks else None)
        EFF_DEF = "BMR の低下（この実験で言う「効果」）= BMR(分類なし) − BMR(分類あり) [pt]。BMR = ミスしたバイト ÷ 要求バイト。正なら分類でミスが減った（良い）。比べる相手は同じ条件（予算・再挿入の有無）の分類なし。"

        # ---- V1. BMR の内訳
        f = Fig(); f.add_legend([("初回アクセス（避けられないミス）", "#9aa5b1", "box"), ("容量不足（物単位 LRU でも出るミス）", "#3b7dbf", "box"),
                                 ("Region 化の余分（collateral）", "#c0504a", "box")])
        lay = [("cold", "#9aa5b1", [r["cold"] for r in rows]), ("lru", "#3b7dbf", [max(r["lru_bmr"] - r["cold"], 0) for r in rows]),
               ("col", "#c0504a", [max(r["collateral"], 0) for r in rows])]
        top = max(r["bmr_none"] for r in rows)
        a = Ax(f, (170, f.legend_h() + 30, 1560, 780), cats=names, yr=(0, top * 1.12), ylabel="BMR [%]", xlabel="トレース")
        tops = a.stack(lay)
        for i, r in enumerate(rows): f.t(a.x0 + a.bw * (i + 0.5), a.Y(tops[i]) - 12, f"{r['bmr_none']:.2f}%（余分 {r['collateral']:+.2f}）", 21, "middle", INK, "bold")
        worst = max(rows, key=lambda r: r["collateral"])
        add("bmr_breakdown", "BMR の内訳: Region 化でどれだけミスが増えるか", f,
            f"Region 化の余分は {worst['name']} で最大 {worst['collateral']:+.2f} pt（BMR {worst['bmr_none']:.2f}% のうち {100*worst['collateral']/max(worst['bmr_none'],1e-9):.0f}%）。"
            + "".join(f" {r['name']}: {r['collateral']:+.2f} pt。" for r in rows if r is not worst)[:300],
            "棒の高さ = Region キャッシュ（分類なし・全受け入れ）の BMR。下から、初回アクセス（どんなキャッシュでも避けられない）、"
            "同じ容量の物単位 LRU でも出るミス、Region ごと追い出すことで余分に出たミス（赤）。分類で取り戻せるのは赤の部分が上限の目安。",
            "collateral = BMR(Region、分類なし) − BMR(同じ容量の物単位 LRU) [pt]。全量で計算。")

        # ---- V2. 文脈ごとの分類の効果（全量）
        for c in ctxs:
            hs = [h for h in hints if key(c, h)]
            groups = [(HINT_NAME.get(h, h), hcol(h), [r["_j"]["effect"].get(key(c, h)) for r in rows], None) for h in hs]
            f = Fig(); f.add_legend([(g[0], g[1], "box") for g in groups])
            a = Ax(f, (170, f.legend_h() + 40, 1560, 780), cats=names, yr=span([v for g in groups for v in g[2]], 0.15),
                   ylabel="BMR の低下 [pt]（分類なし − 分類あり）", xlabel="トレース")
            a.bars(groups, d=3)
            best = [(r["name"], max(((h, r["_j"]["effect"].get(key(c, h))) for h in hs if r["_j"]["effect"].get(key(c, h)) is not None), key=lambda t: t[1], default=(None, None))) for r in rows]
            pos = sum(1 for g in groups for v in g[2] if v is not None and v > 0.02); tot = sum(1 for g in groups for v in g[2] if v is not None)
            add(f"effect_{CTX_ORDER.index(c)}", f"分類による BMR の低下（全量）: {c}", f,
                f"{tot} 個の（トレース, 分類）のうち、BMR の低下が +0.02 pt を超えたのは {pos} 個。" +
                " ".join((f"{n}: 最良は {HINT_NAME.get(h, h).split('（')[0]} {v:+.3f} pt。" if abs(v) >= 0.005 else f"{n}: どの分類でも差なし。") for n, (h, v) in best if h),
                f"条件「{c}」で、分類なしに比べて BMR が何 pt 下がったか。棒が 0 より上なら分類が効いている、下なら逆BMR の低下。",
                EFF_DEF)

        # ---- V3. トレースごとの条件 × 分類の表
        for r in rows:
            hs = [h for h in hints if any(key(c, h) for c in ctxs)]
            M = [[r["_j"]["effect"].get(key(c, h)) if key(c, h) else None for c in ctxs] for h in hs]
            f = Fig(); heat(f, (60, 170, 1560, 800), [HINT_NAME.get(h, h).split("（")[0] for h in hs], ctxs, M, title="BMR の低下 [pt]（分類なし − 分類あり、全量）　行 = 分類、列 = 条件",
                            note="セル = BMR の低下 [pt]（分類なし − 分類あり、全量）。青 = 分類で BMR が下がる、赤 = 上がる。灰 = その組を走らせていない")
            good = [(h, c, M[i][j]) for i, h in enumerate(hs) for j, c in enumerate(ctxs) if M[i][j] is not None and M[i][j] > 0.02]
            add(f"matrix_{r['name']}", f"{r['name']}: どの条件で、どの分類が効くか", f,
                ("BMR の低下が +0.02 pt を超えた組: " + "、".join(f"{h}・{c} {v:+.3f}" for h, c, v in good) + "。") if good else "どの組でもBMR の低下は +0.02 pt を超えない（分類で BMR は下がらない）。",
                "行 = 分類、列 = 条件（全受け入れ / 書き込み予算 3・1 DWPD、それぞれ再挿入の有無）。色が濃いほどBMR の低下が大きい。",
                EFF_DEF)

        # ---- V4. 先頭 N 要求でのBMR の低下の推移 / V5. 書き込み量での推移（全受け入れの分類ごと）
        for h in [h for h in hints if key("全受け入れ", h)]:
            k = key("全受け入れ", h)
            for mode in ("N", "W"):
                ser = []
                for r in rows:
                    cv = r["_j"]["curve"]
                    if k not in cv["eff"]: continue
                    xs = [n / r["_j"]["full_n"] for n in cv["n"]] if mode == "N" else cv.get("write_TB", [])
                    p = [(x, y) for x, y in zip(xs, cv["eff"][k]) if x and x > 0]
                    if p: ser.append((r, p))
                if not ser: continue
                xr = (min(x for _, p in ser for x, _ in p), max(x for _, p in ser for x, _ in p))
                f = Fig(); f.add_legend([(r["name"], tcol[r["name"]], "line") for r, _ in ser] + ([("全量の値", INK, "hollow")]))
                a = Ax(f, (170, f.legend_h() + 30, 1520, 790), xr=xr, xlog=True, yr=span([y for _, p in ser for _, y in p], 0.1),
                       ylabel="BMR の低下 [pt]（分類なし − 分類あり）", xlabel="先頭 N 要求 ÷ 全要求数（対数。右端 = 全量）" if mode == "N" else "先頭から SSD に書いた量 [TB]（全受け入れ・分類なし。対数）")
                if mode == "W":
                    for m in marks:
                        if xr[0] <= m <= xr[1]: a.vline(m, label=f"{m:g} TB")
                for r, p in ser:
                    a.line([x for x, _ in p], [y for _, y in p], tcol[r["name"]], 4)
                    a.dot(p[-1][0], p[-1][1], tcol[r["name"]], 11, hollow=True)
                if mode == "N":
                    txt = []
                    for r, _ in ser:
                        cv = r["_j"]["conv"][k]["0.1"]["frac"]; txt.append(f"{r['name']}: 全量 {r['_j']['effect'][k]:+.3f} pt、±0.1 pt に収まるのは先頭 {fmt(None if cv is None else 100*cv, 0)}% から")
                    add(f"prefixN_{h}", f"部分トレースで足りるか: {HINT_NAME.get(h, h)} による BMR の低下を、先頭 N 要求で測ると", f, "。".join(txt) + "。",
                        "横軸 = 使った要求の割合（対数）。線が右端（全量 = 白丸）の高さに早く落ち着くほど、短い部分トレースでも同じ結論になる。"
                        "線が 0 を横切る区間では、部分トレースだとBMR の低下の向き（符号）が逆に出る。", EFF_DEF + " 先頭 N 要求までの累積で計算（LRU 系は全量 1 回の run から正確に得られる）。")
                else:
                    txt = []
                    for r, _ in ser:
                        a_ = r["_j"].get("at_writes", {}).get(str(WM)) if WM else None
                        if a_ and a_["eff"].get(k) is not None:
                            txt.append(f"{r['name']}: {WM:g} TB 時点 {a_['eff'][k]:+.3f} pt / 全量 {r['_j']['effect'][k]:+.3f} pt（符号{'一致' if a_['sign_ok'].get(k) else '不一致'}）")
                    add(f"prefixW_{h}", f"書いた量で測ると: {HINT_NAME.get(h, h)} による BMR の低下", f, ("。".join(txt) + "。") if txt else "W の印に届くトレースが無い。",
                        "横軸 = 先頭から SSD に書いた量（対数）。縦の破線 = 比べる書き込み量（WRITE_MARKS_TB）。破線と線の交点が「その量だけ書いた部分トレース」で得られるBMR の低下。",
                        EFF_DEF)

        # ---- V6. 書き込み量の印ごとのBMR の低下（全構成）/ V7. 符号が安定する書き込み量 / V8. 収束
        for r in rows:
            j = r["_j"]; aw = j.get("at_writes", {})
            ms = [m for m in marks if aw.get(str(m))]
            if ms:
                f = Fig(); xs = ms + [j["full_write_TB"]] + list(j.get("full_write_TB_by_key", {}).values())
                hs = [h for h in hints if any(key(c, h) for c in ctxs)]
                f.add_legend([(c, PAL[i], "line") for i, c in enumerate(ctxs)])
                W0, gap = 170, 50; pw = (1560 - W0 - gap * (len(hs) - 1)) / len(hs)
                yr = span([v for k in j["effect"] for v in [j["effect"][k]] + [aw[str(m)]["eff"].get(k) for m in ms]], 0.1)
                for hi_, h in enumerate(hs):
                    a = Ax(f, (W0 + hi_ * (pw + gap), f.legend_h() + 70, W0 + hi_ * (pw + gap) + pw, 780), xr=(min(xs) * 0.7, max(xs) * 1.4), xlog=True, yr=yr,
                           title=HINT_NAME.get(h, h).split("（")[0], ylabel="BMR の低下 [pt]（分類なし − 分類あり）" if hi_ == 0 else "", ylab=hi_ == 0)
                    if WM: a.vline(WM)
                    for ci, c in enumerate(ctxs):
                        k = key(c, h)
                        if not k or k not in j["effect"]: continue
                        xk = ms + [j.get("full_write_TB_by_key", {}).get(k, j["full_write_TB"])]
                        a.line(xk, [aw[str(m)]["eff"].get(k) for m in ms] + [j["effect"][k]], PAL[ci], 3.5, marker=True)
                f.t(f.W / 2 + 60, f.H - 18, "先頭から SSD に書いた量 [TB]（対数）。各線の右端 = 全量。赤の破線 = 比べる量", 26, "middle")
                ok = [(k, aw[str(WM)]["sign_ok"].get(k)) for k in effkeys if k in j["effect"]] if WM and aw.get(str(WM)) else []
                bad = [k for k, s in ok if s is False]; nn = [k for k, s in ok if s is None]
                add(f"marks_{r['name']}", f"{r['name']}: 先頭 W TB 書いた時点の BMR の低下は全量に近いか（全構成）", f,
                    (f"{WM:g} TB の時点で、符号が全量と一致: {sum(1 for _, s in ok if s)} / {sum(1 for _, s in ok if s is not None)} 構成。" +
                     (f"不一致: {', '.join(bad)}。" if bad else "") + (f"未達・BMR の低下が小さすぎて判定外: {', '.join(nn)}。" if nn else "")) if ok else "",
                    "分類ごとの小さな図。各線 = 条件（色）。横軸 = その条件の分類なしが SSD に書いた量で、印（WRITE_MARKS_TB）ごとのBMR の低下と、右端 = 全量のBMR の低下をつなぐ。"
                    "赤の破線 = 比べる書き込み量。線が破線のところで右端と同じ側（0 の上 / 下）にあれば、その量の部分トレースで向きは再現できる。",
                    EFF_DEF + " 予算つきの構成は書き込みが遅いので、全受け入れより多くの要求を流した時点になる。")
            st_ = j.get("sign_stable", {})
            ks = [k for k in effkeys if st_.get(k)]
            if ks:
                f = Fig(); f.add_legend([(c, PAL[i], "box") for i, c in enumerate(ctxs)])
                vals = [st_[k]["write_TB"] for k in ks]
                lo = max(min(vals + ([WM] if WM else [])) * 0.5, 1e-3); hi = max(vals + [j["full_write_TB"]]) * 1.6
                labs = [k for k in ks]
                # 横棒: y = 構成、x = 符号が以後安定する書き込み量
                x0, y0, x1, y1 = 330, f.legend_h() + 30, 1540, 790; bh = (y1 - y0) / len(ks)
                ax = Ax(f, (x0, y0, x1, y1), xr=(lo, hi), xlog=True, yr=(0, len(ks)), yticks=False, xlabel="BMR の低下の符号が、以後ずっと全量と同じになる書き込み量 [TB]（対数）")
                for i, k in enumerate(labs):
                    y = y0 + bh * i; c = ctx(k)[0]; col = PAL[ctxs.index(c)] if c in ctxs else MUT
                    f.e.append(f'<rect x="{x0}" y="{y+bh*0.18:.1f}" width="{ax.X(st_[k]["write_TB"])-x0:.1f}" height="{bh*0.64:.1f}" fill="{col}"/>')
                    f.t(x0 - 12, y + bh / 2 + 8, k, 22, "end")
                    f.t(ax.X(st_[k]["write_TB"]) + 8, y + bh / 2 + 8, f"{st_[k]['write_TB']:.2g} TB", 20, "start", INK)
                for xv, col, lab in ((WM, "#c0504a", f"{WM:g} TB"), (j["full_write_TB"], "#555", "全量")):
                    if xv: ax.vline(xv, col); f.t(ax.X(xv), y0 - 10, lab, 22, "middle", col, "bold")
                late = [k for k in ks if WM and st_[k]["write_TB"] > WM]
                add(f"signstable_{r['name']}", f"{r['name']}: 何 TB 書けば BMR の低下の向き（符号）が決まるか", f,
                    (f"{WM:g} TB より後でないと符号が定まらない構成: {', '.join(late)}。" if late else f"全構成で {WM:g} TB までに符号が定まる。") +
                    f" 判定外（全量のBMR の低下が ±0.005 pt 未満）: {', '.join(k for k in effkeys if k in st_ and k not in ks) or 'なし'}。",
                    "棒の長さ = その書き込み量より先では、BMR の低下の符号（向き）がずっと全量と同じ。棒が赤の破線（比べる量）より短ければ、その量の部分トレースで向きは正しく出る。色 = 条件。",
                    "書き込み量は、その構成と同じ条件（予算）の分類なしが SSD に書いた量。")
            cv = j.get("conv", {})
            ks = [k for k in effkeys if k in cv]
            if ks:
                f = Fig(); f.add_legend([("BMR の低下が全量の ±0.1 pt に収まる", "#8fb3d9", "box"), ("±0.02 pt に収まる", "#2f5f8f", "box")])
                g = [("0.1", "#8fb3d9", [None if cv[k]["0.1"]["frac"] is None else 100 * cv[k]["0.1"]["frac"] for k in ks], None),
                     ("0.02", "#2f5f8f", [None if cv[k]["0.02"]["frac"] is None else 100 * cv[k]["0.02"]["frac"] for k in ks], None)]
                a = Ax(f, (170, f.legend_h() + 30, 1560, 720), cats=ks, yr=(0, 105), ylabel="必要な先頭の要求の割合 [%]", rot=-35)
                a.bars(g, labels=len(ks) <= 14, d=0, sign=False)
                m01 = [100 * cv[k]["0.1"]["frac"] for k in ks if cv[k]["0.1"]["frac"] is not None]
                add(f"conv_{r['name']}", f"{r['name']}: BMR の低下が全量の値に収まるまでに要る要求の割合", f,
                    f"±0.1 pt に収まるには先頭 {min(m01):.0f}〜{max(m01):.0f}% の要求が要る（構成による）。" if m01 else "",
                    "棒 = 以後ずっとBMR の低下が全量の ±0.1 pt（薄）/ ±0.02 pt（濃）に収まる、最小の先頭の割合。低いほど短い部分トレースで大きさまで再現できる。",
                    "割合 = 先頭 N 要求 ÷ 全要求数。")

        # ---- V9. collateral の推移（書き込み量）
        ser = [(r, [(w, c) for w, c in zip(r["_j"]["curve"].get("write_TB", []), r["_j"]["curve"]["collateral"]) if w > 0]) for r in rows]
        ser = [(r, p) for r, p in ser if p]
        if ser:
            f = Fig(); f.add_legend([(r["name"], tcol[r["name"]], "line") for r, _ in ser])
            xr = (min(x for _, p in ser for x, _ in p), max(x for _, p in ser for x, _ in p))
            a = Ax(f, (170, f.legend_h() + 30, 1520, 790), xr=xr, xlog=True, yr=span([y for _, p in ser for _, y in p], 0.1),
                   ylabel="BMR の増分 [pt]（Region − 物単位 LRU）", xlabel="先頭から SSD に書いた量 [TB]（全受け入れ・分類なし。対数）")
            for m in marks:
                if xr[0] <= m <= xr[1]: a.vline(m, label=f"{m:g} TB")
            for r, p in ser: a.line([x for x, _ in p], [y for _, y in p], tcol[r["name"]], 4); a.dot(p[-1][0], p[-1][1], tcol[r["name"]], 11, hollow=True)
            txt = []
            for r, _ in ser:
                a_ = r["_j"].get("at_writes", {}).get(str(WM)) if WM else None
                if a_: txt.append(f"{r['name']}: {WM:g} TB 時点 {a_['collateral']:+.3f} pt / 全量 {r['collateral']:+.3f} pt")
            add("collateral_curve", "Region 化の余分（collateral）は短いトレースでも測れるか", f, ("。".join(txt) + "。") if txt else "",
                "横軸 = 先頭から書いた量。線 = その時点までの collateral。右端の白丸 = 全量。早く平らになるほど、短いトレースで Region 化の損を正しく見積もれる。",
                "collateral = BMR(Region、分類なし) − BMR(同じ容量の物単位 LRU) [pt]。")

        # ---- V10. SSD への書き込み量（全量、構成ごと）
        for r in rows:
            fu = r["_j"]["full"]; ks = [k for k in fu if not k.startswith("obj_") and fu[k].get("write")]
            ks.sort(key=lambda k: (CTX_ORDER.index(ctx(k)[0]) if ctx(k)[0] in CTX_ORDER else 9, -1 if ctx(k)[1] == "none" else (hints.index(ctx(k)[1]) if ctx(k)[1] in hints else 9)))
            if not ks: continue
            f = Fig(); f.add_legend([("受け入れ（ミスした物を書く）", "#3b7dbf", "box"), ("再挿入（追い出し前に書き直す）", "#c9683a", "box")])
            lay = [("a", "#3b7dbf", [(fu[k]["write"] - fu[k]["reinsert"]) / 1e12 for k in ks]), ("r", "#c9683a", [fu[k]["reinsert"] / 1e12 for k in ks])]
            a = Ax(f, (170, f.legend_h() + 30, 1560, 720), cats=ks, yr=(0, max(fu[k]["write"] for k in ks) / 1e12 * 1.12), ylabel="SSD に書いた量 [TB]", rot=-35)
            a.stack(lay, labels=len(ks) <= 14, d=1)
            d0 = r["_j"]["t_sec"] / 86400; ssd = r["_j"]["config"]["ssd"]
            nw = fu["none"]["write"]; mx = max(ks, key=lambda k: fu[k]["write"])
            add(f"writes_{r['name']}", f"{r['name']}: 構成ごとの SSD 書き込み量（寿命の負担）", f,
                f"分類なし・全受け入れで {nw/1e12:.1f} TB（{nw/ssd/d0:.1f} DWPD）。最大は {mx} の {fu[mx]['write']/1e12:.1f} TB（分類なしの {fu[mx]['write']/nw:.2f} 倍）。",
                "棒 = 全量を流す間に SSD に書いたバイト。青 = ミスした物の受け入れ、橙 = 再挿入。予算つき（d3_, d1_）は書き込みが予算で抑えられる。",
                "DWPD = 書いた量 ÷ SSD 容量 ÷ 日数。")

        # ---- V11. 埋め値の扱い（変種）の比較
        for r in rows:
            v = r["_v"]
            if len(v) < 2: continue
            vn = sorted(v, key=lambda x: ["drop", "delete", "fix", "asis"].index(x) if x in ("drop", "delete", "fix", "asis") else 9); ks = [k for k in effkeys if all(k in v[x]["effect"] for x in vn)]
            g = [(x, PAL[i], [v[x]["effect"][k] for k in ks], None) for i, x in enumerate(vn)]
            f = Fig(); f.add_legend([(x, PAL[i], "box") for i, x in enumerate(vn)])
            a = Ax(f, (170, f.legend_h() + 30, 1560, 720), cats=ks, yr=span([y for _, _, ys, _ in g for y in ys], 0.15), ylabel="BMR の低下 [pt]（分類なし − 分類あり、全量）", rot=-35)
            a.bars(g, labels=len(ks) * len(vn) <= 30, d=3)
            dmax = max(abs(v[vn[0]]["effect"][k] - v[x]["effect"][k]) for k in ks for x in vn[1:])
            same = all((v[vn[0]]["effect"][k] > 0) == (v[x]["effect"][k] > 0) for k in ks for x in vn[1:])
            add(f"variants_{r['name']}", f"{r['name']}: size = 2^32−1 の扱いで結論は変わるか", f,
                f"変種（{', '.join(vn)}）の間でBMR の低下の差は最大 {dmax:.3f} pt、符号は{'すべて一致' if same else '一部で不一致'}。"
                + " ".join(f"BMR(分類なし) {x}: {v[x]['full']['none']['bmr']:.2f}%。" for x in vn),
                "同じ構成のBMR の低下を、埋め値（サイズ 2^32−1）の扱い方ごとに並べた。棒の高さと向きがそろっていれば、扱いに結論は左右されない。",
                "drop = その要求を無かったことにする / delete = その物を消す操作とみなす（要求に数えない）/ fix = その物の他の要求のサイズに置き換え / asis = そのまま。")

        # ---- V12. グループごとの傾向（平均と範囲）
        for b, g in by_groups.items():
            gs = sorted(g); hs = [h for h in hints if key("全受け入れ", h)]
            if not hs: continue
            groups = []
            for h in hs:
                k = key("全受け入れ", h)
                vals = [[r["_j"]["effect"].get(k) for r in g[x] if r["_j"]["effect"].get(k) is not None] for x in gs]
                groups.append((HINT_NAME.get(h, h).split("（")[0], hcol(h), [sum(v) / len(v) if v else None for v in vals], [(min(v), max(v)) if len(v) > 1 else None for v in vals]))
            f = Fig(); f.add_legend([(n, c, "box") for n, c, _, _ in groups])
            a = Ax(f, (170, f.legend_h() + 30, 1560, 760), cats=[f"{x}（{len(g[x])} 本）" for x in gs],
                   yr=span([y for _, _, ys, er in groups for y in ys] + [t for _, _, _, er in groups for e in er if e for t in e], 0.15), ylabel="BMR の低下 [pt]（全受け入れ）", xlabel=b)
            a.bars(groups, d=3)
            add(f"group_{b.replace('/', '_')}", f"「{b}」ごとの傾向: 分類による BMR の低下（全受け入れ）", f,
                " ".join(f"{x}: " + "、".join(f"{n} {fmt(ys[i], 3, True)}" for n, _, ys, _ in groups) + "。" for i, x in enumerate(gs)),
                f"棒 = そのグループのトレースのBMR の低下の平均。黒い縦線 = 最小〜最大（2 本以上のとき）。グループ = traces.tsv の {b} 列。本数が少ないうちは目安。",
                EFF_DEF)

        # ---- V13. 特徴とBMR の低下の順位相関
        if len(rows) >= 4:
            fk = sorted({k for r in rows for k in r if k.startswith("f_")})
            tg = ["collateral"] + [f"eff_{key('全受け入れ', h)}" for h in hints if key("全受け入れ", h)]
            M = [[spearman([r.get(k) for r in rows], [r.get(t) for r in rows]) for t in tg] for k in fk]
            if fk:
                f = Fig(); heat(f, (60, 140, 1560, 830), [k[2:] for k in fk], [t.replace("eff_", "BMR 低下 ").replace("collateral", "BMR 増分(Region)") for t in tg], M, d=2, lim=1,
                                note=f"Spearman 順位相関 ρ（トレース {len(rows)} 本）。青 = 正の相関、赤 = 負")
                top = sorted(((abs(M[i][j]), fk[i][2:], tg[j]) for i in range(len(fk)) for j in range(len(tg)) if M[i][j] is not None), reverse=True)[:3]
                add("spearman", "どんなトレースで分類が効くか: 特徴と BMR の低下の順位相関", f,
                    "相関が強い組: " + "、".join(f"{a} と {t.replace('eff_', 'BMR 低下 ')}（|ρ| = {v:.2f}）" for v, a, t in top) + "。" if top else "",
                    "行 = トレースの特徴（og_trace_stats の 10 窓の平均など）、列 = BMR の低下と collateral。|ρ| が 1 に近いほど、その特徴の大小でBMR の低下の大小が説明できる。本数が少ないと偶然でも大きく出る。",
                    "var_logs_b = バイト重みのサイズのばらつき Var(log2 s)、rho = サイズと人気の順位相関、gini = 人気の偏り、size_change = 同じ物のサイズが変わる要求の割合。")

    # ================================================================ 合成トレース
    synth_figs(work, add)

    # ================================================================ 出力
    for slug, title, svg, *_ in figs:
        open(f"{out}/{slug}.svg", "w", encoding="utf-8").write(svg)
    pngok = to_png(out, [s for s, *_ in figs])
    write_index(out, figs, pngok)
    if pngok: write_pptx(f"{out}/slides.pptx", out, figs)
    return out, len(figs), pngok


# ---------------------------------------------------------------- 合成トレース
POL_NAME = {"lru": "物単位 LRU", "part[1]": "静的分割（サイズ）", "rfifo[0]": "Region FIFO", "rfifo[1]": "Region FIFO＋サイズ分類",
            "rlru[0]": "Region LRU", "rlru[1]": "Region LRU＋サイズ分類", "rlru[2]": "Region LRU＋グループ分類"}
POL_COL = {"lru": "#222222", "part[1]": "#8a5fb5", "rfifo[0]": "#b8a02a", "rfifo[1]": "#2aa0a8", "rlru[0]": "#7d7d7d", "rlru[1]": "#3f9a64", "rlru[2]": "#c9683a"}


def synth_figs(work, add):
    import glob
    sj = [json.load(open(p)) for p in sorted(glob.glob(f"{work}/synth/*.json")) if not p.endswith(("scaled.json", "writes.json"))]
    sj = [j for j in sj if isinstance(j, dict) and "sim" in j]
    MDEF = "OHR = ヒットした要求 ÷ 要求、BHR = ヒットしたバイト ÷ 要求バイト。理論 = Che 近似（物単位 LRU）と、それを Region に広げたモデル（synth_model）。実測 = cachesim（先頭 25% で温める）。"
    if sj:
        # S1. 理論と実測の一致
        f = Fig(); pts = {}
        for j in sj:
            for fk, s in j["sim"].items():
                m = j["model"].get(fk, {})
                for nm, (so, sb) in s.items():
                    mk = "rfifo" if nm.startswith("rfifo") else nm
                    if mk in m: pts.setdefault(nm, []).append((float(m[mk]["ohr"]), so, float(m[mk]["bhr"]), sb))
        f.add_legend([(POL_NAME.get(n, n), POL_COL.get(n, MUT), "dot") for n in pts])
        err = {}
        for pi, (lab, io, im) in enumerate((("OHR", 1, 0), ("BHR", 3, 2))):
            vals = [p[i] for ps in pts.values() for p in ps for i in (io, im)]
            lo, hi = max(min(vals) - 0.03, 0), min(max(vals) + 0.03, 1)
            a = Ax(f, (170 + pi * 720, f.legend_h() + 60, 170 + pi * 720 + 600, 790), xr=(lo, hi), yr=(lo, hi), title=lab,
                   xlabel=f"理論の {lab}", ylabel="実測のヒット率（OHR / BHR、0〜1）" if pi == 0 else "")
            a.line([lo, hi], [lo, hi], "#999", 2, dash=True)
            for n, ps in pts.items():
                for p in ps: a.dot(p[im], p[io], POL_COL.get(n, MUT), 8)
                err.setdefault(n, []).extend(p[io] - p[im] for p in ps)
        add("synth_model_vs_sim", "合成トレース: 理論は実測をどこまで当てるか", f,
            "実測 − 理論の平均（OHR と BHR をまとめて）: " + "、".join(f"{POL_NAME.get(n, n)} {sum(e)/len(e):+.3f}（最大 {max(map(abs, e)):.3f}）" for n, e in err.items()) + "。",
            "点 = （負荷, 容量, 方式）の 1 組。横 = 理論、縦 = 実測。破線（対角）に乗っていれば理論どおり。左 = OHR、右 = BHR。", MDEF)
        # S2. 負荷ごと: 容量と OHR/BHR
        for j in sj:
            fks = sorted(j["sim"], key=float); f = Fig()
            nms = [n for n in POL_NAME if any(n in j["sim"][k] for k in fks)]
            f.add_legend([(POL_NAME[n], POL_COL[n], "line") for n in nms] + [("理論（破線）", "#555", "dash")])
            for pi, (lab, ii) in enumerate((("OHR", 0), ("BHR", 1))):
                ys = [j["sim"][k][n][ii] for k in fks for n in nms if n in j["sim"][k]]
                a = Ax(f, (170 + pi * 720, f.legend_h() + 60, 170 + pi * 720 + 600, 790), xr=(float(fks[0]) * 0.9, float(fks[-1]) * 1.05), yr=span(ys, 0.08, zero=False),
                       title=lab, xlabel="容量 ÷ フットプリント", ylabel="ヒット率（左 OHR・右 BHR、0〜1）" if pi == 0 else "")
                for n in nms:
                    xs = [float(k) for k in fks if n in j["sim"][k]]
                    a.line(xs, [j["sim"][k][n][ii] for k in fks if n in j["sim"][k]], POL_COL[n], 4, marker=True)
                    mk = "rfifo" if n.startswith("rfifo") else n
                    mv = [(float(k), float(j["model"][k][mk]["ohr" if ii == 0 else "bhr"])) for k in fks if mk in j["model"].get(k, {})]
                    if mv: a.line([x for x, _ in mv], [y for _, y in mv], POL_COL[n], 3, dash=True, op=0.8)
            k0 = fks[-1]; s = j["sim"][k0]
            g = (s["rlru[1]"][0] - s["rlru[0]"][0], s["rlru[1]"][1] - s["rlru[0]"][1]) if "rlru[1]" in s and "rlru[0]" in s else None
            add(f"synth_{j['name']}", f"合成負荷 {j['name']}: 容量とヒット率", f,
                (f"容量 {float(k0):.2f} で、サイズ分類は Region LRU の OHR を {g[0]:+.3f}、BHR を {g[1]:+.3f} 変える。" if g else "") +
                (f" 物単位 LRU との差（Region LRU）: OHR {s['rlru[0]'][0]-s['lru'][0]:+.3f}、BHR {s['rlru[0]'][1]-s['lru'][1]:+.3f}。" if "rlru[0]" in s and "lru" in s else ""),
                "横軸 = キャッシュ容量（フットプリントに対する割合）。実線と点 = 実測、同じ色の破線 = 理論。左 = OHR、右 = BHR。",
                MDEF + f" 負荷の定義 = サイズ:物の数:要求の割合:Zipf 指数（{' '.join(j['spec'])}）。")
        # S3. サイズ分類の利得
        f = Fig(); cats, go, gb, mo, mb = [], [], [], [], []
        for j in sj:
            for k in sorted(j["sim"], key=float):
                s, m = j["sim"][k], j["model"].get(k, {})
                if "rlru[1]" not in s or "rlru[0]" not in s: continue
                cats.append(f"{j['name']} {float(k):.2f}")
                go.append(100 * (s["rlru[1]"][0] - s["rlru[0]"][0])); gb.append(100 * (s["rlru[1]"][1] - s["rlru[0]"][1]))   # [pt]
                ok = "rlru[1]" in m and "rlru[0]" in m
                mo.append(100 * (float(m["rlru[1]"]["ohr"]) - float(m["rlru[0]"]["ohr"])) if ok else None)
                mb.append(100 * (float(m["rlru[1]"]["bhr"]) - float(m["rlru[0]"]["bhr"])) if ok else None)
        if cats:
            f.add_legend([("OHR の上昇（実測）", "#3b7dbf", "box"), ("BHR の上昇（実測）", "#c9683a", "box"), ("理論", "#222", "hollow")])
            a = Ax(f, (170, f.legend_h() + 30, 1560, 720), cats=cats, yr=span(go + gb + [x for x in mo + mb if x is not None], 0.15),
                   ylabel="OHR・BHR の上昇 [pt]（サイズ分類 − 分類なし）", rot=-40)
            a.bars([("o", "#3b7dbf", go, None), ("b", "#c9683a", gb, None)], labels=False)
            for i in range(len(cats)):
                for gi, mv in enumerate((mo[i], mb[i])):
                    if mv is not None: a.dot(a.x0 + a.bw * i + a.bw * 0.1 + a.bw * 0.4 * (gi + 0.5), mv, "#222", 7, hollow=True)
            add("synth_split_gain", "合成トレース: サイズで分けると OHR / BHR はどう変わるか（Region LRU）", f,
                f"OHR の上昇は {min(go):+.1f}〜{max(go):+.1f} pt、BHR の上昇は {min(gb):+.1f}〜{max(gb):+.1f} pt（負 = 下がった）。"
                f"OHR が上がり BHR が下がる組（±0.5 pt を超えるもの）: {sum(1 for o, b in zip(go, gb) if o > 0.5 and b < -0.5)} / {len(go)}。",
                "横 = 負荷と容量の組。棒 = ヒット率（OHR は要求数、BHR はバイトで数える）のサイズ分類あり − なし [pt]（実測）、白丸 = 同じ差の理論値。"
                "青が上・橙が下なら、小さい物を優先して物の数では得をし、バイトでは損をしている（BHR が下がる = BMR が上がる）。",
                MDEF)
    # S4〜S6. 縮小した 240 GiB 想定の負荷
    p = f"{work}/synth/scaled.json"
    if os.path.exists(p) and os.path.getsize(p) > 2:
        d = json.load(open(p)); pols = ["none", "split", "none+re", "split+re"]
        wr = sorted({tuple(k.split("|")[:2]) for k in d if k.count("|") == 2 and k.split("|")[2] in pols})
        if wr:
            PC = {"none": "#7d7d7d", "split": "#3f9a64", "none+re": "#c9683a", "split+re": "#3b7dbf"}
            PN = {"none": "分類なし", "split": "サイズ分類", "none+re": "分類なし＋再挿入", "split+re": "サイズ分類＋再挿入"}
            f = Fig(); f.add_legend([(PN[q], PC[q], "box") for q in pols] + [("物単位 LRU", "#222", "line"), ("理論", "#222", "hollow")])
            cats = [f"{w}・R={r}" for w, r in wr]
            g = [(PN[q], PC[q], [100 * d.get(f"{w}|{r}|{q}", {}).get("bmr", float("nan")) for w, r in wr], None) for q in pols]
            def model_of(w, r):   # H0 = 定常、H<N> = 立ち上がりの隔離込み（実測に近い方）を使う
                mks = sorted(k for k in d if k.startswith(f"{w}|{r}|model_H"))
                mk = next((k for k in mks if not k.endswith("H0")), mks[0] if mks else None)
                return {nm: 100 * (1 - d[mk][nm][1]) for nm in ("rlru[0]", "rlru[1]") if mk and nm in d[mk]}
            vals = ([v for _, _, ys, _ in g for v in ys if v == v] + [100 * d[f"{w}|lru"]["bmr"] for w, _ in wr if f"{w}|lru" in d] +
                    [v for w, r in wr for v in model_of(w, r).values()])
            a = Ax(f, (170, f.legend_h() + 30, 1560, 780), cats=cats, yr=(0, max(vals) * 1.12), ylabel="BMR [%]")
            a.bars(g, d=1, sign=False)
            for i, (w, r) in enumerate(wr):
                if f"{w}|lru" in d:
                    y = a.Y(100 * d[f"{w}|lru"]["bmr"]); x = a.x0 + a.bw * i
                    f.e.append(f'<line x1="{x+a.bw*0.08:.1f}" x2="{x+a.bw*0.92:.1f}" y1="{y:.1f}" y2="{y:.1f}" stroke="#222" stroke-width="4"/>')
                for gi, nm in ((0, "rlru[0]"), (1, "rlru[1]")):
                    v = model_of(w, r).get(nm)
                    if v is not None: a.dot(a.x0 + a.bw * i + a.bw * 0.1 + a.bw * 0.2 * (gi + 0.5), v, "#222", 9, hollow=True)
            rows_ = [(w, r, 100 * d[f"{w}|{r}|none"]["bmr"], 100 * d[f"{w}|{r}|split"]["bmr"], 100 * d[f"{w}|{r}|none+re"]["bmr"]) for w, r in wr if all(f"{w}|{r}|{q}" in d for q in ("none", "split", "none+re"))]
            add("scaled_bmr", "240 GiB 想定（1/64 縮小）: 分類と再挿入で BMR はどう変わるか", f,
                " ".join(f"{w}・R={r}: 分類なし {a_:.2f}% → サイズ分類 {b_:.2f}%、再挿入 {c_:.2f}%。" for w, r, a_, b_, c_ in rows_) +
                (" 理論 − 実測: " + "、".join(f"{w}・R={r} 分類なし {model_of(w, r)['rlru[0]'] - a_:+.1f} pt / サイズ分類 {model_of(w, r)['rlru[1]'] - b_:+.1f} pt"
                                            for w, r, a_, b_, _ in rows_ if len(model_of(w, r)) == 2) +
                 "（差が数 pt を超える組は、モデルの前提が実測の構成と合っていない可能性があるので、数値をそのまま使わない）。" if any(len(model_of(w, r)) == 2 for w, r, *_ in rows_) else ""),
                "棒 = BMR（低いほど良い）。黒の横線 = 同じ容量の物単位 LRU（Region の損がない場合）。白丸 = モデルの予測（分類なし・サイズ分類。立ち上がりの隔離込み）。",
                "SSD 240 GiB、物 64 KiB と 32 MiB、Region 256/626 MiB を、バイト量だけ 1/64 にした負荷（物の数・Region の数は同じなので結果は原寸と一致）。")
            # S5. 時間変化（温め後）
            for w, r in wr:
                ser = [(q, d[f"{w}|{r}|{q}"].get("series", [])) for q in pols if f"{w}|{r}|{q}" in d]
                cum = lambda s_: [sum(x["reqs"] for x in s_[:i + 1]) / 1e6 for i in range(len(s_))]   # 区間ごとの要求数を積み上げる
                ser = [(q, cum(s_), [100 * x["miss_bytes"] / x["req_bytes"] for x in s_]) for q, s_ in ser if len(s_) > 1 and all(x["req_bytes"] > 0 for x in s_)]
                if not ser: continue
                f = Fig(); f.add_legend([(PN[q], PC[q], "line") for q, _, _ in ser])
                xs_all = [x for _, xs_, _ in ser for x in xs_]; ys_all = [y for _, _, ys in ser for y in ys]
                a = Ax(f, (170, f.legend_h() + 30, 1520, 790), xr=(0, max(xs_all)), yr=span(ys_all, 0.1, zero=False), ylabel="区間ごとの BMR [%]", xlabel="流した要求数 [百万]（先頭 25% は温め）")
                a.vline(max(xs_all) / 4, "#999", "温め終わり")
                for q, xs_, ys in ser: a.line(xs_, ys, PC[q], 3.5, marker=len(xs_) <= 40)
                add(f"scaled_series_{w}_{r}", f"240 GiB 想定 {w}・R={r}: BMR の時間変化（定常に入っているか）", f,
                    "最後の区間の BMR: " + "、".join(f"{PN[q]} {ys[-1]:.2f}%" for q, _, ys in ser) + "。",
                    "横 = 流した要求数、縦 = その区間だけの BMR。線が平らになっていれば定常（温め不足でない）。再挿入ありの線が下がり続けるなら、まだ定常に達していない。", "")
    p = f"{work}/synth/writes.json"
    if os.path.exists(p) and os.path.getsize(p) > 2:
        d = json.load(open(p))
        if isinstance(d, list) and d:
            for w in sorted({x["w"] for x in d}):
                xs = [x for x in d if x["w"] == w]
                rs = sorted({x["r"] for x in xs}, key=lambda r: int(re.sub(r"\D", "", r) or 0))
                f = Fig(); f.add_legend([("受け入れ（ミスした物）", "#3b7dbf", "box"), ("再挿入", "#c9683a", "box")])
                ymax = max(x["write"] / x["req_bytes"] for x in xs) * 1.15; gap = 60; pw = (1560 - 170 - gap * (len(rs) - 1)) / len(rs)
                for ri, r in enumerate(rs):
                    xr_ = sorted((x for x in xs if x["r"] == r), key=lambda x: (x["kind"] != "none", x["ff"], x["re"]))
                    cats = [("なし" if x["kind"] == "none" else f"分類{x['ff']:g}") + ("＋再" if x["re"] else "") for x in xr_]
                    a = Ax(f, (170 + ri * (pw + gap), f.legend_h() + 60, 170 + ri * (pw + gap) + pw, 720), cats=cats, yr=(0, ymax), rot=-40,
                           title=f"Region {r}（原寸）", ylabel="書き込み ÷ 要求バイト" if ri == 0 else "", ylab=ri == 0)
                    tops = a.stack([("a", "#3b7dbf", [(x["write"] - x["reinsert"]) / x["req_bytes"] for x in xr_]),
                                    ("r", "#c9683a", [x["reinsert"] / x["req_bytes"] for x in xr_])], labels=False)
                    for i, x in enumerate(xr_): f.t(a.x0 + a.bw * (i + 0.5), a.Y(tops[i]) - 10, f"{100*x['bmr']:.1f}", 17, "middle", INK, "bold")
                f.t(f.W / 2 + 60, f.H - 18, "棒の上の数字 = BMR [%]。なし = 分類なし、分類0.75 = サイズ分類（大物の容量の下限 75%）、＋再 = 再挿入あり", 22, "middle", MUT)
                b0 = next((x for x in xs if x["kind"] == "none" and not x["re"]), None); b1 = next((x for x in xs if x["kind"] == "none" and x["re"] and b0 and x["r"] == b0["r"]), None)
                add(f"writes_synth_{w}", f"240 GiB 想定 {w}: SSD 書き込みの内訳（受け入れと再挿入）", f,
                    (f"Region {b0['r']}・分類なしで再挿入を足すと、書き込みは要求バイトの {b0['write']/b0['req_bytes']:.2f} → {b1['write']/b1['req_bytes']:.2f} 倍、"
                     f"BMR は {100*b0['bmr']:.2f}% → {100*b1['bmr']:.2f}%。" if b0 and b1 else ""),
                    "棒 = SSD に書いたバイト ÷ 要求バイト。青 = ミスした物の受け入れ（= BMR × 要求バイト）、橙 = 再挿入。上の数字 = BMR。同じ BMR なら、差は再挿入の量。"
                    "「分類 ff」の ff = 大きい物の分類に残す容量の下限の割合。",
                    "書き込み = 受け入れ + 再挿入。")


# ---------------------------------------------------------------- 出力の補助
def to_png(out, slugs):
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
            else:
                so = shutil.which("soffice") or shutil.which("libreoffice")
                if so: subprocess.run([so, "--headless", "--convert-to", "png", "--outdir", out] + svgs, capture_output=True)
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
