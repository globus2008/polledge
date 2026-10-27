"""Shared state of the Streamlit app: application context, secrets, current election, cached model."""

from __future__ import annotations

import hmac
import json
import os
from datetime import date
from pathlib import Path

import streamlit as st
from sqlalchemy import func, select

from polledge import i18n
from polledge.config import ElectionConfig, discover_elections, get_election, model_params_for
from polledge.db import PollRow
from polledge.i18n import _
from polledge.logging_setup import setup_logging
from polledge.services import AppContext, open_app

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "settings.yaml"
SECRET_NAMES = ("POLLEDGE_DATABASE_URL", "POLLEDGE_SMTP_PASSWORD")


def secret(name: str) -> str | None:
    """Environment variable first (explicit override, used by tests), then Streamlit secrets."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        return st.secrets.get(name)
    except Exception:  # no secrets file at all
        return None


@st.cache_resource(show_spinner=False)
def _context() -> AppContext:
    for name in SECRET_NAMES:
        value = secret(name)
        if value:
            os.environ[name] = value
    setup_logging(None)
    return open_app(CONFIG)


def context() -> AppContext:
    ctx = _context()
    i18n.set_language(ctx.settings.language)
    return ctx


def reload_context() -> None:
    """Drop cached context and model results, e.g. after new calibration or data."""
    _context.clear()
    st.cache_data.clear()


def is_local_sqlite(ctx: AppContext) -> bool:
    return ctx.settings.db_url().startswith("sqlite")


def require_login(ctx: AppContext) -> None:
    """Password gate. Without APP_PASSWORD the app only runs against a local SQLite database."""
    expected = secret("APP_PASSWORD")
    if not expected:
        if is_local_sqlite(ctx):
            return
        st.error(_("Set APP_PASSWORD in the app secrets - the app refuses to run unprotected with a hosted database."))
        st.stop()
    if st.session_state.get("authenticated"):
        return
    st.title("PollEdge")
    with st.form("login"):
        password = st.text_input(_("Password"), type="password")
        if st.form_submit_button(_("Sign in")):
            if hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
                st.session_state["authenticated"] = True
                st.rerun()
            st.error(_("Wrong password."))
    st.stop()


def elections(ctx: AppContext, history: bool = False) -> dict[str, ElectionConfig]:
    current = discover_elections(ctx.settings, include_history=False)
    if not history:
        return current
    return {k: v for k, v in discover_elections(ctx.settings, include_history=True).items() if k not in current}


def current_election() -> ElectionConfig:
    ctx = context()
    return get_election(ctx.settings, st.session_state["election_id"])


def poll_version(ctx: AppContext, election_id: str) -> str:
    """Changes whenever polls of the election change, so cached model results are recomputed."""
    with ctx.session() as session:
        count, fetched = session.execute(
            select(func.count(PollRow.id), func.max(PollRow.fetched_at)).where(PollRow.election_id == election_id)
        ).one()
    return f"{count}|{fetched}"


@st.cache_data(show_spinner=False, max_entries=20)
def _model(election_id: str, version: str, params_json: str, extra: tuple[str, ...], day: str):
    from polledge.services import model_markets, probabilities_for, run_model_from_db

    ctx = context()
    cfg = get_election(ctx.settings, election_id)
    with ctx.session() as session:
        sim, _params = run_model_from_db(session, ctx.settings, cfg)
    probs = probabilities_for(sim, model_markets(sim, cfg, list(extra)))
    return sim, probs


def model(cfg: ElectionConfig, extra: tuple[str, ...] = ()):
    """(SimulationResult, {market: {outcome: p}}) for the election, cached until polls or params change."""
    ctx = context()
    params = model_params_for(ctx.settings, cfg)
    return _model(cfg.id, poll_version(ctx, cfg.id), json.dumps(params.model_dump(), sort_keys=True), extra,
                  date.today().isoformat())


def money(x: float | None) -> str:
    if x is None:
        return "–"
    return f"{x:,.0f}".replace(",", " ") + f" {context().settings.signals.currency}"
