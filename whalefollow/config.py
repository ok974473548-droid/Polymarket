"""Параметры системы. Значения по умолчанию — из раздела 14 ТЗ.

Все значения стартовые: подбираются на обучающем окне бэктеста и фиксируются
через params_version (хэш набора параметров), который пишется в каждое решение.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields


@dataclass
class Params:
    # --- Скоринг (раздел 6) ---
    shrink_k: float = 25.0                 # байесовское сжатие: n/(n+k)
    bootstrap_b: int = 10_000              # пересборок bootstrap
    null_sims: int = 10_000                # симуляций теста «навык или везение»
    fdr_q: float = 0.10                    # уровень Бенджамини–Хохберга
    min_clusters: int = 50                 # минимум независимых кластеров событий
    min_clusters_category: int = 30
    min_history_days: int = 90
    max_inactive_days: int = 30
    min_total_cost_usd: float = 20_000.0
    clv_windows_s: tuple = (3600, 86400)   # CLV через 1 ч и 24 ч
    clv_cutoff_before_resolution_s: int = 3600
    price_buckets: tuple = (0.0, 0.2, 0.5, 0.8, 1.0001)
    favorite_price: float = 0.9
    favorite_cost_share: float = 0.6
    favorite_pnl_share: float = 0.7
    decay_ratio_min: float = 0.5           # E90 / E365
    live_list_size: int = 100              # размер живого списка
    rating_weights: tuple = (0.40, 0.25, 0.20, 0.15)  # ci_lo, clv24, min(halves), hold_share

    # --- Классификация (раздел 7) ---
    maker_two_sided_share: float = 0.30
    maker_median_hold_h: float = 1.0
    maker_trades_per_market: float = 20.0
    maker_net_turnover: float = 0.2
    maker_flags_needed: int = 2
    insider_wallet_age_days: float = 14.0
    insider_single_event_share: float = 0.7
    insider_big_vs_volume: float = 0.05
    insider_jump_pp: float = 0.15
    insider_jump_share: float = 0.5
    insider_min_clusters: int = 10
    insider_flags_needed: int = 3

    # --- Антисибил (7.2) ---
    sybil_weights: dict = field(default_factory=lambda: {
        "L1": 0.8, "L2": 0.9, "L3": 0.5, "L4": 0.6, "L5": 0.3, "L6": 1.0})
    sybil_link_threshold: float = 1.0
    sybil_grey_threshold: float = 0.5
    sybil_jaccard_min: float = 0.6
    sybil_jaccard_min_markets: int = 20
    sybil_sync_window_s: int = 60
    sybil_sync_share: float = 0.3
    sybil_size_pattern_share: float = 0.5
    sybil_max_wallets_per_funder: int = 20  # общий источник у большего числа кошельков = биржа/мост
    funder_allowlist: tuple = ()           # адреса бирж/мостов/контрактов — не считаются общим источником
    chain_tokens: tuple = ("0x2791bca1f2de4661ed88a30c99a7a9449aa84174",   # USDC.e на Polygon
                           "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359")   # нативный USDC

    # --- Копируемость (раздел 8) ---
    max_delay_s: int = 30
    drift_abs: float = 0.015
    drift_edge_frac: float = 0.25
    depth_frac: float = 0.20
    depth_band: float = 0.02
    min_days_to_resolution: float = 1.0
    max_days_to_resolution: float = 90.0
    min_annual_return: float = 0.30
    min_hold_share: float = 0.6
    min_median_hold_h: float = 24.0
    conviction_min: float = 0.5
    min_liquidity_usd: float = 10_000.0

    # --- Решение (раздел 10) ---
    w_llm_trusted: float = 0.5
    w_llm_untrusted: float = 0.25
    agree_max_diff: float = 0.10
    require_llm: bool = False              # до этапа 7 агент может работать без LLM
    uncertainty_haircut: float = 0.02
    edge_threshold: float = 0.03
    kelly_fraction: float = 0.25
    conviction_clamp: tuple = (0.75, 1.25)
    default_fee_bps: float = 0.0           # комиссии зависят от рынка — сверять с документацией Polymarket

    # --- Риск (10.4) ---
    max_bet_frac: float = 0.02
    max_cluster_frac: float = 0.05
    max_category_frac: float = 0.20
    max_exposure_frac: float = 0.50
    max_open_per_entity: int = 3
    daily_stop_frac: float = 0.05
    monthly_stop_frac: float = 0.10

    # --- Калибровка LLM (9.5) ---
    calibration_min_n: int = 300

    # --- Работа агента ---
    signal_ttl_s: int = 120                # сколько сигнал ждёт решения пользователя
    estimate_cache_s: int = 1800

    def version(self) -> str:
        blob = json.dumps(asdict(self), sort_keys=True, default=list)
        return "p-" + hashlib.sha1(blob.encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: str | None = None, **overrides) -> "Params":
        """Параметры: значения по умолчанию ← JSON-файл ← переменные WF_<ИМЯ> ← overrides."""
        data: dict = {}
        path = path or os.environ.get("WF_PARAMS_FILE")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data.update(json.load(fh))
        names = {f.name: f for f in fields(cls)}
        for name, f in names.items():
            env = os.environ.get("WF_" + name.upper())
            if env is None:
                continue
            default = getattr(cls(), name)
            if isinstance(default, bool):
                data[name] = env.lower() in ("1", "true", "yes")
            elif isinstance(default, int):
                data[name] = int(env)
            elif isinstance(default, float):
                data[name] = float(env)
            elif isinstance(default, (tuple, dict)):
                data[name] = json.loads(env)
            else:
                data[name] = env
        data.update(overrides)
        clean = {}
        for k, v in data.items():
            if k not in names:
                raise KeyError(f"Неизвестный параметр: {k}")
            if isinstance(getattr(cls(), k), tuple) and isinstance(v, list):
                v = tuple(v)
            clean[k] = v
        return cls(**clean)


@dataclass
class Endpoints:
    """Адреса API Polymarket. Сверяйте с https://docs.polymarket.com перед запуском."""
    gamma: str = os.environ.get("WF_GAMMA_URL", "https://gamma-api.polymarket.com")
    data: str = os.environ.get("WF_DATA_URL", "https://data-api.polymarket.com")
    clob: str = os.environ.get("WF_CLOB_URL", "https://clob.polymarket.com")
    anthropic: str = os.environ.get("WF_ANTHROPIC_URL", "https://api.anthropic.com")


