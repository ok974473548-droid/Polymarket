"""Процессы WhaleFollow: три роли из одного образа и прежний режим «всё в одном».

  api       — интерфейс, API, живые события (SSE). Проверки игроков ставит в очередь (таблица check_jobs).
  realtime  — агент, потоки WebSocket, сигналы, стаканы, Telegram (кнопки). Ничего тяжёлого — всегда быстрый.
  worker    — планировщик: рынки, цены, пересчёт китов, «что было бы», резервные копии; выполняет проверки игроков.
  all       — всё в одном процессе (команда `whalefollow run`, как раньше).

Процессы общаются только через базу: события для страниц — таблица live_events, живые цены — live_quotes,
состояние каждого процесса («пульс») — service_status. Падение worker не останавливает сигналы:
realtime от него не зависит.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from . import __version__
from .db import DB

log = logging.getLogger(__name__)

ROLES = ("api", "realtime", "worker", "all")
ROLE_TEXT = {
    "api": "Интерфейс и API",
    "realtime": "Сигналы и потоки",
    "worker": "Фоновые задачи",
    "all": "Всё в одном процессе",
}
HEARTBEAT_S = 5.0
ALIVE_S = 30.0  # без пульса дольше — процесс считается остановленным
STALE_CHECK_S = 120.0  # проверка игрока без движения дольше — процесс worker упал, проверка вернётся в очередь


# ---------------------------------------------------------------- пульс процессов
class Heartbeat:
    """Раз в несколько секунд пишет в service_status: процесс жив, когда работал, что у него внутри."""

    def __init__(self, db: DB, role: str, info_fn: Callable[[], dict] | None = None, interval_s: float = HEARTBEAT_S):
        self.db, self.role, self.info_fn, self.interval_s = db, role, info_fn, interval_s
        self.started = time.time()
        self._stop = threading.Event()

    def beat(self) -> None:
        info: dict = {}
        if self.info_fn is not None:
            try:
                info = self.info_fn() or {}
            except Exception as exc:
                info = {"error": repr(exc)[:200]}
        self.db.upsert(
            "service_status",
            {
                "name": self.role,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "version": __version__,
                "started_ts": self.started,
                "last_ts": time.time(),
                "info": json.dumps(info, ensure_ascii=False, default=str),
            },
            ["name"],
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat()
            except Exception as exc:
                log.warning("пульс %s: %s", self.role, exc)
            self._stop.wait(self.interval_s)

    def start(self) -> Heartbeat:
        threading.Thread(target=self._run, name=f"heartbeat-{self.role}", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


def services_state(db: DB, now: float | None = None) -> list[dict]:
    """Состояние процессов для «Диагностики»: жив ли каждый и когда последний раз работал."""
    now = time.time() if now is None else now
    try:
        rows = db.query("SELECT * FROM service_status ORDER BY name")
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            info = json.loads(r.get("info") or "{}")
        except ValueError:
            info = {}
        ago = max(0.0, now - (r.get("last_ts") or 0))
        out.append(
            {
                "name": r["name"],
                "label": ROLE_TEXT.get(r["name"], r["name"]),
                "alive": ago <= ALIVE_S,
                "last_seen_s": round(ago, 1),
                "uptime_s": round(now - (r.get("started_ts") or now)),
                "pid": r.get("pid"),
                "host": r.get("host"),
                "version": r.get("version"),
                "info": info,
            }
        )
    split = {s["name"] for s in out if s["alive"]} & {"api", "realtime", "worker"}
    if split:  # работают раздельные процессы — старая запись «всё в одном» не показывается
        out = [s for s in out if s["name"] != "all" or s["alive"]]
    return out


def service_info(db: DB, *names: str) -> dict | None:
    """Подробности живого процесса (например, состояние потоков у realtime)."""
    for s in services_state(db):
        if s["name"] in names and s["alive"]:
            return s["info"]
    return None


# ---------------------------------------------------------------- проверки игроков в процессе worker
class CheckRunner:
    """Берёт проверки игроков из очереди (таблица check_jobs) и выполняет их, не больше max_parallel сразу.
    Проверки, застрявшие в упавшем процессе, возвращаются в очередь."""

    def __init__(self, services: Any, name: str = "worker", poll_s: float = 0.5, max_parallel: int | None = None):
        from .services import DEEP_CHECKS

        self.svc, self.name, self.poll_s = services, name, poll_s
        self.max_parallel = max_parallel or DEEP_CHECKS + 2  # загрузка карточки идёт и пока ждут тяжёлые
        self._stop = threading.Event()
        self.ran = 0

    def requeue_stale(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return self.svc.db.execute_count(
            "UPDATE check_jobs SET state='queued', worker=NULL WHERE state='running' AND updated_ts<?",
            (now - STALE_CHECK_S,),
        )

    def claim(self) -> list[dict]:
        busy = len(self.svc._check_threads)
        free = self.max_parallel - busy
        if free <= 0:
            return []
        out = []
        for row in self.svc.db.query(
            "SELECT job_id FROM check_jobs WHERE state='queued' ORDER BY started LIMIT ?", (free,)
        ):
            n = self.svc.db.execute_count(
                "UPDATE check_jobs SET state='running', worker=?, updated_ts=? WHERE job_id=? AND state='queued'",
                (self.name, time.time(), row["job_id"]),
            )
            if n:  # другой исполнитель не успел забрать
                job = self.svc._load_job(row["job_id"])
                if job:
                    out.append(job)
        return out

    def run_once(self) -> int:
        jobs = self.claim()
        for job in jobs:
            job["worker"] = self.name
            self.svc.run_check_job(job, background=True)
            self.ran += 1
        return len(jobs)

    def _loop(self) -> None:
        last_requeue = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_requeue > 30:
                    last_requeue = time.time()
                    if n := self.requeue_stale():
                        log.info("проверок возвращено в очередь: %d", n)
                self.run_once()
            except Exception as exc:
                log.warning("очередь проверок: %s", exc)
            self._stop.wait(self.poll_s)

    def start(self) -> CheckRunner:
        threading.Thread(target=self._loop, name="check-runner", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- живые цены для процесса api
class QuotePublisher:
    """Лучшие цены из потока стаканов (realtime) раз в interval_s пишутся в live_quotes,
    чтобы интерфейс (api) показывал живые цены без своего WebSocket."""

    def __init__(self, db: DB, books: Any, interval_s: float = 1.0):
        self.db, self.books, self.interval_s = db, books, interval_s
        self._last: dict[str, tuple] = {}
        self._stop = threading.Event()

    def collect(self) -> list[dict]:
        rows: list[dict] = []
        c = getattr(self.books, "_client", None)
        if c is None or not c.alive(self.books.STALE_S):
            return rows
        with self.books.lock:
            items = [(t, b) for t, b in self.books.books.items() if b.get("gen") == c.generation]
            for tok, b in items:
                bid = max(b["bids"]) if b["bids"] else None
                ask = min(b["asks"]) if b["asks"] else None
                if self._last.get(tok) != (bid, ask):
                    self._last[tok] = (bid, ask)
                    rows.append({"token_id": tok, "bid": bid, "ask": ask, "ts": time.time()})
        return rows

    def publish_once(self) -> int:
        rows = self.collect()
        if rows:
            self.db.upsert_many("live_quotes", rows, ["token_id"])
        return len(rows)

    def _loop(self) -> None:
        last_touch = 0.0
        while not self._stop.is_set():
            try:
                self.publish_once()
                if time.time() - last_touch > 10:  # неизменные цены тоже «живые», пока поток работает
                    last_touch = time.time()
                    c = getattr(self.books, "_client", None)
                    if c is not None and c.alive(self.books.STALE_S) and self._last:
                        self.db.execute("UPDATE live_quotes SET ts=? WHERE ts>?", (time.time(), time.time() - 60))
            except Exception as exc:
                log.warning("живые цены: %s", exc)
            self._stop.wait(self.interval_s)

    def start(self) -> QuotePublisher:
        threading.Thread(target=self._loop, name="live-quotes", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
