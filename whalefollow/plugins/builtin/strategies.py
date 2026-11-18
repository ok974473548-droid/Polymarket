"""Встроенные стратегии: «Копировать кита» (как было), «Консенсус китов», «Ротация по форме».

Стратегия только решает, брать ли покупку кита в сигнал; дальше — общие проверки (цена, стакан, лимиты, ИИ).
Каждая видит данные только на момент покупки — поэтому та же функция честно работает в бэктесте.
"""

from __future__ import annotations

from ..base import Candidate, StrategyPlugin, Verdict

DAY = 86400


class CopyWhale(StrategyPlugin):
    name = "copy_whale"
    title = "Копировать кита"
    description = "Каждая новая покупка кита на копировании — кандидат в сигнал (как было до плагинов)"
    default_enabled = True

    def accept(self, c: Candidate) -> Verdict:
        return Verdict(True, "кит из списка на копировании купил")


class Consensus(StrategyPlugin):
    name = "consensus"
    title = "Консенсус китов"
    description = "Сигнал, только когда тот же исход за последние 3 дня купили минимум 2 кита из вашего списка"
    min_whales = 2
    window_s = 3 * DAY

    def accept(self, c: Candidate) -> Verdict:
        who = set(c.buyers(self.window_s)) | {c.entity_id}
        n = len(who)
        if n >= self.min_whales:
            return Verdict(True, f"этот исход купили {n} кита из списка за 3 дня")
        return Verdict(False, f"пока купил только {n} кит из списка (нужно {self.min_whales})")


class RotationForm(StrategyPlugin):
    name = "rotation_form"
    title = "Ротация по форме"
    description = (
        "Сигнал, только если кит «в форме» по правилу с экрана «Ротация» на последний недельный пересмотр. "
        "Расписание формы обновляется при открытии экрана ротации и раз в сутки в фоне"
    )
    stale_s = 9 * DAY  # пересмотр старше — формы нет (не опираемся на устаревшее)

    def accept(self, c: Candidate) -> Verdict:
        st = c.db.get_state("rotation_schedule") or {}
        items = [r for r in st.get("items") or [] if r["ts"] <= c.ts]
        if not items:
            return Verdict(False, "нет данных о форме на эту дату — откройте экран «Ротация»")
        last = items[-1]
        if c.ts - last["ts"] > self.stale_s:
            return Verdict(False, "форма давно не пересчитывалась")
        if c.entity_id in last["ids"]:
            return Verdict(True, "кит в форме на последнем пересмотре")
        return Verdict(False, "кит не в форме на последнем пересмотре")


PLUGINS = [CopyWhale(), Consensus(), RotationForm()]
