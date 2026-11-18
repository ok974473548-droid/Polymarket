"""Диспетчер уведомлений: каждое уведомление из колокольчика (DB.notify) рассылается по включённым
плагинам-каналам. У каждого канала своя очередь и свой поток — медленный канал не задерживает другие.

Что присылать, решают настройки «О чём» (сигналы, выход кита, лимиты, итоги недели, сбои) — общие для всех
каналов; «Куда» — флажки каналов (это и есть включение плагина).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import Any

from .base import Notice, NotifierPlugin
from .registry import record, registry

log = logging.getLogger(__name__)

# вид уведомления → флажок «О чём» в настройках
KIND_TO_SETTING = {
    "signal": "signals",
    "settled": "signals",
    "whale_exit": "whale_exit",
    "whale_status": "whale_exit",
    "whale_check": "whale_exit",
    "system": "system",
    "settings": "system",
    "pause": "limits",
    "stopped": "limits",
    "weekly": "weekly",
    "event_edge": "events",
}


def link(kind: str, ref: str) -> str:
    base = os.environ.get("WF_PUBLIC_URL", "http://localhost:8787").rstrip("/")
    from ..push import link as rel

    return base + rel(kind, ref)


class NotificationHub:
    def __init__(self, db: Any):
        self.db = db
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self.sent = 0

    @classmethod
    def attach_to(cls, db: Any) -> NotificationHub:
        hub = cls(db)
        db.listeners.append(hub.on_notify)
        return hub

    def wanted(self, p: NotifierPlugin, kind: str, settings: dict) -> bool:
        if p.kinds is not None and kind not in p.kinds:
            return False
        prefs = settings.get("notify") or {}
        return bool(prefs.get(KIND_TO_SETTING.get(kind, ""), False))

    def on_notify(self, n: dict) -> None:
        from . import context

        ctx = context(self.db)
        notice = Notice(
            kind=n["kind"],
            title=n["title"],
            body=n.get("body") or "",
            ref=n.get("ref") or "",
            url=link(n["kind"], n.get("ref") or ""),
            ts=int(n.get("ts") or 0),
        )
        for p in registry().active("notifier", ctx):
            assert isinstance(p, NotifierPlugin)
            if self.wanted(p, notice.kind, ctx.settings):
                self._queue(p).put(notice)

    def _queue(self, p: NotifierPlugin) -> queue.Queue:
        with self._lock:
            q = self._queues.get(p.name)
            if q is None:
                q = self._queues[p.name] = queue.Queue()
                threading.Thread(target=self._worker, args=(p, q), daemon=True, name=f"notify-{p.name}").start()
            return q

    def _worker(self, p: NotifierPlugin, q: queue.Queue) -> None:
        from . import context

        while True:
            notice = q.get()
            try:
                p.send(context(self.db), notice)
                self.sent += 1
                if p.name != "telegram":  # итог отправки Telegram пишет сам бот (у него своя очередь)
                    record(self.db, p.name, ok=True)
            except Exception as e:
                log.warning("уведомление %s: %s", p.name, e)
                record(self.db, p.name, ok=False, error=f"{type(e).__name__}: {e}")
            finally:
                q.task_done()

    def start_commands(self, services: Any) -> list[str]:
        """Приём команд у каналов, которые это умеют (Telegram) — только в процессе с агентом."""
        from . import context

        started = []
        ctx = context(self.db)
        for p in registry().of_kind("notifier"):  # команды работают, даже если сами уведомления выключены
            assert isinstance(p, NotifierPlugin)
            if p.available(ctx) is not None:
                continue
            try:
                p.start(ctx, services)
                started.append(p.name)
            except Exception as e:
                record(self.db, p.name, ok=False, error=f"запуск: {e}")
        return started

    def wait(self, timeout: float = 3.0) -> None:
        """Для тестов: дождаться, пока очереди опустеют."""
        import time

        t0 = time.time()
        while time.time() - t0 < timeout:
            if all(q.unfinished_tasks == 0 for q in self._queues.values()):
                return
            time.sleep(0.02)
