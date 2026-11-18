"""Цикл агента: новые сделки китов → сигнал → копируемость → оценка → решение → риск → исполнение.

Каждый сигнал (включая пропущенные) записывается в signals + decisions с причинами,
версией параметров и ссылкой на снимок стакана (ТЗ 10.5).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from statistics import median

import numpy as np

from . import fees
from .config import Params
from .db import DB, new_id, now_ts
from .decision.engine import Decision, decide
from .decision.risk import RiskManager
from .estimator.base import Estimate, MarketContext, NoEstimator
from .estimator.calibration import load_calibrator, record_outcomes
from .estimator.sources import build_sources, gather, gather_forecasts, manipulation_flags, materials_for_prompt
from .events import bus
from .execution.paper import LiveTradingDisabled, PaperBroker, Portfolio
from .ingest import pipeline
from .scoring.scorer import latest_category_scores, short
from .signals import copyability
from .signals.orderbook import best, walk

log = logging.getLogger(__name__)
DAY = 86400


class Agent:
    def __init__(
        self,
        db: DB,
        params: Params,
        data,
        clob,
        gamma=None,
        estimator=None,
        sources: list | str | None = None,
        clock: Callable[[], int] | None = None,
        poll_mode: str = "feed",
        rr_per_tick: int = 10,
        forecast_sources: list | None = None,
    ):
        self.db, self.p = db, params
        self.data, self.clob, self.gamma = data, clob, gamma
        self.estimator = estimator or NoEstimator()
        # sources="settings": источники берутся из настроек пользователя (Google News, Manifold);
        # список — явные источники (тесты); None — без сбора новостей
        self.sources = sources
        self.forecast_sources = forecast_sources or []
        self.clock = clock or now_ts
        self.risk = RiskManager(db, params)
        self.broker = PaperBroker(db)
        self.pf = Portfolio(db)
        self.poll_mode = poll_mode  # feed: лента + круговой опрос; wallets: каждый кошелёк отдельно
        self.rr_per_tick = rr_per_tick
        self._rr = 0
        self.streams = None  # потоки WebSocket (streams.Streams) — подключает cli при запуске
        self._lock = threading.RLock()  # опрос и поток сделок обрабатываются по очереди, без двойных решений
        self._last_poll = 0.0
        self.book_stats = {"ws": 0, "http": 0, "http_ms": []}

    # ------------------------------------------------------------ справочники
    def entity_of(self, wallet: str) -> str:
        row = self.db.one("SELECT entity_id FROM entity_wallets WHERE wallet=?", (wallet,))
        return row["entity_id"] if row else wallet

    def wallets_of(self, entity_id: str) -> list[str]:
        rows = self.db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (entity_id,))
        return [r["wallet"] for r in rows] or [entity_id]

    def watchlist(self) -> dict[str, dict]:
        return {r["entity_id"]: r for r in self.db.query("SELECT * FROM watchlist WHERE mode IN ('copy','watch')")}

    TUNABLE = ("edge_threshold",)  # параметры, которые пользователь может поменять с экрана «Пробный режим»

    def params_now(self, settings: dict | None = None) -> Params:
        """Параметры с учётом подстройки из настроек (только разрешённые поля)."""
        ov = (settings or self.risk.settings()).get("param_overrides") or {}
        ov = {k: float(v) for k, v in ov.items() if k in self.TUNABLE}
        return replace(self.p, **ov) if ov else self.p

    def score_of(self, entity_id: str) -> dict:
        return (
            self.db.one("SELECT * FROM wallet_scores WHERE entity_id=? ORDER BY computed_at DESC LIMIT 1", (entity_id,))
            or {}
        )

    # ------------------------------------------------------------ основной цикл
    STREAM_POLL_S = 20  # пока поток сделок жив, опрос — только страховка от пропусков, раз в 20 с

    def run_once(self) -> dict:
        now = self.clock()
        wl = self.watchlist()
        wallets = [w for e in wl for w in self.wallets_of(e)]
        stream_ok = self.streams is not None and self.streams.trades.live()
        if stream_ok and time.monotonic() - self._last_poll < self.STREAM_POLL_S:
            new = []
        else:
            new = self._poll(wallets)
            self._last_poll = time.monotonic()
        with self._lock:
            self._settle()
            self._expire(now)
            summary = self._process(new, now, wl)
            self.risk.check_stops(now)
        summary["new_trades"] = len(new)
        return summary

    def process_stream_trades(self, rows: list[dict]) -> dict:
        """Сделки китов из потока WebSocket: запись (повторы отсекаются) → рынок → сигнал сразу, без ожидания опроса."""
        new = pipeline.store_trades(self.db, rows, now_ts())
        if not new:
            return {"new_trades": 0}
        if self.streams is not None:
            self.streams.books.want([t["token_id"] for t in new])  # стакан по этому исходу — в поток
        if self.gamma is not None:
            pipeline.ensure_markets(self.db, self.gamma, (t["market_id"] for t in new))
        with self._lock:
            now = self.clock()
            summary = self._process(new, now, self.watchlist())
        summary["new_trades"] = len(new)
        return summary

    ORDER_WINDOW_S = 60  # исполнения одной заявки кита (разные мейкеры) приходят отдельными сделками
    DUP_WINDOW_S = 600  # повторный сигнал по тому же исходу того же кита в течение 10 минут не создаётся

    def _merge_fills(self, trades: list[dict]) -> list[dict]:
        """Склеивает исполнения одной заявки в одну сделку: та же транзакция (или ±60 с), кит, исход, сторона.
        Иначе крупная заявка, разобранная по 10 мейкерам, давала 10 сигналов и 10 наших покупок,
        а «убеждённость» кита считалась по кусочку, а не по всей ставке."""
        out: list[dict] = []
        groups: dict[tuple, dict] = {}
        for t in trades:
            if t.get("source"):
                out.append(t)
                continue
            e = self.entity_of(t["wallet"])
            key = (e, t["token_id"], t["side"], t.get("tx_hash") or int(t["ts"]) // self.ORDER_WINDOW_S)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    **t,
                    "size_usd": float(t["size_usd"]),
                    "size_shares": float(t["size_shares"]),
                    "fills": 1,
                }
                out.append(g)
            else:
                g["size_usd"] += float(t["size_usd"])
                g["size_shares"] += float(t["size_shares"])
                g["fills"] += 1
                g["ts"] = min(g["ts"], t["ts"])
                g["price"] = g["size_usd"] / g["size_shares"] if g["size_shares"] else g["price"]
        return out

    def _process(self, new: list[dict], now: int, wl: dict) -> dict:
        summary = {"new_trades": 0, "signals": 0, "pending": 0, "skipped": 0, "executed": 0, "exits": 0, "merged": 0}
        for t in self._merge_fills(new):
            if t.get("source"):
                continue  # операции split/merge из журнала кошелька — не ставки, сигналов не дают
            e = self.entity_of(t["wallet"])
            mode = wl.get(e, {}).get("mode")
            if t["side"] == "SELL":
                if self._on_whale_exit(t, e, now):
                    summary["exits"] += 1
                continue
            if mode != "copy" or now - t["ts"] > max(self.p.max_delay_s * 20, 3600):
                continue  # старые сделки из догрузки истории сигналами не считаются
            if self.db.one(
                "SELECT 1 AS x FROM signals WHERE entity_id=? AND token_id=? AND kind='entry' AND ts>=?",
                (e, t["token_id"], int(t["ts"]) - self.DUP_WINDOW_S),
            ):
                summary["merged"] += 1  # та же ставка кита пришла ещё раз (дозаливка, опрос после потока)
                continue
            if any(p["token_id"] == t["token_id"] for p in self.pf.open_positions()):
                sig = self.create_signal(t, e)
                self._record(sig, Decision("SKIP", reasons=["already_holding"]), {}, "", "skipped")
                summary["signals"] += 1
                summary["skipped"] += 1
                continue  # кит докупает — у нас позиция уже есть, второй раз не входим
            sig = self.create_signal(t, e)
            summary["signals"] += 1
            res = self.evaluate(sig, now)
            summary[{"pending": "pending", "executed": "executed"}.get(res["status"], "skipped")] += 1
        return summary

    DIRECT_POLL_MAX = 40  # до стольких кошельков опрашиваем каждый напрямую на каждом шаге (лимит /trades — 20/с)

    def _poll(self, wallets: list[str]) -> list[dict]:
        """Новые сделки китов из списка. Пока кошельков немного — каждый запрашивается напрямую,
        общая лента Polymarket не читается. Если кошельков больше DIRECT_POLL_MAX — 1 запрос ленты
        (из неё берутся только сделки китов) + несколько кошельков по кругу."""
        if not wallets:
            return []
        if self.poll_mode == "wallets" or len(wallets) <= self.DIRECT_POLL_MAX:
            new = pipeline.ingest_wallet_trades(self.db, self.data, wallets, gamma=self.gamma, workers=8)
            return sorted(new, key=lambda t: t["ts"])
        new = pipeline.poll_feed(self.db, self.data, set(wallets), gamma=self.gamma)
        k = min(self.rr_per_tick, len(wallets))
        batch = [wallets[(self._rr + i) % len(wallets)] for i in range(k)]
        self._rr = (self._rr + k) % len(wallets)
        new += pipeline.ingest_wallet_trades(self.db, self.data, batch, gamma=self.gamma, workers=4)
        uniq = {t["trade_id"]: t for t in new}
        return sorted(uniq.values(), key=lambda t: t["ts"])

    def run_forever(self, interval_s: float = 5.0, stop: Callable[[], bool] | None = None) -> None:
        while not (stop and stop()):
            try:
                log.info("agent: %s", self.run_once())
            except Exception:
                log.exception("ошибка цикла агента")
            time.sleep(interval_s)

    # ------------------------------------------------------------ сигналы
    def create_signal(self, t: dict, entity_id: str, kind: str = "entry") -> dict:
        sig = {
            "signal_id": new_id("s_"),
            "ts": int(t["ts"]),
            "entity_id": entity_id,
            "wallet": t["wallet"],
            "trade_id": t["trade_id"],
            "token_id": t["token_id"],
            "market_id": t.get("market_id"),
            "whale_price": float(t["price"]),
            "whale_size_usd": float(t["size_usd"]),
            "kind": kind,
            "status": "new",
            "expires_ts": None,
            "detected_ts": int(self.clock()),
        }  # задержка = detected − ts
        if not sig["market_id"]:
            tok = self.db.one("SELECT market_id FROM tokens WHERE token_id=?", (t["token_id"],))
            sig["market_id"] = tok["market_id"] if tok else None
        self.db.upsert("signals", sig, ["signal_id"])
        return sig

    def _book(self, token_id: str, now: int, trigger: str = "signal"):
        """Стакан: из потока (в памяти, мгновенно), если он свежий; иначе — запрос к бирже,
        и исход добавляется в поток, чтобы следующий раз было мгновенно."""
        got = self.streams.books.get(token_id) if self.streams is not None else None
        if got is not None:
            bids, asks = got
            self.book_stats["ws"] += 1
        else:
            t0 = time.perf_counter()
            bids, asks = self.clob.book(token_id)
            self.book_stats["http"] += 1
            self.book_stats["http_ms"] = (self.book_stats["http_ms"] + [int((time.perf_counter() - t0) * 1000)])[-50:]
            if self.streams is not None:
                self.streams.books.want([token_id])
        pipeline.store_book(self.db, token_id, bids, asks, now, trigger)
        return bids, asks, f"{token_id}@{now}"

    def evaluate(self, sig: dict, now: int | None = None) -> dict:
        now = now or self.clock()
        market = self.db.one("SELECT * FROM markets WHERE market_id=?", (sig["market_id"],)) or {}
        score = self.score_of(sig["entity_id"])
        cats = latest_category_scores(self.db, sig["entity_id"])
        cat_score = cats.get(market.get("category") or "other")
        settings = self.risk.settings()
        wl = self.db.one("SELECT categories_off FROM watchlist WHERE entity_id=?", (sig["entity_id"],)) or {}
        ent_pos = self.db.query(
            "SELECT market_id, token_id, remaining_shares, cost_usd, category FROM positions WHERE entity_id=?",
            (sig["entity_id"],),
        )
        costs = [p["cost_usd"] for p in ent_pos if p["category"] == market.get("category") and p["cost_usd"]]
        sig["info"] = json.dumps(self.whale_context(sig, ent_pos), ensure_ascii=False)
        bids, asks, snap = self._book(sig["token_id"], now)
        provisional = min(self.p.max_bet_frac * float(settings["bankroll_usd"]), float(settings["max_bet_usd"]))
        chk = copyability.check(
            signal=sig,
            asks=asks,
            market=market,
            score=score,
            cat_score=cat_score,
            entity_positions=[
                p for p in ent_pos if p["market_id"] != sig["market_id"] or p["token_id"] != sig["token_id"]
            ],
            median_cost_cat=median(costs) if costs else None,
            our_size_usd=provisional,
            params=self.p,
            now=now,
            categories_on=settings.get("categories"),
            categories_off_for_entity=json.loads(wl.get("categories_off") or "[]"),
        )
        if not chk.ok:
            return self._record(sig, Decision("SKIP", reasons=chk.reasons), chk.details, snap, "skipped")

        research = self._research(sig, market, now, settings)
        mode = self.ai_mode(settings)
        est, ai_failed = None, False
        if mode["effective"] == "ai":
            est = self._estimate(sig, market, chk.details["ask_now"], now, research)
            ai_failed = est is None
            if ai_failed:
                self._notify_ai_down(now)
        signal_mode = "ai" if est else "manual"
        cal = load_calibrator(self.db)
        q_llm = cal.apply(est.q) if est else None
        trusted = cal.trusted(self.p.calibration_min_n)
        fee_rate = fees.market_rate(market)
        vwap = chk.details["vwap"]
        params = self.params_now(settings)
        params = params if signal_mode == "ai" else replace(params, require_llm=False)
        fee_share = fees.fee_per_share(fee_rate, vwap)
        dec = decide(
            ask_now=chk.details["ask_now"],
            vwap=vwap,
            edge_cat=chk.details["edge_cat"],
            q_llm=q_llm,
            llm_trusted=trusted,
            fee_per_share=fee_share,
            bankroll=float(settings["bankroll_usd"]),
            conviction=chk.details["conviction"],
            days_left=chk.details["days_left"],
            params=params,
            whale_price=float(sig["whale_price"]),
        )
        details = {
            **chk.details,
            "news_flags": research["flags"],
            "llm_trusted": trusted,
            "fee_rate": fee_rate,
            "fee_per_share": round(fee_share, 5),
            "ai_mode": signal_mode,
            "ai_failed": ai_failed,
            "news_count": len(research["items"]),
            "forecast_count": len(research["forecasts"]),
        }
        if dec.decision != "BUY":
            return self._record(sig, dec, details, snap, "skipped")
        rr = self.risk.cap(dec.size_usd, market=market, entity_id=sig["entity_id"], now=now)
        if rr.blocked:
            dec.decision, dec.size_usd = "SKIP", 0.0
            dec.reasons += rr.reasons
            return self._record(sig, dec, details, snap, "skipped")
        dec.size_usd = rr.size_usd
        dec.reasons += rr.reasons
        # без ИИ решение всегда за пользователем — автопокупки нет
        if settings.get("require_confirmation", True) or signal_mode == "manual":
            sig["expires_ts"] = now + self.p.signal_ttl_s
            res = self._record(sig, dec, details, snap, "pending")
            title = (
                f"Новый сигнал от кита {short(sig['entity_id'])}"
                if signal_mode == "ai"
                else f"Кит {short(sig['entity_id'])} сделал ставку — решите сами"
            )
            self.db.notify("signal", title, market.get("question") or "", sig["signal_id"], now)
            return res
        self._record(sig, dec, details, snap, "pending")
        return self.confirm(sig["signal_id"], auto=True)

    def whale_context(self, sig: dict, ent_pos: list[dict]) -> dict:
        """Согласие других китов и уверенность этого кита — подсказки к решению (не меняют формулу)."""
        tokens = [
            r["token_id"] for r in self.db.query("SELECT token_id FROM tokens WHERE market_id=?", (sig["market_id"],))
        ]
        others = self.db.query(
            "SELECT p.entity_id, p.token_id, SUM(p.remaining_shares) AS sh FROM positions p "
            "JOIN watchlist w ON w.entity_id=p.entity_id AND w.mode IN ('copy','watch') "
            "WHERE p.market_id=? AND p.status='open' AND p.entity_id<>? GROUP BY p.entity_id, p.token_id "
            "HAVING SUM(p.remaining_shares)>0",
            (sig["market_id"], sig["entity_id"]),
        )
        same = sorted({short(r["entity_id"]) for r in others if r["token_id"] == sig["token_id"]})
        opposite = sorted(
            {short(r["entity_id"]) for r in others if r["token_id"] != sig["token_id"] and r["token_id"] in tokens}
        )
        costs = [p["cost_usd"] for p in ent_pos if p.get("cost_usd")]
        typical = median(costs) if costs else None
        had = any(p["token_id"] == sig["token_id"] and (p.get("remaining_shares") or 0) > 0 for p in ent_pos)
        return {
            "same": same,
            "opposite": opposite,
            "typical_bet_usd": round(typical, 2) if typical else None,
            "size_ratio": round(sig["whale_size_usd"] / typical, 2) if typical else None,
            "adding": had,
        }

    # ------------------------------------------------------------ режим ИИ, исследование, оценка
    def ai_mode(self, settings: dict | None = None) -> dict:
        settings = settings or self.risk.settings()
        selected = settings.get("ai_mode", "ai")
        available = getattr(self.estimator, "name", "none") != "none"
        return {
            "selected": selected,
            "available": available,
            "effective": "ai" if selected == "ai" and available else "manual",
        }

    def _notify_ai_down(self, now: int) -> None:
        last = self.db.get_state("ai_down_notified", 0) or 0
        if now - last > 3600:
            self.db.set_state("ai_down_notified", now)
            self.db.notify(
                "ai",
                "ИИ сейчас недоступен — решения за вами",
                "Данные собираются как обычно, сигналы ждут вашего решения.",
                "",
                now,
            )

    def _research(self, sig: dict, market: dict, now: int, settings: dict) -> dict:
        """Новости и прогнозы других рынков — в обоих режимах (с ИИ и без)."""
        out = {"items": [], "forecasts": [], "flags": {}}
        if self.sources is None or not sig.get("market_id"):
            return out
        key = f"research:{sig['market_id']}"
        last = self.db.get_state(key, 0) or 0
        if now - last > self.p.estimate_cache_s:
            if self.sources == "settings":
                news_src, fc_src = build_sources(
                    settings.get("news_sources"), settings.get("telegram_channels"), db=self.db
                )
            else:
                news_src, fc_src = list(self.sources), list(self.forecast_sources)
            try:
                gather(self.db, market, news_src)
                gather_forecasts(self.db, market, fc_src)
            except Exception as exc:
                log.warning("сбор новостей: %s", exc)
            self.db.set_state(key, now)
        out["items"] = self.db.query(
            "SELECT * FROM news_items WHERE market_id=? ORDER BY ts_pub DESC LIMIT 30", (sig["market_id"],)
        )
        out["forecasts"] = self.db.query(
            "SELECT * FROM forecasts WHERE market_id=? AND ts_fetched>=? ORDER BY ts_fetched DESC LIMIT 5",
            (sig["market_id"], now - 7 * DAY),
        )
        if out["items"]:
            pts = [
                (r["ts"], r["price"])
                for r in self.db.query(
                    "SELECT ts, price FROM price_history WHERE token_id=? AND ts>=? ORDER BY ts",
                    (sig["token_id"], now - 14 * DAY),
                )
            ]
            out["flags"] = manipulation_flags(out["items"], pts, now)
        return out

    def _estimate(self, sig: dict, market: dict, ask_now: float, now: int, research: dict) -> Estimate | None:
        """Слепая оценка ИИ. Хранится вероятность «Да»; для токена «Нет» берётся дополнение."""
        yes = self.db.one(
            "SELECT token_id FROM tokens WHERE market_id=? AND outcome_label IN ('Yes','YES','yes')",
            (sig["market_id"],),
        )
        is_no = bool(yes) and yes["token_id"] != sig["token_id"]
        cached = self.db.one(
            "SELECT * FROM llm_estimates WHERE market_id=? AND ts>=? ORDER BY ts DESC LIMIT 1",
            (sig["market_id"], now - self.p.estimate_cache_s),
        )
        if cached:
            q_yes = cached["q"]
            est = Estimate(q=q_yes, confidence=cached["confidence"], model_version=cached["model_version"])
        else:
            flags = research["flags"]
            materials = materials_for_prompt(research["items"], flags) if research["items"] else []
            for f in research["forecasts"]:
                materials.append(
                    {
                        "source": f["source"],
                        "source_type": "forecast",
                        "ts_pub": f["ts_fetched"],
                        "title": f"Similar question elsewhere: {f['title']} — {f['probability']:.0%}",
                        "url": f["url"],
                    }
                )
            ctx = MarketContext(
                market_id=sig["market_id"],
                question=market.get("question") or "",
                rules=market.get("rules") or "",
                end_date=str(market.get("end_ts")),
                category=market.get("category") or "other",
                materials=materials,
            )
            try:
                est = self.estimator.estimate(ctx)
            except Exception as exc:
                log.warning("оценка ИИ: %s", exc)
                est = None
            if est is None:
                return None
            if flags.get("coordinated_text") or flags.get("news_after_price_move"):
                est.confidence = "low"
            q_yes = est.q
            eid = new_id("e_")
            self.db.upsert(
                "llm_estimates",
                {
                    "est_id": eid,
                    "signal_id": sig["signal_id"],
                    "market_id": sig["market_id"],
                    "ts": now,
                    "q": q_yes,
                    "confidence": est.confidence,
                    "base_rate": est.base_rate,
                    "evidence": json.dumps(est.to_dict(), ensure_ascii=False),
                    "blind": 1,
                    "model_version": est.model_version,
                },
                ["est_id"],
            )
            # для калибровки сравниваем с рыночной ценой «Да» на момент оценки
            p_yes = (1 - ask_now) if is_no else (ask_now if yes else None)
            self.db.upsert(
                "calibration_log",
                {
                    "est_id": eid,
                    "market_id": sig["market_id"],
                    "q": q_yes,
                    "p_market": p_yes,
                    "y": None,
                    "brier": None,
                    "brier_market": None,
                },
                ["est_id"],
            )
        est.q = 1 - q_yes if is_no else q_yes
        return est

    def _record(self, sig: dict, dec: Decision, details: dict, snap: str, status: str) -> dict:
        sig["status"] = status
        clean = {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in details.items()}
        with self.db.tx():
            self.db.upsert("signals", sig, ["signal_id"])
            self.db.upsert(
                "decisions",
                {
                    "signal_id": sig["signal_id"],
                    "decided_ts": self.clock(),
                    "ask_now": details.get("ask_now"),
                    "vwap_exec": details.get("vwap"),
                    "q_llm": dec.q_llm,
                    "q_whale": dec.q_whale,
                    "q_final": dec.q_final,
                    "edge_net": dec.edge_net,
                    "kelly_frac": dec.kelly_frac,
                    "size_usd": dec.size_usd,
                    "decision": dec.decision,
                    "reasons": json.dumps({"codes": dec.reasons, "details": clean}, default=str),
                    "params_version": self.p.version(),
                    "snapshot_ref": snap,
                    "user_action": None,
                },
                ["signal_id"],
            )
        bus.publish("signal", signal_id=sig["signal_id"], status=status)
        return {
            "signal_id": sig["signal_id"],
            "status": status,
            "decision": dec.decision,
            "size_usd": dec.size_usd,
            "reasons": dec.reasons,
        }

    # ------------------------------------------------------------ действия пользователя
    def confirm(self, signal_id: str, size_usd: float | None = None, auto: bool = False) -> dict:
        now = self.clock()
        sig = self.db.one("SELECT * FROM signals WHERE signal_id=?", (signal_id,))
        dec = self.db.one("SELECT * FROM decisions WHERE signal_id=?", (signal_id,))
        if not sig or not dec or sig["status"] != "pending":
            return {"signal_id": signal_id, "status": "error", "error": "Сигнал не ждёт решения"}
        if sig["expires_ts"] and now > sig["expires_ts"]:
            self.db.execute("UPDATE signals SET status='expired' WHERE signal_id=?", (signal_id,))
            return {"signal_id": signal_id, "status": "expired", "error": "Время вышло"}
        settings = self.risk.settings()
        if settings.get("mode", "paper") != "paper":
            raise LiveTradingDisabled("Реальные ставки в v1 отключены — работает только пробный режим.")
        market = self.db.one("SELECT * FROM markets WHERE market_id=?", (sig["market_id"],)) or {}
        bids, asks, snap = self._book(sig["token_id"], now, "confirm")
        size = min(size_usd or dec["size_usd"], dec["size_usd"])
        fee_rate = fees.market_rate(market)
        fill = walk(asks, usd=size)
        q_eff = (dec["q_final"] or 0) - self.p.uncertainty_haircut
        edge_now = q_eff - fill.avg_price - fees.fee_per_share(fee_rate, fill.avg_price) if fill.shares else -1
        if edge_now < self.params_now(settings).edge_threshold:
            self.db.execute("UPDATE signals SET status='expired' WHERE signal_id=?", (signal_id,))
            self.db.execute("UPDATE decisions SET user_action=? WHERE signal_id=?", ("price_moved", signal_id))
            return {"signal_id": signal_id, "status": "expired", "error": "Цена ушла — ставка больше не выгодна"}
        rr = self.risk.cap(size, market=market, entity_id=sig["entity_id"], now=now)
        if rr.blocked:
            return {"signal_id": signal_id, "status": "blocked", "error": ", ".join(rr.reasons)}
        # предельная цена: по ней запас выгоды с комиссией ещё не меньше порога
        lim0 = q_eff - self.params_now(settings).edge_threshold
        limit = lim0 - fees.fee_per_share(fee_rate, max(min(lim0, 1.0), 0.0))
        res = self.broker.buy(
            signal_id, sig["token_id"], rr.size_usd, asks, ts=now, fee_rate=fee_rate, limit_price=limit
        )
        if not res:
            return {"signal_id": signal_id, "status": "error", "error": "Не удалось исполнить"}
        self.db.execute("UPDATE signals SET status='executed' WHERE signal_id=?", (signal_id,))
        self.db.execute(
            "UPDATE decisions SET user_action=? WHERE signal_id=?", ("auto" if auto else "confirmed", signal_id)
        )
        bus.publish("bet", signal_id=signal_id)
        return {"signal_id": signal_id, "status": "executed", **res}

    def reject(self, signal_id: str) -> dict:
        self.db.execute("UPDATE signals SET status='rejected' WHERE signal_id=? AND status='pending'", (signal_id,))
        self.db.execute("UPDATE decisions SET user_action='skipped' WHERE signal_id=?", (signal_id,))
        bus.publish("signal", signal_id=signal_id, status="rejected")
        return {"signal_id": signal_id, "status": "rejected"}

    def resolve_exit(self, signal_id: str, action: str) -> dict:
        """Ответ пользователя на «кит вышел из позиции»: hold | sell."""
        sig = self.db.one("SELECT * FROM signals WHERE signal_id=? AND kind='whale_exit'", (signal_id,))
        if not sig or sig["status"] != "pending":
            return {"signal_id": signal_id, "status": "error", "error": "Нет такого запроса"}
        if action == "sell":
            pos = next((p for p in self.pf.open_positions() if p["token_id"] == sig["token_id"]), None)
            if pos:
                bids, _, _ = self._book(sig["token_id"], self.clock(), "exit")
                market = self.db.one("SELECT * FROM markets WHERE market_id=?", (sig["market_id"],)) or {}
                res = self.broker.sell(
                    sig["token_id"],
                    pos["shares"],
                    bids,
                    reason=signal_id,
                    ts=self.clock(),
                    fee_rate=fees.market_rate(market),
                )
                self.db.execute("UPDATE signals SET status='executed' WHERE signal_id=?", (signal_id,))
                self.db.execute("UPDATE decisions SET user_action='sold' WHERE signal_id=?", (signal_id,))
                return {"signal_id": signal_id, "status": "sold", **(res or {})}
        self.db.execute("UPDATE signals SET status='executed' WHERE signal_id=?", (signal_id,))
        self.db.execute("UPDATE decisions SET user_action='held' WHERE signal_id=?", (signal_id,))
        return {"signal_id": signal_id, "status": "held"}

    def sell_position(self, token_id: str) -> dict | None:
        """Ручная продажа нашей позиции целиком по лучшим ценам покупателей (виртуально)."""
        pos = next((p for p in self.pf.open_positions() if p["token_id"] == token_id), None)
        if not pos:
            return None
        settings = self.risk.settings()
        if settings.get("mode", "paper") != "paper":
            raise LiveTradingDisabled("Реальные ставки в v1 отключены — работает только пробный режим.")
        tok = self.db.one("SELECT market_id FROM tokens WHERE token_id=?", (token_id,)) or {}
        market = self.db.one("SELECT * FROM markets WHERE market_id=?", (tok.get("market_id"),)) or {}
        bids, _, _ = self._book(token_id, self.clock(), "manual_sell")
        return self.broker.sell(
            token_id, pos["shares"], bids, reason="manual", ts=self.clock(), fee_rate=fees.market_rate(market)
        )

    # ------------------------------------------------------------ события
    EXIT_LEFT_SHARE = (
        0.1  # «вышел» — у кита осталось не больше 10% купленного; меньшая продажа — это частичная фиксация
    )

    def whale_left_share(self, entity_id: str, token_id: str) -> float | None:
        ws = self.wallets_of(entity_id)
        ph = ",".join("?" for _ in ws)
        r = (
            self.db.one(
                "SELECT SUM(CASE WHEN side='BUY' THEN size_shares ELSE 0 END) AS b, "
                "SUM(CASE WHEN side='SELL' THEN size_shares ELSE 0 END) AS s FROM trades "
                f"WHERE token_id=? AND wallet IN ({ph})",
                [token_id] + ws,
            )
            or {}
        )
        if not r.get("b"):
            return None
        return max(0.0, (r["b"] - (r["s"] or 0)) / r["b"])

    def _on_whale_exit(self, t: dict, entity_id: str, now: int) -> bool:
        ours = [p for p in self.pf.open_positions() if p["token_id"] == t["token_id"]]
        if not ours:
            return False
        left = self.whale_left_share(entity_id, t["token_id"])
        if left is not None and left > self.EXIT_LEFT_SHARE:
            return False  # кит лишь частично зафиксировал прибыль — позиция у него осталась
        if self.db.one(
            "SELECT 1 AS x FROM signals WHERE kind='whale_exit' AND token_id=? AND status='pending'", (t["token_id"],)
        ):
            return False  # запрос «кит вышел» по этой ставке уже ждёт решения
        orig = self.db.one(
            "SELECT s.entity_id, d.q_final FROM signals s JOIN decisions d ON d.signal_id=s.signal_id "
            "WHERE s.signal_id=?",
            (ours[0]["signal_id"],),
        )
        if not orig or orig["entity_id"] != entity_id:
            return False
        bids, _, snap = self._book(t["token_id"], now, "whale_exit")
        bid = best(bids) or 0.0
        sig = self.create_signal(t, entity_id, kind="whale_exit")
        q_eff = (orig["q_final"] or 0) - self.p.uncertainty_haircut
        recommend = "HOLD" if q_eff > bid + self.p.edge_threshold else "SELL"
        pos = ours[0]
        sig["status"] = "pending"
        with self.db.tx():
            self.db.upsert("signals", sig, ["signal_id"])
            self.db.upsert(
                "decisions",
                {
                    "signal_id": sig["signal_id"],
                    "decided_ts": now,
                    "ask_now": None,
                    "vwap_exec": bid,
                    "q_llm": None,
                    "q_whale": None,
                    "q_final": orig["q_final"],
                    "edge_net": q_eff - bid,
                    "kelly_frac": None,
                    "size_usd": round(pos["shares"] * bid, 2),
                    "decision": recommend,
                    "reasons": json.dumps(
                        {"codes": ["whale_exit"], "details": {"bid": bid, "shares": pos["shares"], "cost": pos["cost"]}}
                    ),
                    "params_version": self.p.version(),
                    "snapshot_ref": snap,
                    "user_action": None,
                },
                ["signal_id"],
            )
        market = self.db.one("SELECT question FROM markets WHERE market_id=?", (sig["market_id"],)) or {}
        self.db.notify(
            "whale_exit",
            f"Кит {short(entity_id)} вышел из позиции — нужно решение",
            market.get("question") or "",
            sig["signal_id"],
            now,
        )
        return True

    def _settle(self) -> None:
        for s in self.broker.settle(self.clock()):
            q = (
                self.db.one(
                    "SELECT m.question FROM tokens t JOIN markets m ON m.market_id=t.market_id WHERE t.token_id=?",
                    (s["token_id"],),
                )
                or {}
            )
            word = "выиграла" if s["pnl"] > 0 else "проиграла"
            self.db.notify(
                "settled",
                f"Ставка {word}: {'↑ +' if s['pnl'] > 0 else '↓ −'}${abs(s['pnl']):.0f}",
                q.get("question") or "",
                s["token_id"],
                self.clock(),
            )
        record_outcomes(self.db)

    def _expire(self, now: int) -> None:
        self.db.execute(
            "UPDATE signals SET status='expired' WHERE status='pending' AND kind='entry' "
            "AND expires_ts IS NOT NULL AND expires_ts<?",
            (now,),
        )
