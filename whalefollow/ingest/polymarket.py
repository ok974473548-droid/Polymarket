"""Клиенты публичных API Polymarket (только чтение).

  Gamma API — каталог событий и рынков, исходы.
  Data API v2 — сделки кошельков (/v2/trades, курсорная пагинация, snake_case).
  CLOB API — стакан (/book) и история цен (/prices-history).

Разбор ответов вынесен в отдельные функции parse_* — их проверяют тесты на
фиксированных примерах. Поля сверены с docs.polymarket.com (ноябрь 2026);
при изменении API правится только этот модуль.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from .. import fees, net
from ..config import Endpoints
from ..net import ApiError
from ..util import RateLimiter, pmap

log = logging.getLogger(__name__)


class _Http:
    """GET с повторами (429/5xx и обрывы, с учётом Retry-After), общим пулом соединений httpx (HTTP/2)
    и ограничением частоты «ведром жетонов». session — любой объект с методом get (в тестах — подделка)."""

    def __init__(self, base: str, timeout: float = 15.0, retries: int = 4, session: Any = None, rps: float = 15.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.s = session if session is not None else net.client()
        self.limiter = RateLimiter(rps)

    def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        return net.get_json(
            self.s,
            url,
            params=params,
            timeout=self.timeout,
            retries=self.retries,
            before=self.limiter.wait,
            on_retry=lambda err, d: log.warning("GET %s: %s, повтор через %.1f с", url, err, d),
        )

    async def aget(self, path: str, params: dict | None = None, session: Any = None) -> Any:
        """Асинхронный GET с теми же повторами и общим ограничителем частоты."""
        url = f"{self.base}{path}"
        if session is not None:
            return await net.aget_json(
                session, url, params=params, timeout=self.timeout, retries=self.retries, before=self.limiter.await_
            )
        async with net.async_client() as s:
            return await net.aget_json(
                s, url, params=params, timeout=self.timeout, retries=self.retries, before=self.limiter.await_
            )

    def warm(self, path: str, params: dict | None = None, timeout: float = 5.0) -> int:
        """Один лёгкий запрос без повторов: держит соединение (TLS) открытым, чтобы рабочие запросы
        не тратили время на установку связи. Возвращает время ответа в мс."""
        t0 = time.perf_counter()
        r = self.s.get(f"{self.base}{path}", params=params, timeout=timeout)
        _ = r.content  # дочитываем — соединение вернётся в пул
        return int((time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------- разбор ответов


def _json_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (TypeError, ValueError):
        return []


def _ts(v) -> int | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC).timestamp())
    except ValueError:
        return None


from ..categories import pick as _pick_category  # noqa: E402


def market_tags(raw: dict) -> list[str]:
    out = []
    for tag in list(raw.get("tags") or []) + [
        t for ev in (raw.get("events") or []) if isinstance(ev, dict) for t in (ev.get("tags") or [])
    ]:
        if isinstance(tag, dict):
            out.append(str(tag.get("slug") or tag.get("label") or "").strip().lower().replace(" ", "-"))
    out += [str(raw.get(k) or "").strip().lower().replace(" ", "-") for k in ("category", "categorySlug")]
    return [t for t in out if t]


def normalize_category(raw: dict) -> str:
    cands = []
    for key in ("category", "categorySlug"):
        if raw.get(key):
            cands.append(str(raw[key]))
    for tag in raw.get("tags") or []:
        if isinstance(tag, dict):
            cands.append(str(tag.get("slug") or tag.get("label") or ""))
    for ev in raw.get("events") or []:
        if isinstance(ev, dict):
            cands.append(str(ev.get("category") or ""))
            for tag in ev.get("tags") or []:
                if isinstance(tag, dict):
                    cands.append(str(tag.get("slug") or ""))
    return _pick_category([c.strip().lower().replace(" ", "-") for c in cands])


def parse_market(raw: dict) -> tuple[dict, list[dict], dict | None]:
    """Gamma /markets → (строка markets, строки tokens, строка events|None)."""
    market_id = raw.get("conditionId") or raw.get("condition_id")
    token_ids = [str(t) for t in _json_list(raw.get("clobTokenIds"))]
    outcomes = [str(o) for o in _json_list(raw.get("outcomes"))]
    prices = [float(p) for p in _json_list(raw.get("outcomePrices"))]
    events = raw.get("events") or []
    ev = events[0] if events and isinstance(events[0], dict) else None
    event_id = str(ev.get("id")) if ev and ev.get("id") is not None else None
    closed = bool(raw.get("closed"))
    uma = str(raw.get("umaResolutionStatus") or "").lower()
    resolved = bool(closed and prices and max(prices) >= 0.99 and min(prices) <= 0.01)
    # рынок, разрешённый «50 на 50» (отменён или исход не определить): каждой стороне выплачивается по 0,5
    split_half = bool(closed and uma == "resolved" and len(prices) == 2 and all(abs(p - 0.5) <= 0.01 for p in prices))
    disputed = uma in ("disputed", "challenged")
    if disputed:
        resolved = split_half = False  # исход оспорен — выплату не фиксируем, пока спор не закончится
    status = "disputed" if disputed else ("resolved" if resolved or split_half else ("closed" if closed else "open"))
    category = normalize_category(raw)
    market = {
        "market_id": market_id,
        "event_id": event_id,
        "question": raw.get("question"),
        "category": category,
        "end_ts": _ts(raw.get("endDate") or raw.get("end_date_iso")),
        "status": status,
        "resolved_ts": _ts(raw.get("closedTime") or raw.get("umaEndDate")) if resolved or split_half else None,
        "liquidity_usd": float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0),
        "volume_usd": float(raw.get("volumeNum") or raw.get("volume") or 0.0),
        "neg_risk": 1 if raw.get("negRisk") else 0,
        "fee_bps": float(raw["feeBps"]) if raw.get("feeBps") not in (None, "") else None,
        "fee_rate": fees.rate_for(category, market_tags(raw)),
        "url": (
            f"https://polymarket.com/event/{ev['slug']}"
            if ev and ev.get("slug")
            else (f"https://polymarket.com/market/{raw['slug']}" if raw.get("slug") else None)
        ),
        "rules": raw.get("description"),
        "updated_ts": int(time.time()),
    }
    tokens = []
    for i, tid in enumerate(token_ids):
        payout = None
        if resolved and i < len(prices):
            payout = 1.0 if prices[i] >= 0.99 else 0.0
        elif split_half:
            payout = 0.5
        tokens.append(
            {
                "token_id": tid,
                "market_id": market_id,
                "outcome_label": outcomes[i] if i < len(outcomes) else str(i),
                "payout": payout,
            }
        )
    event = None
    if ev:
        event = {
            "event_id": event_id,
            "title": ev.get("title"),
            "category": category,
            "start_ts": _ts(ev.get("startDate")),
            "end_ts": _ts(ev.get("endDate")),
        }
    return market, tokens, event


def trade_id_for(row: dict) -> str:
    """В /v2/trades нет log_index — id строится из полей сделки (идемпотентность загрузки)."""
    key = "|".join(
        str(row.get(k, ""))
        for k in ("transaction_hash", "proxy_wallet", "token_id", "side", "size", "price", "timestamp")
    )
    return hashlib.sha1(key.encode()).hexdigest()


def parse_trade(row: dict, fetched_ts: int | None = None) -> dict:
    """Data API /v2/trades → строка trades."""
    price = float(row["price"])
    size = float(row["size"])
    return {
        "trade_id": trade_id_for(row),
        "ts": int(row["timestamp"]),
        "ts_fetched": fetched_ts or int(time.time()),
        "wallet": str(row["proxy_wallet"]).lower(),
        "token_id": str(row["token_id"]),
        "market_id": row.get("condition_id"),
        "side": str(row["side"]).upper(),
        "price": price,
        "size_shares": size,
        "size_usd": price * size,
        "is_maker": None,
        "tx_hash": str(row["transaction_hash"]).lower() if row.get("transaction_hash") else None,
    }


def parse_book(raw: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """CLOB /book → (bids по убыванию цены, asks по возрастанию)."""
    bids = sorted(((float(b["price"]), float(b["size"])) for b in raw.get("bids", [])), key=lambda x: -x[0])
    asks = sorted(((float(a["price"]), float(a["size"])) for a in raw.get("asks", [])), key=lambda x: x[0])
    return bids, asks


def parse_price_history(raw: dict) -> list[tuple[int, float]]:
    return [(int(p["t"]), float(p["p"])) for p in raw.get("history", [])]


# ---------------------------------------------------------------- клиенты


class GammaClient:
    """Каталог рынков. Важно: без параметра closed Gamma отдаёт только ОТКРЫТЫЕ рынки,
    поэтому запросы по идентификаторам идут с closed=any (иначе закрытые рынки китов не найти).
    Лимит /markets — 300 запросов за 10 с (сверх лимита Cloudflare придерживает запросы)."""

    MAX_IDS = 100  # не больше 100 идентификаторов в одном фильтре (документация Gamma)

    def __init__(self, endpoints: Endpoints | None = None, http: _Http | None = None):
        self.http = http or _Http((endpoints or Endpoints()).gamma, rps=25)
        self._array_mode: str | None = None  # как сервер понимает список: repeat (a&a) или comma (a,b)

    def public_profile(self, address: str) -> dict | None:
        """Публичный профиль Polymarket по кошельку: имя, аватар, X, дата создания. None — профиля нет."""
        try:
            return self.http.get("/public-profile", {"address": address}) or None
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def search_profiles(self, q: str, limit: int = 6) -> list[dict]:
        """Поиск игроков по имени (как строка поиска на сайте)."""
        res = (
            self.http.get(
                "/public-search",
                {"q": q, "search_profiles": "true", "limit_per_type": limit, "events_status": "active"},
            )
            or {}
        )
        out = []
        for p in (res.get("profiles") or [])[:limit]:
            w = p.get("proxyWallet")
            if w:
                out.append(
                    {
                        "wallet": str(w).lower(),
                        "name": p.get("name") or p.get("pseudonym"),
                        "pseudonym": p.get("pseudonym"),
                        "image": p.get("profileImage"),
                    }
                )
        return out

    def markets(
        self, closed: bool | None = None, page_size: int = 500, max_pages: int = 1000, workers: int = 1, **filters
    ) -> Iterator[dict]:
        """Постраничный обход каталога. workers > 1 — страницы запрашиваются волнами параллельно."""

        def page(offset: int):
            params = {"limit": page_size, "offset": offset, **filters}
            if closed is not None:
                params["closed"] = str(closed).lower()
            return self.http.get("/markets", params) or []

        offset, pages = 0, 0
        while pages < max_pages:
            wave = [offset + i * page_size for i in range(max(1, min(workers, max_pages - pages)))]
            results = (
                [page(wave[0])] if len(wave) == 1 else [r for _, r, e in pmap(page, wave, len(wave)) if not _raise(e)]
            )
            for batch in results:
                yield from batch
                if len(batch) < page_size:
                    return
            offset += page_size * len(wave)
            pages += len(wave)

    def market(self, condition_id: str) -> dict | None:
        res = self.http.get("/markets", {"condition_ids": condition_id, "closed": "any"})
        return res[0] if res else None

    def _by_ids(self, ids: list[str], mode: str) -> list[dict]:
        want = {c.lower() for c in ids}
        val = list(ids) if mode == "repeat" else ",".join(ids)
        res = self.http.get("/markets", {"condition_ids": val, "closed": "any", "limit": len(ids)}) or []
        return [m for m in res if str(m.get("conditionId") or m.get("condition_id") or "").lower() in want]

    def markets_by_ids(self, condition_ids: list[str]) -> list[dict]:
        """Несколько рынков (открытых и закрытых) одним запросом.
        Формат списка в документации не указан: при первом вызове пробуются оба варианта
        и запоминается тот, что вернул больше рынков. Недостающее вызывающий код догружает по одному."""
        ids = list(condition_ids)[: self.MAX_IDS]
        if self._array_mode or len(ids) == 1:
            return self._by_ids(ids, self._array_mode or "repeat")
        best, best_mode = [], None
        for mode in ("repeat", "comma"):
            try:
                got = self._by_ids(ids, mode)
            except ApiError as exc:
                log.warning("рынки пачкой (%s): %s", mode, exc)
                continue
            if len(got) > len(best):
                best, best_mode = got, mode
            if len(got) == len(ids):
                break
        if best_mode and len(best) > 1:
            self._array_mode = best_mode
            log.info("каталог Polymarket: список идентификаторов в формате %s", best_mode)
        return best


def _raise(exc):
    if exc is not None:
        raise exc
    return False


class DataClient:
    def __init__(self, endpoints: Endpoints | None = None, http: _Http | None = None):
        self.http = http or _Http((endpoints or Endpoints()).data, rps=18)  # /trades: 200 за 10 с
        self.lb_version: str | None = None

    def trades(
        self,
        user: str | None = None,
        start: int | None = None,
        end: int | None = None,
        condition: str | None = None,
        side: str | None = None,
        limit: int = 1000,
        max_pages: int = 10_000,
        taker_only: bool | None = None,
    ) -> Iterator[dict]:
        params: dict = {"limit": limit}
        if taker_only is not None:
            params["taker_only"] = str(taker_only).lower()
        if user:
            params["user"] = user
            params["start"] = 1 if start is None else max(int(start), 1)
            if end:
                params["end"] = int(end)
        if condition:
            params["condition"] = condition
        if side:
            params["side"] = side
        for _ in range(max_pages):
            res = self.http.get("/v2/trades", params)
            rows = (res or {}).get("data") or []
            yield from rows
            cursor = ((res or {}).get("pagination") or {}).get("next_cursor")
            if not cursor:
                return
            params = {"cursor": cursor, **({"user": user} if user else {})}

    ACTIVITY_TYPES = "SPLIT,MERGE,CONVERSION"
    PAGE_MAX = 1000  # строк за запрос в методах /v2 (дальше — курсор)

    def _v2_pages(self, path: str, params: dict, max_pages: int, deadline: float | None = None) -> Iterator[dict]:
        """Постраничный обход метода Data API v2: {data, pagination: {next_cursor, has_more}}.
        Смещения (offset) в v2 нет — сервер отвечает 400, поэтому только курсор.
        Защита от зацикливания: тот же курсор или страница из уже виденных строк — конец; deadline (time.time())
        ограничивает общее время обхода."""
        anchor = {k: params[k] for k in ("user", "status") if k in params}
        cursors: set = set()
        seen: set = set()
        for _ in range(max_pages):
            if deadline is not None and time.time() > deadline:
                raise ApiError(f"{path}: не уложились во время, получено строк: {len(seen)}")
            res = self.http.get(path, params) or {}
            rows = (res.get("data") if isinstance(res, dict) else res) or []
            keys = [json.dumps(r, sort_keys=True, default=str)[:400] for r in rows]
            fresh = [r for r, k in zip(rows, keys) if k not in seen]
            seen.update(keys)
            yield from fresh
            pag = (res.get("pagination") or {}) if isinstance(res, dict) else {}
            cur = pag.get("next_cursor")
            if not rows or not fresh or not cur or pag.get("has_more") is False or cur in cursors:
                return
            cursors.add(cur)
            params = {**anchor, "cursor": cur}

    def activity(
        self,
        user: str,
        start: int | None = None,
        end: int | None = None,
        types: str = ACTIVITY_TYPES,
        max_pages: int = 200,
    ) -> Iterator[dict]:
        """Журнал операций кошелька (/v2/activity, курсор). Строки приводятся к прежнему виду (camelCase),
        повторы отсекаются по ключу. Старый /activity Polymarket отключил (HTTP 410)."""
        params = {
            "user": user,
            "type": types,
            "limit": self.PAGE_MAX,
            "start": 1 if start is None else max(int(start), 1),
            "sort_direction": "DESC",
        }
        if end:
            params["end"] = int(end)
        seen: set = set()
        for r in self._v2_pages("/v2/activity", params, max_pages):
            r = v1_activity(r)
            key = activity_id_for(r)
            if key not in seen:
                seen.add(key)
                yield r

    POSITION_STATUSES = ("OPEN", "REDEEMABLE", "REDEEMABLE_LOST")

    def positions(self, user: str, max_pages: int = 20, deadline: float | None = None) -> list[dict]:
        """Позиции кошелька, которые ещё держатся (открытые и ждущие погашения) — /v2/positions."""
        out = []
        for st in self.POSITION_STATUSES:
            out += [
                v1_position(r)
                for r in self._v2_pages(
                    "/v2/positions", {"user": user, "status": st, "limit": self.PAGE_MAX}, max_pages, deadline
                )
            ]
        return out

    def closed_positions(self, user: str, max_pages: int = 60, deadline: float | None = None) -> list[dict]:
        """Закрытые позиции кошелька — /v2/positions?status=CLOSED (старый /closed-positions отключён)."""
        return [
            v1_position(r)
            for r in self._v2_pages(
                "/v2/positions", {"user": user, "status": "CLOSED", "limit": self.PAGE_MAX}, max_pages, deadline
            )
        ]

    # ---------------- готовая статистика игрока (Polymarket уже всё посчитал — не пересчитываем сами)
    @staticmethod
    def _data(res):
        return res.get("data") if isinstance(res, dict) else res

    def user_stats(self, user: str) -> dict | None:
        """Карточка профиля: рынков, крупнейший выигрыш, дата регистрации, прибыль за всё время."""
        return self._data(self.http.get("/v2/user-stats", {"user": user}))

    def user_pnl(self, user: str, interval: str = "1m", fidelity: str = "1d") -> list[dict]:
        """Кривая прибыли (нарастающим итогом). interval: 1d 1w 1m all; fidelity: 1h 3h 12h 1d."""
        d = self._data(self.http.get("/v2/user-pnl", {"user": user, "interval": interval, "fidelity": fidelity})) or {}
        return d.get("points") or []

    def user_volume(self, user: str, start: int | None = None, end: int | None = None) -> dict:
        params = {"user": user}
        if start:
            params["start"] = int(start)
        if end:
            params["end"] = int(end)
        return self._data(self.http.get("/v2/user-volume", params)) or {}

    def value(self, user: str) -> float | None:
        d = self._data(self.http.get("/v2/value", {"user": user})) or {}
        return d.get("value")

    def standing(self, user: str, period: str = "all") -> dict | None:
        """Место игрока в рейтинге по прибыли и объёму (None — вне рейтинга)."""
        return self._data(self.http.get("/v2/leaderboard", {"user": user, "time_period": period}))

    def positions_page(
        self,
        user: str | None = None,
        condition: str | None = None,
        status: str = "OPEN",
        sort_by: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Одна страница позиций как есть (поля v2): для карточек в интерфейсе, без обхода всех страниц."""
        params = {"status": status, "limit": limit}
        if user:
            params["user"] = user
        if condition:
            params["condition"] = condition
        if sort_by:
            params["sort_by"] = sort_by
        return self._data(self.http.get("/v2/positions", params)) or []

    def holders(self, condition: str, limit: int = 12, include_pnl: bool = True) -> dict[str, list[dict]]:
        """Крупнейшие держатели рынка по исходам: {token_id: [держатели]}."""
        rows = (
            self._data(
                self.http.get(
                    "/v2/holders",
                    {"condition": condition, "limit": limit, "include_pnl": "true" if include_pnl else "false"},
                )
            )
            or []
        )
        return {str(r.get("token_id")): r.get("holders") or [] for r in rows}

    LB_CATEGORIES = (
        "OVERALL",
        "POLITICS",
        "SPORTS",
        "ESPORTS",
        "CRYPTO",
        "CULTURE",
        "MENTIONS",
        "WEATHER",
        "ECONOMICS",
        "TECH",
        "FINANCE",
    )
    LB_PERIODS = {"DAY": "day", "WEEK": "week", "MONTH": "month", "ALL": "all"}
    LB_V2_MAX = 1000  # строк за один запрос /v2/leaderboard

    def leaderboard(
        self, category: str = "OVERALL", period: str = "MONTH", order_by: str = "PNL", limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """Рейтинг трейдеров Polymarket по прибыли или объёму за день/неделю/месяц/всё время.

        Основной метод — /v2/leaderboard (до 1000 строк за запрос, курсор). Старый /v1/leaderboard Polymarket
        отключил (отвечает HTTP 410); к нему обращаемся, только если v2 недоступен. Ответ приводится к одному виду."""
        limit, offset = max(1, int(limit)), max(0, int(offset))
        try:
            rows = self._leaderboard_v2(category, period, order_by, offset + limit)
            self.lb_version = "v2"
        except ApiError as v2_exc:
            try:
                rows = self._leaderboard_v1(category, period, order_by, offset + limit)
                self.lb_version = "v1"
            except ApiError:
                raise v2_exc from None  # показываем ошибку основного метода
        out = [parse_leader(r) for r in rows]
        return [r for r in out if r["wallet"]][offset : offset + limit]

    def _leaderboard_v2(self, category: str, period: str, order_by: str, need: int) -> list[dict]:
        params = {
            "time_period": self.LB_PERIODS.get(period.upper(), "month"),
            "category": category.lower(),
            "sort_by": "VOLUME" if order_by.upper() in ("VOL", "VOLUME") else "PNL",
            "limit": max(1, min(need, self.LB_V2_MAX)),
        }
        rows: list = []
        for _ in range(10):
            res = self.http.get("/v2/leaderboard", params) or {}
            page = res.get("data") if isinstance(res, dict) else res
            rows += page or []
            pag = (res.get("pagination") or {}) if isinstance(res, dict) else {}
            if len(rows) >= need or not pag.get("has_more") or not pag.get("next_cursor") or not page:
                break
            params = {**params, "cursor": pag["next_cursor"], "limit": max(1, min(need - len(rows), self.LB_V2_MAX))}
        return rows

    def _leaderboard_v1(self, category: str, period: str, order_by: str, need: int) -> list[dict]:
        rows: list = []
        for off in range(0, min(need, 1050), 50):
            page = (
                self.http.get(
                    "/v1/leaderboard",
                    {
                        "category": category.upper(),
                        "timePeriod": period.upper(),
                        "orderBy": "VOL" if order_by.upper() in ("VOL", "VOLUME") else "PNL",
                        "limit": 50,
                        "offset": min(off, 1000),
                    },
                )
                or []
            )
            if isinstance(page, dict):
                page = page.get("data") or []
            rows += page
            if len(page) < 50:
                break
        return rows


def parse_leader(r: dict) -> dict:
    """Строка рейтинга (v1: proxyWallet/userName/vol/pnl; v2: user_id/user_name/volume/pnl)."""

    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    return {
        "rank": int(f(r.get("rank")) or 0),
        "wallet": str(r.get("proxyWallet") or r.get("user_id") or "").lower(),
        "name": r.get("userName") or r.get("user_name") or None,
        "pnl": f(r.get("pnl")),
        "volume": f(r.get("vol") if r.get("vol") is not None else r.get("volume")),
        "x": r.get("xUsername") or r.get("x_username") or None,
        "verified": bool(r.get("verifiedBadge") or r.get("verified")),
    }


def _first(r: dict, *keys):
    for k in keys:
        if r.get(k) is not None:
            return r[k]
    return None


def v1_activity(r: dict) -> dict:
    """Строка /v2/activity (snake_case) → прежний вид (camelCase), который понимает остальной код."""
    if "proxyWallet" in r:
        return r
    return {
        "proxyWallet": r.get("proxy_wallet"),
        "timestamp": r.get("timestamp"),
        "type": r.get("type"),
        "conditionId": r.get("condition_id"),
        "asset": r.get("token_id"),
        "outcomeIndex": r.get("outcome_index"),
        "size": r.get("size"),
        "usdcSize": r.get("usdc_size"),
        "price": r.get("price"),
        "side": r.get("side"),
        "transactionHash": r.get("transaction_hash"),
        "title": r.get("title"),
        "outcome": r.get("outcome"),
    }


def v1_position(r: dict) -> dict:
    """Строка /v2/positions → прежний вид: asset, realizedPnl, cashPnl (нереализованный результат), curPrice."""
    if "asset" in r:
        return r
    return {
        "proxyWallet": r.get("proxy_wallet"),
        "asset": r.get("token_id"),
        "conditionId": r.get("condition_id"),
        "size": r.get("current_size"),
        "avgPrice": r.get("avg_price"),
        "curPrice": r.get("current_price"),
        "currentValue": r.get("current_value"),
        "realizedPnl": r.get("realized_pnl") or 0,
        "cashPnl": r.get("unrealized_pnl") or 0,
        "title": r.get("title"),
        "outcome": r.get("outcome"),
        "outcomeIndex": r.get("outcome_index"),
        "status": r.get("status"),
        "endDate": r.get("end_date"),
    }


def _num(x) -> str:
    try:
        return f"{float(x):.6f}"
    except (TypeError, ValueError):
        return ""


def activity_id_for(r: dict) -> str:
    """Ключ операции. Числа приводятся к одному виду: v1 и v2 пишут их по-разному ("100" и 100.0)."""
    key = "|".join(
        [
            str(_first(r, "transactionHash", "transaction_hash") or "").lower(),
            str(r.get("type") or "").upper(),
            str(_first(r, "conditionId", "condition_id") or ""),
            str(_first(r, "asset", "token_id") or ""),
            str(_first(r, "outcomeIndex", "outcome_index") or ""),
            _num(r.get("size")),
            _num(_first(r, "usdcSize", "usdc_size")),
            str(int(_num(r.get("timestamp")) and float(r.get("timestamp")) or 0)),
            str(_first(r, "proxyWallet", "proxy_wallet") or "").lower(),
        ]
    )
    return hashlib.sha1(key.encode()).hexdigest()


def parse_activity(r: dict) -> dict:
    r = v1_activity(r)
    return {
        "activity_id": activity_id_for(r),
        "wallet": str(r.get("proxyWallet") or "").lower(),
        "ts": int(r.get("timestamp") or 0),
        "type": str(r.get("type") or "").upper(),
        "market_id": r.get("conditionId") or None,
        "token_id": str(r["asset"]) if r.get("asset") else None,
        "outcome_index": r.get("outcomeIndex"),
        "size": float(r.get("size") or 0),
        "usdc_size": float(r.get("usdcSize") or 0),
        "price": float(r.get("price") or 0),
        "tx_hash": r.get("transactionHash"),
    }


class ClobClient:
    def __init__(self, endpoints: Endpoints | None = None, http: _Http | None = None):
        self.http = http or _Http((endpoints or Endpoints()).clob, rps=80)  # /prices-history: 1000 за 10 с

    def book(self, token_id: str):
        return parse_book(self.http.get("/book", {"token_id": token_id}))

    def prices_history(self, token_id: str, start: int | None = None, end: int | None = None, fidelity_min: int = 1):
        params = {"market": token_id, "fidelity": fidelity_min}
        if start:
            params["startTs"] = int(start)
        if end:
            params["endTs"] = int(end)
        if not start and not end:
            params["interval"] = "max"
        return parse_price_history(self.http.get("/prices-history", params))
