"""Советник модуля «События» (этап D): что купить, по какой цене и сколько.

По каждому исходу — обе стороны: «Да» (вероятность выигрыша = оценка модели p) и «Нет» (1 − p).
  * цена акции с комиссией: c = ask + ставка·ask·(1 − ask) — платит тейкер, мы покупаем из стакана;
  * выгода на акцию: p − c (в долларах на акцию с выплатой $1);
  * порог выгоды растёт с неопределённостью: 2 п.п. + половина полуширины 80% интервала;
  * и со сроком: годовая доходность (p / c)^(1/лет) − 1 не меньше 15% — копейка за год не стоит денег;
  * предельная цена заявки — самая высокая цена, при которой оба условия ещё выполняются;
  * сумма — ¼ Келли с комиссией: f = (p − c)/(1 − c) от банка, затем лимиты модуля и общие, затем проход по стакану
    только по уровням не дороже предельной цены (что не набралось — не покупаем дороже).
Если у исхода нет независимых оценок (итог = цена рынка), совета нет: обыграть рынок нечем.
Всё «на момент сейчас»: оценка — последний анализ, цены — текущий стакан.
"""

from __future__ import annotations

import math
import time
from typing import Any

from .. import fees
from ..signals.orderbook import walk

DAY = 86400
MIN_EDGE = 0.02  # минимум выгоды на акцию после комиссии (2 п.п.)
UNC_K = 0.5  # + доля полуширины интервала оценки
MIN_APY = 0.15  # минимум годовой доходности
KELLY_FRAC = 0.25  # ¼ Келли
MIN_BET_USD = 1.0
MIN_YEARS = 1 / 365  # событие «сегодня» считаем как день


def event_fee_rate(db: Any, ev: dict) -> float:
    """Ставка комиссии тейкера: из нашей таблицы рынков, иначе — по теме и тегам события."""
    rows = ev.get("outcomes") or []
    m = db.one("SELECT * FROM markets WHERE market_id=?", (rows[0]["market_id"],)) if rows else None
    if m and m.get("fee_rate") is not None:
        return float(m["fee_rate"])
    return fees.rate_for(ev.get("topic"), ev.get("tag_slugs") or [])


def spent_today(db: Any, now: float) -> float:
    """Сколько уже потрачено на ставки по событиям сегодня (UTC) — лимит модуля."""
    day0 = int(now) - int(now) % DAY
    return float(
        db.scalar(
            "SELECT SUM(f.size_shares*f.price) AS s FROM fills f JOIN orders o ON o.order_id=f.order_id "
            "WHERE f.side='BUY' AND f.ts>=? AND o.signal_id LIKE 'ev:%'",
            (day0,),
            0.0,
        )
        or 0.0
    )


def cost(price: float, rate: float) -> float:
    return price + fees.fee_per_share(rate, price)


def required_edge(half_width: float) -> float:
    return MIN_EDGE + UNC_K * max(0.0, half_width)


def limit_price(p: float, rate: float, need: float, years: float) -> float | None:
    """Самая высокая цена акции, при которой выгода ≥ порога и годовая доходность ≥ MIN_APY (с комиссией)."""
    growth = (1 + MIN_APY) ** max(years, MIN_YEARS)

    def ok(x: float) -> bool:
        c = cost(x, rate)
        return p - c >= need and p / c >= growth

    lo, hi = 0.001, 0.999
    if not ok(lo):
        return None
    for _ in range(40):  # c(x) растёт с x — делим пополам
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    return math.floor(lo * 1000 + 1e-9) / 1000  # вниз до шага цены 0,1¢ — округление вверх нарушило бы порог


def kelly(p: float, c: float) -> float:
    """Доля банка по Келли для акции с выплатой $1, купленной за c (с комиссией) при вероятности p."""
    if c >= 1 or p <= c:
        return 0.0
    return (p - c) / (1 - c)


