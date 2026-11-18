"""Каталог событий Polymarket для вкладки «События» (этап A модуля «События»).

Что здесь:
  * список событий по темам, поиск и сортировка — из Gamma `/events` и `/public-search`, кэш в памяти;
  * разбор события: исходы (у многоисходного события — рынки с «Да/Нет», у одного рынка — его исходы),
    цены, объёмы, изменение за сутки, правила;
  * история цен исходов за период (1Ч … Всё) — CLOB `/prices-history`, кэш;
  * живые цены исходов — поток стаканов (если работает процесс realtime) или быстрый запрос стаканов.

Подробные данные события (рынки и исходы) сохраняются в базу, только когда событие открыли: так работают
виртуальные покупки и «Мои ставки». Весь каталог в базу не пишется.
"""

from __future__ import annotations

import time
from typing import Any

from ..ingest.polymarket import _json_list, _ts, normalize_category, parse_market
from ..util import pmap

# тема приложения → раздел Polymarket (метка tag_slug)
TOPIC_TAG = {
    "politics": "politics",
    "sports": "sports",
    "esports": "esports",
    "crypto": "crypto",
    "finance": "finance",
    "culture": "pop-culture",
    "mentions": "mention-markets",
    "weather": "weather",
    "economics": "economy",
    "tech": "tech",
}
# сортировка списка → (поле Gamma, по возрастанию)
SORTS = {
    "volume24": ("volume24hr", False),
    "volume": ("volume", False),
    "liquidity": ("liquidity", False),
    "ending": ("endDate", True),
    "new": ("startDate", False),
}
# период графика → (interval CLOB, точка раз в N минут)
PERIODS = {
    "1h": ("1h", 1),
    "6h": ("6h", 2),
    "1d": ("1d", 5),
    "1w": ("1w", 30),
    "1m": ("1m", 180),
    "all": ("max", 720),
}
PAGE = 24
MAX_LINES = 6  # линий на графике по умолчанию (крупнейшие исходы); остальные — по выбору


class _TTL:
    def __init__(self) -> None:
        self.d: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: float) -> Any:
        hit = self.d.get(key)
        return hit[1] if hit and time.time() - hit[0] < ttl else None

    def put(self, key: Any, value: Any) -> Any:
        if len(self.d) > 800:
            self.d.clear()
        self.d[key] = (time.time(), value)
        return value


def _f(x: Any) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def topic_of(raw: dict) -> str:
    """Тема события по его меткам — тем же правилом, что у рынков китов (с приоритетом тем)."""
    return normalize_category({"tags": raw.get("tags") or []})


def _outcome_rows(raw: dict) -> list[dict]:
    """Исходы события. Многоисходное (несколько рынков) — по рынку на строку, у каждой «Да» и «Нет».
    Один рынок — по строке на исход (Да/Нет или, например, команды)."""
    # заготовки «Кандидат T» в многоисходных событиях неактивны и без торгов — не показываем
    markets = [
        m
        for m in raw.get("markets") or []
        if isinstance(m, dict)
        and m.get("clobTokenIds")
        and (m.get("active") is not False or m.get("closed"))
        and not m.get("archived")
    ]
    rows: list[dict] = []
    if len(markets) == 1:
        m = markets[0]
        toks = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        labels = [str(o) for o in _json_list(m.get("outcomes"))]
        prices = [_f(p) for p in _json_list(m.get("outcomePrices"))]
        yes_no = [lb.lower() for lb in labels] == ["yes", "no"]
        for i, tok in enumerate(toks):
            if yes_no and i == 1:
                continue  # у рынка «Да/Нет» одна строка: «Да», а «Нет» — вторая кнопка
            other = toks[1 - i] if len(toks) == 2 else None
            p = prices[i] if i < len(prices) else None
            rows.append(
                {
                    "key": tok,
                    "market_id": m.get("conditionId"),
                    "title": ("Да" if yes_no else labels[i] if i < len(labels) else f"Исход {i + 1}"),
                    "image": m.get("image") or m.get("icon"),
                    "yes_token": tok,
                    "no_token": other,
                    "yes_label": "Да" if yes_no else (labels[i] if i < len(labels) else ""),
                    "no_label": "Нет" if yes_no else (labels[1 - i] if len(labels) == 2 else ""),
                    "price": p,
                    "best_bid": _f(m.get("bestBid")) if i == 0 else None,
                    "best_ask": _f(m.get("bestAsk")) if i == 0 else None,
                    "change_1d": _f(m.get("oneDayPriceChange")) if i == 0 else None,
                    "volume": _f(m.get("volumeNum") or m.get("volume")),
                    "closed": bool(m.get("closed")),
                    "single": True,
                }
            )
        return rows
    for m in markets:
        toks = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        prices = [_f(p) for p in _json_list(m.get("outcomePrices"))]
        if len(toks) < 2:
            continue
        rows.append(
            {
                "key": toks[0],
                "market_id": m.get("conditionId"),
                "title": m.get("groupItemTitle") or m.get("question") or "",
                "question": m.get("question"),
                "image": m.get("image") or m.get("icon"),
                "yes_token": toks[0],
                "no_token": toks[1],
                "yes_label": "Да",
                "no_label": "Нет",
                "price": prices[0] if prices else None,
                "best_bid": _f(m.get("bestBid")),
                "best_ask": _f(m.get("bestAsk")),
                "change_1d": _f(m.get("oneDayPriceChange")),
                "volume": _f(m.get("volumeNum") or m.get("volume")),
                "closed": bool(m.get("closed")),
                "single": False,
            }
        )
    # открытые — по убыванию цены (как на Polymarket), закрытые — в конце
    rows.sort(key=lambda r: (r["closed"], -(r["price"] or 0)))
    return rows


