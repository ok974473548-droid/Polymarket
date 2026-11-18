"""Модели запросов и ответов API (pydantic) — по ним строится документация /docs.

Модели запросов мягкие: необязательные поля, лишние поля игнорируются — как и раньше, обработчики сами
проверяют смысл (например, «нужен entity_id») и отвечают понятной ошибкой 400.
Модели ответов описывают главные поля экранов; остальные поля проходят как есть (extra="allow").
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- запросы
class LoginIn(_In):
    password: str = ""


class ConfirmIn(_In):
    size_usd: float | None = Field(None, description="Сумма ставки, $ (по умолчанию — предложенная)")


class ExitIn(_In):
    action: str | None = Field(None, description="hold — держать, sell — продать")


class AddWhaleIn(_In):
    entity_id: str | None = None
    mode: str = Field("watch", description="copy | watch")


class PatchWhaleIn(_In):
    mode: str | None = None
    category: str | None = None
    enabled: bool | None = None


class CheckIn(_In):
    address: str | None = Field(None, description="Адрес кошелька, ссылка на профиль или имя")
    wait: bool = Field(False, description="Ждать результат (для скриптов и тестов)")


class AddPlayersIn(_In):
    wallets: list[str] | None = None
    wallet: str | None = None
    mode: str = "watch"


class OnboardingIn(_In):
    jurisdiction_ok: bool = False
    risk_ack: bool = False
    paper: bool = True


class TuneIn(_In):
    edge_threshold: float = 0


class SettingsIn(BaseModel):
    """Любые поля экрана «Настройки»; проверяет их сервис и возвращает ошибки по полям."""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------- ответы
class ErrorOut(BaseModel):
    error: str
    auth: bool | None = None


class AuthOut(BaseModel):
    required: bool
    ok: bool


class OkOut(_Out):
    ok: bool


class StatusOut(_Out):
    ok: bool
    agent_ok: bool
    stopped: bool
    mode: str | None = None
    onboarded: bool
    need_decision: int
    unread: int
    data_age_s: float | None = None


class ItemsOut(_Out):
    items: list[dict[str, Any]] = []


class DashboardOut(_Out):
    balance_usd: float | None = None
    pending: list[dict[str, Any]] = []


class CheckJobOut(_Out):
    job_id: str | None = None
    state: str | None = None


class SettingsOut(_Out):
    settings: dict[str, Any] = {}
    errors: dict[str, Any] = {}


class StopOut(_Out):
    stopped: bool


class DiagnosticsOut(_Out):
    report: str | None = None
    checks: list[dict[str, Any]] | None = None


Period = Literal["week", "month", "all"]
