"""Согласованность связанных исходов (точность модели, F3).

Связанные исходы одного события обязаны соблюдать порядок:
  * лестница порогов «вверх» (↑, reach, hit, above, over, O/U — «больше», at least, N+): чем выше порог, тем
    вероятность не больше; «вниз» (↓, dip, below, under, less than): чем ниже порог, тем не больше;
  * лестница дат «к дате» (by …): чем раньше дата, тем вероятность не больше;
  * взаимоисключающие исходы (neg-risk): сумма вероятностей = 100%.

Что из этого следует:
  1. **Согласованная оценка**: цены лестницы проецируются на допустимое множество (изотоническая регрессия, пул
     соседних нарушителей) — это оценщик «Согласованность» (только там, где порядок нарушен).
  2. **Без риска (по стакану, с комиссией)**: если A шире B (B ⇒ A), то «Да» на A + «Нет» на B выплачивают не меньше $1
     при любом исходе; если вместе они стоят меньше $1 — прибыль гарантирована. Для взаимоисключающих исходов —
     «Да» на все исходы дешевле $1 (или «Нет» на все дешевле n − 1).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from .. import fees

NUM = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kmbt])?(?![\w])", re.I)
UP_WORDS = ("↑", "reach", " hit", "above", "over", "o/u", "at least", "or more", "higher", "greater", "exceed")
TOP_N = re.compile(r"\btop\s*\d", re.I)  # «Top 5»: меньше N — уже событие (как лестница «вниз»)
PLUS_N = re.compile(r"\d\s*\+")  # «50+» в названии исхода — «не меньше» (лестница «вверх»)
DOWN_WORDS = ("↓", "dip", "below", "under ", "less than", "lower", "fall", "drop")
MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        1,
    )
}
DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})(?:,\s*(\d{4}))?\b", re.I)
MIN_GAP = 0.01  # нарушение порядка меньше 1 п.п. — шум середины стакана, не сигнал
ARB_BUFFER = 0.005  # без риска — только если выгода больше 0,5¢ на пару (округления, задержка стакана)


def _num(tok: tuple[str, str]) -> float:
    v = float(tok[0].replace(",", ""))
    return v * {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get((tok[1] or "").lower(), 1)


@dataclass
class Rung:
    key: str
    title: str
    x: float  # порог или дата (порядковый номер дня)


@dataclass
class Ladder:
    """Ступени упорядочены так, что вероятность должна НЕ расти вдоль списка: каждая следующая — уже предыдущей."""

    kind: str  # up | down | date
    template: str
    rungs: list[Rung]


def _direction(text: str) -> str | None:
    if TOP_N.search(text):
        return "down"
    if PLUS_N.search(text):
        return "up"
    low = f" {text.lower()} "
    up = any(w in low for w in UP_WORDS)
    down = any(w in low for w in DOWN_WORDS)
    if up == down:
        return None
    return "up" if up else "down"


def _date(m: re.Match, today: date) -> date:
    """Дата из названия. Без года — ближайшая будущая (или прошедшая не больше месяца назад): «March 31» в ноябре —
    это март следующего года, иначе порядок лестницы перевернётся."""
    mon, day = MONTHS[m.group(1).lower()], int(m.group(2))
    if m.group(3):
        return date(int(m.group(3)), mon, day)
    d = date(today.year, mon, day)
    return date(today.year + 1, mon, day) if (today - d).days > 31 else d


def ladders(outcomes: list[dict], today: date | None = None) -> list[Ladder]:
    """outcomes: строки события (key, title, question). Ищет лестницы порогов и дат."""
    today = today or date.today()
    groups: dict[tuple[str, str], list[Rung]] = {}
    for o in outcomes:
        title, q = o.get("title") or "", o.get("question") or ""
        dm = DATE.search(title)
        if dm and re.search(r"\b(by|before)\b", q.lower()):
            d = _date(dm, today)
            tmpl = DATE.sub("#", q) if DATE.search(q) else "#date"
            # раньше дата — уже событие: сортируем от поздней к ранней
            groups.setdefault(("date", tmpl), []).append(Rung(o["key"], title, -d.toordinal()))
            continue
        nums = NUM.findall(title)
        if len(nums) != 1:
            continue  # нет числа или диапазон («340-359») — это взаимоисключающие корзины, не лестница
        kind = _direction(title) or (None if TOP_N.search(q) or PLUS_N.search(q) else _direction(q))
        if not kind:
            continue
        tmpl = NUM.sub("#", title).strip()
        x = _num(nums[0])
        # «вверх»: выше порог — уже; «вниз»: ниже порог — уже
        groups.setdefault((kind, tmpl), []).append(Rung(o["key"], title, x if kind == "up" else -x))
    out = []
    for (kind, tmpl), rungs in groups.items():
        if len({r.x for r in rungs}) >= 2:
            out.append(Ladder(kind, tmpl, sorted(rungs, key=lambda r: r.x)))
    return out


def isotonic_down(ps: list[float], ws: list[float] | None = None) -> list[float]:
    """Ближайший (взвешенный) невозрастающий ряд — пул соседних нарушителей."""
    ws = ws or [1.0] * len(ps)
    blocks = [[p, w, 1] for p, w in zip(ps, ws, strict=True)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] < blocks[i + 1][0] - 1e-12:
            a, b = blocks[i], blocks[i + 1]
            w = a[1] + b[1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / w, w, a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out: list[float] = []
    for v, _, n in blocks:
        out += [v] * int(n)
    return out


def projected(outcomes: list[dict], market: dict[str, float], liquid: set[str] | None = None) -> dict[str, dict]:
    """Согласованные вероятности по лестницам: ключ → {p, gap (насколько нарушен порядок), ladder}.
    liquid — исходы с живым двусторонним стаканом: у пустых рынков «цена» 0,5¢ не значит ничего."""
    out: dict[str, dict] = {}
    for lad in ladders(outcomes):
        rungs = [r for r in lad.rungs if r.key in market and (liquid is None or r.key in liquid)]
        if len(rungs) < 2:
            continue
        ps = [market[r.key] for r in rungs]
        proj = isotonic_down(ps)
        for r, p, q in zip(rungs, ps, proj, strict=True):
            if abs(q - p) >= MIN_GAP:
                out[r.key] = {"p": q, "gap": q - p, "ladder": lad.template, "kind": lad.kind}
    return out


# ---------------------------------------------------------------- без риска
ARB_LOG = "arb_log"


def log_arbs(db: Any, ev: dict, arbs: list[dict], now: float | None = None) -> None:
    """Журнал замеченных безрисковых комбинаций (последние 300) — как часто они бывают и сколько дают."""
    if not arbs:
        return
    now = now or time.time()
    log = list(db.get_state(ARB_LOG) or [])
    for a in arbs[:3]:
        sig = (str(ev["id"]), tuple(sorted(leg["token_id"] for leg in a["legs"])))
        # одна и та же комбинация чаще раза в час — одна запись
        if any((x["event_id"], tuple(x["tokens"])) == sig and now - x["ts"] < 3600 for x in log[-50:]):
            continue
        log.append(
            {
                "ts": int(now),
                "event_id": str(ev["id"]),
                "title": ev.get("title", "")[:120],
                "kind": a["kind"],
                "profit": a["profit"],
                "shares": a["shares"],
                "tokens": list(sig[1]),
            }
        )
    db.set_state(ARB_LOG, log[-300:])


def arb_stats(db: Any, days: int = 30, now: float | None = None) -> dict:
    now = now or time.time()
    log = [x for x in (db.get_state(ARB_LOG) or []) if now - x["ts"] <= days * 86400]
    return {
        "days": days,
        "n": len(log),
        "events": len({x["event_id"] for x in log}),
        "best": max((x["profit"] for x in log), default=None),
        "last": log[-5:][::-1],
    }


def _ask(q: dict | None) -> tuple[float, float] | None:
    asks = (q or {}).get("asks") or []
    return (float(asks[0][0]), float(asks[0][1])) if asks else None


def arbitrage(ev: dict, quotes: dict[str, dict], rate: float) -> list[dict]:
    """Комбинации с гарантированной прибылью по лучшим ценам стакана (с комиссией тейкера)."""
    rows = {r["key"]: r for r in ev.get("outcomes") or [] if not r.get("closed")}
    out = []
    for lad in ladders(list(rows.values())):
        rs = [r for r in lad.rungs if r.key in rows]
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                wide, narrow = rows[rs[i].key], rows[rs[j].key]  # narrow ⇒ wide
                a = _ask(quotes.get(wide["yes_token"]))
                b = _ask(quotes.get(narrow.get("no_token") or ""))
                if not a or not b:
                    continue
                cost = a[0] + fees.fee_per_share(rate, a[0]) + b[0] + fees.fee_per_share(rate, b[0])
                if cost < 1 - ARB_BUFFER:
                    out.append(
                        {
                            "kind": "ladder",
                            "legs": [
                                {
                                    "token_id": wide["yes_token"],
                                    "title": wide["title"],
                                    "label": wide.get("yes_label") or "Да",
                                    "price": a[0],
                                    "depth": a[1],
                                },
                                {
                                    "token_id": narrow["no_token"],
                                    "title": narrow["title"],
                                    "label": narrow.get("no_label") or "Нет",
                                    "price": b[0],
                                    "depth": b[1],
                                },
                            ],
                            "cost": round(cost, 4),
                            "profit": round(1 - cost, 4),  # на пару акций, при любом исходе (если сбудутся оба — $2)
                            "shares": round(min(a[1], b[1]), 2),
                            "why": f"«{narrow['title']}» невозможно без «{wide['title']}»",
                        }
                    )
    # взаимоисключающие: «Да» на все исходы дешевле $1 (сбудется ровно один)
    if ev.get("neg_risk") and len(rows) >= 2:
        legs: list[dict] = []
        total, depth = 0.0, float("inf")
        for r in rows.values():
            a = _ask(quotes.get(r["yes_token"]))
            if not a:
                legs = []
                break
            total += a[0] + fees.fee_per_share(rate, a[0])
            depth = min(depth, a[1])
            legs.append({"token_id": r["yes_token"], "title": r["title"], "label": "Да", "price": a[0], "depth": a[1]})
        mids = sum(float(r.get("price") or 0) for r in rows.values())
        if legs and mids >= 0.98 and total < 1 - ARB_BUFFER:  # перечислены все исходы (сумма цен ≈ 100%)
            out.append(
                {
                    "kind": "exclusive_yes",
                    "legs": legs,
                    "cost": round(total, 4),
                    "profit": round(1 - total, 4),
                    "shares": round(depth, 2),
                    "why": "сбудется ровно один из исходов — выплата $1",
                }
            )
    return sorted(out, key=lambda x: -x["profit"])
