"""Understanding a search phrase: direct jumps (addresses, links), what is searched, filters and words.

parse("киты купили да франция за сутки") ->
  kind "trades", filters {whale, side BUY, outcome Yes, period 24 h}, words ["франция" → france],
  and chips the interface shows and lets the user remove. No network and no AI: rules and the lexicon.
"""

import re
import time
from datetime import datetime, timedelta, timezone

from . import lexicon

DAY = 86400
ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
URL = re.compile(r"(?:https?://)?(?:www\.)?(polymarket\.com|polymarketanalytics\.com)/(\S+)", re.I)
NUM = r"\$?\s?(\d+(?:[.,]\d+)?)\s*(тыс\.?|млн|млрд|k|к|m|м|b|%|¢)?(?![\w])"
OPS = {
    ">=": ">=", "≥": ">=", ">": ">=", "больше": ">=", "выше": ">=", "от": ">=", "более": ">=", "свыше": ">=",
    "не менее": ">=", "минимум": ">=", "дороже": ">=", "<=": "<=", "≤": "<=", "<": "<=", "меньше": "<=",
    "ниже": "<=", "до": "<=", "менее": "<=", "не более": "<=", "максимум": "<=", "дешевле": "<=", "=": ">=",
}
OP_RE = "|".join(sorted((re.escape(k) for k in OPS), key=len, reverse=True))
METRICS = [
    (r"винрейт\w*|win\s?rate|wr|выигрыш\w*|побед\w*", "wr"),
    (r"pnl|пнл|прибыл\w*|доход\w*|заработ\w*", "pnl"),
    (r"объ[её]м\w*|оборот\w*", "volume"),
    (r"ликвидн\w*", "liquidity"),
    (r"шанс\w*|вероятност\w*|цен\w*|стоимостью", "price"),
    (r"капитал\w*|стоимост\w*|портфел\w*", "networth"),
    (r"сумм\w*|размер\w*", "amount"),
]
METRIC_RE = "|".join(f"(?P<m{i}>{rx})" for i, (rx, _) in enumerate(METRICS))
LABELS = {"wr": "Выигрыши", "pnl": "PnL", "volume": "Объём", "liquidity": "Ликвидность", "price": "Цена",
          "networth": "Стоимость позиций", "amount": "Сумма сделки"}
KIND_LABELS = {"events": "Рынки", "traders": "Трейдеры", "trades": "Сделки", "tags": "Теги", "mine": "Моё"}
MINE = {"позиц": "positions", "ставк": "positions", "заявк": "orders", "ордер": "orders", "избранн": "watchlist",
        "копи": "copies", "автопилот": "autopilot", "игрок": "autopilot"}
MINE_LABELS = {"positions": "Мои позиции", "orders": "Мои заявки", "watchlist": "Избранное", "copies": "Мои копии",
               "autopilot": "Автопилот"}


def _value(raw, unit):
    v = float(raw.replace(",", "."))
    unit = (unit or "").lower().rstrip(".")
    mult = {"k": 1e3, "к": 1e3, "тыс": 1e3, "m": 1e6, "м": 1e6, "млн": 1e6, "b": 1e9, "млрд": 1e9}.get(unit, 1)
    return v * mult, unit


def _money(v):
    if v >= 1e6:
        return f"${v / 1e6:g} млн"
    if v >= 1e3:
        return f"${v / 1e3:g} тыс."
    return f"${v:g}"


def _end_of(days, now):
    d = datetime.fromtimestamp(now, timezone.utc) + timedelta(days=days)
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()


class _Text:
    """Lower-cased phrase with the spans already taken by filters."""

    def __init__(self, raw):
        self.raw = raw
        self.low = raw.lower().replace("ё", "е")
        self.taken = []

    def free(self, a, b):
        return all(b <= x or a >= y for x, y in self.taken)

    def take(self, a, b):
        self.taken.append((a, b))

    def find(self, pattern):
        for m in re.finditer(pattern, self.low):
            if self.free(m.start(), m.end()):
                yield m


