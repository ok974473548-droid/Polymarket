"""Игроки и рынки по готовым данным Polymarket — без загрузки и пересчёта истории сделок.

Что Polymarket уже считает сам и мы берём как есть:
  • прибыль игрока нарастающим итогом (/v2/user-pnl) и за всё время (/v2/user-stats);
  • объём, число сделок, рынков, крупнейший выигрыш, дата регистрации;
  • стоимость портфеля (/v2/value), место в рейтинге (/v2/leaderboard?user=);
  • позиции с ценой входа и прибылью (/v2/positions), держатели рынка (/v2/holders).
Из этого строятся карточки в интерфейсе: «быстрый взгляд» на игрока за 1–2 с, форма по неделям,
панель рынка. Собственные расчёты (мастерство или удача, копируемость с задержкой) остаются в scoring.

Ответы кэшируются в памяти на несколько минут: повторное открытие карточки — мгновенно.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import UTC, datetime

from .util import pmap

log = logging.getLogger(__name__)
DAY = 86400
ADDR = re.compile(r"0x[a-f0-9]{40}", re.I)


class TTLCache:
    def __init__(self, ttl: float):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        return None

    def put(self, key, value):
        with self._lock:
            if len(self._d) > 500:
                self._d.clear()
            self._d[key] = (time.time(), value)
        return value


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def point_pnl(p: dict) -> float:
    """Прибыль в точке ряда: экономическая (с открытыми позициями), иначе реализованная + нереализованная."""
    if p.get("economic_pnl") is not None:
        return float(p["economic_pnl"])
    return float(p.get("realized_pnl") or 0) + float(p.get("unrealized_pnl") or 0)


def form_weeks(series: list[dict], weeks: int = 26, now: int | None = None) -> list[dict]:
    """Результат по неделям из кривой прибыли: сколько игрок заработал или потерял за каждую неделю."""
    if len(series) < 2:
        return []
    now = now or series[-1]["ts"]
    start = now - weeks * 7 * DAY
    out, j, last = [], 0, None
    pts = sorted(series, key=lambda p: p["ts"])
    for k in range(weeks + 1):  # значение кривой на границе каждой недели
        edge = start + k * 7 * DAY
        while j < len(pts) and pts[j]["ts"] <= edge:
            last = pts[j]["pnl"]
            j += 1
        out.append(last)
    res = []
    for k in range(weeks):
        a, b = out[k], out[k + 1]
        if a is None or b is None:
            continue
        res.append({"ts": start + k * 7 * DAY, "pnl": round(b - a, 2)})
    return res


def form_summary(weeks: list[dict]) -> dict:
    """Серии: сколько недель подряд в плюсе сейчас, лучшая серия, доля плюсовых недель."""
    if not weeks:
        return {"weeks": 0}
    signs = [w["pnl"] > 0 for w in weeks if abs(w["pnl"]) > 0.5]
    cur = 0
    for s in reversed(signs):
        if not s:
            break
        cur += 1
    best = run = 0
    for s in signs:
        run = run + 1 if s else 0
        best = max(best, run)
    last4 = sum(w["pnl"] for w in weeks[-4:])
    return {
        "weeks": len(weeks),
        "green_share": round(sum(signs) / len(signs), 2) if signs else None,
        "streak_now": cur,
        "streak_best": best,
        "last4_pnl": round(last4, 2),
        "state": "hot" if cur >= 3 and last4 > 0 else ("cold" if last4 < 0 else "neutral"),
    }


def max_drawdown(series: list[dict]) -> float:
    peak, dd = None, 0.0
    for p in series:
        peak = p["pnl"] if peak is None else max(peak, p["pnl"])
        dd = max(dd, peak - p["pnl"])
    return round(dd, 2)


def _iso_ts(s) -> int | None:
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=UTC).timestamp())
    except ValueError:
        return None


def position_card(r: dict) -> dict:
    """Позиция игрока (поля /v2/positions) → строка для интерфейса."""
    size = _f(r.get("current_size")) or 0.0
    return {
        "title": r.get("title"),
        "outcome": r.get("outcome"),
        "token_id": str(r.get("token_id") or ""),
        "market_id": r.get("condition_id"),
        "icon": r.get("icon"),
        "url": f"https://polymarket.com/event/{r['event_slug']}" if r.get("event_slug") else None,
        "shares": round(size, 2),
        "avg_cents": round((_f(r.get("avg_price")) or 0) * 100, 1),
        "now_cents": round((_f(r.get("current_price")) or 0) * 100, 1),
        "value_usd": round(_f(r.get("current_value")) or 0, 2),
        "cost_usd": round(_f(r.get("total_cost_usdc")) or _f(r.get("entry_cost_usdc")) or 0, 2),
        "pnl_usd": round(_f(r.get("total_pnl")) or 0, 2),
        "pnl_pct": _f(r.get("percent_pnl")),
        "realized_usd": round(_f(r.get("realized_pnl")) or 0, 2),
        "end_ts": _iso_ts(r.get("end_date")),
        "first_ts": r.get("first_entry_at"),
        "last_ts": r.get("last_event_at"),
        "status": r.get("status"),
    }


class Players:
    """Готовые данные Polymarket по игрокам и рынкам, с кэшем."""

    def __init__(self, data, gamma=None, clob=None):
        self.data, self.gamma, self.clob = data, gamma, clob
        self.cards = TTLCache(300)
        self.markets = TTLCache(90)
        self.search = TTLCache(120)

    # ------------------------------------------------------------ поиск игрока
    def resolve(self, text: str) -> list[dict]:
        """Ссылка на профиль, адрес или имя → список игроков (кошелёк, имя, аватар)."""
        text = (text or "").strip()
        if not text:
            return []
        m = ADDR.search(text)
        if m:
            w = m.group(0).lower()
            prof = None
            if self.gamma is not None and hasattr(self.gamma, "public_profile"):
                try:
                    prof = self.gamma.public_profile(w)
                except Exception:
                    prof = None
            return [
                {
                    "wallet": w,
                    "name": (prof or {}).get("name") or (prof or {}).get("pseudonym"),
                    "image": (prof or {}).get("profileImage"),
                    "exact": True,
                }
            ]
        name = text
        u = re.search(r"polymarket\.com/(?:@|profile/@?)([^/?#\s]+)", text)
        if u:
            name = u.group(1)
        name = name.lstrip("@").strip()
        if len(name) < 2 or self.gamma is None or not hasattr(self.gamma, "search_profiles"):
            return []
        hit = self.search.get(name.lower())
        if hit is not None:
            return hit
        res = self.gamma.search_profiles(name)
        exact = [r for r in res if (r.get("name") or "").lower() == name.lower()]
        out = [{**r, "exact": r in exact} for r in (exact + [r for r in res if r not in exact])]
        return self.search.put(name.lower(), out)

    # ------------------------------------------------------------ карточка игрока
    def card(self, wallet: str, with_positions: bool = True, refresh: bool = False) -> dict:
        wallet = wallet.lower()
        key = (wallet, with_positions)
        if not refresh:
            hit = self.cards.get(key) or (self.cards.get((wallet, True)) if not with_positions else None)
            if hit is not None:
                return hit
        d, g = self.data, self.gamma
        now = int(time.time())
        jobs = {
            "profile": (lambda: g.public_profile(wallet)) if g is not None and hasattr(g, "public_profile") else None,
            "stats": lambda: d.user_stats(wallet),
            "pnl": lambda: d.user_pnl(wallet, "all", "1d"),
            "value": lambda: d.value(wallet),
            "vol30": lambda: d.user_volume(wallet, now - 30 * DAY, now),
            "rank_all": lambda: d.standing(wallet, "all"),
            "rank_month": lambda: d.standing(wallet, "month"),
            "open": (lambda: d.positions_page(wallet, status="OPEN", sort_by="CURRENT_VALUE", limit=12))
            if with_positions
            else None,
        }
        jobs = {k: v for k, v in jobs.items() if v is not None}
        got, errors = {}, []
        for k, res, exc in pmap(lambda kv: kv[1](), list(jobs.items()), workers=len(jobs)):
            if exc is not None:
                errors.append(k[0])
                log.info("карточка %s: %s — %s", wallet[:10], k[0], exc)
            else:
                got[k[0]] = res
        if not got.get("stats") and not got.get("pnl") and len(errors) >= len(jobs) - 1:
            return {"ok": False, "wallet": wallet, "error": "Polymarket не ответил — попробуйте позже"}
        prof = got.get("profile") or {}
        stats = got.get("stats") or {}
        ra, rm = got.get("rank_all") or {}, got.get("rank_month") or {}
        series = [
            {"ts": int(p["timestamp"]), "pnl": round(point_pnl(p), 2)}
            for p in (got.get("pnl") or [])
            if p.get("timestamp") is not None
        ]
        series.sort(key=lambda p: p["ts"])
        allp = stats.get("all_time_pnl") or {}
        pnl_all = point_pnl(allp) if allp else (series[-1]["pnl"] if series else _f(ra.get("pnl")))

        def delta(days):
            if not series:
                return None
            cut = now - days * DAY
            base = next((p["pnl"] for p in reversed(series) if p["ts"] <= cut), series[0]["pnl"])
            return round(series[-1]["pnl"] - base, 2)

        weeks = form_weeks(series, 26, now)
        v30 = got.get("vol30") or {}
        name = prof.get("name") or ra.get("user_name") or prof.get("pseudonym")
        out = {
            "ok": True,
            "wallet": wallet,
            "name": name,
            "pseudonym": prof.get("pseudonym"),
            "image": prof.get("profileImage") or ra.get("profile_image"),
            "x": prof.get("xUsername") or ra.get("x_username"),
            "verified": bool(prof.get("verifiedBadge") or ra.get("verified")),
            "bio": prof.get("bio"),
            "profile_url": f"https://polymarket.com/profile/{wallet}",
            "joined_ts": stats.get("join_date") or _iso_ts(prof.get("createdAt")),
            "pnl_all_usd": round(pnl_all, 2) if pnl_all is not None else None,
            "pnl_30d_usd": delta(30),
            "pnl_7d_usd": delta(7),
            "pnl_90d_usd": delta(90),
            "markets": stats.get("trades"),
            "biggest_win_usd": _f(stats.get("biggest_win")),
            "trades": allp.get("trade_count"),
            "volume_usd": _f(allp.get("volume_usdc")),
            "volume_30d_usd": _f(v30.get("volume_usdc")),
            "trades_30d": v30.get("trade_count"),
            "value_usd": _f(got.get("value")),
            "rank_pnl": ra.get("rank_pnl") or None,
            "rank_volume": ra.get("rank_volume") or None,
            "rank_pnl_month": rm.get("rank_pnl") or None,
            "series": _thin(series, 300),
            "max_drawdown_usd": max_drawdown(series),
            "weeks": weeks,
            "form": form_summary(weeks),
            "partial": errors,
        }
        if with_positions:
            out["positions"] = [position_card(r) for r in (got.get("open") or [])]
        return self.cards.put(key, out)

    # ------------------------------------------------------------ панель рынка
    def market(self, market_id: str, token_id: str | None = None, wallets: list[str] | None = None) -> dict:
        """Рынок по готовым данным: правила, объём, ликвидность, цены исходов, держатели, позиции китов."""
        key = (market_id, token_id, tuple(sorted(wallets or [])))
        hit = self.markets.get(key)
        if hit is not None:
            return hit
        jobs = {}
        if self.gamma is not None and hasattr(self.gamma, "market"):
            jobs["raw"] = lambda: self.gamma.market(market_id)
        if hasattr(self.data, "holders"):
            jobs["holders"] = lambda: self.data.holders(market_id, limit=10, include_pnl=True)
        for w in (wallets or [])[:4]:
            if hasattr(self.data, "positions_page"):
                jobs["w:" + w] = lambda w=w: self.data.positions_page(w, condition=market_id, status="OPEN", limit=5)
        got = {}
        for (k, _), res, exc in pmap(lambda kv: kv[1](), list(jobs.items()), workers=max(1, len(jobs))):
            if exc is None:
                got[k] = res
            else:
                log.info("рынок %s: %s — %s", market_id[:10], k, exc)
        raw = got.get("raw") or {}
        outcomes = [str(o) for o in _jl(raw.get("outcomes"))]
        prices = [_f(p) for p in _jl(raw.get("outcomePrices"))]
        toks = [str(t) for t in _jl(raw.get("clobTokenIds"))]
        ev = (raw.get("events") or [{}])[0] if raw.get("events") else {}
        mine = {w.lower() for w in (wallets or [])}
        holders = []
        for tok, hs in (got.get("holders") or {}).items():
            label = outcomes[toks.index(tok)] if tok in toks and toks.index(tok) < len(outcomes) else None
            for h in hs:
                w = str(h.get("proxy_wallet") or "").lower()
                holders.append(
                    {
                        "wallet": w,
                        "name": h.get("name") or h.get("pseudonym"),
                        "image": h.get("profile_image"),
                        "token_id": tok,
                        "outcome": label,
                        "shares": round(_f(h.get("amount")) or 0, 1),
                        "avg_cents": round((_f(h.get("avg_price")) or 0) * 100, 1)
                        if h.get("avg_price") is not None
                        else None,
                        "pnl_usd": round(_f(h.get("total_pnl")) or 0, 2) if h.get("total_pnl") is not None else None,
                        "value_usd": round(_f(h.get("current_value")) or 0, 2)
                        if h.get("current_value") is not None
                        else None,
                        "ours": w in mine,
                        "same_side": token_id is not None and tok == str(token_id),
                    }
                )
        holders.sort(key=lambda h: -(h["value_usd"] or h["shares"] or 0))
        whale_pos = []
        for k, rows in got.items():
            if k.startswith("w:"):
                for r in rows or []:
                    whale_pos.append({**position_card(r), "wallet": k[2:]})
        out = {
            "ok": bool(raw) or bool(holders),
            "market_id": market_id,
            "question": raw.get("question"),
            "rules": raw.get("description"),
            "image": raw.get("image") or raw.get("icon"),
            "end_ts": _iso_ts(raw.get("endDate")),
            "start_ts": _iso_ts(raw.get("startDate")),
            "volume_usd": _f(raw.get("volumeNum") or raw.get("volume")),
            "volume_24h_usd": _f(raw.get("volume24hr")),
            "liquidity_usd": _f(raw.get("liquidityNum") or raw.get("liquidity")),
            "spread": _f(raw.get("spread")),
            "best_bid": _f(raw.get("bestBid")),
            "best_ask": _f(raw.get("bestAsk")),
            "change_1d": _f(raw.get("oneDayPriceChange")),
            "event_title": ev.get("title"),
            "url": f"https://polymarket.com/event/{ev['slug']}"
            if ev.get("slug")
            else (f"https://polymarket.com/market/{raw['slug']}" if raw.get("slug") else None),
            "outcomes": [
                {
                    "label": o,
                    "price": prices[i] if i < len(prices) else None,
                    "token_id": toks[i] if i < len(toks) else None,
                    "ours": token_id is not None and i < len(toks) and toks[i] == str(token_id),
                }
                for i, o in enumerate(outcomes)
            ],
            "holders": holders[:16],
            "whale_positions": whale_pos,
        }
        return self.markets.put(key, out)


def _jl(v) -> list:
    import json

    if isinstance(v, list):
        return v
    try:
        return json.loads(v) if v else []
    except (TypeError, ValueError):
        return []


def _thin(series: list[dict], n: int) -> list[dict]:
    if len(series) <= n:
        return series
    step = len(series) / n
    out = [series[int(i * step)] for i in range(n)]
    if out[-1] is not series[-1]:
        out.append(series[-1])
    return out
