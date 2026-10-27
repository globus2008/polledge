"""Streamlit entry point: login, election selection and navigation."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from polledge.errors import PollEdgeError
from polledge.i18n import _
from polledge.web import views
from polledge.web.state import context, elections, require_login


def main() -> None:
    st.set_page_config(page_title="PollEdge", page_icon=":material/how_to_vote:", layout="wide")
    try:
        ctx = context()
    except (PollEdgeError, SQLAlchemyError) as exc:
        st.error(_("Cannot open the database: {error}", error=str(exc).splitlines()[0]))
        st.stop()
    require_login(ctx)

    current = elections(ctx)
    if not current:
        st.error(_("No elections are configured (config/elections/*.yaml)"))
        st.stop()
    with st.sidebar:
        st.markdown("### PollEdge")
        ids = list(current)
        default = st.session_state.get("election_id", ids[0])
        st.session_state["election_id"] = st.selectbox(
            _("Election"), ids, index=ids.index(default) if default in ids else 0,
            format_func=lambda k: current[k].name,
        )

    pages = [
        st.Page(views.model_page, title=_("Model"), icon=":material/monitoring:", url_path="model", default=True),
        st.Page(views.signals_page, title=_("Signals"), icon=":material/bolt:", url_path="signals"),
        st.Page(views.odds_page, title=_("Betting odds"), icon=":material/table_chart:", url_path="odds"),
        st.Page(views.journal_page, title=_("Journal"), icon=":material/menu_book:", url_path="journal"),
        st.Page(views.calibration_page, title=_("Calibration and backtest"), icon=":material/tune:",
                url_path="calibration"),
    ]
    page = st.navigation(pages)
    try:
        page.run()
    except PollEdgeError as exc:
        st.error(str(exc))
    with st.sidebar:
        st.caption(_("Analysis only - the application never places bets."))
