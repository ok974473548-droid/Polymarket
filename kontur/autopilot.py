"""Autopilot: keeps a roster of auto-selected players, copies them from one shared budget and
replaces those who fall behind with the best player who passed probation on the bench.

States of a player for a user: bench (probation without money) → active (copied with money)
→ retired (cooldown) or banned. The rules are documented in AUTOPILOT.md.
"""

import json
import time
from datetime import datetime, timezone

from . import bus
from . import copytrade
from . import db as store
from . import notify
from . import packs
from . import polymarket as pm
from . import scout
from . import trading

DAY = 86400
REVIEW_EVERY = 600
EPS = 1e-6
MANUAL_REMOVE_DAYS = 7      # at least this long a player taken off the bench by hand stays off

SETTINGS = {
    # roster and money
    "active_count": 8, "bench_size": 4, "copy_pct": 10, "event_cap_pct": 10,
    # selection (same keys as scout.DEFAULTS)
    **scout.DEFAULTS,
    # what is copied and how
    "price_min": 0.02, "min_leader_trade": 5, "max_days_to_end": 60, "copy_slip": 0.03,
    # "limit": a limit order at the player's price that waits a few minutes for the price to come back;
    # "market": buy at once within the allowed price move (until 1.10.0 the only way)
    "copy_mode": "limit", "limit_wait_min": 2, "copy_slip_pct": 3,
    # probation
    "probation_days": 14, "probation_min_bets": 5, "allow_early": True, "early_days": 7, "bootstrap": True,
    # replacement
    "loss_streak": 5, "recent_min_wr": 55, "max_drawdown_pct": 30, "inactive_days": 14,
    "rotation": True, "rotation_margin": 15, "min_tenure_days": 21, "max_swaps_per_day": 3, "cooldown_days": 30,
}
LIMITS = {
    "active_count": (int, 3, 20), "bench_size": (int, 0, 12), "copy_pct": (float, 2, 50), "event_cap_pct": (float, 2, 50),
    "min_bets": (int, 15, 300), "min_wr": (float, 50, 95), "min_lb": (float, 40, 90), "min_roi": (float, -10, 50),
    "min_edge": (float, 0, 0.2), "max_top_share": (float, 10, 100), "min_recent": (int, 0, 30), "max_slip": (float, 0.005, 0.1),
    "price_min": (float, 0.01, 0.5), "min_leader_trade": (float, 1, 1000),
    "min_payout": (float, 1.1, 10), "min_copyable": (float, 0, 100),
    "max_days_to_end": (int, 0, 730), "copy_slip": (float, 0.005, 0.2),
    "copy_mode": (str, ("limit", "market")), "limit_wait_min": (int, 1, 30), "copy_slip_pct": (float, 0.5, 50),
    "selection": (str, tuple(scout.SELECTIONS)), "min_skill_edge": (float, 0, 30),
    "probation_days": (int, 3, 60), "probation_min_bets": (int, 1, 50), "allow_early": (bool,), "early_days": (int, 1, 60),
    "bootstrap": (bool,), "loss_streak": (int, 3, 20), "recent_min_wr": (float, 20, 90), "max_drawdown_pct": (float, 5, 90),
    "inactive_days": (int, 3, 90), "rotation": (bool,), "rotation_margin": (float, 5, 50), "min_tenure_days": (int, 3, 120),
    "max_swaps_per_day": (int, 1, 10), "cooldown_days": (int, 0, 365),
}
NAMES = {  # as in the settings dialog, for error messages
    "active_count": "Игроков в составе",
    "bench_size": "Мест на скамейке",
    "copy_pct": "Копия ставки, % доли игрока",
    "event_cap_pct": "Лимит на одно событие, % бюджета",
    "min_wr": "Доля выигрышей копий, от",
    "min_lb": "Надёжная граница (Уилсон), от",
    "min_bets": "Решённых ставок за 180 дней, от",
    "min_roi": "Доходность на ставку, от",
    "min_edge": "Преимущество над ценой, от",
    "max_top_share": "Доля прибыли от лучшей ставки, до",
    "min_recent": "Новых ставок за 14 дней, от",
    "max_slip": "Проскальзывание через 60 с, до",
    "price_min": "Цена ставки, от",
    "min_payout": "Выигрыш при успехе, не меньше",
    "min_copyable": "Ставок с таким выигрышем у игрока, от",
    "min_leader_trade": "Сделка игрока, от",
    "max_days_to_end": "До разрешения рынка, дней",
    "copy_slip": "Цена копии хуже цены игрока, не больше",
    "copy_mode": "Как покупать копию",
    "limit_wait_min": "Ждать цену игрока, минут",
    "copy_slip_pct": "Цена копии хуже цены игрока, % цены, не больше",
    "selection": "Как отбирать игроков",
    "min_skill_edge": "Доходность сверх цены, от",
    "probation_days": "Срок испытания, дней",
    "probation_min_bets": "Новых ставок за испытание, от",
    "early_days": "Досрочно — после дней на скамейке",
    "loss_streak": "Проигрышей подряд",
    "recent_min_wr": "Доля выигрышей последних 20 копий, ниже",
    "max_drawdown_pct": "Убыток копий игрока, % его доли",
    "inactive_days": "Нет новых ставок, дней",
    "rotation_margin": "Ротация, если сильнее на баллов",
    "min_tenure_days": "Ротация не раньше, дней в составе",
    "max_swaps_per_day": "Замен в сутки, не больше",
    "cooldown_days": "Выбывший может вернуться через, дней",
}
LOG_KINDS = {"enable", "disable", "bench", "promote", "bootstrap", "pass", "fail", "replace", "rotate", "retire", "ban",
             "unban", "pin", "unpin", "settings", "release", "done", "manual"}


# ---------------------------------------------------------------- settings and account

def settings(db, uid):
    raw = json.loads(store.kv_get(db, f"ap:{uid}:settings", "{}") or "{}")
    out = {**SETTINGS, **{k: v for k, v in raw.items() if k in SETTINGS}}
    out["enabled"] = bool(raw.get("enabled"))
    return out


def _save_settings(db, uid, s):
    store.kv_set(db, f"ap:{uid}:settings", json.dumps(s))


