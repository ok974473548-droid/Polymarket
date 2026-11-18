"""Сетевые клиенты (httpx): общий пул соединений, HTTP/2, таймауты, повторы на 429/5xx с учётом Retry-After.

Один клиент на процесс (`client()`): все модули — Polymarket, новости, Kalshi, Telegram, диагностика — ходят
через общий пул, поэтому защищённое соединение с каждым сервисом открывается один раз и переиспользуется.
HTTP/2 позволяет нескольким потокам слать запросы к одному сервису по одному соединению.
Прокси (HTTPS_PROXY / NO_PROXY) и сертификаты (SSL_CERT_FILE) берутся из окружения, как и раньше.

Для асинхронного кода есть `async_client()` и `aget_json()` с теми же правилами повторов.
"""

from __future__ import annotations

import asyncio
import email.utils
import os
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__

USER_AGENT = f"whalefollow/{__version__}"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=90.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0

# Ошибки сети и ответов — для `except`: httpx.HTTPError покрывает и обрыв, и неуспешный статус
HTTPError = httpx.HTTPError
RequestError = httpx.RequestError
HTTPStatusError = httpx.HTTPStatusError
TimeoutException = httpx.TimeoutException

_lock = threading.Lock()
_client: httpx.Client | None = None


def _http2() -> bool:
    return os.environ.get("WF_HTTP2", "on").strip().lower() not in ("0", "off", "false", "no")


def make_client(
    timeout: httpx.Timeout | float = TIMEOUT, headers: dict | None = None, http2: bool | None = None
) -> httpx.Client:
    """Новый клиент с настройками WhaleFollow (обычно хватает общего `client()`)."""
    return httpx.Client(
        http2=_http2() if http2 is None else http2,
        timeout=timeout,
        limits=LIMITS,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        follow_redirects=True,
    )


def client() -> httpx.Client:
    """Общий клиент процесса (потокобезопасный)."""
    global _client
    with _lock:
        if _client is None or _client.is_closed:
            _client = make_client()
        return _client


def async_client(timeout: httpx.Timeout | float = TIMEOUT, headers: dict | None = None) -> httpx.AsyncClient:
    """Асинхронный клиент с теми же настройками (создаётся в своём цикле событий)."""
    return httpx.AsyncClient(
        http2=_http2(),
        timeout=timeout,
        limits=LIMITS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )


def retry_after_s(resp: Any, now: float | None = None) -> float | None:
    """Сколько ждать по заголовку Retry-After (секунды или дата HTTP); None — заголовка нет."""
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw in (None, ""):
        return None
    raw = str(raw).strip()
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER_S))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return max(0.0, min(dt.timestamp() - (now if now is not None else time.time()), MAX_RETRY_AFTER_S))


class ApiError(RuntimeError):
    """Запрос не удался (после повторов) или сервис ответил ошибкой 4xx."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def backoff_delay(attempt: int, resp: Any = None, base: float = 1.0) -> float:
    """Пауза перед повтором: Retry-After, если сервис его прислал, иначе 1, 2, 4… с."""
    ra = retry_after_s(resp) if resp is not None else None
    return ra if ra is not None else base * (2**attempt)


def get_json(
    session: Any,
    url: str,
    params: dict | None = None,
    timeout: Any = None,
    retries: int = 4,
    before: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[str, float], None] | None = None,
) -> Any:
    """GET → JSON с повторами на 429/5xx и обрывы сети. Ошибки 4xx (кроме 429) — сразу ApiError."""
    last: Exception | None = None
    for attempt in range(retries):
        resp = None
        try:
            if before:
                before()
            resp = session.get(url, params=params, timeout=timeout)
            code = resp.status_code
            if code in RETRY_STATUSES:
                raise ApiError(f"HTTP {code}", code)
            if code >= 400:
                raise ApiError(f"HTTP {code}: {(resp.text or '')[:200]}", code)
            return resp.json()
        except (httpx.HTTPError, ApiError, OSError, ValueError) as exc:
            last = exc
            if isinstance(exc, ApiError) and exc.status is not None and exc.status not in RETRY_STATUSES:
                raise ApiError(f"GET {url} failed: {exc}", exc.status) from exc
            if attempt == retries - 1:
                break
            delay = backoff_delay(attempt, resp)
            if on_retry:
                on_retry(str(exc), delay)
            sleep(delay)
    status = last.status if isinstance(last, ApiError) else None
    raise ApiError(f"GET {url} failed: {last}", status) from last


async def aget_json(
    session: httpx.AsyncClient,
    url: str,
    params: dict | None = None,
    timeout: Any = None,
    retries: int = 4,
    before: Callable[[], Any] | None = None,
) -> Any:
    """Асинхронный вариант `get_json` (те же правила повторов)."""
    last: Exception | None = None
    for attempt in range(retries):
        resp = None
        try:
            if before:
                r = before()
                if asyncio.iscoroutine(r):
                    await r
            resp = await session.get(url, params=params, timeout=timeout if timeout is not None else TIMEOUT)
            code = resp.status_code
            if code in RETRY_STATUSES:
                raise ApiError(f"HTTP {code}", code)
            if code >= 400:
                raise ApiError(f"HTTP {code}: {(resp.text or '')[:200]}", code)
            return resp.json()
        except (httpx.HTTPError, ApiError, OSError, ValueError) as exc:
            last = exc
            if isinstance(exc, ApiError) and exc.status is not None and exc.status not in RETRY_STATUSES:
                raise ApiError(f"GET {url} failed: {exc}", exc.status) from exc
            if attempt == retries - 1:
                break
            await asyncio.sleep(backoff_delay(attempt, resp))
    status = last.status if isinstance(last, ApiError) else None
    raise ApiError(f"GET {url} failed: {last}", status) from last
