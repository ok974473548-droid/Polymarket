"""Готовность к реальным данным: проверка доступа, первая загрузка, тема «Разное»."""

from whalefollow import cli
from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld


def test_check_command_reports_each_api(monkeypatch, capsys):
    w = SyntheticWorld(seed=1, n_events=40, n_random=3)
    monkeypatch.setattr(cli, "_clients", lambda e: (FakeGamma(w), FakeData(w), FakeClob(w)))
    from whalefollow import diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_ws_probe",
        lambda url, sub, want_message, wait_s=6.0: (True, "соединение 120 мс, первые данные через 30 мс"),
    )  # без выхода в сеть
    assert cli.main(["check"]) == 0
    out = capsys.readouterr().out
    assert "Gamma): OK" in out and "Data API): OK" in out and "CLOB): OK" in out
    assert "Поток сделок (WebSocket): OK" in out and "Поток стаканов (WebSocket): OK" in out

    class Broken:
        def markets(self, **_):
            raise RuntimeError("HTTP 403")

    monkeypatch.setattr(cli, "_clients", lambda e: (Broken(), FakeData(w), FakeClob(w)))
    assert cli.main(["check"]) == 1
    assert "ограничен" in capsys.readouterr().out


def test_first_load_status_and_other_category():
    w = SyntheticWorld(seed=2, n_events=80, n_random=5)
    db = DB("sqlite://:memory:")
    db.init_schema()
    db.set_setting("categories", ["economics", "politics"])  # настройки, сохранённые до обновления
    db.set_setting("auto_discover", True)  # проверяем режим автопоиска китов
    p = fast_params()
    svc = Services(db, p, clock=lambda: w.now)
    assert svc.status()["bootstrap"]["running"] is False
    agent = Agent(db, p, FakeData(w), FakeClob(w), FakeGamma(w), clock=lambda: w.now)
    sch = Scheduler(
        db,
        p,
        FakeGamma(w),
        FakeData(w),
        FakeClob(w),
        agent,
        clock=lambda: w.now,
        discover_max_new=50,
        discover_pages=10,
        prices_backfill_per_run=2000,
    )
    assert "other" in db.get_setting("categories")
    sch.run_due("fast")
    st = svc.status()
    assert st["bootstrap"]["running"] and not st["bootstrap"]["scored"] and st["data_age_s"] == 0
    sch.run_due("slow")
    sch.run_due("heavy")
    b = svc.status()["bootstrap"]
    assert b["scored"] and b["candidates"] > 0 and b["error"] is None
