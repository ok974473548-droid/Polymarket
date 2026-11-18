"""Приложение FastAPI: маршруты API, вход по паролю, поток событий (SSE) и файлы интерфейса.

Что делает сервер, кроме маршрутов:
  • изменяющие запросы к API — только с Content-Type: application/json (защита от подделки запросов с чужих
    сайтов: без разрешения CORS браузер такой запрос не отправит, а разрешения мы не даём);
  • если задан пароль (WF_UI_PASSWORD) — без входа API отвечает 401;
  • заголовки безопасности на всех ответах; CORS выключен (включается только явно, cors_origin);
  • файлы интерфейса — из памяти, со сжатой копией и ETag (повторная загрузка — ответ 304 без тела);
  • JSON-ответы больше 1 КБ сжимаются gzip.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..auth import COOKIE, Auth
from ..execution.paper import LiveTradingDisabled
from ..logs import get_logger
from . import schemas as S
from .routes import HttpError, build_router, json_response
from .static import static_entry

log = get_logger(__name__)

PUBLIC_API = {("GET", "/api/auth"), ("POST", "/api/login"), ("POST", "/api/logout")}
SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def gate(auth: Auth | None, method: str, path: str, content_type: str | None, cookie: str | None):
    """Проверки до обработки запроса к API: (статус, ответ) или None, если можно продолжать."""
    if method not in SAFE_METHODS and "application/json" not in (content_type or "").lower():
        return 415, {"error": "Нужен Content-Type: application/json"}
    if auth is not None and auth.required and (method, path) not in PUBLIC_API and not auth.valid(cookie):
        return 401, {"error": "Нужен вход", "auth": True}
    return None


class GuardMiddleware:
    """Чистый ASGI-слой (без буферизации ответа — поток событий идёт без задержек):
    заголовки безопасности и CORS на всех ответах, проверка входа и Content-Type для /api."""

    def __init__(self, app: ASGIApp, auth: Auth | None, cors_origin: str | None = None):
        self.app, self.auth, self.cors_origin = app, auth, cors_origin

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = list(SECURITY_HEADERS)
        if self.cors_origin:  # по умолчанию чужим сайтам доступ к API не даётся
            extra += [
                (b"access-control-allow-origin", self.cors_origin.encode()),
                (b"access-control-allow-methods", b"GET, POST, PUT, PATCH, OPTIONS"),
                (b"access-control-allow-headers", b"Content-Type"),
            ]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        path, method = scope["path"], scope["method"]
        if path.startswith("/api/") and method != "OPTIONS":
            headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
            cookie = Auth.from_cookie_header(headers.get("cookie"))
            blocked = gate(self.auth, method, path, headers.get("content-type"), cookie)
            if blocked:
                # тело дочитываем: иначе непрочитанные байты на открытом (keep-alive) соединении
                # испортят следующий запрос
                more = True
                while more:
                    msg = await receive()
                    more = msg.get("more_body", False) and msg["type"] == "http.request"
                resp = json_response(None, blocked[1], status=blocked[0])
                await resp(scope, receive, send_with_headers)
                return
        await self.app(scope, receive, send_with_headers)


def create_app(
    services: Any,
    auth: Auth | None = None,
    cors_origin: str | None = None,
    events: Callable[..., Iterator[tuple[str, dict]]] | None = None,
    stop: threading.Event | None = None,
) -> FastAPI:
    """Приложение для uvicorn. events — генератор событий для /api/events (по умолчанию services.event_stream)."""
    auth = auth if auth is not None else Auth(services.db)
    events = events if events is not None else getattr(services, "event_stream", None)
    stop = stop or threading.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import anyio.to_thread

        # пул потоков для синхронных обработчиков: экраны, проверки игроков и поток событий одновременно
        anyio.to_thread.current_default_thread_limiter().total_tokens = 64
        yield
        stop.set()

    app = FastAPI(
        title="WhaleFollow API",
        version=__version__,
        lifespan=lifespan,
        description="API интерфейса WhaleFollow. Ставки только виртуальные.",
    )
    app.state.stop = stop

    # ---------------------------------------------------------------- ошибки → {"error": ...}
    @app.exception_handler(HttpError)
    async def _http_error(request: Request, exc: HttpError) -> Response:
        return json_response(request, {"error": exc.message}, status=exc.status)

    @app.exception_handler(LiveTradingDisabled)
    async def _live(request: Request, exc: LiveTradingDisabled) -> Response:
        return json_response(request, {"error": str(exc)}, status=409)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> Response:
        errs = exc.errors()
        if any(e.get("type") == "json_invalid" for e in errs):
            return json_response(request, {"error": "Некорректный JSON"}, status=400)
        where = "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ())[1:])}: {e.get('msg')}" for e in errs[:3])
        return json_response(request, {"error": f"Неверный запрос — {where}"}, status=400)

    @app.exception_handler(StarletteHTTPException)
    async def _starlette(request: Request, exc: StarletteHTTPException) -> Response:
        code = exc.status_code
        if code in (404, 405) and request.url.path.startswith("/api/"):
            return json_response(request, {"error": f"Нет маршрута {request.method} {request.url.path}"}, status=404)
        return json_response(request, {"error": str(exc.detail)}, status=code)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> Response:
        log.exception("api_error", path=request.url.path, method=request.method)
        return json_response(request, {"error": repr(exc)}, status=500)

    # ---------------------------------------------------------------- вход
    @app.get("/api/auth", responses={200: {"model": S.AuthOut}}, tags=["Вход"])
    def auth_state(request: Request) -> Response:
        cookie = request.cookies.get(COOKIE)
        return json_response(
            request, {"required": bool(auth and auth.required), "ok": auth is None or auth.valid(cookie)}
        )

    @app.post(
        "/api/login",
        responses={200: {"model": S.OkOut}, 401: {"model": S.ErrorOut}, 429: {"model": S.ErrorOut}},
        tags=["Вход"],
    )
    def login(request: Request, body: S.LoginIn | None = None) -> Response:
        if auth is None or not auth.required:
            return json_response(request, {"ok": True})
        ip = request.client.host if request.client else ""
        if auth.blocked(ip):
            return json_response(request, {"error": "Слишком много попыток. Подождите 10 минут."}, status=429)
        tok = auth.login(body.password if body else "", ip)
        if not tok:
            return json_response(request, {"error": "Неверный пароль", "auth": True}, status=401)
        return json_response(request, {"ok": True}, cookie=Auth.cookie_header(tok))

    @app.post("/api/logout", responses={200: {"model": S.OkOut}}, tags=["Вход"])
    def logout(request: Request) -> Response:
        return json_response(request, {"ok": True}, cookie=Auth.cookie_header("", max_age=0))

    # ---------------------------------------------------------------- живые события (SSE)
    @app.get(
        "/api/events",
        tags=["Состояние"],
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}, "description": "События hello / change / price / ping"}},
    )
    def sse(request: Request) -> Response:
        if events is None:
            raise HttpError(404, "Поток событий недоступен")

        def gen() -> Iterator[bytes]:
            yield b"retry: 2000\n\n"
            for ev, data in events(stop.is_set):
                yield f"event: {ev}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode()

        return StreamingResponse(
            gen(),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.include_router(build_router(services))

    # ---------------------------------------------------------------- интерфейс
    @app.options("/{path:path}", include_in_schema=False)
    async def options(path: str) -> Response:
        return Response(status_code=204)

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def web(request: Request, path: str) -> Response:
        if path.startswith("api/"):
            raise HttpError(404, f"Нет маршрута {request.method} /{path}")
        e = static_entry("/" + path)
        if not e:
            raise HttpError(404, "Не найдено")
        if request.headers.get("if-none-match") == e["etag"]:
            # файл не менялся — браузер берёт свою копию, по сети уходит только заголовок
            return Response(status_code=304, headers={"ETag": e["etag"]})
        zipped = e["gz"] is not None and "gzip" in request.headers.get("accept-encoding", "").lower()
        headers = {"Cache-Control": "no-cache", "ETag": e["etag"], "Vary": "Accept-Encoding"}
        if zipped:
            headers["Content-Encoding"] = "gzip"
        return Response(e["gz"] if zipped else e["data"], media_type=e["ctype"], headers=headers)

    app.add_middleware(GuardMiddleware, auth=auth, cors_origin=cors_origin)
    return app
