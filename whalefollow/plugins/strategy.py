"""Стратегии в работе агента и в бэктесте.

gate(...) — живой фильтр: включённые стратегии работают как «или» — сигнал создаётся, если его взяла
хотя бы одна. Ошибка стратегии = «не взяла» (и видна на экране «Плагины»).
backtest(...) — та же функция accept на истории ваших китов: повтор с задержкой, проскальзыванием,
комиссиями и лимитами (как «что было бы» в профиле кита), без заглядывания вперёд.
"""

from __future__ import annotations

from typing import Any

from .base import Candidate, StrategyPlugin, Verdict, tracked_map
from .registry import record, registry


def gate(db: Any, sig: dict, market: dict, settings: dict) -> dict:
    from . import context

    ctx = context(db, settings)
    active = [p for p in registry().active("strategy", ctx) if isinstance(p, StrategyPlugin)]
    if not active:
        return {"ok": False, "by": [], "verdicts": {"": "все стратегии выключены (экран «Плагины»)"}}
    c = Candidate(
        db=db,
        ts=int(sig["ts"]),
        entity_id=sig["entity_id"],
        wallet=sig.get("wallet") or "",
        token_id=sig["token_id"],
        market_id=sig.get("market_id") or "",
        price=float(sig["whale_price"]),
        size_usd=float(sig.get("whale_size_usd") or 0),
        category=market.get("category") or "other",
    )
    verdicts: dict[str, str] = {}
    by: list[str] = []
    for p in active:
        try:
            v = p.accept(c)
            if not isinstance(v, Verdict):
                v = Verdict(bool(v))
            record(db, p.name, ok=True)
        except Exception as e:
            record(db, p.name, ok=False, error=f"{type(e).__name__}: {e}")
            v = Verdict(False, f"ошибка стратегии: {e}")
        verdicts[p.name] = v.reason
        if v.ok:
            by.append(p.name)
    return {"ok": bool(by), "by": by, "verdicts": verdicts}


def backtest(
    db: Any, params: Any, name: str, days: int = 90, now: int | None = None, settings: dict | None = None
) -> dict:
    """Бэктест одной стратегии на китах из списка (кандидаты — киты на копировании, как в живой работе)."""
    from ..backtest.whale_sim import simulate_whale
    from ..db import now_ts
    from . import user_settings

    p = registry().get(name)
    if not isinstance(p, StrategyPlugin):
        return {"ok": False, "error": "Нет такой стратегии"}
    now = int(now or now_ts())
    settings = settings if settings is not None else user_settings(db)
    copy_map = tracked_map(db, ("copy",))
    if not copy_map:
        return {"ok": False, "error": "Нет китов на копировании — бэктесту нечего повторять"}
    all_map = tracked_map(db)
    errors: list[str] = []

    def accept(t: dict) -> bool:
        c = Candidate(
            db=db,
            ts=int(t["ts"]),
            entity_id=copy_map.get(t["wallet"], t["wallet"]),
            wallet=t["wallet"],
            token_id=t["token_id"],
            market_id=t["market_id"] or "",
            price=float(t["price"]),
            size_usd=float(t["size_usd"] or 0),
            category=t["category"] or "other",
            tracked=all_map,
        )
        try:
            v = p.accept(c)
            return bool(v.ok if isinstance(v, Verdict) else v)
        except Exception as e:
            if len(errors) < 3:
                errors.append(str(e))
            return False

    res = simulate_whale(db, params, list(copy_map), settings, now, days=days, accept=accept, entity_of=copy_map)
    if not res.get("ok"):
        return res
    sm = res["summary"]
    n, roi = sm.get("closed", 0), sm.get("roi_pct")
    if sm.get("n", 0) == 0:
        text = "Стратегия не взяла бы ни одной ставки ваших китов за этот период."
    elif n < 10:
        text = f"Закрытых ставок {n} — для вывода мало, это лишь ориентир."
    elif roi is not None and roi > 0:
        text = "За период стратегия была бы в плюсе с задержкой, проскальзыванием и комиссиями."
    else:
        text = "За период стратегия была бы в минусе с задержкой, проскальзыванием и комиссиями."
    sm["text"] = text
    sm["whales"] = len(set(copy_map.values()))
    out = {"ok": True, "name": name, "title": p.title, "summary": sm, "bets": res["bets"]}
    if errors:
        out["errors"] = errors
    return out