def parse(raw, now=None):
    now = now or time.time()
    raw = (raw or "").strip()[:200]
    q = {"raw": raw, "kind": None, "go": None, "filters": {}, "chips": [], "words": [], "notes": [], "sort": None}
    if not raw:
        return q
    t = _Text(raw)
    f = q["filters"]

    def chip(key, label, span):
        q["chips"].append({"key": key, "label": label, "span": list(span)})
        t.take(*span)

    # direct jumps
    m = URL.search(raw)
    if m:
        path = m.group(2).split("?")[0].strip("/")
        parts = path.split("/")
        if parts[0] == "event" and len(parts) > 1:
            q["go"] = "/event/" + parts[1]
        elif parts[0] in ("profile", "traders") and len(parts) > 1 and ADDRESS.fullmatch(parts[1]):
            q["go"] = "/traders/" + parts[1].lower()
        elif parts[0].startswith("@"):
            raw = parts[0][1:]
            t = _Text(raw)
            q["kind"] = "traders"
        if q["go"]:
            return q
    m = ADDRESS.search(raw)
    if m:
        q["go"] = "/traders/" + m.group(0).lower()
        q["kind"] = "traders"
        return q

    # leading symbol: @ traders, # tags, $ markets
    lead = t.low[:1]
    if lead in "@#$" and len(t.low) > 1 and not re.match(r"\$\s?\d", t.low):
        q["kind"] = {"@": "traders", "#": "tags", "$": "events"}[lead]
        t.take(0, 1)
        if lead in "@#":  # a name follows: no filters
            return _finish(q, t)

    # mine: "мои позиции", "моё избранное", "в автопилоте"
    for m in t.find(r"\b(?:мои|мой|мое|моя|мою|моих|в)\s+(позиц\w*|ставк\w*|заявк\w*|ордер\w*|избранн\w*|копи\w*|автопилот\w*|игрок\w* автопилота)"):
        word = m.group(1)
        if word.startswith("ставк") and m.group(0).startswith("в "):
            continue
        sub = next(v for k, v in MINE.items() if word.startswith(k))
        q["kind"] = "mine"
        f["mine"] = sub
        chip("mine", MINE_LABELS[sub], m.span())

    # comparisons: "винрейт > 70%", "объём от 1м", "дешевле 0,3", "от 70% выигрышей"
    pending = []
    for m in t.find(rf"(?:{METRIC_RE})\s*(?P<op>{OP_RE})?\s*{NUM}"):
        metric = next(METRICS[i][1] for i in range(len(METRICS)) if m.group(f"m{i}"))
        pending.append((metric, OPS.get(m.group("op") or ">=", ">="), *_value(m.group(len(METRICS) + 2), m.group(len(METRICS) + 3)), m.span()))
        t.take(*m.span())
    for m in t.find(rf"(?P<op>{OP_RE})\s*{NUM}\s*(?:{METRIC_RE})?"):
        op = OPS[m.group("op")]
        metric = next((METRICS[i][1] for i in range(len(METRICS)) if m.group(f"m{i}")), None)
        if metric is None and m.group("op") in ("дешевле", "дороже"):
            metric = "price"
        if m.group("op") == "до" and not metric and not m.group(3):
            continue  # "до 2027" is a word of the title, not a limit
        pending.append((metric, op, *_value(m.group(2), m.group(3)), m.span()))
        t.take(*m.span())

    # whales, buys and sells, outcome
    for m in t.find(r"\b(?:кит\w*|крупн\w* (?:сделк\w*|ставк\w*|покупк\w*)|whales?)\b"):
        f["whale"] = True
        chip("whale", "Киты: от $10 000", m.span())
    for m in t.find(r"\b(?:купил\w*|покупк\w*|покупа\w*|bought|buys?)\b"):
        f["side"] = "BUY"
        chip("side", "Покупки", m.span())
        break
    for m in t.find(r"\b(?:продал\w*|продаж\w*|прода[ею]т\w*|sold|sells?)\b"):
        f["side"] = "SELL"
        chip("side", "Продажи", m.span())
        break
    if f.get("side"):
        for m in t.find(r"\b(?:на\s+)?(да|yes|нет|no)\b"):
            f["outcome"] = "Yes" if m.group(1) in ("да", "yes") else "No"
            chip("outcome", "Исход: " + ("Да" if f["outcome"] == "Yes" else "Нет"), m.span())
            break

    # time
    when = [
        (r"\bсегодня\b|\btoday\b", 0, "сегодня"), (r"\bзавтра\b|\btomorrow\b", 1, "до завтра"),
        (r"\b(?:за\s+)?(?:сутк\w*|24\s?ч\w*|день|последний день)\b", 1, "сутки"), (r"\bза\s+час\b|\bчас\b", 1 / 24, "час"),
        (r"\b(?:на\s+|за\s+|до\s+конца\s+)?(?:эт\w+\s+)?недел\w*\b|\b7\s?дн\w*", 7, "неделя"),
        (r"\b(?:в\s+|за\s+|до\s+конца\s+)?(?:эт\w+\s+)?месяц\w*\b|\b30\s?дн\w*", 30, "месяц"),
        (r"\bза\s+(\d+)\s+(?:дн\w*|сут\w*)\b", None, "дней"),
    ]
    for rx, days, label in when:
        for m in t.find(rx):
            d = days if days is not None else float(m.group(1))
            f["days"] = d
            f["days_label"] = label if days is not None else f"{int(d)} дн."
            t.take(*m.span())
            q["chips"].append({"key": "days", "label": "", "span": list(m.span())})
            break
        if "days" in f:
            break
    ends = list(t.find(r"\b(?:закрыва\w*|заканчива\w*|истека\w*|разреш\w*|итог\w*|ends?|closing)\b"))
    for m in ends:
        f["ending"] = True
        t.take(*m.span())

    # what is searched
    kinds = [
        (r"\b(?:трейдер\w*|игрок\w*|кошел[её]к\w*|кошельк\w*|профил\w*|юзер\w*|пользовател\w*|traders?|wallets?)\b", "traders"),
        (r"\b(?:рынок|рынк\w*|событи\w*|прогноз\w*|markets?|events?)\b", "events"),
        (r"\b(?:тег\w*|tags?)\b", "tags"),
    ]
    for rx, kind in kinds:
        for m in t.find(rx):
            if q["kind"] in (None, kind):
                q["kind"] = kind
                chip("kind", KIND_LABELS[kind], m.span())
            break
    for m in t.find(r"\bсделк\w*|\btrades?\b"):
        if f.get("side") or f.get("whale") or (f.get("days") and not f.get("ending")) or m.group(0).startswith("trade"):
            q["kind"] = q["kind"] if q["kind"] == "mine" else "trades"
            chip("kind", KIND_LABELS["trades"], m.span())

    # state and sort
    for m in t.find(r"\bактивн\w*\b|\bactive\b"):
        f["active"] = True
        chip("active", "Активные", m.span())
    for m in t.find(r"\bнов(?:ые|ых|ое|ый|ая|ую)\b|\bnew\b"):
        f["new"] = True
        chip("new", "Новые за 7 дней", m.span())
    for m in t.find(r"\b(?:горяч\w*|популярн\w*|трендов\w*|в тренде|hot|trending)\b"):
        q["sort"] = "hot"
        chip("sort", "Сначала популярные", m.span())
    for m in t.find(r"\bпо\s+(объ[её]м\w*|прибыл\w*|pnl|винрейт\w*|выигрыш\w*|ликвидн\w*|дат\w*)"):
        word = m.group(1)
        q["sort"] = "volume" if word.startswith("об") else "pnl" if word.startswith(("при", "pnl")) else \
            "wr" if word.startswith(("вин", "выи")) else "liquidity" if word.startswith("ликв") else "end"
        chip("sort", "Сортировка: " + {"volume": "объём", "pnl": "PnL", "wr": "выигрыши", "liquidity": "ликвидность",
                                         "end": "дата окончания"}[q["sort"]], m.span())

    # categories (single words)
    for m in t.find(r"[a-zа-я][\w-]*"):
        cat = lexicon.category(m.group(0))
        if cat:
            f["category"], label = cat
            chip("category", "Категория: " + label, m.span())
            break

    # infer the kind from the filters when the phrase did not name it
    if q["kind"] is None:
        metrics = {p[0] for p in pending}
        if metrics & {"wr", "pnl", "networth"} or f.get("active"):
            q["kind"] = "traders"
        elif f.get("side") or "amount" in metrics or (f.get("whale") and f.get("days") is not None):
            q["kind"] = "trades"
        elif metrics & {"price", "volume", "liquidity"} or f.get("ending") or f.get("new"):
            q["kind"] = "events"
        elif f.get("days"):
            q["kind"] = "events"
            f["ending"] = True

    # resolve comparisons now that the kind is known
    for metric, op, value, unit, span in pending:
        if metric is None:
            metric = {"traders": "wr" if unit == "%" else "pnl", "trades": "amount",
                      "events": "price" if unit in ("%", "¢") or value <= 1 else "volume"}.get(q["kind"], "price" if value <= 1 else "volume")
        if metric == "wr" and unit != "%" and value <= 1:
            value *= 100
        if metric == "price":
            value = value / 100 if unit in ("%", "¢") or value > 1 else value
        key = f"{metric}_{'min' if op == '>=' else 'max'}"
        f[key] = value
        sign = "≥" if op == ">=" else "≤"
        shown = f"{value:g}%" if metric == "wr" else f"${value:.2f}".replace(".", ",") if metric == "price" else _money(value)
        q["chips"].append({"key": key, "label": f"{LABELS[metric]} {sign} {shown}", "span": list(span)})

    # time filter meaning: market end for markets, period for trades
    if "days" in f:
        label = f.pop("days_label")
        chip_ = next(c for c in q["chips"] if c["key"] == "days")
        if q["kind"] == "trades" or (not f.get("ending") and q["kind"] in (None, "traders")):
            f["period_h"] = f.pop("days") * 24 or 24
            chip_["key"], chip_["label"] = "period_h", "За " + {"сегодня": "сегодня", "до завтра": "2 дня", "сутки": "сутки",
                                                                 "час": "час", "неделя": "неделю", "месяц": "месяц"}.get(label, label)
            if q["kind"] is None:
                q["kind"] = "trades"
        else:
            days = f.pop("days")
            f["end_before"] = _end_of(days, now) if days >= 1 or days == 0 else now + days * DAY
            chip_["key"], chip_["label"] = "end_before", "Окончание: " + label
            q["kind"] = q["kind"] or "events"
    f.pop("ending", None)
    if f.get("whale"):
        f["amount_min"] = max(f.get("amount_min") or 0, 10_000)
        if q["kind"] in (None, "traders") and not f.get("side") and "period_h" not in f:
            q["kind"] = "traders"
            f.pop("amount_min")
            f["networth_min"] = max(f.get("networth_min") or 0, 1_000_000)
            for c in q["chips"]:
                if c["key"] == "whale":
                    c["key"], c["label"] = "networth_min", "Киты: позиции от $1 млн"
        elif q["kind"] in (None, "events"):
            q["kind"] = "trades"

    return _finish(q, t)