def summary(raw: dict) -> dict:
    """Карточка события для списка."""
    rows = _outcome_rows(raw)
    open_rows = [r for r in rows if not r["closed"]]
    return {
        "id": str(raw.get("id")),
        "slug": raw.get("slug"),
        "title": raw.get("title") or "",
        "image": raw.get("image") or raw.get("icon"),
        "topic": topic_of(raw),
        "end_ts": _ts(raw.get("endDate")),
        "volume": _f(raw.get("volume")) or 0.0,
        "volume24": _f(raw.get("volume24hr")) or 0.0,
        "liquidity": _f(raw.get("liquidity")) or 0.0,
        "outcomes_n": len(open_rows),
        "top": [{"title": r["title"], "price": r["price"], "change_1d": r["change_1d"]} for r in open_rows[:3]],
        "single": bool(rows and rows[0]["single"]),
        "url": f"https://polymarket.com/event/{raw['slug']}" if raw.get("slug") else None,
    }


def detail(raw: dict) -> dict:
    rows = _outcome_rows(raw)
    s = summary(raw)
    open_rows = [r for r in rows if not r["closed"]]
    chart = [r["key"] for r in open_rows[:MAX_LINES]]
    return {
        **s,
        "description": raw.get("description") or "",
        "resolution_source": raw.get("resolutionSource") or "",
        "neg_risk": bool(raw.get("negRisk") or raw.get("enableNegRisk")),
        "tags": [t.get("label") for t in raw.get("tags") or [] if isinstance(t, dict) and t.get("label")][:8],
        "tag_slugs": [str(t.get("slug")) for t in raw.get("tags") or [] if isinstance(t, dict) and t.get("slug")],
        "outcomes": rows,
        "chart_keys": chart,
        "start_ts": _ts(raw.get("startDate")),
        "closed": bool(raw.get("closed")),
    }


def _thin(pts: list[tuple[int, float]], n: int) -> list[tuple[int, float]]:
    """Не больше n точек на линию (равномерно), последняя точка сохраняется."""
    if len(pts) <= n:
        return pts
    step = len(pts) / n
    out = [pts[int(i * step)] for i in range(n)]
    if out[-1] != pts[-1]:
        out.append(pts[-1])
    return out


