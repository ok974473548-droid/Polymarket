"""Комиссии Polymarket (docs.polymarket.com/trading/fees, сверено 17.11.2026).

Платит только тейкер (тот, кто забирает заявку из стакана): fee = акции × ставка × p × (1 − p).
Комиссия максимальна при цене 0,5 и стремится к нулю у 0 и 1. Мы копируем китов
рыночными покупками, то есть всегда тейкеры.

Ставки по темам (тейкер): крипто 0,07; спорт, экономика, культура, погода, прочее 0,05;
политика, финансы, технологии, упоминания 0,04; геополитика 0.
Если Polymarket изменит ставки — достаточно поправить таблицу TAKER_FEE_RATE.
"""
from __future__ import annotations

TAKER_FEE_RATE = {
    "crypto": 0.07, "sports": 0.05, "economics": 0.05, "culture": 0.05, "weather": 0.05, "other": 0.05,
    "politics": 0.04, "finance": 0.04, "tech": 0.04, "mentions": 0.04, "geopolitics": 0.0,
}
DEFAULT_RATE = 0.05
FEE_FREE_TAGS = {"geopolitics", "world", "world-events", "geopolitical"}


def rate_for(category: str | None, tags: list[str] | None = None) -> float:
    """Ставка комиссии для рынка по теме; теги «геополитика/мировые события» — без комиссии."""
    if tags and any(t in FEE_FREE_TAGS for t in tags):
        return 0.0
    return TAKER_FEE_RATE.get(category or "other", DEFAULT_RATE)


def market_rate(market: dict | None) -> float:
    if market and market.get("fee_rate") is not None:
        return float(market["fee_rate"])
    return rate_for((market or {}).get("category"))


def fee_per_share(rate: float, price: float) -> float:
    return rate * price * (1.0 - price)


def fee_usd(rate: float, shares: float, price: float) -> float:
    return round(shares * fee_per_share(rate, price), 5)
