"""Virtual trading in US dollars against live Polymarket order books.

* Market orders take liquidity from the current CLOB book (fill-and-kill): a buy spends a
  dollar amount including fees, a sell sells a number of shares.
* Limit orders first take any crossing liquidity, the remainder rests in "Open Orders" and
  fills at its own price once the book reaches it (as a maker, so taker-only fees do not apply).
* Fees follow the market's Polymarket schedule: shares x rate x (p x (1 - p)) ^ exponent,
  charged to the taker side only when the schedule says so.
* Resolved markets pay each share its final outcome price ($1 or $0).
"""

import json
import threading
import time

from . import books
from . import bus
from . import db as store
from . import polymarket as pm

MIN_BUY = 1.0
EPS = 1e-9


def r6(x):
    return round(float(x) + 0.0, 6)


def fee_per_share(price, rate, exponent=1.0):
    return rate * (price * (1 - price)) ** exponent if rate else 0.0


# ---------------------------------------------------------------- accounts

def main_account(db, uid):
    return store.one(db.execute("SELECT * FROM accounts WHERE user_id=? AND kind='main'", (uid,)))


def owned_account(db, uid, aid):
    acc = store.one(db.execute("SELECT * FROM accounts WHERE id=? AND user_id=?", (int(aid), uid)))
    if not acc:
        raise LookupError("Счёт не найден")
    return acc


def _move(db, aid, amount, kind, note="", ref=None):
    """Change a balance and append to the ledger (inside an open transaction)."""
    bal = db.execute("SELECT balance FROM accounts WHERE id=?", (aid,)).fetchone()[0]
    new = r6(bal + amount)
    if new < -EPS:
        raise ValueError("Недостаточно средств на счёте")
    new = max(new, 0.0)
    db.execute("UPDATE accounts SET balance=? WHERE id=?", (new, aid))
    db.execute("INSERT INTO ledger(account_id, kind, amount, balance_after, note, ref, at) VALUES(?,?,?,?,?,?,?)",
               (aid, kind, r6(amount), new, note, ref, time.time()))
    bus.publish("account", {"account_id": aid})  # open pages of this person refresh their numbers
    return new


def _amount(value, name="Сумма"):
    amount = pm.num(value)
    if amount is None or not 0 < amount <= 1e9:
        raise ValueError(f"{name}: укажите положительное число")
    return r6(amount)


def deposit(db, uid, aid, amount):
    acc = owned_account(db, uid, aid)
    amount = _amount(amount)
    with store.tx(db):
        _move(db, acc["id"], amount, "deposit", "Пополнение виртуального счёта")
    return summary(db, uid, acc["id"])


def withdraw(db, uid, aid, amount):
    """Main account: removes virtual dollars. Copy profile: returns free cash to the main account."""
    acc = owned_account(db, uid, aid)
    amount = _amount(amount)
    with store.tx(db):
        if acc["balance"] + EPS < amount:
            raise ValueError("Вывести можно только свободные средства")
        if acc["kind"] == "copy":
            main = main_account(db, uid)
            _move(db, acc["id"], -amount, "transfer_out", "Вывод на основной счёт")
            _move(db, main["id"], amount, "transfer_in", f"Вывод из профиля «{acc['name']}»")
        else:
            _move(db, acc["id"], -amount, "withdraw", "Списание с виртуального счёта")
    return summary(db, uid, acc["id"])


def transfer_from_main(db, uid, aid, amount):
    acc = owned_account(db, uid, aid)
    if acc["kind"] != "copy":
        raise ValueError("Перевод возможен только в профиль копирования")
    main = main_account(db, uid)
    amount = _amount(amount)
    with store.tx(db):
        if main["balance"] + EPS < amount:
            raise ValueError("На основном счёте недостаточно средств")
        _move(db, main["id"], -amount, "transfer_out", f"Перевод в профиль «{acc['name']}»")
        _move(db, acc["id"], amount, "transfer_in", "Перевод с основного счёта")
    return summary(db, uid, acc["id"])


# ---------------------------------------------------------------- market metadata

INFO_TTL = 20.0              # seconds a market's terms are reused (quotes, copies of a burst of fills)
_info_cache = {}
_info_lock = threading.Lock()


