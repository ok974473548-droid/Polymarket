"""Работа со стаканом: исполнение объёма проходом по уровням (VWAP), глубина."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    avg_price: float
    shares: float
    filled_usd: float
    worst_price: float
    complete: bool


def walk(levels: list[tuple[float, float]], usd: float | None = None, shares: float | None = None) -> Fill:
    """Проходит уровни (asks — для покупки, bids — для продажи), пока не наберёт usd или shares."""
    got_sh, got_usd, worst = 0.0, 0.0, float("nan")
    for price, size in levels:
        if price <= 0 or size <= 1e-9:  # пустые и «пыльные» уровни (ничтожный объём) не берём
            continue
        if usd is not None:
            need = usd - got_usd
            if need <= 1e-9:
                break
            take_sh = min(size, need / price)
        else:
            need = shares - got_sh
            if need <= 1e-9:
                break
            take_sh = min(size, need)
        got_sh += take_sh
        got_usd += take_sh * price
        worst = price
    target_done = (usd is not None and got_usd >= usd - 1e-6) or (shares is not None and got_sh >= shares - 1e-6)
    avg = got_usd / got_sh if got_sh > 0 else float("nan")
    return Fill(avg, got_sh, got_usd, worst, bool(target_done))


def best(levels) -> float | None:
    return levels[0][0] if levels else None


def depth_usd(levels, band: float) -> float:
    """Объём (USD) в пределах band от лучшей цены."""
    if not levels:
        return 0.0
    top = levels[0][0]
    return sum(p * s for p, s in levels if abs(p - top) <= band + 1e-12)
