"""Источники информации и защита от манипуляций (ТЗ 9.1, 9.3, 9.4).

Встроен RSS-источник (СМИ, агентства, блоги регуляторов). Соцсети и Telegram
подключаются через тот же интерфейс NewsSource (нужны их API-ключи); их вес
низкий, и они служат только триггером проверки.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from typing import Protocol

from urllib.parse import quote_plus

import requests

from ..db import DB, new_id, now_ts

log = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {"primary": 0.9, "forecast": 0.8, "media": 0.7, "social": 0.2, "telegram": 0.2}
PRIMARY_TYPES = {"primary"}


@dataclass
class NewsItem:
    source: str
    source_type: str
    ts_pub: int
    title: str
    url: str

    @property
    def text_hash(self) -> str:
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        return hashlib.sha1(norm.encode()).hexdigest()


class NewsSource(Protocol):
    name: str
    source_type: str

    def search(self, query: str, since_ts: int) -> list[NewsItem]: ...


class RssSource:
    """Лента RSS/Atom. Отбор по ключевым словам вопроса."""

    def __init__(self, name: str, url: str, source_type: str = "media", timeout: float = 15.0):
        self.name, self.url, self.source_type, self.timeout = name, url, source_type, timeout

    def search(self, query: str, since_ts: int) -> list[NewsItem]:
        try:
            r = requests.get(self.url, timeout=self.timeout, headers={"User-Agent": "whalefollow/0.1"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:
            log.warning("RSS %s: %s", self.name, exc)
            return []
        words = [w for w in re.findall(r"\w{4,}", query.lower())][:8]
        out = []
        for it in root.iter():
            if not it.tag.endswith("item") and not it.tag.endswith("entry"):
                continue
            title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
            link = it.findtext("link") or ""
            pub = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
            try:
                ts = int(parsedate_to_datetime(pub).timestamp()) if pub else now_ts()
            except (TypeError, ValueError):
                ts = now_ts()
            if ts < since_ts:
                continue
            hits = sum(w in title.lower() for w in words)
            if words and hits >= max(1, len(words) // 3):
                out.append(NewsItem(self.name, self.source_type, ts, title, link))
        return out


STOP = {"will", "the", "a", "an", "be", "by", "in", "on", "of", "to", "for", "and", "or", "at", "is", "are",
        "before", "after", "end", "this", "that", "with", "than", "more", "less", "2025", "2026", "2027"}


def keywords(question: str, n: int = 7) -> str:
    """Поисковый запрос из вопроса рынка: без служебных слов."""
    words = re.findall(r"[\w$%.'-]+", question or "")
    out = [w for w in words if w.lower().strip(".'") not in STOP]
    return " ".join(out[:n])


def parse_rss(content: bytes, source_name: str, source_type: str, since_ts: int = 0) -> list[NewsItem]:
    """RSS/Atom → новости. В Google News у каждой новости свой издатель в <source>."""
    root = ET.fromstring(content)
    out = []
    for it in root.iter():
        if not (it.tag.endswith("item") or it.tag.endswith("entry")):
            continue
        title = (it.findtext("title") or it.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = it.findtext("link") or ""
        pub = it.findtext("pubDate") or it.findtext("{http://www.w3.org/2005/Atom}updated") or ""
        publisher = (it.findtext("source") or "").strip()
        try:
            ts = int(parsedate_to_datetime(pub).timestamp()) if pub else now_ts()
        except (TypeError, ValueError):
            ts = now_ts()
        if ts < since_ts or not title:
            continue
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3]
        out.append(NewsItem(publisher or source_name, source_type, ts, title, link))
    return out


class GoogleNewsSource:
    """Поиск Google News по ключевым словам вопроса (RSS, без ключа)."""
    name, source_type = "Google News", "media"

    def __init__(self, lang: str = "en-US", country: str = "US", timeout: float = 15.0):
        self.lang, self.country, self.timeout = lang, country, timeout

    def url(self, query: str) -> str:
        return (f"https://news.google.com/rss/search?q={quote_plus(query)}+when:14d"
                f"&hl={self.lang}&gl={self.country}&ceid={self.country}:{self.lang.split('-')[0]}")

    def search(self, query: str, since_ts: int) -> list[NewsItem]:
        q = keywords(query)
        if not q:
            return []
        try:
            r = requests.get(self.url(q), timeout=self.timeout, headers={"User-Agent": "whalefollow/0.1"})
            r.raise_for_status()
            return parse_rss(r.content, self.name, self.source_type, since_ts)[:15]
        except Exception as exc:
            log.warning("Google News: %s", exc)
            return []


@dataclass
class Forecast:
    source: str
    title: str
    url: str
    probability: float
    resolved: bool = False


def parse_manifold(rows: list) -> list[Forecast]:
    out = []
    for m in rows or []:
        prob = m.get("probability")
        if prob is None or m.get("outcomeType") not in (None, "BINARY"):
            continue
        out.append(Forecast("Manifold", str(m.get("question", "")), str(m.get("url", "")), float(prob),
                            bool(m.get("isResolved"))))
    return out


class ManifoldSource:
    """Похожие вопросы на Manifold Markets и их вероятности (публичный API без ключа).
    Совпадение вопросов не гарантировано — пользователь и ИИ видят формулировку."""
    name, source_type = "Manifold", "forecast"

    def __init__(self, base: str = "https://api.manifold.markets", timeout: float = 15.0):
        self.base, self.timeout = base.rstrip("/"), timeout

    def forecasts(self, query: str) -> list[Forecast]:
        q = keywords(query, 6)
        if not q:
            return []
        try:
            r = requests.get(f"{self.base}/v0/search-markets",
                             params={"term": q, "limit": 5, "filter": "open", "contractType": "BINARY"},
                             timeout=self.timeout)
            r.raise_for_status()
            return [f for f in parse_manifold(r.json()) if not f.resolved][:3]
        except Exception as exc:
            log.warning("Manifold: %s", exc)
            return []


# ---------------------------------------------------------------- Telegram (публичные каналы)

CHANNEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,63}$")


def normalize_channel(raw: str) -> str | None:
    """@name, t.me/name, https://t.me/s/name → name. Некорректное имя → None."""
    s = (raw or "").strip()
    s = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/(s/)?", "", s, flags=re.I).lstrip("@").split("/")[0].split("?")[0]
    return s if CHANNEL_RE.match(s) else None


