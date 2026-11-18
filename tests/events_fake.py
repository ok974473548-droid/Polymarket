"""Поддельные события Polymarket для тестов и демо-сервера: живые рынки синтетического мира, сложенные
в события Gamma (многоисходное и «Да/Нет»), и история цен по периодам — без сети."""

from __future__ import annotations

import math

from synthetic import FakeClob, FakeGamma, SyntheticWorld

NAMES = ["Анна Смирнова", "Борис Ким", "Вера Ли", "Глеб Орлов"]


def make_events(w: SyntheticWorld) -> list[dict]:
    multi = []
    for name, price in zip(NAMES, (0.41, 0.27, 0.18, 0.09), strict=True):
        m = w.add_live_market(category="politics", pi=price, price=price, life_days=30)
        raw = w.gamma_market(m)
        raw.update(
            {
                "groupItemTitle": name,
                "question": f"Победит ли {name}?",
                "active": True,
                "bestBid": round(price - 0.005, 3),
                "bestAsk": round(price + 0.005, 3),
                "oneDayPriceChange": 0.012 if price > 0.2 else -0.004,
                "image": None,
            }
        )
        raw.pop("events", None)
        multi.append(raw)
    single_m = w.add_live_market(category="economics", pi=0.62, price=0.62, life_days=20)
    single = w.gamma_market(single_m)
    single.update({"active": True, "bestBid": 0.615, "bestAsk": 0.625, "oneDayPriceChange": 0.03})
    single.pop("events", None)
    end = multi[0]["endDate"]
    return [
        {
            "id": "9001",
            "slug": "demo-election",
            "title": "Кто победит на выборах мэра?",
            "description": "Рынок разрешится по официальным итогам выборов.",
            "resolutionSource": "https://example.org/results",
            "negRisk": True,
            "volume": 1_250_000.0,
            "volume24hr": 84_000.0,
            "liquidity": 120_000.0,
            "endDate": end,
            "startDate": "2026-10-01T00:00:00Z",
            "tags": [{"slug": "politics", "label": "Politics"}, {"slug": "elections", "label": "Elections"}],
            "markets": multi,
        },
        {
            "id": "9002",
            "slug": "demo-rate",
            "title": "Снизит ли центробанк ставку в декабре?",
            "description": "Да — если ставка будет снижена на заседании в декабре.",
            "negRisk": False,
            "volume": 430_000.0,
            "volume24hr": 12_000.0,
            "liquidity": 60_000.0,
            "endDate": single["endDate"],
            "tags": [{"slug": "economy", "label": "Economy"}],
            "markets": [single],
        },
    ]


class EventsGamma(FakeGamma):
    def __init__(self, world: SyntheticWorld, events: list[dict]):
        super().__init__(world)
        self.evs = events
        self.calls = 0

    def events(self, **params) -> list[dict]:
        self.calls += 1
        tag = params.get("tag_slug")
        out = [e for e in self.evs if not tag or any(t["slug"] == tag for t in e["tags"])]
        off = int(params.get("offset") or 0)
        return out[off : off + int(params.get("limit") or 25)]

    def event(self, event_id: str) -> dict | None:
        self.calls += 1
        return next((e for e in self.evs if e["id"] == str(event_id)), None)

    def search_events(self, q: str, limit: int = 20, status: str = "active") -> list[dict]:
        ql = q.lower()
        return [e for e in self.evs if ql in e["title"].lower()][:limit]


class EventsClob(FakeClob):
    def prices_interval(self, token_id: str, interval: str, fidelity_min: int) -> list[tuple[int, float]]:
        span = {"1h": 3600, "6h": 6 * 3600, "1d": 86400, "1w": 7 * 86400, "1m": 30 * 86400, "max": 60 * 86400}[interval]
        now = self.w.now
        b = self.book(token_id)
        last = (b[0][0][0] + b[1][0][0]) / 2 if b and b[0] and b[1] else 0.5
        n = min(200, max(10, span // (fidelity_min * 60)))
        seed = sum(map(ord, token_id[-6:]))
        return [
            (
                int(now - span + span * k / n),
                round(max(0.01, min(0.99, last + 0.06 * math.sin((k + seed) / 9) * (1 - k / n))), 4),
            )
            for k in range(n + 1)
        ]
