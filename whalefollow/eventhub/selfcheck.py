"""Самопроверка модели событий (модуль «События», этап E).

1. **Исходы.** Каждый сохранённый анализ (`event_analyses`: оценка каждого оценщика, цена рынка и итог модели на тот
   момент) ждёт закрытия рынка. Фоновая задача раз в час обновляет рынки из анализов и узнаёт исход (`tokens.payout`).
2. **Отчёт точности.** Берутся только анализы, сделанные ДО закрытия рынка. У исхода может быть много анализов —
   у каждого вес 1/число, чтобы часто открываемое событие не перевешивало остальные. Сравнение — на одних и тех же
   анализах: Brier итога модели против Brier цены рынка в тот же момент. Вывод честный: «лучше рынка» — только если
   разница больше двух стандартных ошибок (ошибки считаются по событиям: исходы одного события связаны).
3. **Обучение — только по закрытым событиям.** λ (насколько итог отходит от цены) подбирается по прошлым анализам
   и сжимается к исходным 0,3 (мало данных — почти не меняется). Вес каждого оценщика умножается на множитель по
   тому, улучшала ли его поправка цену в прошлом (0,2…2). Расти λ и веса могут, только когда польза доказана
   (вывод «лучше рынка»), снижаться — всегда. Режим «с ИИ» проверяется только вперёд: в анализы попадают
   лишь ответы, данные до закрытия рынка, — проверка ИИ на прошлом недопустима (он мог знать исходы).
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from typing import Any

from ..db import DB

LAMBDA_PRIOR = 0.3
LAMBDA_MAX = 0.6
LAMBDA_K = 100.0  # сжатие λ к исходному: столько «виртуальных» исходов с λ = 0,3
LAMBDA_GRID = [round(x * 0.025, 3) for x in range(0, 25)]  # 0 … 0,6
MIN_OUTCOMES = 30  # меньше — «мало данных», параметры не меняются
WEIGHT_K = 50.0
WEIGHT_MIN, WEIGHT_MAX = 0.2, 2.0
TOPIC_MIN = 50  # λ по теме — когда по ней закрыто не меньше стольких исходов
STATE = "event_model_params"
BUCKETS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0001]


def _logit(p: float) -> float:
    p = min(max(float(p), 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


# ---------------------------------------------------------------- исходы
def pending_markets(db: DB, limit: int = 300) -> list[str]:
    """Рынки из анализов, исход которых ещё неизвестен."""
    return [
        r["market_id"]
        for r in db.query(
            "SELECT DISTINCT ea.market_id FROM event_analyses ea LEFT JOIN tokens t ON t.token_id=ea.outcome_key "
            "WHERE ea.market_id IS NOT NULL AND t.payout IS NULL LIMIT ?",
            (limit,),
        )
    ]


def resolve(db: DB, gamma: Any) -> int:
    """Обновить рынки из анализов (статус, исход) — чтобы самопроверка узнала, чем кончилось."""
    from ..ingest import pipeline

    ids = pending_markets(db)
    if not ids:
        return 0
    n = pipeline.ensure_markets(db, gamma, ids)
    return n + pipeline.refresh_markets(db, gamma, ids)


# ---------------------------------------------------------------- выборка закрытых анализов
def samples(db: DB, now: float | None = None, mode: str | None = None) -> list[dict]:
    """Анализы до закрытия рынка с известным исходом; у каждого — вес 1/число анализов этого исхода и режима."""
    now = now or time.time()
    rows = db.query(
        "SELECT ea.event_id, ea.outcome_key, ea.mode, ea.origin, ea.ts, ea.market_p, ea.anchor_p, ea.model_p, ea.final_p, "
        "ea.lam, ea.components, t.payout AS y, m.category AS topic, m.resolved_ts, m.end_ts "
        "FROM event_analyses ea JOIN tokens t ON t.token_id=ea.outcome_key JOIN markets m ON m.market_id=t.market_id "
        "WHERE t.payout IS NOT NULL AND ea.market_p IS NOT NULL" + (" AND ea.mode=?" if mode else ""),
        (mode,) if mode else (),
    )
    out = []
    for r in rows:
        close = r["resolved_ts"] or r["end_ts"]
        if close and r["ts"] >= close:
            continue  # анализ после закрытия — не в счёт
        if r["resolved_ts"] and r["resolved_ts"] > now:
            continue
        r["components"] = json.loads(r["components"] or "[]")
        out.append(r)
    cnt: dict[tuple, int] = defaultdict(int)
    for r in out:
        cnt[(r["event_id"], r["outcome_key"], r["mode"])] += 1
    for r in out:
        r["w"] = 1.0 / cnt[(r["event_id"], r["outcome_key"], r["mode"])]
    return out


def _brier(rows: list[dict], key: str) -> float:
    w = sum(r["w"] for r in rows)
    return sum(r["w"] * (float(r[key]) - float(r["y"])) ** 2 for r in rows) / w if w else float("nan")


def _logloss(rows: list[dict], key: str) -> float:
    w = sum(r["w"] for r in rows)
    tot = 0.0
    for r in rows:
        p = min(max(float(r[key]), 1e-4), 1 - 1e-4)
        y = float(r["y"])
        tot -= r["w"] * (y * math.log(p) + (1 - y) * math.log(1 - p))
    return tot / w if w else float("nan")


def compare(rows: list[dict], key: str = "final_p", base: str = "market_p") -> dict:
    """Brier key против base на одних и тех же анализах; разница и её ошибка — по событиям."""
    rows = [r for r in rows if r.get(key) is not None and r.get(base) is not None]
    n_out = len({(r["event_id"], r["outcome_key"]) for r in rows})
    n_ev = len({r["event_id"] for r in rows})
    if not rows:
        return {"n": 0, "events": 0}
    per_ev: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        d = (float(r[key]) - float(r["y"])) ** 2 - (float(r[base]) - float(r["y"])) ** 2
        per_ev[r["event_id"]][0] += r["w"] * d
        per_ev[r["event_id"]][1] += r["w"]
    vals = [s / w for s, w in per_ev.values() if w]
    mean = sum(vals) / len(vals)
    se = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1) / len(vals)) if len(vals) > 1 else float("inf")
    b_key, b_base = _brier(rows, key), _brier(rows, base)
    return {
        "n": n_out,
        "events": n_ev,
        "brier": round(b_key, 5),
        "brier_market": round(b_base, 5),
        "logloss": round(_logloss(rows, key), 5),
        "logloss_market": round(_logloss(rows, base), 5),
        "diff": round(mean, 6),  # < 0 — модель точнее
        "se": round(se, 6) if math.isfinite(se) else None,
        "verdict": verdict(n_out, mean, se),
    }


def verdict(n: int, diff: float, se: float) -> dict:
    if n < MIN_OUTCOMES:
        return {
            "code": "few",
            "label": "Мало данных",
            "text": f"Закрыто исходов: {n}; для вывода нужно хотя бы {MIN_OUTCOMES}",
        }
    if math.isfinite(se) and diff + 2 * se < 0:
        return {
            "code": "better",
            "label": "Лучше рынка",
            "text": "Итог модели точнее цены рынка, и это больше случайного разброса",
        }
    if math.isfinite(se) and diff - 2 * se > 0:
        return {
            "code": "worse",
            "label": "Хуже рынка",
            "text": "Итог модели менее точен, чем цена рынка: λ и веса снижаются сами",
        }
    return {
        "code": "same",
        "label": "Пока не лучше рынка",
        "text": "Разница с ценой рынка в пределах случайности — модель пока не доказала, что обыгрывает рынок",
    }


def calibration(rows: list[dict], key: str = "final_p") -> list[dict]:
    out = []
    for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:], strict=True):
        g = [r for r in rows if r.get(key) is not None and lo <= float(r[key]) < hi]
        w = sum(r["w"] for r in g)
        if not w:
            continue
        out.append(
            {
                "range": f"{round(lo * 100)}–{min(round(hi * 100), 100)}%",
                "n": len({(r["event_id"], r["outcome_key"]) for r in g}),
                "predicted": round(sum(r["w"] * float(r[key]) for r in g) / w, 4),
                "actual": round(sum(r["w"] * float(r["y"]) for r in g) / w, 4),
            }
        )
    return out


# ---------------------------------------------------------------- оценщики: помогала ли их поправка цене
def estimator_rows(rows: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for c in r["components"]:
            if c.get("role") == "anchor" or c.get("p") is None:
                continue
            lm = _logit(r["market_p"])
            blend = _sig(lm + LAMBDA_PRIOR * (_logit(c["p"]) - lm))
            by[c["name"]].append({**r, "comp_p": float(c["p"]), "blend_p": blend, "title": c.get("title") or c["name"]})
    return by


def weight_mult(cmp: dict) -> float:
    """Множитель веса оценщика: улучшала поправка цену (Brier ниже) — больше, ухудшала — меньше; мало данных — 1."""
    n = cmp.get("n") or 0
    if n < 20 or not cmp.get("brier_market"):
        return 1.0
    gain = -cmp["diff"] / cmp["brier_market"]  # доля улучшения Brier
    shrink = n / (n + WEIGHT_K)
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN, 1 + 10 * gain * shrink)), 3)


# ---------------------------------------------------------------- λ
def fit_lambda(rows: list[dict]) -> tuple[float, float, int]:
    """λ с наименьшим Brier на прошлых анализах (итог = якорь + λ·(сводная − якорь)), затем сжатие к 0,3."""
    rows = [r for r in rows if r.get("model_p") is not None and r.get("anchor_p") is not None]
    n = len({(r["event_id"], r["outcome_key"]) for r in rows})
    if n < MIN_OUTCOMES:
        return LAMBDA_PRIOR, LAMBDA_PRIOR, n

    def brier_at(lam: float) -> float:
        w = tot = 0.0
        for r in rows:
            la = _logit(r["anchor_p"])
            p = _sig(la + lam * (_logit(r["model_p"]) - la))
            tot += r["w"] * (p - float(r["y"])) ** 2
            w += r["w"]
        return tot / w

    best = min(LAMBDA_GRID, key=brier_at)
    lam = (n * best + LAMBDA_K * LAMBDA_PRIOR) / (n + LAMBDA_K)
    return round(min(LAMBDA_MAX, max(0.0, lam)), 4), best, n


def learn(db: DB, now: float | None = None) -> dict:
    """Подбор λ (общего и по темам) и множителей весов оценщиков — только по анализам закрытых рынков.
    Режим «с ИИ» и без ИИ — вместе (у ИИ свой оценщик со своим множителем)."""
    now = now or time.time()
    rows = samples(db, now)
    lam, raw, n = fit_lambda(rows)
    # Главный принцип: отходить от цены дальше можно, только когда итог модели на закрытых событиях доказанно
    # точнее рынка (вывод «лучше рынка» по тем самым итогам, что показывались тогда, — это проверка «вперёд»).
    # Снижать λ и веса можно всегда: осторожность доказательств не требует.
    proven = compare(rows)["verdict"]["code"] == "better" if rows else False
    if lam > LAMBDA_PRIOR and not proven:
        lam = LAMBDA_PRIOR
    by_topic = {}
    topics = {r["topic"] for r in rows if r.get("topic")}
    for t in sorted(topics):
        tr = [r for r in rows if r.get("topic") == t]
        lt, rt, nt = fit_lambda(tr)
        if nt >= TOPIC_MIN:
            if lt > LAMBDA_PRIOR and compare(tr)["verdict"]["code"] != "better":
                lt = LAMBDA_PRIOR
            by_topic[t] = {"lambda": lt, "raw": rt, "n": nt}
    weights = {}
    for name, er in estimator_rows(rows).items():
        cmp = compare(er, "blend_p", "market_p")
        mult = weight_mult(cmp)
        if mult > 1 and cmp.get("verdict", {}).get("code") != "better":
            mult = 1.0  # больший вес — только за доказанную пользу
        weights[name] = {"mult": mult, "n": cmp.get("n", 0), "diff": cmp.get("diff")}
    prev = db.get_state(STATE) or {}
    hist = list(prev.get("history") or [])
    entry = {"ts": int(now), "lambda": lam, "n": n}
    if not hist or hist[-1].get("lambda") != lam or hist[-1].get("n") != n:
        hist = (hist + [entry])[-30:]
    params = {
        "lambda": lam,
        "lambda_raw": raw,
        "proven": proven,
        "n": n,
        "by_topic": by_topic,
        "weights": weights,
        "fitted_ts": int(now),
        "history": hist,
    }
    db.set_state(STATE, params)
    return params


def params(db: DB) -> dict:
    p = db.get_state(STATE) or {}
    return {
        "lambda": float(p.get("lambda", LAMBDA_PRIOR)),
        "by_topic": p.get("by_topic") or {},
        "weights": p.get("weights") or {},
        "n": int(p.get("n") or 0),
        "fitted_ts": p.get("fitted_ts"),
        "history": p.get("history") or [],
        "lambda_raw": p.get("lambda_raw"),
        "proven": bool(p.get("proven")),
    }


def lambda_for(p: dict, topic: str | None) -> float:
    t = (p.get("by_topic") or {}).get(topic or "")
    return float(t["lambda"]) if t else float(p.get("lambda", LAMBDA_PRIOR))


def mult_for(p: dict, name: str) -> float:
    w = (p.get("weights") or {}).get(name)
    return float(w["mult"]) if w else 1.0


# ---------------------------------------------------------------- отчёт для экрана
def report(db: DB, now: float | None = None) -> dict:
    now = now or time.time()
    rows = samples(db, now)
    par = params(db)
    modes = {}
    for mode, title in (("manual", "Без ИИ"), ("ai", "С ИИ")):
        mr = [r for r in rows if r["mode"] == mode]
        if not mr:
            continue
        modes[mode] = {
            "title": title,
            **compare(mr),
            "model_only": compare(mr, "model_p"),  # сводная оценка без сжатия к цене — для понимания
            # только где были независимые оценки (иначе итог = цена и разница ровно ноль)
            "with_info": compare([r for r in mr if r.get("model_p") is not None]),
            "calibration": calibration(mr),
            "calibration_market": calibration(mr, "market_p"),
        }
    topics = []
    for t in sorted({r["topic"] for r in rows if r.get("topic")}):
        c = compare([r for r in rows if r.get("topic") == t])
        topics.append({"topic": t, **c, "lambda": lambda_for(par, t)})
    ests = []
    for name, er in sorted(estimator_rows(rows).items()):
        c = compare(er, "blend_p", "market_p")
        ests.append(
            {
                "name": name,
                "title": er[0]["title"],
                **c,
                "alone": compare(er, "comp_p", "market_p").get("brier"),
                "mult": mult_for(par, name),
            }
        )
    pending = len(pending_markets(db, 100_000))
    by_origin = {
        str(r["o"] or "page"): int(r["n"])
        for r in db.query(
            "SELECT origin AS o, COUNT(DISTINCT event_id || '|' || outcome_key) AS n FROM event_analyses GROUP BY origin"
        )
    }
    closed_by_origin: dict[str, int] = defaultdict(int)
    for key in {(r["event_id"], r["outcome_key"], r.get("origin") or "page") for r in rows}:
        closed_by_origin[key[2]] += 1
    total = db.scalar("SELECT COUNT(DISTINCT event_id || '|' || outcome_key) AS n FROM event_analyses", (), 0) or 0
    return {
        "ok": True,
        "modes": modes,
        "topics": topics,
        "estimators": ests,
        "params": par,
        "analyses_outcomes": int(total),
        "pending_markets": pending,
        "by_origin": by_origin,  # исходов с анализами: страница / «Следить» / теневые
        "closed_by_origin": dict(closed_by_origin),
        "min_outcomes": MIN_OUTCOMES,
    }
