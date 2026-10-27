"""Smoke tests of the Streamlit pages on a temporary database (no network)."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from polledge.config import load_election
from polledge.db import make_session_factory
from polledge.odds.file_source import CsvOddsSource
from polledge.odds.repository import store_quotes
from polledge.polls.parser import parse_election_html
from polledge.polls.repository import upsert_polls

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["model_page", "signals_page", "odds_page", "journal_page", "calibration_page"]


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('web') / 'web.db'}"
    election = load_election(ROOT / "config" / "elections" / "fr_pres_2027.yaml")
    html = (ROOT / "tests" / "fixtures" / "fr_pres_2027_snippet.html").read_text(encoding="utf-8")
    sessions = make_session_factory(url)
    with sessions() as session:
        upsert_polls(session, parse_election_html(html, election))
        store_quotes(session, CsvOddsSource(ROOT / "data" / "odds" / "fr_pres_2027_sample.csv").fetch(), "csv")
    return url


def _run_page(page: str) -> None:
    import streamlit as st

    from polledge.web import views
    from polledge.web.state import context

    context()
    st.session_state["election_id"] = "fr_pres_2027"
    getattr(views, page)()


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page, database, monkeypatch):
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from polledge.web.state import reload_context

    reload_context()
    at = AppTest.from_function(_run_page, args=(page,), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title  # every page has a heading


def test_signals_page_shows_signals(database, monkeypatch):
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    from polledge.web.state import reload_context

    reload_context()
    at = AppTest.from_function(_run_page, args=("signals_page",), default_timeout=120)
    at.run()
    assert not at.exception
    assert len(at.dataframe) >= 3  # one table per market with odds


def test_environment_overrides_secrets_file(database, monkeypatch):
    # A developer's .streamlit/secrets.toml must never redirect tests to the production database.
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    from polledge.web.state import secret

    assert secret("POLLEDGE_DATABASE_URL") == database


def test_login_required_for_hosted_database(monkeypatch, database):
    # A non-SQLite URL without APP_PASSWORD must refuse to run (checked before any query).
    from polledge.web import state

    class Ctx:
        class settings:
            @staticmethod
            def db_url():
                return "postgresql+psycopg://x"

    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert not state.is_local_sqlite(Ctx())
