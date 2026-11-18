"""Решение по сигналу (ТЗ 10.1–10.3): комбинация оценок, остаточный edge, дробный Келли."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from ..config import Params


@dataclass
class Decision:
    decision: str  # BUY | SKIP
    size_usd: float = 0.0
    q_whale: float | None = None
    q_llm: float | None = None
    q_final: float | None = None
    q_eff: float | None = None
    edge_net: float | None = None
    kelly_full: float | None = None
    kelly_frac: float | None = None
    annual_return: float | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def kelly(q: float, p: float) -> float:
    """f* = (q − p)/(1 − p) для покупки акции по цене p при вероятности q."""
    if p >= 1 or q <= p:
        return 0.0
    return (q - p) / (1 - p)


def decide(
    *,
    ask_now: float,
    vwap: float,
    edge_cat: float,
    q_llm: float | None,
    llm_trusted: bool,
    fee_per_share: float,
    bankroll: float,
    conviction: float,
    days_left: float,
    params: Params,
    whale_price: float | None = None,
) -> Decision:
    # Оценка «по киту»: его цена входа плюс его обычное преимущество в этой теме. Если цена после его
    # сделки выросла, рост — это уже частично отыгранная его информация: прибавлять преимущество к новой
    # цене значило бы посчитать его дважды. Если цена упала — берём текущую (осторожнее).
    base = ask_now if whale_price is None else min(float(whale_price), ask_now)
    q_whale = float(np.clip(base + edge_cat, 0.01, 0.99))
    d = Decision("SKIP", q_whale=q_whale, q_llm=q_llm)
    if q_llm is None:
        if params.require_llm:
            d.reasons.append("no_estimate")
            return d
        q_final = q_whale
    else:
        if abs(q_whale - q_llm) > params.agree_max_diff:
            d.reasons.append("disagree")  # кит и независимая оценка расходятся — пропуск, а не усреднение
            return d
        w = params.w_llm_trusted if llm_trusted else params.w_llm_untrusted
        q_final = (1 - w) * q_whale + w * q_llm
    d.q_final = q_final
    d.q_eff = q_final - params.uncertainty_haircut
    d.edge_net = d.q_eff - vwap - fee_per_share
    if d.edge_net < params.edge_threshold:
        d.reasons.append("small_edge")
        return d
    if days_left and np.isfinite(days_left) and days_left > 0:
        d.annual_return = d.edge_net / vwap * 365 / days_left
        if d.annual_return < params.min_annual_return:
            d.reasons.append("low_annual_return")
            return d
    d.kelly_full = kelly(d.q_eff, min(vwap + fee_per_share, 0.999))  # цена акции вместе с комиссией
    d.kelly_frac = params.kelly_fraction * d.kelly_full
    lo, hi = params.conviction_clamp
    d.size_usd = round(d.kelly_frac * bankroll * float(np.clip(conviction, lo, hi)), 2)
    if d.size_usd <= 0:
        d.reasons.append("small_edge")
        return d
    d.decision = "BUY"
    return d