def update_settings(db, uid, body):
    s = settings(db, uid)
    changed = []
    for key, value in (body or {}).items():
        if key not in LIMITS:
            continue
        spec = LIMITS[key]
        if spec[0] is bool:
            v = bool(value)
        elif spec[0] is str:
            v = str(value)
            if v not in spec[1]:
                raise ValueError(f"«{NAMES.get(key, key)}»: неизвестный вариант")
        else:
            v = pm.num(value)
            if v is None or not spec[1] <= v <= spec[2]:
                lo, hi = (f"{x:g}".replace(".", ",") for x in spec[1:3])
                raise ValueError(f"«{NAMES.get(key, key)}»: допустимо от {lo} до {hi}")
            v = int(round(v)) if spec[0] is int else round(float(v), 4)
        if s.get(key) != v:
            s[key] = v
            changed.append(key)
    if s["price_min"] >= scout.payout_price(s["min_payout"]):
        raise ValueError(f"При выигрыше ×{s['min_payout']:g} покупка не дороже ${scout.payout_price(s['min_payout']):.2f}: "
                         "нижняя граница цены должна быть меньше")
    if body and body.get("reset"):
        s = {**SETTINGS, "enabled": s["enabled"]}
        changed = ["reset"]
    _save_settings(db, uid, s)
    if changed:
        log(db, uid, "settings", "", "Настройки изменены" if changed != ["reset"] else "Настройки сброшены по умолчанию")
        store.kv_set(db, f"ap:{uid}:review_now", "1")
    return s


def account_id(db, uid):
    v = store.kv_get(db, f"ap:{uid}:account", "")
    if not v:
        return None
    row = db.execute("SELECT id FROM accounts WHERE id=? AND user_id=?", (int(v), uid)).fetchone()
    return row[0] if row else None


def ensure_account(db, uid):
    aid = account_id(db, uid)
    if aid:
        return aid
    with store.tx(db):
        cur = db.execute("INSERT INTO accounts(user_id, kind, name, balance, paused, created) VALUES(?,?,?,0,1,?)",
                         (uid, "copy", "Автопилот", time.time()))
    store.kv_set(db, f"ap:{uid}:account", str(cur.lastrowid))
    return cur.lastrowid


def enable(db, uid, on, budget=None):
    aid = ensure_account(db, uid)
    store.kv_set(db, "scout:wanted", "1")
    if budget:
        trading.transfer_from_main(db, uid, aid, budget)
    s = settings(db, uid)
    s["enabled"] = bool(on)
    _save_settings(db, uid, s)
    db.execute("UPDATE accounts SET paused=? WHERE id=?", (0 if on else 1, aid))
    log(db, uid, "enable" if on else "disable", "", "Автопилот включён" if on else "Автопилот выключен: новые копии не делаются")
    store.kv_set(db, f"ap:{uid}:review_now", "1")
    return overview(db, uid)


def log(db, uid, kind, wallet, text):
    db.execute("INSERT INTO ap_log(user_id, at, kind, wallet, text) VALUES(?,?,?,?,?)", (uid, time.time(), kind, wallet or "", text))
    bus.publish("autopilot", {"user_id": uid})  # the roster changed: the open autopilot page refreshes


def _who(db, wallet):
    row = db.execute("SELECT name FROM scout_wallets WHERE wallet=?", (wallet,)).fetchone()
    return (row[0] if row and row[0] else wallet[:6] + "…" + wallet[-4:])


# ---------------------------------------------------------------- copying (called by copytrade.poll)

def _equity(db, aid):
    return trading.equity(db, aid)


def price_allowance(price, s):
    """How much more than the player a copy may pay: at most copy_slip dollars and at most copy_slip_pct of the
    price (but a cent is always allowed). Until 1.10.0 only the dollar limit applied: with $0.20 allowed, a copy
    of a 15¢ buy was filled at 31¢."""
    return min(s["copy_slip"], max(0.01, price * s["copy_slip_pct"] / 100))


def _open_cost(db, uid, *, wallet=None, event_slug=None):
    """Money in a player's (or an event's) open copies, plus what their waiting limit orders hold."""
    sql = "SELECT COALESCE(SUM(CASE WHEN bought>0 THEN cost*shares/bought ELSE 0 END),0) FROM ap_bets WHERE user_id=? AND status='open'"
    params = [uid]
    if wallet:
        sql += " AND wallet=?"
        params.append(wallet)
    if event_slug:
        sql += " AND event_slug=?"
        params.append(event_slug)
    held = db.execute(sql, params).fetchone()[0]
    aid = account_id(db, uid)
    if not aid:
        return held
    sql = """SELECT COALESCE(SUM(o.reserved),0) FROM orders o JOIN copy_trades c ON c.id=o.copy_trade_id
             WHERE o.account_id=? AND o.status='open' AND o.side='buy' AND c.autopilot=1"""
    params = [aid]
    if wallet:
        sql += " AND c.leader=?"
        params.append(wallet)
    if event_slug:
        sql += " AND o.event_slug=?"
        params.append(event_slug)
    return held + db.execute(sql, params).fetchone()[0]