class _TmeParser(HTMLParser):
    """Разбор веб-версии канала t.me/s/<канал>: сообщения, их текст, время и ссылка."""

    def __init__(self):
        super().__init__()
        self.msgs: list[dict] = []
        self._cur: dict | None = None
        self._text_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and a.get("data-post"):
            self._cur = {"post": a["data-post"], "text": "", "ts": None}
            self.msgs.append(self._cur)
        if self._cur is None:
            return
        if tag == "div" and "tgme_widget_message_text" in cls:
            self._text_depth = 1
            self._chunks = []
            return
        if self._text_depth and tag == "div":
            self._text_depth += 1
        if self._text_depth and tag == "br":
            self._chunks.append(" ")
        if tag == "time" and a.get("datetime") and self._cur.get("ts") is None:
            try:
                self._cur["ts"] = int(datetime.fromisoformat(a["datetime"]).timestamp())
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if self._text_depth and tag == "div":
            self._text_depth -= 1
            if self._text_depth == 0 and self._cur is not None:
                self._cur["text"] = re.sub(r"\s+", " ", "".join(self._chunks)).strip()

    def handle_data(self, data):
        if self._text_depth:
            self._chunks.append(data)


def parse_tme(html: str, channel: str, since_ts: int = 0) -> list[NewsItem]:
    p = _TmeParser()
    p.feed(html)
    out = []
    for m in p.msgs:
        if not m["text"] or (m["ts"] or 0) < since_ts:
            continue
        out.append(NewsItem(f"@{channel}", "telegram", m["ts"] or now_ts(), m["text"][:240],
                            f"https://t.me/{m['post']}"))
    return out


