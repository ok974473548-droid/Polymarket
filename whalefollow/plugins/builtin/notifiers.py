"""Встроенные каналы уведомлений: Telegram (с кнопками и командами), push, Discord, почта.

Включение — флажки «Куда» в настройках (`notify.telegram`, `notify.push`, ...), как и раньше.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from typing import Any

from ... import net
from ..base import Notice, NotifierPlugin, PluginContext


class Telegram(NotifierPlugin):
    name = "telegram"
    title = "Telegram"
    description = "Сообщения от бота; под сигналом — «Купить» / «Пропустить», команды /status /stop /resume"
    setting = ("notify", "telegram")
    requires_env = ("WF_TELEGRAM_BOT_TOKEN", "WF_TELEGRAM_CHAT_ID")

    def __init__(self) -> None:
        self._tg: Any = None
        self._lock = threading.Lock()

    def bot(self, ctx: PluginContext) -> Any:
        from ...notifier import TelegramNotifier

        with self._lock:
            if self._tg is None or self._tg.db is not ctx.db:
                self._tg = TelegramNotifier(ctx.db, ctx.env("WF_TELEGRAM_BOT_TOKEN"), ctx.env("WF_TELEGRAM_CHAT_ID"))
            return self._tg

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        tg = self.bot(ctx)
        n = {"kind": notice.kind, "title": notice.title, "body": notice.body, "ref": notice.ref}
        tg.q.put({"text": tg.format(n), "reply_markup": tg.buttons(n)})  # отправку и её итог ведёт сам бот

    def start(self, ctx: PluginContext, services: Any = None) -> None:
        self.bot(ctx).attach(services)  # приём нажатий и команд — только в процессе с агентом


class Push(NotifierPlugin):
    name = "push"
    title = "Push на телефон и компьютер"
    description = "Уведомления браузера даже при закрытой вкладке; устройства подписываются в настройках"
    setting = ("notify", "push")
    kinds = ("signal", "whale_exit", "whale_check", "pause", "stopped", "system", "weekly", "event_edge")

    def __init__(self) -> None:
        self._sender: Any = None

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        from ... import push

        if not push.devices(ctx.db):
            return  # никто не подписан — отправлять некуда, это не ошибка
        if self._sender is None or self._sender.db is not ctx.db:
            self._sender = push.PushNotifier(ctx.db)
        msg = push.message({"kind": notice.kind, "title": notice.title, "body": notice.body, "ref": notice.ref})
        r = self._sender.send_all(msg)
        if r["failed"] and not r["sent"]:
            raise RuntimeError("не доставлено: " + "; ".join(r["errors"][:2]))


class Discord(NotifierPlugin):
    name = "discord"
    title = "Discord"
    description = "Сообщения в канал Discord через вебхук (WF_DISCORD_WEBHOOK)"
    setting = ("notify", "discord")
    requires_env = ("WF_DISCORD_WEBHOOK",)

    def __init__(self, session: Any = None) -> None:
        self.s = session

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        text = (
            f"**{notice.title}**"
            + (f"\n{notice.body}" if notice.body else "")
            + (f"\n{notice.url}" if notice.url else "")
        )
        s = self.s or net.client()
        r = s.post(
            ctx.env("WF_DISCORD_WEBHOOK"), json={"content": text[:1900], "allowed_mentions": {"parse": []}}, timeout=15
        )
        if r.status_code >= 300:
            raise RuntimeError(f"Discord ответил HTTP {r.status_code}")


class Email(NotifierPlugin):
    name = "email"
    title = "Почта"
    description = "Письма через ваш почтовый сервер (SMTP): WF_SMTP_HOST, WF_EMAIL_TO, при необходимости логин и пароль"
    setting = ("notify", "email")
    default_enabled = False
    requires_env = ("WF_SMTP_HOST", "WF_EMAIL_TO")
    kinds = ("signal", "whale_exit", "pause", "stopped", "system", "weekly", "event_edge")

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        host = ctx.env("WF_SMTP_HOST")
        port = int(ctx.env("WF_SMTP_PORT") or 587)
        user, password = ctx.env("WF_SMTP_USER"), os.environ.get("WF_SMTP_PASSWORD", "")
        to = [a.strip() for a in ctx.env("WF_EMAIL_TO").split(",") if a.strip()]
        msg = EmailMessage()
        msg["Subject"] = f"WhaleFollow: {notice.title}"[:200]
        msg["From"] = ctx.env("WF_EMAIL_FROM") or user or to[0]
        msg["To"] = ", ".join(to)
        msg.set_content((notice.body + "\n\n" if notice.body else "") + (notice.url or ""))
        tls = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=tls, timeout=20) as smtp:
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.starttls(context=tls)
                if user:
                    smtp.login(user, password)
                smtp.send_message(msg)


PLUGINS = [Telegram(), Push(), Discord(), Email()]
