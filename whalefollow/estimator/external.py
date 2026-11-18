"""Цены других площадок как независимые прогнозы: Kalshi и букмекеры (The Odds API).

Kalshi (публичный API, без ключа): у API нет поиска по тексту, поэтому открытые события
с рынками раз в 30 минут сохраняются в ext_markets, а похожий вопрос ищется локально
по совпадению значимых слов. Вероятность — середина между лучшей покупкой и продажей «Да»
(при узком спреде) или цена последней сделки.

Букмекеры (нужен WF_ODDS_API_KEY): коэффициенты «победитель матча» по выбранным видам спорта
раз в 12 часов (1 кредит на вид спорта). Вероятности — медиана 1/коэффициент по букмекерам,
нормированная к сумме 1 (убирается маржа). Сопоставление с рынком Polymarket — по названиям команд.

Совпадение вопросов не гарантировано: пользователь и ИИ видят формулировку, это подсказка, а не довод.
"""
from __future__ import annotations

from typing import Any

import json
import logging
import os
import re
import statistics
from datetime import datetime

from .. import net

from ..db import DB, now_ts
from .sources import STOP, Forecast

log = logging.getLogger(__name__)

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_SPORTS = ("basketball_nba", "americanfootball_nfl", "icehockey_nhl", "baseball_mlb", "soccer_epl",
                  "soccer_uefa_champs_league", "mma_mixed_martial_arts")
GENERIC = {"united", "city", "club", "real", "state", "team", "sporting", "athletic", "athletico", "atletico",
           "fc", "sc", "ac", "afc", "cf", "de", "la", "los", "las", "san", "new", "york", "the"}
EXTRA_STOP = {"win", "wins", "beat", "beats", "game", "match", "vs", "versus", "yes", "no", "price", "above", "below",
              "reach", "hit", "market", "who", "which", "what", "how", "many", "much", "between"}


def _ts(s) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9$%]+", (text or "").lower()):
        if w in STOP or w in EXTRA_STOP or (len(w) < 3 and not w.isdigit()):
            continue
        out.add(w)
    return out


def similarity(a: set[str], b: set[str]) -> float:
    """Коэффициент Дайса по значимым словам: 1 — одинаковые наборы, 0 — ничего общего."""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Kalshi