def advise_side(
    *,
    p: float,
    asks: list[tuple[float, float]],
    rate: float,
    half_width: float,
    years: float,
    bank: float,
    cap_usd: float,
) -> dict:
    """Совет по одной стороне исхода. cap_usd — сколько можно поставить по лимитам (на одну ставку и остаток дня)."""
    need = required_edge(half_width)
    best = asks[0][0] if asks else None
    out: dict[str, Any] = {
        "p": round(p, 4),
        "ask": best,
        "need": round(need, 4),
        "limit_price": limit_price(p, rate, need, years),
        "size_usd": 0.0,
        "shares": 0.0,
    }
    if best is None:
        return {**out, "verdict": "no_book", "reason": "Нет заявок на продажу"}
    c = cost(best, rate)
    out["cost"] = round(c, 4)
    out["edge"] = round(p - c, 4)
    out["apy"] = round((p / c) ** (1 / max(years, MIN_YEARS)) - 1, 3) if c > 0 and p > 0 else None
    lim = out["limit_price"]
    if lim is None or best > lim + 1e-9:
        if p - c <= 0:
            why = "Цена выше оценки модели — выгоды нет"
        elif p - c < need:
            why = f"Выгода {_pp(p - c)} меньше порога {_pp(need)} (с учётом неопределённости оценки)"
        else:
            why = f"Слишком долго ждать: доходность {round((out['apy'] or 0) * 100)}% годовых меньше {round(MIN_APY * 100)}%"
        return {**out, "verdict": "no", "reason": why}
    f = KELLY_FRAC * kelly(p, c)
    want = f * bank
    out["kelly_usd"] = round(want, 2)
    size = min(want, cap_usd)
    if cap_usd < MIN_BET_USD:
        return {**out, "verdict": "limit", "reason": "Лимит ставок по событиям на сегодня исчерпан"}
    if size < MIN_BET_USD:
        return {**out, "verdict": "small", "reason": f"Выгода есть, но ставка по Келли меньше ${MIN_BET_USD:.0f}"}
    fill = walk([(px, sz) for px, sz in asks if px <= lim + 1e-9], usd=size)
    if fill.shares <= 0:
        return {**out, "verdict": "no", "reason": "В стакане нет акций по выгодной цене"}
    fee = fees.fee_usd(rate, fill.shares, fill.avg_price)
    usd = fill.filled_usd
    return {
        **out,
        "verdict": "buy",
        "reason": "",
        "size_usd": round(usd, 2),
        "shares": round(fill.shares, 2),
        "avg_price": round(fill.avg_price, 4),
        "fee_usd": round(fee, 2),
        "partial": not fill.complete,  # в стакане по выгодной цене меньше, чем советует Келли
        "capped": size < want - 0.01,  # урезано лимитами
        # сценарии: выиграл (акция = $1), проиграл (акция = $0), в среднем по оценке модели
        "win_usd": round(fill.shares - usd - fee, 2),
        "lose_usd": round(-usd - fee, 2),
        "ev_usd": round(fill.shares * p - usd - fee, 2),
    }


def _pp(x: float) -> str:
    return f"{x * 100:.1f} п.п.".replace(".", ",", 1)


def advise(
    analysis: dict,
    ev: dict,
    quotes: dict[str, dict],
    rate: float,
    bank: float,
    cap_usd: float,
    now: float | None = None,
) -> dict:
    """Совет по всем открытым исходам события. quotes: токен → {asks: [(цена, акции)], …}."""
    now = now or time.time()
    years = max(((ev.get("end_ts") or now) - now) / (365 * DAY), MIN_YEARS)
    rows = {r["key"]: r for r in ev.get("outcomes") or []}
    items = []
    for a in analysis.get("outcomes") or []:
        r = rows.get(a["key"])
        if r is None or r.get("closed"):
            continue
        hw = (a["hi"] - a["lo"]) / 2
        no_info = a.get("model") is None
        for side, tok, label, p in (
            ("yes", r["yes_token"], r.get("yes_label") or "Да", a["final"]),
            ("no", r.get("no_token"), r.get("no_label") or "Нет", 1 - a["final"]),
        ):
            if not tok:
                continue
            if side == "no" and r.get("single") and (r.get("yes_label") or "Да") != "Да":
                continue  # рынок «команда А / команда Б»: «Нет» у А — это «Да» у Б, он уже есть отдельной строкой
            asks = [(float(px), float(sz)) for px, sz in (quotes.get(tok) or {}).get("asks") or []]
            base = {"key": a["key"], "title": r["title"], "side": side, "label": label, "token_id": tok}
            if no_info:
                items.append(
                    {
                        **base,
                        "p": round(p, 4),
                        "ask": asks[0][0] if asks else None,
                        "verdict": "no_info",
                        "reason": "Нет независимых оценок — обыграть цену нечем",
                        "size_usd": 0.0,
                        "shares": 0.0,
                    }
                )
                continue
            items.append(
                {
                    **base,
                    **advise_side(p=p, asks=asks, rate=rate, half_width=hw, years=years, bank=bank, cap_usd=cap_usd),
                }
            )
    buys = sorted((x for x in items if x["verdict"] == "buy"), key=lambda x: -x["ev_usd"])
    near = sorted(
        (x for x in items if x.get("edge") is not None and x["verdict"] != "buy"),
        key=lambda x: -(x["edge"] - x["need"]),
    )
    return {
        "items": items,
        "best": buys[0] if buys else None,
        "buys": buys,
        "closest": near[0] if near and not buys else None,  # ближе всего к выгоде — «что должно измениться»
        "fee_rate": rate,
        "years": round(years, 4),
        "params": {"min_edge": MIN_EDGE, "unc_k": UNC_K, "min_apy": MIN_APY, "kelly_frac": KELLY_FRAC},
        "cap_usd": round(cap_usd, 2),
    }


