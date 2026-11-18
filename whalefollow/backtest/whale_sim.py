"""«Что было бы, если бы копировали этого кита» — симуляция по данным одного кита.

Честные допущения (как в общем бэктесте, раздел 11.4):
  * копируется первая покупка кита в каждом исходе за окно (повторные докупки — нет);
  * наша цена входа — не лучше цены кита: max(цена кита, цена рынка через задержку) + штраф
    за проскальзывание; история цен почасовая, поэтому это оценка, а не точная цена стакана;
  * если цена успела уйти дальше порога (как в живой проверке) — ставка пропускается;
  * сумма — как в настройках: min(доля банка, максимум на ставку), дневной лимит соблюдается;
  * комиссия тейкера Polymarket при покупке и при продаже;
  * выход: кит полностью вышел до закрытия рынка — продаём следом (через задержку, со штрафом);
    иначе — выплата при закрытии; не закрытые — по последней цене (помечаются как открытые).
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Callable

from .. import fees
from ..config import Params
from ..db import DB

DAY = 86400


class _Prices:
    def __init__(self, db: DB, tokens: list[str]):
        self.idx: dict[str, tuple[list[int], list[float]]] = {}
        for i in range(0, len(tokens), 500):
            chunk = tokens[i : i + 500]
            ph = ",".join("?" for _ in chunk)
            rows = db.query(
                f"SELECT token_id, ts, price FROM price_history WHERE token_id IN ({ph}) ORDER BY ts", chunk
            )
            tmp: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
            for r in rows:
                tmp[r["token_id"]][0].append(r["ts"])
                tmp[r["token_id"]][1].append(r["price"])
            self.idx.update(tmp)

    def at(self, token: str, ts: int, tol: int = 6 * 3600) -> float | None:
        s = self.idx.get(token)
        if not s:
            return None
        i = bisect.bisect_right(s[0], ts) - 1
        if i < 0 or ts - s[0][i] > tol:
            return None
        return float(s[1][i])

    def last(self, token: str) -> float | None:
        s = self.idx.get(token)
        return float(s[1][-1]) if s and s[1] else None


def simulate_whale(
    db: DB,
    params: Params,
    wallets: list[str],
    settings: dict,
    now: int,
    days: int = 90,
    delay_s: int | None = None,
    slippage: float = 0.01,
    start_ts: int | None = None,
    end_ts: int | None = None,
    accept: Callable[[dict], bool] | None = None,
    entity_of: dict[str, str] | None = None,
) -> dict:
    """Повтор ставок кита с задержкой и комиссиями. По умолчанию — ставки за последние days дней;
    со start_ts/end_ts — ставки, открытые в этот отрезок (итог — как они закрылись на самом деле).

    Бэктест стратегии (плагина): wallets — кошельки нескольких китов, entity_of — кошелёк → кит,
    accept(сделка) — берёт ли стратегия эту покупку (вызывается по порядку времени, видит только прошлое).
    По каждому исходу копируется первая покупка, которую стратегия взяла; выход — когда ЭТОТ кит вышел."""
    if not wallets:
        return {"ok": False, "error": "Нет кошельков"}
    start = int(start_ts) if start_ts else now - days * DAY
    end = int(end_ts) if end_ts else now
    days = max(1, round((end - start) / DAY))
    delay = int(delay_s if delay_s is not None else params.max_delay_s)
    ph = ",".join("?" for _ in wallets)
    trades = db.query(
        "SELECT t.ts, t.wallet, t.token_id, t.market_id, t.side, t.price, t.size_shares, t.size_usd, m.category, m.question, "
        "m.fee_rate, tk.payout, tk.outcome_label, m.resolved_ts FROM trades t "
        "LEFT JOIN markets m ON m.market_id=t.market_id LEFT JOIN tokens tk ON tk.token_id=t.token_id "
        f"WHERE t.wallet IN ({ph}) AND (t.source IS NULL OR t.source='') AND t.ts>=? ORDER BY t.ts",
        list(wallets) + [start - 365 * DAY],
    )
    if not trades:
        return {"ok": True, "bets": [], "summary": _empty(days, delay)}
    # чистая позиция кита по токену во времени — чтобы понять, когда он вышел полностью
    ent = entity_of or {}
    net: dict[tuple[str, str], float] = defaultdict(float)  # (кит, исход) → чистая позиция
    exit_ts: dict[str, int] = {}
    exit_px_whale: dict[str, float] = {}  # цена, по которой кит закрыл позицию (последняя продажа)
    first_buy: dict[str, dict] = {}
    rejected: set[tuple[str, str]] = set()
    for t in trades:
        tok = t["token_id"]
        e = ent.get(t["wallet"], "")
        sh = float(t["size_shares"] or 0)
        if t["side"] == "BUY":
            net[(e, tok)] += sh
            if start <= t["ts"] <= end and tok not in first_buy and (e, tok) not in rejected:
                if accept is None or accept(t):
                    first_buy[tok] = {**t, "entity": e}
                else:
                    rejected.add((e, tok))  # стратегия не взяла первую покупку кита — его докупки не пересматриваем
        else:
            net[(e, tok)] -= sh
            fb = first_buy.get(tok)
            if fb is not None and fb["entity"] == e and net[(e, tok)] <= 1e-6 and tok not in exit_ts:
                exit_ts[tok] = t["ts"]
                exit_px_whale[tok] = float(t["price"])
    prices = _Prices(db, list(first_buy))
    bank = float(settings.get("bankroll_usd", 1000))
    stake = min(params.max_bet_frac * bank, float(settings.get("max_bet_usd", 40)))
    day_limit = float(settings.get("daily_limit_usd", 1e12))
    cats_on = set(settings.get("categories") or [])
    spent_day: dict[int, float] = defaultdict(float)
    bets, skipped = [], defaultdict(int)
    if accept is not None:
        skipped["strategy"] = len({tok for _, tok in rejected if tok not in first_buy})
    for tok, t in sorted(first_buy.items(), key=lambda kv: kv[1]["ts"]):
        cat = t["category"] or "other"
        if cats_on and cat not in cats_on:
            skipped["topic_off"] += 1
            continue
        whale_px = float(t["price"])
        px = prices.at(tok, t["ts"] + delay)
        entry = min(max(px if px is not None else whale_px, whale_px) + slippage, 0.999)
        if entry - whale_px > max(params.drift_abs, 0.02) + slippage:
            skipped["price_moved"] += 1
            continue
        if entry >= 0.97:
            skipped["no_upside"] += 1
            continue
        day = int(t["ts"] // DAY)
        if spent_day[day] + stake > day_limit:
            skipped["day_limit"] += 1
            continue
        spent_day[day] += stake
        rate = float(t["fee_rate"]) if t["fee_rate"] is not None else fees.rate_for(cat)
        shares = stake / entry
        fee = fees.fee_usd(rate, shares, entry)
        status, value, exit_px, closed_ts = "open", None, None, None
        x_ts = exit_ts.get(tok)
        resolved = t["payout"] is not None and (t["resolved_ts"] is None or x_ts is None or t["resolved_ts"] <= x_ts)
        if resolved:
            status, exit_px = "resolved", float(t["payout"])
            value = shares * exit_px
            closed_ts = t["resolved_ts"] or x_ts or t["ts"]
        elif x_ts is not None and x_ts <= now:
            # продаём следом за китом: цена через задержку, а если истории цен нет — цена его продажи
            p = prices.at(tok, x_ts + delay)
            if p is None:
                p = exit_px_whale.get(tok)
            if p is not None:
                exit_px = max(p - slippage, 0.0)
                status = "sold"
                value = shares * exit_px - fees.fee_usd(rate, shares, exit_px)
                closed_ts = x_ts
        if value is None:
            last = prices.last(tok)
            value = shares * (last if last is not None else entry)
            exit_px = last
        pnl = value - stake - fee
        # кит на ту же сумму: его вход и ЕГО выход (продажа без нашей задержки или выплата рынка)
        w_exit = exit_px_whale.get(tok, exit_px) if status == "sold" else exit_px
        whale_pnl = (stake / whale_px) * (w_exit if w_exit is not None else whale_px) - stake
        bets.append(
            {
                "ts": t["ts"],
                "question": t["question"] or "Рынок загружается",
                "side": t["outcome_label"],
                "category": cat,
                "entity_id": t["entity"] or None,
                "whale_cents": round(whale_px * 100, 1),
                "entry_cents": round(entry * 100, 1),
                "exit_cents": round(exit_px * 100, 1) if exit_px is not None else None,
                "status": status,
                "stake_usd": round(stake, 2),
                "fee_usd": round(fee, 2),
                "pnl_usd": round(pnl, 2),
                "whale_pnl_usd": round(whale_pnl, 2),
                "estimated_price": px is None,
                "closed_ts": closed_ts,
            }
        )
    out = _summary(bets, skipped, days, delay, stake, now, start)
    out["start_ts"], out["end_ts"] = start, end
    return {"ok": True, "bets": bets[::-1][:30], "summary": out}


def _empty(days: int, delay: int) -> dict:
    return {
        "days": days,
        "delay_s": delay,
        "n": 0,
        "closed": 0,
        "pnl_usd": 0.0,
        "roi_pct": None,
        "win_pct": None,
        "invested_usd": 0.0,
        "fees_usd": 0.0,
        "max_drawdown_usd": 0.0,
        "whale_roi_pct": None,
        "skipped": {},
        "curve": [],
        "open": 0,
        "verdict": "no_data",
        "text": "За этот период кит не делал ставок, которые можно было бы повторить.",
    }


def _summary(
    bets: list[dict], skipped: dict, days: int, delay: int, stake: float, now: int, start: int | None = None
) -> dict:
    if not bets:
        out = _empty(days, delay)
        out["skipped"] = dict(skipped)
        return out
    closed = [b for b in bets if b["status"] != "open"]
    invested = sum(b["stake_usd"] for b in bets)
    pnl = sum(b["pnl_usd"] for b in bets)
    closed_inv = sum(b["stake_usd"] for b in closed)
    closed_pnl = sum(b["pnl_usd"] for b in closed)
    whale_closed = sum(b["whale_pnl_usd"] for b in closed)
    # кривая: прибыль закрытых ставок нарастающим итогом — по дате, когда ставка закрылась (тогда деньги
    # и появляются), от нуля в начале периода до сегодняшнего итога; последняя точка = итог закрытых ставок
    t0 = start if start is not None else now - days * DAY
    t0 = min([t0] + [b["ts"] for b in bets])
    step = 7 * DAY if now - t0 > 21 * DAY else DAY
    n = max(1, -(-(now - t0) // step))
    curve, peak, dd = [{"ts": t0, "pnl_usd": 0.0}], 0.0, 0.0
    for k in range(1, n + 1):
        hi = min(t0 + k * step, now)
        acc = sum(b["pnl_usd"] for b in closed if (b.get("closed_ts") or b["ts"]) <= hi)
        peak = max(peak, acc)
        dd = max(dd, peak - acc)
        curve.append({"ts": hi, "pnl_usd": round(acc, 2)})
    roi = 100 * closed_pnl / closed_inv if closed_inv else None
    whale_roi = 100 * whale_closed / closed_inv if closed_inv else None
    if len(closed) < 10:
        verdict, text = (
            "few",
            f"Закрытых ставок пока {len(closed)} — для вывода мало, смотрите на результат как на ориентир.",
        )
    elif roi is not None and roi > 0:
        verdict, text = "good", "Копирование этого кита за период было бы прибыльным даже с задержкой и комиссиями."
    else:
        verdict, text = "bad", "С задержкой и комиссиями копирование этого кита за период было бы убыточным."
    if whale_roi is not None and roi is not None and whale_roi - roi > 10:
        text += " Большая часть его прибыли приходится на цену входа, которую повторить не успеть."
    return {
        "days": days,
        "delay_s": delay,
        "stake_usd": round(stake, 2),
        "n": len(bets),
        "closed": len(closed),
        "open": len(bets) - len(closed),
        "invested_usd": round(invested, 2),
        "pnl_usd": round(pnl, 2),
        "closed_pnl_usd": round(closed_pnl, 2),
        "roi_pct": round(roi, 1) if roi is not None else None,
        "whale_roi_pct": round(whale_roi, 1) if whale_roi is not None else None,
        "win_pct": round(100 * sum(1 for b in closed if b["pnl_usd"] > 0) / len(closed), 1) if closed else None,
        "fees_usd": round(sum(b["fee_usd"] for b in bets), 2),
        "max_drawdown_usd": round(dd, 2),
        "estimated_share": round(sum(1 for b in bets if b["estimated_price"]) / len(bets), 2),
        "skipped": dict(skipped),
        "curve": curve,
        "verdict": verdict,
        "text": text,
    }
