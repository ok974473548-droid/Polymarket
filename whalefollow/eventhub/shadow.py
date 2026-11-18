"""Теневые анализы (точность модели, F1): данные для самопроверки и самообучения.

Модель учится только на закрытых событиях, а анализы сохраняются, лишь когда пользователь открыл событие — это единицы
исходов в неделю. Теневые анализы сами берут широкую выборку событий по всем темам и раз в несколько часов
сохраняют по ним анализ без ИИ (бесплатно) — с ценой рынка на тот момент. Через 1–2 месяца — тысячи закрытых исходов.

Выборка: по каждой теме — события, которые скоро закрываются (исходы придут быстрее), и самые крупные за сутки; только
с заметным объёмом (у тонких рынков цена шумная) и не закрывающиеся в ближайшие часы (там цена уже почти 0 или 1).
По каждому событию — до 12 исходов с ценой от 2% до 98%: исходы «почти наверняка нет» ничему не учат.

Нагрузка ограничена: очередь событий обновляется раз в `every_h` часов, задача worker раз в 15 минут разбирает её
порцией с бюджетом времени. Факты досье старше 45 дней удаляются (оценки оценщиков остаются в анализах).
"""

from __future__ import annotations

import time
from typing import Any

from ..db import DB
from . import advisor, consistency, model
from . import dossier as dz
from .catalog import TOPIC_TAG

STATE = "shadow_queue"
MIN_VOLUME = 10_000.0  # $ — у более тонких рынков цена слишком шумная, чтобы с ней сравнивать
MIN_LEFT_S = 6 * 3600  # закрывается раньше — цена уже почти 0 или 1
MAX_HORIZON_S = 120 * 86400  # дальше — исход придёт слишком нескоро
MAX_OUTCOMES = 12
P_RANGE = (0.02, 0.98)
BATCH = 12  # событий за один проход задачи
BUDGET_S = 240.0  # и не дольше стольких секунд
KEEP_EVIDENCE_S = 45 * 86400
DEFAULTS = {"enabled": True, "per_topic": 10, "every_h": 6}


def settings_of(s: dict) -> dict:
    cfg = {**DEFAULTS, **(s.get("shadow") or {})}
    cfg["per_topic"] = max(0, min(50, int(cfg["per_topic"])))
    cfg["every_h"] = max(1, min(48, int(cfg["every_h"])))
    return cfg