class EventsCatalog:
    LIST_TTL = 45.0
    EVENT_TTL = 10.0
    QUOTES_TTL = 2.0

    def __init__(self, gamma: Any, clob: Any = None, db: Any = None):
        self.gamma, self.clob, self.db = gamma, clob, db
        self.cache = _TTL()

    # ------------------------------------------------------------ список
    def browse(self, topic: str = "", sort: str = "volume24", q: str = "", offset: int = 0) -> dict:
        topic = topic if topic in TOPIC_TAG else ""
        sort = sort if sort in SORTS else "volume24"
        q = (q or "").strip()[:80]
        offset = max(0, int(offset))
        key = ("list", topic, sort, q.lower(), offset)
        hit = self.cache.get(key, self.LIST_TTL)
        if hit is not None:
            return hit
        if q:
            raw = self.gamma.search_events(q, limit=PAGE)
            raw = [e for e in raw if not e.get("closed")]
            if topic:
                raw = [e for e in raw if topic_of(e) == topic]
            has_more = False
        else:
            order, asc = SORTS[sort]
            params: dict[str, Any] = {
                "limit": PAGE + 1,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": order,
                "ascending": str(asc).lower(),
            }
            if topic:
                params["tag_slug"] = TOPIC_TAG[topic]
            if sort == "ending":
                params["end_date_min"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            raw = self.gamma.events(**params)
            has_more = len(raw) > PAGE
            raw = raw[:PAGE]
        items = [summary(e) for e in raw]
        for it in items:  # тема из меток бывает «Разное» — если искали по разделу, это его тема
            if topic and it["topic"] == "other":
                it["topic"] = topic
        return self.cache.put(key, {"ok": True, "items": items, "has_more": has_more, "offset": offset})

    # ------------------------------------------------------------ событие
    def raw_event(self, event_id: str, fresh: bool = False) -> dict | None:
        key = ("event", str(event_id))
        if not fresh:
            hit = self.cache.get(key, self.EVENT_TTL)
            if hit is not None:
                return hit
        raw = self.gamma.event(str(event_id))
        if raw is None:
            return None
        return self.cache.put(key, raw)

    def get(self, event_id: str) -> dict | None:
        raw = self.raw_event(event_id)
        if raw is None:
            return None
        if self.db is not None:
            self.store(raw)
        return detail(raw)

    def store(self, raw: dict) -> int:
        """Рынки и исходы открытого события — в базу (для виртуальных покупок и «Моих ставок»)."""
        ev_stub = {k: raw.get(k) for k in ("id", "slug", "title", "startDate", "endDate")} | {
            "tags": raw.get("tags") or []
        }
        n = 0
        for m in raw.get("markets") or []:
            if not isinstance(m, dict) or not m.get("conditionId"):
                continue
            mrow, toks, ev = parse_market({**m, "events": [ev_stub]})
            if mrow["category"] == "other":
                mrow["category"] = topic_of(raw)
            self.db.upsert("markets", mrow, ["market_id"])
            if toks:
                self.db.upsert_many("tokens", toks, ["token_id"])
            if ev:
                self.db.upsert("events", {**ev, "category": mrow["category"]}, ["event_id"])
            n += 1
        return n

    # ------------------------------------------------------------ история цен
    def history(self, event_id: str, period: str = "1w", keys: list[str] | None = None) -> dict:
        period = period if period in PERIODS else "1w"
        ev = self.get(event_id)
        if ev is None:
            return {"ok": False, "error": "Событие не найдено"}
        rows = {r["key"]: r for r in ev["outcomes"]}
        want = [k for k in (keys or ev["chart_keys"]) if k in rows][:12]
        interval, fid = PERIODS[period]
        ttl = 20.0 if period in ("1h", "6h") else 120.0

        def load(tok: str) -> list[tuple[int, float]]:
            ck = ("hist", tok, period)
            hit = self.cache.get(ck, ttl)
            if hit is not None:
                return hit
            pts = self.clob.prices_interval(tok, interval, fid)
            if period == "all" and len(pts) < 120:  # молодое событие: при редких точках график ступенчатый — подробнее
                pts = self.clob.prices_interval(tok, interval, 60) or pts
            return self.cache.put(ck, _thin(pts, 800))

        lines = []
        for tok, pts, exc in pmap(load, want, workers=6):
            r = rows[tok]
            lines.append(
                {
                    "key": tok,
                    "title": r["title"],
                    "points": [] if exc else [{"ts": int(t), "p": round(float(p), 4)} for t, p in (pts or [])],
                    "error": str(exc)[:120] if exc else None,
                }
            )
        return {"ok": True, "period": period, "lines": lines}

    # ------------------------------------------------------------ живые цены
    def quotes(self, tokens: list[str], live: dict[str, dict] | None = None) -> dict[str, dict]:
        """Лучшие цены по исходам: из потока (live, если свежие), иначе стакан CLOB (кэш 2 с)."""
        out: dict[str, dict] = {}
        need = []
        for t in tokens:
            q = (live or {}).get(t)
            if q and (q.get("bid") is not None or q.get("ask") is not None):
                out[t] = {**q, "live": True}
            else:
                hit = self.cache.get(("q", t), self.QUOTES_TTL)
                if hit is not None:
                    out[t] = hit
                else:
                    need.append(t)
        if need and self.clob is not None:
            for tok, book, exc in pmap(self.clob.book, need[:24], workers=8):
                if exc or book is None:
                    continue
                bids, asks = book
                out[tok] = self.cache.put(
                    ("q", tok),
                    {
                        "bid": bids[0][0] if bids else None,
                        "ask": asks[0][0] if asks else None,
                        "bids": bids[:8],
                        "asks": asks[:8],
                        "live": False,
                        "ts": time.time(),
                    },
                )
        return out
