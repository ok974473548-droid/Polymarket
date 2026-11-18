"""Ротация китов по форме: каждую неделю копируем только тех, кто «в форме», и проверяем, держится ли форма.

Данные: кривые прибыли китов — готовые из Polymarket (накопленная прибыль по дням, players.py / user-pnl);
объём и число ставок по неделям — из сохранённых сделок китов.

Правила честности:
  * пересмотр раз в неделю, в момент T_k; «в форме» решается только по данным до T_k включительно;
  * результат выбора — то, что случилось за следующую неделю (T_k, T_k+1];
  * доходность копирования за неделю = прибыль кита за неделю / вложенное, где вложенное ≈ 4 недели его типичных
    покупок (среднее за последние 12 недель): прибыль недели приносят и ставки, сделанные раньше, а крупные киты
    ставят рывками — неделя без покупок не должна давать деление на ноль. Результат недели ограничен ±100%
    вложенного (потерять больше нельзя, большее — артефакт оценки). Портфель — поровну между выбранными китами;
  * неделя кита учитывается, только если у нас есть его сделки за это время (с первой сохранённой сделки) —
    иначе прибыль делилась бы на пропущенный объём;
    никого не выбрали — неделя «в деньгах» (0%);
  * «идеал задним числом» каждую неделю берёт лучших по уже известному результату — это недостижимо,
    кривая показывает лишь потолок;
  * «случайный выбор» — столько же китов, сколько выбрала ротация, 300 случайных розыгрышей (медиана и разброс).

Главная цифра — держится ли форма: ранговая корреляция (Спирмен) результата кита в прошлом окне и в следующем
таком же окне, по всем китам и датам пересмотра; значимость — перестановками внутри каждой даты.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

DAY = 86400
WEEK = 7 * DAY
MIN_DENOM_USD = 100.0  # вложено меньше — неделю кита в доходности не учитываем (нечего повторять)
CAPITAL_WEEKS = 4  # вложенное ≈ столько недель типичных покупок
CAPITAL_LOOKBACK = 12  # типичные покупки — среднее за столько последних недель (крупные киты ставят рывками)
WEEK_CLIP = 1.0  # недельный результат кита ограничен ±100% вложенного


@dataclass(frozen=True)
class FormRule:
    window_weeks: int = 8  # окно, по которому судим о форме
    min_bets: int = 5  # минимум ставок (разных рынков) в окне
    min_result_usd: float = 0.0  # результат за окно не ниже
    max_drawdown_share: float = 0.5  # просадка в окне — не больше этой доли заработанного за окно

    @classmethod
    def from_dict(cls, d: dict | None) -> FormRule:
        d = d or {}
        base = cls()
        return cls(
            window_weeks=int(min(max(int(d.get("window_weeks", base.window_weeks)), 2), 52)),
            min_bets=int(min(max(int(d.get("min_bets", base.min_bets)), 0), 500)),
            min_result_usd=float(d.get("min_result_usd", base.min_result_usd)),
            max_drawdown_share=float(min(max(float(d.get("max_drawdown_share", base.max_drawdown_share)), 0.0), 1.0)),
        )


@dataclass
class Whale:
    entity_id: str
    name: str
    series: list[tuple[int, float]]  # (время, накопленная прибыль $) — по возрастанию времени
    buys: list[tuple[int, float, str]]  # (время, сумма покупки $, рынок)


def merge_series(series_list: list[list[tuple[int, float]]]) -> list[tuple[int, float]]:
    """Сумма кривых нескольких кошельков одного кита (в каждой точке — последние известные значения)."""
    series_list = [sorted(s) for s in series_list if s]
    if not series_list:
        return []
    if len(series_list) == 1:
        return series_list[0]
    ts = sorted({t for s in series_list for t, _ in s})
    idx = [0] * len(series_list)
    last: list[float | None] = [None] * len(series_list)
    out = []
    for t in ts:
        for i, s in enumerate(series_list):
            while idx[i] < len(s) and s[idx[i]][0] <= t:
                last[i] = s[idx[i]][1]
                idx[i] += 1
        out.append((t, float(sum(v for v in last if v is not None))))
    return out


def value_at(series: list[tuple[int, float]], ts_sorted: np.ndarray, t: int) -> float | None:
    """Значение кривой в момент t — последняя точка не позже t (без заглядывания вперёд)."""
    i = int(np.searchsorted(ts_sorted, t, side="right")) - 1
    return None if i < 0 else series[i][1]


def window_drawdown(series: list[tuple[int, float]], ts_sorted: np.ndarray, t0: int, t1: int) -> tuple[float, float]:
    """(пик заработанного, наибольшая просадка) внутри окна (t0, t1] относительно значения в t0."""
    base = value_at(series, ts_sorted, t0)
    if base is None:
        return 0.0, 0.0
    i0 = int(np.searchsorted(ts_sorted, t0, side="right"))
    i1 = int(np.searchsorted(ts_sorted, t1, side="right"))
    peak = dd = 0.0
    for _, v in series[i0:i1]:
        g = v - base
        peak = max(peak, g)
        dd = max(dd, peak - g)
    return peak, dd


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return float("nan")
    rx = np.argsort(np.argsort(x, kind="stable"), kind="stable").astype(float)
    ry = np.argsort(np.argsort(y, kind="stable"), kind="stable").astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return float("nan")
    return float(np.corrcoef(rx, ry)[0, 1])


def persistence(pairs: list[tuple[int, float, float]], perms: int = 1000, seed: int = 11) -> dict:
    """Держится ли форма: связь результата в прошлом окне и в следующем. pairs — (дата, прошлое, будущее).
    p-value — доля перестановок (будущее перемешивается внутри каждой даты) с корреляцией не меньше наблюдаемой."""
    if len(pairs) < 3:
        return {"pairs": len(pairs), "rho": None, "p_value": None}
    dates = np.array([p[0] for p in pairs])
    x = np.array([p[1] for p in pairs], float)
    y = np.array([p[2] for p in pairs], float)
    rho = _spearman(x, y)
    if not math.isfinite(rho):
        return {"pairs": len(pairs), "rho": None, "p_value": None}
    rng = np.random.default_rng(seed)
    groups = [np.flatnonzero(dates == d) for d in np.unique(dates)]
    hits = 0
    for _ in range(perms):
        yp = y.copy()
        for g in groups:
            yp[g] = y[g][rng.permutation(len(g))]
        r = _spearman(x, yp)
        hits += int(math.isfinite(r) and r >= rho - 1e-12)
    # верхняя оценка вероятности получить такую связь случайно; для отрицательной — с другой стороны
    p_up = (1 + hits) / (1 + perms)
    return {"pairs": len(pairs), "rho": round(rho, 3), "p_value": round(p_up, 4)}


def verdict(pers: dict, n_whales: int) -> dict:
    rho, p, n = pers.get("rho"), pers.get("p_value"), pers.get("pairs", 0)
    if rho is None or n < 30 or n_whales < 3:
        return {
            "holds": None,
            "title": "Мало данных, чтобы судить",
            "text": f"Для вывода нужно хотя бы 30 пар «прошлое окно → следующее» по 3+ китам, сейчас {n}. "
            "Добавьте китов с долгой историей или уменьшите окно.",
        }
    if rho > 0 and p is not None and p < 0.05:
        return {
            "holds": True,
            "title": "Форма держится",
            "text": f"Киты, которые были хороши в прошлом окне, чаще хороши и в следующем (связь {rho:+.2f}, "
            f"вероятность случайности {p:.1%}). Ротация по форме имеет смысл.",
        }
    if rho < 0 and p is not None and p > 0.99:  # строже: этот вывод прямо советует не копировать
        return {
            "holds": False,
            "title": "Наоборот: после удачи — неудача",
            "text": f"У этих китов за хорошим окном чаще идёт плохое (связь {rho:+.2f}). "
            "Копировать «тех, кто в форме» здесь вредно.",
        }
    return {
        "holds": False,
        "title": "Связи нет — форма не держится",
        "text": f"Результат кита в прошлом окне ничего не говорит о следующем (связь {rho:+.2f}, вероятность "
        f"случайности {p:.0%}). Ротация по форме здесь не помогает — выбор «в форме» не лучше случайного.",
    }


def simulate_rotation(whales: list[Whale], rule: FormRule, now: int, seed: int = 7, n_random: int = 300) -> dict:
    """Расчёт экрана ротации по готовым кривым прибыли и сделкам китов."""
    W = rule.window_weeks
    prepared = []
    first_ts = None
    for w in whales:
        if len(w.series) < 2:
            continue
        ts = np.array([t for t, _ in w.series], dtype=np.int64)
        prepared.append((w, ts))
        first_ts = ts[0] if first_ts is None else min(first_ts, ts[0])
    if not prepared or first_ts is None:
        return {"ok": False, "error": "Нет кривых прибыли китов из Polymarket", "rule": asdict(rule)}
    # недели, выровненные к now: T_K = now, T_k = now − (K−k)·неделя
    n_weeks = int((now - first_ts) // WEEK)
    bounds = [now - (n_weeks - k) * WEEK for k in range(n_weeks + 1)]
    K = len(bounds)
    E = len(prepared)
    cum = np.full((E, K), np.nan)
    vol = np.zeros((E, K))  # объём покупок за неделю, заканчивающуюся в T_k
    bets_w: list[list[set]] = [[set() for _ in range(K)] for _ in range(E)]
    for i, (w, ts) in enumerate(prepared):
        for k, t in enumerate(bounds):
            v = value_at(w.series, ts, t)
            cum[i, k] = np.nan if v is None else v
        for t, usd, market in w.buys:
            k = int(math.ceil((t - bounds[0]) / WEEK)) if t > bounds[0] else -1
            if 1 <= k < K:
                vol[i, k] += usd
                bets_w[i][k].add(market)
    # недели, где у нас есть сделки кита: прибыль без сделок делилась бы на пропущенный объём
    covered_from = np.array(
        [math.ceil((min(b[0] for b in w.buys) - bounds[0]) / WEEK) if w.buys else K + 1 for w, _ in prepared]
    )
    cum[np.arange(K)[None, :] < covered_from[:, None]] = np.nan
    # доходность недели k (T_{k-1}, T_k]: прибыль / вложенное (CAPITAL_WEEKS недель типичных покупок)
    ret = np.full((E, K), np.nan)
    for k in range(1, K):
        lo = max(1, k - CAPITAL_LOOKBACK + 1)
        n_cov = np.clip(k + 1 - np.maximum(lo, covered_from), 1, None)  # недель с данными в окне
        denom = vol[:, lo : k + 1].sum(axis=1) / n_cov * CAPITAL_WEEKS
        pnl = cum[:, k] - cum[:, k - 1]
        ok = np.isfinite(pnl) & (denom >= MIN_DENOM_USD)
        ret[ok, k] = np.clip(pnl[ok] / denom[ok], -WEEK_CLIP, WEEK_CLIP)

    weeks_out, in_form_hist = [], []
    curve_rot, curve_all, curve_ideal = [0.0], [0.0], [0.0]
    rnd_cum = np.zeros(n_random)
    rnd_hist = [np.zeros(n_random)]
    rng = np.random.default_rng(seed)
    pairs: list[tuple[int, float, float]] = []
    hits_in, n_in, hits_out, n_out = 0, 0, 0, 0
    for k in range(W, K - 1):  # пересмотр в T_k, результат — за неделю k+1
        form = np.zeros(E, bool)
        for i, (w, ts) in enumerate(prepared):
            a, b = cum[i, k - W], cum[i, k]
            if not (np.isfinite(a) and np.isfinite(b)):
                continue
            gain = b - a
            n_bets = len(set().union(*bets_w[i][k - W + 1 : k + 1]))
            peak, dd = window_drawdown(w.series, ts, bounds[k - W], bounds[k])
            dd_ok = peak <= 0 or dd <= rule.max_drawdown_share * peak
            form[i] = n_bets >= rule.min_bets and gain >= rule.min_result_usd and dd_ok
            # пара «прошлое окно → следующее окно» для главной цифры (доход на вложенный доллар).
            # Берутся только даты через W недель: окна разных пар не пересекаются, иначе одна полоса удачи
            # засчитывалась бы много раз и связь находилась бы там, где её нет
            if k + W < K and (K - 1 - W - k) % W == 0:
                v_past = vol[i, k - W + 1 : k + 1].sum()
                v_next = vol[i, k + 1 : k + W + 1].sum()
                c_next = cum[i, k + W]
                if v_past >= MIN_DENOM_USD and v_next >= MIN_DENOM_USD and np.isfinite(c_next):
                    pairs.append((k, gain / v_past, (c_next - b) / v_next))
        r_next = ret[:, k + 1]
        avail = np.isfinite(r_next)
        sel = form & avail
        n_sel = int(sel.sum())
        r_rot = float(r_next[sel].mean()) if n_sel else 0.0
        r_all = float(r_next[avail].mean()) if avail.any() else 0.0
        n_pick = max(1, n_sel)
        r_ideal = float(np.sort(r_next[avail])[::-1][:n_pick].mean()) if avail.any() else 0.0
        if avail.any():
            pool = r_next[avail]
            m = min(n_pick, len(pool))
            draws = np.array([rng.choice(pool, size=m, replace=False).mean() for _ in range(n_random)])
        else:
            draws = np.zeros(n_random)
        for i in range(E):
            if avail[i] and np.isfinite(cum[i, k]):
                if form[i]:
                    n_in += 1
                    hits_in += int(r_next[i] > 0)
                else:
                    n_out += 1
                    hits_out += int(r_next[i] > 0)
        curve_rot.append(curve_rot[-1] + r_rot)
        curve_all.append(curve_all[-1] + r_all)
        curve_ideal.append(curve_ideal[-1] + r_ideal)
        rnd_cum = rnd_cum + draws
        rnd_hist.append(rnd_cum.copy())
        names = [prepared[i][0].name for i in np.flatnonzero(form)]
        in_form_hist.append(form)
        weeks_out.append(
            {
                "ts": int(bounds[k]),
                "in_form": names,
                "n_in_form": int(form.sum()),
                "rotation_pct": round(r_rot * 100, 2),
                "all_pct": round(r_all * 100, 2),
            }
        )
    if not weeks_out:
        return {
            "ok": False,
            "error": f"История короче окна: нужно больше {W} недель кривой прибыли",
            "rule": asdict(rule),
        }
    rnd_arr = np.array(rnd_hist)
    ts_curve = [int(bounds[W])] + [int(bounds[k + 1]) for k in range(W, K - 1)]

    def pct(v) -> list[float]:
        return [round(float(x) * 100, 2) for x in v]

    # кто в форме сейчас (по данным до now) — для решения на эту неделю
    current = []
    k = K - 1
    for i, (w, ts) in enumerate(prepared):
        a, b = cum[i, k - W] if k - W >= 0 else np.nan, cum[i, k]
        if not (np.isfinite(a) and np.isfinite(b)):
            current.append({"entity_id": w.entity_id, "name": w.name, "in_form": False, "why": "мало истории"})
            continue
        gain = b - a
        n_bets = len(set().union(*bets_w[i][max(0, k - W + 1) : k + 1]))
        peak, dd = window_drawdown(w.series, ts, bounds[k - W], bounds[k])
        reasons = []
        if n_bets < rule.min_bets:
            reasons.append(f"ставок {n_bets} из {rule.min_bets}")
        if gain < rule.min_result_usd:
            reasons.append("результат за окно в минусе" if gain < 0 else "результат ниже порога")
        if peak > 0 and dd > rule.max_drawdown_share * peak:
            reasons.append(
                "отдал больше, чем заработал за окно" if dd >= peak else f"отдал {dd / peak:.0%} заработанного"
            )
        current.append(
            {
                "entity_id": w.entity_id,
                "name": w.name,
                "in_form": not reasons,
                "gain_usd": round(float(gain), 2),
                "bets": n_bets,
                "drawdown_usd": round(float(dd), 2),
                "why": "; ".join(reasons) or "в форме",
            }
        )
    current.sort(key=lambda r: (not r["in_form"], -(r.get("gain_usd") or 0)))
    pers = persistence(pairs)
    total = {
        "rotation": curve_rot[-1],
        "all": curve_all[-1],
        "ideal": curve_ideal[-1],
        "random_median": float(np.median(rnd_arr[-1])),
    }
    return {
        "ok": True,
        "rule": asdict(rule),
        "whales": E,
        "weeks": len(weeks_out),
        "persistence": pers,
        "verdict": verdict(pers, E),
        "hit_rate": {
            "in_form": round(hits_in / n_in, 3) if n_in else None,
            "not_in_form": round(hits_out / n_out, 3) if n_out else None,
            "n_in_form": n_in,
            "n_not_in_form": n_out,
        },
        "curves": {
            "ts": ts_curve,
            "rotation": pct(curve_rot),
            "all": pct(curve_all),
            "ideal": pct(curve_ideal),
            "random_median": pct(np.median(rnd_arr, axis=1)),
            "random_p10": pct(np.percentile(rnd_arr, 10, axis=1)),
            "random_p90": pct(np.percentile(rnd_arr, 90, axis=1)),
        },
        "total_pct": {k: round(v * 100, 1) for k, v in total.items()},
        "weeks_in_cash": sum(1 for w in weeks_out if w["n_in_form"] == 0),
        "history": weeks_out[-26:],
        "current": current,
    }


def load_whales(db: Any, players: Any, entity_ids: list[str] | None = None, name_of: Any = None) -> list[Whale]:
    """Пул: киты из списка пользователя. Кривые — готовые из Polymarket (по каждому кошельку кита),
    покупки — из сохранённых сделок."""
    from .scoring.scorer import short
    from .util import pmap

    if entity_ids is None:
        entity_ids = [r["entity_id"] for r in db.query("SELECT entity_id FROM watchlist WHERE mode != 'off'")]
    out = []
    for e in sorted(set(entity_ids)):
        wallets = [r["wallet"] for r in db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))] or [e]
        got = pmap(lambda w: players.pnl_series(w), wallets, workers=min(8, len(wallets)))
        series = merge_series([res for _, res, exc in got if exc is None and res])
        ph = ",".join("?" for _ in wallets)
        buys = [
            (int(r["ts"]), float(r["size_usd"] or 0), r["market_id"] or r["token_id"])
            for r in db.query(
                f"SELECT ts, size_usd, market_id, token_id FROM trades WHERE wallet IN ({ph}) AND side='BUY' "
                "AND (source IS NULL OR source='')",
                wallets,
            )
        ]
        name = name_of(e) if name_of is not None else None
        out.append(Whale(entity_id=e, name=name or short(e), series=series, buys=buys))
    return out