class TelegramChannelSource:
    """Поиск по публичным каналам через t.me/s/<канал>?q=… (без ключей и бота).
    Вес Telegram низкий: это триггер «проверить», а не доказательство (ТЗ 9.3)."""
    name, source_type = "Telegram", "telegram"

    def __init__(self, channels: list[str], timeout: float = 15.0, per_channel: int = 5):
        self.channels = [c for c in (normalize_channel(x) for x in channels) if c]
        self.timeout, self.per_channel = timeout, per_channel

    def search(self, query: str, since_ts: int) -> list[NewsItem]:
        q = keywords(query, 3)
        out: list[NewsItem] = []
        if not q:
            return out
        for ch in self.channels:
            try:
                r = requests.get(f"https://t.me/s/{ch}", params={"q": q}, timeout=self.timeout,
                                 headers={"User-Agent": "Mozilla/5.0 (compatible; whalefollow/0.1)"})
                r.raise_for_status()
                out.extend(parse_tme(r.text, ch, since_ts)[-self.per_channel:])
            except Exception as exc:
                log.warning("Telegram @%s: %s", ch, exc)
        return out


# ---------------------------------------------------------------- Reddit (соцсети)

def parse_reddit(payload: dict, since_ts: int = 0) -> list[NewsItem]:
    out = []
    for ch in ((payload or {}).get("data") or {}).get("children", []):
        d = ch.get("data") or {}
        ts = int(d.get("created_utc") or 0)
        if not d.get("title") or ts < since_ts:
            continue
        out.append(NewsItem(f"r/{d.get('subreddit', '?')}", "social", ts, str(d["title"])[:240],
                            "https://www.reddit.com" + str(d.get("permalink", ""))))
    return out


class RedditSource:
    """Открытый поиск Reddit за неделю (без ключа; при частых запросах Reddit может ограничить доступ)."""
    name, source_type = "Reddit", "social"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def search(self, query: str, since_ts: int) -> list[NewsItem]:
        q = keywords(query, 5)
        if not q:
            return []
        try:
            r = requests.get("https://www.reddit.com/search.json",
                             params={"q": q, "sort": "new", "t": "week", "limit": 15},
                             headers={"User-Agent": "whalefollow/0.1 (research bot)"}, timeout=self.timeout)
            r.raise_for_status()
            return parse_reddit(r.json(), since_ts)[:10]
        except Exception as exc:
            log.warning("Reddit: %s", exc)
            return []


def build_sources(enabled: dict | None, telegram_channels: list[str] | None = None,
                  db: DB | None = None) -> tuple[list, list]:
    """(источники новостей, источники прогнозов) по настройкам пользователя.
    Kalshi и букмекеры читают локальный кэш внешних рынков — нужен db."""
    enabled = enabled or {}
    news: list = [GoogleNewsSource()] if enabled.get("google_news", True) else []
    if enabled.get("reddit"):
        news.append(RedditSource())
    if telegram_channels:
        news.append(TelegramChannelSource(telegram_channels))
    fc: list = [ManifoldSource()] if enabled.get("manifold", True) else []
    if db is not None:
        from .external import BookmakerSource, KalshiSource
        if enabled.get("kalshi", True):
            fc.append(KalshiSource(db))
        if enabled.get("bookmakers", True):
            fc.append(BookmakerSource(db))
    return news, fc


def gather_forecasts(db: DB, market: dict, sources: list) -> list[dict]:
    from ..util import pmap
    rows = []
    q = market.get("question") or ""
    def ask(s):
        return list(s.forecasts(q, market=market) if getattr(s, "needs_market", False) else s.forecasts(q))
    for src, found, exc in pmap(ask, sources, workers=4):
        if exc:
            log.warning("прогнозы %s: %s", type(src).__name__, exc)
            continue
        for f in found:
            rows.append({"forecast_id": new_id("fc_"), "market_id": market["market_id"], "ts_fetched": now_ts(),
                         "source": f.source, "title": f.title, "url": f.url, "probability": f.probability,
                         "resolved": int(f.resolved)})
    if rows:
        db.upsert_many("forecasts", rows, ["forecast_id"])
    return rows


