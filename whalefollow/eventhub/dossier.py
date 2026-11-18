"""Досье события (модуль «События», этап B): факты из всех подходящих источников-плагинов.

Как собирается:
  * берутся включённые и доступные плагины-источники, которые подходят событию по теме (`applies`);
  * у кого есть `dossier(ctx, event)` — спрашиваем его; у остальных: `news()` по названию события и
    `forecasts()` по вопросам главных исходов (как раньше для сигналов китов);
  * источники опрашиваются параллельно; ошибка одного не мешает другим и видна на экране «Плагины»;
  * факты чистятся от повторов, новостям даётся вес по надёжности источника, всё сохраняется с моментом
    сбора (модель на этапе E проверяется только по тому, что было известно тогда).
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict
from typing import Any

from ..db import DB, new_id, now_ts
from ..estimator.external import similarity, words
from ..estimator.sources import keywords, manipulation_flags, source_weight
from ..plugins import EventInfo, Evidence, OutcomeInfo, SourcePlugin, context, record, registry
from ..util import pmap

NEWS_DAYS = 14
TOP_OUTCOMES = 5  # по скольким главным исходам спрашивать другие площадки
KEEP_COLLECTIONS = 5  # сколько прошлых сборов хранить по событию
STALE_S = 30 * 60  # досье старше — собирается заново при открытии события
MIN_SIMILARITY = 0.3  # насколько вопрос другой площадки должен совпадать по словам с вопросом исхода

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def event_info(ev: dict) -> EventInfo:
    rows = [r for r in ev.get("outcomes") or [] if not r.get("closed")]
    return EventInfo(
        id=str(ev["id"]),
        title=ev.get("title") or "",
        topic=ev.get("topic") or "other",
        description=ev.get("description") or "",
        end_ts=ev.get("end_ts"),
        tags=list(ev.get("tags") or []),
        outcomes=[
            OutcomeInfo(
                key=r["key"],
                title=r["title"],
                question=(r.get("question") or (ev.get("title") if r.get("single") else r["title"])) or "",
                price=r.get("price"),
                market_id=r.get("market_id") or "",
                yes_token=r.get("yes_token") or "",
                no_token=r.get("no_token") or "",
            )
            for r in rows
        ],
    )


def _default(p: SourcePlugin, ctx: Any, ev: EventInfo) -> list[Evidence]:
    """Источник без своего dossier(): новости по названию события, прогнозы — по вопросам исходов."""
    out: list[Evidence] = []
    if p.gives_news:
        q = keywords(ev.title, 8)
        for it in p.news(ctx, q, now_ts() - NEWS_DAYS * 86400)[:15]:
            out.append(
                Evidence("news", it.source, it.title, it.url, int(it.ts_pub or 0), meta={"source_type": it.source_type})
            )
    if p.gives_forecasts:
        for o in ev.outcomes[:TOP_OUTCOMES]:
            market = {"question": o.question, "market_id": o.market_id, "category": ev.topic}
            qw = _wordset(o.question)
            for f in p.forecasts(ctx, o.question, market)[:3]:
                sim = similarity(qw, _wordset(f.title))
                if sim < MIN_SIMILARITY:
                    continue  # другой вопрос (поиск площадки нашёл что-то по одному слову) — в досье не берём
                out.append(
                    Evidence(
                        "forecast",
                        f.source,
                        f.title,
                        f.url,
                        now_ts(),
                        value=float(f.probability),
                        unit="prob",
                        outcome_key=o.key,
                        note=f"похожий вопрос к исходу «{o.title}», совпадение слов {round(sim * 100)}%",
                        meta={"similarity": round(sim, 3), "question": o.question},
                    )
                )
    return out


def _wordset(text: str) -> set[str]:
    """Значимые слова для сравнения вопросов: как у Kalshi (латиница) + слова на других алфавитах."""
    return words(text) | {w for w in re.findall(r"[^\W\d_]{3,}", (text or "").lower()) if not w.isascii()}


def _norm_title(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def collect(db: DB, ev: dict, clients: dict | None = None, progress: Any = None) -> dict:
    """Собирает досье события и сохраняет его. Возвращает {"n", "sources": {имя: {...}}}."""
    info = event_info(ev)
    ctx = context(db, clients=clients or {})
    plugs = [p for p in registry().active("source", ctx) if isinstance(p, SourcePlugin) and _applies(p, info)]
    if progress:
        progress(0, len(plugs))

    def run(p: SourcePlugin) -> tuple[list[Evidence], int]:
        t0 = time.perf_counter()
        got = p.dossier(ctx, info)
        if got is None:
            got = _default(p, ctx, info)
        return list(got), int((time.perf_counter() - t0) * 1000)

    done = [0]

    def tick(*_: Any) -> None:
        done[0] += 1
        if progress:
            progress(done[0], len(plugs))

    results = pmap(run, plugs, workers=8, on_done=lambda *_: tick())
    sources: dict[str, dict] = {}
    items: list[Evidence] = []
    for p, res, exc in results:
        if exc is not None:
            msg = f"{type(exc).__name__}: {exc}"[:200]
            sources[p.name] = {"title": p.title, "n": 0, "error": msg}
            record(db, p.name, ok=False, error=f"досье: {msg}")
            continue
        got, ms = res or ([], 0)
        sources[p.name] = {"title": p.title, "n": len(got), "error": None, "ms": ms}
        record(db, p.name, ok=True, note=f"досье: фактов {len(got)}")
        items += got
    # повторы: одна и та же ссылка или одинаковый заголовок новости
    seen: set[str] = set()
    uniq: list[Evidence] = []
    for it in items:
        key = f"{it.kind}|{it.url or it.source + '|' + _norm_title(it.title)}|{it.outcome_key}"
        if it.kind == "news":
            key = _norm_title(it.title) or key
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    flags = _news_flags(db, info, [it for it in uniq if it.kind == "news"])
    ts = now_ts()
    rows = []
    for it in uniq:
        st = str(it.meta.get("source_type") or ("forecast" if it.kind == "forecast" else "data"))
        rows.append(
            {
                "id": new_id("evd_"),
                "event_id": info.id,
                "collected_ts": ts,
                "kind": it.kind,
                "source": it.source,
                "title": it.title[:400],
                "url": it.url,
                "ts_pub": int(it.ts or 0) or None,
                "value": it.value,
                "unit": it.unit,
                "outcome_key": it.outcome_key,
                "note": it.note[:300],
                "weight": round(source_weight(db, it.source, st), 3) if it.kind == "news" else None,
                "meta": json.dumps(it.meta, ensure_ascii=False, default=str) if it.meta else None,
            }
        )
    with db.tx():
        if rows:
            db.upsert_many("evidence", rows, ["id"])
        db.upsert(
            "event_dossiers",
            {
                "event_id": info.id,
                "title": info.title,
                "topic": info.topic,
                "collected_ts": ts,
                "n_items": len(rows),
                "sources": json.dumps({**sources, "_flags": flags}, ensure_ascii=False),
            },
            ["event_id"],
        )
        old = [
            r["collected_ts"]
            for r in db.query(
                "SELECT DISTINCT collected_ts FROM evidence WHERE event_id=? ORDER BY collected_ts DESC", (info.id,)
            )
        ][KEEP_COLLECTIONS:]
        if old:
            db.execute("DELETE FROM evidence WHERE event_id=? AND collected_ts<=?", (info.id, max(old)))
    db.set_state("events_used_ts", ts)  # модуль используется — фоновым задачам источников (Kalshi) есть смысл работать
    return {"n": len(rows), "sources": sources}


def _applies(p: SourcePlugin, ev: EventInfo) -> bool:
    try:
        return bool(p.applies(ev))
    except Exception:
        return False


def _news_flags(db: DB, ev: EventInfo, news: list[Evidence]) -> dict:
    """Признаки вброса по новостям (как у сигналов): одинаковые тексты, новости после движения цены."""
    if not news or not ev.outcomes:
        return {}
    tok = ev.outcomes[0].yes_token
    pts = [
        (r["ts"], r["price"])
        for r in db.query(
            "SELECT ts, price FROM price_history WHERE token_id=? AND ts>=? ORDER BY ts",
            (tok, now_ts() - NEWS_DAYS * 86400),
        )
    ]
    items = [
        {
            "title": n.title,
            "ts_pub": n.ts,
            "source": n.source,
            "source_type": n.meta.get("source_type", "media"),
            "text_hash": _norm_title(n.title),
        }
        for n in news
    ]
    try:
        return manipulation_flags(items, pts, now_ts()) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------- фоновый сбор (не держит запрос интерфейса)
def job_state(event_id: str) -> dict | None:
    with _lock:
        j = _jobs.get(str(event_id))
        return dict(j) if j else None


def start(db: DB, ev: dict, clients: dict | None = None) -> dict:
    """Запускает сбор в фоне, если он ещё не идёт. Возвращает состояние."""
    eid = str(ev["id"])
    with _lock:
        j = _jobs.get(eid)
        if j and j["state"] == "running" and time.time() - j["started"] < 180:
            return dict(j)
        j = _jobs[eid] = {"state": "running", "started": time.time(), "done": 0, "total": 0, "error": None}

    def progress(done: int, total: int) -> None:
        with _lock:
            j["done"], j["total"] = done, total

    def work() -> None:
        try:
            res = collect(db, ev, clients, progress)
            with _lock:
                j.update(state="done", finished=time.time(), n=res["n"])
        except Exception as exc:  # pragma: no cover — сбой базы и т.п.
            with _lock:
                j.update(state="error", finished=time.time(), error=str(exc)[:200])

    threading.Thread(target=work, daemon=True, name=f"dossier-{eid}").start()
    return dict(j)


def load(db: DB, event_id: str) -> dict:
    """Последнее досье события: факты по видам, итог по источникам, признаки вброса."""
    d = db.one("SELECT * FROM event_dossiers WHERE event_id=?", (str(event_id),))
    if not d:
        return {"collected_ts": None, "items": [], "sources": {}, "flags": {}}
    rows = db.query(
        "SELECT kind, source, title, url, ts_pub, value, unit, outcome_key, note, weight, meta FROM evidence "
        "WHERE event_id=? AND collected_ts=? ORDER BY ts_pub DESC",
        (str(event_id), d["collected_ts"]),
    )
    for r in rows:
        r["meta"] = json.loads(r["meta"]) if r.get("meta") else {}
    src = json.loads(d["sources"] or "{}")
    flags = src.pop("_flags", {}) or {}
    return {"collected_ts": d["collected_ts"], "items": rows, "sources": src, "flags": flags}


def evidence_dict(e: Evidence) -> dict:
    return asdict(e)
