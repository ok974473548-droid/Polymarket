"""Встроенный HTTP-сервер (только стандартная библиотека) и адаптер к FastAPI."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from ..execution.paper import LiveTradingDisabled
from .routes import HttpError, build_routes, dispatch

log = logging.getLogger(__name__)

WEB_DIR = Path(str(resources.files("whalefollow").joinpath("web")))
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
PUBLIC_API = {("GET", "/api/auth"), ("POST", "/api/login"), ("POST", "/api/logout")}
SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"),
                    ("Referrer-Policy", "no-referrer"))


def _static_target(path: str) -> Path | None:
    rel = path.lstrip("/") or "index.html"
    target = (WEB_DIR / rel).resolve()
    if not target.is_relative_to(WEB_DIR.resolve()):
        return None                                   # попытка выйти за пределы папки
    if not target.is_file():
        if "." in rel.rsplit("/", 1)[-1]:
            return None
        target = WEB_DIR / "index.html"
    return target


def static_file(path: str) -> tuple[bytes, str] | None:
    """Файл интерфейса по пути запроса. Неизвестный путь без расширения → index.html (роутинг на клиенте)."""
    target = _static_target(path)
    if target is None:
        return None
    return target.read_bytes(), CTYPES.get(target.suffix, "application/octet-stream")


GZIP_MIN = 1024                                       # меньшие ответы не сжимаем: выигрыша нет
COMPRESSIBLE = ("text/", "application/json", "image/svg+xml")
_static_cache: dict[str, tuple] = {}
_static_lock = threading.Lock()


def static_entry(path: str) -> dict | None:
    """Файл интерфейса из памяти: содержимое, сжатая копия и метка версии (ETag).
    Файл перечитывается, только если изменился на диске."""
    target = _static_target(path)
    if target is None:
        return None
    st = target.stat()
    key = str(target)
    with _static_lock:
        hit = _static_cache.get(key)
        if hit and hit[0] == (st.st_mtime_ns, st.st_size):
            return hit[1]
    data = target.read_bytes()
    ctype = CTYPES.get(target.suffix, "application/octet-stream")
    entry = {"data": data, "ctype": ctype, "etag": '"' + hashlib.sha1(data).hexdigest()[:16] + '"',
             "gz": gzip.compress(data, 6) if ctype.startswith(COMPRESSIBLE) and len(data) >= GZIP_MIN else None}
    with _static_lock:
        _static_cache[key] = ((st.st_mtime_ns, st.st_size), entry)
    return entry


def maybe_gzip(data: bytes, ctype: str, accept_encoding: str | None) -> tuple[bytes, bool]:
    if len(data) >= GZIP_MIN and "gzip" in (accept_encoding or "").lower() and ctype.startswith(COMPRESSIBLE):
        return gzip.compress(data, 5), True
    return data, False


def gate(auth, method: str, path: str, content_type: str | None, cookie: str | None):
    """Проверки до обработки запроса к API: (статус, ответ) или None, если можно продолжать.
    1) Изменяющие запросы — только с Content-Type: application/json. Чужой сайт не может отправить такой
       запрос в ваш браузер без разрешения CORS, а разрешения мы не даём — это защита от подделки запросов.
    2) Если задан пароль — нужен действующий вход."""
    if method not in ("GET", "HEAD", "OPTIONS") and "application/json" not in (content_type or "").lower():
        return 415, {"error": "Нужен Content-Type: application/json"}
    if auth is not None and auth.required and (method, path) not in PUBLIC_API and not auth.valid(cookie):
        return 401, {"error": "Нужен вход", "auth": True}
    return None


def auth_routes(auth, method: str, path: str, body: dict | None, ip: str, cookie: str | None):
    """Вход / выход / состояние входа: (статус, ответ, Set-Cookie | None) или None, если это не они."""
    from ..auth import Auth
    if (method, path) == ("GET", "/api/auth"):
        return 200, {"required": bool(auth and auth.required), "ok": auth is None or auth.valid(cookie)}, None
    if (method, path) == ("POST", "/api/logout"):
        return 200, {"ok": True}, Auth.cookie_header("", max_age=0)
    if (method, path) == ("POST", "/api/login"):
        if auth is None or not auth.required:
            return 200, {"ok": True}, None
        if auth.blocked(ip):
            return 429, {"error": "Слишком много попыток. Подождите 10 минут."}, None
        tok = auth.login((body or {}).get("password", ""), ip)
        if not tok:
            return 401, {"error": "Неверный пароль", "auth": True}, None
        return 200, {"ok": True}, Auth.cookie_header(tok)
    return None


def make_handler(routes, auth=None, cors_origin: str | None = None, events=None):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1: браузер держит соединение открытым и не устанавливает его заново на каждый запрос
        protocol_version = "HTTP/1.1"
        timeout = 60                                    # простаивающее соединение закрывается через минуту
        wbufsize = 1 << 16                              # заголовки и тело уходят одним пакетом (сброс — после ответа)

        def setup(self):
            super().setup()
            try:                                        # без задержки Нейгла: иначе ответ по открытому
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # соединению ждёт ~40 мс
            except OSError:
                pass

        def _send(self, status: int, payload, cookie: str | None = None) -> None:
            ctype = "application/json; charset=utf-8"
            if status in (204, 304):
                data, zipped = b"", False                # у этих ответов не бывает тела
            else:
                data = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")
                data, zipped = maybe_gzip(data, ctype, self.headers.get("Accept-Encoding"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if zipped:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            if cors_origin:                            # по умолчанию чужим сайтам доступ к API не даётся
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            e = static_entry(path)
            if not e:
                return self._send(404, {"error": "Не найдено"})
            if self.headers.get("If-None-Match") == e["etag"]:
                # файл не менялся — браузер берёт свою копию, по сети уходит только заголовок
                self.send_response(304)
                self.send_header("ETag", e["etag"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            zipped = e["gz"] is not None and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            data = e["gz"] if zipped else e["data"]
            self.send_response(200)
            self.send_header("Content-Type", e["ctype"])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")      # проверять версию каждый раз (ETag), но не качать заново
            self.send_header("ETag", e["etag"])
            if zipped:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            from ..auth import Auth
            url = urlparse(self.path)
            if method == "GET" and not url.path.startswith("/api/"):
                return self._static(url.path)
            cookie = Auth.from_cookie_header(self.headers.get("Cookie"))
            # тело читается всегда, даже если запрос будет отклонён: иначе непрочитанные байты
            # на открытом (keep-alive) соединении испортят следующий запрос
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            blocked = gate(auth, method, url.path, self.headers.get("Content-Type"), cookie)
            if blocked:
                return self._send(*blocked)
            body = None
            if raw:
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return self._send(400, {"error": "Некорректный JSON"})
            ar = auth_routes(auth, method, url.path, body, self.client_address[0], cookie)
            if ar:
                return self._send(ar[0], ar[1], ar[2])
            if method == "GET" and url.path == "/api/events" and events is not None:
                return self._sse(events)
            try:
                # без общего замка: база сама разводит потоки, долгий запрос не держит остальные
                res = dispatch(routes, method, url.path, dict(parse_qsl(url.query)), body)
                self._send(200, res)
            except HttpError as exc:
                self._send(exc.status, {"error": exc.message})
            except LiveTradingDisabled as exc:
                self._send(409, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                log.exception("API error")
                self._send(500, {"error": repr(exc)})

        def _sse(self, factory) -> None:
            """Поток событий (Server-Sent Events): страница получает изменения сразу, без опроса."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")              # не копить в обратном прокси
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(b"retry: 2000\n\n")
                self.wfile.flush()
                for ev, data in factory():
                    self.wfile.write(f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                                     .encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
                pass                                                  # страница закрыта

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_OPTIONS(self):
            self._send(204, {})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def serve(services, host: str = "127.0.0.1", port: int = 8787, auth=None) -> ThreadingHTTPServer:
    if auth is None:
        from ..auth import Auth
        auth = Auth(services.db)
    events = getattr(services, "event_stream", None)
    httpd = ThreadingHTTPServer((host, port), make_handler(build_routes(services), auth, events=events))
    httpd.daemon_threads = True
    return httpd


def fastapi_app(services, auth=None):
    """Тот же набор маршрутов в FastAPI: `uvicorn 'whalefollow.api.server:app_factory' --factory`."""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from ..auth import COOKIE, Auth
    auth = auth or Auth(services.db)
    from .. import __version__
    app = FastAPI(title="WhaleFollow API", version=__version__)
    routes = build_routes(services)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH"])
    async def handle(path: str, request: Request):
        body = None
        full = "/api/" + path
        cookie = request.cookies.get(COOKIE)
        blocked = gate(auth, request.method, full, request.headers.get("content-type"), cookie)
        if blocked:
            return JSONResponse(blocked[1], status_code=blocked[0])
        if request.method in ("POST", "PUT", "PATCH"):
            raw = await request.body()
            body = json.loads(raw) if raw else None
        ar = auth_routes(auth, request.method, full, body, request.client.host if request.client else "", cookie)
        if ar:
            resp = JSONResponse(ar[1], status_code=ar[0])
            if ar[2]:
                resp.headers["Set-Cookie"] = ar[2]
            return resp
        try:
            return dispatch(routes, request.method, full, dict(request.query_params), body)
        except HttpError as exc:
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except LiveTradingDisabled as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    from fastapi.responses import Response

    @app.get("/{path:path}")
    async def web(path: str):
        found = static_file("/" + path)
        if not found:
            return JSONResponse({"error": "Не найдено"}, status_code=404)
        return Response(found[0], media_type=found[1])

    return app