# ---------------------------------------------------------------- анализ события целиком (страница и «Следить»)
MAX_BOOK_OUTCOMES = 20  # стаканы «Да» и «Нет» — для 20 самых вероятных исходов (остальные — цена Gamma, без совета)


def market_quotes(cat: Any, ev: dict) -> tuple[dict[str, float], dict[str, dict]]:
    """Цены исходов как на Polymarket (середина спроса и предложения; при спреде шире 10¢ — последняя цена)
    и стаканы «Да»/«Нет» для совета."""
    open_rows = [r for r in ev["outcomes"] if not r["closed"]]
    top = sorted(open_rows, key=lambda r: -(r.get("price") or 0))[:MAX_BOOK_OUTCOMES]
    toks = [t for r in top for t in (r["yes_token"], r.get("no_token")) if t]
    qs: dict[str, dict] = {}
    for i in range(0, len(toks), 24):
        try:
            qs.update(cat.quotes(toks[i : i + 24]))
        except Exception:  # стакан не ответил — совет по этим исходам будет «нет заявок»
            pass
    market: dict[str, float] = {}
    for r in open_rows:
        q = qs.get(r["yes_token"]) or {}
        bid, ask = q.get("bid"), q.get("ask")
        if bid is not None and ask is not None and ask - bid <= 0.10:
            market[r["key"]] = (bid + ask) / 2
        elif r.get("price") is not None:
            market[r["key"]] = float(r["price"])
    return market, qs


def evaluate(
    db: Any, cat: Any, ev: dict, mode: str, clients: dict, ai_limit: int, start_jobs: bool = True, sync: bool = False
) -> dict:
    """Досье (собрать в фоне, если нет или устарело) → ИИ (в режиме «с ИИ», в фоне, дневной лимит) → модель.
    Возвращает анализ модели + стаканы + состояние фоновых задач."""
    from . import ai, model
    from . import dossier as dz

    eid = str(ev["id"])
    open_rows = [r for r in ev["outcomes"] if not r["closed"]]
    ev_open = {**ev, "outcomes": open_rows}
    market, qs = market_quotes(cat, ev)
    dj = dz.job_state(eid)
    cur = dz.load(db, eid)
    if (
        start_jobs
        and not (dj and dj["state"] == "running")
        and (cur["collected_ts"] is None or time.time() - cur["collected_ts"] > dz.STALE_S)
    ):
        if sync:  # фоновая проверка «Следить»: досье нужно сейчас, она и так в фоне
            dz.collect(db, ev, clients)
            dj = {"state": "done"}
        else:
            dj = dz.start(db, ev, clients)
    job = None
    if mode == "ai":
        d = dz.load(db, eid)
        if d["collected_ts"] is not None and ai.cached(db, eid, d["collected_ts"]) is None:
            job = ai.job_state(eid)
            retry = bool(job and job["state"] == "error" and time.time() - float(job.get("finished") or 0) > 600)
            if start_jobs and (not job or job["state"] == "done" or retry):
                job = ai.start(db, dz.event_info(ev_open), d["items"], d["collected_ts"], ai_limit)
        elif d["collected_ts"] is None:
            job = {"state": "wait_dossier"}
    res = model.analyze(db, ev_open, mode, market)
    return {"analysis": res, "quotes": qs, "ai_job": job, "dossier_job": (dj or {}).get("state")}


def caps(db: Any, risk: Any, now: float) -> dict:
    """Сколько можно поставить на событие сейчас: лимит модуля на ставку, общий лимит на ставку, доля банка,
    остаток дневного лимита модуля; общие стопы, паузы и общий дневной лимит — через RiskManager."""
    s = risk.settings()
    bank = float(s["bankroll_usd"])
    per_bet = min(float(s["events_max_bet_usd"]), float(s["max_bet_usd"]), risk.p.max_bet_frac * bank)
    left = max(0.0, float(s["events_daily_limit_usd"]) - spent_today(db, now))
    cap = min(per_bet, left)
    rr = risk.cap(max(cap, MIN_BET_USD), market={}, entity_id="", now=int(now))
    return {
        "bank": bank,
        "per_bet": round(per_bet, 2),
        "left_today": round(left, 2),
        "cap": 0.0 if rr.blocked else round(min(cap, rr.size_usd), 2),
        "paused": list(rr.reasons) if rr.blocked else [],
    }
