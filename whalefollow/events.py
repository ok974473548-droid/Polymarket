"""Шина событий для живого интерфейса: агент сообщает «что-то изменилось», поток событий (SSE)
сразу будит открытые страницы. Внутри процесса; если событие пропущено, страницы всё равно узнают
об изменениях по сверке состояния базы (раз в полсекунды)."""

from __future__ import annotations

import threading


class Bus:
    def __init__(self):
        self._cond = threading.Condition()
        self.version = 0
        self.last: dict = {}

    def publish(self, kind: str, **data) -> None:
        with self._cond:
            self.version += 1
            self.last = {"kind": kind, **data}
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        """Ждёт события новее seen не дольше timeout секунд; возвращает текущую версию."""
        with self._cond:
            if self.version == seen:
                self._cond.wait(timeout)
            return self.version


bus = Bus()