def _finish(q, t):
    """Free words: what is left of the phrase, with translations and alternatives."""
    for m in re.finditer(r"[^\s,;:!?«»\"()\[\]@#$]+", t.low):
        if not t.free(m.start(), m.end()):
            continue
        word = m.group(0).strip(".'’`-")
        if not word or word in lexicon.STOP or (len(word) == 1 and not word.isdigit()):
            continue
        q["words"].append(_word(word))
    q["words"] = _phrases(q["words"])
    q["notes"] = [f"{w['word']} → {w['en']}" for w in q["words"] if w.get("en")]
    return q


def _word(word):
    """One free word with its alternatives; the search keeps those the index knows."""
    item = {"word": word, "en": None, "alts": []}
    if lexicon.cyrillic(word):
        en = lexicon.translate(word)
        if en:
            item["en"] = en
        item["alts"].append(("layout", word.translate(lexicon.RU_TO_EN)))
        item["alts"].append(("translit", lexicon.to_latin(word)))
    else:
        ru = word.translate(lexicon.EN_TO_RU)
        en = lexicon.translate(ru) if lexicon.cyrillic(ru) else None
        if en:
            item["alts"].append(("layout-ru", en))
        cyr = lexicon.to_cyrillic(word)
        en2 = lexicon.translate(cyr)
        if en2 and en2 != word:
            item["alts"].append(("translit-ru", en2))
    return item


def _phrases(words):
    """Join neighbours that form a phrase of the lexicon: "лига чемпионов", "ле пен", "нью йорк"."""
    out, i = [], 0
    while i < len(words):
        if i + 1 < len(words) and lexicon.cyrillic(words[i]["word"]):
            first, second = words[i]["word"], words[i + 1]["word"]
            hit = None
            for s in lexicon.stems(second):
                key = f"{first} {s}"
                if key in lexicon.TERMS:
                    hit = lexicon.TERMS[key]
                    break
            if hit:
                out.append({"word": f"{first} {second}", "en": hit, "alts": []})
                i += 2
                continue
        out.append(words[i])
        i += 1
    return out