DEFAULT_SETTINGS = {
    # Пользовательские настройки (экран «Настройки»)
    "mode": "paper",                 # paper | manual | auto (auto в v1 заблокирован)
    "bankroll_usd": 1000.0,
    "max_bet_usd": 40.0,
    "daily_limit_usd": 200.0,
    "daily_loss_stop_frac": 0.05,
    "monthly_loss_stop_frac": 0.10,
    "categories": ["politics", "sports", "esports", "finance", "economics", "tech", "other"],   # см. categories.py
    "require_confirmation": True,
    "ai_mode": "ai",                 # ai — оценка Claude; manual — без ИИ, решение принимает пользователь
    "news_sources": {"google_news": True, "manifold": True, "reddit": False,
                     "kalshi": True,              # похожие вопросы на Kalshi (публичные цены)
                     "bookmakers": True},         # коэффициенты букмекеров для спорта (нужен WF_ODDS_API_KEY)
    "telegram_channels": [],         # публичные каналы: имена без @ (читаются через t.me/s/…)
    "notify": {"telegram": True, "push": True, "email": False,
               "signals": True, "whale_exit": True, "limits": True, "weekly": True, "system": True},
    "paper_started_ts": None,
    "param_overrides": {},
    "auto_discover": False,          # автопоиск китов по всей ленте; выключен — данные только по китам из списка           # подстройка параметров с экрана «Пробный режим» (сейчас — только порог выгоды)
}
