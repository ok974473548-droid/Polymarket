"""Запуск сервера интерфейса: FastAPI под uvicorn.

`serve()` возвращает объект с прежним интерфейсом (`serve_forever()`, `shutdown()`, `server_address`):
порт открывается сразу (port=0 — свободный, удобно в тестах), обслуживание — в вызвавшем потоке.
"""

from __future__ import annotations

import os
import socket
import threading
from typing import Any

import uvicorn

from ..auth import Auth
from .app import create_app


class UIServer:
    def __init__(self, app: Any, host: str, port: int, stop: threading.Event):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.set_inheritable(True)
        self.sock = sock
        self.server_address: tuple[str, int] = sock.getsockname()[:2]
        self.stop = stop
        self.app = app
        cfg = uvicorn.Config(
            app,
            log_config=None,
            access_log=False,
            server_header=False,
            timeout_keep_alive=60,  # браузер держит соединение, как раньше
            timeout_graceful_shutdown=3,
            lifespan="on",
            http=os.environ.get("WF_HTTP_IMPL", "auto"),
        )
        self.server = uvicorn.Server(cfg)
        self._running = threading.Event()
        self._done = threading.Event()

    def serve_forever(self) -> None:
        self._running.set()
        try:
            self.server.run(sockets=[self.sock])
        finally:
            self._done.set()

    def shutdown(self, timeout: float = 10.0) -> None:
        self.stop.set()  # потоки событий (SSE) закрываются сами
        self.server.should_exit = True
        if self._running.is_set():
            self._done.wait(timeout)
        try:
            self.sock.close()
        except OSError:
            pass


def serve(
    services: Any, host: str = "127.0.0.1", port: int = 8787, auth: Auth | None = None, cors_origin: str | None = None
) -> UIServer:
    if auth is None:
        auth = Auth(services.db)
    stop = threading.Event()
    app = create_app(services, auth, cors_origin=cors_origin or os.environ.get("WF_CORS_ORIGIN") or None, stop=stop)
    return UIServer(app, host, port, stop)
