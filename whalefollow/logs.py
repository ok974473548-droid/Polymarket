"""Журнал (structlog): JSON в Docker, читаемые строки в консоли; по желанию — Sentry.

Формат выбирается переменной WF_LOG_FORMAT=json|console; если не задана — JSON внутри Docker,
иначе консольный вид. Обычные `logging.getLogger(...)` во всех модулях проходят через тот же вывод,
поэтому старые сообщения выглядят так же, как новые структурные.

Sentry включается, только если задан SENTRY_DSN и установлен пакет sentry-sdk
(`pip install -e ".[sentry]"`). Личные данные в Sentry не отправляются.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

_configured = False


def _default_format() -> str:
    fmt = (os.environ.get("WF_LOG_FORMAT") or "").strip().lower()
    if fmt in ("json", "console"):
        return fmt
    return "json" if os.path.exists("/.dockerenv") else "console"


def setup_logging(level: int | str = logging.INFO, fmt: str | None = None, stream: Any = None) -> None:
    """Настраивает structlog и стандартный logging один раз за процесс (повторный вызов — перенастройка)."""
    global _configured
    fmt = fmt or _default_format()
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=fmt == "json"),
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.PositionalArgumentsFormatter(),
                    structlog.processors.StackInfoRenderer(),
                    structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    renderer: Any = (structlog.processors.JSONRenderer(ensure_ascii=False) if fmt == "json"
                     else structlog.dev.ConsoleRenderer(colors=getattr(stream or sys.stderr, "isatty", lambda: False)()))
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.PositionalArgumentsFormatter()],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info if fmt == "json" else _keep,
                    renderer],
    )
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_whalefollow", False):
            root.removeHandler(h)
    handler._whalefollow = True                         # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "hpack", "websockets", "uvicorn.access", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
    _configured = True
    _setup_sentry()


def _keep(_: Any, __: str, event: dict) -> dict:
    return event


def _setup_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning("SENTRY_DSN задан, но пакет sentry-sdk не установлен — Sentry выключен")
        return
    from . import __version__
    sentry_sdk.init(dsn=dsn, release=f"whalefollow@{__version__}", send_default_pii=False,
                    traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or 0),
                    environment=os.environ.get("SENTRY_ENVIRONMENT", "production"))


def get_logger(name: str | None = None) -> Any:
    return structlog.stdlib.get_logger(name)