def market_info(cond, tok):
    """Gamma terms of one outcome: name, index, fees, size limits and status (reused for INFO_TTL seconds)."""
    cond, tok = pm.condition(cond), pm.token(tok)
    now = time.monotonic()
    with _info_lock:
        hit = _info_cache.get((cond, tok))
        if hit and now - hit[0] < INFO_TTL:
            return hit[1]
    info = _market_info(cond, tok)
    with _info_lock:
        if len(_info_cache) > 2000:
            _info_cache.clear()
        _info_cache[(cond, tok)] = (now, info)
    return info


def market_info_cached(cond, tok):
    with _info_lock:
        hit = _info_cache.get((pm.condition(cond), pm.token(tok)))
    return bool(hit and time.monotonic() - hit[0] < INFO_TTL)


def _market_info(cond, tok):
    raw = pm.markets_by_condition([cond]).get(cond) or pm.markets_by_condition([cond], closed=True).get(cond)
    if not raw:
        raise LookupError("Рынок не найден на Polymarket")
    c = pm.contract(raw)
    index = next((i for i, o in enumerate(c["outcomes"]) if o["token"] == tok), None)
    if index is None:
        raise ValueError("Исход не относится к этому рынку")
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    ev = events[0] if events and isinstance(events[0], dict) else {}
    return {**c, "index": index, "outcome": c["outcomes"][index]["name"], "token": tok,
            "event_slug": pm.text(ev.get("slug"), 240), "event_title": pm.text(ev.get("title"), 700),
            "event_image": pm.text(ev.get("image"), 600), "raw": raw}


# ---------------------------------------------------------------- matching

def _take(levels, *, side, budget=None, shares=None, cap=None, rate=0.0, exponent=1.0, charge=True):
    """Walk book levels. Buy: spend up to budget (incl. fees). Sell: sell up to shares."""
    legs, spent, got = [], 0.0, 0.0
    for price, size in levels:
        if cap is not None and ((side == "buy" and price > cap + EPS) or (side == "sell" and price < cap - EPS)):
            break
        f = fee_per_share(price, rate, exponent) if charge else 0.0
        if side == "buy":
            left = budget - spent
            if left <= 1e-7:
                break
            q = min(size, left / (price + f))
        else:
            left = shares - got
            if left <= 1e-7:
                break
            q = min(size, left)
        q = int(q * 1e6) / 1e6
        if q <= 0:
            break
        fee = r6(q * f)
        legs.append((price, q, fee))
        if side == "buy":
            spent += q * price + fee
        got += q
    return legs


def _fill_summary(legs):
    shares = sum(q for _, q, _ in legs)
    gross = sum(p * q for p, q, _ in legs)
    fees = sum(f for _, _, f in legs)
    return {"shares": r6(shares), "gross": r6(gross), "fees": r6(fees),
            "avg": (gross / shares) if shares else None}


def available_shares(db, aid, tok):
    pos = db.execute("SELECT shares FROM positions WHERE account_id=? AND token_id=? AND status='open'", (aid, tok)).fetchone()
    held = pos[0] if pos else 0.0
    locked = db.execute("""SELECT COALESCE(SUM(shares - filled_shares),0) FROM orders WHERE account_id=? AND token_id=?
                           AND side='sell' AND status='open'""", (aid, tok)).fetchone()[0]
    return max(0.0, r6(held - locked))


def _spec(body):
    side = body.get("side")
    kind = body.get("type", "market")
    if side not in {"buy", "sell"} or kind not in {"market", "limit"}:
        raise ValueError("Укажите сторону (покупка/продажа) и тип заявки")
    spec = {"side": side, "type": kind, "cond": pm.condition(body.get("condition")), "token": pm.token(body.get("token"))}
    if kind == "market" and side == "buy":
        spec["amount"] = _amount(body.get("amount"), "Сумма покупки")
        if spec["amount"] < MIN_BUY:
            raise ValueError("Минимальная покупка — $1")
    else:
        spec["shares"] = _amount(body.get("shares"), "Количество акций")
    if kind == "limit":
        price = pm.num(body.get("price"))
        if price is None or not 0 < price < 1:
            raise ValueError("Лимитная цена должна быть от $0.001 до $0.999")
        spec["price"] = price
        exp = pm.num(body.get("expires"))
        spec["expires"] = exp if exp and exp > time.time() else None
    cap = pm.num(body.get("max_price"))
    spec["cap"] = cap if cap is not None and 0 < cap < 1 else None
    return spec


