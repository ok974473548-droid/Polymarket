"""Лимиты и стопы (ТЗ 10.4). Работают поверх решения и могут только уменьшить ставку."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..config import DEFAULT_SETTINGS, Params
from ..db import DB, now_ts
from ..execution.paper import Portfolio

MIN_ORDER_USD = 1.0


def _day_start(ts: int) -> int:
    d = datetime.fromtimestamp(ts, tz=UTC)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _month_start(ts: int) -> int:
    d = datetime.fromtimestamp(ts, tz=UTC)
    return int(datetime(d.year, d.month, 1, tzinfo=UTC).timestamp())


@dataclass
class RiskResult:
    size_usd: float
    reasons: list[str] = field(default_factory=list)
    blocked: bool = False


class RiskManager:
    def __init__(self, db: DB, params: Params):
        self.db = db
        self.p = params
        self.pf = Portfolio(db)

    # --- настройки пользователя
    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        for k in DEFAULT_SETTINGS:
            v = self.db.get_setting(k)
            if v is not None:
                # вложенные настройки дополняются новыми ключами по умолчанию (например, новые источники)
                s[k] = (
                    {**DEFAULT_SETTINGS[k], **v} if isinstance(v, dict) and isinstance(DEFAULT_SETTINGS[k], dict) else v
                )
        return s

    @property
    def bankroll(self) -> float:
        return float(self.settings()["bankroll_usd"])

    # --- состояние
    def stopped(self) -> bool:
        return bool(self.db.get_state("kill_switch", False))

    def set_stopped(self, value: bool) -> None:
        self.db.set_state("kill_switch", bool(value))
        if value:
            self.db.notify(
                "stopped", "Система остановлена", "Новые ставки не создаются. Открытые остаются до закрытия рынков."
            )

    def pause_reason(self, now: int | None = None) -> str | None:
        now = now or now_ts()
        paused = self.db.get_state("paused", None)
        if not paused:
            return None
        if paused.get("until") and now >= paused["until"]:
            self.db.set_state("paused", None)
            return None
        return paused.get("reason")

    def check_stops(self, now: int | None = None) -> str | None:
        """Дневной и месячный стоп по убытку. Возвращает причину паузы или None."""
        now = now or now_ts()
        s = self.settings()
        bank = float(s["bankroll_usd"])
        eq_now = self.pf.equity(bank)
        for key, start, frac, until in (
            (
                "day",
                _day_start(now),
                float(s.get("daily_loss_stop_frac", self.p.daily_stop_frac)),
                _day_start(now) + 86400,
            ),
            ("month", _month_start(now), float(s.get("monthly_loss_stop_frac", self.p.monthly_stop_frac)), None),
        ):
            mark = self.db.get_state(f"equity_{key}_start")
            if not mark or mark.get("ts") != start:
                mark = {"ts": start, "equity": eq_now}
                self.db.set_state(f"equity_{key}_start", mark)
            base = max(mark["equity"], 1e-9)
            loss = (mark["equity"] - eq_now) / base
            if loss >= frac:
                reason = "daily_loss_stop" if key == "day" else "monthly_loss_stop"
                if self.pause_reason(now) != reason:
                    self.db.set_state("paused", {"reason": reason, "until": until, "ts": now})
                    title = "Система на паузе" if key == "day" else "Пауза: убыток за месяц"
                    body = (
                        f"Убыток за {'сегодня' if key == 'day' else 'месяц'} достиг {loss:.0%} — сработал защитный лимит."
                        + (" Работа продолжится завтра." if key == "day" else " Нужна ручная проверка.")
                    )
                    self.db.notify("pause", title, body)
                return reason
        return None

    # --- лимиты на ставку
    def cap(self, size_usd: float, *, market: dict, entity_id: str, now: int | None = None) -> RiskResult:
        now = now or now_ts()
        if self.stopped():
            return RiskResult(0.0, ["stopped"], True)
        reason = self.pause_reason(now) or self.check_stops(now)
        if reason:
            return RiskResult(0.0, [reason], True)
        s = self.settings()
        bank = float(s["bankroll_usd"])
        reasons: list[str] = []
        caps = {"max_bet": min(self.p.max_bet_frac * bank, float(s["max_bet_usd"]))}
        spent_today = self.pf.spent_since(_day_start(now))
        caps["daily_limit"] = float(s["daily_limit_usd"]) - spent_today

        open_pos = self.pf.open_positions()
        exposure = {"total": 0.0, "event": 0.0, "category": 0.0}
        n_entity = 0
        for pos in open_pos:
            m = (
                self.db.one(
                    "SELECT m.event_id, m.category FROM tokens t JOIN markets m ON m.market_id=t.market_id "
                    "WHERE t.token_id=?",
                    (pos["token_id"],),
                )
                or {}
            )
            exposure["total"] += pos["cost"]
            if m.get("event_id") and m.get("event_id") == market.get("event_id"):
                exposure["event"] += pos["cost"]
            if m.get("category") == market.get("category"):
                exposure["category"] += pos["cost"]
            sig = self.db.one("SELECT entity_id FROM signals WHERE signal_id=?", (pos["signal_id"],))
            if sig and sig["entity_id"] == entity_id:
                n_entity += 1
        caps["event"] = self.p.max_cluster_frac * bank - exposure["event"]
        caps["category"] = self.p.max_category_frac * bank - exposure["category"]
        caps["exposure"] = self.p.max_exposure_frac * bank - exposure["total"]
        caps["cash"] = self.pf.cash(bank)
        if n_entity >= self.p.max_open_per_entity:
            return RiskResult(0.0, ["entity_limit"], True)
        allowed = size_usd
        for name, cap in caps.items():
            if cap < allowed:
                allowed = max(cap, 0.0)
                reasons.append(f"capped_{name}")
        if allowed < MIN_ORDER_USD:
            blocked_by = reasons[-1].replace("capped_", "") if reasons else "limit"
            return RiskResult(0.0, reasons + [f"limit_{blocked_by}"], True)
        return RiskResult(round(allowed, 2), reasons, False)
