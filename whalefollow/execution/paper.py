"""Бумажное исполнение и портфель (виртуальные деньги).

Реальное исполнение в v1 не реализовано (ТЗ раздел 1): оно включается только
после приёмки paper trading и юридической проверки (раздел 13).
"""
from __future__ import annotations

from collections import defaultdict

from .. import fees
from ..db import DB, new_id, now_ts
from ..signals.orderbook import walk


class LiveTradingDisabled(RuntimeError):
    pass


class PaperBroker:
    def __init__(self, db: DB):
        self.db = db

    def buy(self, signal_id: str, token_id: str, usd: float, asks, fee_bps: float = 0.0,
            ts: int | None = None, limit_price: float | None = None, fee_rate: float | None = None) -> dict | None:
        """fee_rate — ставка комиссии тейкера Polymarket (fee = акции × ставка × p × (1−p));
        fee_bps — устаревший процент от суммы (если ставка не задана)."""
        levels = [(p, s) for p, s in asks if limit_price is None or p <= limit_price + 1e-9]
        fill = walk(levels, usd=usd)
        if fill.shares <= 0:
            return None
        ts = ts or now_ts()
        oid = new_id("o_")
        fee = fees.fee_usd(fee_rate, fill.shares, fill.avg_price) if fee_rate is not None \
            else fill.filled_usd * fee_bps / 1e4
        with self.db.tx():
            self.db.upsert("orders", {"order_id": oid, "signal_id": signal_id, "ts": ts, "token_id": token_id,
                                      "side": "BUY", "limit_price": limit_price, "size_usd": usd,
                                      "status": "filled" if fill.complete else "partial", "is_paper": 1}, ["order_id"])
            self.db.upsert("fills", {"fill_id": new_id("f_"), "order_id": oid, "ts": ts, "token_id": token_id,
                                     "side": "BUY", "price": fill.avg_price, "size_shares": fill.shares,
                                     "fee_usd": fee}, ["fill_id"])
        return {"order_id": oid, "avg_price": fill.avg_price, "shares": fill.shares, "usd": fill.filled_usd, "fee": fee}

    def sell(self, token_id: str, shares: float, bids, reason: str = "manual", ts: int | None = None,
             fee_bps: float = 0.0, fee_rate: float | None = None) -> dict | None:
        fill = walk(bids, shares=shares)
        if fill.shares <= 0:
            return None
        ts = ts or now_ts()
        oid = new_id("o_")
        fee = fees.fee_usd(fee_rate, fill.shares, fill.avg_price) if fee_rate is not None \
            else fill.filled_usd * fee_bps / 1e4
        with self.db.tx():
            self.db.upsert("orders", {"order_id": oid, "signal_id": reason, "ts": ts, "token_id": token_id,
                                      "side": "SELL", "limit_price": None, "size_usd": fill.filled_usd,
                                      "status": "filled", "is_paper": 1}, ["order_id"])
            self.db.upsert("fills", {"fill_id": new_id("f_"), "order_id": oid, "ts": ts, "token_id": token_id,
                                     "side": "SELL", "price": fill.avg_price, "size_shares": fill.shares,
                                     "fee_usd": fee}, ["fill_id"])
        return {"order_id": oid, "avg_price": fill.avg_price, "shares": fill.shares, "usd": fill.filled_usd, "fee": fee}

    def settle(self, ts: int | None = None) -> list[dict]:
        """Закрывает позиции в разрешённых рынках по выплате 1/0."""
        out = []
        for pos in Portfolio(self.db).open_positions():
            tok = self.db.one("SELECT payout FROM tokens WHERE token_id=?", (pos["token_id"],))
            if not tok or tok["payout"] is None:
                continue
            oid = new_id("o_")
            ts_ = ts or now_ts()
            with self.db.tx():
                self.db.upsert("orders", {"order_id": oid, "signal_id": "settle", "ts": ts_, "token_id": pos["token_id"],
                                          "side": "SETTLE", "limit_price": None, "size_usd": pos["shares"] * tok["payout"],
                                          "status": "filled", "is_paper": 1}, ["order_id"])
                self.db.upsert("fills", {"fill_id": new_id("f_"), "order_id": oid, "ts": ts_, "token_id": pos["token_id"],
                                         "side": "SETTLE", "price": float(tok["payout"]), "size_shares": pos["shares"],
                                         "fee_usd": 0.0}, ["fill_id"])
            # итог всей позиции: выплата + выручка от частичных продаж − вся себестоимость − комиссии
            pnl = (pos["shares"] * tok["payout"] + pos["proceeds"] - pos["cost"] - pos.get("cost_removed", 0.0)
                   - pos.get("fees", 0.0))
            out.append({**pos, "payout": tok["payout"], "pnl": pnl})
        return out