def quote(db, uid, body):
    """Preview a fill on the current book without changing anything (used by the order ticket)."""
    spec = _spec({**body, "amount": body.get("amount") or 1, "shares": body.get("shares") or 1})
    info = market_info(spec["cond"], spec["token"])
    bk = books.get(spec["token"], watch_ttl=120)   # the ticket re-quotes as the person types: keep this book live
    levels = bk["asks"] if spec["side"] == "buy" else bk["bids"]
    cap = spec.get("price") if spec["type"] == "limit" else spec["cap"]
    legs = _take(levels, side=spec["side"], budget=spec.get("amount"), shares=spec.get("shares"), cap=cap,
                 rate=info["fee_rate"], exponent=info["fee_exponent"])
    s = _fill_summary(legs)
    best = levels[0][0] if levels else None
    out = {"best": best, "fee_rate": info["fee_rate"], "min_size": info["min_size"], "tick": info["tick"], **s}
    if spec["side"] == "buy":
        out["to_win"] = s["shares"]
        out["cost"] = r6(s["gross"] + s["fees"])
    else:
        out["receive"] = r6(s["gross"] - s["fees"])
    if spec["type"] == "limit":
        rest = max(0.0, spec["shares"] - s["shares"])
        out["resting"] = r6(rest)
        if spec["side"] == "buy":
            out["cost"] = r6(s["gross"] + s["fees"] + rest * spec["price"])
            out["to_win"] = spec["shares"]
        else:
            out["receive"] = r6(s["gross"] - s["fees"] + rest * spec["price"])
    if uid:
        acc = main_account(db, uid) if not body.get("account") else owned_account(db, uid, body["account"])
        out["balance"] = acc["balance"]
        out["available_shares"] = available_shares(db, acc["id"], spec["token"])
    return out


