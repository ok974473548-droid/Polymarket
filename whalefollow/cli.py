"""Командная строка WhaleFollow.

whalefollow init-db
whalefollow sync-markets [--closed true|false]
whalefollow ingest --wallets 0xabc,0xdef | --wallets-file wallets.txt | --global
whalefollow sync-prices [--days 30]
whalefollow snapshot-books
whalefollow build                        # позиции + антисибил
whalefollow score
whalefollow backtest --select-days-ago 60 [--delays 5,30,120,600]
whalefollow walkforward --months 6
whalefollow run-agent [--once] [--interval 5] [--llm]
whalefollow serve [--port 8787] [--with-agent]
whalefollow run [--port 8787] [--llm]      # пробный режим: интерфейс + сбор данных + агент по расписанию
whalefollow check                          # проверка доступа к Polymarket
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading

from .config import Endpoints, Params
from .db import DB, now_ts

DAY = 86400
log = logging.getLogger("whalefollow")


def _clients(endpoints: Endpoints):
    from .ingest.polymarket import ClobClient, DataClient, GammaClient

    return GammaClient(endpoints), DataClient(endpoints), ClobClient(endpoints)


def _estimator(force: bool = False):
    """Claude, если есть ANTHROPIC_API_KEY; иначе режим без ИИ (данные собираются, решает пользователь)."""
    import os

    from .estimator.base import NoEstimator

    if not os.environ.get("ANTHROPIC_API_KEY"):
        if force:
            print("ANTHROPIC_API_KEY не задан — работаем без ИИ: сигналы будут ждать вашего решения.")
        return NoEstimator()
    from .estimator.llm import ClaudeEstimator

    return ClaudeEstimator()


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_build(db: DB, params: Params) -> dict:
    from .positions import assign_entities, build_positions
    from .scoring.sybil import run_antisybil

    build_positions(db)
    res = run_antisybil(db, params)
    assign_entities(db)
    return res


def cmd_check(endpoints: Endpoints) -> int:
    """Проверка доступа к Polymarket: каталог, лента сделок, стакан."""
    gamma, data, clob = _clients(endpoints)
    ok = True
    try:
        m = next(iter(gamma.markets(closed=False, page_size=5, max_pages=1)))
        print("Каталог рынков (Gamma): OK —", m.get("question", "")[:70])
        from .ingest.polymarket import _json_list

        token = (_json_list(m.get("clobTokenIds")) or [None])[0]
    except Exception as exc:
        ok, token = False, None
        print("Каталог рынков (Gamma): ОШИБКА —", exc)
    try:
        t = next(iter(data.trades(limit=5, max_pages=1)))
        print("Лента сделок (Data API): OK — последняя сделка", t.get("timestamp"))
    except Exception as exc:
        ok = False
        print("Лента сделок (Data API): ОШИБКА —", exc)
    if token:
        try:
            bids, asks = clob.book(str(token))
            print(f"Стакан (CLOB): OK — {len(bids)} заявок на покупку, {len(asks)} на продажу")
        except Exception as exc:
            ok = False
            print("Стакан (CLOB): ОШИБКА —", exc)
    if hasattr(data, "leaderboard"):
        try:
            rows = data.leaderboard("OVERALL", "WEEK", "PNL", limit=5)
            print(f"Рейтинг трейдеров (Data API, метод {getattr(data, 'lb_version', '?')}): OK — {len(rows)} строк")
        except Exception as exc:
            ok = False
            print("Рейтинг трейдеров: ОШИБКА —", exc)
    from . import diagnostics

    for name, url, sub in (
        (
            "Поток сделок (WebSocket)",
            diagnostics.WS_LIVE,
            {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]},
        ),
        (
            "Поток стаканов (WebSocket)",
            diagnostics.WS_MARKET,
            {"assets_ids": [str(token)], "type": "market"} if token else None,
        ),
    ):
        try:
            good, detail = diagnostics._ws_probe(url, sub, want_message=sub is not None)
            print(f"{name}: {'OK' if good else 'ПРОБЛЕМА'} — {detail}")
        except Exception as exc:
            print(f"{name}: ОШИБКА — {exc} (агент будет работать опросом, это медленнее)")
    # скорость: закрытые рынки по идентификаторам (так грузится история китов) и история цен
    try:
        import time as _t

        closed = list(gamma.markets(closed=True, page_size=50, max_pages=1))
        ids = [m.get("conditionId") for m in closed if m.get("conditionId")]
        t0 = _t.time()
        got = gamma.markets_by_ids(ids) if ids else []
        print(
            f"Закрытые рынки пачкой: {'OK' if len(got) == len(ids) else 'ПРОБЛЕМА'} — найдено {len(got)} из {len(ids)} "
            f"за {_t.time() - t0:.1f} с (формат списка: {gamma._array_mode or 'не определён'})"
        )
        from .ingest.polymarket import _json_list
        from .util import pmap

        toks = [t for m in closed[:20] for t in _json_list(m.get("clobTokenIds"))[:1]]
        t0 = _t.time()
        res = pmap(lambda x: clob.prices_history(str(x), fidelity_min=60), toks, 16)
        okn = sum(1 for _, r, e in res if e is None)
        print(f"История цен: {okn} из {len(toks)} рынков за {_t.time() - t0:.1f} с")
    except Exception as exc:
        print("Проверка скорости: ОШИБКА —", exc)
    from .ingest.chain import USDC_E, ExplorerClient

    chain = ExplorerClient.from_env()
    if chain is None:
        print(
            "Блокчейн (переводы USDC): не подключён — добавьте WF_ETHERSCAN_API_KEY "
            "или WF_CHAIN_PROVIDER=blockscout в .env"
        )
    else:
        try:
            rows = chain.token_transfers("0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e", USDC_E, page_size=5, max_pages=1)
            print(f"Блокчейн ({chain.provider}): OK — получено {len(rows)} переводов для проверки")
        except Exception as exc:
            print(
                f"Блокчейн ({chain.provider}): ОШИБКА —",
                exc,
                "\n  Если Etherscan пишет про тариф или сеть — попробуйте WF_CHAIN_PROVIDER=blockscout",
            )
    if not ok:
        print(
            "\nЕсли везде ошибки 403 или нет соединения — доступ к Polymarket из вашей сети ограничен. "
            "Проверьте, разрешено ли использование Polymarket в вашей стране."
        )
    return 0 if ok else 1


def resolve_db_url(url: str | None, in_docker: bool | None = None, data_dir: str = "/data") -> str:
    """В Docker база должна лежать в томе /data, иначе пропадает при каждой пересборке контейнера.
    Относительный путь SQLite (например, из старого .env: sqlite:///whalefollow.db) переводится в /data."""
    url = url or os.environ.get("WF_DB_URL", "sqlite:///whalefollow.db")
    if in_docker is None:
        in_docker = os.path.exists("/.dockerenv") and os.path.isdir(data_dir)
    if not in_docker or not url.startswith("sqlite:///") or url.startswith("sqlite:////"):
        return url
    rel = url[len("sqlite:///") :]
    if not rel or rel == ":memory:":
        return url
    fixed = f"sqlite:///{data_dir}/{os.path.basename(rel)}"
    print(
        f"База перенесена в том {data_dir}: {fixed} (путь {url} внутри контейнера терялся при пересборке)",
        file=sys.stderr,
    )
    return fixed


def _streams_on() -> bool:
    return os.environ.get("WF_STREAMS", "on").strip().lower() not in ("0", "off", "false", "no")


def _make_scheduler(db, params, gamma, data, clob, agent, role: str, clock=None):
    from .estimator.external import KalshiClient, OddsClient
    from .ingest.chain import ExplorerClient
    from .scheduler import Scheduler

    groups = {"realtime": ("fast", "books"), "worker": ("slow", "heavy")}.get(role)
    chain = odds = None
    if role in ("worker", "all"):
        chain = ExplorerClient.from_env()
        print(
            "Блокчейн (антисибил):",
            f"{chain.provider}" if chain else "выключен (нет WF_ETHERSCAN_API_KEY или WF_CHAIN_PROVIDER=blockscout)",
        )
        odds = OddsClient.from_env()
        print("Букмекеры:", "включены" if odds else "выключены (нет WF_ODDS_API_KEY)")
    return Scheduler(
        db,
        params,
        gamma,
        data,
        clob,
        agent,
        chain=chain,
        kalshi=KalshiClient(),
        odds=odds,
        groups=groups,
        role=None if role == "all" else role,
        clock=clock,
    )


def run_role(
    role: str,
    db,
    params,
    gamma,
    data,
    clob,
    port: int = 8787,
    host: str = "127.0.0.1",
    llm: bool = False,
    block: bool = True,
    clock=None,
):
    """Запуск процесса по роли: api | realtime | worker | all (см. whalefollow/runtime.py)."""
    from . import plugins
    from .agent import Agent
    from .plugins.notify import NotificationHub
    from .push import PushNotifier
    from .runtime import CheckRunner, Heartbeat, QuotePublisher
    from .scheduler import wait_forever
    from .services import Services

    agent = None
    if role in ("api", "realtime", "all"):  # api: агент без цикла — подтверждение ставки и продажа с экрана
        agent = Agent(db, params, data, clob, gamma, estimator=_estimator(llm), sources="settings", clock=clock)
        if role != "api":
            print("ИИ:", "доступен (Claude)" if agent.estimator.name != "none" else "недоступен — режим без ИИ")
    svc = Services(db, params, agent=agent, data=data, gamma=gamma, clock=clock)
    if role == "api":
        svc.check_mode = "queue"  # проверки игроков выполняет worker
    sched = None
    if role in ("realtime", "worker", "all"):
        sched = _make_scheduler(db, params, gamma, data, clob, agent, role)  # расписание — по настоящим часам
    if role in ("realtime", "all"):
        if _streams_on():
            from .streams import Streams
            from .tracked import tracked_wallets

            agent.streams = Streams(
                db, agent, wallets_fn=lambda: tracked_wallets(db), tokens_fn=sched._active_tokens
            ).start()
            print("Потоки реального времени: включены (сделки китов и стаканы — через WebSocket, опрос — страховка)")
            if role == "realtime":
                QuotePublisher(db, agent.streams.books).start()  # живые цены для процесса api
        else:
            print("Потоки реального времени: выключены (WF_STREAMS=off) — только опрос")
    # уведомления — плагины-каналы (Telegram, push, Discord, почта, свои); отправка — из любого процесса,
    # где появилось уведомление
    hub = NotificationHub.attach_to(db)
    svc.push_sender = PushNotifier(db)  # кнопка «Проверить» в настройках push
    if role in ("realtime", "all"):
        tg_on = bool(os.environ.get("WF_TELEGRAM_BOT_TOKEN") and os.environ.get("WF_TELEGRAM_CHAT_ID"))
        print("Telegram:", "включён" if tg_on else "выключен (нет WF_TELEGRAM_BOT_TOKEN / WF_TELEGRAM_CHAT_ID)")
        hub.start_commands(svc)  # кнопки «Купить/Пропустить» и команды /status /stop /resume — только здесь
    reg = plugins.registry()
    print(f"Плагины: {len(reg.plugins)}" + (f", ошибок загрузки: {len(reg.load_errors)}" if reg.load_errors else ""))
    runner = None
    if role in ("worker",):
        runner = CheckRunner(svc).start()
        print("Проверки игроков: выполняются здесь (очередь в базе)")

    def info() -> dict:
        out: dict = {}
        if sched is not None:
            out["jobs"] = sorted({j.group for j in sched.jobs if j.group in sched.groups})
        if role in ("realtime", "all"):
            out["streams"] = svc.streams_status()
        if runner is not None:
            out["checks_done"] = runner.ran
        return out

    Heartbeat(db, role, info_fn=info).start()
    if sched is not None:
        if role in ("worker", "all"):
            print("Первый проход: поиск кандидатов и скоринг займут от нескольких минут до часа.")
        sched.start()
    if role in ("api", "all"):
        _serve_ui(svc, db, port, host)
    elif block:
        print(f"Процесс {role} запущен (Ctrl+C — остановить)")
        wait_forever()
    return svc, sched, runner


def _public_cloud() -> bool:
    """Запущены на облачной площадке с публичным адресом (Railway задаёт RAILWAY_PROJECT_ID / RAILWAY_ENVIRONMENT)."""
    return bool(os.environ.get("RAILWAY_PROJECT_ID") or os.environ.get("RAILWAY_ENVIRONMENT"))


def _serve_ui(svc, db, port: int, host: str) -> None:
    from .api.server import serve
    from .auth import Auth

    auth = Auth(db)
    if not auth.required and _public_cloud():
        # Railway и подобные дают адрес в интернете — без пароля интерфейс (и виртуальный банк) открыт всем
        raise SystemExit("Сервер в облаке (Railway): задайте переменную WF_UI_PASSWORD — без пароля запуск запрещён.")
    httpd = serve(svc, host=host, port=port, auth=auth)
    print(
        "Вход в интерфейс:",
        "по паролю (WF_UI_PASSWORD)"
        if auth.required
        else "без пароля — не открывайте порт в интернет (задайте WF_UI_PASSWORD)",
    )
    print(f"API: http://{host}:{port}/api/dashboard  (Ctrl+C — остановить)")
    print(f"Документация API: http://{host}:{port}/docs")
    try:
        httpd.serve_forever()  # Ctrl+C / SIGTERM — плавная остановка (uvicorn)
    except KeyboardInterrupt:
        httpd.shutdown()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="whalefollow", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default=None, help="URL базы (по умолчанию WF_DB_URL или sqlite:///whalefollow.db)")
    ap.add_argument("--params", default=None, help="JSON с параметрами")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="создать или обновить схему базы (миграции Alembic)")
    sub.add_parser("migrate", help="обновить схему базы до последней версии и показать версию")
    p = sub.add_parser("sync-markets")
    p.add_argument("--closed", choices=["true", "false"], default=None)
    p = sub.add_parser("ingest")
    p.add_argument("--wallets", default="")
    p.add_argument("--wallets-file", default=None)
    p.add_argument("--global", dest="global_feed", action="store_true")
    p = sub.add_parser("sync-prices")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("snapshot-books")
    sub.add_parser("build")
    sub.add_parser("score")
    p = sub.add_parser("backtest")
    p.add_argument("--select-days-ago", type=int, default=60)
    p.add_argument("--delays", default="5,30,120,600")
    p.add_argument("--size", type=float, default=20.0)
    p = sub.add_parser("walkforward")
    p.add_argument("--months", type=int, default=6)
    p = sub.add_parser("run-agent")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--llm", action="store_true", help="включить слепую оценку Claude (нужен ANTHROPIC_API_KEY)")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--with-agent", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--llm", action="store_true")
    for name, help_ in (
        ("run", "всё в одном процессе: интерфейс + агент + потоки + планировщик"),
        ("api", "процесс api: интерфейс, API, живые события"),
        ("realtime", "процесс realtime: агент, потоки WebSocket, сигналы, Telegram"),
        ("worker", "процесс worker: рынки, цены, пересчёт китов, резервные копии, проверки игроков"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--port", type=int, default=8787)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--llm", action="store_true")
    sub.add_parser("check", help="проверить доступ к API Polymarket")
    p = sub.add_parser("alive", help="жив ли процесс (для healthcheck): код 0 — пульс свежий")
    p.add_argument("role", choices=["api", "realtime", "worker", "all"])
    args = ap.parse_args(argv)
    from .logs import setup_logging

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    params = Params.load(args.params)

    if args.cmd == "check":
        return cmd_check(Endpoints())

    if args.cmd == "alive":
        from .runtime import services_state

        st = {s["name"]: s for s in services_state(DB(resolve_db_url(args.db)))}
        ok = bool(st.get(args.role, {}).get("alive"))
        print(f"{args.role}: {'жив' if ok else 'нет пульса'}")
        return 0 if ok else 1

    db = DB(resolve_db_url(args.db))
    before = db.schema_version()
    version = db.init_schema()
    endpoints = Endpoints()
    if args.cmd in ("init-db", "migrate"):
        print(
            f"Схема базы: версия {version}"
            + (
                f" (была {before})"
                if before and before != version
                else " (старая база помечена как стартовая)"
                if before is None and db.scalar("SELECT COUNT(*) AS n FROM markets", default=0)
                else ""
            )
        )
        return 0
    from .ingest import pipeline

    gamma, data, clob = _clients(endpoints)
    if args.cmd == "sync-markets":
        closed = None if args.closed is None else args.closed == "true"
        print("рынков:", pipeline.sync_markets(db, gamma, closed=closed))
    elif args.cmd == "ingest":
        wallets = [w.strip() for w in args.wallets.split(",") if w.strip()]
        if args.wallets_file:
            with open(args.wallets_file, encoding="utf-8") as fh:
                wallets += [ln.strip() for ln in fh if ln.strip()]
        if args.global_feed:
            print("новых сделок:", len(pipeline.ingest_global_trades(db, data, gamma=gamma)))
        if wallets:
            print("новых сделок:", len(pipeline.ingest_wallet_trades(db, data, wallets, gamma=gamma)))
    elif args.cmd == "sync-prices":
        toks = [r["token_id"] for r in db.query("SELECT DISTINCT token_id FROM trades")]
        print("точек цены:", pipeline.sync_prices(db, clob, toks, start=now_ts() - args.days * DAY))
    elif args.cmd == "snapshot-books":
        toks = [
            r["token_id"]
            for r in db.query(
                "SELECT DISTINCT t.token_id FROM tokens t JOIN markets m ON m.market_id=t.market_id WHERE m.status='open' "
                "AND t.token_id IN (SELECT token_id FROM positions WHERE entity_id IN (SELECT entity_id FROM watchlist))"
            )
        ]
        print("стаканов:", len(pipeline.snapshot_books(db, clob, toks)))
    elif args.cmd == "build":
        _print(cmd_build(db, params))
    elif args.cmd == "score":
        from .scoring.scorer import run_scoring

        df = run_scoring(db, params)
        print(df["status"].value_counts().sort("status") if df.height else "нет позиций")
    elif args.cmd == "backtest":
        from .backtest.engine import run_backtest

        now = now_ts()
        _print(
            run_backtest(
                db,
                params,
                now - args.select_days_ago * DAY,
                now,
                delays=tuple(int(x) for x in args.delays.split(",")),
                size_usd=args.size,
            )
        )
    elif args.cmd == "walkforward":
        from .backtest.engine import quick_params, walk_forward

        now = now_ts()
        _print(walk_forward(db, quick_params(params), now - args.months * 30 * DAY, now - 30 * DAY))
    elif args.cmd in ("run", "api", "realtime", "worker"):
        role = "all" if args.cmd == "run" else args.cmd
        run_role(role, db, params, gamma, data, clob, port=args.port, host=args.host, llm=args.llm)
    elif args.cmd in ("run-agent", "serve"):
        from .agent import Agent

        agent = Agent(
            db, params, data, clob, gamma, estimator=_estimator(getattr(args, "llm", False)), sources="settings"
        )
        if args.cmd == "run-agent":
            if args.once:
                _print(agent.run_once())
            else:
                agent.run_forever(args.interval)
        else:
            from .services import Services

            if args.with_agent:
                threading.Thread(target=agent.run_forever, kwargs={"interval_s": 5}, daemon=True).start()
            _serve_ui(Services(db, params, agent=agent, data=data, gamma=gamma), db, args.port, args.host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
