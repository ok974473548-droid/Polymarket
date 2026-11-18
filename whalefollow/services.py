"""Бэкенд под экраны интерфейса: простые ответы без сложных метрик.

Каждая функция возвращает JSON-совместимый dict для одного экрана макета
(Сигналы, Карточка сигнала, Мои ставки, Киты, Профиль кита, Добавить кита,
Итоги, Настройки, Первый запуск, состояния и уведомления).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime

import numpy as np

from . import categories as _cats
from . import fees, reconcile, tracked
from .config import DEFAULT_SETTINGS, Params
from .db import DB, now_ts
from .decision.risk import RiskManager, _day_start
from .estimator.calibration import load_calibrator
from .execution.paper import Portfolio
from .ingest import pipeline
from .players import Players
from .positions import build_positions
from .scoring import scorer

DAY = 86400
log = logging.getLogger(__name__)

# Причины решений простыми словами (для «Недавние решения», журнала и уведомлений)
REASONS_RU = {
    "too_late": "Сигнал пришёл слишком поздно",
    "price_moved": "Цена уже выросла, выгода пропала",
    "slippage": "Пришлось бы покупать заметно дороже кита",
    "thin_book": "Мало предложений по хорошей цене",
    "no_book": "Нет заявок на продажу",
    "scalper": "Кит торгует на быстрых скачках, его не повторить",
    "too_soon": "Рынок закрывается слишком скоро",
    "too_long": "Деньги были бы заморожены слишком долго",
    "low_annual_return": "Выгода слишком мала для такого срока",
    "whale_hedged": "У кита есть противоположная ставка — похоже на страховку",
    "low_conviction": "Ставка кита слишком мала для него — похоже на пробную",
    "market_not_open": "Рынок уже не принимает ставки",
    "low_liquidity": "В рынке мало денег",
    "category_not_proven": "В этой теме кит ещё не доказал преимущество",
    "category_off_by_user": "Тема выключена в настройках",
    "category_off_for_whale": "Тема выключена для этого кита",
    "disagree": "Новости не подтверждают ставку кита",
    "no_estimate": "Нет независимой оценки",
    "small_edge": "Выгода слишком мала",
    "stopped": "Система остановлена",
    "daily_loss_stop": "Пауза: дневной лимит убытка",
    "monthly_loss_stop": "Пауза: месячный лимит убытка",
    "entity_limit": "Уже открыто много ставок по этому киту",
    "limit_daily_limit": "Дневной лимит исчерпан",
    "limit_max_bet": "Лимит на одну ставку",
    "limit_event": "Лимит на одно событие",
    "limit_category": "Лимит на одну тему",
    "limit_exposure": "Лимит на все открытые ставки",
    "limit_cash": "Недостаточно денег",
}
CATEGORY_RU = {k: v.lower() for k, v in _cats.LABEL_RU.items()}
STATUS_RU = {
    "approved": "Надёжный",
    "candidate": "Проверяется",
    "watch": "Проверяется",
    "excluded": "Не копируем",
    "insufficient": "Мало данных",
}
CLASS_REASON_RU = {
    "maker_arb": "Маркет-мейкер",
    "favorite_grinder": "Покупает почти решённое",
    "insider_suspect": "Нужна проверка: необычные ставки",
}


def reason_text(codes: list[str]) -> str:
    for c in codes:
        if c in REASONS_RU:
            return REASONS_RU[c]
    return "Не прошло проверку" if codes else ""


def _fmt_age(ts: int, now: int) -> str:
    d = now - ts
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < DAY:
        return f"{d // 3600} ч назад"
    return "вчера" if d < 2 * DAY else f"{d // DAY} дн назад"


def _f(v):
    return None if v is None or (isinstance(v, float) and not np.isfinite(v)) else v


CHECK_STAGES = {
    "profile": "Берём готовую статистику Polymarket",
    "trades": "Загружаем историю ставок",
    "activity": "Загружаем операции кошелька",
    "markets": "Загружаем рынки",
    "prices": "Загружаем историю цен",
    "score": "Считаем, можно ли его копировать",
}
DEEP_CHECKS = 2  # глубоких проверок одновременно (остальные ждут очереди)
CHECK_MAX_PAGES = 100  # до 100 тыс. сделок: больше — почти всегда маркет-мейкер
CHECK_PRICE_TOKENS = 300  # история цен для последних 300 рынков кита (для проверки «входит раньше рынка»)
CHECK_CACHE_S = 600  # повторная проверка того же адреса в течение 10 минут — мгновенно


def _thousands(x: float) -> str:
    return f"{x:,.0f}".replace(",", "\u00a0")


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    return one if n % 10 == 1 else (few if 2 <= n % 10 <= 4 else many)


def scheduler_state(db: DB) -> dict:
    from .scheduler import scheduler_state as _st

    return _st(db)


class Services:
    def __init__(self, db: DB, params: Params, agent=None, data=None, gamma=None, clock=None, clob=None):
        self.db, self.p, self.agent = db, params, agent
        self.data, self.gamma = data, gamma
        self.clob = clob or (getattr(agent, "clob", None) if agent is not None else None)
        self.clock = clock or now_ts
        # проверки игроков: состояние — в таблице check_jobs (видно всем процессам);
        # check_mode="local" — выполняются здесь же в фоне, "queue" — их выполняет процесс worker
        self.check_mode = "local"
        self._checks_lock = threading.Lock()
        self._check_threads: dict[str, threading.Thread] = {}
        self.risk = RiskManager(db, params)
        self.pf = Portfolio(db)
        self.players = Players(data, gamma, self.clob) if data is not None else None
        self._deep = threading.BoundedSemaphore(DEEP_CHECKS)

    # ------------------------------------------------------------ общие
    def _market_of_token(self, token_id: str) -> dict:
        return (
            self.db.one(
                "SELECT m.*, t.outcome_label FROM tokens t JOIN markets m ON m.market_id=t.market_id "
                "WHERE t.token_id=?",
                (token_id,),
            )
            or {}
        )

    def _freshness(self) -> tuple[int | None, bool]:
        """Свежесть данных: по фактической работе агента (если идёт планировщик), иначе по последним сделкам."""
        now = self.clock()
        sched = scheduler_state(self.db) or {}
        ag = sched.get("agent")
        if ag and ag.get("last_run"):
            return max(0, now - int(ag["last_run"])), ag.get("ok") is not False
        return pipeline.data_freshness_s(self.db), True

    def bootstrap(self) -> dict:
        """Прогресс первой загрузки на реальных данных."""
        sched = scheduler_state(self.db) or {}
        score = sched.get("score") or {}
        disc = sched.get("discover") or {}
        scored = bool(score.get("runs")) and score.get("ok") is not False
        whales = int(self.db.scalar("SELECT COUNT(*) AS n FROM watchlist WHERE mode='copy'", default=0))
        out = {
            "scored": scored,
            "whales": whales,
            "auto_discover": tracked.auto_discover(self.db),
            "tracked": len(tracked.tracked_entities(self.db)),
            "candidates": int(self.db.scalar("SELECT COUNT(*) AS n FROM wallet_sync", default=0)),
            "running": bool(sched),
            "error": disc.get("error") or score.get("error"),
        }
        if not scored:
            out["trades"] = int(self.db.scalar("SELECT COUNT(*) AS n FROM trades", default=0))
        return out

    def status(self) -> dict:
        fresh, agent_ok = self._freshness()
        pause = self.risk.pause_reason(self.clock())
        return {
            "ok": not self.risk.stopped() and pause is None and agent_ok and (fresh is None or fresh < 120),
            "bootstrap": self.bootstrap(),
            "agent_ok": agent_ok,
            "stopped": self.risk.stopped(),
            "paused": pause,
            "paused_text": REASONS_RU.get(pause or ""),
            "data_age_s": fresh,
            "mode": self.risk.settings()["mode"],
            "onboarded": bool(self.db.get_setting("onboarding")),
            "need_decision": int(
                self.db.scalar(
                    "SELECT COUNT(*) AS n FROM signals WHERE kind='whale_exit' AND status='pending'", default=0
                )
            ),
            "unread": int(self.db.scalar("SELECT COUNT(*) AS n FROM notifications WHERE is_read=0", default=0)),
            "ai": self.ai_mode(),
        }

    def chain_status(self) -> dict:
        from .ingest.chain import ExplorerClient

        c = ExplorerClient.from_env()
        return {
            "available": c is not None,
            "provider": c.provider if c else None,
            "wallets_checked": int(
                self.db.scalar("SELECT COUNT(*) AS n FROM chain_sync WHERE fetched_ts IS NOT NULL", default=0)
            ),
            "transfers": int(self.db.scalar("SELECT COUNT(*) AS n FROM transfers", default=0)),
            "errors": int(self.db.scalar("SELECT COUNT(*) AS n FROM chain_sync WHERE error IS NOT NULL", default=0)),
        }

    def ai_mode(self) -> dict:
        if self.agent is not None:
            return self.agent.ai_mode(self.risk.settings())
        sel = self.risk.settings().get("ai_mode", "ai")
        return {"selected": sel, "available": False, "effective": "manual"}

    # ------------------------------------------------------------ экран «Сигналы»
    def dashboard(self) -> dict:
        now = self.clock()
        s = self.risk.settings()
        bank = float(s["bankroll_usd"])
        eq = self.pf.equity(bank)
        eq30 = (
            self.pf.equity(bank, until=now - 30 * DAY)
            if self.db.scalar("SELECT COUNT(*) AS n FROM fills", default=0)
            else bank
        )
        spent = self.pf.spent_since(_day_start(now))
        pending = []
        for r in self.db.query(
            "SELECT s.*, d.size_usd FROM signals s JOIN decisions d ON d.signal_id=s.signal_id "
            "WHERE s.status='pending' AND s.kind='entry' ORDER BY s.ts DESC"
        ):
            m = self._market_of_token(r["token_id"])
            pending.append(
                {
                    "signal_id": r["signal_id"],
                    "question": m.get("question"),
                    "whale": scorer.short(r["entity_id"]),
                    "category": CATEGORY_RU.get(m.get("category"), "разное"),
                    "seconds_left": max(0, (r["expires_ts"] or now) - now),
                    "action": "Купить",
                    "size_usd": r["size_usd"],
                    "token_id": r["token_id"],
                    "whale_cents": round(r["whale_price"] * 100, 1) if r.get("whale_price") else None,
                    "side": m.get("outcome_label"),
                }
            )
        recent = []
        for r in self.db.query(
            "SELECT s.signal_id, s.token_id, s.status, d.decision, d.size_usd, d.reasons, d.user_action "
            "FROM signals s JOIN decisions d ON d.signal_id=s.signal_id WHERE s.kind='entry' "
            "AND s.status!='pending' ORDER BY s.ts DESC LIMIT 5"
        ):
            m = self._market_of_token(r["token_id"])
            codes = json.loads(r["reasons"] or "{}").get("codes", [])
            if r["status"] == "executed":
                item = {"label": "Куплено", "text": f"Куплено на ${r['size_usd']:.0f}"}
            elif r["status"] == "rejected":
                item = {"label": "Пропущено", "text": "Пропущено вами"}
            elif r["status"] == "expired":
                item = {"label": "Время вышло", "text": "Цена ушла, ставка больше не выгодна"}
            else:
                item = {"label": "Пропущено", "text": reason_text(codes)}
            recent.append({"signal_id": r["signal_id"], "question": m.get("question"), **item})
        return {
            "balance_usd": round(eq, 2),
            "pnl_30d_usd": round(eq - eq30, 2),
            "pnl_30d_pct": round((eq - eq30) / eq30 * 100, 1) if eq30 else 0.0,
            "open_bets": len(self.pf.open_positions()),
            "daily_limit": {"spent_usd": round(spent, 2), "limit_usd": float(s["daily_limit_usd"])},
            "pending": pending,
            "recent": recent,
            "unread_notifications": int(
                self.db.scalar("SELECT COUNT(*) AS n FROM notifications WHERE is_read=0", default=0)
            ),
            "status": self.status(),
            "mode": s["mode"],
        }

    # ------------------------------------------------------------ экран «Карточка сигнала»
    def signal_detail(self, signal_id: str) -> dict | None:
        now = self.clock()
        r = self.db.one(
            "SELECT s.*, d.* FROM signals s JOIN decisions d ON d.signal_id=s.signal_id WHERE s.signal_id=?",
            (signal_id,),
        )
        if not r:
            return None
        m = self._market_of_token(r["token_id"])
        info = json.loads(r["reasons"] or "{}")
        det, codes = info.get("details", {}), info.get("codes", [])
        price = r["vwap_exec"] or det.get("ask_now")
        size = r["size_usd"] or 0.0
        payout = size / price if price else 0.0
        days = det.get("days_left")
        checks = [
            {"ok": True, "title": "Кит проверен", "text": "Стабильно выигрывает в этой теме, а не по случайности"}
        ]
        if "price_moved" in codes or "slippage" in codes:
            checks.append({"ok": False, "title": "Цена уже ушла", "text": REASONS_RU["price_moved"]})
        else:
            checks.append(
                {"ok": True, "title": "Цена ещё выгодная", "text": "С момента ставки кита цена почти не изменилась"}
            )
        if r["q_llm"] is not None:
            checks.append(
                {
                    "ok": "disagree" not in codes,
                    "title": "ИИ согласен" if "disagree" not in codes else "ИИ не согласен",
                    "text": "Независимая оценка по новостям подтверждает расчёт"
                    if "disagree" not in codes
                    else REASONS_RU["disagree"],
                }
            )
        else:
            checks.append(
                {
                    "ok": None,
                    "title": "Новости — оцените сами",
                    "text": "ИИ не участвует в решении. Ниже — что пишут и что думают на других рынках прогнозов",
                }
            )
        ctx = json.loads(r.get("info") or "{}")
        same, opp = ctx.get("same") or [], ctx.get("opposite") or []
        if same or opp:
            checks.append(
                {
                    "ok": False if len(opp) > len(same) else (True if same and not opp else None),
                    "title": (
                        f"Ещё {len(same)} {plural_ru(len(same), 'кит', 'кита', 'китов')} на этой стороне"
                        if same
                        else "Другие киты против"
                    )
                    + (f", {len(opp)} — против" if same and opp else ""),
                    "text": ", ".join(
                        filter(
                            None,
                            [("за: " + ", ".join(same)) if same else "", ("против: " + ", ".join(opp)) if opp else ""],
                        )
                    ),
                }
            )
        if ctx.get("size_ratio"):
            ratio = ctx["size_ratio"]
            checks.append(
                {
                    "ok": True if ratio >= 1.5 else None,
                    "title": (
                        "Крупная ставка для этого кита"
                        if ratio >= 1.5
                        else ("Обычная ставка для этого кита" if ratio >= 0.5 else "Небольшая ставка для этого кита")
                    ),
                    "text": f"${_thousands(r['whale_size_usd'])} — это {str(round(ratio, 1)).replace('.', ',')}× "
                    f"от его обычной ставки (${_thousands(ctx['typical_bet_usd'])})"
                    + (". Докупает к уже открытой позиции" if ctx.get("adding") else ""),
                }
            )
        if days:
            checks.append(
                {"ok": None, "title": f"Деньги будут заморожены {int(round(days))} дн", "text": "До закрытия рынка"}
            )
        fee_usd = (size / price) * det["fee_per_share"] if price and size and det.get("fee_per_share") else 0.0
        delay = (r.get("detected_ts") - r["ts"]) if r.get("detected_ts") and r.get("ts") else None
        s = self.risk.settings()
        spent = self.pf.spent_since(_day_start(now))
        live = None
        if r["status"] == "pending" and r["kind"] == "entry":
            q = self.quote(r["token_id"]) or {}
            q_eff = (r["q_final"] or 0) - self.p.uncertainty_haircut
            ask = q.get("ask") or det.get("ask_now")
            rate = fees.market_rate(self.db.one("SELECT * FROM markets WHERE market_id=?", (r["market_id"],)))
            live = {
                "token_id": r["token_id"],
                "whale_price": r["whale_price"],
                "ask": ask,
                "bid": q.get("bid"),
                "live": bool(q.get("live")),
                "q_eff": round(q_eff, 4),
                "fee_rate": rate,
                "threshold": self._edge_threshold(self.risk.settings()),
                "edge_decided": r["edge_net"],
                "ask_decided": det.get("ask_now"),
                "window_s": max(1, (r["expires_ts"] or now) - (r.get("decided_ts") or r["ts"])),
            }
        pend_ids = [
            x["signal_id"]
            for x in self.db.query(
                "SELECT signal_id FROM signals WHERE status='pending' AND kind='entry' ORDER BY ts DESC"
            )
        ]
        return {
            "signal_id": signal_id,
            "status": r["status"],
            "kind": r["kind"],
            "live": live,
            "pending_ids": pend_ids,
            "token_id": r["token_id"],
            "question": m.get("question"),
            "whale": scorer.short(r["entity_id"]),
            "entity_id": r["entity_id"],
            "category": CATEGORY_RU.get(m.get("category"), "разное"),
            "seconds_left": max(0, (r["expires_ts"] or now) - now) if r["status"] == "pending" else 0,
            "recommendation": r["decision"],
            "side": m.get("outcome_label"),
            "size_usd": round(size, 2),
            "price_cents": round(price * 100) if price else None,
            "if_win_usd": round(payout - size, 2),
            "if_lose_usd": round(-size, 2),
            "payout_usd": round(payout, 2),
            "plain": (
                f"Вы ставите ${size:.0f} на «{m.get('outcome_label')}». Если событие произойдёт, вы получите "
                f"около ${payout:.0f}, иначе потеряете ${size:.0f}."
            )
            if size
            else reason_text(codes),
            "checks": checks,
            "skip_reason": reason_text(codes) if r["decision"] == "SKIP" else None,
            "fee_usd": round(fee_usd, 2),
            "delay_s": delay,
            **self._signal_research(r, det, m),
            "limits": {
                "max_bet_usd": float(s["max_bet_usd"]),
                "left_today_usd": round(float(s["daily_limit_usd"]) - spent, 2),
            },
        }

    def _edge_threshold(self, settings: dict) -> float:
        ov = (settings.get("param_overrides") or {}).get("edge_threshold")
        return float(ov) if ov is not None else float(self.p.edge_threshold)

    def quote(self, token_id: str) -> dict | None:
        """Лучшие цены по исходу: из потока (живые), иначе последний снимок стакана."""
        streams = getattr(self.agent, "streams", None) if self.agent is not None else None
        if streams is not None:
            got = streams.books.get(token_id)
            if got is not None:
                bids, asks = got
                return {"bid": bids[0][0] if bids else None, "ask": asks[0][0] if asks else None, "live": True}
        else:  # поток стаканов работает в другом процессе (realtime) — его цены в live_quotes
            lq = self.db.one(
                "SELECT bid, ask, ts FROM live_quotes WHERE token_id=? AND ts>?", (token_id, time.time() - 30)
            )
            if lq:
                return {"bid": lq["bid"], "ask": lq["ask"], "live": True}
        row = self.db.one(
            "SELECT bids, asks, ts FROM orderbook_snapshots WHERE token_id=? ORDER BY ts DESC LIMIT 1", (token_id,)
        )
        if not row:
            return None
        bids, asks = json.loads(row["bids"] or "[]"), json.loads(row["asks"] or "[]")
        return {
            "bid": float(bids[0][0]) if bids else None,
            "ask": float(asks[0][0]) if asks else None,
            "live": False,
            "ts": row["ts"],
        }

    # ------------------------------------------------------------ живой интерфейс (поток событий)
    def fingerprint(self) -> dict:
        """Короткий слепок состояния: изменился — открытые страницы обновляются."""
        sig = (
            self.db.one(
                "SELECT COUNT(*) AS n, MAX(ts) AS m, "
                "SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS p FROM signals"
            )
            or {}
        )
        pend = self.db.query("SELECT signal_id FROM signals WHERE status='pending' ORDER BY ts DESC LIMIT 50")
        fills = self.db.one("SELECT COUNT(*) AS n, MAX(ts) AS m FROM fills") or {}
        notif = (
            self.db.one("SELECT COUNT(*) AS n, SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) AS u FROM notifications")
            or {}
        )
        return {
            "signals": (sig.get("n"), sig.get("m"), ",".join(r["signal_id"] for r in pend)),
            "bets": (fills.get("n"), fills.get("m")),
            "notifications": (notif.get("n"), notif.get("u")),
        }

    def _live_tokens(self) -> list[str]:
        toks = [
            r["token_id"]
            for r in self.db.query("SELECT DISTINCT token_id FROM signals WHERE status='pending' LIMIT 30")
        ]
        toks += [p["token_id"] for p in self.pf.open_positions()]
        return list(dict.fromkeys(toks))[:60]

    def event_stream(self, should_stop=lambda: False, tick: float = 0.5, heartbeat: float = 15.0, max_s: float = 600.0):
        """События для страницы (SSE): change — что изменилось; price — новые цены по исходам на экране;
        ping — связь жива. Через max_s поток закрывается, браузер сам переподключится."""
        from .events import bus

        seen = bus.version
        t0 = last_hb = last_q = time.time()
        last_ev = int(self.db.scalar("SELECT MAX(id) AS m FROM live_events", default=0))
        prev = self.fingerprint()
        quotes: dict = {}
        toks: list = []
        toks_ts = 0.0
        yield "hello", {"pending": len([x for x in prev["signals"][2].split(",") if x])}
        while not should_stop() and time.time() - t0 < max_s:
            seen = bus.wait(seen, tick)
            # события других процессов (агент в realtime, проверки в worker) — через таблицу live_events
            evs = self.db.query("SELECT id, kind FROM live_events WHERE id>? ORDER BY id LIMIT 200", (last_ev,))
            if evs:
                last_ev = evs[-1]["id"]
            fp = self.fingerprint()
            if fp != prev or any(e["kind"] == "check" for e in evs):
                changed = [k for k in fp if fp[k] != prev[k]] + sorted({e["kind"] for e in evs} - {"signal", "bet"})
                pending = [x for x in fp["signals"][2].split(",") if x]
                new = [x for x in pending if x not in prev["signals"][2].split(",")]
                prev = fp
                yield "change", {"what": changed, "new_pending": new}
            now = time.time()
            if now - last_q >= 1.0:
                last_q = now
                if now - toks_ts > 5:
                    toks, toks_ts = self._live_tokens(), now
                for tok in toks:
                    q = self.quote(tok)
                    if q and (q["bid"], q["ask"]) != quotes.get(tok):
                        quotes[tok] = (q["bid"], q["ask"])
                        yield "price", {"token_id": tok, "bid": q["bid"], "ask": q["ask"], "live": q["live"]}
            if now - last_hb >= heartbeat:
                last_hb = now
                yield "ping", {}

    FLAG_TEXT = {
        "coordinated_text": "Одинаковые тексты в нескольких каналах — похоже на вброс",
        "news_after_price_move": "Новости вышли уже после движения цены — это следствие, а не причина",
        "already_priced": "Цена уже сдвинулась после новостей — информация, скорее всего, учтена",
    }

    def _signal_research(self, r: dict, det: dict, m: dict) -> dict:
        """Данные для решения: новости, прогнозы других рынков, оценка ИИ, история кита, цены."""
        items = self.db.query(
            "SELECT source, source_type, source_weight, ts_pub, title, url FROM news_items "
            "WHERE market_id=? ORDER BY ts_pub DESC LIMIT 12",
            (r["market_id"],),
        )
        news = [
            {
                "source": i["source"],
                "title": i["title"],
                "url": i["url"],
                "when": _fmt_age(i["ts_pub"], self.clock()),
                "reliable": (i["source_weight"] or 0) >= 0.5,
                "kind": {
                    "primary": "первоисточник",
                    "media": "СМИ",
                    "forecast": "прогнозы",
                    "telegram": "Telegram",
                    "social": "соцсети",
                }.get(i["source_type"], i["source_type"]),
            }
            for i in items
        ]
        fcs = self.db.query(
            "SELECT source, title, url, probability FROM forecasts WHERE market_id=? ORDER BY ts_fetched DESC LIMIT 6",
            (r["market_id"],),
        )
        flags = det.get("news_flags") or {}
        warnings = [t for k, t in self.FLAG_TEXT.items() if flags.get(k)]
        if news and flags and not flags.get("enough_confirmation"):
            warnings.append("Меньше двух первоисточников — относитесь к новостям осторожно")
        ai = None
        est = self.db.one(
            "SELECT * FROM llm_estimates WHERE signal_id=? ORDER BY ts DESC LIMIT 1", (r["signal_id"],)
        ) or self.db.one(
            "SELECT * FROM llm_estimates WHERE market_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (r["market_id"], r["decided_ts"] or self.clock()),
        )
        if r["q_llm"] is not None and est:
            ev = json.loads(est["evidence"] or "{}")
            ai = {
                "probability_pct": round(r["q_llm"] * 100),
                "confidence": {"low": "низкая", "medium": "средняя", "high": "высокая"}.get(
                    est["confidence"], est["confidence"]
                ),
                "base_rate_pct": round(est["base_rate"] * 100) if est["base_rate"] is not None else None,
                "for": ev.get("arguments_for", [])[:5],
                "against": ev.get("arguments_against", [])[:5],
                "uncertain": ev.get("key_uncertainties", [])[:3],
                "trusted": bool(det.get("llm_trusted")),
            }
        sc = (
            self.db.one(
                "SELECT * FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (r["entity_id"],)
            )
            or {}
        )
        cat = scorer.latest_category_scores(self.db, r["entity_id"]).get(m.get("category") or "other") or {}
        whale = {
            "return_90d_pct": round(sc["return_90d"] * 100, 1) if _f(sc.get("return_90d")) is not None else None,
            "events": sc.get("n_clusters"),
            "events_in_topic": cat.get("n_clusters"),
            "edge_cents": round((cat.get("edge_shrunk") or sc.get("edge_shrunk") or 0) * 100, 1),
            "hold_pct": round((sc.get("hold_share") or 0) * 100),
        }
        prices = {
            "whale_cents": round(r["whale_price"] * 100, 1) if r.get("whale_price") else None,
            "now_cents": round(det["ask_now"] * 100, 1) if det.get("ask_now") else None,
            "model_cents": round(r["q_whale"] * 100) if r.get("q_whale") else None,
        }
        return {
            "mode": det.get("ai_mode") or ("ai" if r["q_llm"] is not None else "manual"),
            "ai_failed": bool(det.get("ai_failed")),
            "ai": ai,
            "news": news,
            "forecasts": [
                {
                    "source": f["source"],
                    "title": f["title"],
                    "url": f["url"],
                    "probability_pct": round(f["probability"] * 100),
                }
                for f in fcs
            ],
            "warnings": warnings,
            "whale_stats": whale,
            "prices": prices,
        }

    # ------------------------------------------------------------ экран «Мои ставки»
    def _bet_row(self, p: dict, now: int) -> dict:
        """Строка нашей ставки: цена входа наша и кита, текущая цена, прибыль, комиссия, что делает кит."""
        is_open = p["shares"] > 1e-9
        m = self._market_of_token(p["token_id"])
        sig = (
            self.db.one(
                "SELECT s.*, d.q_final, d.edge_net, d.vwap_exec FROM signals s "
                "LEFT JOIN decisions d ON d.signal_id=s.signal_id WHERE s.signal_id=?",
                (p["signal_id"],),
            )
            or {}
        )
        e = sig.get("entity_id", "")
        stake = p["cost"] + p.get("cost_removed", 0.0)
        bought = p.get("bought") or p["shares"]
        entry = stake / bought if bought else None
        px = None
        if is_open:
            q = self.quote(p["token_id"])
            px = q["bid"] if q and q.get("bid") is not None else self.pf.mark_price(p["token_id"])
            px = px if px is not None else (p["cost"] / p["shares"])
            value = p["shares"] * px
            # если часть уже продана — её результат (выручка минус списанная себестоимость) тоже в прибыли
            pnl = value + p["proceeds"] - p["cost"] - p.get("cost_removed", 0.0) - p.get("fees", 0.0)
            base = p["cost"] + p.get("cost_removed", 0.0)
        else:
            payout = self.db.scalar("SELECT payout FROM tokens WHERE token_id=?", (p["token_id"],))
            value = p["proceeds"]
            pnl = value - stake - p.get("fees", 0.0)
            base = stake
        ws = self._wallets_of(e) if e else []
        whale_left = None
        if ws:
            ph = ",".join("?" for _ in ws)
            # по сделкам, а не по ночному пересчёту позиций: видно сразу после ставки кита
            whale_left = self.db.scalar(
                "SELECT SUM(CASE WHEN side='BUY' THEN size_shares ELSE -size_shares END) AS s FROM trades "
                f"WHERE token_id=? AND wallet IN ({ph})",
                [p["token_id"]] + ws,
            )
        exited = bool(
            self.db.one("SELECT 1 AS x FROM signals WHERE kind='whale_exit' AND token_id=?", (p["token_id"],))
        )
        whale_state = (
            "вышел"
            if exited or (whale_left is not None and whale_left <= 1e-6)
            else ("держит" if whale_left else "нет данных")
        )
        if not is_open:
            result = (
                ("выиграла" if payout == 1 else "вернули половину (50/50)" if payout == 0.5 else "проиграла")
                if payout is not None
                and p.get("closed_ts")
                and not self.db.one("SELECT 1 AS x FROM fills WHERE token_id=? AND side='SELL'", (p["token_id"],))
                else "продана"
            )
        return {
            "token_id": p["token_id"],
            "question": m.get("question"),
            "url": m.get("url"),
            "whale": scorer.short(e),
            "entity_id": e,
            "category": CATEGORY_RU.get(m.get("category"), "разное"),
            "side": m.get("outcome_label"),
            "stake_usd": round(p["cost"] if is_open else stake, 2),
            "value_usd": round(value, 2),
            "pnl_usd": round(pnl, 2),
            "change_pct": round(100 * pnl / base, 1) if base else 0.0,
            "entry_cents": round(entry * 100, 1) if entry else None,
            "whale_cents": round(sig["whale_price"] * 100, 1) if sig.get("whale_price") else None,
            "now_cents": round(px * 100, 1) if px is not None else None,
            "shares": round(p["shares"] if is_open else bought, 2),
            "fees_usd": round(p.get("fees", 0.0), 2),
            "shares_exact": p["shares"] if is_open else None,
            "cost_exact": p["cost"] if is_open else None,
            "if_win_usd": round(p["shares"] * 1.0 - p["cost"] - p.get("fees", 0.0), 2) if is_open else None,
            "opened_ago": _fmt_age(p["opened_ts"], now) if p.get("opened_ts") else None,
            "held_days": round(((p.get("closed_ts") or now) - p["opened_ts"]) / DAY, 1) if p.get("opened_ts") else None,
            "closes_in_days": max(0, int(((m.get("end_ts") or now) - now) / DAY)) if is_open else None,
            "whale_state": whale_state,
            "whale_exited": whale_state == "вышел",
            "result": None if is_open else result,
            "is_open": is_open,
        }

    def positions(self, status: str = "open") -> dict:
        now = self.clock()
        allpos = self.pf.positions()
        rows = [self._bet_row(p, now) for p in allpos.values() if (p["shares"] > 1e-9) == (status == "open")]
        rows.sort(key=lambda r: r["closes_in_days"] if r["closes_in_days"] is not None else 0)
        decisions = []
        for r in self.db.query(
            "SELECT s.signal_id, s.entity_id, s.token_id, d.decision, d.reasons FROM signals s "
            "JOIN decisions d ON d.signal_id=s.signal_id WHERE s.kind='whale_exit' AND s.status='pending'"
        ):
            m = self._market_of_token(r["token_id"])
            det = json.loads(r["reasons"] or "{}").get("details", {})
            p = allpos.get(r["token_id"], {})
            sell_usd = det.get("shares", 0) * det.get("bid", 0)
            decisions.append(
                {
                    "signal_id": r["signal_id"],
                    "whale": scorer.short(r["entity_id"]),
                    "question": m.get("question"),
                    "stake_usd": round(p.get("cost", 0), 2),
                    "recommendation": "hold" if r["decision"] == "HOLD" else "sell",
                    "text": (
                        "Наша оценка не изменилась: шанс по-прежнему выше цены."
                        if r["decision"] == "HOLD"
                        else "Шанс уже не выше цены — разумно продать."
                    ),
                    "sell_now_usd": round(sell_usd, 2),
                    "sell_pnl_usd": round(sell_usd - p.get("cost", 0), 2),
                }
            )
        in_play = sum(r["stake_usd"] for r in rows) if status == "open" else None
        closed_rows = (
            [self._bet_row(p, now) for p in allpos.values() if p["shares"] <= 1e-9] if status == "open" else rows
        )
        wins = [r for r in closed_rows if r["pnl_usd"] > 0]
        summary = {
            "open_value_usd": round(sum(r["value_usd"] for r in rows), 2) if status == "open" else None,
            "open_pnl_usd": round(sum(r["pnl_usd"] for r in rows), 2) if status == "open" else None,
            "realized_pnl_usd": round(sum(r["pnl_usd"] for r in closed_rows), 2),
            "win_pct": round(100 * len(wins) / len(closed_rows), 1) if closed_rows else None,
            "fees_usd": round(sum(p.get("fees", 0.0) for p in allpos.values()), 2),
        }
        return {
            "status": status,
            "in_play_usd": in_play,
            "items": rows,
            "need_decision": decisions,
            "summary": summary,
            "counts": {
                "open": len(self.pf.open_positions()),
                "closed": sum(1 for p in allpos.values() if p["shares"] <= 1e-9),
            },
        }

    def bet_detail(self, token_id: str) -> dict | None:
        """Страница нашей ставки: цифры, график цены с отметками входа, хронология событий."""
        now = self.clock()
        p = self.pf.positions().get(token_id)
        if not p:
            return None
        row = self._bet_row(p, now)
        sig = (
            self.db.one(
                "SELECT s.*, d.q_final, d.q_llm, d.edge_net, d.vwap_exec, d.reasons FROM signals s "
                "LEFT JOIN decisions d ON d.signal_id=s.signal_id WHERE s.signal_id=?",
                (p["signal_id"],),
            )
            or {}
        )
        m = (
            self.db.one(
                "SELECT m.* FROM tokens t JOIN markets m ON m.market_id=t.market_id WHERE t.token_id=?", (token_id,)
            )
            or {}
        )
        fills = self.db.query(
            "SELECT ts, side, price, size_shares, fee_usd FROM fills WHERE token_id=? ORDER BY ts", (token_id,)
        )
        t0 = min([f["ts"] for f in fills] + [sig.get("ts") or now]) - 2 * DAY
        pts = self.db.query(
            "SELECT ts, price FROM price_history WHERE token_id=? AND ts>=? ORDER BY ts", (token_id, t0)
        )
        step = max(1, len(pts) // 240)  # не больше ~240 точек на графике
        chart = [{"ts": x["ts"], "price": round(x["price"], 4)} for x in pts[::step]]
        if pts and (not chart or chart[-1]["ts"] != pts[-1]["ts"]):
            chart.append({"ts": pts[-1]["ts"], "price": round(pts[-1]["price"], 4)})
        events = []
        if sig.get("ts"):
            events.append(
                {
                    "ts": sig["ts"],
                    "kind": "whale",
                    "text": f"Кит {scorer.short(sig['entity_id'])} купил по "
                    f"{round((sig.get('whale_price') or 0) * 100)}¢ на ${_thousands(sig.get('whale_size_usd') or 0)}",
                }
            )
        if sig.get("detected_ts"):
            events.append(
                {
                    "ts": sig["detected_ts"],
                    "kind": "signal",
                    "text": f"Сигнал — через {max(0, sig['detected_ts'] - sig['ts'])} с после сделки кита",
                }
            )
        for f in fills:
            px = f"{f['price'] * 100:.1f}".replace(".", ",").replace(",0", "")
            sh = f"{f['size_shares']:.1f}".replace(".", ",")
            txt = {
                "BUY": f"Купили {sh} акций по {px}¢",
                "SELL": f"Продали {sh} акций по {px}¢",
                "SETTLE": "Рынок закрылся: " + ("ставка выиграла" if f["price"] >= 1 else "ставка проиграла"),
            }[f["side"]]
            if f["fee_usd"]:
                txt += f", комиссия ${f['fee_usd']:.2f}".replace(".", ",")
            events.append({"ts": f["ts"], "kind": f["side"].lower(), "text": txt})
        for x in self.db.query(
            "SELECT ts, detected_ts FROM signals WHERE kind='whale_exit' AND token_id=?", (token_id,)
        ):
            events.append({"ts": x["ts"], "kind": "exit", "text": "Кит продал свою позицию"})
        if m.get("end_ts") and row["is_open"]:
            events.append(
                {"ts": m["end_ts"], "kind": "end", "text": "Ожидаемое закрытие рынка", "future": m["end_ts"] > now}
            )
        events.sort(key=lambda e: e["ts"])
        det = json.loads(sig.get("reasons") or "{}").get("details") or {}
        return {
            **row,
            "signal_id": sig.get("signal_id"),
            "market_end_ts": m.get("end_ts"),
            "chart": chart,
            "marks": {
                "whale": {"ts": sig.get("ts"), "price": sig.get("whale_price")},
                "ours": {"ts": fills[0]["ts"] if fills else None, "price": fills[0]["price"] if fills else None},
            },
            "events": [{**e, "when": datetime.fromtimestamp(e["ts"], tz=UTC).strftime("%d.%m %H:%M")} for e in events],
            "model": {
                "q_pct": round(sig["q_final"] * 100) if sig.get("q_final") else None,
                "ai_pct": round(sig["q_llm"] * 100) if sig.get("q_llm") else None,
                "edge_pct": round(sig["edge_net"] * 100, 1) if sig.get("edge_net") is not None else None,
                "fee_rate": det.get("fee_rate"),
            },
        }

    def sell_bet(self, token_id: str) -> dict:
        if self.agent is None:
            return {"ok": False, "error": "Агент не запущен"}
        res = self.agent.sell_position(token_id)
        return {
            "ok": bool(res and res.get("shares")),
            **(res or {}),
            **({} if res and res.get("shares") else {"error": "Сейчас некому продать — в стакане нет покупателей"}),
        }

    # ------------------------------------------------------------ экраны «Киты» и «Профиль»
    def _risk_level(self, s: dict) -> str:
        if not s or s.get("class") not in (None, "normal") or (s.get("edge_ci_lo") or 0) <= 0:
            return "высокий"
        if (s.get("edge_ci_lo") or 0) > 0.03 and (s.get("hold_share") or 0) >= 0.8 and (s.get("n_clusters") or 0) >= 80:
            return "низкий"
        return "средний"

    def _wallets_of(self, e: str) -> list[str]:
        return [r["wallet"] for r in self.db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))] or [e]

    def whale_stats(self, e: str, weeks: int = 12) -> dict:
        """Сводка по ставкам кита из его позиций: выигрыши, прибыль, размер ставок, активность."""
        ws = self._wallets_of(e)
        ph = ",".join("?" for _ in ws)
        r = (
            self.db.one(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN status IN ('closed','resolved') THEN 1 ELSE 0 END) AS n_closed, "
                "SUM(CASE WHEN status IN ('closed','resolved') AND pnl_usd>0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN status IN ('closed','resolved') THEN pnl_usd ELSE 0 END) AS pnl, "
                "SUM(CASE WHEN status IN ('closed','resolved') THEN cost_usd ELSE 0 END) AS closed_cost, "
                "SUM(cost_usd) AS volume, AVG(cost_usd) AS avg_bet, AVG(avg_entry) AS avg_entry, "
                "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS n_open, "
                "SUM(CASE WHEN status='open' THEN cost_usd * remaining_shares / NULLIF(shares_bought, 0) ELSE 0 END) "
                "AS open_cost, MAX(opened_ts) AS last_bet "
                f"FROM positions WHERE wallet IN ({ph})",
                ws,
            )
            or {}
        )
        holds = [
            x["d"]
            for x in self.db.query(
                f"SELECT closed_ts - opened_ts AS d FROM positions WHERE wallet IN ({ph}) AND closed_ts IS NOT NULL "
                "AND opened_ts IS NOT NULL",
                ws,
            )
            if x["d"] is not None and x["d"] >= 0
        ]
        last_trade = self.db.scalar(f"SELECT MAX(ts) AS m FROM trades WHERE wallet IN ({ph})", ws)
        now = self.clock()
        start = now - weeks * 7 * DAY
        closed = self.db.query(
            f"SELECT closed_ts, pnl_usd FROM positions WHERE wallet IN ({ph}) AND closed_ts>=? "
            "AND pnl_usd IS NOT NULL ORDER BY closed_ts",
            ws + [start],
        )
        spark, acc = [], 0.0
        for k in range(weeks):
            lo, hi = start + k * 7 * DAY, start + (k + 1) * 7 * DAY
            acc += sum(c["pnl_usd"] for c in closed if lo <= c["closed_ts"] < hi)
            spark.append(round(acc, 2))
        n_closed = int(r.get("n_closed") or 0)
        return {
            "bets": int(r.get("n") or 0),
            "closed": n_closed,
            "win_pct": round(100 * (r.get("wins") or 0) / n_closed, 1) if n_closed else None,
            "pnl_usd": round(r.get("pnl") or 0, 2),
            "roi_pct": round(100 * (r.get("pnl") or 0) / r["closed_cost"], 1) if r.get("closed_cost") else None,
            "volume_usd": round(r.get("volume") or 0, 2),
            "avg_bet_usd": round(r.get("avg_bet") or 0, 2),
            "avg_entry_cents": round((r.get("avg_entry") or 0) * 100) if r.get("avg_entry") else None,
            "open": int(r.get("n_open") or 0),
            "open_usd": round(r.get("open_cost") or 0, 2),
            "hold_days": round(float(np.median(holds)) / DAY, 1) if holds else None,
            "last_trade_ago_s": (now - last_trade) if last_trade else None,
            "spark": spark,
        }

    def whale_bets(self, e: str, limit: int = 15, status: str | None = None) -> list[dict]:
        """Собственные ставки кита (не наши копии): что, по какой цене, сколько и чем кончилось."""
        ws = self._wallets_of(e)
        ph = ",".join("?" for _ in ws)
        where = " AND p.status='open'" if status == "open" else (" AND p.status<>'open'" if status == "closed" else "")
        rows = self.db.query(
            "SELECT p.*, m.question, m.url, m.end_ts, t.outcome_label FROM positions p "
            "LEFT JOIN markets m ON m.market_id=p.market_id LEFT JOIN tokens t ON t.token_id=p.token_id "
            f"WHERE p.wallet IN ({ph}){where} ORDER BY p.opened_ts DESC LIMIT ?",
            ws + [limit],
        )
        now = self.clock()
        out = []
        for b in rows:
            st = b["status"]
            if st == "open":
                label = "держит"
            elif b["held_to_resolution"]:
                label = "выиграл" if (b["pnl_usd"] or 0) > 0 else "проиграл"
            else:
                label = "продал"
            out.append(
                {
                    "question": b["question"] or "Рынок загружается",
                    "side": b["outcome_label"],
                    "url": b["url"],
                    "entry_cents": round((b["avg_entry"] or 0) * 100),
                    "stake_usd": round(b["cost_usd"] or 0, 2),
                    "pnl_usd": round(b["pnl_usd"], 2) if b["pnl_usd"] is not None else None,
                    "state": label,
                    "category": CATEGORY_RU.get(b["category"], "разное"),
                    "when": _fmt_age(b["opened_ts"], now),
                }
            )
        return out

    def _whale_row(self, e: str, s: dict, wl: dict | None) -> dict:
        mode = (wl or {}).get("mode", "off")
        cs = scorer.latest_category_scores(self.db, e)
        proven = sorted((c for c in cs.values() if c["status"] == "approved"), key=lambda c: -(c["edge_shrunk"] or 0))
        cats = [c["category"] for c in proven] or [
            c["category"] for c in sorted(cs.values(), key=lambda c: -(c["n_clusters"] or 0))
        ]
        status = s.get("status") if s else "insufficient"
        label = STATUS_RU.get(status, "Мало данных")
        note = None
        if status == "excluded":
            note = CLASS_REASON_RU.get(s.get("class"), "Нет преимущества")
        elif s and s.get("hold_share") is not None and s["hold_share"] < self.p.min_hold_share:
            label, note = "Не копируем", "Быстрые сделки"
        elif s and (s.get("edge_ci_lo") or 0) <= 0 and status != "approved":
            note = "Нет преимущества" if status == "candidate" else None
        if mode == "watch":
            label = "Только следить"
        rec = reconcile.latest(self.db, e)
        pm = None
        if self.players is not None:  # из кэша карточек, без запросов к Polymarket
            c = self.players.cards.get((e, True)) or self.players.cards.get((e, False))
            if c and c.get("ok"):
                pm = {k: c.get(k) for k in ("name", "image", "pnl_all_usd", "pnl_30d_usd", "value_usd", "verified")}
                pm["form"] = (c.get("form") or {}).get("state")
        return {
            "pm": pm,
            "stats": self.whale_stats(e),
            "reconcile": rec["verdict"] if rec else None,
            "events": int(s["n_clusters"]) if s and s.get("n_clusters") is not None else None,
            "entity_id": e,
            "name": scorer.short(e),
            "categories": [CATEGORY_RU.get(c, c) for c in cats][:2],
            "return_90d_pct": round(s["return_90d"] * 100, 1) if s and _f(s.get("return_90d")) is not None else None,
            "risk": self._risk_level(s),
            "status": label,
            "note": note,
            "copy": mode == "copy",
            "can_copy": status == "approved",
        }

    def whales(self) -> dict:
        scores = (
            {r["entity_id"]: r for r in scorer.latest_scores(self.db).to_dicts()}
            if self.db.scalar("SELECT COUNT(*) AS n FROM wallet_scores", default=0)
            else {}
        )
        wl = {r["entity_id"]: r for r in self.db.query("SELECT * FROM watchlist")}
        rows = [self._whale_row(e, scores.get(e, {}), wl[e]) for e in wl]
        rows.sort(key=lambda r: (not r["copy"], -(r["return_90d_pct"] or -999)))
        cand = [(e, s) for e, s in scores.items() if s["status"] == "approved" and e not in wl]
        cand.sort(key=lambda es: -(es[1].get("score") or 0))
        candidates = []
        for e, s in cand[:10]:
            cs = scorer.latest_category_scores(self.db, e)
            cats = [
                c["category"]
                for c in sorted(cs.values(), key=lambda c: -(c["edge_shrunk"] or 0))
                if c["status"] == "approved"
            ]
            candidates.append(
                {
                    "entity_id": e,
                    "name": scorer.short(e),
                    "risk": self._risk_level(s),
                    "categories": [CATEGORY_RU.get(c, c) for c in cats][:2],
                }
            )
        auto = tracked.auto_discover(self.db)
        return {
            "items": rows,
            "new_candidates": len(cand) if auto else 0,
            "candidates": candidates if auto else [],
            "auto_discover": auto,
        }

    def whale_profile(self, entity_id: str) -> dict | None:
        s = self.db.one("SELECT * FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (entity_id,))
        if not s:
            return None
        wl = self.db.one("SELECT * FROM watchlist WHERE entity_id=?", (entity_id,)) or {}
        off = set(json.loads(wl.get("categories_off") or "[]"))
        cats = scorer.latest_category_scores(self.db, entity_id)
        months: dict[str, list[float]] = {}
        for r in self.db.query(
            "SELECT closed_ts, pnl_usd, cost_usd FROM positions WHERE entity_id=? AND "
            "pnl_usd IS NOT NULL AND closed_ts IS NOT NULL ORDER BY closed_ts",
            (entity_id,),
        ):
            m = datetime.fromtimestamp(int(r["closed_ts"]), tz=UTC).strftime("%Y-%m")
            acc = months.setdefault(m, [0.0, 0.0])
            acc[0] += r["pnl_usd"] or 0.0
            acc[1] += r["cost_usd"] or 0.0
        series = []
        cum_pnl = cum_cost = 0.0
        last7 = sorted(months.items())[-7:]
        for m, (pnl, cost) in last7:
            cum_pnl += pnl
            cum_cost += cost
            series.append({"month": m, "cum_return_pct": round(cum_pnl / max(cum_cost, 1) * 100, 1)})
        ours = self.db.query(
            "SELECT s.signal_id, s.token_id, s.whale_price, s.ts, s.status, s.kind, d.decision, d.size_usd "
            "FROM signals s LEFT JOIN decisions d ON d.signal_id=s.signal_id WHERE s.entity_id=? "
            "ORDER BY s.ts DESC LIMIT 6",
            (entity_id,),
        )
        now = self.clock()
        last = []
        for r in ours:
            m = self._market_of_token(r["token_id"])
            label = {
                "pending": "Ждёт решения",
                "executed": f"Повторили ${r['size_usd'] or 0:.0f}",
                "skipped": "Пропустили",
                "expired": "Время вышло",
                "rejected": "Пропущено вами",
            }.get(r["status"], r["status"])
            if r["kind"] == "whale_exit":
                label = "Кит вышел"
            last.append(
                {
                    "question": m.get("question"),
                    "price_cents": round((r["whale_price"] or 0) * 100),
                    "side": m.get("outcome_label"),
                    "when": _fmt_age(r["ts"], now),
                    "our_action": label,
                }
            )
        copied = self.db.query(
            "SELECT d.size_usd, s.token_id FROM signals s JOIN decisions d ON d.signal_id=s.signal_id "
            "WHERE s.entity_id=? AND s.status='executed' AND s.kind='entry'",
            (entity_id,),
        )
        allpos = self.pf.positions()
        our_pnl = 0.0
        for c in copied:
            p = allpos.get(c["token_id"])
            if p:
                px = self.pf.mark_price(c["token_id"]) or 0
                our_pnl += p["proceeds"] + p["shares"] * px - (p["cost"] + p.get("cost_removed", 0.0))
        topics = []
        for cat, c in sorted(cats.items(), key=lambda kv: -(kv[1]["n_clusters"] or 0)):
            proven = c["status"] == "approved"
            topics.append(
                {
                    "category": cat,
                    "label": CATEGORY_RU.get(cat, cat),
                    "proven": proven,
                    "enabled": proven and cat not in off,
                    "text": "Сильная сторона · копируем"
                    if proven and cat not in off
                    else ("Выключено вами" if proven else "Мало данных — включится сама, когда наберётся история"),
                }
            )
        row = self._whale_row(entity_id, s, wl)
        wallets = [
            r["wallet"] for r in self.db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (entity_id,))
        ] or [entity_id]
        first = self.db.scalar(
            "SELECT MIN(first_seen_ts) AS m FROM wallets WHERE wallet IN ({})".format(",".join("?" for _ in wallets)),
            wallets,
        )
        link_text = {
            "L1": "общий источник денег",
            "L2": "переводы между кошельками",
            "L3": "одни и те же рынки",
            "L4": "сделки в одни и те же секунды",
            "L5": "одинаковые суммы ставок",
            "L6": "один владелец",
        }
        links = []
        if len(wallets) > 1:
            ph = ",".join("?" for _ in wallets)
            for lk in self.db.query(
                f"SELECT wallet_a, wallet_b, link_type FROM wallet_links WHERE wallet_a IN ({ph}) "
                f"AND wallet_b IN ({ph})",
                wallets + wallets,
            ):
                links.append(
                    {
                        "a": scorer.short(lk["wallet_a"]),
                        "b": scorer.short(lk["wallet_b"]),
                        "why": link_text.get(lk["link_type"], lk["link_type"]),
                    }
                )
        ops = {
            r["type"]: r["n"]
            for r in self.db.query(
                f"SELECT type, COUNT(*) AS n FROM activities WHERE wallet IN ({','.join('?' for _ in wallets)}) GROUP BY type",
                wallets,
            )
        }
        return {
            **row,
            "wallets": wallets,
            "tracked_since": first,
            "links": links,
            "whale_bets": self.whale_bets(entity_id, 15, "closed"),
            "whale_open": self.whale_bets(entity_id, 10, "open"),
            "reconcile": reconcile.describe(reconcile.latest(self.db, entity_id)),
            "operations": {
                "split": ops.get("SPLIT", 0),
                "merge": ops.get("MERGE", 0),
                "conversion": ops.get("CONVERSION", 0),
            },
            "copied_count": len(copied),
            "copied_pnl_usd": round(our_pnl, 2),
            "hold_text": "до закрытия рынка" if (s.get("hold_share") or 0) >= 0.6 else "продаёт раньше",
            "series": series,
            "topics": topics,
            "last_bets": last,
        }

    def set_whale(
        self, entity_id: str, mode: str | None = None, category: str | None = None, enabled: bool | None = None
    ) -> dict:
        wl = self.db.one("SELECT * FROM watchlist WHERE entity_id=?", (entity_id,))
        if not wl:
            wl = {
                "entity_id": entity_id,
                "mode": "watch",
                "added_by": "user",
                "added_ts": now_ts(),
                "categories_off": "[]",
            }
        msg = None
        if mode:
            if mode == "copy":
                s = self.db.one(
                    "SELECT status FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (entity_id,)
                )
                if not s or s["status"] != "approved":
                    mode, msg = "watch", "Кит не прошёл проверку — копирование выключено, оставили «Только следить»."
            wl["mode"] = mode
        if category is not None and enabled is not None:
            off = set(json.loads(wl.get("categories_off") or "[]"))
            off.discard(category) if enabled else off.add(category)
            wl["categories_off"] = json.dumps(sorted(off))
        self.db.upsert("watchlist", wl, ["entity_id"])
        return {"entity_id": entity_id, "mode": wl["mode"], "message": msg}

    # ------------------------------------------------------------ экран «Добавить кита»
    def start_check(self, address: str, wait_s: float = 1.5) -> dict:
        """Запускает проверку кошелька в фоне и сразу отвечает: экран показывает этапы и счётчики.
        Если проверка успела закончиться за wait_s — в ответе уже результат."""
        addr = _parse_address(address)
        if not addr:
            return {
                "ok": False,
                "state": "error",
                "error": "Не похоже на адрес кошелька или ссылку на профиль Polymarket",
            }
        if self.data is None and self.check_mode == "local":
            return {"ok": False, "state": "error", "error": "Нет подключения к данным Polymarket"}
        now = time.time()
        with self._checks_lock, self.db.tx():
            self.db.execute("DELETE FROM check_jobs WHERE state IN ('done', 'error') AND finished<?", (now - 3600,))
            row = self.db.one(
                "SELECT job_id FROM check_jobs WHERE address=? AND (state IN ('queued', 'running') "
                "OR (state='done' AND finished>?)) ORDER BY started DESC LIMIT 1",
                (addr, now - CHECK_CACHE_S),
            )
            if row:
                job = self._load_job(row["job_id"]) or {}
            else:
                job = {
                    "job_id": "chk_" + addr[2:10] + str(int(now * 1000) % 100000),
                    "address": addr,
                    "state": "queued" if self.check_mode == "queue" else "running",
                    "stage": "profile",
                    "done": 0,
                    "total": 0,
                    "started": now,
                    "finished": 0.0,
                    "result": None,
                    "error": None,
                    "stages_done": [],
                    "card": None,
                    "timings": None,
                }
                self._save_job(job, force=True)
        if not row and self.check_mode == "local":
            self.run_check_job(job, background=True)
        deadline = time.time() + wait_s
        th = self._check_threads.get(job["job_id"])
        if th is not None:
            th.join(wait_s)
        else:
            while time.time() < deadline and job.get("state") in ("queued", "running"):
                time.sleep(0.1)
                job = self._load_job(job["job_id"]) or job
        return self._check_view(self._load_job(job["job_id"]) or job)

    def check_status(self, job_id: str) -> dict | None:
        job = self._load_job(job_id)
        return self._check_view(job) if job else None

    # ---------- хранение проверок (таблица check_jobs)
    JSON_FIELDS = ("stages_done", "card", "result", "timings")

    def _save_job(self, job: dict, force: bool = False) -> None:
        now = time.time()
        if not force and now - job.get("_saved", 0.0) < 0.3:  # ход проверки — не чаще 3 раз в секунду
            return
        job["_saved"] = now
        row = {
            k: job.get(k)
            for k in ("job_id", "address", "state", "stage", "done", "total", "started", "finished", "error", "worker")
        }
        row.update({k: json.dumps(job.get(k), ensure_ascii=False, default=str) for k in self.JSON_FIELDS})
        row["updated_ts"] = now
        self.db.upsert("check_jobs", row, ["job_id"])

    def _load_job(self, job_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM check_jobs WHERE job_id=?", (job_id,))
        if not row:
            return None
        for k in self.JSON_FIELDS:
            row[k] = json.loads(row[k]) if row.get(k) else None
        row["stages_done"] = row["stages_done"] or []
        row["finished"] = row["finished"] or 0.0
        return row

    def running_checks(self) -> list[dict]:
        return self.db.query("SELECT job_id, address FROM check_jobs WHERE state IN ('queued', 'running')")

    def run_check_job(self, job: dict, background: bool = False) -> None:
        """Выполняет проверку (здесь или в фоновом потоке этого процесса)."""
        if not background:
            return self._run_check(job)
        th = threading.Thread(target=self._run_check, args=(job,), daemon=True, name=f"check-{job['address'][:8]}")
        self._check_threads[job["job_id"]] = th
        th.start()
        return None

    def _run_check(self, job: dict) -> None:
        times = {"_t": time.time()}

        def progress(stage: str, done: int, total: int) -> None:
            if stage != job["stage"]:
                now = time.time()
                times[job["stage"]] = times.get(job["stage"], 0.0) + now - times["_t"]
                times["_t"] = now
                if job["stage"] not in job["stages_done"]:
                    job["stages_done"].append(job["stage"])
            job.update(stage=stage, done=done, total=total)
            self._save_job(job, force=stage != job.get("_saved_stage"))
            job["_saved_stage"] = stage

        try:
            if self.players is not None:  # готовая статистика — сразу, до долгой загрузки истории
                try:
                    job["card"] = self.players.card(job["address"])
                except Exception as exc:
                    log.info("карточка %s: %s", job["address"], exc)
            self._save_job(job, force=True)
            progress("trades", 0, 0)
            with self._deep:  # тяжёлая часть — не больше DEEP_CHECKS одновременно
                job["result"] = self.check_wallet(job["address"], progress=progress)
            job["state"] = "done"
            self._after_check(job["address"], job["result"])
        except Exception as exc:
            job["state"], job["error"] = "error", f"Не удалось проверить: {exc}"[:300]
        job["finished"] = time.time()
        times[job["stage"]] = times.get(job["stage"], 0.0) + job["finished"] - times["_t"]
        job["timings"] = {k: round(v, 1) for k, v in times.items() if k in CHECK_STAGES}
        if job["result"]:
            job["result"]["timings"] = job["timings"]
            log.info("проверка %s: %s", job["address"], job["timings"])
        self._save_job(job, force=True)
        self._check_threads.pop(job["job_id"], None)
        self.db.publish("check", job_id=job["job_id"], state=job["state"])
        r = job["result"] or {}
        if job["state"] == "done" and job["finished"] - job["started"] > 20 and r.get("ok"):
            self.db.notify(
                "whale_check",
                f"Проверка кита {r.get('name')} готова",
                "Подходит для копирования" if r.get("fits") else "Не подходит — можно только следить",
                r.get("entity_id", ""),
            )

    def _check_view(self, job: dict) -> dict:
        out = {
            "ok": job["state"] != "error",
            "job_id": job["job_id"],
            "state": "running" if job["state"] == "queued" else job["state"],  # в очереди — для экрана «идёт»
            "queued": job["state"] == "queued",
            "stage": job["stage"],
            "stage_text": CHECK_STAGES.get(job["stage"], ""),
            "done": job["done"],
            "total": job["total"],
            "stages": [
                {
                    "key": k,
                    "text": t,
                    "status": "done"
                    if (k in job["stages_done"] or job["state"] == "done")
                    else ("active" if k == job["stage"] else "wait"),
                }
                for k, t in CHECK_STAGES.items()
            ],
            "elapsed_s": round((job["finished"] or time.time()) - job["started"], 1),
        }
        if job.get("card"):
            out["card"] = job["card"]
        if job["state"] == "done":
            out.update(job["result"] or {})
            out["state"] = "done"
        if job["state"] == "error":
            out["error"] = job["error"]
        return out

    def check_wallet(self, address: str, progress=None) -> dict:
        """Проверяет кошелёк по тем же правилам, что и остальных (раздел 6).
        Читает и считает только данные этого игрока, а не всю базу."""
        t0 = time.time()
        prog = progress or (lambda *a: None)
        addr = _parse_address(address)
        if not addr:
            return {"ok": False, "error": "Не похоже на адрес кошелька или ссылку на профиль Polymarket"}
        if self.data is None:
            return {"ok": False, "error": "Нет подключения к данным Polymarket"}
        sync = self.db.one("SELECT full_loaded_ts FROM wallet_sync WHERE wallet=?", (addr,))
        full = not (sync and sync["full_loaded_ts"])  # уже загружен поиском кандидатов — только новые сделки
        prog("trades", 0, 0)
        pipeline.ingest_wallet_trades(
            self.db,
            self.data,
            [addr],
            gamma=self.gamma,
            full_history=full,
            max_pages=CHECK_MAX_PAGES if full else None,
            progress=prog,
        )
        if full:
            self.db.upsert("wallet_sync", {"wallet": addr, "full_loaded_ts": now_ts()}, ["wallet"])
        e = self.db.scalar("SELECT entity_id FROM entity_wallets WHERE wallet=?", (addr,), addr)
        wallets = [r["wallet"] for r in self.db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))] or [
            addr
        ]
        try:  # split / merge / conversion — без них позиции считаются неверно
            pipeline.sync_activities(self.db, self.data, wallets, gamma=self.gamma, progress=prog)
        except Exception as exc:
            log.warning("операции кошелька %s: %s", addr, exc)
        if self.gamma is not None:  # рынки старых сделок, которых нет в каталоге
            pipeline.ensure_markets(self.db, self.gamma, pipeline.missing_trade_markets(self.db, 20000, wallets), prog)
        build_positions(self.db, wallets)
        ph = ",".join("?" for _ in wallets)
        if self.clob is not None:
            toks = [
                r["token_id"]
                for r in self.db.query(
                    f"SELECT p.token_id FROM positions p WHERE p.wallet IN ({ph}) AND NOT EXISTS "
                    "(SELECT 1 FROM price_history h WHERE h.token_id=p.token_id) GROUP BY p.token_id "
                    "ORDER BY MAX(p.opened_ts) DESC LIMIT ?",
                    wallets + [CHECK_PRICE_TOKENS],
                )
            ]
            if toks:
                prog("prices", 0, len(toks))
                pipeline.sync_prices(self.db, self.clob, toks, fidelity_min=60, progress=prog)
        prog("score", 0, 1)
        f = scorer.load_frames(self.db, [e])
        stats = {
            "trades": int(len(f["trades"])),
            "markets": int(f["pos"]["market_id"].n_unique()) if f["pos"].height else 0,
            "seconds": None,
        }
        if f["pos"].is_empty() or e not in set(f["pos"]["entity_id"].to_list()):
            stats["seconds"] = round(time.time() - t0, 1)
            return {
                "ok": True,
                "entity_id": e,
                "name": scorer.short(e),
                "fits": False,
                "stats": stats,
                "checks": [{"ok": False, "title": "Нет истории ставок", "text": "У кошелька нет закрытых ставок"}],
            }
        df, cats = scorer.score_entities(
            f["pos"],
            f["trades"],
            f["wallets"],
            f["markets"],
            f["prices"],
            self.p,
            self.clock(),
            entities=[e],
            pvalue_pool=scorer.pvalue_pool(self.db),
        )
        scorer.save_scores(self.db, df, cats, self.clock())
        prog("score", 1, 1)
        rec = None  # сверка с Polymarket больше не нужна: прибыль и позиции берутся у Polymarket готовыми
        r = df.row(0, named=True)
        ch = scorer.approval_checks(r, self.p)
        checks = [
            {
                "ok": ch["enough_history"] and ch["enough_volume"],
                "title": "История достаточная",
                "text": "Больше трёх месяцев и много независимых событий"
                if ch["enough_history"]
                else f"Пока {int(r['n_clusters'])} независимых событий, нужно {self.p.min_clusters}",
            },
            {
                "ok": ch["not_luck"] and ch["edge_positive"] and ch["stable"],
                "title": "Результат не случайный",
                "text": (
                    "Преимущество подтверждено проверкой, а не серией удач"
                    if ch["not_luck"] and ch["edge_positive"] and ch["stable"]
                    else "Результат неустойчивый: в первой и второй половине истории он разный"
                    if ch["not_luck"] and ch["edge_positive"]
                    else "Пока нельзя отличить от удачи"
                ),
            },
            {
                "ok": ch["holds_positions"] and ch["class_ok"],
                "title": "Его ставки можно копировать",
                "text": "Держит позиции до конца, а не торгует на быстрых скачках цены"
                if ch["holds_positions"] and ch["class_ok"]
                else CLASS_REASON_RU.get(r["class"], "Продаёт слишком быстро — повторить не получится"),
            },
        ]
        fits = r["status"] == "approved"
        strong = [
            CATEGORY_RU.get(c, c)
            for c, st in (cats.select("category", "status").iter_rows() if not cats.is_empty() else [])
            if st == "approved"
        ]
        stats["seconds"] = round(time.time() - t0, 1)
        return {
            "ok": True,
            "entity_id": e,
            "name": scorer.short(e),
            "fits": fits,
            "checks": checks,
            "risk": self._risk_level(r),
            "strong_topics": strong,
            "stats": stats,
            "reconcile": rec,
            "hint": None if fits else "Можно добавить в режиме «Только следить» — копирование будет выключено.",
        }

    # ------------------------------------------------------------ экран «Итоги»
    def results(self, period: str = "month") -> dict:
        now = self.clock()
        days = {"week": 7, "month": 30}.get(period)
        s = self.risk.settings()
        bank = float(s["bankroll_usd"])
        first_fill = self.db.scalar("SELECT MIN(ts) AS m FROM fills")
        start = now - days * DAY if days else (first_fill or now)
        eq0 = self.pf.equity(bank, until=start) if first_fill and start >= first_fill else bank
        eq = self.pf.equity(bank)
        points = []
        steps = 10
        for i in range(steps + 1):
            t = int(start + (now - start) * i / steps)
            points.append({"ts": t, "balance_usd": round(self.pf.equity(bank, until=t) if first_fill else bank, 2)})
        sig = self.db.query(
            "SELECT s.status, s.token_id, s.entity_id FROM signals s WHERE s.kind='entry' AND s.ts>=?", (start,)
        )
        made = [x for x in sig if x["status"] == "executed"]
        skipped = sum(1 for x in sig if x["status"] in ("skipped", "rejected", "expired"))
        allpos = self.pf.positions()
        won = lost = waiting = 0
        by_whale: dict[str, dict] = {}
        seen_tok: set = set()
        for x in made:
            p = allpos.get(x["token_id"])
            if not p or x["token_id"] in seen_tok:
                continue  # одна позиция — один раз, даже если сигналов по ней было несколько
            seen_tok.add(x["token_id"])
            fees_paid = p.get("fees", 0.0)
            if p["shares"] > 1e-9:
                waiting += 1
            else:
                pnl = p["proceeds"] - (p["cost"] + p.get("cost_removed", 0.0)) - fees_paid
                won += pnl > 0
                lost += pnl <= 0
            px = self.pf.mark_price(x["token_id"]) or 0
            pnl_now = p["proceeds"] + p["shares"] * px - (p["cost"] + p.get("cost_removed", 0.0)) - fees_paid
            w = by_whale.setdefault(x["entity_id"], {"whale": scorer.short(x["entity_id"]), "bets": 0, "pnl_usd": 0.0})
            w["bets"] += 1
            w["pnl_usd"] += pnl_now
        cal = load_calibrator(self.db)
        if cal.n < 30:
            accuracy = {"label": "Мало данных", "text": "Нужно больше закрытых рынков, чтобы оценить точность"}
        elif not np.isfinite(cal.brier_market):
            accuracy = {"label": "Мало данных", "text": "Нет рыночных цен для сравнения — точность пока не оценить"}
        elif cal.brier_llm_common <= cal.brier_market * 1.02:
            accuracy = {
                "label": "В норме",
                "text": "Когда система оценивает шанс в 70%, такие события сбываются примерно в 7 случаях из 10",
            }
        else:
            accuracy = {
                "label": "Хуже рынка",
                "text": "Независимая оценка пока не лучше рыночной цены — она используется только как фильтр",
            }
        return {
            "period": period,
            "pnl_usd": round(eq - eq0, 2),
            "pnl_pct": round((eq - eq0) / eq0 * 100, 1) if eq0 else 0,
            "balance_usd": round(eq, 2),
            "series": points,
            "bets": {"made": len(made), "won": int(won), "lost": int(lost), "waiting": waiting},
            "skipped_signals": skipped,
            "accuracy": accuracy,
            "by_whale": sorted(
                ({**v, "pnl_usd": round(v["pnl_usd"], 2)} for v in by_whale.values()), key=lambda v: -v["pnl_usd"]
            ),
            "mode": s["mode"],
        }

    # ------------------------------------------------------------ экран «Пробный режим» (приёмка этапа 6 ТЗ)
    JOB_LABELS = {
        "agent": "Агент: новые сделки китов",
        "books": "Стаканы",
        "markets": "Каталог рынков",
        "discover": "Поиск кандидатов",
        "resolutions": "Исходы рынков",
        "prices": "История цен",
        "transfers": "Переводы USDC (антисибил)",
        "activity": "Операции кошельков (split/merge)",
        "reconcile": "Сверка с Polymarket",
        "kalshi": "Рынки Kalshi",
        "odds": "Коэффициенты букмекеров",
        "backup": "Резервная копия базы",
        "warm": "Поддержка соединений",
        "recategorize": "Обновление тем рынков",
        "score": "Пересчёт китов",
        "housekeeping": "Чистка",
        "weekly": "Итоги недели",
    }

    def trial(self, target_signals: int = 200, min_weeks: int = 4, max_drawdown: float = 0.15) -> dict:
        now = self.clock()
        s = self.risk.settings()
        bank = float(s["bankroll_usd"])
        started = self.db.get_setting("paper_started_ts") or self.db.scalar("SELECT MIN(ts) AS m FROM signals") or now
        days_in = max(0.0, (now - started) / DAY)
        counts = {
            r["status"]: r["n"]
            for r in self.db.query("SELECT status, COUNT(*) AS n FROM signals WHERE kind='entry' GROUP BY status")
        }
        n_signals = sum(counts.values())
        buy = self.db.query(
            "SELECT d.q_final, d.vwap_exec, d.ask_now, s.status, t.payout FROM decisions d "
            "JOIN signals s ON s.signal_id=d.signal_id JOIN tokens t ON t.token_id=s.token_id "
            "WHERE s.kind='entry' AND d.decision='BUY' AND d.q_final IS NOT NULL"
        )
        n_buy = len(buy)
        known = [r for r in buy if r["payout"] is not None]
        # калибровка по корзинам: прогноз системы против частоты исхода
        edges = [0.0, 0.4, 0.5, 0.6, 0.7, 0.8, 1.01]
        buckets = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            g = [r for r in known if lo <= r["q_final"] < hi]
            if not g:
                continue
            q = float(np.mean([r["q_final"] for r in g]))
            y = float(np.mean([r["payout"] for r in g]))
            buckets.append(
                {
                    "range": f"{int(lo * 100)}–{min(int(hi * 100), 100)}%",
                    "n": len(g),
                    "predicted_pct": round(q * 100, 1),
                    "actual_pct": round(y * 100, 1),
                    "deviation_pp": round((y - q) * 100, 1),
                    "ok": None if len(g) < 30 else abs(y - q) <= 0.05,
                }
            )
        judged = [b for b in buckets if b["ok"] is not None]
        calib_ok = None if not judged else all(b["ok"] for b in judged)
        mk = [r for r in known if r["ask_now"] is not None]  # система и рынок — на одних и тех же сигналах
        brier_sys = float(np.mean([(r["q_final"] - r["payout"]) ** 2 for r in mk])) if mk else None
        brier_mkt = float(np.mean([(r["ask_now"] - r["payout"]) ** 2 for r in mk])) if mk else None
        hypo = [r["payout"] / r["vwap_exec"] - 1 for r in known if r["vwap_exec"]]
        # кривая капитала и просадка
        pts, peak, dd = [], bank, 0.0
        first_fill = self.db.scalar("SELECT MIN(ts) AS m FROM fills")
        steps = 20
        for i in range(steps + 1):
            t = int(started + (now - started) * i / steps)
            eq = self.pf.equity(bank, until=t) if first_fill and t >= first_fill else bank
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak if peak else 0)
            pts.append({"ts": t, "balance_usd": round(eq, 2)})
        criteria = [
            {
                "key": "signals",
                "title": f"Не меньше {target_signals} сигналов",
                "ok": n_signals >= target_signals or None,
                "value": f"{n_signals} из {target_signals}",
                "progress": min(1.0, n_signals / target_signals),
            },
            {
                "key": "weeks",
                "title": f"Не меньше {min_weeks} недель",
                "ok": days_in >= min_weeks * 7 or None,
                "value": f"{int(days_in)} из {min_weeks * 7} дней",
                "progress": min(1.0, days_in / (min_weeks * 7)),
            },
            {
                "key": "calibration",
                "title": "Калибровка в допуске ±5 п.п.",
                "ok": calib_ok,
                "value": (
                    "мало исходов: нужно 30+ в корзине"
                    if calib_ok is None
                    else ("в допуске" if calib_ok else "вне допуска")
                ),
                "progress": min(1.0, max((b["n"] for b in buckets), default=0) / 30),
            },
            {
                "key": "drawdown",
                "title": f"Просадка не больше {int(max_drawdown * 100)}%",
                "ok": dd <= max_drawdown,
                "value": f"{dd * 100:.1f}%",
                "progress": None,
            },
        ]
        if any(c["ok"] is False for c in criteria):
            verdict = {
                "status": "problem",
                "label": "Есть проблема",
                "text": "Один из критериев не выполняется. Переходить к реальным ставкам нельзя — нужно разобраться.",
            }
        elif all(c["ok"] for c in criteria):
            verdict = {
                "status": "ready",
                "label": "Критерии выполнены",
                "text": "Можно переходить к следующему этапу: юридическая проверка и малый живой запуск.",
            }
        else:
            verdict = {
                "status": "running",
                "label": "Идёт пробный режим",
                "text": "Система копит сигналы и исходы. Ничего делать не нужно — подтверждайте или пропускайте сигналы.",
            }
        cal = self.db.query("SELECT q, p_market, y FROM calibration_log WHERE y IS NOT NULL")
        common = [c for c in cal if c["p_market"] is not None]  # ИИ и рынок сравниваются на одних и тех же рынках
        ai_block = {
            "estimates": int(self.db.scalar("SELECT COUNT(*) AS n FROM llm_estimates", default=0)),
            "with_outcome": len(cal),
            "needed": self.p.calibration_min_n,
            "brier_ai": float(np.mean([(c["q"] - c["y"]) ** 2 for c in common])) if common else None,
            "brier_market": float(np.mean([(c["p_market"] - c["y"]) ** 2 for c in common])) if common else None,
            "mode": self.ai_mode(),
        }
        ai_block["better_than_market"] = (
            None
            if ai_block["brier_ai"] is None or ai_block["brier_market"] is None
            else ai_block["brier_ai"] < ai_block["brier_market"]
        )
        ai_block["trusted"] = bool(ai_block["better_than_market"]) and len(common) >= self.p.calibration_min_n
        jobs_state = scheduler_state(self.db) or {}
        jobs = [
            {
                "name": k,
                "label": self.JOB_LABELS.get(k, k),
                "ok": v.get("ok"),
                "runs": v.get("runs", 0),
                "ago_s": (now - v["last_run"]) if v.get("last_run") else None,
                "error": v.get("error"),
            }
            for k, v in jobs_state.items()
        ]
        return {
            "latency": self._latency(now),
            "tuning": self.tuning(),
            "started_ts": started,
            "days": round(days_in, 1),
            "verdict": verdict,
            "criteria": criteria,
            "signals": {
                "total": n_signals,
                "executed": counts.get("executed", 0),
                "pending": counts.get("pending", 0),
                "skipped_by_system": counts.get("skipped", 0),
                "skipped_by_you": counts.get("rejected", 0),
                "expired": counts.get("expired", 0),
                "buy_recommendations": n_buy,
                "with_outcome": len(known),
            },
            "calibration": {
                "buckets": buckets,
                "brier_system": brier_sys,
                "brier_market": brier_mkt,
                "better_than_market": None if brier_sys is None or brier_mkt is None else brier_sys < brier_mkt,
            },
            "hypothetical_roi_pct": round(float(np.mean(hypo)) * 100, 1) if hypo else None,
            "max_drawdown_pct": round(dd * 100, 1),
            "series": pts,
            "jobs": jobs,
            "ai": ai_block,
        }

    def _latency(self, now: int) -> dict:
        """Задержка: от сделки кита до нашего сигнала (за 7 дней). Для копирования это главное время."""
        rows = [
            r["d"]
            for r in self.db.query(
                "SELECT detected_ts - ts AS d FROM signals WHERE kind='entry' AND detected_ts IS NOT NULL AND ts>=?",
                (now - 7 * DAY,),
            )
            if r["d"] is not None and r["d"] >= 0
        ]
        if not rows:
            return {"n": 0, "median_s": None, "p90_s": None}
        return {
            "n": len(rows),
            "median_s": round(float(np.median(rows)), 1),
            "p90_s": round(float(np.percentile(rows, 90)), 1),
        }

    TUNE_GRID = (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
    TUNE_MIN_N = 30

    def tuning(self) -> dict:
        """Подстройка порога «запас выгоды» по реальным исходам: что было бы при другом пороге.
        Учитываются все сигналы с посчитанным запасом — и купленные, и пропущенные из-за малого запаса."""
        rows = self.db.query(
            "SELECT d.edge_net, d.vwap_exec, d.reasons, t.payout FROM decisions d JOIN signals s ON s.signal_id=d.signal_id "
            "JOIN tokens t ON t.token_id=s.token_id WHERE s.kind='entry' AND d.edge_net IS NOT NULL "
            "AND d.vwap_exec IS NOT NULL AND d.vwap_exec>0 AND t.payout IS NOT NULL"
        )
        res = []
        for r in rows:
            fee = (json.loads(r["reasons"] or "{}").get("details") or {}).get("fee_per_share") or 0.0
            res.append(
                (float(r["edge_net"]), (float(r["payout"]) - fee) / float(r["vwap_exec"]) - 1, float(r["payout"]))
            )
        current = float(
            (self.risk.settings().get("param_overrides") or {}).get("edge_threshold", self.p.edge_threshold)
        )
        grid = []
        for t in self.TUNE_GRID:
            g = [x for x in res if x[0] >= t]
            grid.append(
                {
                    "threshold": t,
                    "n": len(g),
                    "win_pct": round(100 * float(np.mean([x[2] for x in g])), 1) if g else None,
                    "avg_return_pct": round(100 * float(np.mean([x[1] for x in g])), 1) if g else None,
                    "total_return": round(float(np.sum([x[1] for x in g])), 2) if g else 0.0,
                    "current": abs(t - current) < 1e-9,
                }
            )
        ok = [g for g in grid if g["n"] >= self.TUNE_MIN_N]
        best = max(ok, key=lambda g: g["total_return"]) if ok else None
        return {
            "with_outcome": len(res),
            "min_n": self.TUNE_MIN_N,
            "current": current,
            "grid": grid,
            "recommended": best["threshold"] if best else None,
            "text": (
                f"Нужно ещё {self.TUNE_MIN_N - len(res)} сигналов с известным исходом"
                if not best
                else (
                    "Текущий порог лучший по итогам"
                    if abs(best["threshold"] - current) < 1e-9
                    else f"По исходам лучше порог {best['threshold'] * 100:.0f}%: средний доход "
                    f"{str(best['avg_return_pct']).replace('.', ',')}% на ставку на {best['n']} сигналах"
                )
            ),
        }

    def set_tuning(self, edge_threshold: float) -> dict:
        t = float(edge_threshold)
        if not 0.01 <= t <= 0.15:
            return {"ok": False, "error": "Порог должен быть от 1% до 15%"}
        ov = dict(self.risk.settings().get("param_overrides") or {})
        ov["edge_threshold"] = t
        self.db.set_setting("param_overrides", ov)
        self.db.notify("settings", f"Порог выгоды изменён на {t * 100:.0f}%", "Подстройка по итогам пробного режима")
        return {"ok": True, "tuning": self.tuning()}

    # ------------------------------------------------------------ «Настройки», «Первый запуск», уведомления
    def get_settings(self) -> dict:
        s = self.risk.settings()
        started = s.get("paper_started_ts")
        days_paper = (self.clock() - started) / DAY if started else 0
        return {
            **s,
            "auto_available": days_paper >= 60,
            "auto_days_left": max(0, int(60 - days_paper)),
            "ai": self.ai_mode(),
            "chain": self.chain_status(),
            "integrations": self.integrations(),
            "wallet_connected": False,
            "live_trading_enabled": False,
        }

    def integrations(self) -> dict:
        """Что подключено через .env — для экрана настроек (сами ключи не показываются)."""
        import os

        last_backup = (scheduler_state(self.db) or {}).get("backup", {})
        return {
            "password": bool(os.environ.get("WF_UI_PASSWORD")),
            "telegram": bool(os.environ.get("WF_TELEGRAM_BOT_TOKEN") and os.environ.get("WF_TELEGRAM_CHAT_ID")),
            "odds_api": bool(os.environ.get("WF_ODDS_API_KEY")),
            "backup_ok": last_backup.get("ok"),
            "backup_ago_s": (self.clock() - last_backup["last_run"]) if last_backup.get("last_run") else None,
        }

    def update_settings(self, patch: dict) -> dict:
        allowed = set(DEFAULT_SETTINGS)
        errors = {}
        for k, v in patch.items():
            if k not in allowed:
                errors[k] = "неизвестная настройка"
                continue
            if k == "mode":
                if v not in ("paper", "manual", "auto"):
                    errors[k] = "paper | manual | auto"
                    continue
                if v != "paper":
                    errors[k] = "Реальные ставки в этой версии отключены: сначала пробный режим и юридическая проверка"
                    continue
            if k in ("bankroll_usd", "max_bet_usd", "daily_limit_usd") and (not isinstance(v, (int, float)) or v <= 0):
                errors[k] = "должно быть положительным числом"
                continue
            if k == "ai_mode" and v not in ("ai", "manual"):
                errors[k] = "ai | manual"
                continue
            if k == "telegram_channels":
                from .estimator.sources import normalize_channel

                raw = v if isinstance(v, list) else re.split(r"[\s,;]+", str(v or ""))
                raw = [x for x in raw if str(x).strip()]
                good = [normalize_channel(x) for x in raw]
                bad = [x for x, g in zip(raw, good) if not g]
                if bad:
                    errors[k] = "не похоже на публичный канал: " + ", ".join(map(str, bad[:5]))
                    continue
                v = list(dict.fromkeys(good))[:30]
            if k == "news_sources" and not isinstance(v, dict):
                errors[k] = "ожидается {google_news: bool, manifold: bool}"
                continue
            if k == "auto_discover" and not isinstance(v, bool):
                errors[k] = "true | false"
                continue
            if k == "param_overrides":
                t = (v or {}).get("edge_threshold") if isinstance(v, dict) else None
                if (
                    not isinstance(v, dict)
                    or set(v) - {"edge_threshold"}
                    or (t is not None and not (isinstance(t, (int, float)) and 0.01 <= t <= 0.15))
                ):
                    errors[k] = "можно менять только порог выгоды: от 0,01 до 0,15"
                    continue
            if k in ("daily_loss_stop_frac", "monthly_loss_stop_frac") and not (0 < float(v) < 1):
                errors[k] = "доля от 0 до 1"
                continue
            self.db.set_setting(k, v)
        return {"settings": self.get_settings(), "errors": errors}

    # ------------------------------------------------------------ «Диагностика»
    def diagnostics(self, live: bool = False) -> dict:
        """Проверка источников (live — запросы прямо сейчас) + состояние задач и объём данных."""
        from . import __version__ as ver
        from . import diagnostics as diag

        probe = diag.run(self.db) if live else (self.db.get_state("diagnostics") or None)
        jobs_state = scheduler_state(self.db) or {}
        now = self.clock()
        jobs = [
            {
                "name": k,
                "label": self.JOB_LABELS.get(k, k),
                "ok": v.get("ok"),
                "runs": v.get("runs", 0),
                "ago_s": (now - v["last_run"]) if v.get("last_run") else None,
                "error": v.get("error"),
            }
            for k, v in jobs_state.items()
        ]
        counts = {
            k: int(self.db.scalar(f"SELECT COUNT(*) AS n FROM {t}", default=0) or 0)
            for k, t in (
                ("trades", "trades"),
                ("markets", "markets"),
                ("prices", "price_history"),
                ("signals", "signals"),
                ("bets", "fills"),
            )
        }
        st = self.status()
        out = {
            "version": ver,
            "probe": probe,
            "jobs": jobs,
            "counts": counts,
            "usage": self.data_usage(),
            "agent_ok": st["agent_ok"],
            "data_age_s": st["data_age_s"],
            "ai": st["ai"],
            "integrations": self.integrations(),
            "chain": self.chain_status(),
            "streams": self.streams_status(),
            "warm": self.db.get_state("warm") or None,
            "services": self.services_status(),
        }
        out["report"] = self._diag_report(out)
        return out

    def services_status(self) -> list[dict]:
        """Процессы (api / realtime / worker или «всё в одном»): жив ли каждый, когда последний раз работал."""
        from .runtime import services_state

        return [
            {k: v for k, v in s.items() if k != "info"} | {"jobs": s["info"].get("jobs")}
            for s in services_state(self.db)
        ]

    def streams_status(self) -> dict | None:
        """Состояние потоков WebSocket и откуда агент берёт стаканы (поток или запрос)."""
        ag = self.agent
        streams = getattr(ag, "streams", None) if ag is not None else None
        if streams is None:  # потоки — в процессе realtime: берём состояние из его пульса
            from .runtime import service_info

            info = service_info(self.db, "realtime", "all") or {}
            return info.get("streams")
        st = streams.status()
        bs = getattr(ag, "book_stats", None) or {}
        ms = sorted(bs.get("http_ms") or [])
        st["agent_books"] = {
            "from_stream": bs.get("ws", 0),
            "by_request": bs.get("http", 0),
            "request_median_ms": ms[len(ms) // 2] if ms else None,
        }
        return st

    @staticmethod
    def _diag_report(d: dict) -> str:
        """Текст для копирования: его можно прислать разработчику, без ключей и адресов."""
        lines = [f"WhaleFollow {d['version']}"]
        if d["probe"]:
            for c in d["probe"]["checks"]:
                t = f"{c['ms']} мс" + (
                    f", первое подключение {c['cold_ms']} мс"
                    if c.get("cold_ms") and c.get("warm_ms") is not None
                    else ""
                )
                lines.append(f"{'OK ' if c['ok'] else 'ERR'} {c['label']}: {c['detail']} ({t})")
        else:
            lines.append("Проверка источников ещё не запускалась")
        s = d.get("streams")
        if s:
            for key, name in (("trades", "Поток сделок"), ("books", "Поток стаканов")):
                x = s[key]
                state = "работает" if x["live"] else ("не запущен" if not x["started"] else "нет связи")
                lines.append(
                    f"{'OK ' if x['live'] or not x['started'] else 'ERR'} {name}: {state}"
                    + (f", ошибка: {x['error']}" if x.get("error") and not x["live"] else "")
                )
        for sv in d.get("services") or []:
            lines.append(
                f"{'OK ' if sv['alive'] else 'ERR'} процесс {sv['label']}: "
                + (f"работает {sv['uptime_s'] // 60} мин" if sv["alive"] else f"нет пульса {int(sv['last_seen_s'])} с")
            )
        for j in d["jobs"]:
            if j["ok"] is False:
                lines.append(f"ERR задача {j['label']}: {j['error']}")
        u = d["usage"]
        lines.append(
            f"Китов-кошельков: {u['tracked_wallets']}, сделок по ним: {u['trades_tracked']}, "
            f"прочих сделок: {u['trades_other']}"
        )
        lines.append("Данные: " + ", ".join(f"{k}={v}" for k, v in d["counts"].items()))
        lines.append(f"Агент: {'работает' if d['agent_ok'] else 'не отвечает'}; ИИ: {d['ai'].get('effective')}")
        return "\n".join(lines)

    # ------------------------------------------------------------ поиск китов в рейтинге
    LB_CATS = {"overall": "OVERALL", **{k: k.upper() for k in _cats.KEYS}}
    LB_PAGE = 50
    LB_FETCH = 500  # строк рейтинга за один запрос: дальше листание идёт из памяти, без обращений к API
    LB_TTL = 600

    def leaderboard(
        self, category: str = "overall", period: str = "month", order: str = "pnl", offset: int = 0
    ) -> dict:
        """Рейтинг Polymarket: загружается только сам рейтинг (без истории игроков).
        Первые 500 строк приходят одним запросом и 10 минут хранятся в памяти — листание мгновенное."""
        if self.data is None or not hasattr(self.data, "leaderboard"):
            return {"ok": False, "error": "Нет подключения к данным Polymarket", "items": []}
        cat = self.LB_CATS.get(category, "OVERALL")
        period = period if period in ("day", "week", "month", "all") else "month"
        order = order if order in ("pnl", "vol") else "pnl"
        offset = max(0, int(offset))
        need = max(self.LB_FETCH, offset + self.LB_PAGE)
        key = (cat, period, order)
        cache = self.__dict__.setdefault("_lb_cache", {})
        hit = cache.get(key)
        if hit and time.time() - hit[0] < self.LB_TTL and (len(hit[1]) >= need or hit[2]):
            rows, complete = hit[1], hit[2]
        else:
            try:
                rows = self.data.leaderboard(cat, period, order, limit=need, offset=0)
            except Exception as exc:
                return {"ok": False, "error": self._lb_error(exc), "items": []}
            complete = len(rows) < need  # рейтинг кончился раньше — дальше листать нечего
            cache[key] = (time.time(), rows, complete)
        page = rows[offset : offset + self.LB_PAGE]
        tracked_ws = set(tracked.tracked_wallets(self.db))
        ws = [r["wallet"] for r in page]
        ents, statuses = {}, {}
        if ws:
            ph = ",".join("?" for _ in ws)
            ents = {
                r["wallet"]: r["entity_id"]
                for r in self.db.query(f"SELECT wallet, entity_id FROM entity_wallets WHERE wallet IN ({ph})", ws)
            }
            ids = list({ents.get(w, w) for w in ws})
            ph2 = ",".join("?" for _ in ids)
            for r in self.db.query(
                f"SELECT entity_id, status, computed_at FROM wallet_scores "
                f"WHERE entity_id IN ({ph2}) ORDER BY computed_at",
                ids,
            ):
                statuses[r["entity_id"]] = r["status"]  # последний расчёт перезаписывает ранние
        items = []
        for r in page:
            w = r["wallet"]
            sc = statuses.get(ents.get(w, w))
            items.append(
                {
                    **r,
                    "short": scorer.short(w),
                    "roi_pct": round(100 * r["pnl"] / r["volume"], 1)
                    if r.get("pnl") is not None and r.get("volume")
                    else None,
                    "in_list": w in tracked_ws,
                    "checked": STATUS_RU.get(sc) if sc else None,
                    "profile_url": f"https://polymarket.com/profile/{w}",
                }
            )
        return {
            "ok": True,
            "items": items,
            "category": category,
            "period": period,
            "order": order,
            "offset": offset,
            "has_more": offset + self.LB_PAGE < len(rows) or (not complete and len(page) == self.LB_PAGE),
        }

    @staticmethod
    def _lb_error(exc: Exception) -> str:
        import re

        from .diagnostics import explain_status

        m = re.search(r"HTTP (\d{3})", str(exc))
        if m:
            detail, hint = explain_status(int(m.group(1)), "polymarket")
            return f"Рейтинг Polymarket не ответил: {detail}." + (f" {hint}" if hint else "")
        return f"Рейтинг Polymarket не ответил: {exc}"[:240]

    # ------------------------------------------------------------ «что было бы, если бы копировали»
    def simulate(self, entity_id: str, days: int = 90, start: int | None = None, end: int | None = None) -> dict:
        """«Что было бы»: за последние days дней или за выбранные даты [start, end] (ставки, открытые в этот
        отрезок; результат — по факту, как они закрылись)."""
        from .backtest.whale_sim import simulate_whale

        lat = self._latency(self.clock())
        delay = int(lat["median_s"]) if lat.get("median_s") else None
        now = self.clock()
        if start:
            start = int(start)
            end = min(int(end or now), now)
            if end - start < DAY:
                return {"ok": False, "error": "Период должен быть не короче суток"}
            res = simulate_whale(
                self.db,
                self.p,
                self._wallets_of(entity_id),
                self.risk.settings(),
                now,
                delay_s=delay,
                start_ts=start,
                end_ts=end,
            )
        else:
            res = simulate_whale(
                self.db,
                self.p,
                self._wallets_of(entity_id),
                self.risk.settings(),
                now,
                days=max(7, min(int(days), 365)),
                delay_s=delay,
            )
        res["entity_id"] = entity_id
        return res

    # ------------------------------------------------------------ игроки и рынки по готовым данным Polymarket
    def _our_view(self, wallet: str) -> dict:
        e = self.db.scalar("SELECT entity_id FROM entity_wallets WHERE wallet=?", (wallet,), wallet)
        wl = self.db.one("SELECT mode FROM watchlist WHERE entity_id=?", (e,))
        sc = self.db.one("SELECT status FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (e,))
        job = next((j for j in self.running_checks() if j["address"] == wallet), None)
        return {
            "entity_id": e,
            "in_list": bool(wl and wl["mode"] != "off"),
            "mode": (wl or {}).get("mode"),
            "checked": STATUS_RU.get(sc["status"]) if sc else None,
            "can_copy": bool(sc and sc["status"] == "approved"),
            "checking": job["job_id"] if job else None,
            "copy_after_check": wallet in set(self.db.get_state("copy_after_check") or []),
        }

    def player_card(self, wallet: str, mini: bool = False, refresh: bool = False) -> dict:
        addr = _parse_address(wallet)
        if not addr:
            return {"ok": False, "error": "Не похоже на адрес кошелька"}
        if self.players is None:
            return {"ok": False, "error": "Нет подключения к данным Polymarket", "wallet": addr, **self._our_view(addr)}
        try:
            card = self.players.card(addr, with_positions=not mini, refresh=refresh)
        except Exception as exc:
            card = {"ok": False, "wallet": addr, "error": f"Polymarket не ответил: {exc}"[:200]}
        out = {**card, **self._our_view(addr), "short": scorer.short(addr)}
        for pos in out.get("positions") or []:  # в каких позициях игрока мы тоже стоим
            pos["we_hold"] = bool(self.db.one("SELECT 1 AS x FROM fills WHERE token_id=? LIMIT 1", (pos["token_id"],)))
        return out

    def resolve_players(self, q: str) -> dict:
        if self.players is None:
            return {"ok": False, "items": [], "error": "Нет подключения к данным Polymarket"}
        try:
            items = self.players.resolve(q)
        except Exception as exc:
            return {"ok": False, "items": [], "error": f"Поиск не ответил: {exc}"[:200]}
        return {
            "ok": True,
            "items": [{**i, "short": scorer.short(i["wallet"]), **self._our_view(i["wallet"])} for i in items],
        }

    def add_players(self, wallets: list[str], mode: str = "watch") -> dict:
        """Добавить игроков пачкой. Каждый сразу попадает в список «Только следить», в фоне идёт глубокая
        проверка; при mode=copy копирование включится само, если игрок её пройдёт."""
        addrs = list(dict.fromkeys(a for a in (_parse_address(w) for w in wallets or []) if a))[:100]
        if not addrs:
            return {"ok": False, "error": "Нет адресов"}
        pending = set(self.db.get_state("copy_after_check") or [])
        added = []
        for a in addrs:
            e = self.db.scalar("SELECT entity_id FROM entity_wallets WHERE wallet=?", (a,), a)
            sc = self.db.one(
                "SELECT status FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (e,)
            )
            want = "copy" if mode == "copy" and sc and sc["status"] == "approved" else "watch"
            self.set_whale(e, mode=want)
            if mode == "copy" and want != "copy":
                pending.add(a)
            job = self.start_check(a, wait_s=0) if self.data is not None else {}
            added.append({"wallet": a, "entity_id": e, "mode": want, "job_id": job.get("job_id")})
        self.db.set_state("copy_after_check", sorted(pending))
        n = len(added)
        return {
            "ok": True,
            "added": added,
            "message": f"Добавлено: {n}. Проверка идёт в фоне"
            + (" — копирование включится у тех, кто её пройдёт." if mode == "copy" else "."),
        }

    def _after_check(self, wallet: str, result: dict | None) -> None:
        pending = set(self.db.get_state("copy_after_check") or [])
        if wallet not in pending or not result:
            return
        pending.discard(wallet)
        self.db.set_state("copy_after_check", sorted(pending))
        e = result.get("entity_id") or wallet
        if result.get("fits"):
            self.set_whale(e, mode="copy")
            self.db.notify("whale_check", f"Кит {result.get('name')} прошёл проверку — копирование включено", "", e)
        else:
            self.db.notify(
                "whale_check", f"Кит {result.get('name')} не прошёл проверку — оставили «Только следить»", "", e
            )

    def market_panel(self, token_id: str) -> dict | None:
        """Панель рынка для ставки и сигнала: правила, объём, ликвидность, цены исходов, кто держит,
        позиция кита сейчас (готовые данные Polymarket) и график цены."""
        m = self.db.one(
            "SELECT m.*, t.outcome_label FROM tokens t JOIN markets m ON m.market_id=t.market_id WHERE t.token_id=?",
            (token_id,),
        )
        if not m:
            return None
        sig = (
            self.db.one(
                "SELECT entity_id, whale_price, ts FROM signals WHERE token_id=? ORDER BY ts DESC LIMIT 1", (token_id,)
            )
            or {}
        )
        wallets = self._wallets_of(sig["entity_id"]) if sig.get("entity_id") else []
        tracked_ws = set(tracked.tracked_wallets(self.db))
        panel = {}
        if self.players is not None:
            try:
                panel = self.players.market(m["market_id"], token_id, wallets)
            except Exception as exc:
                panel = {"ok": False, "error": str(exc)[:200]}
        for h in panel.get("holders") or []:
            h["in_list"] = h["wallet"] in tracked_ws
            h["short"] = scorer.short(h["wallet"])
        now = self.clock()
        pts = self.db.query(
            "SELECT ts, price FROM price_history WHERE token_id=? AND ts>=? ORDER BY ts", (token_id, now - 30 * DAY)
        )
        if len(pts) < 5 and self.clob is not None:
            try:
                pts = [
                    {"ts": int(t), "price": float(p)}
                    for t, p in (self.clob.prices_history(token_id, start=now - 14 * DAY, fidelity_min=60) or [])
                ]
            except Exception as exc:
                log.info("история цен %s: %s", token_id[:10], exc)
        step = max(1, len(pts) // 240)
        chart = [{"ts": x["ts"], "price": round(x["price"], 4)} for x in pts[::step]]
        return {
            **panel,
            "ok": True,
            "token_id": token_id,
            "question": panel.get("question") or m.get("question"),
            "side": m.get("outcome_label"),
            "category": CATEGORY_RU.get(m.get("category"), "разное"),
            "rules": panel.get("rules") or m.get("rules"),
            "end_ts": panel.get("end_ts") or m.get("end_ts"),
            "volume_usd": panel.get("volume_usd") or m.get("volume_usd"),
            "liquidity_usd": panel.get("liquidity_usd") or m.get("liquidity_usd"),
            "url": panel.get("url") or m.get("url"),
            "fee_rate": fees.market_rate(m),
            "chart": chart,
            "whale_mark": {"ts": sig.get("ts"), "price": sig.get("whale_price")},
            "whale": scorer.short(sig["entity_id"]) if sig.get("entity_id") else None,
        }

    def data_usage(self) -> dict:
        """Сколько данных хранится и сколько из них не относится к китам из списка."""
        import os

        u = tracked.usage(self.db)
        size = None
        if self.db.kind == "sqlite" and self.db.path != ":memory:":
            size = sum(os.path.getsize(self.db.path + x) for x in ("", "-wal") if os.path.exists(self.db.path + x))
        return {
            **u,
            "db_size_mb": round(size / 1e6, 1) if size else None,
            "auto_discover": tracked.auto_discover(self.db),
        }

    def purge_untracked(self) -> dict:
        """Удаляет данные по кошелькам, которых нет в списке китов. Идущие проверки не трогает."""
        keep = [j["address"] for j in self.running_checks()]
        removed = tracked.purge_untracked(self.db, keep)
        if self.db.kind == "sqlite" and self.db.path != ":memory:":

            def vacuum():
                try:
                    import sqlite3

                    c = sqlite3.connect(self.db.path, timeout=120)
                    c.execute("VACUUM")
                    c.close()
                except Exception as exc:
                    log.warning("VACUUM: %s", exc)

            threading.Thread(target=vacuum, daemon=True, name="vacuum").start()
        self.db.notify(
            "settings",
            "Данные по другим кошелькам удалены",
            f"Сделок удалено: {removed.get('trades', 0):,}".replace(",", " "),
        )
        return {"ok": True, "removed": removed, "usage": self.data_usage()}

    def onboarding(self, jurisdiction_ok: bool, risk_ack: bool, paper: bool = True) -> dict:
        if not (jurisdiction_ok and risk_ack):
            return {"ok": False, "error": "Подтвердите оба пункта, чтобы продолжить"}
        self.db.set_setting("onboarding", {"jurisdiction_ok": True, "risk_ack": True, "ts": self.clock()})
        self.db.set_setting("mode", "paper")
        if not self.db.get_setting("paper_started_ts"):
            self.db.set_setting("paper_started_ts", self.clock())
        return {"ok": True, "next_step": 2}

    def notifications(self, limit: int = 20) -> dict:
        now = self.clock()
        rows = self.db.query("SELECT * FROM notifications ORDER BY ts DESC LIMIT ?", (limit,))
        return {
            "items": [
                {
                    "id": r["notif_id"],
                    "kind": r["kind"],
                    "title": r["title"],
                    "body": r["body"],
                    "ref": r["ref"],
                    "when": _fmt_age(r["ts"], now),
                    "unread": not r["is_read"],
                }
                for r in rows
            ]
        }

    def mark_read(self) -> dict:
        self.db.execute("UPDATE notifications SET is_read=1")
        return {"ok": True}

    def stop_all(self) -> dict:
        self.risk.set_stopped(True)
        return self.status()

    def resume(self) -> dict:
        self.risk.set_stopped(False)
        self.db.set_state("paused", None)
        return self.status()

    def journal(self, limit: int = 100) -> dict:
        rows = self.db.query(
            "SELECT s.signal_id, s.ts, s.entity_id, s.token_id, s.kind, s.status, d.decision, d.size_usd, "
            "d.edge_net, d.q_whale, d.q_llm, d.q_final, d.reasons, d.params_version, d.user_action "
            "FROM signals s JOIN decisions d ON d.signal_id=s.signal_id ORDER BY s.ts DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            info = json.loads(r["reasons"] or "{}")
            m = self._market_of_token(r["token_id"])
            out.append(
                {
                    **{
                        k: r[k]
                        for k in (
                            "signal_id",
                            "ts",
                            "kind",
                            "status",
                            "decision",
                            "size_usd",
                            "edge_net",
                            "q_whale",
                            "q_llm",
                            "q_final",
                            "params_version",
                            "user_action",
                        )
                    },
                    "whale": scorer.short(r["entity_id"]),
                    "question": m.get("question"),
                    "reason": reason_text(info.get("codes", [])),
                    "codes": info.get("codes", []),
                }
            )
        return {"items": out}


def _parse_address(text: str) -> str | None:
    m = re.search(r"0x[a-f0-9]{40}", text or "", re.I)
    return m.group(0).lower() if m else None


def utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()