def gather(db: DB, market: dict, sources: list, lookback_days: int = 14) -> list[dict]:
    """Собирает материалы по рынку и пишет их в news_items с весом источника."""
    from ..util import pmap
    since = now_ts() - lookback_days * 86400
    items: list[dict] = []
    q = market.get("question") or ""
    # источники опрашиваются параллельно: время сбора = самый медленный источник, а не сумма
    for src, found, exc in pmap(lambda s: list(s.search(q, since)), sources, workers=6):
        if exc:
            log.warning("новости %s: %s", type(src).__name__, exc)
            continue
        for it in found:
            w = source_weight(db, it.source, it.source_type)
            row = {"item_id": new_id("ni_"), "market_id": market["market_id"], "ts_pub": it.ts_pub,
                   "ts_fetched": now_ts(), "source": it.source, "source_type": it.source_type,
                   "source_weight": w, "url": it.url, "title": it.title, "text_hash": it.text_hash}
            items.append(row)
    if items:
        db.insert_ignore("news_items", items)
    return items


def source_weight(db: DB, source: str, source_type: str, prior_n: float = 20.0) -> float:
    """Вес = байесовская смесь начального веса типа и наблюдаемой точности источника."""
    prior = DEFAULT_WEIGHTS.get(source_type, 0.5)
    row = db.one("SELECT n, hits FROM source_stats WHERE source=?", (source,))
    if not row or not row["n"]:
        return prior
    return float((prior * prior_n + row["hits"]) / (prior_n + row["n"]))


def update_source_stats(db: DB, source: str, source_type: str, confirmed: bool) -> None:
    row = db.one("SELECT n, hits FROM source_stats WHERE source=?", (source,)) or {"n": 0, "hits": 0}
    n, hits = row["n"] + 1, row["hits"] + int(confirmed)
    db.upsert("source_stats", {"source": source, "source_type": source_type, "n": n, "hits": hits,
                               "weight": source_weight(db, source, source_type)}, ["source"])


def manipulation_flags(items: list[dict], price_moves: list[tuple[int, float]] | None = None,
                       now: int | None = None) -> dict:
    """Проверки 9.4: подтверждения, время публикации против движения цены, копии текста."""
    now = now or now_ts()
    primary = {i["source"] for i in items if i["source_type"] in PRIMARY_TYPES}
    hashes: dict[str, set] = {}
    for i in items:
        hashes.setdefault(i["text_hash"], set()).add(i["source"])
    coordinated = any(len(srcs) >= 3 for srcs in hashes.values())
    first_move_ts = None
    if price_moves:
        base = price_moves[0][1]
        for ts, p in price_moves:
            if abs(p - base) >= 0.05:
                first_move_ts = ts
                break
    first_news = min((i["ts_pub"] for i in items), default=None)
    news_after_move = bool(first_move_ts and first_news and first_news > first_move_ts)
    already_priced = False
    if price_moves and first_news:
        before = [p for ts, p in price_moves if ts <= first_news]
        if before:
            already_priced = abs(price_moves[-1][1] - before[-1]) >= 0.05
    return {
        "primary_confirmations": len(primary),
        "enough_confirmation": len(primary) >= 2,
        "coordinated_text": coordinated,
        "news_after_price_move": news_after_move,
        "already_priced": already_priced,
    }


def materials_for_prompt(items: list[dict], flags: dict) -> list[dict]:
    """Материалы для модели: скоординированные копии убираем, низковесные помечаем как слухи."""
    seen, out = set(), []
    for i in sorted(items, key=lambda x: -(x["source_weight"] or 0)):
        if flags.get("coordinated_text") and i["text_hash"] in seen:
            continue
        seen.add(i["text_hash"])
        tag = i["source_type"] if (i["source_weight"] or 0) >= 0.5 else f"{i['source_type']}, unverified"
        out.append({"source": i["source"], "source_type": tag, "ts_pub": i["ts_pub"],
                    "title": i["title"], "url": i["url"]})
    return out


def sleep(s: float) -> None:
    time.sleep(s)