def copy_buy(db, uid, ct, trade, info):
    """Size and place an autopilot copy of a leader's buy. Returns (status, reason, extra) like copytrade."""
    if ct.get("mode") != "normal":
        return "skipped", "Игрок выведен из состава: повторяются только его продажи", {}
    s = settings(db, uid)
    price, size = trade.get("price"), trade.get("size")
    if price is None or size is None:
        return "skipped", "Сделка игрока без цены", {}
    if price * size < s["min_leader_trade"]:
        return "skipped", f"Сделка игрока меньше ${s['min_leader_trade']:g}", {}
    if price < s["price_min"] - EPS:
        return "skipped", f"Цена ${price:.2f} ниже ${s['price_min']:.2f}", {}
    # a win must pay back at least min_payout times the copy (1.12.0: until then any price up to $0.97 was bought,
    # and a $6 copy at $0.92 won $6.50)
    top = scout.payout_price(s["min_payout"])
    if price > top + EPS:
        return "skipped", (f"Малый выигрыш: по цене ${price:.2f} ставка вернёт ×{scout.payout(price):.2f}, "
                           f"нужно от ×{s['min_payout']:g}").replace(".", ","), {}
    if scout.SHORT_TERM.search(str(trade.get("title") or "")):
        return "skipped", "Краткосрочный крипторынок: задержка копии слишком велика", {}
    end = scout._date_ts(info.get("end"))
    if s["max_days_to_end"] and end and end - time.time() > s["max_days_to_end"] * DAY:
        return "skipped", f"До разрешения {int((end - time.time()) / DAY)} дн., лимит {s['max_days_to_end']}", {}
    aid = ct["account_id"]
    eq = _equity(db, aid)
    share = eq["total"] / s["active_count"]
    amount = share * s["copy_pct"] / 100
    room = share - _open_cost(db, uid, wallet=ct["leader"])
    if room < copytrade.MIN_COPY:
        return "skipped", "Доля игрока занята открытыми ставками", {}
    amount = min(amount, room)
    event_room = eq["total"] * s["event_cap_pct"] / 100 - _open_cost(db, uid, event_slug=info.get("event_slug") or "")
    if event_room < copytrade.MIN_COPY:
        return "skipped", "Достигнут лимит автопилота на одно событие", {}
    amount = min(amount, event_room)
    cash = db.execute("SELECT balance FROM accounts WHERE id=?", (aid,)).fetchone()[0]
    amount = round(min(amount, cash), 2)
    if amount < copytrade.MIN_COPY:
        return "skipped", "Недостаточно свободных денег в автопилоте" if cash < copytrade.MIN_COPY else "Размер копии меньше $1", {}
    if s["copy_mode"] == "limit":
        return _limit_buy(db, uid, ct, trade, info, s, amount, cash)
    allow = price_allowance(price, s)
    cap = min(0.999, price + allow, top)        # not above the price that still pays min_payout
    try:
        order = trading.place(db, uid, {"side": "buy", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "amount": amount, "max_price": cap},
                              account_id=aid, source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = f"Цена ушла больше чем на ${allow:.3f}".rstrip("0").rstrip(".") if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"amount": amount}
    spent = order["filled"] * (order["avg"] or 0) + order["fees"]
    _record_buy(db, uid, aid, ct, info, trade, order["filled"], spent)
    return "filled", "", {"amount": round(spent, 6), "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


def _limit_buy(db, uid, ct, trade, info, s, amount, cash):
    """A limit order at the player's own price, waiting limit_wait_min minutes for the price to come back.

    Right after a player's buy the cheap asks are gone; they often come back within a minute or two. Research on
    1 512 wallets (research/limit_entry.py): copies bought only when the price returned within a minute earned
    +6.3% per copy (95% interval +2.9…+10.1%) against +1.5% for buying at once, with 42% of buys filled. What is
    not filled in time is cancelled; what fills later is recorded by _on_order."""
    price = trade["price"]
    shares = round(int(amount / price * 100) / 100, 2)
    if shares < info["min_size"]:
        need = info["min_size"] * price
        if need > min(amount * 2, cash):
            return "skipped", f"Копия меньше минимальной заявки рынка ({info['min_size']:g} акций)", {"amount": amount}
        shares = info["min_size"]
    wait = s["limit_wait_min"]
    try:
        order = trading.place(db, uid, {"side": "buy", "type": "limit", "condition": info["condition"], "token": info["token"],
                                        "shares": shares, "price": price, "expires": time.time() + wait * 60},
                              account_id=ct["account_id"], source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        return "skipped", str(exc), {"amount": amount}
    filled = order["filled"] or 0.0
    spent = filled * (order["avg"] or 0) + (order["fees"] or 0)
    if filled > EPS:
        _record_buy(db, uid, ct["account_id"], ct, info, trade, filled, spent)
    extra = {"amount": round(spent, 6) if filled > EPS else None, "shares": filled or None, "price": order["avg"],
             "order_id": order["id"]}
    if order["status"] == "filled":
        return "filled", "", extra
    return "waiting", f"Ждёт цену игрока ${price:.3f} до {wait} мин".replace(".", ",", 1), extra


def _record_buy(db, uid, aid, ct, info, trade, shares, spent):
    if shares <= EPS:
        return
    row = db.execute("SELECT id FROM ap_bets WHERE user_id=? AND wallet=? AND token_id=? AND status='open'",
                     (uid, ct["leader"], info["token"])).fetchone()
    if row:
        db.execute("UPDATE ap_bets SET shares=shares+?, bought=bought+?, cost=cost+? WHERE id=?", (shares, shares, spent, row[0]))
    else:
        db.execute("""INSERT INTO ap_bets(user_id, account_id, wallet, copy_trade_id, token_id, condition_id, title, outcome, event_slug,
                      image, opened, shares, bought, cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (uid, aid, ct["leader"], ct["id"], info["token"], info["condition"],
                    info.get("question") or trade.get("title") or "", info.get("outcome") or trade.get("outcome") or "",
                    info.get("event_slug") or trade.get("event_slug") or "", info.get("event_image") or trade.get("icon") or "",
                    time.time(), shares, shares, spent))


def _on_order(db, order, event, shares, cost):
    """A waiting limit copy filled later, or ended (expired, cancelled): keep the copy and its journal row right."""
    if not order or order["source"] != "copy" or order["side"] != "buy" or not order["copy_trade_id"]:
        return
    ct = store.one(db.execute("SELECT * FROM copy_trades WHERE id=? AND autopilot=1", (order["copy_trade_id"],)))
    if not ct:
        return
    uid = db.execute("SELECT user_id FROM accounts WHERE id=?", (ct["account_id"],)).fetchone()[0]
    if event == "fill" and shares > EPS:
        info = {"token": order["token_id"], "condition": order["condition_id"], "question": order["title"],
                "outcome": order["outcome"], "event_slug": order["event_slug"], "event_image": order["image"]}
        _record_buy(db, uid, ct["account_id"], ct, info, {}, shares, cost)
    filled = order["filled_shares"] or 0.0
    done = order["status"] != "open"
    if filled > EPS:
        status, reason = ("filled", "") if done else ("waiting", "Исполнена часть, остальное ждёт цену игрока")
        if done and order["status"] != "filled":
            reason = f"Исполнено {filled:g} из {order['shares']:g} акций: остальное не дождалось цены игрока"
    elif done:
        status = "skipped"
        reason = ("Игрок продал раньше, чем цена вернулась" if "продал" in (order["reason"] or "")
                  else "Цена не вернулась к цене игрока за отведённое время")
    else:
        return
    db.execute("""UPDATE copy_events SET status=?, reason=?, shares=?, amount=?, price=? WHERE order_id=?""",
               (status, reason, filled or None, round(order["filled_cost"] + order["fees"], 6) if filled > EPS else None,
                order["filled_cost"] / filled if filled > EPS else None, order["id"]))
    bus.publish("copy", {"account_id": ct["account_id"], "status": status})


trading.ORDER_HOOKS.append(_on_order)


def _close_part(db, bet, shares, proceeds):
    left = max(0.0, bet["shares"] - shares)
    if left <= EPS:
        total = bet["proceeds"] + proceeds
        db.execute("UPDATE ap_bets SET shares=0, proceeds=?, status='closed', closed=?, pnl=? WHERE id=?",
                   (total, time.time(), total - bet["cost"], bet["id"]))
    else:
        db.execute("UPDATE ap_bets SET shares=?, proceeds=proceeds+? WHERE id=?", (left, proceeds, bet["id"]))


def copy_sell(db, uid, ct, trade, info):
    """Mirror a leader's sale within the shares this player's copies hold."""
    # a copy still waiting for the player's price is not bought after the player has left
    withdrawn = trading.cancel_copy_orders(db, ct["account_id"], info["token"], ct["id"], "Игрок продал до исполнения")
    bet = store.one(db.execute("SELECT * FROM ap_bets WHERE user_id=? AND wallet=? AND token_id=? AND status='open'",
                               (uid, ct["leader"], info["token"])))
    if not bet:
        return "skipped", ("Покупка ещё ждала цену игрока — снята" if withdrawn else "У автопилота нет копии этой позиции"), {}
    # Until 1.4.4 the Data API position was taken as post-sale even when it was not updated yet: a full exit of the
    # player sold only about half of the copy.
    fraction = copytrade.sell_fraction(ct["leader"], info, trade)
    have = trading.available_shares(db, ct["account_id"], info["token"])
    shares = round(min(have, bet["shares"] * fraction if fraction < 0.999 else bet["shares"]), 6)
    if shares <= EPS:
        return "skipped", "Слишком маленький объём продажи", {}
    s = settings(db, uid)
    floor = max(0.001, (trade.get("price") or 0) - s["copy_slip"])
    try:
        order = trading.place(db, uid, {"side": "sell", "type": "market", "condition": info["condition"], "token": info["token"],
                                        "shares": shares, "max_price": floor},
                              account_id=ct["account_id"], source="copy", copy_trade_id=ct["id"], info=info)
    except ValueError as exc:
        reason = f"Цена ушла больше чем на ${s['copy_slip']:.2f}" if "ликвидности" in str(exc) else str(exc)
        return "skipped", reason, {"shares": shares}
    got = order["filled"] * (order["avg"] or 0) - order["fees"]
    _close_part(db, bet, order["filled"], got)
    return "filled", "", {"amount": round(got, 6), "shares": order["filled"], "price": order["avg"], "order_id": order["id"]}


def sync_bets(db, uid):
    """Close copies of resolved markets and of positions the user sold by hand."""
    aid = account_id(db, uid)
    if not aid:
        return
    open_bets = store.rows(db.execute("SELECT * FROM ap_bets WHERE user_id=? AND status='open'", (uid,)))
    by_token = {}
    for b in open_bets:
        by_token.setdefault(b["token_id"], []).append(b)
    for tok, bets in by_token.items():
        pos = store.one(db.execute("SELECT * FROM positions WHERE account_id=? AND token_id=?", (aid, tok)))
        if not pos:
            continue
        with store.tx(db):
            if pos["status"] == "redeemed":
                per_share = pos["last_price"] or 0.0
                for b in bets:
                    _close_part(db, b, b["shares"], b["shares"] * per_share)
                continue
            held = sum(b["shares"] for b in bets)
            if pos["shares"] < held - EPS:
                ratio = 1 - max(0.0, pos["shares"]) / held
                for b in bets:
                    _close_part(db, b, b["shares"] * ratio, b["shares"] * ratio * (pos["last_price"] or 0.0))


# ---------------------------------------------------------------- player statistics

def _copies(db, uid, wallet, since=None):
    sql = "SELECT * FROM ap_bets WHERE user_id=? AND wallet=?"
    params = [uid, wallet]
    if since:
        sql += " AND opened>=?"
        params.append(since)
    return store.rows(db.execute(sql + " ORDER BY opened", params))


def copy_stats(db, uid, wallet, since=None):
    bets = _copies(db, uid, wallet, since)
    aid = account_id(db, uid)
    closed = sorted([b for b in bets if b["status"] == "closed" and abs(b["pnl"] or 0) > 0.002 * max(b["cost"], 1)],
                    key=lambda b: b["closed"])
    wins = sum(1 for b in closed if b["pnl"] > 0)
    streak = 0
    for b in reversed(closed):
        w = b["pnl"] > 0
        if streak == 0:
            streak = 1 if w else -1
        elif (streak > 0) == w:
            streak += 1 if w else -1
        else:
            break
    last = closed[-20:]
    open_cost = open_value = partial = 0.0
    for b in bets:
        if b["status"] != "open":
            continue
        held = b["shares"] / b["bought"] if b["bought"] else 0
        open_cost += b["cost"] * held
        # Money from a partial sale of a copy that is still open (1.4.2; before, it was left out of the result).
        partial += (b["proceeds"] or 0.0) - b["cost"] * (1 - held) if b["bought"] else 0
        price = db.execute("SELECT last_price FROM positions WHERE account_id=? AND token_id=?", (aid, b["token_id"])).fetchone()
        open_value += b["shares"] * (price[0] if price and price[0] is not None else 0)
    realized = sum(b["pnl"] or 0 for b in bets if b["status"] == "closed") + partial
    return {"closed": len(closed), "wins": wins, "losses": len(closed) - wins, "wr": wins / len(closed) if closed else None,
            "streak": streak, "recent_n": len(last), "recent_wr": (sum(1 for b in last if b["pnl"] > 0) / len(last)) if last else None,
            "open": sum(1 for b in bets if b["status"] == "open"), "open_cost": open_cost, "open_value": open_value,
            "realized": realized, "pnl": realized + open_value - open_cost,
            "last_copy": max((b["opened"] for b in bets), default=None)}


def shadow_stats(db, wallet, since):
    """Probation: the player's new bets since `since`, valued as copies (no money involved)."""
    cand = scout.candidate(db, wallet)
    slip = (cand["metrics"] or {}).get("slip") if cand and cand["metrics"] else scout.DEFAULT_SLIP
    decided, open_r, new = [], [], 0
    for b in scout.stored_bets(db, wallet):
        if b["opened"] < since or b["short"] or not scout.PRICE_MIN <= b["entry"] <= scout.PRICE_MAX:
            continue
        if b["entry"] * b["size"] < scout.MIN_COST:
            continue
        new += 1
        if b["state"] == "open":
            if b["mark"] is not None:
                open_r.append(scout.copy_result(b["entry"], b["mark"], slip or scout.DEFAULT_SLIP, False)[0])
        else:
            x, paid = scout.decided_exit(b)
            r, _ = scout.copy_result(b["entry"], x, slip or scout.DEFAULT_SLIP, paid)
            if abs(r) >= scout.PUSH:
                decided.append(r)
    wins = sum(1 for r in decided if r > 0)
    every = decided + open_r
    return {"new": new, "decided": len(decided), "wins": wins, "wr": wins / len(decided) if decided else None,
            "roi": sum(decided) / len(decided) if decided else None, "open": len(open_r),
            "roi_all": sum(every) / len(every) if every else None}


def _relaxed(s):
    """Thresholds with a margin for players already selected, so they are not dropped on noise.

    Activity: half the requirement (until 1.10.0 none, and players with 4 new bets in two weeks stayed in a roster
    that asked for 20)."""
    return {**s, "min_lb": s["min_lb"] - 5, "min_wr": s["min_wr"] - 7, "min_roi": s["min_roi"] - 2,
            "min_recent": s["min_recent"] // 2, "min_skill_edge": max(0.0, s["min_skill_edge"] - 1),
            "min_copyable": max(0.0, s["min_copyable"] - 10),
            "min_edge": max(0.0, s["min_edge"] - 0.01), "max_slip": s["max_slip"] + 0.01}


def _cut(db, relaxed=False):
    """Significance threshold of skill; a selected player keeps their place up to twice the threshold (at least 2%)."""
    cut = scout.skill_cut(db)
    return max(cut * 2, 0.02) if relaxed else cut


def _verdict(db, wallet, s, relaxed=False):
    cand = scout.candidate(db, wallet)
    if not cand or not cand["metrics"]:
        return None, None
    if not scout.current(cand["metrics"]):
        # counted by an older version and waiting to be recomputed (worker.job_rescore): not judged until then, or
        # a change of the rules (1.10.0: skill against the price) dropped players for a figure not counted yet
        return cand, None
    return cand, scout.judge(cand["metrics"], cand["kind"], _relaxed(s) if relaxed else s, _cut(db, relaxed))


def _probation(db, player, s, now):
    shadow = shadow_stats(db, player["wallet"], player["bench_since"])
    days = (now - player["bench_since"]) / DAY
    cand, verdict = _verdict(db, player["wallet"], s, relaxed=True)
    target = s["min_wr"] / 100
    fail = None
    if verdict is not None and not verdict["passed"] and cand["profiled"] and cand["profiled"] > player["bench_since"]:
        fail = "Перестал проходить отбор: " + ", ".join(verdict["failed"]).lower()
    elif shadow["decided"] >= 5 and shadow["wr"] < target - 0.25:
        fail = f"На испытании выиграно {shadow['wr'] * 100:.0f}% из {shadow['decided']} ставок"
    elif shadow["roi_all"] is not None and shadow["roi_all"] < -0.25:
        fail = f"Результат теневых копий {shadow['roi_all'] * 100:.0f}%"
    elif days >= 2 * s["probation_days"] and shadow["new"] < 2:
        fail = "За время испытания почти нет новых ставок"
    healthy = fail is None and (shadow["roi_all"] is None or shadow["roi_all"] >= -0.05) and \
        (shadow["decided"] < 3 or shadow["wr"] >= target - 0.15)
    ready = healthy and days >= s["probation_days"] and shadow["new"] >= s["probation_min_bets"]
    early = healthy and days >= s["early_days"]
    return {"days": days, "need_days": s["probation_days"], "need_bets": s["probation_min_bets"], "ready": ready,
            "early_ok": early, "fail": fail, "shadow": shadow}


def _active_problem(db, uid, player, s, share, now):
    st = copy_stats(db, uid, player["wallet"], player["active_since"])
    if st["closed"] >= 5:
        if st["streak"] <= -s["loss_streak"]:
            return f"Серия из {-st['streak']} проигрышей подряд", st
        if st["recent_n"] >= 10 and st["recent_wr"] * 100 < s["recent_min_wr"]:
            return f"Выиграно {st['recent_wr'] * 100:.0f}% из последних {st['recent_n']} копий", st
        if share > 0 and st["pnl"] <= -share * s["max_drawdown_pct"] / 100:
            return f"Убыток копий ${-st['pnl']:,.2f} — {(-st['pnl'] / share) * 100:.0f}% доли игрока", st
    cand = scout.candidate(db, player["wallet"])
    last_bet = packs.latest(db, player["wallet"])
    last = max(x for x in (st["last_copy"], last_bet, player["active_since"]) if x)
    if now - last >= s["inactive_days"] * DAY:
        return f"Нет новых ставок {int((now - last) / DAY)} дн.", st
    if cand and cand["metrics"] and cand["profiled"] and cand["profiled"] > player["active_since"]:
        _, verdict = _verdict(db, player["wallet"], s, relaxed=True)
        if verdict and not verdict["passed"]:
            # replaced only when someone can take the place (1.10.0): after a change of the selection rules or with
            # low activity an empty seat is worse than the player; losses, a losing streak and silence stay hard rules
            return "Больше не проходит отбор: " + ", ".join(verdict["failed"]).lower(), {**st, "soft": True}
    return None, st


# ---------------------------------------------------------------- roster moves

def _swaps_today(db, uid):
    return db.execute("SELECT COUNT(*) FROM ap_log WHERE user_id=? AND kind IN ('replace','rotate') AND at>?",
                      (uid, time.time() - DAY)).fetchone()[0]


def _start_copy(db, uid, aid, wallet):
    ts, keys = copytrade._latest_cursor(wallet)
    with store.tx(db):
        cur = db.execute("""INSERT INTO copy_trades(account_id, leader, nickname, sizing, amount, notify, cursor_ts, cursor_keys,
                            created, autopilot, mode) VALUES(?,?,?,?,?,0,?,?,?,1,'normal')""",
                         (aid, wallet, _who(db, wallet), "fixed", 1.1, ts, json.dumps(keys), time.time()))
    return cur.lastrowid


def _stop_copy(db, player):
    if player.get("copy_trade_id"):
        db.execute("UPDATE copy_trades SET mode='exit' WHERE id=? AND mode='normal'", (player["copy_trade_id"],))


def _promote(db, uid, aid, wallet, kind, text, early=False):
    ctid = _start_copy(db, uid, aid, wallet)
    now = time.time()
    db.execute("""INSERT INTO ap_players(user_id, wallet, state, since, active_since, early, copy_trade_id, reason)
                  VALUES(?,?,'active',?,?,?,?, '') ON CONFLICT(user_id, wallet) DO UPDATE SET state='active', active_since=?,
                  early=?, copy_trade_id=?, reason=''""",
               (uid, wallet, now, now, int(early), ctid, now, int(early), ctid))
    log(db, uid, kind, wallet, text)


def _retire(db, uid, player, kind, reason, s, cooldown=True):
    _stop_copy(db, player)
    until = time.time() + (s["cooldown_days"] * DAY if cooldown else 0)
    db.execute("UPDATE ap_players SET state='retired', reason=?, until=?, pinned=0 WHERE user_id=? AND wallet=?",
               (reason, until, uid, player["wallet"]))
    verb = {"replace": "заменён", "rotate": "выведен из состава", "fail": "не прошёл испытание", "retire": "выбыл"}.get(kind, "выбыл")
    log(db, uid, kind, player["wallet"], f"{_who(db, player['wallet'])} {verb}: {reason[:1].lower() + reason[1:]}")


def _players(db, uid, state=None):
    sql, params = "SELECT * FROM ap_players WHERE user_id=?", [uid]
    if state:
        sql += " AND state=?"
        params.append(state)
    return store.rows(db.execute(sql, params))


def _pool(db, uid, s):
    """Passing candidates that are not in this user's roster in any state."""
    taken = {r["wallet"] for r in _players(db, uid)}
    return [c for c in scout.candidates(db, s) if c["passed"] and c["wallet"] not in taken]


def _sync_waiting(db, aid):
    """Journal rows still "waiting" whose order is over (it may have filled before the row was written)."""
    for order in store.rows(db.execute("""SELECT o.* FROM orders o JOIN copy_events e ON e.order_id=o.id
                                          WHERE o.account_id=? AND e.status='waiting' AND o.status!='open'""", (aid,))):
        _on_order(db, order, "sync", 0.0, 0.0)


def _finish_exits(db, uid, aid):
    """Exit-only copies with nothing left to sell are finished."""
    for ct in store.rows(db.execute("SELECT id FROM copy_trades WHERE account_id=? AND autopilot=1 AND mode='exit'", (aid,))):
        if not db.execute("SELECT 1 FROM ap_bets WHERE user_id=? AND copy_trade_id=? AND status='open'", (uid, ct["id"])).fetchone():
            db.execute("UPDATE copy_trades SET mode='done', paused=1 WHERE id=?", (ct["id"],))


def review(db, uid, now=None):
    """One autopilot cycle for a user: probation, problems, replacements, refills."""
    now = now or time.time()
    s = settings(db, uid)
    aid = account_id(db, uid)
    if not aid:
        return
    sync_bets(db, uid)
    for p in _players(db, uid, "retired"):
        if p["until"] and p["until"] < now:
            db.execute("DELETE FROM ap_players WHERE user_id=? AND wallet=?", (uid, p["wallet"]))
    if not s["enabled"]:
        _finish_exits(db, uid, aid)
        store.kv_set(db, f"ap:{uid}:reviewed", str(now))
        return
    eq = _equity(db, aid)
    share = eq["total"] / s["active_count"]
    swaps_left = s["max_swaps_per_day"] - _swaps_today(db, uid)
    vacancies = 0

    # 1. active players that fall behind
    takers = None
    for p in sorted(_players(db, uid, "active"), key=lambda r: r["active_since"] or 0):
        problem, st = _active_problem(db, uid, p, s, share, now)
        db.execute("UPDATE ap_players SET checked=?, reason=? WHERE user_id=? AND wallet=?",
                   (now, problem or "", uid, p["wallet"]))
        if problem and st.get("soft"):
            if takers is None:     # bench players who may enter the roster now
                prs = [_probation(db, b, s, now) for b in _players(db, uid, "bench")]
                takers = sum(1 for pr in prs if pr["ready"] or (s["allow_early"] and pr["early_ok"]))
            if takers <= 0:
                continue           # nobody to take the place: an inactive player is better than an empty seat
            takers -= 1
        if problem and not p["pinned"] and swaps_left > 0:
            _retire(db, uid, p, "replace", problem, s)
            notify.add(db, uid, "autopilot", "Автопилот заменил игрока", f"{_who(db, p['wallet'])}: {problem}", "/autopilot?tab=log")
            swaps_left -= 1
            vacancies += 1

    # 2. bench probation
    probation = {}
    for p in _players(db, uid, "bench"):
        pr = _probation(db, p, s, now)
        probation[p["wallet"]] = pr
        db.execute("UPDATE ap_players SET shadow=?, checked=? WHERE user_id=? AND wallet=?",
                   (json.dumps({k: v for k, v in pr.items() if k != "fail"}), now, uid, p["wallet"]))
        if pr["fail"] and not p["pinned"]:
            _retire(db, uid, p, "fail", pr["fail"], s)
            probation.pop(p["wallet"])
        elif pr["ready"] and not json.loads(p["shadow"] or "{}").get("ready"):
            log(db, uid, "pass", p["wallet"], f"{_who(db, p['wallet'])} прошёл испытание и готов войти в состав")

    def bench_by_score(only_ready):
        out = []
        for p in _players(db, uid, "bench"):
            pr = probation.get(p["wallet"])
            if not pr or not (pr["ready"] or (not only_ready and pr["early_ok"])):
                continue
            cand = scout.candidate(db, p["wallet"])
            out.append((scout.score(cand["metrics"], s["selection"]) if cand and cand["metrics"] else 0, p, pr))
        return sorted(out, key=lambda x: -x[0])

    # 3. fill free places
    active = _players(db, uid, "active")
    if len(active) > s["active_count"]:
        extra = sorted([p for p in active if not p["pinned"]],
                       key=lambda p: scout.score((scout.candidate(db, p["wallet"]) or {}).get("metrics") or {"n": 0},
                                                 s["selection"]))[:len(active) - s["active_count"]]
        for p in extra:
            _retire(db, uid, p, "retire", "Размер состава уменьшен", s, cooldown=False)
        active = _players(db, uid, "active")
    free = s["active_count"] - len(active)
    # Until the roster has been full once, places go straight to players who pass selection:
    # suitable players are rare and are found gradually while the pool is being checked.
    filled_at = store.kv_get(db, f"ap:{uid}:bootstrapped", "")
    first_time = not filled_at or (filled_at.isdigit() and int(filled_at) < s["active_count"])
    if free > 0 and first_time and s["bootstrap"]:
        picks = _pool(db, uid, s)[:free]
        for c in picks:
            _promote(db, uid, aid, c["wallet"], "bootstrap",
                     f"{c['name'] or c['wallet'][:10]} — в стартовом составе по истории (балл {c['score']:.0f})")
        free -= len(picks)
    if free > 0:
        ready = bench_by_score(only_ready=True)
        if len(ready) < free and s["allow_early"]:
            ready += [x for x in bench_by_score(only_ready=False) if x[1]["wallet"] not in {r[1]["wallet"] for r in ready}]
        for sc, p, pr in ready[:free]:
            early = not pr["ready"]
            _promote(db, uid, aid, p["wallet"], "promote",
                     f"{_who(db, p['wallet'])} вошёл в состав" + (" досрочно: готовых на скамейке не было" if early else
                                                                  f" после испытания ({pr['shadow']['new']} ставок за {pr['days']:.0f} дн.)"),
                     early=early)

    # 4. rotation: a clearly stronger ready player replaces the weakest settled one
    if s["rotation"] and swaps_left > 0:
        ready = bench_by_score(only_ready=True)
        settled = []
        for p in _players(db, uid, "active"):
            if p["pinned"] or now - (p["active_since"] or now) < s["min_tenure_days"] * DAY:
                continue
            cand = scout.candidate(db, p["wallet"])
            settled.append((scout.score(cand["metrics"], s["selection"]) if cand and cand["metrics"] else 0, p))
        settled.sort(key=lambda x: x[0])
        if ready and settled and ready[0][0] >= settled[0][0] + s["rotation_margin"]:
            weak_score, weak = settled[0]
            best_score, best, _ = ready[0]
            _retire(db, uid, weak, "rotate", f"Ротация: на скамейке игрок сильнее на {best_score - weak_score:.0f} баллов", s, cooldown=False)
            _promote(db, uid, aid, best["wallet"], "promote", f"{_who(db, best['wallet'])} вошёл в состав вместо {_who(db, weak['wallet'])}")
            notify.add(db, uid, "autopilot", "Автопилот усилил состав",
                       f"{_who(db, best['wallet'])} вместо {_who(db, weak['wallet'])}", "/autopilot?tab=log")

    # 5. keep the bench full
    bench = _players(db, uid, "bench")
    need = s["bench_size"] - len(bench)
    if need > 0:
        for c in _pool(db, uid, s)[:need]:
            db.execute("INSERT INTO ap_players(user_id, wallet, state, since, bench_since) VALUES(?,?,'bench',?,?)",
                       (uid, c["wallet"], now, now))
            log(db, uid, "bench", c["wallet"], f"{c['name'] or c['wallet'][:10]} на скамейке: начато испытание (балл {c['score']:.0f})")
    _finish_exits(db, uid, aid)
    _sync_waiting(db, aid)
    if len(_players(db, uid, "active")) >= s["active_count"]:
        # the size it was full at: a bigger roster later fills its new places the same way (until 1.10.0 a flag,
        # and places added after the first full roster waited a week or two for the bench)
        store.kv_set(db, f"ap:{uid}:bootstrapped", str(s["active_count"]))
    store.kv_set(db, f"ap:{uid}:reviewed", str(now))
    store.kv_set(db, f"ap:{uid}:review_now", "")


# ---------------------------------------------------------------- manual actions

def action(db, uid, wallet, what):
    wallet = pm.address(wallet).lower()
    s = settings(db, uid)
    p = store.one(db.execute("SELECT * FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet)))
    who = _who(db, wallet)
    if what == "bench":
        scout.add_wallet(db, wallet)
        store.kv_set(db, "scout:wanted", "1")
        if p and p["state"] in ("active", "bench"):
            raise ValueError("Игрок уже в автопилоте")
        now = time.time()
        db.execute("""INSERT INTO ap_players(user_id, wallet, state, since, bench_since, manual) VALUES(?,?,'bench',?,?,1)
                      ON CONFLICT(user_id, wallet) DO UPDATE SET state='bench', bench_since=?, manual=1, reason='', until=NULL""",
                   (uid, wallet, now, now, now))
        log(db, uid, "manual", wallet, f"{who} добавлен на скамейку вручную")
    elif not p:
        raise LookupError("Игрока нет в автопилоте")
    elif what in ("pin", "unpin"):
        db.execute("UPDATE ap_players SET pinned=? WHERE user_id=? AND wallet=?", (int(what == "pin"), uid, wallet))
        log(db, uid, what, wallet, f"{who} {'закреплён: автопилот не будет его менять' if what == 'pin' else 'откреплён'}")
    elif what == "replace":
        if p["state"] != "active":
            raise ValueError("Заменить можно только игрока из состава")
        _retire(db, uid, p, "retire", "Заменён вручную", s)
    elif what == "promote":
        if p["state"] != "bench":
            raise ValueError("В состав переводится игрок со скамейки")
        active = len(_players(db, uid, "active"))
        if active >= s["active_count"]:
            raise ValueError("Состав заполнен: сначала замените кого-то или увеличьте размер состава")
        aid = ensure_account(db, uid)
        _promote(db, uid, aid, wallet, "promote", f"{who} переведён в состав вручную", early=True)
    elif what == "ban":
        if p["state"] == "active":
            _stop_copy(db, p)
        db.execute("UPDATE ap_players SET state='banned', pinned=0, reason='Исключён вручную', until=NULL WHERE user_id=? AND wallet=?",
                   (uid, wallet))
        log(db, uid, "ban", wallet, f"{who} исключён: автопилот больше не будет его выбирать")
    elif what == "remove" and p["state"] == "bench":
        # until 1.10.0 the row was deleted and the next review put the same player back on the bench at once
        days = max(MANUAL_REMOVE_DAYS, s["cooldown_days"])
        db.execute("UPDATE ap_players SET state='retired', pinned=0, reason=?, until=? WHERE user_id=? AND wallet=?",
                   ("Убран со скамейки вручную", time.time() + days * DAY, uid, wallet))
        log(db, uid, "retire", wallet, f"{who} убран со скамейки вручную: автопилот не вернёт его {days} дн.")
    elif what in ("unban", "remove"):
        if p["state"] == "active":
            raise ValueError("Игрок в составе: сначала замените его")
        db.execute("DELETE FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet))
        log(db, uid, "unban" if what == "unban" else "retire", wallet,
            f"{who} {'снова может быть выбран' if what == 'unban' else 'снова может быть выбран автопилотом'}")
    else:
        raise ValueError("Неизвестное действие")
    store.kv_set(db, f"ap:{uid}:review_now", "1")
    return player(db, uid, wallet)


# ---------------------------------------------------------------- views

def _card(db, uid, p, s, share, now):
    cand = scout.candidate(db, p["wallet"])
    m = (cand or {}).get("metrics") or {}
    verdict = scout.judge(m, cand["kind"], s, _cut(db)) if scout.current(m) else None
    card = {"wallet": p["wallet"], "name": (cand or {}).get("name") or "", "image": (cand or {}).get("image") or "",
            "state": p["state"], "pinned": bool(p["pinned"]), "manual": bool(p["manual"]), "early": bool(p["early"]),
            "since": p["since"], "bench_since": p["bench_since"], "active_since": p["active_since"], "reason": p["reason"],
            "until": p["until"], "kind": (cand or {}).get("kind"), "kind_label": scout.TYPES.get((cand or {}).get("kind"), "Считается"),
            "profiled": (cand or {}).get("profiled"),
            "profile": {**{k: m.get(k) for k in ("n", "wr", "lb", "roi", "edge", "avg_entry", "new_14d", "slip_measured",
                                                  "recent_wr", "slip_live", "slip_live_n")},
                        "skill": m.get("skill")} if m else None,
            "score": verdict["score"] if verdict else None, "passed": verdict["passed"] if verdict else None,
            "failed": verdict["failed"] if verdict else [], "recounting": bool(m) and not scout.current(m)}
    if p["state"] in ("active", "retired"):
        st = copy_stats(db, uid, p["wallet"], p["active_since"])
        card["copies"] = st
        card["share"] = share
        card["used"] = st["open_cost"]
        warn = []
        if st["streak"] <= -2:
            warn.append(f"{-st['streak']} проигрыша подряд")
        if p["reason"] and p["state"] == "active":
            warn.append(p["reason"] + (" — игрок закреплён" if p["pinned"] else ""))
        card["warnings"] = warn
    if p["state"] == "bench":
        card["probation"] = json.loads(p["shadow"]) if p["shadow"] else None
    return card


def overview(db, uid):
    s = settings(db, uid)
    aid = account_id(db, uid)
    now = time.time()
    acc = trading.summary(db, uid, aid) if aid else None
    share = (acc["total"] / s["active_count"]) if acc else 0
    players = _players(db, uid)
    cards = {k: [] for k in ("active", "bench", "retired", "banned")}
    for p in players:
        cards[p["state"]].append(_card(db, uid, p, s, share, now))
    cards["active"].sort(key=lambda c: -(c["score"] or 0))
    cards["bench"].sort(key=lambda c: (not (c.get("probation") or {}).get("ready"), -(c["score"] or 0)))
    cards["retired"].sort(key=lambda c: -(c["until"] or 0))
    bets = store.rows(db.execute("SELECT status, pnl, cost FROM ap_bets WHERE user_id=?", (uid,)))
    closed = [b for b in bets if b["status"] == "closed" and abs(b["pnl"] or 0) > 0.002 * max(b["cost"], 1)]
    wins = sum(1 for b in closed if b["pnl"] > 0)
    expected = [c["profile"]["wr"] for c in cards["active"] if c.get("profile") and c["profile"].get("wr") is not None]
    returns = [expected_return(c["profile"]) for c in cards["active"] if c.get("profile")]
    returns = [r for r in returns if r is not None]
    return {"settings": s, "defaults": SETTINGS, "enabled": s["enabled"], "account": acc, "share": share,
            "selections": scout.SELECTIONS, "payout_price": scout.payout_price(s["min_payout"]),
            "stats": {"closed": len(closed), "wins": wins, "wr": wins / len(closed) if closed else None,
                      "realized": sum(b["pnl"] or 0 for b in closed),
                      "open": sum(1 for b in bets if b["status"] == "open"),
                      "waiting": db.execute("SELECT COUNT(*) FROM orders WHERE account_id=? AND status='open' AND side='buy'",
                                            (aid,)).fetchone()[0] if aid else 0,
                      "expected_wr": sum(expected) / len(expected) if expected else None,
                      "expected_roi": sum(returns) / len(returns) if returns else None},
            "players": cards, "swaps_today": _swaps_today(db, uid),
            "reviewed": float(store.kv_get(db, f"ap:{uid}:reviewed", "0") or 0) or None, "scout": scout.status(db)}


def expected_return(profile):
    """What a copy of this player is expected to earn per $1, after the price paid, slippage and fees: the skill
    edge, shrunk towards zero by how little history there is. Without enough history for that: the average copy
    return, shrunk the same way. Until 1.10.0 the page showed only the historical win rate (79% on 24.11.2026),
    which mostly reflects how expensive the bought outcomes were."""
    sk = profile.get("skill")
    if sk:
        return sk["edge"]
    if profile.get("roi") is None or not profile.get("n"):
        return None
    return profile["roi"] * profile["n"] / (profile["n"] + scout.K_SHRINK)


def player(db, uid, wallet):
    wallet = pm.address(wallet).lower()
    s = settings(db, uid)
    cand = scout.candidate(db, wallet)
    p = store.one(db.execute("SELECT * FROM ap_players WHERE user_id=? AND wallet=?", (uid, wallet)))
    aid = account_id(db, uid)
    share = (trading.equity(db, aid)["total"] / s["active_count"]) if aid else 0
    out = {"wallet": wallet, "in_roster": bool(p), "card": _card(db, uid, p, s, share, time.time()) if p else None,
           "name": (cand or {}).get("name") or "", "image": (cand or {}).get("image") or "", "profiled": (cand or {}).get("profiled"),
           "kind": (cand or {}).get("kind"), "kind_label": scout.TYPES.get((cand or {}).get("kind"), ""),
           "facts": (cand or {}).get("facts"), "sources": (cand or {}).get("sources") or [], "metrics": None, "verdict": None}
    if cand and cand["metrics"]:
        out["metrics"] = cand["metrics"]
        out["verdict"] = scout.judge(cand["metrics"], cand["kind"], s, _cut(db)) if scout.current(cand["metrics"]) else None
    keys = ("title", "outcome", "event_slug", "entry", "exit", "state", "mark", "opened", "closed", "size")
    recent = [{k: b[k] for k in keys} for b in sorted(scout.stored_bets(db, wallet), key=lambda b: -b["opened"])[:25]]
    for b in recent:
        b["exit"] = scout.settled(b["exit"])  # bets stored before 1.4.4 keep the rounding noise: 0.99990 is a payout of $1
    out["recent_bets"] = recent
    out["copies"] = _copies(db, uid, wallet)[-50:][::-1] if p else []
    return out


def bets(db, uid, status="", wallet=""):
    sql, params = "SELECT b.*, s.name AS player FROM ap_bets b LEFT JOIN scout_wallets s ON s.wallet=b.wallet WHERE b.user_id=?", [uid]
    if status in ("open", "closed"):
        sql += " AND b.status=?"
        params.append(status)
    if wallet:
        sql += " AND b.wallet=?"
        params.append(wallet.lower())
    rows = store.rows(db.execute(sql + " ORDER BY b.opened DESC LIMIT 300", params))
    aid = account_id(db, uid)
    for r in rows:
        pos = db.execute("SELECT last_price, status FROM positions WHERE account_id=? AND token_id=?", (aid, r["token_id"])).fetchone()
        r["price"] = pos[0] if pos else None
        r["entry"] = r["cost"] / r["bought"] if r["bought"] else None
        r["value"] = r["shares"] * r["price"] if r["status"] == "open" and r["price"] is not None else None
        r["resolved"] = r["status"] == "closed" and bool(pos) and pos[1] == "redeemed"
        r["result"] = bet_result(r)
    return rows


def bet_result(r):
    """Profit or loss of one copy so far: what it brought back (sales, payout) plus what is left at the mid price, minus its cost.

    Until 1.4.2 an open copy showed only its unsold part (value - cost of the shares left): money from a partial sale was lost.
    """
    if r["status"] == "closed":
        return r["pnl"]
    if r["value"] is None:
        return None
    return r["value"] + (r["proceeds"] or 0.0) - r["cost"]


def log_rows(db, uid, limit=300):
    rows = store.rows(db.execute("""SELECT l.*, s.name, s.image FROM ap_log l LEFT JOIN scout_wallets s ON s.wallet=l.wallet
                                    WHERE l.user_id=? ORDER BY l.id DESC LIMIT ?""", (uid, limit)))
    return rows


def backtest_result(db, uid):
    raw = store.kv_get(db, f"ap:{uid}:backtest", "")
    return json.loads(raw) if raw else None


def run_backtest(db, uid):
    s = settings(db, uid)
    res = scout.backtest(db, s, s["active_count"])
    store.kv_set(db, f"ap:{uid}:backtest", json.dumps(res))
    store.kv_set(db, f"ap:{uid}:backtest_req", "")
    return res


def users(db):
    """Users who have an autopilot account."""
    blocked = {r[0] for r in db.execute("SELECT id FROM users WHERE blocked=1")}
    return [uid for uid in (int(r[0].split(":")[1]) for r in db.execute("SELECT key FROM kv WHERE key LIKE 'ap:%:account'"))
            if uid not in blocked]  # a blocked person's autopilot waits until they are unblocked


def is_autopilot_account(db, aid):
    return db.execute("SELECT 1 FROM kv WHERE key LIKE 'ap:%:account' AND value=?", (str(aid),)).fetchone() is not None


def day_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp()
