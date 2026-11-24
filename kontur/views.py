"""Which chart an event page shows, as polymarket.com decides it (research of 24.11.2026, 42 event pages).

    round   a crypto "Up or Down" round we have a live coin price for: the coin price against the round's target
    asset   "What price will Bitcoin hit", "Bitcoin above ___": the coin price over the event (no probability lines)
    match   a game with teams: the main market's outcomes in the teams' colours, a draw in grey
    single  one market: one line (the first outcome) and "N% chance" in large type
    multi   mutually exclusive outcomes (negRisk): the most likely outcomes, each in its own colour
    none    independent questions (mentions, "by date" ladders, thresholds): the list only, no chart

The rules are checked in this order; the first that fits wins. `view(event)` takes the event as normalised by
polymarket.event (full=True) and needs nothing else, so it is cheap and testable.
"""

import re

ROUND = re.compile(r"^([a-z]+)-updown-(5m|15m|1h|4h|1d)-(\d{9,11})$")
ROUND_SYMBOLS = {"btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec"}   # coins with a live Chainlink price in RTDS
ASSET_TAGS = {"hit-price", "multi-strikes"}
COINS = {"bitcoin": "btc", "ethereum": "eth", "solana": "sol", "xrp": "xrp", "dogecoin": "doge", "bnb": "bnb",
         "hyperliquid": "hype", "zcash": "zec"}

# Polymarket's own line colours (dark theme) for outcomes without a colour of their own, then more of ours.
PALETTE = ["#87BFFF", "#2797FF", "#FDC503", "#FF7F0E", "#C13B83", "#0BA4AE", "#8B5CF6", "#EF4444",
           "#22D3EE", "#A3E635", "#F472B6", "#34CA5B"]
SINGLE = "#2797FF"
DRAW = "#9AA4B2"
TEAM_FALLBACK = ["#2797FF", "#FF7F0E", "#FDC503"]
MULTI_LINES = 12          # lines sent for the legend; the page shows the first four
BACKGROUND = (11, 16, 21)  # the app's page colour, #0b1015


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def _luminance(rgb):
    def lin(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(rgb, other, share):
    return tuple(a + (b - a) * share for a, b in zip(rgb, other))


def readable(hex_color, background=BACKGROUND, ratio=3.0):
    """The colour, lightened towards white until a line in it stands out on the dark page (contrast >= 3:1).

    Team colours are often dark (navy, forest green) and vanish on a dark background."""
    rgb = _rgb(hex_color)
    bg = _luminance(background)
    for step in range(11):
        c = _mix(rgb, (255, 255, 255), step / 10)
        if (_luminance(c) + 0.05) / (bg + 0.05) >= ratio:
            return _hex(c)
    return "#FFFFFF"


def _close(a, b):
    return sum(abs(x - y) for x, y in zip(_rgb(a), _rgb(b))) < 60


def _line(contract, index, name, color):
    o = contract["outcomes"][index]
    return {"token": o.get("token"), "name": name, "color": color, "condition": contract["condition"],
            "index": index, "price": o.get("price")}


def _named(contract):
    """Two outcomes named after the sides (teams, players), not Yes/No."""
    names = [o["name"].strip().lower() for o in contract["outcomes"]]
    return len(names) == 2 and all(o.get("token") for o in contract["outcomes"]) and not set(names) & {"yes", "no"}


def _team_of(text, teams):
    t = (text or "").strip().lower()
    for i, team in enumerate(teams):
        names = {team["name"].strip().lower(), team["abbr"].strip().lower()} - {""}
        if t in names or any(n and (t.startswith(n) or n in t.split()) for n in names):
            return i
    return None


def _team_colors(teams):
    colors = []
    for i, team in enumerate(teams):
        c = readable(team["color"]) if team.get("color") else TEAM_FALLBACK[i % len(TEAM_FALLBACK)]
        # two sides in the same colour (both flags red): the second one lighter, so the lines can be told apart
        if colors and _close(c, colors[0]):
            c = _hex(_mix(_rgb(c), (255, 255, 255), 0.45))
        colors.append(c)
    return colors


def _match_lines(e):
    teams = e.get("teams") or []
    colors = _team_colors(teams)
    contracts = e.get("contracts") or []
    moneyline = [c for c in contracts if c.get("type") == "moneyline" and c["outcomes"]]
    two_way = [c for c in moneyline if _named(c)]
    main = two_way[0] if two_way else None
    if main is None and not moneyline:
        main = next((c for c in contracts if _named(c) and not c["closed"]), None)
    if main is not None:
        lines = []
        for i, o in enumerate(main["outcomes"]):
            k = _team_of(o["name"], teams)
            k = i if k is None else k
            lines.append(_line(main, i, o["name"], colors[k] if k < len(colors) else TEAM_FALLBACK[i % 3]))
        return lines, [main["condition"]]
    # three-way (football): one Yes/No market per team and one for the draw
    picks = [c for c in moneyline if len(c["outcomes"]) == 2 and c["outcomes"][0].get("token")][:3]
    if not picks:
        return [], []
    lines, draw = [], None
    for c in picks:
        label = c.get("label") or c.get("question") or ""
        if re.match(r"\s*(draw|tie|ничья)\b", label, re.I):
            draw = _line(c, 0, "Ничья", DRAW)
            continue
        k = _team_of(label, teams)
        lines.append((k if k is not None else 9, _line(c, 0, label, colors[k] if k is not None and k < len(colors)
                                                      else TEAM_FALLBACK[len(lines) % 3])))
    lines = [ln for _, ln in sorted(lines, key=lambda x: x[0])]
    if draw:
        lines.insert(1 if lines else 0, draw)
    return lines, [ln["condition"] for ln in lines]


def coin(tags):
    for t in tags:
        if t in COINS:
            return COINS[t]
    return ""


def view(e):
    """{kind, lines: [{token, name, color, condition, index, price}], main: [conditions listed first], symbol}."""
    tags = {t.get("slug") for t in e.get("tags") or []}
    contracts = e.get("contracts") or []
    m = ROUND.match(e.get("slug") or "")
    if m and m.group(1) in ROUND_SYMBOLS:
        return {"kind": "round", "symbol": m.group(1), "lines": [], "main": []}
    if tags & ASSET_TAGS:
        return {"kind": "asset", "symbol": coin(tags), "lines": [], "main": []}
    if e.get("teams"):
        lines, main = _match_lines(e)
        if lines:
            return {"kind": "match", "lines": lines, "main": main}
    if len(contracts) == 1 and contracts[0]["outcomes"] and contracts[0]["outcomes"][0].get("token"):
        c = contracts[0]
        return {"kind": "single", "lines": [_line(c, 0, c["outcomes"][0]["name"], SINGLE)], "main": [c["condition"]]}
    binary = [c for c in contracts if len(c["outcomes"]) == 2 and c["outcomes"][0].get("token")]
    if e.get("neg_risk") and binary:
        pool = [c for c in binary if not c["closed"]] or binary
        pool = sorted(pool, key=lambda c: -(c["outcomes"][0].get("price") or 0))[:MULTI_LINES]
        used, lines = set(), []
        for i, c in enumerate(pool):
            color = readable(c["color"]) if c.get("color") else None
            if not color or color in used:
                color = next((p for p in PALETTE if p not in used), PALETTE[i % len(PALETTE)])
            used.add(color)
            lines.append(_line(c, 0, c.get("label") or c.get("question") or "", color))
        return {"kind": "multi", "lines": lines, "main": []}
    return {"kind": "none", "lines": [], "main": []}