class KalshiClient:
    def __init__(self, base: str = KALSHI_BASE, timeout: float = 20.0, session: Any = None):
        self.base, self.timeout = base.rstrip("/"), timeout
        self.s = session if session is not None else net.client()

    def open_events(self, max_pages: int = 60):
        cursor = None
        for _ in range(max_pages):
            params = {"status": "open", "with_nested_markets": "true", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            r = self.s.get(f"{self.base}/events", params=params, timeout=self.timeout)
            r.raise_for_status()
            data = r.json() or {}
            yield from data.get("events") or []
            cursor = data.get("cursor")
            if not cursor:
                return


def kalshi_price(m: dict) -> float | None:
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    if bid and ask and 0 < bid <= ask < 1 and ask - bid <= 0.10:
        return round((bid + ask) / 2, 4)
    last = _f(m.get("last_price_dollars"))
    return last if last and 0 < last < 1 else None


def kalshi_rows(events, now: int | None = None) -> list[dict]:
    now = now or now_ts()
    rows = []
    for ev in events:
        title = ev.get("title") or ""
        series = (ev.get("series_ticker") or ev.get("event_ticker") or "").lower()
        for m in ev.get("markets") or []:
            if str(m.get("status", "active")).lower() not in ("active", "open", "initialized"):
                continue
            p = kalshi_price(m)
            if p is None:
                continue
            sub = m.get("yes_sub_title") or m.get("subtitle") or ""
            rows.append({"source": "kalshi", "ext_id": m.get("ticker"), "title": title, "subtitle": sub,
                         "probability": p, "close_ts": _ts(m.get("close_time")),
                         "url": f"https://kalshi.com/markets/{series}" if series else "https://kalshi.com",
                         "extra": json.dumps({"event": ev.get("event_ticker")}), "updated_ts": now})
    return [r for r in rows if r["ext_id"]]


def sync_kalshi(db: DB, client: KalshiClient | None = None) -> int:
    client = client or KalshiClient()
    now = now_ts()
    rows = kalshi_rows(client.open_events(), now)
    with db.tx():
        db.upsert_many("ext_markets", rows, ["source", "ext_id"])
        db.execute("DELETE FROM ext_markets WHERE source='kalshi' AND updated_ts<?", (now,))   # закрытые
    return len(rows)


class _Cache:
    """Внешние рынки в памяти с привязкой к времени обновления таблицы."""

    def __init__(self, db: DB, source: str):
        self.db, self.source = db, source
        self._at, self._rows = None, []

    def rows(self) -> list[dict]:
        at = self.db.scalar("SELECT MAX(updated_ts) AS m FROM ext_markets WHERE source=?", (self.source,))
        if at != self._at:
            self._rows = self.db.query("SELECT * FROM ext_markets WHERE source=?", (self.source,))
            for r in self._rows:
                r["_w"] = words(f"{r['title']} {r['subtitle'] or ''}")
            self._at = at
        return self._rows


class KalshiSource:
    name, source_type = "Kalshi", "forecast"

    def __init__(self, db: DB, min_similarity: float = 0.5, min_common: int = 3):
        self.cache = _Cache(db, "kalshi")
        self.min_sim, self.min_common = min_similarity, min_common

    def forecasts(self, question: str) -> list[Forecast]:
        q = words(question)
        scored = []
        for r in self.cache.rows():
            common = len(q & r["_w"])
            if common < self.min_common:
                continue
            s = similarity(q, r["_w"])
            if s >= self.min_sim:
                scored.append((s, r))
        scored.sort(key=lambda x: -x[0])
        out = []
        for s, r in scored[:2]:
            t = r["title"] + (f" — {r['subtitle']}" if r["subtitle"] else "")
            out.append(Forecast(source="Kalshi", title=t, url=r["url"], probability=float(r["probability"])))
        return out


# ---------------------------------------------------------------- букмекеры
class OddsClient:
    def __init__(self, api_key: str, base: str = ODDS_BASE, timeout: float = 20.0, session: Any = None):
        self.key, self.base, self.timeout = api_key, base.rstrip("/"), timeout
        self.s = session if session is not None else net.client()
        self.remaining: str | None = None

    @classmethod
    def from_env(cls) -> "OddsClient | None":
        key = os.environ.get("WF_ODDS_API_KEY")
        return cls(key) if key else None

    def odds(self, sport: str) -> list[dict]:
        r = self.s.get(f"{self.base}/sports/{sport}/odds/", timeout=self.timeout,
                       params={"apiKey": self.key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"})
        self.remaining = getattr(r, "headers", {}).get("x-requests-remaining", self.remaining)
        if r.status_code == 404:
            return []                                  # вид спорта не в сезоне
        r.raise_for_status()
        return r.json() or []


def devig(event: dict) -> dict[str, float]:
    """Вероятности исходов без маржи букмекера: медиана 1/коэффициент, нормировка к 1."""
    implied: dict[str, list[float]] = {}
    for bk in event.get("bookmakers") or []:
        for mk in bk.get("markets") or []:
            if mk.get("key") != "h2h":
                continue
            for o in mk.get("outcomes") or []:
                price = _f(o.get("price"))
                if price and price > 1:
                    implied.setdefault(o["name"], []).append(1 / price)
    med = {k: statistics.median(v) for k, v in implied.items() if v}
    total = sum(med.values())
    return {k: v / total for k, v in med.items()} if total else {}


def odds_rows(events: list[dict], sport: str, now: int | None = None) -> list[dict]:
    now = now or now_ts()
    rows = []
    for ev in events:
        probs = devig(ev)
        home, away = ev.get("home_team"), ev.get("away_team")
        if not probs or not home or not away or home not in probs:
            continue
        rows.append({"source": "odds", "ext_id": ev.get("id"), "title": f"{home} vs {away}", "subtitle": sport,
                     "probability": round(probs[home], 4), "close_ts": _ts(ev.get("commence_time")),
                     "url": "https://the-odds-api.com", "updated_ts": now,
                     "extra": json.dumps({"home": home, "away": away, "probs": probs, "sport": sport},
                                         ensure_ascii=False)})
    return [r for r in rows if r["ext_id"]]


def sync_odds(db: DB, client: OddsClient, sports: list[str] | None = None) -> int:
    sports = sports or [s for s in os.environ.get("WF_ODDS_SPORTS", "").split(",") if s.strip()] or list(DEFAULT_SPORTS)
    now = now_ts()
    rows = []
    for sp in sports:
        try:
            rows += odds_rows(client.odds(sp.strip()), sp.strip(), now)
        except net.HTTPError as exc:
            log.warning("букмекеры %s: %s", sp, exc)
    with db.tx():
        db.upsert_many("ext_markets", rows, ["source", "ext_id"])
        db.execute("DELETE FROM ext_markets WHERE source='odds' AND close_ts IS NOT NULL AND close_ts<?",
                   (now - 6 * 3600,))                # прошедшие матчи
    if client.remaining is not None:
        db.set_state("odds_remaining", client.remaining)
    return len(rows)


def team_keys(name: str) -> set[str]:
    """Отличительные слова названия команды: «Los Angeles Lakers» → {angeles, lakers}."""
    return {w for w in re.findall(r"[a-z0-9]+", name.lower()) if len(w) >= 4 and w not in GENERIC} or \
        {name.lower()}


class BookmakerSource:
    """Только спортивные рынки. Вероятность — для первого исхода рынка (или «Да»)."""
    name, source_type = "Букмекеры", "forecast"
    needs_market = True

    def __init__(self, db: DB):
        self.db = db
        self.cache = _Cache(db, "odds")

    def _match(self, text: str, row: dict) -> dict | None:
        ex = json.loads(row["extra"] or "{}")
        low = text.lower()
        home, away = ex.get("home", ""), ex.get("away", "")
        if any(k in low for k in team_keys(home)) and any(k in low for k in team_keys(away)):
            return ex
        return None

    def forecasts(self, question: str, market: dict | None = None) -> list[Forecast]:
        if not market or (market.get("category") or "") != "sports":
            return []
        order = " ORDER BY rowid" if self.db.kind == "sqlite" else ""        # порядок исходов = порядок вставки
        labels = [r["outcome_label"] for r in self.db.query(
            f"SELECT outcome_label FROM tokens WHERE market_id=?{order}", (market.get("market_id"),))]
        text = " ".join([question or ""] + labels)
        for row in self.cache.rows():
            ex = self._match(text, row)
            if not ex:
                continue
            probs = ex["probs"]
            yes_no = {str(x).lower() for x in labels} <= {"yes", "no"}
            if yes_no:
                # «Will X win/beat …»: субъект — команда, названная в вопросе первой
                pos = {t: min((question.lower().find(k) for k in team_keys(t) if k in question.lower()), default=10**6)
                       for t in (ex["home"], ex["away"])}
                subject = min(pos, key=pos.get)
            else:
                first = (labels[0] if labels else "").lower()
                subject = next((t for t in (ex["home"], ex["away"]) if any(k in first for k in team_keys(t))),
                               ex["home"])
            p = probs.get(subject)
            if p is None:
                continue
            parts = " · ".join(f"{k} {v * 100:.0f}%" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))
            return [Forecast(source="Букмекеры", title=f"{subject}: {parts}", url=row["url"], probability=round(p, 4))]
        return []
