"""Демо-сервер интерфейса на синтетических данных — для сценариев Playwright (frontend/e2e) и скриншотов.

Без сети: синтетический мир из tests/synthetic.py, готовые данные Polymarket из tests/test_players.py.
Запуск: python tests/demo_server.py --port 8899 [--web путь_к_сборке]
Перед запуском сервера: пересчёт китов, три кита на копировании, сигналы, одна подтверждённая ставка.
Ставки — виртуальные, как и во всём приложении.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from events_fake import EventsClob, EventsGamma, make_events  # noqa: E402
from test_players import ReadyData, ReadyGamma  # noqa: E402

from whalefollow import categories  # noqa: E402
from whalefollow.agent import Agent  # noqa: E402
from whalefollow.db import DB  # noqa: E402
from whalefollow.estimator.base import FunctionEstimator  # noqa: E402
from whalefollow.ingest.polymarket import parse_market  # noqa: E402
from whalefollow.positions import assign_entities, build_positions  # noqa: E402
from whalefollow.scoring.scorer import run_scoring  # noqa: E402
from whalefollow.scoring.sybil import run_antisybil  # noqa: E402
from whalefollow.services import Services  # noqa: E402

from conftest import fast_params  # noqa: E402
from synthetic import FakeData, FakeGamma, SyntheticWorld, load_world  # noqa: E402


def _mix(dst: type, src: type) -> None:
    for k, v in vars(src).items():
        if callable(v) and not k.startswith("__") and not hasattr(dst, k):
            setattr(dst, k, v)


class DemoData(FakeData):
    def leaderboard(self, category="OVERALL", period="MONTH", order_by="PNL", limit=50, offset=0):
        rows = []
        for i, w in enumerate(sorted({t["proxy_wallet"] for t in self.w.trades})[:120]):
            pnl = 250_000 / (i + 1) ** 0.7
            rows.append(
                {
                    "rank": i + 1,
                    "wallet": w,
                    "name": f"trader_{i + 1:03d}",
                    "pnl": pnl,
                    "volume": pnl * 18,
                    "verified": i % 7 == 0,
                }
            )
        return rows[offset : offset + limit]


DEMO_FC = {"Анна Смирнова": 0.33, "Борис Ким": 0.17, "Вера Ли": 0.47, "Глеб Орлов": 0.03}


def _demo_source():
    from whalefollow.estimator.sources import Forecast, NewsItem
    from whalefollow.plugins import Evidence, SourcePlugin

    class Demo(SourcePlugin):
        name = "demo_source"
        title = "Демо-источник"
        description = "Новости, прогнозы и данные для демонстрации (без сети)"

        def news(self, ctx, query, since_ts):
            now = int(time.time())
            return [
                NewsItem(
                    "Демо-новости",
                    "media",
                    now - 3600 * (i + 1),
                    f"Новость {i + 1}: {query}",
                    f"https://example.org/n{i}",
                )
                for i in range(3)
            ]

        def forecasts(self, ctx, question, market=None):
            # две площадки согласны: у «Веры Ли» шанс 47% при цене 18% — советник предложит ставку;
            # у остальных чуть ниже цены (сумма по площадкам = 100%)
            p = next((v for k, v in DEMO_FC.items() if k in question), 0.33)
            return [
                Forecast("Демо-площадка", question, "https://example.org/f", p),
                Forecast("Демо-биржа", question, "https://example.org/g", p),
            ]

        def dossier(self, ctx, ev):
            out = self.news_and_forecasts(ctx, ev)
            out.append(
                Evidence(
                    "data", "Демо-данные", "Явка на прошлых выборах", value=41.5, unit="%", note="пример числа по теме"
                )
            )
            return out

        def news_and_forecasts(self, ctx, ev):
            from whalefollow.eventhub.dossier import _default

            return _default(self, ctx, ev)

    return Demo()


def _closed_analyses(db: DB, now: int) -> None:
    """Экран «Точность модели событий»: 80 закрытых исходов с анализами до закрытия (модель чуть точнее шумной
    цены) — без сети, затем подбор λ и весов, как это делает worker раз в час."""
    import json
    import math
    import random

    from whalefollow.eventhub import selfcheck

    rnd = random.Random(7)
    sig = lambda x: 1 / (1 + math.exp(-x))
    lg = lambda p: math.log(p / (1 - p))
    for i in range(80):
        topic = ("politics", "crypto", "sports", "weather")[i % 4]
        q = rnd.uniform(0.1, 0.9)
        y = 1 if rnd.random() < q else 0
        market = sig(lg(q) + rnd.gauss(0, 0.5))
        mdl = sig(lg(q) + rnd.gauss(0, 0.4))
        final = sig(lg(market) + 0.3 * (lg(mdl) - lg(market)))
        close = now - (i + 1) * 3 * 3600
        mid, tok = f"demo-closed-m{i}", f"demo-closed-t{i}"
        db.upsert(
            "markets",
            {
                "market_id": mid,
                "event_id": f"demo-closed-{i}",
                "category": topic,
                "status": "resolved",
                "resolved_ts": close,
                "end_ts": close,
                "question": f"Закрытый вопрос {i}",
            },
            ["market_id"],
        )
        db.upsert(
            "tokens", {"token_id": tok, "market_id": mid, "outcome_label": "Yes", "payout": float(y)}, ["token_id"]
        )
        comp = [
            {
                "name": "platforms",
                "title": "Другие площадки прогнозов",
                "p": round(mdl, 4),
                "w": 0.65,
                "role": "independent",
                "note": "",
            }
        ]
        db.upsert(
            "event_analyses",
            {
                "id": f"demo-closed-a{i}",
                "event_id": f"demo-closed-{i}",
                "ts": close - 86400,
                "mode": "manual",
                "outcome_key": tok,
                "market_id": mid,
                "market_p": market,
                "anchor_p": market,
                "model_p": mdl,
                "final_p": final,
                "lo": 0.0,
                "hi": 1.0,
                "confidence": "средняя",
                "lam": 0.3,
                "dossier_ts": close - 86400,
                "components": json.dumps(comp, ensure_ascii=False),
            },
            ["id"],
        )
    selfcheck.learn(db, now)


def build(db_url: str = "sqlite://:memory:") -> tuple[Services, dict]:
    _mix(DemoData, ReadyData)
    _mix(FakeGamma, ReadyGamma)
    w = SyntheticWorld(seed=4, now=int(time.time()) // 3600 * 3600)  # «сейчас» — настоящее: таймеры сигналов идут
    evs = make_events(w)  # события для вкладки «События» (многоисходное и «Да/Нет»)
    db = DB(db_url)
    db.init_schema()
    load_world(db, w)
    p = fast_params()
    build_positions(db)
    run_antisybil(db, p)
    assign_entities(db)
    sc = run_scoring(db, p, now=w.now)
    db.set_setting("onboarding", {"jurisdiction_ok": True, "risk_ack": True, "ts": 1})
    db.set_setting("categories", categories.KEYS + ["other"])
    # досье без сети: встроенные сетевые источники выключены, вместо них — демонстрационный источник
    db.set_setting("news_sources", dict.fromkeys(("google_news", "manifold", "kalshi", "bookmakers", "reddit"), False))
    from whalefollow import plugins

    plugins.registry().register(_demo_source(), "демо")
    _closed_analyses(db, w.now)
    data = DemoData(w)
    ReadyData.__init__(data)
    gamma = EventsGamma(w, evs)
    agent = Agent(db, p, data, EventsClob(w), gamma, estimator=FunctionEstimator(lambda c: 0.64), clock=lambda: w.now)
    svc = Services(db, p, agent=agent, data=data, gamma=gamma, clock=lambda: w.now)
    best = sc.filter(sc["status"] == "approved").sort("score", descending=True)["entity_id"].to_list()
    for e in best[:3]:
        svc.set_whale(e, mode="copy")
    agent.run_once()  # первая догрузка истории сигналов не создаёт
    # новые ставки китов на живых рынках → сигналы, ждущие решения
    for e in best[:3]:
        cats = [
            r["category"]
            for r in db.query(
                "SELECT category FROM wallet_category_scores WHERE entity_id=? AND status='approved'", (e,)
            )
        ]
        for cat, price in zip((cats or ["economics"]) * 2, (0.52, 0.47), strict=False):
            m = w.add_live_market(category=cat, pi=0.75, price=price)
            mrow, toks, _ = parse_market(w.gamma_market(m))
            db.upsert("markets", mrow, ["market_id"])
            db.upsert_many("tokens", toks, ["token_id"])
            w.whale_trade_now(agent.wallets_of(e)[0], m, m.yes_token, 3000)
    agent.run_once()
    pend = [r["signal_id"] for r in db.query("SELECT signal_id FROM signals WHERE status='pending' ORDER BY ts DESC")]
    bet = None
    for sid in pend[-1:]:  # одна ставка подтверждена заранее — для сценария «продать ставку»
        r = agent.confirm(sid)
        if r.get("status") == "executed":
            bet = sid
    # прошлая проверка источников и пульс процессов — для экрана «Диагностика»
    now = time.time()
    db.set_state(
        "diagnostics",
        {
            "ts": now - 120,
            "ok": True,
            "polymarket_median_ms": 84,
            "checks": [
                {
                    "group": "polymarket",
                    "label": "Рынки (Gamma)",
                    "detail": "ответ получен",
                    "ms": 81,
                    "ok": True,
                    "speed": "быстро",
                },
                {
                    "group": "polymarket",
                    "label": "Сделки (Data API v2)",
                    "detail": "ответ получен",
                    "ms": 87,
                    "ok": True,
                    "speed": "быстро",
                },
                {
                    "group": "polymarket",
                    "label": "Стаканы (CLOB)",
                    "detail": "ответ получен",
                    "ms": 64,
                    "ok": True,
                    "speed": "мгновенно",
                },
                {
                    "group": "news",
                    "label": "Google News",
                    "detail": "статьи найдены",
                    "ms": 240,
                    "ok": True,
                    "speed": "нормально",
                },
            ],
        },
    )
    for name in ("api", "realtime", "worker"):
        db.upsert(
            "service_status",
            {
                "name": name,
                "pid": 1,
                "host": "demo",
                "version": "demo",
                "started_ts": now - 7200,
                "last_ts": now,
                "info": "{}",
            },
            ["name"],
        )
    return svc, {"pending": [s for s in pend if s != bet], "bet": bet, "whales": best[:3]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--web", default="", help="другая папка интерфейса (например, прежний для сравнения скорости)")
    args = ap.parse_args()
    from pathlib import Path

    from whalefollow.api import static
    from whalefollow.api.server import serve

    if args.web:
        static.WEB_DIR = Path(args.web).resolve()

    svc, info = build()
    httpd = serve(svc, port=args.port)
    print(f"DEMO READY http://127.0.0.1:{args.port} pending={len(info['pending'])} bet={bool(info['bet'])}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
