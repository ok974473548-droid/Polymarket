"""Слепая оценка вероятности через Claude (ТЗ 9.2).

Модели НЕ передаются цена рынка и ставка кита — иначе она «якорится» и повторяет рынок.
Ответ — строго JSON. Ключ: переменная окружения ANTHROPIC_API_KEY.
Модель задаётся WF_LLM_MODEL (по умолчанию claude-sonnet-5-5).
"""
from __future__ import annotations

import json
import logging
import os
import re

from .. import net

from ..config import Endpoints
from .base import Estimate, MarketContext

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a careful superforecaster. You estimate the probability that a prediction-market "
    "question resolves YES. You are NOT told the market price; do not guess it or reason about it. "
    "Work from the resolution rules and the provided materials. Start from a base rate for a reference "
    "class of similar events, then adjust for specific evidence. Treat social media and Telegram posts as "
    "leads, not proof; prefer primary sources. If materials are thin, keep the estimate close to the base "
    "rate and say confidence is low. Reply with JSON only."
)

SCHEMA_HINT = {
    "base_rate": "number 0-1",
    "arguments_for": ["short strings with source names"],
    "arguments_against": ["short strings with source names"],
    "q": "number 0-1, final probability of YES",
    "confidence": "low | medium | high",
    "key_uncertainties": ["what would change the estimate"],
    "sources": ["urls or source names actually used"],
}


def build_prompt(ctx: MarketContext, max_items: int = 25) -> str:
    lines = [
        f"Question: {ctx.question}",
        f"Category: {ctx.category}",
        f"Resolution date: {ctx.end_date}",
        f"Resolution rules: {ctx.rules or 'not provided'}",
        "",
        "Materials (newest first):",
    ]
    items = sorted(ctx.materials, key=lambda m: m.get("ts_pub") or 0, reverse=True)[:max_items]
    if not items:
        lines.append("- none found")
    for m in items:
        lines.append(f"- [{m.get('source_type', '?')}] {m.get('source', '?')}: {m.get('title', '')} ({m.get('url', '')})")
    lines += ["", "Return JSON with exactly these keys:", json.dumps(SCHEMA_HINT, ensure_ascii=False)]
    return "\n".join(lines)


def parse_reply(text: str, model: str) -> Estimate | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        q = float(data["q"])
    except (ValueError, KeyError, TypeError):
        return None
    if not 0 <= q <= 1:
        return None
    conf = str(data.get("confidence", "low")).lower()
    return Estimate(
        q=min(max(q, 0.01), 0.99),
        confidence=conf if conf in ("low", "medium", "high") else "low",
        base_rate=float(data["base_rate"]) if data.get("base_rate") is not None else None,
        arguments_for=[str(x) for x in data.get("arguments_for", [])][:10],
        arguments_against=[str(x) for x in data.get("arguments_against", [])][:10],
        key_uncertainties=[str(x) for x in data.get("key_uncertainties", [])][:10],
        sources=[str(x) for x in data.get("sources", [])][:20],
        model_version=model, blind=True,
    )


class ClaudeEstimator:
    name = "claude"

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 endpoints: Endpoints | None = None, timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("WF_LLM_MODEL", "claude-sonnet-5-5")
        self.url = (endpoints or Endpoints()).anthropic.rstrip("/") + "/v1/messages"
        self.timeout = timeout
        if not self.api_key:
            raise RuntimeError("Нужен ANTHROPIC_API_KEY для оценки через Claude")

    def estimate(self, ctx: MarketContext) -> Estimate | None:
        body = {"model": self.model, "max_tokens": 1200, "system": SYSTEM,
                "messages": [{"role": "user", "content": build_prompt(ctx)}]}
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        try:
            r = net.client().post(self.url, headers=headers, json=body, timeout=self.timeout)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        except net.HTTPError as exc:
            log.warning("LLM недоступна: %s", exc)
            return None
        est = parse_reply(text, self.model)
        if est is None:
            log.warning("LLM вернула неразборчивый ответ: %s", text[:200])
        return est
