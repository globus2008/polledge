"""Web page for adding and editing elections without touching the code or the repository.

Configs saved here live in the database (app_setting), so the web app, the NAS container and the
GitHub Actions job all pick them up on their next run - no commit, no rebuild.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from polledge.config import discover_elections, election_files, election_sources, parse_election_yaml
from polledge.errors import PollEdgeError
from polledge.i18n import _
from polledge.web.state import context

MARKET_TEMPLATE = """\
# Election without a poll model: the prediction market (Polymarket) plays the model and
# signals appear when a Czech bookmaker pays more than its price.
id: xx_pres_2026                 # unique id: letters, digits, underscores
name: "Country – presidential election 2026"
type: single_round               # single_round | two_round
election_date: 2026-11-01
probability_source: market
reference_market: polymarket

candidates:                      # id: name and the names bookmakers / Polymarket use
  CAND_A: {name: "Candidate A", aliases: ["Full Name A"]}
  CAND_B: {name: "Candidate B", aliases: ["Full Name B"]}

bookmakers:
  polymarket:
    pages: ["polymarket-event-slug"]          # the part of the address after polymarket.com/event/
    markets:
      "polymarket-event-slug": winner
  betano:                                     # optional: Betano is searched for these words
    discover: ["Country", "prezident"]
    markets:
      "Vítěz": winner
"""

POLLS_TEMPLATE = """\
# Election with a poll model from Wikipedia ("Opinion polling for ..." page or section).
id: xx_parl_2027
name: "Country – parliamentary election 2027"
type: single_round
election_date: 2027-05-01
wiki_page: "Opinion polling for the 2027 Country parliamentary election"
default_sample_size: 1000

candidates:
  PARTY_A: {name: "Party A", aliases: ["PA"]}
  PARTY_B: {name: "Party B", aliases: ["PB"]}

first_round:
  table_match: "Polling firm"     # text that identifies the poll table
  columns: {pollster: "Polling firm", fieldwork: "Fieldwork date", sample: "Sample size"}
  candidates: {"PA": PARTY_A, "PB": PARTY_B}   # column header -> candidate id
