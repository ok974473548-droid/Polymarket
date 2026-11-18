"""Общие помощники: параллельные запросы к API и ограничение частоты."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")

Progress = Callable[[str, int, int], None]      # (этап, сделано, всего)


def pmap(fn: Callable[[T], R], items: Iterable[T], workers: int = 8,
         on_done: Callable[[int, int], None] | None = None) -> list[tuple[T, R | None, Exception | None]]:
    """Выполняет fn для каждого элемента в нескольких потоках (сетевые запросы).
    Возвращает [(элемент, результат | None, ошибка | None)] в исходном порядке.
    Ошибка одного элемента не прерывает остальные. Запись в БД — в вызывающем потоке."""
    items = list(items)
    out: list = [None] * len(items)
    if not items:
        return []
    if workers <= 1 or len(items) == 1:
        for i, it in enumerate(items):
            try:
                out[i] = (it, fn(it), None)
            except Exception as exc:  # noqa: BLE001 — ошибка возвращается вызывающему
                out[i] = (it, None, exc)
            if on_done:
                on_done(i + 1, len(items))
        return out
    done = 0
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                out[i] = (items[i], f.result(), None)
            except Exception as exc:  # noqa: BLE001
                out[i] = (items[i], None, exc)
            done += 1
            if on_done:
                on_done(done, len(items))
    return out


def chunks(seq: list[T], n: int) -> list[list[T]]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


class RateLimiter:
    """Ограничение частоты «ведром жетонов»: короткая пачка до burst запросов уходит сразу, без пауз,
    дальше — ровно rps в секунду. Общий для всех потоков клиента.

    Лимиты Polymarket считаются окнами по 10 с; при burst = rps за любые 10 с уходит не больше 11·rps
    запросов — это ниже лимита каждого метода при выбранных rps (см. клиенты)."""

    def __init__(self, rps: float, burst: float | None = None):
        self.rate = float(rps) if rps and rps > 0 else 0.0
        self.capacity = float(burst if burst is not None else max(1.0, self.rate))
        self._tokens = self.capacity
        self._t = time.monotonic()
        self._lock = threading.Lock()

    @property
    def interval(self) -> float:
        return 1.0 / self.rate if self.rate else 0.0

    def wait(self) -> None:
        if not self.rate:
            return
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.rate)
            self._t = now
            self._tokens -= 1.0                       # жетон берётся сразу; долг отрабатывается паузой
            delay = -self._tokens / self.rate if self._tokens < 0 else 0.0
        if delay > 0:
            time.sleep(delay)
