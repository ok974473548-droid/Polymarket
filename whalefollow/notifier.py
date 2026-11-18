"""Уведомления и управление через Telegram (бот). Включаются переменными окружения:

  WF_TELEGRAM_BOT_TOKEN — токен бота от @BotFather
  WF_TELEGRAM_CHAT_ID   — ваш chat id (бот @userinfobot подскажет)
  WF_PUBLIC_URL         — адрес интерфейса для ссылок, по умолчанию http://localhost:8787

Что отправлять, решают настройки пользователя (экран «Настройки» → «Уведомления»).
Под сигналом — кнопки «Купить» / «Пропустить», под выходом кита — «Держать» / «Продать».
Команды: /status — состояние, /stop — остановить всё, /resume — продолжить, /help.
Бот принимает нажатия и команды только из чата WF_TELEGRAM_CHAT_ID — остальные игнорируются.
Отправка и приём идут в фоновых потоках, чтобы не задерживать агента.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any

from . import net
from .config import DEFAULT_SETTINGS
from .db import DB

log = logging.getLogger(__name__)

KIND_TO_SETTING = {
    "signal": "signals",
    "settled": "signals",
    "whale_exit": "whale_exit",
    "whale_status": "whale_exit",
    "whale_check": "whale_exit",
    "system": "system",
    "settings": "system",
    "pause": "limits",
    "stopped": "limits",
    "weekly": "weekly",
}

HELP = (
    "Команды:\n/status — состояние системы\n/stop — остановить всё (новые ставки не делаются)\n"
    "/resume — продолжить работу\n/help — эта справка\n\nПод сигналами — кнопки «Купить» и «Пропустить»."
)


class TelegramNotifier:
    def __init__(
        self, db: DB, token: str, chat_id: str, public_url: str | None = None, session: Any = None, poll: bool = False
    ):
        self.db, self.token, self.chat_id = db, token, str(chat_id)
        self.public_url = (public_url or os.environ.get("WF_PUBLIC_URL", "http://localhost:8787")).rstrip("/")
        self.s = session if session is not None else net.client()
        self.api = f"https://api.telegram.org/bot{token}"
        self.q: queue.Queue = queue.Queue()
        self.sent = 0
        self.services = None  # подключается attach(): кнопки и команды
        self._offset = 0
        self._stop = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="tg-send").start()

    @classmethod
    def from_env(cls, db: DB) -> TelegramNotifier | None:
        token, chat = os.environ.get("WF_TELEGRAM_BOT_TOKEN"), os.environ.get("WF_TELEGRAM_CHAT_ID")
        if not token or not chat:
            return None
        n = cls(db, token, chat)
        db.listeners.append(n.on_notify)
        return n

    def attach(self, services, start_polling: bool = True) -> None:
        """Включает кнопки и команды: нажатия обрабатывает Services (агент, стоп и т.д.)."""
        self.services = services
        if start_polling:
            threading.Thread(target=self._poll_loop, daemon=True, name="tg-poll").start()

    # ------------------------------------------------------------ отправка
    def wanted(self, kind: str) -> bool:
        prefs = {**DEFAULT_SETTINGS["notify"], **(self.db.get_setting("notify") or {})}
        return bool(prefs.get("telegram")) and bool(prefs.get(KIND_TO_SETTING.get(kind, ""), False))

    def link(self, kind: str, ref: str) -> str:
        if kind == "signal" and ref:
            return f"{self.public_url}/#/signal/{ref}"
        if kind == "whale_exit":
            return f"{self.public_url}/#/positions"
        if kind in ("whale_check", "whale_status") and ref:
            return f"{self.public_url}/#/whales/{ref}"
        return f"{self.public_url}/#/"

    def format(self, n: dict) -> str:
        lines = [n["title"]]
        if n.get("body"):
            lines.append(n["body"])
        lines.append(self.link(n["kind"], n.get("ref") or ""))
        return "\n".join(lines)

    def buttons(self, n: dict) -> dict | None:
        ref = n.get("ref") or ""
        if not ref or self.services is None:
            return None
        if n["kind"] == "signal" and ref.startswith("s_"):
            return {
                "inline_keyboard": [
                    [
                        {"text": "✅ Купить", "callback_data": f"c:{ref}"},
                        {"text": "Пропустить", "callback_data": f"r:{ref}"},
                    ]
                ]
            }
        if n["kind"] == "whale_exit" and ref.startswith("s_"):
            return {
                "inline_keyboard": [
                    [{"text": "Держать", "callback_data": f"h:{ref}"}, {"text": "Продать", "callback_data": f"x:{ref}"}]
                ]
            }
        return None

    def on_notify(self, n: dict) -> None:
        if self.wanted(n["kind"]):
            self.q.put({"text": self.format(n), "reply_markup": self.buttons(n)})

    def send(self, text: str, reply_markup: dict | None = None) -> None:
        self.q.put({"text": text, "reply_markup": reply_markup})

    def _call(self, method: str, payload: dict, timeout: float = 15):
        r = self.s.post(f"{self.api}/{method}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json() if hasattr(r, "json") else {}

    def _worker(self) -> None:
        while True:
            msg = self.q.get()
            try:
                payload = {"chat_id": self.chat_id, "text": msg["text"], "disable_web_page_preview": True}
                if msg.get("reply_markup"):
                    payload["reply_markup"] = msg["reply_markup"]
                self._call("sendMessage", payload)
                self.sent += 1
            except net.HTTPError as exc:
                log.warning("Telegram: не удалось отправить: %s", exc)
            finally:
                self.q.task_done()

    # ------------------------------------------------------------ приём нажатий и команд
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                res = self._call(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]},
                    timeout=35,
                )
                for upd in (res or {}).get("result", []):
                    self._offset = max(self._offset, int(upd["update_id"]) + 1)
                    self.handle_update(upd)
            except Exception as exc:
                log.warning("Telegram: приём обновлений: %s", exc)
                self._stop.wait(10)

    def _from_our_chat(self, chat_id) -> bool:
        return str(chat_id) == self.chat_id

    def handle_update(self, upd: dict) -> str | None:
        """Обрабатывает одно обновление; возвращает текст ответа (для тестов)."""
        if "callback_query" in upd:
            cq = upd["callback_query"]
            msg = cq.get("message") or {}
            if not self._from_our_chat((msg.get("chat") or {}).get("id")):
                return None
            text = self.do_action(cq.get("data") or "")
            try:
                self._call("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text[:190]})
                if msg.get("message_id"):
                    self._call(
                        "editMessageText",
                        {
                            "chat_id": self.chat_id,
                            "message_id": msg["message_id"],
                            "text": f"{msg.get('text', '')}\n\n→ {text}",
                            "disable_web_page_preview": True,
                        },
                    )
            except net.HTTPError as exc:
                log.warning("Telegram: ответ на нажатие: %s", exc)
            return text
        msg = upd.get("message") or {}
        if not self._from_our_chat((msg.get("chat") or {}).get("id")):
            return None
        cmd = (msg.get("text") or "").strip().split()[0].split("@")[0].lower() if msg.get("text") else ""
        text = self.do_command(cmd)
        if text:
            self.send(text)
        return text

    def do_action(self, data: str) -> str:
        svc = self.services
        if svc is None or ":" not in data:
            return "Действие недоступно"
        act, sid = data.split(":", 1)
        agent = getattr(svc, "agent", None)
        if agent is None:
            return "Агент не запущен"
        try:
            if act == "c":
                r = agent.confirm(sid)
                if r.get("status") == "executed":
                    return f"Куплено: {r.get('shares', 0):.1f} акций по {r.get('avg_price', 0) * 100:.0f}¢"
                return r.get("error") or "Не удалось купить"
            if act == "r":
                agent.reject(sid)
                return "Пропущено"
            if act in ("h", "x"):
                r = agent.resolve_exit(sid, "hold" if act == "h" else "sell")
                return {"held": "Держим позицию", "sold": "Продано"}.get(r.get("status"), r.get("error") or "Готово")
        except Exception as exc:
            log.warning("Telegram: действие %s: %s", data, exc)
            return f"Ошибка: {exc}"[:180]
        return "Неизвестное действие"

    def do_command(self, cmd: str) -> str | None:
        svc = self.services
        if cmd in ("/start", "/help"):
            return HELP
        if svc is None:
            return None
        if cmd == "/status":
            st = svc.status()
            d = svc.dashboard()
            bal = d.get("balance", {}) if isinstance(d, dict) else {}
            return (
                f"{'Работает' if st.get('agent_ok') else 'Агент не отвечает'}"
                f"{' · ОСТАНОВЛЕНО' if st.get('stopped') else ''}\n"
                f"Ждут решения: {st.get('need_decision', 0)}\n"
                f"Баланс: ${bal.get('equity_usd', bal.get('balance_usd', 0)):,.0f}".replace(",", " ")
            )
        if cmd == "/stop":
            svc.stop_all()
            return "Остановлено: новые ставки не делаются. /resume — продолжить."
        if cmd == "/resume":
            svc.resume()
            return "Работа продолжена."
        return None


def wait_until(pred, timeout: float = 2.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False
