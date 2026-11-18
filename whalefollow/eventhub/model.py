"""Модель оценки исходов события (модуль «События», этап C).

Шаги для каждого исхода:
  1. якорь — цена Polymarket сейчас (середина спроса и предложения); если включена «Цена с поправкой» — поправленная;
  2. независимые оценки — плагины-оценщики по досье (другие площадки, крипто-порог, погода, ИИ вслепую в режиме
     «с ИИ», …), у каждой вес = вес оценщика × качество данных;
  3. сводная оценка — средневзвешенное в логитах;
  4. итог = якорь + λ·(сводная − якорь) в логитах, λ = λ0·сила данных (λ0 = 0,3, пока модель не доказала на закрытых
     событиях, что точнее рынка: проверка на 2432 закрытых рынках показала, что цена Polymarket уже очень точна);
  5. взаимоисключающие исходы: сумма итогов = сумме цен (у полного события ≈100%) — исходы без оценок сдвигаются
     пропорционально в обратную сторону; неопределённость — разброс оценщиков, всплеск новостей.
Всё «на момент сейчас»: досье — последний сбор, цены — текущие. Каждый анализ сохраняется (этап E — самопроверка).
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from ..db import DB, new_id
from ..plugins import EstimatorPlugin, context, record, registry
from . import dossier as dz

LAMBDA0 = 0.3  # насколько далеко итог может отойти от цены при сильных данных (этап E подберёт по точности)
SAVE_EVERY_S = 600  # в историю анализов — не чаще раза в 10 мин на событие и режим (или при новом досье/ИИ)
Z80 = 1.2816  # 80% интервал


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _news_spike(items: list[dict], now: float) -> bool:
    fresh = [x for x in items if x["kind"] == "news" and x.get("ts_pub") and now - x["ts_pub"] < 86400]
    return len(fresh) >= 5


def analyze(db: DB, ev: dict, mode: str = "manual", market: dict[str, float] | None = None, save: bool = True) -> dict:
    """ev — событие из каталога (detail), market — цены исходов сейчас (ключ → вероятность «Да»)."""
    now = time.time()
    info = dz.event_info(ev)
    d = dz.load(db, info.id)
    items = d["items"]
    market = dict(market or {})
    for r in ev.get("outcomes") or []:
        if r["key"] not in market and r.get("price") is not None:
            market[r["key"]] = float(r["price"])
    ctx = context(db)
    plugs = [p for p in registry().active("estimator", ctx) if isinstance(p, EstimatorPlugin)]
    anchor = dict(market)
    comps: dict[str, list[dict]] = {o.key: [] for o in info.outcomes}
    used: dict[str, dict] = {}
    for p in plugs:
        if p.needs_ai and mode != "ai":
            continue
        try:
            if not p.applies(info, items):
                continue
            est = p.estimate(ctx, info, items, market)
            record(db, p.name, ok=True, note=f"оценок: {len(est)}")
        except Exception as exc:
            record(db, p.name, ok=False, error=f"{type(exc).__name__}: {exc}")
            used[p.name] = {"title": p.title, "error": str(exc)[:200]}
            continue
        used[p.name] = {"title": p.title, "n": len(est), "role": p.role, "weight": p.weight}
        for e in est:
            if e.outcome_key not in comps:
                continue
            if p.role == "anchor":
                anchor[e.outcome_key] = float(e.p)
            comps[e.outcome_key].append(
                {
                    "name": p.name,
                    "title": p.title,
                    "p": round(float(e.p), 5),
                    "q": round(float(e.quality), 3),
                    "w": round(p.weight * float(e.quality), 3) if p.role != "anchor" else None,
                    "role": p.role,
                    "note": e.note,
                }
            )
    spike = _news_spike(items, now)
    rows = []
    for o in info.outcomes:
        m = market.get(o.key)
        a = anchor.get(o.key, m)
        if a is None:
            continue
        ind = [c for c in comps[o.key] if c["role"] != "anchor" and c["w"] and c["w"] > 0]
        wsum = sum(c["w"] for c in ind)
        if ind:
            lm = sum(c["w"] * _logit(c["p"]) for c in ind) / wsum
            model_p = _sig(lm)
            strength = min(1.0, wsum)
            lam = LAMBDA0 * strength
            final = _sig(_logit(a) + lam * (lm - _logit(a)))
            sd = math.sqrt(sum(c["w"] * (_logit(c["p"]) - lm) ** 2 for c in ind) / wsum) if len(ind) > 1 else 0.6
        else:
            model_p, lam, final, sd, strength = None, 0.0, a, 0.0, 0.0
        width = lam * sd + 0.08 + (0.2 if spike else 0.0)
        lo, hi = _sig(_logit(final) - Z80 * width), _sig(_logit(final) + Z80 * width)
        if not ind:
            conf = "только цена"
        elif len(ind) >= 2 and sd < 0.35 and strength >= 0.8:
            conf = "высокая"
        elif sd < 0.7:
            conf = "средняя"
        else:
            conf = "низкая"
        rows.append(
            {
                "key": o.key,
                "title": o.title,
                "market_id": o.market_id,
                "market": m,
                "anchor": a,
                "model": model_p,
                "final": final,
                "lo": lo,
                "hi": hi,
                "lam": round(lam, 3),
                "confidence": conf,
                "components": comps[o.key],
            }
        )
    # Взаимоисключающие исходы: сумма итогов = сумме цен рынка (у полного события ≈100%). Исходы с независимыми
    # оценками двигаются, остальные — пропорционально в обратную сторону, чтобы сумма не менялась. Нормировать к ровно
    # 100% нельзя: при сумме середин 97% или неполном списке исходов это дало бы «выгоду» на пустом месте.
    if info.exclusive and len(rows) > 1 and any(r["model"] is not None for r in rows):
        total = sum(r["anchor"] for r in rows)
        moved = [r for r in rows if r["model"] is not None]
        rest = [r for r in rows if r["model"] is None]
        s_moved = sum(r["final"] for r in moved)
        a_rest = sum(r["anchor"] for r in rest)
        if rest and a_rest > 0 and total - s_moved > 0:
            k_rest, k_moved = (total - s_moved) / a_rest, 1.0
        else:  # оценки у всех исходов (или остальным не осталось места) — масштабируем все к сумме рынка
            k_rest = k_moved = total / sum(r["final"] for r in rows)
        for r in rows:
            k = k_moved if r["model"] is not None else k_rest
            r["final"], r["lo"], r["hi"] = r["final"] * k, min(1.0, r["lo"] * k), min(1.0, r["hi"] * k)
    for r in rows:
        r["diff"] = (r["final"] - r["market"]) if r["market"] is not None else None
    res = {
        "event_id": info.id,
        "mode": mode,
        "ts": int(now),
        "dossier_ts": d["collected_ts"],
        "lambda0": LAMBDA0,
        "news_spike": spike,
        "estimators": used,
        "outcomes": rows,
        "explanation": explain(rows, spike, d["collected_ts"] is not None),
    }
    if save:
        _save(db, res)
    return res


def _num(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def _pct(p: float, nd: int = 1) -> str:
    return _num(p * 100, nd) + "%"


def explain(rows: list[dict], spike: bool, has_dossier: bool) -> list[str]:
    """Объяснение без ИИ — по шаблону из тех же данных."""
    out: list[str] = []
    if not has_dossier:
        return ["Досье ещё не собрано — оценка равна цене рынка."]
    moved = sorted(
        (r for r in rows if r.get("diff") is not None and abs(r["diff"]) >= 0.02), key=lambda r: -abs(r["diff"])
    )
    for r in moved[:3]:
        ind = sorted((c for c in r["components"] if c["role"] != "anchor" and c["w"]), key=lambda c: -c["w"])
        why = "; ".join(f"{c['title']} {_pct(c['p'], 0)}" for c in ind[:3])
        sign = "выше" if r["diff"] > 0 else "ниже"
        out.append(
            f"«{r['title']}»: {_pct(r['final'])} — {sign} цены {_pct(r['market'])} на {_num(abs(r['diff']) * 100)} п.п."
            + (f" Основание: {why}." if why else "")
        )
    if not moved:
        if any(r["model"] is not None for r in rows):
            out.append("Независимые оценки близки к цене рынка — модель с рынком согласна.")
        else:
            out.append(
                "По этому событию нет независимых оценок (другие площадки, расчёты по теме) — оценка равна цене рынка."
            )
    if spike:
        out.append("За сутки много свежих новостей — цена может не успеть их учесть; неопределённость увеличена.")
    out.append(
        "Модель осторожна: итог отходит от цены не больше чем на 30% разницы, пока на закрытых событиях не доказано, что она точнее рынка."
    )
    return out


def _save(db: DB, res: dict) -> None:
    last = db.one(
        "SELECT ts, dossier_ts, components FROM event_analyses WHERE event_id=? AND mode=? ORDER BY ts DESC LIMIT 1",
        (res["event_id"], res["mode"]),
    )
    sig = json.dumps(sorted((r["key"], [c["p"] for c in r["components"]]) for r in res["outcomes"]))
    if last and res["ts"] - last["ts"] < SAVE_EVERY_S and last["dossier_ts"] == res["dossier_ts"]:
        prev = db.query(
            "SELECT outcome_key, components FROM event_analyses WHERE event_id=? AND mode=? AND ts=?",
            (res["event_id"], res["mode"], last["ts"]),
        )
        prev_sig = json.dumps(
            sorted((r["outcome_key"], [c["p"] for c in json.loads(r["components"] or "[]")]) for r in prev)
        )
        if prev_sig == sig:
            return  # недавно сохраняли и оценщики те же — новую точку не пишем
    rows = [
        {
            "id": new_id("ea_"),
            "event_id": res["event_id"],
            "ts": res["ts"],
            "mode": res["mode"],
            "outcome_key": r["key"],
            "market_id": r["market_id"],
            "market_p": r["market"],
            "anchor_p": r["anchor"],
            "model_p": r["model"],
            "final_p": r["final"],
            "lo": r["lo"],
            "hi": r["hi"],
            "confidence": r["confidence"],
            "lam": r["lam"],
            "dossier_ts": res["dossier_ts"],
            "components": json.dumps(r["components"], ensure_ascii=False),
        }
        for r in res["outcomes"]
    ]
    if rows:
        db.upsert_many("event_analyses", rows, ["id"])


def history(db: DB, event_id: str, mode: str, since: int = 0) -> dict[str, list[dict]]:
    """Линия модели на графике: итоговая оценка по исходам во времени."""
    out: dict[str, list[dict]] = {}
    for r in db.query(
        "SELECT ts, outcome_key, final_p FROM event_analyses WHERE event_id=? AND mode=? AND ts>=? ORDER BY ts",
        (event_id, mode, since),
    ):
        out.setdefault(r["outcome_key"], []).append({"ts": int(r["ts"]), "p": round(float(r["final_p"]), 4)})
    return out


def as_json(res: dict) -> Any:
    return res
