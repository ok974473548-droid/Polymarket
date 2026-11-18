"""Синтетический рынок предсказаний — только для автотестов (в программу не входит).

Создаёт события, рынки, траектории цен и игроков с заранее известным поведением:
  skilled   — знают истинную вероятность точнее рынка (настоящее преимущество);
  random    — ставят случайно по рыночной цене (преимущества нет);
  grinder   — покупают почти решённые рынки по 0.9+ (favorite_grinder);
  maker     — много мелких сделок в обе стороны, быстрые выходы (maker_arb);
  sybil     — один умный владелец на трёх кошельках с общим источником денег;
  insider   — новые кошельки, крупная ставка прямо перед скачком цены.

Фейковые клиенты (FakeGamma, FakeData, FakeClob) отдают данные в форматах
настоящих API Polymarket, поэтому через них проходит тот же код загрузки.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

DAY = 86400
CATS = ["politics", "sports", "economics", "tech", "crypto"]
EXCHANGE = "0xexchange00000000000000000000000000000"   # общий биржевой кошелёк — в allowlist


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Market:
    market_id: str
    event_id: str
    category: str
    question: str
    t_open: int
    t_resolve: int
    pi: float
    y: int
    yes_token: str
    no_token: str
    times: np.ndarray = field(repr=False, default=None)
    yes_prices: np.ndarray = field(repr=False, default=None)
    jump_at: int | None = None
    volume: float = 0.0

    def price(self, token: str, ts: int) -> float:
        i = int(np.searchsorted(self.times, ts, side="right") - 1)
        i = min(max(i, 0), len(self.times) - 1)
        p = float(self.yes_prices[i])
        return p if token == self.yes_token else 1.0 - p


class SyntheticWorld:
    def __init__(self, seed: int = 1, n_events: int = 500, days: int = 400, n_skilled: int = 5,
                 n_random: int = 40, now: int = 1_790_000_000, step_h: int = 3, skill_noise: float = 0.05,
                 price_noise: float = 0.13, bets_per_wallet: tuple = (90, 140)):
        self.rng = np.random.default_rng(seed)
        self.now = now
        self.t0 = now - days * DAY
        self.step = step_h * 3600
        self.markets: dict[str, Market] = {}
        self.token_market: dict[str, str] = {}
        self.trades: list[dict] = []
        self.transfers: list[dict] = []
        self.roles: dict[str, str] = {}
        self.first_seen: dict[str, int] = {}
        self.skill_noise = skill_noise
        self.price_noise = price_noise
        self.bets_range = bets_per_wallet
        self._make_markets(n_events)
        self._make_wallets(n_skilled, n_random)

    # ------------------------------------------------------------ рынки
    def _make_markets(self, n_events: int) -> None:
        r = self.rng
        mid = 0
        for ev in range(n_events):
            cat = CATS[ev % len(CATS)]
            n_m = 2 if r.random() < 0.15 else 1
            t_open = int(self.t0 + r.uniform(0, (self.now - self.t0) - 2 * DAY))
            for _ in range(n_m):
                life = int(r.uniform(4, 45) * DAY)
                t_res = t_open + life
                pi = float(np.clip(r.beta(2, 2), 0.03, 0.97))
                y = int(r.random() < pi)
                m = Market(f"0xm{mid:05d}", f"ev{ev:05d}", cat, f"Synthetic question #{mid}?",
                           t_open, t_res, pi, y, f"t{mid:05d}y", f"t{mid:05d}n")
                self._make_path(m)
                self.markets[m.market_id] = m
                self.token_market[m.yes_token] = m.market_id
                self.token_market[m.no_token] = m.market_id
                mid += 1

    def _make_path(self, m: Market, jump_at: int | None = None) -> None:
        r = self.rng
        end = min(m.t_resolve, self.now)
        times = np.arange(m.t_open, end + 1, self.step, dtype=np.int64)
        if len(times) == 0:
            times = np.array([m.t_open], dtype=np.int64)
        p0 = float(np.clip(m.pi + r.normal(0, self.price_noise), 0.03, 0.97))
        frac = (times - m.t_open) / max(m.t_resolve - m.t_open, 1)
        # сначала рынок постепенно узнаёт истинную вероятность π (на этом зарабатывают «умные»,
        # отсюда их положительный CLV), ближе к концу цена сходится к исходу y
        y_end = m.y if m.t_resolve <= self.now else m.pi
        path = (p0 + (m.pi - p0) * (1 - np.exp(-4 * frac)) + (y_end - m.pi) * frac ** 3
                + r.normal(0, 0.01, len(times)))
        if jump_at is not None:
            pre = times < jump_at
            path = np.where(pre, p0 + r.normal(0, 0.005, len(times)), y_end * 0.9 + 0.05)
        m.times = times
        m.yes_prices = np.clip(path, 0.01, 0.99)
        m.jump_at = jump_at

    def resolved(self, m: Market) -> bool:
        return m.t_resolve <= self.now

    # ------------------------------------------------------------ игроки
    ROLE_PREFIX = {"skilled": "5c", "random": "ad", "grinder": "9d", "maker": "3a", "sybil": "5b", "insider": "1d"}

    def _addr(self, kind: str, i: int) -> str:
        """Адрес в формате 0x + 40 hex-символов; префикс выдаёт роль (для тестов)."""
        return f"0x{self.ROLE_PREFIX[kind]}{i:038d}"

    def _trade(self, wallet: str, token: str, side: str, price: float, size_usd: float, ts: int) -> None:
        price = float(np.clip(price, 0.01, 0.99))
        shares = size_usd / price
        self.trades.append({
            "proxy_wallet": wallet, "side": side, "token_id": token,
            "condition_id": self.token_market[token], "size": round(shares, 4), "price": round(price, 4),
            "timestamp": int(ts), "outcome": "Yes" if token.endswith("y") else "No",
            "transaction_hash": f"0x{len(self.trades):064x}", "title": "",
        })
        m = self.markets[self.token_market[token]]
        m.volume += size_usd
        self.first_seen[wallet] = min(self.first_seen.get(wallet, ts), ts)

    def _fund(self, wallet: str, funder: str, ts: int, amount: float = 5000.0) -> None:
        self.transfers.append({"transfer_id": f"x{len(self.transfers)}", "ts": int(ts),
                               "from_wallet": funder, "to_wallet": wallet, "amount": amount})

    def _bet(self, wallet: str, m: Market, token: str, ts: int, size: float, hold_prob: float) -> None:
        p = m.price(token, ts)
        self._trade(wallet, token, "BUY", p, size, ts)
        if self.rng.random() > hold_prob:
            t_sell = int(ts + self.rng.uniform(0.2, 0.8) * (m.t_resolve - ts))
            if t_sell < self.now:
                p_s = m.price(token, t_sell)
                shares = size / max(p, 0.01)
                self._trade(wallet, token, "SELL", p_s, shares * p_s, t_sell)

    def _eligible(self, latest_open: int | None = None) -> list[Market]:
        latest_open = latest_open or self.now
        return [m for m in self.markets.values() if m.t_open < latest_open and m.jump_at is None]

    def _make_wallets(self, n_skilled: int, n_random: int) -> None:
        r = self.rng
        exchange = EXCHANGE
        mk = list(self.markets.values())
        # рынки «инсайдеров» готовим заранее: цена стоит, потом резко прыгает к исходу
        insider_markets = mk[-2:]
        for m in insider_markets:
            m.t_resolve = min(m.t_resolve, self.now - DAY)
            if m.t_resolve - 2 * DAY <= m.t_open:
                m.t_open = m.t_resolve - 5 * DAY
            self._make_path(m, jump_at=m.t_resolve - 2 * DAY)
        mk = [m for m in mk if m.jump_at is None]
        # умные: знают π с шумом skill_noise, ставят при расхождении с ценой
        for i in range(n_skilled):
            w = self._addr("skilled", i)
            self.roles[w] = "skilled"
            self._fund(w, exchange, self.t0)
            for m in r.permutation(mk)[: int(r.integers(*self.bets_range)) * 2]:
                ts = int(m.t_open + r.uniform(0.0, 0.12) * (m.t_resolve - m.t_open))
                if ts >= self.now or m.jump_at:
                    continue
                belief = m.pi + r.normal(0, self.skill_noise)
                p = m.price(m.yes_token, ts)
                if belief - p > 0.06:
                    self._bet(w, m, m.yes_token, ts, float(r.lognormal(7.6, 0.4)), 0.85)
                elif p - belief > 0.06:
                    self._bet(w, m, m.no_token, ts, float(r.lognormal(7.6, 0.4)), 0.85)
        # случайные
        for i in range(n_random):
            w = self._addr("random", i)
            self.roles[w] = "random"
            self._fund(w, exchange, self.t0)
            for m in r.permutation(mk)[: int(r.integers(*self.bets_range))]:
                ts = int(m.t_open + r.uniform(0.02, 0.6) * (m.t_resolve - m.t_open))
                if ts >= self.now:
                    continue
                tok = m.yes_token if r.random() < 0.5 else m.no_token
                self._bet(w, m, tok, ts, float(r.lognormal(7.4, 0.5)), 0.85)
        # «гриндеры» фаворитов
        for i in range(2):
            w = self._addr("grinder", i)
            self.roles[w] = "grinder"
            self._fund(w, exchange, self.t0)
            for m in r.permutation(mk)[:160]:
                ts = int(m.t_resolve - r.uniform(0.3, 1.5) * DAY)
                if ts >= self.now or ts <= m.t_open:
                    continue
                win_tok = m.yes_token if m.price(m.yes_token, ts) >= 0.5 else m.no_token
                if m.price(win_tok, ts) >= 0.9:
                    self._bet(w, m, win_tok, ts, float(r.lognormal(8.0, 0.3)), 1.0)
        # маркет-мейкеры
        for i in range(2):
            w = self._addr("maker", i)
            self.roles[w] = "maker"
            self._fund(w, exchange, self.t0)
            for m in r.permutation(mk)[:70]:
                base = int(m.t_open + r.uniform(0.05, 0.5) * (m.t_resolve - m.t_open))
                for k in range(24):
                    ts = base + k * 600
                    if ts >= self.now:
                        break
                    for tok in (m.yes_token, m.no_token):
                        p = m.price(tok, ts)
                        size = float(r.uniform(50, 200))
                        self._trade(w, tok, "BUY", p - 0.005, size, ts)
                        self._trade(w, tok, "SELL", p + 0.005, size, ts + 120)
        # сибил: один умный владелец, три кошелька, общий источник, синхронные сделки
        owner_funder = "0xownerfunder0000000000000000000000000000"
        sybils = [self._addr("sybil", i) for i in range(3)]
        for w in sybils:
            self.roles[w] = "sybil"
            self._fund(w, owner_funder, self.t0 + DAY)
        for m in r.permutation(mk)[:260]:
            ts = int(m.t_open + r.uniform(0.0, 0.12) * (m.t_resolve - m.t_open))
            if ts >= self.now:
                continue
            belief = m.pi + r.normal(0, self.skill_noise)
            p = m.price(m.yes_token, ts)
            tok = m.yes_token if belief - p > 0.06 else (m.no_token if p - belief > 0.06 else None)
            if tok:
                for j, w in enumerate(sybils):
                    self._bet(w, m, tok, ts + j * 15, 700.0, 0.9)
        # «инсайдеры»: свежий кошелёк, крупная ставка перед скачком цены
        for i, m in enumerate(insider_markets):
            w = self._addr("insider", i)
            self.roles[w] = "insider"
            tok = m.yes_token if m.y == 1 else m.no_token
            ts = m.jump_at - 3600
            self._fund(w, f"0xfresh{i:034d}", ts - 3 * DAY)
            self.first_seen[w] = ts - 3 * DAY
            self._trade(w, tok, "BUY", m.price(tok, ts), 40_000.0, ts)

    # ------------------------------------------------------------ живые события для агента
    def add_live_market(self, category: str = "economics", pi: float = 0.7, price: float = 0.55,
                        life_days: float = 14) -> Market:
        mid = len(self.markets)
        m = Market(f"0xm{mid:05d}", f"ev_live{mid}", category, f"Live question #{mid}?",
                   self.now - DAY, int(self.now + life_days * DAY), pi, 1, f"t{mid:05d}y", f"t{mid:05d}n")
        m.times = np.array([m.t_open, self.now], dtype=np.int64)
        m.yes_prices = np.array([price, price])
        self.markets[m.market_id] = m
        self.token_market[m.yes_token] = m.market_id
        self.token_market[m.no_token] = m.market_id
        m.volume = 200_000.0
        return m

    def whale_trade_now(self, wallet: str, m: Market, token: str, size_usd: float, side: str = "BUY") -> None:
        self._trade(wallet, token, side, m.price(token, self.now), size_usd, self.now)

    def set_price(self, m: Market, yes_price: float, ts: int | None = None) -> None:
        ts = ts or self.now
        m.times = np.append(m.times, ts)
        m.yes_prices = np.append(m.yes_prices, yes_price)

    # ------------------------------------------------------------ форматы API
    def gamma_market(self, m: Market) -> dict:
        res = self.resolved(m)
        prices = [str(m.y), str(1 - m.y)] if res else [f"{m.price(m.yes_token, self.now):.3f}",
                                                         f"{1 - m.price(m.yes_token, self.now):.3f}"]
        return {
            "conditionId": m.market_id, "question": m.question, "category": m.category,
            "clobTokenIds": json.dumps([m.yes_token, m.no_token]), "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(prices), "endDate": _iso(m.t_resolve), "closed": res,
            "closedTime": _iso(m.t_resolve) if res else None, "volumeNum": m.volume,
            "liquidityNum": max(20_000.0, m.volume / 5), "events": [{"id": m.event_id, "title": m.event_id}],
            "description": "Resolves YES if the synthetic event happens.",
        }

    def price_points(self, token: str, start: int | None = None, end: int | None = None):
        if token not in self.token_market:      # токен не из этого мира (например, остался в базе от прошлого демо)
            return []
        m = self.markets[self.token_market[token]]
        start, end = start or 0, min(end or self.now, self.now)
        sel = (m.times >= start) & (m.times <= end)
        ps = m.yes_prices[sel] if token == m.yes_token else 1 - m.yes_prices[sel]
        return [(int(t), float(p)) for t, p in zip(m.times[sel], ps)]

    def book(self, token: str, depth_usd: float = 3000.0):
        if token not in self.token_market:
            return [], []
        p = self.markets[self.token_market[token]].price(token, self.now)
        asks = [(round(min(p + 0.005 + 0.01 * k, 0.99), 3), depth_usd / max(p, 0.01)) for k in range(6)]
        bids = [(round(max(p - 0.005 - 0.01 * k, 0.01), 3), depth_usd / max(p, 0.01)) for k in range(6)]
        return bids, asks


class FakeGamma:
    def __init__(self, world: SyntheticWorld):
        self.w = world

    def markets(self, closed=None, **_):
        for m in self.w.markets.values():
            if m.t_open > self.w.now:
                continue
            if closed is not None and self.w.resolved(m) != closed:
                continue
            yield self.w.gamma_market(m)

    def market(self, condition_id: str):
        m = self.w.markets.get(condition_id)
        return self.w.gamma_market(m) if m else None


class FakeData:
    def __init__(self, world: SyntheticWorld):
        self.w = world

    def trades(self, user=None, start=None, end=None, condition=None, side=None, limit=1000, max_pages=None, **_):
        end = min(end or self.w.now, self.w.now)
        rows = self.w.trades if user else sorted(self.w.trades, key=lambda t: -t["timestamp"])  # лента: новые первыми
        if not user and max_pages:
            rows = [t for t in rows if t["timestamp"] <= end][: limit * max_pages]
        for t in rows:
            if user and t["proxy_wallet"] != user.lower():
                continue
            if start and t["timestamp"] < start:
                continue
            if t["timestamp"] > end:
                continue
            if condition and t["condition_id"] != condition:
                continue
            yield t


class FakeClob:
    def __init__(self, world: SyntheticWorld):
        self.w = world

    def book(self, token_id: str):
        return self.w.book(token_id)

    def prices_history(self, token_id, start=None, end=None, fidelity_min: int = 1):
        return self.w.price_points(token_id, start, end)


def load_world(db, world: SyntheticWorld) -> None:
    """Загружает синтетический мир в БД через тот же пайплайн, что и реальные данные."""
    from whalefollow.ingest import pipeline
    pipeline.sync_markets(db, FakeGamma(world))
    pipeline.store_trades(db, world.trades)
    tokens = [t for m in world.markets.values() for t in (m.yes_token, m.no_token)]
    pipeline.sync_prices(db, FakeClob(world), tokens)
    db.insert_ignore("transfers", world.transfers)
    for w, ts in world.first_seen.items():
        db.execute("UPDATE wallets SET first_seen_ts=? WHERE wallet=?", (ts, w))