def pick(cat: Any, per_topic: int, now: float | None = None) -> list[dict]:
    """События для теневых анализов: по каждой теме — половина «скоро закроются», половина «крупные за сутки»."""
    t: float = now or time.time()
    out: list[dict] = []
    seen: set[str] = set()

    def ok(it: dict) -> bool:
        end = float(it.get("end_ts") or 0)
        return (
            end > 0
            and t + MIN_LEFT_S <= end <= t + MAX_HORIZON_S
            and (it.get("volume") or 0) >= MIN_VOLUME
            and it.get("outcomes_n", 0) > 0
        )

    for topic in TOPIC_TAG:
        got: list[dict] = []
        for sort, want in (("ending", (per_topic + 1) // 2), ("volume24", per_topic)):
            for offset in (0, 24, 48):
                try:
                    items = cat.browse(topic, sort, "", offset)["items"]
                except Exception:
                    break
                for it in items:
                    if len(got) >= want or it["id"] in seen or not ok(it):
                        continue
                    seen.add(it["id"])
                    got.append({"id": it["id"], "topic": topic, "title": it["title"]})
                if len(got) >= want or len(items) < 24:
                    break
        out += got
    return out


def outcomes_for_shadow(ev: dict) -> list[dict]:
    rows = [
        r
        for r in ev["outcomes"]
        if not r["closed"] and r.get("price") is not None and P_RANGE[0] <= r["price"] <= P_RANGE[1]
    ]
    return sorted(rows, key=lambda r: -(r["price"] or 0))[:MAX_OUTCOMES]


def analyze_one(db: DB, cat: Any, event_id: str, clients: dict) -> int:
    """Досье (если устарело) → модель без ИИ → анализ с пометкой «теневой». Возвращает число сохранённых исходов."""
    ev = cat.get(event_id)  # заодно сохраняет рынки и исходы — по ним самопроверка узнает, чем кончилось
    if ev is None:
        return 0
    rows = outcomes_for_shadow(ev)
    if not rows:
        return 0
    cur = dz.load(db, event_id)
    if cur["collected_ts"] is None or time.time() - cur["collected_ts"] > dz.STALE_S:
        dz.collect(db, ev, clients)
    # цена — как показывает Polymarket (Gamma); стаканы для каждого исхода не запрашиваем, чтобы не нагружать CLOB
    market = {r["key"]: float(r["price"]) for r in rows}
    # связанные исходы (лестницы, взаимоисключающие) — стаканы нужны: ищем комбинации без риска и копим статистику
    open_rows = [r for r in ev["outcomes"] if not r["closed"]]
    if ev.get("neg_risk") or consistency.ladders(open_rows):
        _, qs = advisor.market_quotes(cat, ev)
        consistency.log_arbs(db, ev, consistency.arbitrage(ev, qs, advisor.event_fee_rate(db, ev)))
    res = model.analyze(db, {**ev, "outcomes": rows}, "manual", market, origin="shadow")
    return len(res["outcomes"])


def prune(db: DB, now: float) -> int:
    """Старые факты досье (оценки оценщиков уже сохранены в анализах)."""
    cut = int(now - KEEP_EVIDENCE_S)
    n = db.scalar("SELECT COUNT(*) AS n FROM evidence WHERE collected_ts<?", (cut,), 0) or 0
    if n:
        db.execute("DELETE FROM evidence WHERE collected_ts<?", (cut,))
        db.execute("DELETE FROM event_dossiers WHERE collected_ts<?", (cut,))
    return int(n)


def run(db: DB, cat: Any, clients: dict, settings: dict, now: float | None = None, budget_s: float = BUDGET_S) -> dict:
    """Один проход: при необходимости обновить очередь, разобрать порцию с бюджетом времени."""
    now = now or time.time()
    cfg = settings_of(settings)
    st = dict(db.get_state(STATE) or {})
    if not cfg["enabled"] or cfg["per_topic"] == 0:
        return {"state": "off"}
    queue = list(st.get("queue") or [])
    if not queue and now - float(st.get("filled_ts") or 0) >= cfg["every_h"] * 3600:
        queue = [x["id"] for x in pick(cat, cfg["per_topic"], now)]
        st.update(filled_ts=int(now), filled_n=len(queue), round=int(st.get("round") or 0) + 1)
    t0 = time.monotonic()
    done = saved = errors = 0
    while queue and done < BATCH and time.monotonic() - t0 < budget_s:
        eid = queue.pop(0)
        try:
            saved += analyze_one(db, cat, eid, clients)
        except Exception as exc:
            errors += 1
            st["last_error"] = f"{eid}: {type(exc).__name__}: {exc}"[:300]
        done += 1
    st.update(
        queue=queue,
        last_ts=int(now),
        last_done=done,
        last_saved=saved,
        total_events=int(st.get("total_events") or 0) + done,
        total_errors=int(st.get("total_errors") or 0) + errors,
    )
    st["pruned"] = prune(db, now)
    db.set_state(STATE, st)
    return {"state": "ok", "done": done, "saved": saved, "errors": errors, "left": len(queue)}


def status(db: DB) -> dict:
    st = dict(db.get_state(STATE) or {})
    n_out = db.scalar(
        "SELECT COUNT(DISTINCT event_id || '|' || outcome_key) AS n FROM event_analyses WHERE origin='shadow'", (), 0
    )
    n_ev = db.scalar("SELECT COUNT(DISTINCT event_id) AS n FROM event_analyses WHERE origin='shadow'", (), 0)
    return {
        "round": st.get("round") or 0,
        "filled_ts": st.get("filled_ts"),
        "last_ts": st.get("last_ts"),
        "queue": len(st.get("queue") or []),
        "events": int(n_ev or 0),
        "outcomes": int(n_out or 0),
        "errors": int(st.get("total_errors") or 0),
        "last_error": st.get("last_error"),
    }