"""

NEW_MARKET, NEW_POLLS = "new:market", "new:polls"


def _initial_text(choice: str, ctx) -> str:
    if choice == NEW_MARKET:
        return MARKET_TEMPLATE
    if choice == NEW_POLLS:
        return POLLS_TEMPLATE
    kind, election_id = choice.split(":", 1)
    if kind == "edit":
        return ctx.settings.db_elections.get(election_id, "")
    path = election_files(ctx.settings).get(election_id)
    return path.read_text(encoding="utf-8") if path else ""


def _preview(ctx, cfg) -> None:
    """Dry run: parse polls from Wikipedia and map Polymarket names - nothing is stored."""
    from polledge.odds.scrapers.mapping import NameMapper, RawMarket
    from polledge.odds.scrapers.polymarket import API_URL, parse_event
    from polledge.polls.cleaning import normalize_name
    from polledge.polls.parser import parse_election_html

    if cfg.uses_polls:
        try:
            with st.spinner(_("Reading polls from Wikipedia...")):
                polls = parse_election_html(ctx.wiki_client().get_page_html(cfg.wiki_page), cfg)
        except PollEdgeError as exc:
            st.error(_("Polls: {error}", error=exc))
        else:
            st.write(_("Polls found: {count}", count=len(polls)))
            rows = [{_("Pollster"): p.pollster, _("Date"): p.end_date, _("Round"): p.round, _("Sample"): p.sample_size,
                     **{cfg.candidate_name(c): v for c, v in p.shares.items()}}
                    for p in sorted(polls, key=lambda p: p.end_date, reverse=True)[:15]]
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    book = cfg.bookmakers.get("polymarket")
    if book is None:
        return
    fetcher = ctx.fetcher()
    try:
        mapper = NameMapper(cfg, book)
        for slug in book.pages:
            try:
                body, _fetched = fetcher.get_with_time(API_URL.format(slug=slug))
                title, selections = parse_event(body)
            except PollEdgeError as exc:
                st.error(f"Polymarket {slug}: {exc}")
                continue
            mapped = mapper.map_market(RawMarket(slug, [(n, 1 / p) for n, p in selections]))
            st.write(f"**Polymarket – {title}**" + ("" if mapped else " · " + _("no market mapping for this slug")))
            if mapped:
                st.dataframe(pd.DataFrame([
                    {_("Name on Polymarket"): n, _("Price"): p,
                     _("Candidate"): n if n not in mapped.unmatched and n in mapped.odds  # binary / seat brackets
                     else _("not matched") if n in mapped.unmatched
                     else cfg.candidate_name(mapper.candidates.get(normalize_name(n), n))}
                    for n, p in selections
                ]), hide_index=True, width="stretch")
                if mapped.unmatched:
                    st.warning(_("Add these names to the candidates' aliases: {names}",
                                 names=", ".join(mapped.unmatched)))
    finally:
        fetcher.close()


def elections_page() -> None:
    from polledge.services import delete_db_election, refresh_db_elections, save_db_election

    ctx = context()
    st.title(_("Elections"))
    st.caption(_("Elections saved here are stored in the database: the web app, the NAS container and the hourly "
                 "GitHub job use them on their next run. No change of the code or a rebuild is needed."))
    if message := st.session_state.pop("elections_flash", None):
        st.success(message)

    sources = election_sources(ctx.settings)
    configs = discover_elections(ctx.settings, include_history=True)
    st.dataframe(pd.DataFrame([
        {"id": cfg.id, _("Name"): cfg.name, _("Date"): cfg.election_date,
         _("Probabilities"): _("polls") if cfg.uses_polls else _("market ({source})", source=cfg.reference_market),
         _("Stored in"): _("database") if sources.get(cfg.id) == "database" else _("file in the repository")}
        for cfg in sorted(configs.values(), key=lambda c: c.election_date, reverse=True)
    ]), hide_index=True, width="stretch")

    st.subheader(_("Add or edit an election"))
    file_ids = [eid for eid, src in sources.items() if src == "file"]
    options = [NEW_MARKET, NEW_POLLS, *(f"edit:{e}" for e in sorted(ctx.settings.db_elections)),
               *(f"copy:{e}" for e in sorted(file_ids))]
    # A widget's value can only be set before it is drawn: apply the choice requested by Save / Delete.
    if "elections_next_choice" in st.session_state:
        st.session_state["elections_choice"] = st.session_state.pop("elections_next_choice")
    if st.session_state.get("elections_choice") not in options:
        st.session_state.pop("elections_choice", None)
    labels = {NEW_MARKET: _("New election – prices from Polymarket"), NEW_POLLS: _("New election – polls from Wikipedia")}
    choice = st.selectbox(_("Start from"), options, key="elections_choice", format_func=lambda o: labels.get(o) or (
        _("Edit {name}", name=configs[o[5:]].name if o[5:] in configs else o[5:]) if o.startswith("edit:")
        else _("Copy of {name}", name=configs[o[5:]].name if o[5:] in configs else o[5:])))
    st.caption(_("Copy of an election from a file: saving it under the same id overrides the file. "
                 "Change the id to create a new election."))
    text = st.text_area(_("Configuration (YAML)"), value=_initial_text(choice, ctx), height=460,
                        key=f"elections_yaml_{choice}")

    c1, c2, c3 = st.columns(3)
    check = c1.button(_("Check"), width="stretch")
    save = c2.button(_("Save"), type="primary", width="stretch")
    delete = choice.startswith("edit:") and c3.button(_("Delete"), width="stretch")
    if check or save:
        try:
            cfg = parse_election_yaml(text)
        except PollEdgeError as exc:
            st.error(str(exc))
            return
        if save:
            with ctx.session() as session:
                save_db_election(session, text)
            refresh_db_elections(ctx)
            st.cache_data.clear()
            st.session_state["elections_flash"] = _("Election '{name}' saved.", name=cfg.name)
            st.session_state["elections_next_choice"] = f"edit:{cfg.id}"
            st.rerun()
        st.success(_("The configuration is valid: {name}, {date}.", name=cfg.name, date=cfg.election_date))
        _preview(ctx, cfg)
    if delete:
        election_id = choice[5:]
        with ctx.session() as session:
            delete_db_election(session, election_id)
        refresh_db_elections(ctx)
        st.cache_data.clear()
        st.session_state["elections_flash"] = _("Election '{id}' deleted from the database.", id=election_id)
        st.session_state["elections_next_choice"] = NEW_MARKET
        st.rerun()
