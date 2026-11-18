"""Переводы USDC в сети Polygon — для антисибила (сигналы L1 «общий источник денег» и L2 «прямые переводы»).

Используется Etherscan-совместимый запрос account/tokentx: одна страница даёт историю
переводов токена по адресу. Поддерживаются два провайдера с одинаковым форматом:

  Etherscan v2   https://api.etherscan.io/v2/api      (chainid=137, ключ WF_ETHERSCAN_API_KEY)
  Blockscout     https://api.blockscout.com/v2/api    (chain_id=137, ключ WF_BLOCKSCOUT_API_KEY — по возможности)

Какой выбрать: WF_CHAIN_PROVIDER=etherscan|blockscout (по умолчанию — тот, для которого задан ключ).
Контракты USDC на Polygon задаются параметром chain_tokens (USDC.e и нативный USDC);
если Polymarket сменит залоговый токен, достаточно поменять параметр.
"""
from __future__ import annotations

import logging
import os
import time

import requests

from ..db import DB, now_ts

log = logging.getLogger(__name__)

USDC_E = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"     # USDC.e (bridged) — залог Polymarket
USDC_NATIVE = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
DEFAULT_TOKENS = (USDC_E, USDC_NATIVE)

PROVIDERS = {
    "etherscan": {"base": "https://api.etherscan.io/v2/api", "chain_param": "chainid", "key_env": "WF_ETHERSCAN_API_KEY"},
    "blockscout": {"base": "https://api.blockscout.com/v2/api", "chain_param": "chain_id", "key_env": "WF_BLOCKSCOUT_API_KEY"},
}


class ChainError(RuntimeError):
    pass


class ExplorerClient:
    def __init__(self, provider: str = "etherscan", api_key: str | None = None, base: str | None = None,
                 chain_id: int = 137, timeout: float = 20.0, min_interval_s: float = 0.25,
                 session: requests.Session | None = None):
        cfg = PROVIDERS[provider]
        self.provider = provider
        self.base = base or cfg["base"]
        self.chain_param = cfg["chain_param"]
        self.api_key = api_key
        self.chain_id = chain_id
        self.timeout = timeout
        self.min_interval_s = min_interval_s   # бесплатные тарифы — несколько запросов в секунду
        self.s = session or requests.Session()
        self._last = 0.0

    @classmethod
    def from_env(cls) -> "ExplorerClient | None":
        provider = os.environ.get("WF_CHAIN_PROVIDER", "").lower()
        if not provider:
            provider = "etherscan" if os.environ.get("WF_ETHERSCAN_API_KEY") else (
                "blockscout" if os.environ.get("WF_BLOCKSCOUT_API_KEY") else "")
        if provider not in PROVIDERS:
            return None
        key = os.environ.get(PROVIDERS[provider]["key_env"])
        if provider == "etherscan" and not key:
            return None
        return cls(provider, key, base=os.environ.get("WF_CHAIN_API_URL") or None)

    def token_transfers(self, address: str, token: str, page_size: int = 1000, max_pages: int = 10) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            wait = self.min_interval_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            params = {self.chain_param: self.chain_id, "module": "account", "action": "tokentx",
                      "contractaddress": token, "address": address, "page": page, "offset": page_size,
                      "startblock": 0, "endblock": 99_999_999, "sort": "asc"}
            if self.api_key:
                params["apikey"] = self.api_key
            r = self.s.get(self.base, params=params, timeout=self.timeout)
            self._last = time.time()
            if r.status_code >= 400:
                raise ChainError(f"{self.provider}: HTTP {r.status_code}")
            data = r.json()
            result = data.get("result")
            if str(data.get("status")) == "0":
                msg = f"{data.get('message', '')} {result if isinstance(result, str) else ''}".strip()
                if "no transactions" in msg.lower() or result == []:
                    break
                raise ChainError(f"{self.provider}: {msg}")
            batch = result or []
            rows.extend(batch)
            if len(batch) < page_size:
                break
        return rows


def parse_transfers(rows: list[dict], tokens: tuple = DEFAULT_TOKENS) -> list[dict]:
    """tokentx → строки таблицы transfers (сумма в USDC)."""
    allowed = {t.lower() for t in tokens}
    out = []
    for r in rows:
        contract = str(r.get("contractAddress", "")).lower()
        if allowed and contract not in allowed:
            continue
        try:
            decimals = int(r.get("tokenDecimal") or 6)
            amount = int(r["value"]) / 10 ** decimals
            ts = int(r["timeStamp"])
        except (KeyError, ValueError, TypeError):
            continue
        frm, to = str(r.get("from", "")).lower(), str(r.get("to", "")).lower()
        tid = f"{r.get('hash', '')}:{r.get('logIndex', '')}:{frm}:{to}:{r.get('value', '')}"
        out.append({"transfer_id": tid, "ts": ts, "from_wallet": frm, "to_wallet": to, "amount": amount})
    return out


def sync_transfers(db: DB, client: ExplorerClient, wallets: list[str], tokens: tuple = DEFAULT_TOKENS,
                   max_wallets: int = 60, refresh_days: int = 30) -> dict:
    """Загружает переводы USDC для кошельков, у которых их ещё нет (или давно не обновлялись)."""
    now = now_ts()
    done = {r["wallet"]: r["fetched_ts"] for r in db.query("SELECT wallet, fetched_ts FROM chain_sync")}
    todo = [w for w in dict.fromkeys(x.lower() for x in wallets)
            if w not in done or now - (done[w] or 0) > refresh_days * 86400][:max_wallets]
    n_rows, errors = 0, 0
    with db.job("chain_transfers") as job:
        for w in todo:
            try:
                rows = []
                for token in tokens:
                    rows.extend(client.token_transfers(w, token))
                n_rows += db.insert_ignore("transfers", parse_transfers(rows, tokens))
                db.upsert("chain_sync", {"wallet": w, "fetched_ts": now, "error": None}, ["wallet"])
            except Exception as exc:
                errors += 1
                db.upsert("chain_sync", {"wallet": w, "fetched_ts": None, "error": repr(exc)[:200]}, ["wallet"])
                log.warning("переводы %s: %s", w, exc)
                if errors >= 5 and errors == len(todo[: todo.index(w) + 1]):
                    raise ChainError(f"Обозреватель блокчейна не отвечает: {exc}") from exc
        job["rows"] = n_rows
    return {"wallets": len(todo), "transfers": n_rows, "errors": errors}
