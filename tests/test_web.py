"""Smoke tests of the Streamlit pages on a temporary database (no network)."""

from datetime import datetime
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from polledge.config import load_election
from polledge.db import make_session_factory
from polledge.odds.base import OddsQuote
from polledge.odds.file_source import CsvOddsSource
from polledge.odds.repository import store_quotes
from polledge.polls.parser import parse_election_html
from polledge.polls.repository import upsert_polls

ROOT = Path(__file__).resolve().parents[1]
PAGES = ["model_page", "signals_page", "odds_page", "journal_page", "calibration_page"]
MARKET_PAGES = ["model_page", "signals_page", "odds_page", "journal_page"]


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('web') / 'web.db'}"
    election = load_election(ROOT / "config" / "elections" / "fr_pres_2027.yaml")
    html = (ROOT / "tests" / "fixtures" / "fr_pres_2027_snippet.html").read_text(encoding="utf-8")
    sessions = make_session_factory(url)
    with sessions() as session:
        upsert_polls(session, parse_election_html(html, election))
        store_quotes(session, CsvOddsSource(ROOT / "data" / "odds" / "fr_pres_2027_sample.csv").fetch(), "csv")
        t = datetime(2026, 10, 28, 9)
        store_quotes(session, [
            OddsQuote("us_pres_2028", "Polymarket", "winner", "VANCE", 1 / 0.18, t),
            OddsQuote("us_pres_2028", "Polymarket", "winner", "HARRIS", 1 / 0.043, t),
            OddsQuote("us_pres_2028", "Betano", "winner", "VANCE", 5.0, t),
            OddsQuote("us_pres_2028", "Betano", "winner", "HARRIS", 25.0, t),
        ], "test")
    return url


def _run_page(page: str, election: str = "fr_pres_2027") -> None:
    import streamlit as st

    from polledge.web import views
    from polledge.web.state import context

    import os

    # Guard: the page must use the temporary test database, never a database from secrets.toml.
    assert context().settings.db_url() == os.environ["POLLEDGE_TEST_DB"], "test is not using the test database"
    st.session_state["election_id"] = election
    getattr(views, page)()


@pytest.mark.parametrize("page", MARKET_PAGES)
def test_market_mode_pages_render(page, database, monkeypatch):
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
    from polledge.web.state import reload_context

    reload_context()
    at = AppTest.from_function(_run_page, args=(page, "us_pres_2028"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    if page in ("model_page", "signals_page"):
        assert at.dataframe  # reference probabilities / comparison table


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(page, database, monkeypatch):
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    from polledge.web.state import reload_context

    reload_context()
    at = AppTest.from_function(_run_page, args=(page,), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title  # every page has a heading


def test_signals_page_shows_signals(database, monkeypatch):
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
    from polledge.web.state import reload_context

    reload_context()
    at = AppTest.from_function(_run_page, args=("signals_page",), default_timeout=120)
    at.run()
    assert not at.exception
    assert len(at.dataframe) >= 3  # one table per market with odds


def test_environment_overrides_secrets_file(database, monkeypatch):
    # A developer's .streamlit/secrets.toml must never redirect tests to the production database.
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
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


def test_context_rebuilt_after_code_reload(database, monkeypatch):
    # Streamlit keeps cached resources across a redeploy; a context built by the old code must be replaced.
    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    from polledge.config import Settings
    from polledge.web import state

    state.reload_context()
    first = state.context()
    assert state.context() is first  # normally cached

    class ReloadedSettings(Settings):  # stands for the class object after a module reload
        pass

    monkeypatch.setattr(state, "Settings", ReloadedSettings)
    assert state.context() is not first


def _paste_page() -> None:
    import os

    import streamlit as st

    from polledge.web import views
    from polledge.web.state import context

    assert context().settings.db_url() == os.environ["POLLEDGE_TEST_DB"]
    st.session_state["election_id"] = "us_pres_2028"
    views._paste_odds(context(), views.current_election())


def test_paste_import_saves_winner_market(database, monkeypatch):
    from sqlalchemy import select

    from polledge.db import OddsQuoteRow
    from polledge.web.state import reload_context

    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
    reload_context()
    text = (ROOT / "tests" / "fixtures" / "paste_fortuna_us2028.txt").read_text(encoding="utf-8")
    at = AppTest.from_function(_paste_page, default_timeout=120)
    at.run()
    assert at.selectbox(key="paste_kind").value == "winner"  # default market type
    at.text_input(key="paste_bm").input("iFortuna")
    at.text_area(key="paste_text").input(text)
    at.run()
    assert not at.exception and not at.warning
    at.button(key="paste_save").click()
    at.run()
    assert at.success, [w.value for w in at.warning]
    with make_session_factory(database)() as s:
        rows = s.scalars(select(OddsQuoteRow).where(OddsQuoteRow.source == "paste")).all()
    odds = {r.outcome: r.odds for r in rows}
    assert {r.bookmaker for r in rows} == {"Fortuna"}  # "iFortuna" normalised
    assert odds["VANCE"] == 4.0 and odds["HARRIS"] == 20.0 and len(odds) == 21
    assert odds["Glenn Youngkin"] == 50.0  # unknown name kept as on the page


def test_paste_warns_about_wrong_market_type(database, monkeypatch):
    from polledge.web.state import reload_context

    monkeypatch.setenv("POLLEDGE_DATABASE_URL", database)
    monkeypatch.setenv("POLLEDGE_TEST_DB", database)
    reload_context()
    text = (ROOT / "tests" / "fixtures" / "paste_fortuna_us2028.txt").read_text(encoding="utf-8")
    at = AppTest.from_function(_paste_page, default_timeout=120)
    at.run()
    at.selectbox(key="paste_kind").set_value("share_over")
    at.run()
    at.text_area(key="paste_text").input(text)
    at.run()
    assert any("Election winner" in w.value or "Vítěz" in w.value for w in at.warning)
