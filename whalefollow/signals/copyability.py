"""Фильтр копируемости (ТЗ раздел 8). Любое «нет» — пропуск с записанной причиной."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import Params
from .orderbook import best, depth_usd, walk

DAY = 86400


@dataclass
class CopyCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def check(*, signal: dict, asks: list, market: dict, score: dict, cat_score: dict | None,
          entity_positions: list[dict], median_cost_cat: float | None, our_size_usd: float,
          params: Params, now: int, categories_on: list[str] | None = None,
          categories_off_for_entity: list[str] | None = None) -> CopyCheck:
    r: list[str] = []
    d: dict = {}
    cat = market.get("category") or "other"

    # 8.8 рынок
    if market.get("status") != "open":
        r.append("market_not_open")
    if (market.get("liquidity_usd") or 0) < params.min_liquidity_usd:
        r.append("low_liquidity")
    # категория: допущена по истории кита и включена пользователем
    if not cat_score or cat_score.get("status") != "approved":
        r.append("category_not_proven")
    if categories_on is not None and cat not in categories_on:
        r.append("category_off_by_user")
    if categories_off_for_entity and cat in categories_off_for_entity:
        r.append("category_off_for_whale")

    # 8.1 задержка
    delay = now - int(signal["ts"])
    d["delay_s"] = delay
    if delay > params.max_delay_s:
        r.append("too_late")

    # 8.2 дрейф цены
    ask_now = best(asks)
    d["ask_now"] = ask_now
    e_cat = (cat_score or {}).get("edge_shrunk") or score.get("edge_shrunk") or 0.0
    d["edge_cat"] = e_cat
    if ask_now is None:
        r.append("no_book")
    else:
        drift = ask_now - float(signal["whale_price"])
        d["drift"] = drift
        if drift > max(params.drift_abs, params.drift_edge_frac * e_cat):
            r.append("price_moved")

        # 8.3 глубина и проскальзывание
        fill = walk(asks, usd=our_size_usd)
        depth = depth_usd(asks, params.depth_band)
        d.update(vwap=fill.avg_price, depth_usd=depth, fill_complete=fill.complete)
        if our_size_usd > params.depth_frac * depth or not fill.complete:
            r.append("thin_book")
        elif fill.avg_price - float(signal["whale_price"]) > max(params.drift_abs, params.drift_edge_frac * e_cat):
            r.append("slippage")

    # 8.4 тип игрока
    hold = score.get("hold_share")
    med_h = score.get("median_hold_h")
    d.update(hold_share=hold, median_hold_h=med_h)
    if hold is None or not np.isfinite(hold) or hold < params.min_hold_share \
            or (med_h is not None and np.isfinite(med_h) and med_h < params.min_median_hold_h):
        r.append("scalper")

    # 8.5 срок до разрешения (годовая доходность проверяется в решении, когда известен edge)
    end_ts = market.get("end_ts")
    days_left = (end_ts - now) / DAY if end_ts else float("nan")
    d["days_left"] = days_left
    if not np.isfinite(days_left) or days_left < params.min_days_to_resolution:
        r.append("too_soon")
    elif days_left > params.max_days_to_resolution:
        r.append("too_long")

    # 8.6 конфликты: у кита противоположная позиция в том же рынке
    other = [p for p in entity_positions if p["market_id"] == signal["market_id"]
             and p["token_id"] != signal["token_id"] and (p.get("remaining_shares") or 0) > 0]
    if other:
        r.append("whale_hedged")

    # 8.7 убеждённость
    conviction = float(signal["whale_size_usd"]) / median_cost_cat if median_cost_cat else 1.0
    d["conviction"] = conviction
    if conviction < params.conviction_min:
        r.append("low_conviction")

    return CopyCheck(not r, r, d)
