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