class LiveBroker:
    def __init__(self, *_, **__):
        raise LiveTradingDisabled(
            "Реальные ставки в v1 отключены: сначала приёмка пробного режима и юридическая проверка (ТЗ, разделы 12–13).")


class Portfolio:
    """Наши позиции и деньги — из таблицы fills."""

    def __init__(self, db: DB):
        self.db = db

    def _fills(self, until: int | None = None) -> list[dict]:
        sql = ("SELECT f.*, o.signal_id FROM fills f JOIN orders o ON o.order_id=f.order_id")
        if until is not None:
            return self.db.query(sql + " WHERE f.ts<=? ORDER BY f.ts", (until,))
        return self.db.query(sql + " ORDER BY f.ts")

    def positions(self, until: int | None = None) -> dict[str, dict]:
        acc: dict[str, dict] = defaultdict(lambda: {"shares": 0.0, "cost": 0.0, "bought": 0.0, "proceeds": 0.0,
                                                    "opened_ts": None, "closed_ts": None, "signal_id": None, "fees": 0.0})
        for f in self._fills(until):
            a = acc[f["token_id"]]
            a["fees"] += f["fee_usd"] or 0
            if f["side"] == "BUY":
                a["shares"] += f["size_shares"]
                a["cost"] += f["size_shares"] * f["price"]
                a["bought"] += f["size_shares"]
                a["opened_ts"] = a["opened_ts"] or f["ts"]
                a["signal_id"] = a["signal_id"] or f["signal_id"]
                a["closed_ts"] = None                     # повторный вход после полного закрытия — позиция снова открыта
            else:
                # средняя себестоимость снимается пропорционально проданным акциям
                frac = min(f["size_shares"] / a["shares"], 1.0) if a["shares"] > 0 else 0
                a["proceeds"] += f["size_shares"] * f["price"]
                a["cost_removed"] = a.get("cost_removed", 0.0) + a["cost"] * frac
                a["cost"] -= a["cost"] * frac
                a["shares"] -= f["size_shares"]
                if a["shares"] <= 1e-9:
                    a["shares"] = 0.0
                    a["closed_ts"] = f["ts"]
        for tok, a in acc.items():
            a["token_id"] = tok
        return dict(acc)

    def open_positions(self, until: int | None = None) -> list[dict]:
        return [p for p in self.positions(until).values() if p["shares"] > 1e-9]

    def cash(self, bankroll: float, until: int | None = None) -> float:
        c = bankroll
        for f in self._fills(until):
            usd = f["size_shares"] * f["price"]
            c += -usd if f["side"] == "BUY" else usd
            c -= f["fee_usd"] or 0
        return c

    def mark_price(self, token_id: str, at: int | None = None) -> float | None:
        tok = self.db.one("SELECT payout FROM tokens WHERE token_id=?", (token_id,))
        if tok and tok["payout"] is not None and at is None:
            return float(tok["payout"])
        row = self.db.one("SELECT price FROM price_history WHERE token_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                          (token_id, at or 2**62))
        if row:
            return float(row["price"])
        book = self.db.one("SELECT bids FROM orderbook_snapshots WHERE token_id=? ORDER BY ts DESC LIMIT 1", (token_id,))
        if book:
            import json
            bids = json.loads(book["bids"])
            return float(bids[0][0]) if bids else None
        return None

    def equity(self, bankroll: float, until: int | None = None) -> float:
        eq = self.cash(bankroll, until)
        for p in self.open_positions(until):
            px = self.mark_price(p["token_id"], until)
            eq += p["shares"] * (px if px is not None else p["cost"] / max(p["shares"], 1e-9))
        return eq

    def spent_since(self, ts: int) -> float:
        return float(self.db.scalar("SELECT SUM(size_shares*price) AS s FROM fills WHERE side='BUY' AND ts>=?", (ts,), 0.0))
