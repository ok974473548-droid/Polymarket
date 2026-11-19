"""Railway: порт из PORT, без пароля в облаке сервер не запускается."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whalefollow import cli

ROOT = Path(__file__).resolve().parent.parent


def test_public_cloud_detect(monkeypatch):
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    assert not cli._public_cloud()
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p1")
    assert cli._public_cloud()


def test_refuses_without_password_on_railway(monkeypatch):
    from whalefollow.db import DB

    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p1")
    monkeypatch.delenv("WF_UI_PASSWORD", raising=False)
    db = DB("sqlite://:memory:")
    db.init_schema()
    with pytest.raises(SystemExit, match="WF_UI_PASSWORD"):
        cli._serve_ui(None, db, 0, "127.0.0.1")


def test_docker_and_railway_config():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "${PORT:-8787}" in dockerfile
    assert not any(line.strip().startswith("VOLUME") for line in dockerfile.splitlines())
    cfg = json.loads((ROOT / "railway.json").read_text())
    assert cfg["build"]["builder"] == "DOCKERFILE" and cfg["deploy"]["healthcheckPath"] == "/api/auth"


@pytest.mark.sqlite_only
def test_sqlite_folder_created(tmp_path):
    from whalefollow.db import DB

    db = DB(f"sqlite:///{tmp_path}/нет/такой/папки/wf.db")  # как /data на Railway без тома
    db.init_schema()
    assert (tmp_path / "нет" / "такой" / "папки" / "wf.db").exists()