def _apply_fills(db, order, legs, liquidity):
    """Record fills, move cash and update the position (inside a transaction)."""
    aid, tok, now = order["account_id"], order["token_id"], time.time()
    total_q = total_gross = total_fee = 0.0
    for price, q, fee in legs:
        db.execute("INSERT INTO fills(order_id, account_id, token_id, side, price, shares, fee, liquidity, at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (order["id"], aid, tok, order["side"], price, q, fee, liquidity, now))
        total_q += q
        total_gross += price * q
        total_fee += fee
    if total_q <= 0:
        return 0.0
    pos = store.one(db.execute("SELECT * FROM positions WHERE account_id=? AND token_id=?", (aid, tok)))
    if not pos or pos["status"] != "open":
        db.execute("""INSERT INTO positions(account_id, token_id, condition_id, event_slug, title, outcome, outcome_index, image,
                      shares, cost, bought, realized, status, updated) VALUES(?,?,?,?,?,?,?,?,0,0,0,COALESCE(?,0),'open',?)
                      ON CONFLICT(account_id, token_id) DO UPDATE SET status='open', shares=0, cost=0, bought=0, payout=NULL""",
                   (aid, tok, order["condition_id"], order["event_slug"], order["title"], order["outcome"],
                    order.get("outcome_index"), order["image"], pos["realized"] if pos else 0, now))
        pos = store.one(db.execute("SELECT * FROM positions WHERE account_id=? AND token_id=?", (aid, tok)))
    if order["side"] == "buy":
        cash = -(total_gross + total_fee)
        if order["type"] == "limit" and liquidity == "maker":
            # A resting buy already reserved shares x limit price; release the reserve into the fill.
            db.execute("UPDATE orders SET reserved=MAX(0, reserved - ?) WHERE id=?", (r6(total_q * order["limit_price"]), order["id"]))
            _move(db, aid, r6(total_q * order["limit_price"] + cash), "trade", order["title"], order["id"])
        else:
            _move(db, aid, r6(cash), "trade", order["title"], order["id"])
        db.execute("UPDATE positions SET shares=?, cost=?, bought=?, last_price=?, price_at=?, updated=? WHERE account_id=? AND token_id=?",
                   (r6(pos["shares"] + total_q), r6(pos["cost"] + total_gross + total_fee), r6(pos["bought"] + total_q),
                    legs[-1][0], now, now, aid, tok))
    else:
        proceeds = total_gross - total_fee
        avg_cost = pos["cost"] / pos["shares"] if pos["shares"] > EPS else 0.0
        removed = avg_cost * total_q
        left = r6(pos["shares"] - total_q)
        _move(db, aid, r6(proceeds), "trade", order["title"], order["id"])
        db.execute("""UPDATE positions SET shares=?, cost=?, realized=?, last_price=?, price_at=?, status=?, updated=?
                      WHERE account_id=? AND token_id=?""",
                   (max(left, 0.0), r6(max(pos["cost"] - removed, 0.0)), r6(pos["realized"] + proceeds - removed),
                    legs[-1][0], now, "open" if left > 1e-6 else "closed", now, aid, tok))
    db.execute("""UPDATE orders SET filled_shares=filled_shares+?, filled_cost=filled_cost+?, fees=fees+?, updated=? WHERE id=?""",
               (r6(total_q), r6(total_gross), r6(total_fee), now, order["id"]))
    return total_q


def place(db, uid, body, *, account_id=None, source="manual", copy_trade_id=None, info=None, book=None):
    """Validate, match against the live book and store an order. Returns the stored order."""
    spec = _spec(body)
    acc = owned_account(db, uid, account_id or body.get("account") or main_account(db, uid)["id"])
    info = info or market_info(spec["cond"], spec["token"])
    if info["closed"] or not info["accepting"]:
        raise ValueError("Рынок закрыт для торговли")
    if spec["type"] == "limit" and spec["shares"] + EPS < info["min_size"]:
        raise ValueError(f"Минимум для лимитной заявки — {info['min_size']:g} акций")
    if spec["type"] == "limit":
        tick = info["tick"] or 0.01
        spec["price"] = round(round(spec["price"] / tick) * tick, 6)
        if not 0 < spec["price"] < 1:
            raise ValueError("Цена вне допустимого шага рынка")
    bk = book or books.get(spec["token"])
    side = spec["side"]
    levels = bk["asks"] if side == "buy" else bk["bids"]
    now = time.time()
    with store.tx(db):
        acc = store.one(db.execute("SELECT * FROM accounts WHERE id=?", (acc["id"],)))
        if side == "sell" and spec["shares"] > available_shares(db, acc["id"], spec["token"]) + 1e-6:
            raise ValueError("Недостаточно акций для продажи")
        budget = None
        if side == "buy":
            budget = spec["amount"] if spec["type"] == "market" else None
            if spec["type"] == "market" and budget > acc["balance"] + EPS:
                raise ValueError("Недостаточно средств на счёте")
        cap = spec["price"] if spec["type"] == "limit" else spec["cap"]
        if spec["type"] == "limit" and side == "buy":
            # Take crossing asks first, but never spend more than shares x limit price plus fees.
            rough = spec["shares"] * spec["price"]
            need = rough + spec["shares"] * fee_per_share(spec["price"], info["fee_rate"], info["fee_exponent"])
            if rough > acc["balance"] + EPS:
                raise ValueError("Недостаточно средств для лимитной заявки")
            legs = _take(levels, side="buy", budget=min(need, acc["balance"]), cap=cap,
                         rate=info["fee_rate"], exponent=info["fee_exponent"])
            # Trim to the requested number of shares.
            trimmed, have = [], 0.0
            for p, q, f in legs:
                q2 = min(q, spec["shares"] - have)
                if q2 <= 1e-7:
                    break
                trimmed.append((p, r6(q2), r6(f * q2 / q) if q else 0.0))
                have += q2
            legs = trimmed
        else:
            legs = _take(levels, side=side, budget=budget, shares=spec.get("shares"), cap=cap,
                         rate=info["fee_rate"], exponent=info["fee_exponent"])
        cur = db.execute("""INSERT INTO orders(account_id, token_id, condition_id, event_slug, title, outcome, image, side, type,
                            amount, shares, limit_price, fee_rate, fee_exponent, taker_only, status, source, copy_trade_id,
                            expires, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (acc["id"], spec["token"], spec["cond"], info["event_slug"], info["question"] or info["event_title"],
                          info["outcome"], info["image"] or info["event_image"], side, spec["type"], spec.get("amount"),
                          spec.get("shares"), spec.get("price"), info["fee_rate"], info["fee_exponent"], int(info["taker_only"]),
                          "pending", source, copy_trade_id, spec.get("expires"), now, now))
        order = store.one(db.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)))
        order["outcome_index"] = info["index"]
        filled = _apply_fills(db, order, legs, "taker") if legs else 0.0
        if spec["type"] == "market":
            status = "filled" if filled > 0 else "rejected"
            reason = "" if filled > 0 else "В стакане нет подходящей ликвидности"
            db.execute("UPDATE orders SET status=?, reason=?, updated=? WHERE id=?", (status, reason, now, order["id"]))
            if not filled:
                raise ValueError(reason)
        else:
            rest = r6(spec["shares"] - filled)
            if rest <= 1e-6:
                db.execute("UPDATE orders SET status='filled', updated=? WHERE id=?", (now, order["id"]))
            else:
                if side == "buy":
                    reserve = r6(rest * spec["price"])
                    _move(db, acc["id"], -reserve, "reserve", "Резерв лимитной заявки", order["id"])
                    db.execute("UPDATE orders SET reserved=?, status='open', updated=? WHERE id=?", (reserve, now, order["id"]))
                else:
                    db.execute("UPDATE orders SET status='open', updated=? WHERE id=?", (now, order["id"]))
        db.execute("UPDATE positions SET outcome_index=COALESCE(outcome_index, ?) WHERE account_id=? AND token_id=?",
                   (info["index"], acc["id"], spec["token"]))
    return public_order(store.one(db.execute("SELECT * FROM orders WHERE id=?", (order["id"],))))


def cancel(db, uid, oid, reason="Отменена пользователем", status="cancelled"):
    with store.tx(db):
        order = store.one(db.execute("""SELECT o.* FROM orders o JOIN accounts a ON a.id=o.account_id
                                        WHERE o.id=? AND a.user_id=?""", (int(oid), uid)))
        if not order:
            raise LookupError("Заявка не найдена")
        if order["status"] != "open":
            raise ValueError("Заявка уже не активна")
        _release(db, order, reason, status)
    return public_order(store.one(db.execute("SELECT * FROM orders WHERE id=?", (order["id"],))))


def _release(db, order, reason, status):
    if order["side"] == "buy" and order["reserved"] > 0:
        _move(db, order["account_id"], order["reserved"], "release", "Возврат резерва заявки", order["id"])
    db.execute("UPDATE orders SET status=?, reserved=0, reason=?, updated=? WHERE id=?",
               (status, reason, time.time(), order["id"]))


REFILL_AFTER = 20.0          # seconds before a partly filled resting order may fill again


def match_resting(db, tokens_books, now=None):
    """Fill resting limit orders whose price the book has reached. Called by the worker.

    Virtual orders do not take liquidity out of the real book, so the same displayed size could be counted
    again on every check. The first touch fills at once; a partly filled order fills again only after
    REFILL_AFTER seconds, the same pace as the 20-second check of 1.1.1, however often the books are read.
    """
    done = 0
    now = now or time.time()
    for tok, bk in tokens_books.items():
        orders = store.rows(db.execute("SELECT * FROM orders WHERE status='open' AND token_id=? ORDER BY created", (tok,)))
        for order in orders:
            rest = r6(order["shares"] - order["filled_shares"])
            if rest <= 1e-6:
                continue
            if order["filled_shares"] > 1e-6 and now - order["updated"] < REFILL_AFTER:
                continue
            if order["side"] == "buy":
                avail = sum(s for p, s in bk["asks"] if p <= order["limit_price"] + EPS)
            else:
                avail = sum(s for p, s in bk["bids"] if p >= order["limit_price"] - EPS)
            q = r6(min(rest, avail))
            if q <= 1e-6:
                continue
            fee = 0.0 if order["taker_only"] else r6(q * fee_per_share(order["limit_price"], order["fee_rate"], order["fee_exponent"]))
            with store.tx(db):
                fresh = store.one(db.execute("SELECT * FROM orders WHERE id=? AND status='open'", (order["id"],)))
                if not fresh:
                    continue
                if fresh["side"] == "sell" and available_shares(db, fresh["account_id"], tok) + r6(fresh["shares"] - fresh["filled_shares"]) + 1e-6 < q:
                    _release(db, fresh, "Недостаточно акций", "cancelled")
                    continue
                _apply_fills(db, fresh, [(fresh["limit_price"], q, fee)], "maker")
                left = r6(fresh["shares"] - fresh["filled_shares"] - q)
                if left <= 1e-6:
                    db.execute("UPDATE orders SET status='filled', reserved=0, updated=? WHERE id=?", (time.time(), fresh["id"]))
            bus.publish("fill", {"account_id": fresh["account_id"], "order_id": fresh["id"]})
            done += 1
    return done


def expire_orders(db):
    now = time.time()
    for order in store.rows(db.execute("SELECT * FROM orders WHERE status='open' AND expires IS NOT NULL AND expires < ?", (now,))):
        with store.tx(db):
            _release(db, order, "Срок заявки истёк", "expired")


def settle(db, cond, raw):
    """Redeem positions and cancel open orders of a resolved market."""
    c = pm.contract(raw)
    if not c or not raw.get("closed"):
        return 0
    prices = [o["price"] for o in c["outcomes"]]
    if any(p is None for p in prices):
        return 0
    binary = all(abs(p - round(p)) < 1e-6 for p in prices)
    # A market voided as 50-50 pays $0.50 a share; only once Polymarket marks it resolved (closed markets show
    # their last prices until then). Before 1.4.4 such positions stayed open forever.
    fifty = raw.get("resolutionStatus") == "resolved" and all(abs(p - 0.5) < 1e-6 for p in prices)
    if not (binary or fifty):
        return 0  # not final yet (disputed or partially resolved)
    count = 0
    by_token = {o["token"]: o["price"] for o in c["outcomes"]}
    for order in store.rows(db.execute("SELECT * FROM orders WHERE status='open' AND condition_id=?", (cond,))):
        with store.tx(db):
            _release(db, order, "Рынок разрешён", "cancelled")
    for pos in store.rows(db.execute("SELECT * FROM positions WHERE condition_id=? AND status='open'", (cond,))):
        payout = by_token.get(pos["token_id"])
        if payout is None:
            continue
        with store.tx(db):
            amount = r6(pos["shares"] * payout)
            if amount:
                _move(db, pos["account_id"], amount, "redeem", "Погашение: " + pos["title"])
            db.execute("""UPDATE positions SET status='redeemed', payout=?, realized=?, last_price=?, cost=0, shares=0, updated=?
                          WHERE account_id=? AND token_id=?""",
                       (amount, r6(pos["realized"] + amount - pos["cost"]), payout, time.time(), pos["account_id"], pos["token_id"]))
        bus.publish("account", {"account_id": pos["account_id"]})  # a losing position pays nothing but still closes
        count += 1
    return count


# ---------------------------------------------------------------- views

def public_order(o):
    filled = o["filled_shares"]
    return {"id": o["id"], "account": o["account_id"], "token": o["token_id"], "condition": o["condition_id"],
            "event_slug": o["event_slug"], "title": o["title"], "outcome": o["outcome"], "image": o["image"],
            "side": o["side"], "type": o["type"], "amount": o["amount"], "shares": o["shares"], "price": o["limit_price"],
            "filled": filled, "avg": (o["filled_cost"] / filled) if filled else None, "fees": o["fees"],
            "status": o["status"], "reason": o["reason"], "source": o["source"], "expires": o["expires"],
            "created": o["created"], "updated": o["updated"]}


def position_rows(db, aid, status="open"):
    if status == "open":
        sql = "SELECT * FROM positions WHERE account_id=? AND status='open' AND shares > 0.000001 ORDER BY updated DESC"
    else:
        sql = "SELECT * FROM positions WHERE account_id=? AND status IN ('closed','redeemed') ORDER BY updated DESC"
    out = []
    for p in store.rows(db.execute(sql, (aid,))):
        price = p["last_price"]
        avg = p["cost"] / p["shares"] if p["shares"] > EPS else None
        value = r6(p["shares"] * price) if price is not None else None
        out.append({"token": p["token_id"], "condition": p["condition_id"], "event_slug": p["event_slug"], "title": p["title"],
                    "outcome": p["outcome"], "image": p["image"], "shares": p["shares"], "avg": avg, "price": price,
                    "cost": p["cost"], "value": value, "to_win": p["shares"],
                    "pnl": r6(value - p["cost"]) if value is not None else None,
                    "realized": p["realized"], "status": p["status"], "payout": p["payout"], "updated": p["updated"]})
    return out


def equity(db, aid):
    acc = db.execute("SELECT balance FROM accounts WHERE id=?", (aid,)).fetchone()[0]
    reserved = db.execute("SELECT COALESCE(SUM(reserved),0) FROM orders WHERE account_id=? AND status='open'", (aid,)).fetchone()[0]
    value = db.execute("""SELECT COALESCE(SUM(shares * COALESCE(last_price, cost / NULLIF(shares,0), 0)),0) FROM positions
                          WHERE account_id=? AND status='open'""", (aid,)).fetchone()[0]
    cost = db.execute("SELECT COALESCE(SUM(cost),0) FROM positions WHERE account_id=? AND status='open'", (aid,)).fetchone()[0]
    return {"cash": r6(acc), "reserved": r6(reserved), "positions": r6(value), "invested": r6(cost),
            "unrealized": r6(value - cost), "total": r6(acc + reserved + value)}


def summary(db, uid, aid):
    acc = owned_account(db, uid, aid)
    eq = equity(db, aid)
    day_ago = time.time() - 86400
    past = db.execute("SELECT equity FROM equity_history WHERE account_id=? AND at <= ? ORDER BY at DESC LIMIT 1",
                      (aid, day_ago)).fetchone()
    first = db.execute("SELECT equity FROM equity_history WHERE account_id=? ORDER BY at LIMIT 1", (aid,)).fetchone()
    base = past[0] if past else (first[0] if first else eq["total"])
    flows = db.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger WHERE account_id=? AND at > ? AND
                          kind IN ('deposit','withdraw','transfer_in','transfer_out')""", (aid, day_ago if past else 0)).fetchone()[0]
    net_in = db.execute("""SELECT COALESCE(SUM(amount),0) FROM ledger WHERE account_id=? AND
                           kind IN ('deposit','withdraw','transfer_in','transfer_out')""", (aid,)).fetchone()[0]
    day_change = r6(eq["total"] - base - (flows if past else 0))
    return {"id": acc["id"], "kind": acc["kind"], "name": acc["name"], "paused": bool(acc["paused"]), **eq,
            "day_change": day_change if past else r6(eq["total"] - net_in),
            "day_change_pct": (day_change / base * 100) if past and base else None,
            "profit": r6(eq["total"] - net_in)}


def history(db, uid, aid, window="1w"):
    owned_account(db, uid, aid)
    span = {"1d": 86400, "1w": 7 * 86400, "1m": 31 * 86400, "ytd": None, "all": None}.get(window, 7 * 86400)
    if window == "ytd":
        lt = time.gmtime()
        since = time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, 0))
    else:
        since = time.time() - span if span else 0
    points = [[r[0], r[1]] for r in db.execute("SELECT at, equity FROM equity_history WHERE account_id=? AND at >= ? ORDER BY at",
                                               (aid, since))]
    points.append([time.time(), equity(db, aid)["total"]])
    return points


