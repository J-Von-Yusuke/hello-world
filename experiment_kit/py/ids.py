"""ids.py — paper 系の図のスクリプトが役割で使う方式の名前（11-20）。名前の付け方は config.sh の「方式の名前の付け方」：
R-閾値-配分-再挿入[-adm][~変種]。方式を足しても、ここは役割（基準・CacheLib の n の線・λ の線・2×2・頑健性の比べる相手）を
変えるときだけ直す。色は方式ごとに 1 色（無ければ並んだ順のパレット）。"""
BASE = "r256-none-nat-n1"           # 差の基準 = いまの CacheLib（R=256 MiB・分類なし・n=1）
PROPOSAL = "ru-otsu-ghost-cb"       # 現在の提案（閾値 = 大津・再利用の強さ、配分 = 名簿、書き直し = 損得）
PREVIOUS = "ru-st-climb-nauto"      # 以前の提案（st ＋ 下限の山登り ＋ n=auto）
RU_ONLY = "ru-none-nat-n1"          # Region を RU にしただけ
VAR = "~"                           # 頑健性の条件の印（ru-st-climb-nauto~c0 → 条件 c0）

# 交換関係の図：CacheLib の書き直しの閾値の線（n=0〜3）と、現在の提案の書き込みの値段 λ の線
CURVE = [("r256-none-nat-n0", "n=0"), (BASE, "n=1"), ("r256-none-nat-n2", "n=2"), ("r256-none-nat-n3", "n=3")]
LCURVE = [(PROPOSAL, "λ=0"), (PROPOSAL + ".l01", "λ=0.1"), (PROPOSAL + ".l03", "λ=0.3"), (PROPOSAL + ".l1", "λ=1")]
PT_BIG = {PROPOSAL: 12, "ru-otsu-ghost-n1": 12, "r256-otsu-ghost-n1": 12, PREVIOUS: 11, "r256-st-climb-n1": 11, "ru-st-climb-n1": 10,
          "ru-st-climb-n1-adm": 11}                                                    # 点の強調の大きさ（無ければ 9）
PT_HOLLOW = {"ru-st-ff.90-n3", "ru-none-nat-nauto", "r256-none-nat-nauto", "ru-none-nat-nauto-adm", "ru-none-nat-cb", "ru-none-nat-cb-adm"}   # 切り分け用は白抜き

# 受け入れ × 置き場所の 2×2（篩なし・置き場所なし、篩なし・あり、篩あり・置き場所なし、篩あり・あり）
ORTH = [("現在の提案", "current", ("ru-none-nat-cb", PROPOSAL, "ru-none-nat-cb-adm", PROPOSAL + "-adm")),
        ("以前の提案", "previous", ("ru-none-nat-nauto", PREVIOUS, "ru-none-nat-nauto-adm", PREVIOUS + "-adm"))]

# 頑健性の条件をまたいだ図：(提案, 図の名前の印, 日本語, 英語)。比べる相手は CacheLib n=1 / n=3
ROBUST_PROPS = [(PROPOSAL, "_v2", "現在の提案", "proposal"), (PREVIOUS, "", "以前の提案", "previous proposal"),
                ("ru-st-climb.w0-nauto", "_w0", "以前の提案・BHR のみ", "previous, BHR only"), (RU_ONLY, "_rubase", "R=RU のみ", "R=RU only")]

COL = {BASE: "#4d4d4d", "r256-none-nat-n0": "#d0d0d0", "r256-none-nat-n2": "#a0a0a0", "r256-none-nat-n3": "#7f7f7f", RU_ONLY: "#a6a6a6",
       PROPOSAL: "#03045e", PREVIOUS: "#9c755f", "ru-otsu-ghost-n1": "#d62828", "r256-otsu-ghost-n1": "#f77f00", "ru-otsu-ghost-n1-adm": "#2a9d8f",
       "ru-otsu-ghost-cb.first": "#7209b7", "ru-none-nat-nauto": "#9fb6cc", "r256-none-nat-nauto": "#dcc3ae", "ru-st-ff.90-n3": "#3b7dbf",
       "r256-st-ff.90-n3": "#e3a37c", "r256-st-climb-nauto": "#c9683a", "ru-st-climb.w0-nauto": "#5b3a8c", "r256-st-climb.w0-nauto": "#8c3a5b",
       "ru-st-climb-nauto.ii": "#2a9d8f", "ru-dram.k2-climb-nauto": "#6a994e", "ru-dram.t1-climb-nauto": "#a7c957", "ru-st-nat-nauto": "#e9c46a",
       "ru-st-nat-n0": "#f4a261", "ru-st-climb.gate-nauto": "#264653", "r256-none-nat-n1-adm": "#b5838d", "ru-none-nat-nauto-adm": "#9d8189",
       "ru-st-climb-nauto-adm": "#6d597a", "ru-dram.t2-climb-nauto-adm": "#b56576", "r256-st-climb-n1": "#e76f51", "ru-st-climb-n1": "#0b6e4f",
       "r256-st-climb-n1-adm": "#f4845f", "ru-st-climb-n1-adm": "#40916c", "r256-none-nat-cb.first": "#6c757d", "r256-st-climb-cb.first": "#9c6644",
       "ru-st-climb-cb.first": "#3a86ff", "ru-st-climb-cb.first-adm": "#8338ec"}
