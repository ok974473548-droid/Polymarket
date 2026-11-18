"""Оценка исходов события через Claude «вслепую» (модуль «События», режим «с ИИ»).

Claude видит правила, срок, исходы, новости и числа из досье — но НЕ видит цены: ни Polymarket, ни других площадок,
ни сделок (иначе он подстраивается под рынок). Несколько независимых ответов, по каждому исходу берётся середина.
Ответ — строго по схеме JSON (structured outputs). При отказе модели по соображениям безопасности сервер сам
повторяет запрос на запасной модели (fallbacks). Дневной лимит анализов — настройка `events_ai_daily_limit`.

Ключ — ANTHROPIC_API_KEY; модель — WF_EVENTS_LLM_MODEL (если не задана — WF_LLM_MODEL, как у сигналов); ответов —
WF_EVENTS_AI_SAMPLES (по умолчанию 3).
"""

from __future__ import annotations

import json
import logging
import os
import statistics
import threading
import time
from datetime import UTC, datetime
from typing import Any

from ..db import DB
from ..estimator.llm import DEFAULT_MODEL

log = logging.getLogger(__name__)

MODEL = os.environ.get("WF_EVENTS_LLM_MODEL") or os.environ.get("WF_LLM_MODEL") or DEFAULT_MODEL
SAMPLES = int(os.environ.get("WF_EVENTS_AI_SAMPLES") or 3)
CACHE_S = 6 * 3600  # ответ ИИ годен, пока досье то же и не старше 6 ч

SYSTEM = (
    "You are a careful superforecaster estimating the probabilities of the outcomes of a prediction-market event. "
    "You are NOT told any market prices or other platforms' odds; do not guess them or reason about them. "
    "Work from the resolution rules, the dates and the provided materials. Start from a base rate for a reference "
    "class of similar events, then adjust for specific evidence. Treat social media and Telegram posts as leads, "
    "not proof; prefer primary sources. If materials are thin, stay close to the base rate and say confidence is low. "
    "Give a probability of YES for every listed outcome. If the outcomes are mutually exclusive, they must sum to 1. "
    "Write key_factors and what_would_change in Russian, short and plain."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "p": {"type": "number"}},
                "required": ["id", "p"],
                "additionalProperties": False,
            },
        },
        "base_rate": {"type": "string"},
        "key_factors": {"type": "array", "items": {"type": "string"}},
        "what_would_change": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["outcomes", "base_rate", "key_factors", "what_would_change", "confidence"],
    "additionalProperties": False,
}

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_prompt(ev: Any, dossier_items: list[dict], ids: dict[str, str]) -> str:
    """ev — EventInfo; ids — ключ исхода → короткий id для ответа. Цен и прогнозов площадок здесь нет намеренно."""
    lines = [
        f"Event: {ev.title}",
        f"Today: {datetime.now(UTC).strftime('%Y-%m-%d')}",
        f"Resolution date: {datetime.fromtimestamp(ev.end_ts, UTC).strftime('%Y-%m-%d') if ev.end_ts else 'not provided'}",
        f"Mutually exclusive outcomes: {'yes' if getattr(ev, 'exclusive', False) else 'no'}",
        f"Resolution rules: {(ev.description or 'not provided')[:3000]}",
        "",
        "Outcomes (give P(YES) for each id):",
    ]
    for o in ev.outcomes:
        lines.append(f"- {ids[o.key]}: {o.title} — {o.question}")
    data = [x for x in dossier_items if x["kind"] == "data"]
    news = sorted((x for x in dossier_items if x["kind"] == "news"), key=lambda x: -(x.get("ts_pub") or 0))[:25]
    lines += ["", "Data:"]
    lines += [
        f"- {x['title']}: {x.get('value')} {x.get('unit') or ''} ({x.get('note') or ''}; {x.get('source')})"
        for x in data
    ] or ["- none"]
    lines += ["", "News and posts (newest first):"]
    for x in news:
        when = datetime.fromtimestamp(x["ts_pub"], UTC).strftime("%Y-%m-%d") if x.get("ts_pub") else "?"
        lines.append(f"- [{when}] {x.get('source')}: {x.get('title')}")
    if not news:
        lines.append("- none found")
    return "\n".join(lines)


