"""«Следить» за событием (модуль «События», этап D).

Фоновая проверка (процесс worker, раз в 15 минут): по каждому событию из списка — досье (если устарело), модель,
совет. Если появилась выгодная сторона, которой не было в прошлый раз (или выгода выросла на 2 п.п. и больше),
приходит уведомление (колокольчик → push, Telegram и другие каналы по настройкам «О чём» → «Выгода в событиях»).
Закрытые события перестают проверяться сами. Всё на момент проверки, ставки сами не делаются.
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..db import DB

CHECK_EVERY_S = 900
RENOTIFY_GAIN = 0.02  # повторить уведомление, если выгода выросла на 2 п.п.


def get(db: DB, event_id: str) -> dict | None:
    r = db.one("SELECT * FROM event_watch WHERE event_id=? AND active=1", (str(event_id),))
    if not r:
        return None
    return {k: r[k] for k in ("event_id", "title", "created_ts", "mode", "checked_ts", "notified_ts", "last_error")}


def listing(db: DB) -> list[dict]:
    return db.query(
        "SELECT event_id, title, created_ts, checked_ts, notified_ts, last_error FROM event_watch "
        "WHERE active=1 ORDER BY created_ts DESC"
    )


def set_watch(db: DB, event_id: str, on: bool, title: str = "", mode: str = "") -> dict | None:
    now = int(time.time())
    if on:
        db.upsert(
            "event_watch",
            {
                "event_id": str(event_id),
                "title": title,
                "created_ts": now,
                "active": 1,
                "mode": mode or None,
                "checked_ts": None,
                "notified_ts": None,
                "state": None,
                "last_error": None,
            },
            ["event_id"],
        )
    else:
        db.execute("UPDATE event_watch SET active=0 WHERE event_id=?", (str(event_id),))
    return get(db, event_id)


def gains(advice: dict) -> dict[str, float]:
    """Выгодные стороны: «ключ|сторона» → выгода на акцию (после комиссии); без риска — «arb|токены» → прибыль на пару."""
    out = {f"{x['key']}|{x['side']}": float(x["edge"]) for x in advice.get("buys") or []}
    for a in advice.get("arbs") or []:
        out["arb|" + "|".join(sorted(leg["token_id"] for leg in a["legs"]))] = float(a["profit"])
    return out


def news(prev: dict[str, float], cur: dict[str, float]) -> list[str]:
    """Стороны, о которых стоит сообщить: новые или с выросшей выгодой."""
    return [k for k, e in cur.items() if k not in prev or e >= prev[k] + RENOTIFY_GAIN]


def check(db: DB, cat: Any, clients: dict, caps_fn: Any, settings: dict, now: float | None = None) -> dict:
    """Одна фоновая проверка всех событий из списка. caps_fn(now) → {bank, cap} — сколько можно ставить."""
    from . import advisor

    now = now or time.time()
    done = notified = 0
    for w in db.query("SELECT * FROM event_watch WHERE active=1"):
        if w["checked_ts"] and now - w["checked_ts"] < CHECK_EVERY_S - 30:
            continue
        eid = w["event_id"]
        try:
            ev = cat.get(eid)
            if ev is None or ev.get("closed") or not [r for r in ev["outcomes"] if not r["closed"]]:
                db.execute("UPDATE event_watch SET active=0, checked_ts=? WHERE event_id=?", (int(now), eid))
                continue
            mode = w["mode"] or str(settings.get("ai_mode") or "manual")
            from . import ai

            if mode == "ai" and not ai.available():
                mode = "manual"
            ex = advisor.evaluate(
                db, cat, ev, mode, clients, int(settings.get("events_ai_daily_limit") or 0), sync=True, origin="watch"
            )
            caps = caps_fn(now)
            adv = advisor.advise(
                ex["analysis"],
                ev,
                ex["quotes"],
                advisor.event_fee_rate(db, ev),
                caps["bank"],
                max(caps["cap"], advisor.MIN_BET_USD),
                now,
            )
            # для уведомления важна выгода, а не остаток лимита: при исчерпанном лимите сообщаем всё равно
            cur = gains(adv)
            prev = json.loads(w["state"] or "{}")
            fresh = news(prev, cur)
            from . import consistency

            consistency.log_arbs(db, ev, adv.get("arbs") or [], now)
            fresh_arbs = [
                a
                for a in adv.get("arbs") or []
                if "arb|" + "|".join(sorted(leg["token_id"] for leg in a["legs"])) in fresh
            ]
            if fresh_arbs:
                a = fresh_arbs[0]
                legs = " + ".join(
                    f"«{leg['label']}» {leg['title']} по {leg['price'] * 100:.1f}¢" for leg in a["legs"][:3]
                )
                db.notify(
                    "event_edge",
                    f"Без риска: {ev['title'][:80]}",
                    f"{legs}: прибыль {a['profit'] * 100:.1f}¢ на пару при любом исходе".replace(".", ","),
                    ref=eid,
                )
                notified += 1
            elif fresh:
                best = max((x for x in adv["buys"] if f"{x['key']}|{x['side']}" in fresh), key=lambda x: x["edge"])
                title = f"Выгода: {ev['title'][:80]}"
                body = (
                    f"«{best['title']}» — купить «{best['label']}» до {best['limit_price'] * 100:.1f}¢: "
                    f"оценка модели {best['p'] * 100:.1f}%, цена {best['ask'] * 100:.1f}¢, "
                    f"выгода {best['edge'] * 100:.1f} п.п. после комиссии"
                ).replace(".", ",")
                db.notify("event_edge", title, body, ref=eid)
                notified += 1
            db.execute(
                "UPDATE event_watch SET checked_ts=?, state=?, last_error=NULL"
                + (", notified_ts=?" if fresh else "")
                + " WHERE event_id=?",
                (int(now), json.dumps(cur), *((int(now),) if fresh else ()), eid),
            )
            done += 1
        except Exception as exc:
            db.execute(
                "UPDATE event_watch SET checked_ts=?, last_error=? WHERE event_id=?",
                (int(now), f"{type(exc).__name__}: {exc}"[:300], eid),
            )
    return {"checked": done, "notified": notified}
