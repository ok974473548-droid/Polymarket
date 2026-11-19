"""Планировщик пробного режима: сбор данных и агент по расписанию (раздел 4 ТЗ).

Каждая очередь — свой поток, чтобы долгая задача не задерживала остальные:
fast:
  agent          каждые 5 с   новые сделки китов → сигналы → решения
books:
  books          каждые 20 с  снимки стаканов по рынкам китов и нашим ставкам (из потока WebSocket, иначе запросами)
  warm           каждые 45 с  держит соединения с Polymarket открытыми (без новой установки связи на каждый запрос)
slow (сбор данных):
  markets        каждые 10 мин  каталог открытых рынков
  resolutions    каждые 30 мин  исходы рынков, где есть наши ставки или позиции китов
  discover       каждые 15 мин  поиск кандидатов в свежей ленте и загрузка их полной истории
  prices         каждый час     история цен (для CLV и отметки ставок)
  transfers      каждые 30 мин  переводы USDC из блокчейна Polygon (антисибил), если подключён обозреватель
heavy (расчёты; стартует после первого прохода slow):
  score          раз в сутки    позиции → антисибил → скоринг → живой список китов
  housekeeping   раз в сутки    чистка базы по срокам хранения (retention.py) и сжатие файла
  weekly         раз в неделю   итоги недели в уведомления

При первом запуске очередь slow сразу проходит все задачи, затем heavy строит
первый список китов.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC

from .config import Params
from .db import DB, now_ts
from .execution.paper import Portfolio
from .ingest import pipeline
from .positions import assign_entities, build_positions
from .scoring.incremental import run_incremental
from .scoring.sybil import run_antisybil
from .tracked import auto_discover, migrate_watchlist, tracked_entities, tracked_wallets

log = logging.getLogger(__name__)
DAY = 86400


@dataclass
class Job:
    name: str
    interval_s: float
    fn: Callable[[], object]
    group: str
    last_run: float = 0.0
    last_ok: bool | None = None
    last_error: str | None = None
    runs: int = 0
    fail_streak: int = 0
    alerted: bool = False
    result: object = field(default=None, repr=False)


class Scheduler:
    def __init__(
        self,
        db: DB,
        params: Params,
        gamma,
        data,
        clob,
        agent,
        clock: Callable[[], int] | None = None,
        chain=None,
        kalshi=None,
        odds=None,
        discover_max_new: int = 30,
        discover_pages: int = 5,
        books_max_tokens: int = 150,
        prices_backfill_per_run: int = 300,
        groups: tuple[str, ...] | None = None,
        role: str | None = None,
    ):
        self.db, self.p = db, params
        self.gamma, self.data, self.clob, self.agent = gamma, data, clob, agent
        self.chain = chain  # обозреватель блокчейна (ExplorerClient) или None
        self.kalshi, self.odds = kalshi, odds  # клиенты Kalshi и The Odds API (или None)
        self.clock = clock or now_ts
        self.discover_max_new = discover_max_new
        self.discover_pages = discover_pages
        self.books_max_tokens = books_max_tokens
        self.prices_backfill = prices_backfill_per_run
        # процесс realtime ведёт группы fast и books, worker — slow и heavy; «всё в одном» — все четыре
        self.groups = tuple(groups or self.GROUPS)
        self.state_key = f"scheduler:{role}" if role else "scheduler"
        self._stop = threading.Event()
        self._slow_ready = threading.Event()  # первый проход сбора данных завершён — можно считать
        self._state_lock = threading.Lock()
        self.jobs = [
            Job("agent", 5, self.job_agent, "fast"),
            Job("books", 20, self.job_books, "books"),
            Job("warm", 45, self.job_warm, "books"),
            Job("markets", 600, self.job_markets, "slow"),
            Job("discover", 900, self.job_discover, "slow"),
            Job("resolutions", 1800, self.job_resolutions, "slow"),
            Job("prices", 3600, self.job_prices, "slow"),
            Job("transfers", 1800, self.job_transfers, "slow"),
            Job("activity", 6 * 3600, self.job_activity, "slow"),
            Job("recategorize", 6 * 3600, self.job_recategorize, "slow"),
            Job("score", DAY, self.job_score, "heavy"),
            Job("score_changes", 3600, self.job_score_changes, "heavy"),
            Job("housekeeping", DAY, self.job_housekeeping, "heavy"),
            Job("weekly", 7 * DAY, self.job_weekly, "heavy"),
            Job("backup", DAY, self.job_backup, "heavy"),
            Job("rotation", DAY, self.job_rotation, "heavy"),
            Job("event_watch", 300, self.job_event_watch, "slow"),
            Job("event_selfcheck", 3600, self.job_event_selfcheck, "slow"),
            Job("shadow", 900, self.job_shadow, "heavy"),
        ]
        # фоновые задачи плагинов-источников (кэш Kalshi, коэффициенты букмекеров, свои плагины)
        from . import plugins

        clients = {k: v for k, v in (("kalshi", kalshi), ("odds", odds)) if v is not None}
        for name, interval, fn in plugins.sync_jobs(db, clients):
            self.jobs.append(Job(name, interval, fn, "slow"))
        if not db.get_setting("paper_started_ts"):
            db.set_setting("paper_started_ts", self.clock())
        # одноразово: тема «Разное» (Polymarket не указал тему) включена и у тех, кто уже сохранял настройки
        cats = db.get_setting("categories")
        if cats is not None and "other" not in cats and not db.get_setting("migrated_other_category"):
            db.set_setting("categories", list(cats) + ["other"])
        db.set_setting("migrated_other_category", True)
        # одноразово (v0.12): темы как в меню Polymarket. Рынки, которые раньше попадали в соседнюю тему,
        # остаются включёнными: финансы были частью экономики, киберспорт — спорта, упоминания и погода — «Разного»
        cats = db.get_setting("categories")
        if cats is not None and not db.get_setting("migrated_topics_v2"):
            add = (
                (["finance"] if "economics" in cats else [])
                + (["esports"] if "sports" in cats else [])
                + (["mentions", "weather"] if "other" in cats else [])
            )
            db.set_setting("categories", list(dict.fromkeys(list(cats) + add)))
        db.set_setting("migrated_topics_v2", True)

    # ------------------------------------------------------------ задачи
    def job_agent(self):
        return self.agent.run_once()

    def _active_tokens(self) -> list[str]:
        """Токены, по которым нужен свежий стакан: наши открытые ставки, ждущие сигналы,
        открытые позиции китов из живого списка в открытых рынках."""
        # в процессе worker агента нет — открытые ставки берутся из базы напрямую
        pf = self.agent.pf if self.agent is not None else Portfolio(self.db)
        ours = [p["token_id"] for p in pf.open_positions()]
        pend = [r["token_id"] for r in self.db.query("SELECT token_id FROM signals WHERE status='pending'")]
        whales = [
            r["token_id"]
            for r in self.db.query(
                "SELECT p.token_id FROM positions p JOIN markets m ON m.market_id=p.market_id "
                "WHERE m.status='open' AND p.status='open' AND p.entity_id IN (SELECT entity_id FROM watchlist WHERE mode='copy') "
                "GROUP BY p.token_id ORDER BY MAX(p.opened_ts) DESC LIMIT ?",
                (self.books_max_tokens,),
            )
        ]
        from .services import watched_tokens

        seen, out = set(), []
        for t in watched_tokens(self.db) + ours + pend + whales:  # открытая страница события — первой
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out[: self.books_max_tokens]

    def job_books(self):
        """Снимки стаканов: из потока WebSocket (без запросов), остальное — запросами к бирже."""
        toks = self._active_tokens()
        streams = getattr(self.agent, "streams", None)
        rest, n = [], 0
        now = self.clock()
        for t in toks:
            got = streams.books.get(t) if streams is not None else None
            if got is None:
                rest.append(t)
                continue
            pipeline.store_book(self.db, t, got[0], got[1], now, "stream")
            n += 1
        return n + len(pipeline.snapshot_books(self.db, self.clob, rest, "schedule", now))

    def job_recategorize(self):
        """Одноразово после обновления тем: перечитать уже загруженные рынки, чтобы они получили новые темы
        (финансы, киберспорт, упоминания, погода). Повторяется, пока не пройдёт без ошибок."""
        if self.db.get_setting("recategorized_v2"):
            return None
        ids = [r["market_id"] for r in self.db.query("SELECT market_id FROM markets")]
        n = 0
        for chunk in [ids[i : i + 500] for i in range(0, len(ids), 500)]:
            n += pipeline.refresh_markets(self.db, self.gamma, chunk)
        self.db.set_setting("recategorized_v2", True)
        return n

    def job_warm(self):
        """Держит соединения с Polymarket открытыми: первый запрос после простоя не тратит время
        на установку защищённой связи (это 2–3 круга до сервера). Возвращает время ответа, мс."""
        out = {}
        for name, client, path, params in (
            ("gamma", self.gamma, "/markets", {"limit": 1, "closed": "false"}),
            ("data", self.data, "/v2/trades", {"limit": 1}),
            ("clob", self.clob, "/time", None),
        ):
            http = getattr(client, "http", None)
            if http is None or not hasattr(http, "warm"):
                continue
            try:
                out[name] = http.warm(path, params)
            except Exception as exc:
                out[name] = f"нет связи: {type(exc).__name__}"
        self.db.set_state("warm", {"ts": self.clock(), **out})
        return out

    def _scope(self) -> list[str] | None:
        """Кошельки, по которым собираются данные: только киты из списка.
        None — все кошельки (если пользователь включил автопоиск китов)."""
        return None if auto_discover(self.db) else tracked_wallets(self.db)

    def job_markets(self):
        """Полный каталог рынков нужен только для автопоиска; иначе рынки догружаются по сделкам китов."""
        if not auto_discover(self.db):
            return None
        return pipeline.sync_markets(self.db, self.gamma, closed=False)

    def job_resolutions(self):
        now = self.clock()
        scope = self._scope()
        if scope is not None and not scope and not self.db.scalar("SELECT COUNT(*) AS n FROM fills", default=0):
            return None  # никого не отслеживаем и ставок нет — нечего обновлять
        if scope is None:
            ids = {
                r["market_id"]
                for r in self.db.query(
                    "SELECT market_id FROM markets WHERE status IN ('open','closed') AND end_ts IS NOT NULL AND end_ts<=? "
                    "LIMIT 300",
                    (now,),
                )
            }
        else:
            # рынки китов из списка: истёкшие и открытые позиции (бывают досрочные исходы)
            ids = set()
            for chunk in [scope[i : i + 500] for i in range(0, len(scope), 500)]:
                ph = ",".join("?" for _ in chunk)
                ids |= {
                    r["market_id"]
                    for r in self.db.query(
                        "SELECT DISTINCT m.market_id FROM markets m JOIN trades t ON t.market_id=m.market_id "
                        f"WHERE t.wallet IN ({ph}) AND m.status IN ('open','closed') AND "
                        "(m.end_ts IS NULL OR m.end_ts<=? OR m.market_id IN (SELECT market_id FROM positions "
                        "WHERE status='open'))",
                        chunk + [now],
                    )
                }
        ids |= {
            r["market_id"]
            for r in self.db.query("SELECT DISTINCT t.market_id FROM fills f JOIN tokens t ON t.token_id=f.token_id")
        }
        n = pipeline.refresh_markets(self.db, self.gamma, ids)
        # догрузка рынков из сделок, которых нет в каталоге
        return n + pipeline.ensure_markets(
            self.db,
            self.gamma,
            pipeline.missing_trade_markets(self.db, 3000, wallets=scope or None) if scope is None or scope else [],
        )

    def job_discover(self):
        """Автопоиск новых китов по общей ленте — только если пользователь его включил."""
        if not auto_discover(self.db):
            return None
        return len(
            pipeline.discover_wallets(
                self.db, self.data, self.gamma, pages=self.discover_pages, max_new=self.discover_max_new
            )
        )

    def job_prices(self):
        now = self.clock()
        scope = self._scope()
        # история для токенов без цен: часовые точки (только рынки китов из списка)
        missing = (
            pipeline.tokens_missing_prices(self.db, self.prices_backfill, wallets=scope)
            if scope is None or scope
            else []
        )
        n = pipeline.sync_prices(self.db, self.clob, missing, fidelity_min=60)
        # свежие точки для активных рынков: последние 3 часа поминутно
        n += pipeline.sync_prices(self.db, self.clob, self._active_tokens(), start=now - 3 * 3600, end=now)
        return n

    def job_transfers(self):
        """Переводы USDC для кандидатов: сначала живой список и допущенные, потом остальные."""
        if self.chain is None:
            return None
        from .ingest.chain import sync_transfers

        scope = self._scope()
        if scope is not None:
            return sync_transfers(self.db, self.chain, scope, tuple(self.p.chain_tokens)) if scope else None
        prio = [
            r["wallet"]
            for r in self.db.query(
                "SELECT e.wallet FROM entity_wallets e JOIN watchlist w ON w.entity_id=e.entity_id "
                "UNION SELECT entity_id FROM watchlist WHERE entity_id NOT LIKE 'E-%'"
            )
        ]
        prio += [
            r["entity_id"]
            for r in self.db.query(
                "SELECT entity_id FROM wallet_scores WHERE status IN ('approved','candidate','watch') "
                "AND entity_id NOT LIKE 'E-%' ORDER BY score DESC"
            )
        ]
        prio += [r["wallet"] for r in self.db.query("SELECT wallet FROM wallet_sync ORDER BY feed_volume_usd DESC")]
        return sync_transfers(self.db, self.chain, prio, tuple(self.p.chain_tokens))

    def _priority_wallets(self, limit: int) -> list[str]:
        scope = self._scope()
        if scope is not None:
            return scope[:limit]
        rows = self.db.query(
            "SELECT e.wallet FROM entity_wallets e JOIN watchlist w ON w.entity_id=e.entity_id "
            "UNION SELECT entity_id FROM watchlist WHERE entity_id NOT LIKE 'E-%'"
        )
        out = [r["wallet"] for r in rows]
        out += [
            r["entity_id"]
            for r in self.db.query(
                "SELECT entity_id FROM wallet_scores WHERE status IN ('approved','candidate','watch') "
                "AND entity_id NOT LIKE 'E-%' ORDER BY score DESC LIMIT ?",
                (limit,),
            )
        ]
        return list(dict.fromkeys(out))[:limit]

    def job_activity(self):
        """split / merge / conversion для китов и кандидатов (новые операции)."""
        if not hasattr(self.data, "activity"):
            return None
        wallets = self._priority_wallets(200)
        return pipeline.sync_activities(self.db, self.data, wallets, gamma=self.gamma) if wallets else None

    def job_reconcile(self):
        """Сверка с Polymarket для живого списка китов."""
        if not hasattr(self.data, "positions"):
            return None
        from .reconcile import reconcile

        n = 0
        for r in self.db.query("SELECT entity_id FROM watchlist LIMIT 50"):
            e = r["entity_id"]
            wallets = [
                x["wallet"] for x in self.db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))
            ] or [e]
            try:
                rec = reconcile(self.db, self.data, e, wallets, self.clock())
                n += 1
                if rec["verdict"] == "bad":
                    prev = self.db.one(
                        "SELECT verdict FROM wallet_reconcile WHERE entity_id=? AND computed_at<? "
                        "ORDER BY computed_at DESC LIMIT 1",
                        (e, rec["computed_at"]),
                    )
                    if not prev or prev["verdict"] != "bad":
                        from .scoring.scorer import short

                        self.db.notify(
                            "whale_status",
                            f"Кит {short(e)}: расхождение с Polymarket",
                            "Наш расчёт его прибыли заметно отличается — оценка может быть неточной",
                            e,
                            self.clock(),
                        )
            except Exception as exc:
                log.warning("сверка %s: %s", e, exc)
        return n

    def _plugin_job(self, name: str):
        for job in self.jobs:
            if job.name == name:
                return job.fn()
        return None

    def job_kalshi(self):
        """Открытые рынки Kalshi в локальный кэш — теперь задача плагина «kalshi»."""
        return self._plugin_job("kalshi")

    def job_odds(self):
        """Коэффициенты букмекеров — задача плагина «bookmakers» (нужен WF_ODDS_API_KEY)."""
        return self._plugin_job("odds")

    def _prepare_scoring(self) -> tuple[bool, list[str] | None]:
        """Позиции → антисибил → игроки (быстрые шаги). (есть ли кого считать, игроки или None — все)."""
        scope = self._scope()
        if scope is not None and not scope:
            return False, None  # никого не отслеживаем — считать нечего
        build_positions(self.db, scope)
        sy = run_antisybil(self.db, self.p, wallets=scope)
        assign_entities(self.db)
        migrate_watchlist(self.db)  # объединённые кошельки — один кит в списке
        log.info("антисибил: %s", sy)
        if scope is None:
            return True, None
        ph = ",".join("?" for _ in scope)
        ents = sorted(
            {
                r["entity_id"]
                for r in self.db.query(f"SELECT DISTINCT entity_id FROM positions WHERE wallet IN ({ph})", scope)
            }
        )
        return bool(ents), ents

    def job_score(self):
        """Полный пересчёт раз в сутки: все показатели, зависящие от даты (окно 90 дней, активность), свежие."""
        ok, ents = self._prepare_scoring()
        if not ok:
            return None
        res = run_incremental(self.db, self.p, now=self.clock(), entities=ents, full=True, add_new=ents is None)
        log.info("скоринг (полный): %s", res)
        return res["status"]

    def job_score_changes(self):
        """Частый быстрый пересчёт: только киты, у которых появились сделки, исходы рынков или изменился состав
        кошельков. Остальные не трогаются (их оценки обновит суточный полный пересчёт)."""
        if not self.db.scalar("SELECT COUNT(*) AS n FROM score_inputs", default=0):
            return None  # первый полный пересчёт ещё не прошёл
        ok, ents = self._prepare_scoring()
        if not ok:
            return None
        res = run_incremental(self.db, self.p, now=self.clock(), entities=ents, full=False, add_new=False)
        if res["scored"]:
            log.info("скоринг (изменившиеся): %s", res)
        return {"scored": res["scored"], "skipped": res["skipped"]}

    def job_housekeeping(self):
        now = self.clock()
        cut = now - 30 * DAY
        self.db.execute("DELETE FROM job_runs WHERE started_at<?", (now - 7 * DAY,))
        self.db.execute("DELETE FROM orderbook_snapshots WHERE ts<? AND trigger='schedule'", (now - 90 * DAY,))
        # история оценок растёт на (число игроков × дни): старше 30 дней храним только последнюю оценку
        for t in ("wallet_scores", "wallet_category_scores", "wallet_flags"):
            self.db.execute(
                f"DELETE FROM {t} WHERE computed_at<? AND computed_at < "
                f"(SELECT MAX(x.computed_at) FROM {t} x WHERE x.entity_id={t}.entity_id)",
                (cut,),
            )
        self.db.execute("DELETE FROM notifications WHERE ts<? AND is_read=1", (now - 180 * DAY,))
        # сроки хранения (настройка «retention»): игроки не из списка, стаканы, новости, кэш площадок, старые рынки…
        from . import retention

        removed = retention.cleanup(self.db, now=now)
        space = retention.compact(self.db)
        log.info("чистка базы: удалено %s, место %s", removed, space)
        self.db.set_state("retention_last", {"ts": int(now), "removed": removed, "space": space})
        return {"removed": sum(v for v in removed.values() if isinstance(v, int)), **space}

    def job_backup(self, keep: int = 7):
        """Резервная копия SQLite раз в сутки (без остановки работы), хранится keep последних."""
        if self.db.kind != "sqlite" or self.db.path == ":memory:":
            return None
        import sqlite3
        from datetime import datetime

        folder = os.environ.get("WF_BACKUP_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(self.db.path)), "backups"
        )
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.fromtimestamp(self.clock(), tz=UTC).strftime("%Y%m%d")
        dst_path = os.path.join(folder, f"whalefollow-{stamp}.db")
        dst = sqlite3.connect(dst_path)
        try:
            src = sqlite3.connect(self.db.path)
            try:
                src.backup(dst)  # согласованная копия, пока приложение работает
            finally:
                src.close()
            dst.execute("PRAGMA journal_mode=DELETE")  # копия — обычный самостоятельный файл, без -wal
        finally:
            dst.close()
        old = sorted(f for f in os.listdir(folder) if f.startswith("whalefollow-") and f.endswith(".db"))
        for f in old[:-keep]:
            for suffix in ("", "-wal", "-shm", "-journal"):
                if os.path.exists(os.path.join(folder, f + suffix)):
                    os.remove(os.path.join(folder, f + suffix))
        return dst_path

    def job_rotation(self):
        """Расписание «кто в форме» для стратегии «Ротация по форме» — только если она включена."""
        from . import plugins
        from . import rotation as rot
        from .players import Players

        reg = plugins.registry()
        p = reg.get("rotation_form")
        if p is None or self.data is None or not reg.is_enabled(p, plugins.user_settings(self.db)):
            return None
        wl = tracked_entities(self.db)
        if not wl:
            return None
        rule = rot.FormRule.from_dict(self.db.get_setting("rotation_rule") or {})
        now = int(self.clock())
        res = rot.simulate_rotation(
            rot.load_whales(self.db, Players(self.data, self.gamma, self.clob), wl), rule, now=now
        )
        sched = rot.schedule_from(res)
        if sched:
            self.db.set_state("rotation_schedule", {"computed_ts": now, "items": sched})
        return len(sched)

    def job_event_watch(self):
        """«Следить» в модуле «События»: каждое событие из списка — раз в 15 мин (досье, модель, совет, уведомление)."""
        from .decision.risk import RiskManager
        from .eventhub import advisor, watch
        from .eventhub.catalog import EventsCatalog
        from .plugins import user_settings

        if not self.db.scalar("SELECT COUNT(*) AS n FROM event_watch WHERE active=1", (), 0):
            return
        cat = EventsCatalog(self.gamma, self.clob, self.db)
        risk = RiskManager(self.db, self.p)
        clients = {k: v for k, v in (("data", self.data), ("gamma", self.gamma), ("clob", self.clob)) if v is not None}
        watch.check(self.db, cat, clients, lambda now: advisor.caps(self.db, risk, now), user_settings(self.db))

    def job_event_selfcheck(self):
        """Самопроверка модели событий: исходы закрытых рынков из анализов, затем λ и веса — только по прошлому."""
        from .eventhub import selfcheck

        if not self.db.scalar("SELECT COUNT(*) AS n FROM event_analyses", (), 0):
            return None
        n = selfcheck.resolve(self.db, self.gamma) if self.gamma is not None else 0
        selfcheck.learn(self.db, self.clock())
        return n

    def job_shadow(self):
        """Теневые анализы событий (данные для самообучения): порция из очереди с бюджетом времени."""
        from .eventhub import shadow
        from .eventhub.catalog import EventsCatalog
        from .plugins import user_settings

        if self.gamma is None:
            return None
        cat = EventsCatalog(self.gamma, self.clob, self.db)
        clients = {k: v for k, v in (("data", self.data), ("gamma", self.gamma), ("clob", self.clob)) if v is not None}
        return shadow.run(self.db, cat, clients, user_settings(self.db), self.clock()).get("saved")

    def job_weekly(self):
        if next(j for j in self.jobs if j.name == "weekly").runs == 0:
            return None  # первый проход сразу после старта — итогов ещё нет
        from .services import Services

        r = Services(self.db, self.p, clock=self.clock).results("week")
        sign = "↑ +" if r["pnl_usd"] > 0 else ("↓ −" if r["pnl_usd"] < 0 else "")
        self.db.notify(
            "weekly",
            f"Итоги недели: {sign}${abs(r['pnl_usd']):.0f}",
            f"Ставок: {r['bets']['made']}, пропущено сигналов: {r['skipped_signals']}",
            "",
            self.clock(),
        )
        return r["pnl_usd"]

    # ------------------------------------------------------------ запуск
    def run_due(self, group: str, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        ran = []
        for j in self.jobs:
            if j.group != group or now - j.last_run < j.interval_s:
                continue
            j.last_run = now
            try:
                j.result = j.fn()
                j.last_ok, j.last_error = True, None
                if j.alerted:
                    self.db.notify("system", f"Снова работает: {self._label(j.name)}", "", "", self.clock())
                j.fail_streak, j.alerted = 0, False
            except Exception as exc:
                j.last_ok, j.last_error = False, repr(exc)[:300]
                j.fail_streak += 1
                log.exception("задача %s упала", j.name)
                if j.fail_streak >= self.ALERT_AFTER and not j.alerted:
                    j.alerted = True
                    self.db.notify(
                        "system",
                        f"Не работает: {self._label(j.name)}",
                        f"{j.fail_streak} сбоя подряд. Последняя ошибка: {j.last_error[:160]}",
                        "",
                        self.clock(),
                    )
            j.runs += 1
            ran.append(j.name)
            if self._stop.is_set():
                break
        if ran:
            with self._state_lock:
                self.db.set_state(
                    self.state_key,
                    {
                        j.name: {
                            "last_run": int(j.last_run),
                            "ok": j.last_ok,
                            "error": j.last_error,
                            "runs": j.runs,
                            "interval_s": j.interval_s,
                        }
                        for j in self.jobs
                        if j.group in self.groups
                    },
                )
        return ran

    GROUPS = ("fast", "books", "slow", "heavy")
    ALERT_AFTER = 3  # уведомление после стольких сбоев подряд (и одно — когда задача снова заработала)

    @staticmethod
    def _label(name: str) -> str:
        from .services import Services

        return Services.JOB_LABELS.get(name, name)

    def _loop(self, group: str) -> None:
        if group == "heavy":
            self._slow_ready.wait()  # сначала данные, потом расчёт
        while not self._stop.is_set():
            self.run_due(group)
            if group == "slow":
                self._slow_ready.set()
            self._stop.wait(1.0)

    def start(self) -> None:
        if "slow" not in self.groups:
            self._slow_ready.set()
        for g in self.groups:
            threading.Thread(target=self._loop, args=(g,), name=f"wf-{g}", daemon=True).start()
        log.info("планировщик запущен")

    def stop(self) -> None:
        self._stop.set()
        self._slow_ready.set()


STATE_KEYS = ("scheduler", "scheduler:realtime", "scheduler:worker")


def scheduler_state(db: DB) -> dict:
    """Состояние задач всех процессов: по каждой задаче — запись с самым свежим запуском
    (после перехода с «всё в одном» на три процесса старая запись не мешает)."""
    out: dict = {}
    for key in STATE_KEYS:
        for name, rec in (db.get_state(key, {}) or {}).items():
            if name not in out or (rec.get("last_run") or 0) >= (out[name].get("last_run") or 0):
                out[name] = rec
    return out


def wait_forever() -> None:
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