def orders(db, uid, aid, status="open"):
    owned_account(db, uid, aid)
    if status == "open":
        rows = db.execute("SELECT * FROM orders WHERE account_id=? AND status='open' ORDER BY created DESC", (aid,))
    else:
        rows = db.execute("SELECT * FROM orders WHERE account_id=? AND status!='open' AND status!='pending' ORDER BY created DESC LIMIT 500", (aid,))
    return [public_order(dict(r)) for r in rows]


def activity(db, uid, aid, limit=200):
    owned_account(db, uid, aid)
    return store.rows(db.execute("SELECT kind, amount, balance_after, note, ref, at FROM ledger WHERE account_id=? ORDER BY id DESC LIMIT ?",
                                 (aid, limit)))


def snapshot_equity(db):
    now = time.time()
    for (aid,) in db.execute("SELECT id FROM accounts").fetchall():
        last = db.execute("SELECT MAX(at) FROM equity_history WHERE account_id=?", (aid,)).fetchone()[0]
        if last and now - last < 3600:
            continue
        db.execute("INSERT OR REPLACE INTO equity_history VALUES(?,?,?)", (aid, now, equity(db, aid)["total"]))


def mark_prices(db, prices_by_token):
    """Store new mid prices; returns the accounts whose positions changed in value (their pages refresh live)."""
    now = time.time()
    moved = []
    for tok, price in prices_by_token.items():
        if price is None:
            continue
        cur = db.execute("""UPDATE positions SET last_price=?, price_at=? WHERE token_id=? AND status='open'
                            AND (last_price IS NULL OR abs(last_price - ?) > 1e-9)""", (price, now, tok, price))
        if cur.rowcount:
            moved.append(tok)
        db.execute("UPDATE positions SET price_at=? WHERE token_id=? AND status='open'", (now, tok))
    accounts = sorted({r[0] for i in range(0, len(moved), 500) for r in db.execute(
        f"SELECT DISTINCT account_id FROM positions WHERE status='open' AND token_id IN ({','.join('?' * len(moved[i:i + 500]))})",
        moved[i:i + 500])})
    if accounts:
        bus.publish("prices", {"accounts": accounts})
    return accounts
