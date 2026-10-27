from datetime import date, datetime, timedelta

import pytest
import yaml

from polledge.timeutil import utcnow
from polledge.cache import PageCache
from polledge.config import Settings
from polledge.db import PollRow, make_session_factory
from polledge.errors import PollEdgeError
from polledge.migrate import copy_database
from polledge.polls.repository import load_polls, upsert_polls
from polledge.polls.types import Poll
from polledge.services import delete_calibration, load_calibration, open_app, save_calibration


def test_db_url(monkeypatch, tmp_path):
    monkeypatch.delenv("POLLEDGE_DATABASE_URL", raising=False)
    s = Settings(base_dir=tmp_path)
    assert s.db_url() == f"sqlite:///{tmp_path / 'data' / 'polledge.db'}"
    assert Settings(database_url="postgres://u:p@h/db").db_url() == "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url="postgresql://u:p@h/db?sslmode=require").db_url() == \
        "postgresql+psycopg://u:p@h/db?sslmode=require"
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", "sqlite:///x.db")
    assert Settings(database_url="postgres://ignored").db_url() == "sqlite:///x.db"


def test_page_cache_roundtrip():
    cache = PageCache(make_session_factory(":memory:"), "ns")
    assert cache.get("u") is None
    when = utcnow() - timedelta(hours=2)
    cache.put("u", "body", fetched_at=when)
    page = cache.get("u")
    assert page.body == "body" and 1.9 < page.age_hours() < 2.1
    cache.put("u", "new")
    assert cache.get("u").body == "new"
    assert PageCache(cache.sessions, "other").get("u") is None  # namespaces are separate


def _project(tmp_path, calibrated: dict | None = None):
    (tmp_path / "config" / "elections").mkdir(parents=True)
    settings = {"database": str(tmp_path / "db.sqlite"), "log_dir": None, "model": {"sigma_sys_pp": 2.5}}
    (tmp_path / "config" / "settings.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    if calibrated is not None:
        (tmp_path / "config" / "calibrated.yaml").write_text(yaml.safe_dump(calibrated), encoding="utf-8")
    return tmp_path / "config" / "settings.yaml"


def test_calibration_lives_in_database(tmp_path, monkeypatch):
    monkeypatch.delenv("POLLEDGE_DATABASE_URL", raising=False)
    ctx = open_app(_project(tmp_path))
    assert ctx.settings.model.sigma_sys_pp == 2.5
    with ctx.session() as session:
        save_calibration(session, {"model": {"sigma_sys_pp": 1.5}})
    ctx = open_app(tmp_path / "config" / "settings.yaml")
    assert ctx.settings.model.sigma_sys_pp == 1.5
    assert ctx.settings.uncalibrated_model.sigma_sys_pp == 2.5
    with ctx.session() as session:
        assert delete_calibration(session)
    assert open_app(tmp_path / "config" / "settings.yaml").settings.model.sigma_sys_pp == 2.5


def test_legacy_calibration_file_is_imported(tmp_path, monkeypatch):
    monkeypatch.delenv("POLLEDGE_DATABASE_URL", raising=False)
    ctx = open_app(_project(tmp_path, {"model": {"half_life_days": 3}}))
    assert ctx.settings.model.half_life_days == 3
    with ctx.session() as session:
        assert load_calibration(session)["model"] == {"half_life_days": 3}


def test_copy_database(tmp_path):
    src_url, dst_url = f"sqlite:///{tmp_path / 'a.db'}", f"sqlite:///{tmp_path / 'b.db'}"
    src = make_session_factory(src_url)
    with src() as s:
        upsert_polls(s, [Poll("e", "first", "Ifop", date(2026, 10, 1), 1000, {"A": 60.0, "B": 40.0}, scenario="A|B")])
        save_calibration(s, {"model": {"sigma_sys_pp": 2.0}})
    counts = copy_database(src_url, dst_url)
    assert counts["poll"] == 1 and counts["poll_share"] == 2 and counts["app_setting"] == 1
    dst = make_session_factory(dst_url)
    with dst() as s:
        (poll,) = load_polls(s, "e")
        assert poll.shares == {"A": 60.0, "B": 40.0}
        # New rows still get fresh ids after the copy.
        upsert_polls(s, [Poll("e", "first", "Elabe", date(2026, 10, 2), 900, {"A": 55.0, "B": 45.0}, scenario="A|B")])
        assert s.query(PollRow).count() == 2
    with pytest.raises(PollEdgeError):
        copy_database(src_url, dst_url)  # target not empty
    assert copy_database(src_url, dst_url, replace=True)["poll"] == 1
