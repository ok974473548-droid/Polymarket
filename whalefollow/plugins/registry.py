"""Реестр плагинов: поиск, включение/выключение, состояние и ошибки.

Где ищутся плагины (по порядку; при совпадении имени остаётся первый, второй — в списке ошибок загрузки):
  1. встроенные (`whalefollow/plugins/builtin/`);
  2. пакеты Python с entry point в группе `whalefollow.plugins` (объект плагина, класс, список или функция);
  3. файлы `*.py` в папке плагинов: `WF_PLUGINS_DIR`, иначе `/data/plugins` (Docker) или `./plugins`.
     В файле — список `PLUGINS = [...]` или просто объекты плагинов на верхнем уровне модуля.

Включён ли плагин: у старых встроенных — прежние настройки (`news_sources`, `notify`), чтобы ничего не
сломалось; у остальных — настройка `plugins` ({имя: true/false}), по умолчанию `default_enabled`.
Состояние (последний успех, последняя ошибка, счётчики) хранится в базе — его видят все процессы.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

from .base import KINDS, Plugin, PluginContext

log = logging.getLogger(__name__)

EP_GROUP = "whalefollow.plugins"


def plugins_dir() -> Path | None:
    env = os.environ.get("WF_PLUGINS_DIR")
    if env:
        return Path(env)
    for p in (Path("/data/plugins"), Path("plugins")):
        if p.is_dir():
            return p
    return None


def _as_plugins(obj: Any) -> list[Plugin]:
    """Объект, класс, список или функция → список плагинов."""
    if isinstance(obj, Plugin):
        return [obj]
    if isinstance(obj, type) and issubclass(obj, Plugin):
        return [obj()]
    if isinstance(obj, (list, tuple)):
        out: list[Plugin] = []
        for x in obj:
            out += _as_plugins(x)
        return out
    if callable(obj):
        return _as_plugins(obj())
    raise TypeError(f"не плагин: {obj!r}")


class Registry:
    def __init__(self) -> None:
        self.plugins: dict[str, Plugin] = {}
        self.load_errors: list[dict] = []  # {"origin", "error"} — показываются на экране «Плагины»
        self._lock = threading.Lock()

    # ------------------------------------------------------------ загрузка
    def register(self, p: Plugin, origin: str = "встроенный") -> bool:
        if not p.name or p.kind not in KINDS:
            self.load_errors.append({"origin": origin, "error": f"у плагина {p!r} нет имени или вид не из {KINDS}"})
            return False
        if p.name in self.plugins:
            self.load_errors.append(
                {"origin": origin, "error": f"плагин «{p.name}» уже есть ({self.plugins[p.name].origin})"}
            )
            return False
        p.origin = origin
        self.plugins[p.name] = p
        return True

    def load_builtin(self) -> None:
        from .builtin import event_sources, notifiers, sources, strategies

        for mod in (sources, event_sources, notifiers, strategies):
            for p in mod.PLUGINS:
                self.register(p, "встроенный")

    def load_entry_points(self) -> None:
        try:
            eps = metadata.entry_points(group=EP_GROUP)
        except Exception as e:  # pragma: no cover — повреждённые метаданные пакетов
            self.load_errors.append({"origin": "entry points", "error": str(e)})
            return
        for ep in eps:
            try:
                for p in _as_plugins(ep.load()):
                    self.register(p, f"пакет {ep.value}")
            except Exception as e:
                self.load_errors.append({"origin": f"пакет {ep.value}", "error": f"{type(e).__name__}: {e}"})

    def load_dir(self, folder: Path | None) -> None:
        if folder is None or not folder.is_dir():
            return
        for f in sorted(folder.glob("*.py")):
            if f.name.startswith("_"):
                continue
            origin = f"файл {f.name}"
            try:
                mod_name = f"wf_plugin_{f.stem}"
                spec = importlib.util.spec_from_file_location(mod_name, f)
                if spec is None or spec.loader is None:
                    raise ImportError("не удалось прочитать файл")
                mod = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                found = getattr(mod, "PLUGINS", None)
                items = (
                    _as_plugins(found)
                    if found is not None
                    else [v for v in vars(mod).values() if isinstance(v, Plugin)]
                )
                if not items:
                    raise ValueError("в файле нет плагинов: объявите PLUGINS = [МойПлагин()]")
                for p in items:
                    self.register(p, origin)
            except Exception as e:
                log.warning("плагин %s: %s", f, e)
                tb = traceback.format_exception_only(type(e), e)[-1].strip()
                self.load_errors.append({"origin": origin, "error": tb})

    # ------------------------------------------------------------ выборка
    def of_kind(self, kind: str) -> list[Plugin]:
        return [p for p in self.plugins.values() if p.kind == kind]

    def get(self, name: str) -> Plugin | None:
        return self.plugins.get(name)

    # ------------------------------------------------------------ включение
    @staticmethod
    def is_enabled(p: Plugin, settings: dict) -> bool:
        if p.setting is not None:
            group, key = p.setting
            v = (settings.get(group) or {}).get(key)
            return p.default_enabled if v is None else bool(v)
        v = (settings.get("plugins") or {}).get(p.name)
        return p.default_enabled if v is None else bool(v)

    def set_enabled(self, db: Any, name: str, on: bool) -> bool:
        p = self.get(name)
        if p is None:
            return False
        if p.setting is not None:
            group, key = p.setting
            cur = dict(db.get_setting(group) or {})
            cur[key] = bool(on)
            db.set_setting(group, cur)
        else:
            cur = dict(db.get_setting("plugins") or {})
            cur[name] = bool(on)
            db.set_setting("plugins", cur)
        return True

    def active(self, kind: str, ctx: PluginContext) -> list[Plugin]:
        """Включённые и доступные плагины этого вида."""
        out = []
        for p in self.of_kind(kind):
            try:
                if self.is_enabled(p, ctx.settings) and p.available(ctx) is None:
                    out.append(p)
            except Exception as e:
                record(ctx.db, p.name, ok=False, error=f"проверка доступности: {e}")
        return out


# ---------------------------------------------------------------- состояние (видно всем процессам)
def record(db: Any, name: str, ok: bool, error: str = "", note: str = "") -> None:
    """Итог вызова плагина: успех или ошибка. Пишется в базу (видно всем процессам); успехи —
    не чаще раза в 5 с на плагин, чтобы частые вызовы не нагружали базу. Ошибки — сразу."""
    try:
        now = time.time()
        _pending[name] = _pending.get(name, 0) + 1
        if ok and now - _last_write.get(name, 0.0) < 5:
            return
        key = "plugin:" + name
        st = dict(db.get_state(key) or {})
        st["calls"] = st.get("calls", 0) + _pending.pop(name, 0)
        _last_write[name] = now
        if ok:
            st["ok_ts"] = now
            if note:
                st["note"] = note[:200]
        else:
            st["errors"] = st.get("errors", 0) + 1
            st["err_ts"] = now
            st["error"] = (error or "ошибка")[:300]
        db.set_state(key, st)
    except Exception:  # состояние — подсказка для экрана, не должно мешать работе
        pass


_pending: dict[str, int] = {}
_last_write: dict[str, float] = {}


def status(db: Any, name: str) -> dict:
    return dict(db.get_state("plugin:" + name) or {})


# ---------------------------------------------------------------- общий реестр процесса
_REG: Registry | None = None
_REG_LOCK = threading.Lock()


def registry(reload: bool = False) -> Registry:
    global _REG
    with _REG_LOCK:
        if _REG is None or reload:
            r = Registry()
            r.load_builtin()
            r.load_entry_points()
            r.load_dir(plugins_dir())
            _REG = r
        return _REG


def reset_registry() -> None:
    """Для тестов: следующий вызов registry() загрузит плагины заново."""
    global _REG
    with _REG_LOCK:
        _REG = None