def _ask(client: Any, prompt: str) -> dict | None:
    import anthropic

    try:
        r = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # отказ по безопасности — сервер повторит на запасной модели
            system=SYSTEM,
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
    except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        log.warning("ИИ (события): %s", exc)
        return None
    if r.stop_reason == "refusal":
        return None
    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        return None


def estimate(db: DB, ev: Any, dossier_items: list[dict], dossier_ts: int | None, client: Any = None) -> dict:
    """Несколько ответов Claude → середина по каждому исходу. Сохраняет результат в кэш."""
    from ..util import pmap

    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    ids = {o.key: f"o{i + 1}" for i, o in enumerate(ev.outcomes)}
    back = {v: k for k, v in ids.items()}
    prompt = build_prompt(ev, dossier_items, ids)
    answers = [
        a for _, a, exc in pmap(lambda _: _ask(client, prompt), range(SAMPLES), workers=SAMPLES) if exc is None and a
    ]
    if not answers:
        raise RuntimeError("ИИ не ответил")
    per: dict[str, list[float]] = {}
    for a in answers:
        for o in a.get("outcomes") or []:
            k = back.get(str(o.get("id")))
            try:
                p = float(o.get("p"))
            except (TypeError, ValueError):
                continue
            if k and 0 <= p <= 1:
                per.setdefault(k, []).append(p)
    outs = {k: statistics.median(v) for k, v in per.items()}
    if getattr(ev, "exclusive", False) and outs:
        tot = sum(outs.values())
        outs = {k: v / tot for k, v in outs.items()} if tot > 0 else outs
    confs = [str(a.get("confidence")) for a in answers]
    res = {
        "outcomes": outs,
        "confidence": max(set(confs), key=confs.count),
        "key_factors": list(answers[0].get("key_factors") or [])[:6],
        "what_would_change": list(answers[0].get("what_would_change") or [])[:5],
        "base_rate": answers[0].get("base_rate"),
        "samples": len(answers),
        "model": MODEL,
        "ts": int(time.time()),
        "dossier_ts": dossier_ts,
    }
    db.set_state(f"ai_event:{ev.id}", res)
    return res


def cached(db: DB, event_id: str, dossier_ts: int | None = None) -> dict | None:
    res = db.get_state(f"ai_event:{event_id}")
    if not res or time.time() - float(res.get("ts") or 0) > CACHE_S:
        return None
    if dossier_ts is not None and res.get("dossier_ts") != dossier_ts:
        return None  # досье обновилось — нужен новый ответ
    return res


# ---------------------------------------------------------------- дневной лимит и фоновый запуск
def used_today(db: DB) -> int:
    st = db.get_state("ai_events_day") or {}
    return int(st.get("n") or 0) if st.get("day") == datetime.now(UTC).strftime("%Y-%m-%d") else 0


def _count(db: DB) -> None:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    db.set_state("ai_events_day", {"day": day, "n": used_today(db) + 1})


def job_state(event_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(str(event_id))
        return dict(j) if j else None


def start(db: DB, ev: Any, dossier_items: list[dict], dossier_ts: int | None, limit: int, client: Any = None) -> dict:
    eid = str(ev.id)
    with _lock:
        j = _jobs.get(eid)
        if j and j["state"] == "running" and time.time() - j["started"] < 300:
            return dict(j)
        if used_today(db) >= limit:
            return {"state": "limit", "error": f"Дневной лимит анализов с ИИ исчерпан ({limit})"}
        j = _jobs[eid] = {"state": "running", "started": time.time(), "error": None}

    def work() -> None:
        try:
            _count(db)
            estimate(db, ev, dossier_items, dossier_ts, client)
            with _lock:
                j.update(state="done", finished=time.time())
        except Exception as exc:
            with _lock:
                j.update(state="error", finished=time.time(), error=str(exc)[:200])

    threading.Thread(target=work, daemon=True, name=f"ai-event-{eid}").start()
    return dict(j)
