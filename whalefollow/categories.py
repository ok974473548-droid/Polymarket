"""Темы рынков — как в меню Polymarket (ноябрь 2026): Politics, Sports, Esports, Crypto, Finance, Culture,
Mentions, Weather, Economics, Tech. «other» — Polymarket тему не указал.

Один список на всё приложение: разбор рынков, настройки, рейтинг трейдеров, комиссии, подписи в интерфейсе."""

from __future__ import annotations

# (ключ, подпись в интерфейсе, по умолчанию включена)
TOPICS = [
    ("politics", "Политика", True),
    ("sports", "Спорт", True),
    ("esports", "Киберспорт", True),
    ("crypto", "Крипто", False),
    ("finance", "Финансы", True),
    ("culture", "Культура", False),
    ("mentions", "Упоминания", False),
    ("weather", "Погода", False),
    ("economics", "Экономика", True),
    ("tech", "Технологии", True),
]
OTHER = ("other", "Разное (тема не указана)", True)
KEYS = [k for k, _, _ in TOPICS]
LABEL_RU = {k: lbl for k, lbl, _ in TOPICS} | {"other": "Разное"}
DEFAULT_ON = [k for k, _, on in TOPICS if on] + ["other"]

# метки и разделы Polymarket → тема
TAG_MAP = {
    "politics": "politics",
    "elections": "politics",
    "us-politics": "politics",
    "world-elections": "politics",
    "geopolitics": "politics",
    "world": "politics",
    "trump": "politics",
    "global-elections": "politics",
    "sports": "sports",
    "nba": "sports",
    "nfl": "sports",
    "nhl": "sports",
    "mlb": "sports",
    "soccer": "sports",
    "football": "sports",
    "tennis": "sports",
    "golf": "sports",
    "ufc": "sports",
    "mma": "sports",
    "boxing": "sports",
    "f1": "sports",
    "formula-1": "sports",
    "cricket": "sports",
    "ncaa": "sports",
    "cbb": "sports",
    "cfb": "sports",
    "esports": "esports",
    "e-sports": "esports",
    "counter-strike": "esports",
    "cs2": "esports",
    "csgo": "esports",
    "league-of-legends": "esports",
    "lol": "esports",
    "dota-2": "esports",
    "dota2": "esports",
    "valorant": "esports",
    "crypto": "crypto",
    "bitcoin": "crypto",
    "ethereum": "crypto",
    "solana": "crypto",
    "crypto-prices": "crypto",
    "finance": "finance",
    "stocks": "finance",
    "equities": "finance",
    "earnings": "finance",
    "ipos": "finance",
    "ipo": "finance",
    "commodities": "finance",
    "forex": "finance",
    "business": "finance",
    "big-tech-stocks": "finance",
    "economy": "economics",
    "economics": "economics",
    "fed": "economics",
    "fed-rates": "economics",
    "inflation": "economics",
    "macro": "economics",
    "gdp": "economics",
    "jobs": "economics",
    "interest-rates": "economics",
    "recession": "economics",
    "culture": "culture",
    "pop-culture": "culture",
    "entertainment": "culture",
    "movies": "culture",
    "music": "culture",
    "awards": "culture",
    "tv": "culture",
    "celebrities": "culture",
    "mentions": "mentions",
    "mention-markets": "mentions",
    "weather": "weather",
    "climate": "weather",
    "hurricanes": "weather",
    "temperature": "weather",
    "tech": "tech",
    "technology": "tech",
    "ai": "tech",
    "science": "tech",
    "space": "tech",
    "big-tech": "tech",
}
# если у рынка несколько меток — побеждает более узкая тема (киберспорт важнее спорта, финансы — экономики)
PRIORITY = ["esports", "mentions", "weather", "crypto", "finance", "economics", "tech", "culture", "sports", "politics"]


def pick(candidates) -> str:
    found = {TAG_MAP[c] for c in candidates if c in TAG_MAP}
    for k in PRIORITY:
        if k in found:
            return k
    return "other"
