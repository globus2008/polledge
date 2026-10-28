"""Pages of the Streamlit app. All texts are English and translated through polledge.i18n."""

from __future__ import annotations

import tempfile
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from sqlalchemy import select

from polledge.config import OTHER, ElectionResult, model_params_for
from polledge.db import PaperBetRow
from polledge.errors import PollEdgeError
from polledge.i18n import _
from polledge.model.markets import BINARY_OUTCOMES, MARKET_TYPES, MarketSpec, market_probabilities, parse_market
from polledge.odds.base import OddsQuote
from polledge.odds.file_source import quote_from_record, source_for_path
from polledge.odds.margin import latest_books
from polledge.odds.repository import delete_quotes, load_quotes, quote_rows, store_quotes
from polledge.polls.repository import load_polls
from polledge.presentation import market_label, outcome_label
from polledge.timeutil import to_local
from polledge.web import charts
from polledge.web.state import context, current_election, elections, model, money, reload_context

PCT = st.column_config.NumberColumn(format="percent")
ODDS = st.column_config.NumberColumn(format="%.2f")


def _error(exc: Exception) -> None:
    st.error(str(exc))


def analysis_only() -> None:
    st.caption(_("Analysis only - the application never places bets."))


# ---------------------------------------------------------------- model

def model_page() -> None:
    ctx = context()
    cfg = current_election()
    st.title(cfg.name)
    if not cfg.uses_polls:
        _market_overview(ctx, cfg)
        return

    with st.sidebar:
        if st.button(_("Update polls from Wikipedia"), width="stretch"):
            from polledge.services import update_polls

            wiki = ctx.wiki_client()
            try:
                with st.spinner(_("Downloading polls...")), ctx.session() as session:
                    parsed, inserted, updated = update_polls(session, wiki, cfg)
                st.success(_("{election}: {parsed} polls read, {inserted} new, {updated} updated",
                             election=cfg.name, parsed=parsed, inserted=inserted, updated=updated))
            except PollEdgeError as exc:
                _error(exc)
            finally:
                wiki.close()

    try:
        sim, probs = model(cfg)
    except PollEdgeError as exc:
        _error(exc)
        return

    avg = sim.average
    two_round = cfg.type == "two_round"
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(_("Model as of"), avg.as_of.strftime(_("%Y-%m-%d")))
    c2.metric(_("Days to the election"), (cfg.election_date - avg.as_of).days)
    c3.metric(_("Polls in the average"), avg.n_polls)
    c4.metric(_("Simulations"), f"{sim.n_sims:,}".replace(",", " "))

    lo, hi = np.quantile(sim.shares, [0.1, 0.9], axis=0)
    winner = probs.get("winner", {})
    rows = []
    for j in np.argsort(-avg.mean):
        cid = sim.candidates[j]
        row = {
            _("Candidate / party"): cfg.candidate_name(cid),
            _("Average"): avg.mean[j],
            _("10th percentile"): lo[j],
            _("90th percentile"): hi[j],
        }
        if two_round:
            row[_("Qualifies for runoff")] = None if cid == OTHER else probs.get(f"qualify_runoff:{cid}", {}).get("yes")
        row[_("Wins")] = None if cid == OTHER else winner.get(cid)
        if not two_round:
            row["≥ 5 %"] = None if cid == OTHER else probs.get(f"threshold:{cid}:5", {}).get("yes")
        rows.append(row)
    table = pd.DataFrame(rows)
    st.subheader(_("First-round estimate") if two_round else _("Result estimate"))
    config = {col: PCT for col in table.columns if col != _("Candidate / party")}
    config[_("Wins")] = st.column_config.ProgressColumn(_("Wins"), format="percent", min_value=0.0, max_value=1.0)
    st.dataframe(table, hide_index=True, width="stretch", column_config=config)

    if two_round and sim.finalists is not None:
        st.subheader(_("Most likely runoff pairs"))
        pairs = market_probabilities(sim, MarketSpec("finalists"))
        source = {"polls": _("head-to-head polls"), "fallback": _("first-round ratio (no head-to-head polls)")}
        pair_rows = [
            {_("Pair"): outcome_label(cfg, pair), _("Probability"): p,
             _("Runoff estimate source"): source.get(sim.runoff_sources.get(pair, ""), "")}
            for pair, p in sorted(pairs.items(), key=lambda kv: -kv[1])[:6]
        ]
        st.dataframe(pd.DataFrame(pair_rows), hide_index=True, width="stretch",
                     column_config={_("Probability"): PCT})

    _poll_chart(cfg, sim)
    _custom_market(cfg, sim)

    if avg.house_effects:
        with st.expander(_("House effects (pollster deviation from the average, pp)")):
            top = [sim.candidates[j] for j in np.argsort(-avg.mean)[:6] if sim.candidates[j] != OTHER]
            he = pd.DataFrame(
                [{_("Pollster"): p, **{cfg.candidate_name(c): eff.get(c, 0.0) for c in top}}
                 for p, eff in sorted(avg.house_effects.items())]
            )
            st.dataframe(he, hide_index=True, width="stretch",
                         column_config={cfg.candidate_name(c): st.column_config.NumberColumn(format="%+.1f") for c in top})

    params = model_params_for(ctx.settings, cfg)
    st.caption(_("Model parameters: half-life {hl} days, systematic error {sys} pp, horizon error {hor} pp/√day.",
                 hl=params.half_life_days, sys=params.sigma_sys_pp, hor=params.sigma_horizon_pp_per_sqrt_day))


def _market_overview(ctx, cfg) -> None:
    """Elections without a poll model: probabilities of the reference market and the best bookmaker odds."""
    from polledge.services import build_market_comparisons

    source = cfg.reference_market.capitalize()
    st.info(_("This election has no poll model: the probabilities come from {source}. A signal means a "
              "bookmaker pays more than the {source} price implies.", source=source))
    with ctx.session() as session:
        quotes = load_quotes(session, cfg.id)
    comparisons = build_market_comparisons(cfg, quotes, ctx.settings)
    if not comparisons:
        st.info(_("No prices from {source} yet - they are downloaded every hour.", source=source))
        return
    c1, c2 = st.columns(2)
    c1.metric(_("Days to the election"), (cfg.election_date - date.today()).days)
    c2.metric(_("Election date"), cfg.election_date.strftime(_("%Y-%m-%d")))
    for comp in comparisons:
        st.subheader(market_label(cfg, comp.market))
        rows = [{
            _("Outcome"): outcome_label(cfg, o.outcome),
            _("Probability ({source})", source=source): o.p_model,
            _("Best odds"): o.best_odds,
            _("Bookmaker"): o.best_bookmaker,
        } for o in sorted(comp.outcomes, key=lambda o: -o.p_model) if o.in_model][:20]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            _("Probability ({source})", source=source): st.column_config.ProgressColumn(
                format="percent", min_value=0.0, max_value=1.0),
            _("Best odds"): ODDS,
        })


def _poll_chart(cfg, sim) -> None:
    ctx = context()
    with ctx.session() as session:
        polls = [p for p in load_polls(session, cfg.id) if p.round == "first"]
    if not polls:
        return
    top = [sim.candidates[j] for j in np.argsort(-sim.average.mean) if sim.candidates[j] != OTHER][:charts.MAX_SERIES]
    colors = charts.series_colors(list(cfg.candidates), top)
    records = [
        {"date": pd.Timestamp(p.end_date), "label": cfg.candidate_name(c), "share": 100 * v / sum(p.shares.values()),
         "pollster": p.pollster}
        for p in polls for c, v in p.shares.items() if c in colors
    ]
    if not records:
        return
    df = pd.DataFrame(records)
    named = {cfg.candidate_name(c): col for c, col in colors.items()}
    st.subheader(_("Poll trend"))
    chart = charts.poll_trend(df, named, _("Fieldwork end"), _("Share (%)"), _("Candidate / party"))
    st.altair_chart(chart, width="stretch", theme="streamlit")
    with st.expander(_("Polls as a table")):
        wide = df.pivot_table(index=["date", "pollster"], columns="label", values="share").reset_index()
        st.dataframe(wide.sort_values("date", ascending=False), hide_index=True, width="stretch")


def _custom_market(cfg, sim) -> None:
    with st.expander(_("Probability of another market")):
        spec = _market_picker(cfg, sim.candidates, key="custom")
        if spec is None:
            return
        try:
            probs = market_probabilities(sim, spec)
        except (KeyError, ValueError) as exc:
            _error(exc)
            return
        df = pd.DataFrame([{_("Outcome"): outcome_label(cfg, o), _("Probability"): p} for o, p in probs.items()])
        st.dataframe(df.sort_values(_("Probability"), ascending=False).head(12), hide_index=True,
                     width="stretch", column_config={_("Probability"): PCT})


def _market_picker(cfg, candidates: list[str], key: str) -> MarketSpec | None:
    kinds = sorted(MARKET_TYPES, key=lambda k: list(MARKET_TYPES).index(k))
    if cfg.type != "two_round":
        kinds = [k for k in kinds if k not in ("finalists", "qualify_runoff", "first_round_winner")]
    names = {cid: cfg.candidate_name(cid) for cid in candidates if cid != OTHER}
    c1, c2, c3 = st.columns(3)
    kind = c1.selectbox(_("Market type"), kinds, key=f"{key}_kind",
                        format_func=lambda k: MarketSpec(k, "X", 5.0).describe({"X": "…"}))
    candidate = value = None
    if kind in ("qualify_runoff", "threshold", "share_over"):
        if not names:
            return None
        candidate = c2.selectbox(_("Candidate / party"), list(names), format_func=names.get, key=f"{key}_cand")
    if kind in ("threshold", "share_over"):
        value = c3.number_input(_("Share (%)"), min_value=0.5, max_value=99.5, value=5.0 if kind == "threshold" else 30.0,
                                step=0.5, key=f"{key}_value")
    return MarketSpec(kind, candidate, value)


# ---------------------------------------------------------------- signals

def signals_page() -> None:
    from polledge.journal.paper import record_signals, record_snapshot
    from polledge.services import (
        build_comparisons, build_market_comparisons, fetch_odds, market_fair_probs, params_payload,
    )

    ctx = context()
    cfg = current_election()
    st.title(_("Signals"))
    st.caption(cfg.name)

    if cfg.bookmakers:
        with st.sidebar:
            if st.button(_("Fetch odds from bookmakers"), width="stretch"):
                fetcher = ctx.fetcher()
                try:
                    with st.spinner(_("Downloading odds...")), ctx.session() as session:
                        reports = fetch_odds(session, fetcher, cfg)
                finally:
                    fetcher.close()
                for r in reports:
                    if r.error:
                        st.error(f"{r.bookmaker}: {r.error}")
                    else:
                        st.success(_("{election} – {bookmaker}: {count} odds, {inserted} new", election=cfg.name,
                                     bookmaker=r.bookmaker, count=r.quotes, inserted=r.inserted))

    with ctx.session() as session:
        quotes = load_quotes(session, cfg.id)
    _sources_status(cfg, quotes)
    if not quotes:
        if cfg.bookmakers:
            st.info(_("There are no odds for this election yet. They are downloaded automatically every hour "
                      "as soon as a configured source lists the election; you can also enter them on the Odds page."))
        else:
            st.info(_("There are no odds for this election yet. Enter them on the Odds page."))
        return
    if cfg.uses_polls:
        try:
            sim, _probs = model(cfg)
        except PollEdgeError as exc:
            _error(exc)
            return
        comparisons = build_comparisons(sim, quotes, ctx.settings)
        as_of = sim.average.as_of
    else:
        st.info(_("This election has no poll model: the probabilities come from {source}. A signal means a "
                  "bookmaker pays more than the {source} price implies.", source=cfg.reference_market.capitalize()))
        comparisons = build_market_comparisons(cfg, quotes, ctx.settings)
        as_of = date.today()
    sig = ctx.settings.signals

    n_signals = sum(len(c.signals) for c in comparisons)
    c1, c2, c3 = st.columns(3)
    c1.metric(_("Signals"), n_signals)
    c2.metric(_("Arbitrages"), sum(c.is_arbitrage for c in comparisons))
    c3.metric(_("Bankroll"), money(sig.bankroll))
    st.caption(_("Edge threshold {threshold} · Kelly {kelly} · bankroll {bankroll}",
                 threshold=f"{sig.edge_threshold:.0%}", kelly=sig.kelly_fraction, bankroll=money(sig.bankroll)))
    only = st.toggle(_("Show only rows with a signal"), value=False)

    reference_names = ", ".join(sig.reference_bookmakers)
    for comp in comparisons:
        has_reference = any(o.p_reference is not None for o in comp.outcomes)
        rows = []
        ordered = sorted(comp.outcomes, key=lambda o: (not o.in_model, -o.p_model, -(o.p_reference or 0)))
        for o in ordered:
            if (o.best_odds is None and o.p_reference is None) or (only and not o.is_signal):
                continue
            row = {
                _("Outcome"): outcome_label(cfg, o.outcome),
                _("Model"): o.p_model if o.in_model else None,
                _("Market (no margin)"): o.p_market,
            }
            if has_reference:
                row[_("Reference ({sources})", sources=reference_names)] = o.p_reference
            row.update({
                _("Best odds"): o.best_odds,
                _("Bookmaker"): o.best_bookmaker,
                _("Edge"): o.edge,
                _("Stake"): money(o.stake) if o.stake > 0 else "",
                _("Signal"): ("✅ " + _("SIGNAL") if o.is_signal else _("stake below minimum") if o.below_min_stake
                              else "" if o.in_model else _("not in model")),
            })
            rows.append(row)
        if not rows:
            continue
        st.subheader(market_label(cfg, comp.market))
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            _("Model"): PCT, _("Market (no margin)"): PCT, _("Edge"): st.column_config.NumberColumn(format="percent"),
            _("Reference ({sources})", sources=reference_names): PCT, _("Best odds"): ODDS,
        })
        if has_reference and not comp.margins:
            st.caption(_("Only reference prices so far - signals and stakes appear once a bookmaker you can bet "
                         "with lists this market."))
        notes = [_("Margins: {margins}", margins=", ".join(f"{bm} {m:.1%}" for bm, m in comp.margins.items()))]
        if comp.arbitrage_sum is not None:
            if comp.is_arbitrage:
                notes.append("**" + _("ARBITRAGE:") + "** " + _("sum of 1/best odds = {value} < 1",
                                                                  value=f"{comp.arbitrage_sum:.4f}"))
            else:
                notes.append(_("Sum of 1/best odds: {value} (no arbitrage)", value=f"{comp.arbitrage_sum:.4f}"))
        st.caption(" · ".join(notes))

    if st.button(_("Save the signals as virtual bets in the journal"), type="primary", disabled=n_signals == 0):
        with ctx.session() as session:
            created = record_signals(session, cfg.id, comparisons)
            probs = {c.market: {o.outcome: o.p_model for o in c.outcomes if o.in_model} for c in comparisons}
            record_snapshot(session, cfg.id, probs, params_payload(model_params_for(ctx.settings, cfg), as_of),
                            market_fair_probs(comparisons))
        st.success(_("Saved {count} new virtual bets to the journal.", count=len(created)))
    analysis_only()


def _sources_status(cfg, quotes: list[OddsQuote]) -> None:
    """Which automatic sources are configured for the election and when they last delivered odds."""
    from polledge.services import skipped_sources

    if not cfg.bookmakers:
        return
    reference = {n.lower() for n in context().settings.signals.reference_bookmakers}
    latest: dict[str, tuple[datetime, int]] = {}
    for q in quotes:
        when, count = latest.get(q.bookmaker.lower(), (q.timestamp, 0))
        latest[q.bookmaker.lower()] = (max(when, q.timestamp), count + 1)
    rows = []
    for name, book in cfg.bookmakers.items():
        when, count = latest.get(name.lower(), (None, 0))
        home_only = name.lower() in skipped_sources()
        if count:
            state = _("{count} odds, latest {time}", count=count, time=to_local(when).strftime(_("%Y-%m-%d %H:%M")))
            if home_only:
                state += " · " + _("downloaded from the home network (NAS)")
        elif home_only:
            state = _("downloaded from the home network (NAS) - no odds yet")
        elif book.discover and not book.pages:
            state = _("does not list this election yet (searching for: {keywords})", keywords=", ".join(book.discover))
        else:
            state = _("no odds downloaded yet")
        rows.append({_("Source"): name.capitalize(),
                     _("Type"): _("market reference") if name.lower() in reference else _("bookmaker"),
                     _("Status"): state})
    with st.expander(_("Automatic odds sources (downloaded every hour)"), expanded=not quotes):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# ---------------------------------------------------------------- odds

def odds_page() -> None:
    ctx = context()
    cfg = current_election()
    st.title(_("Betting odds"))
    st.caption(cfg.name)
    tab_enter, tab_import, tab_stored = st.tabs([_("Enter odds"), _("Import file"), _("Stored odds")])

    with tab_enter:
        _enter_odds(ctx, cfg)
    with tab_import:
        _import_odds(ctx, cfg)
    with tab_stored:
        _stored_odds(ctx, cfg)


def _enter_odds(ctx, cfg) -> None:
    candidates = list(cfg.candidates)
    spec = _market_picker(cfg, candidates, key="enter")
    if spec is None:
        return
    c1, c2, c3 = st.columns(3)
    bookmaker = c1.text_input(_("Bookmaker"), value="Tipsport", key="enter_bm")
    day = c2.date_input(_("Date"), value=datetime.now().date(), key="enter_date")
    at = c3.time_input(_("Time"), value=time(datetime.now().hour, datetime.now().minute), key="enter_time")

    if spec.kind in BINARY_OUTCOMES:
        outcomes = list(BINARY_OUTCOMES[spec.kind])
    elif spec.kind == "finalists":
        outcomes = []
    else:
        outcomes = candidates
    editor_df = pd.DataFrame({"outcome": outcomes, _("Outcome"): [outcome_label(cfg, o) for o in outcomes],
                              _("Odds"): [None] * len(outcomes)})
    if spec.kind == "finalists":
        st.caption(_("Enter the pair as ID|ID, e.g. LE_PEN|PHILIPPE."))
        editor_df = pd.DataFrame({"outcome": [""], _("Outcome"): [""], _("Odds"): [None]})
    edited = st.data_editor(
        editor_df, hide_index=True, width="stretch", key=f"editor_{spec.id}",
        num_rows="dynamic" if spec.kind == "finalists" else "fixed",
        disabled=[_("Outcome")] if spec.kind != "finalists" else [],
        column_config={"outcome": None if spec.kind != "finalists" else st.column_config.TextColumn(_("Pair (ID|ID)")),
                       _("Odds"): st.column_config.NumberColumn(min_value=1.01, step=0.01, format="%.2f")},
    )
    if st.button(_("Save odds"), type="primary"):
        timestamp = datetime.combine(day, at)
        quotes, errors = [], []
        for i, row in edited.iterrows():
            odds = row[_("Odds")]
            if odds is None or pd.isna(odds):
                continue
            record = {"election_id": cfg.id, "bookmaker": bookmaker.strip(), "market": spec.id,
                      "outcome": row["outcome"], "odds": odds, "timestamp": timestamp}
            try:
                quotes.append(quote_from_record(record, _("row {n}", n=i + 1)))
            except ValueError as exc:
                errors.append(str(exc))
        for err in errors:
            st.error(err)
        if quotes and bookmaker.strip():
            with ctx.session() as session:
                inserted, updated = store_quotes(session, quotes, "web")
            st.success(_("{count} odds read, {inserted} new, {updated} updated.", count=len(quotes),
                         inserted=inserted, updated=updated))
        elif not errors:
            st.warning(_("Fill in the bookmaker and at least one odds value."))


def _import_odds(ctx, cfg) -> None:
    st.caption(_("CSV with columns election_id, bookmaker, market, outcome, odds, timestamp - or YAML, see README."))
    upload = st.file_uploader(_("Odds file (.csv or .yaml)"), type=["csv", "yaml", "yml"])
    if upload is None or not st.button(_("Import")):
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / upload.name
        path.write_bytes(upload.getvalue())
        try:
            source = source_for_path(path)
            quotes = source.fetch(cfg.id)
        except PollEdgeError as exc:
            _error(exc)
            return
    for err in source.errors:
        st.warning(err)
    with ctx.session() as session:
        inserted, updated = store_quotes(session, quotes, source.name)
    st.success(_("{count} odds read, {inserted} new, {updated} updated.", count=len(quotes),
                 inserted=inserted, updated=updated))


def _stored_odds(ctx, cfg) -> None:
    with ctx.session() as session:
        quotes = load_quotes(session, cfg.id)
        rows = quote_rows(session, cfg.id)
    if not rows:
        st.info(_("No odds stored yet."))
        return
    st.subheader(_("Bookmaker margins (latest odds)"))
    books = [{_("Bookmaker"): b.bookmaker, _("Market"): market_label(cfg, b.market), _("Outcomes"): len(b.odds),
              _("Margin"): b.margin, _("Time"): to_local(b.timestamp), _("Complete"): b.complete}
             for b in latest_books(quotes)]
    st.dataframe(pd.DataFrame(books), hide_index=True, width="stretch", column_config={_("Margin"): PCT})

    st.subheader(_("All stored odds"))
    df = pd.DataFrame([{"id": r.id, _("Time"): to_local(r.timestamp), _("Bookmaker"): r.bookmaker,
                        _("Market"): market_label(cfg, r.market), _("Outcome"): outcome_label(cfg, r.outcome),
                        _("Odds"): r.odds, _("Source"): r.source} for r in rows])
    selection = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                             selection_mode="multi-row", column_config={"id": None, _("Odds"): ODDS}, key="odds_table")
    selected = [int(df.iloc[i]["id"]) for i in selection.selection.rows]
    if st.button(_("Delete selected odds ({count})", count=len(selected)), disabled=not selected):
        with ctx.session() as session:
            n = delete_quotes(session, selected)
        st.success(_("Deleted {count} odds.", count=n))
        st.rerun()


# ---------------------------------------------------------------- journal

def journal_page() -> None:
    from polledge.journal.paper import settle

    ctx = context()
    cfg = current_election()
    st.title(_("Journal"))
    st.caption(cfg.name)
    with ctx.session() as session:
        bets = session.scalars(
            select(PaperBetRow).where(PaperBetRow.election_id == cfg.id).order_by(PaperBetRow.created_at.desc())
        ).all()
    if not bets:
        st.info(_("The journal is empty."))
    else:
        settled = [b for b in bets if b.status in ("won", "lost")]
        staked = sum(b.stake for b in settled)
        profit = sum(b.profit or 0 for b in settled)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric(_("Bets in total"), len(bets))
        c2.metric(_("Open bets"), sum(b.status == "open" for b in bets))
        c3.metric(_("Profit / loss"), money(profit))
        c4.metric("ROI", f"{profit / staked:.1%}" if staked else "–")
        status = {"open": _("open"), "won": _("won"), "lost": _("lost"), "void": _("void")}
        st.dataframe(pd.DataFrame([{
            _("Time"): to_local(b.created_at), _("Market"): market_label(cfg, b.market), _("Outcome"): outcome_label(cfg, b.outcome),
            _("Bookmaker"): b.bookmaker, _("Odds"): b.odds, _("Model"): b.p_model, _("Edge"): b.edge,
            _("Stake"): money(b.stake), _("Status"): status.get(b.status, b.status),
            _("Profit"): money(b.profit) if b.profit is not None else "",
        } for b in bets]), hide_index=True, width="stretch",
            column_config={_("Model"): PCT, _("Edge"): PCT, _("Odds"): ODDS})

    with st.expander(_("Enter the election result and settle the journal"), expanded=False):
        prefill = cfg.result or ElectionResult()
        df = pd.DataFrame({
            "id": list(cfg.candidates),
            _("Candidate / party"): [c.name for c in cfg.candidates.values()],
            _("First round (%)"): [prefill.first_round.get(c) for c in cfg.candidates],
            _("Runoff (%)"): [prefill.runoff.get(c) for c in cfg.candidates],
        })
        column_config = {"id": None, _("First round (%)"): st.column_config.NumberColumn(min_value=0.0, max_value=100.0),
                         _("Runoff (%)"): st.column_config.NumberColumn(min_value=0.0, max_value=100.0)}
        if cfg.type != "two_round":
            column_config[_("Runoff (%)")] = None
        edited = st.data_editor(df, hide_index=True, width="stretch", disabled=[_("Candidate / party")],
                                column_config=column_config, key="result_editor")
        if st.button(_("Settle"), type="primary"):
            first = {r["id"]: float(r[_("First round (%)")]) for _i, r in edited.iterrows()
                     if pd.notna(r[_("First round (%)")])}
            runoff = {r["id"]: float(r[_("Runoff (%)")]) for _i, r in edited.iterrows()
                      if cfg.type == "two_round" and pd.notna(r[_("Runoff (%)")])}
            if not first:
                st.error(_("Pass the result with --result file.yaml (keys first_round and optionally runoff)"))
                return
            if runoff and len(runoff) != 2:
                st.error(_("The runoff result needs exactly two candidates."))
                return
            with ctx.session() as session:
                report = settle(session, cfg.id, ElectionResult(first_round=first, runoff=runoff))
            c1, c2, c3 = st.columns(3)
            c1.metric(_("Won / lost / void"), f"{report.n_won} / {report.n_lost} / {report.n_void}")
            c2.metric(_("Profit / loss"), money(report.profit))
            c3.metric("ROI", f"{report.roi:.1%}" if report.roi is not None else "–")
            st.write(pd.DataFrame([
                {_("Score"): _("Model Brier score (rows with odds)"), _("Value"): report.brier_model_same_rows},
                {_("Score"): _("Market Brier score (no margin)"), _("Value"): report.brier_market},
                {_("Score"): _("Model Brier score (all snapshots)"), _("Value"): report.brier_model},
            ]))


# ---------------------------------------------------------------- calibration

def calibration_page() -> None:
    from polledge.calibration.backtest import evaluate_election

    ctx = context()
    st.title(_("Calibration and backtest"))
    history = elections(ctx, history=True)
    calibrated = ctx.settings.model != ctx.settings.uncalibrated_model
    base = ctx.settings.uncalibrated_model or ctx.settings.model
    m = ctx.settings.model
    st.write(_("Current model parameters ({source}):", source=_("calibrated") if calibrated else "settings.yaml"))
    st.dataframe(pd.DataFrame([
        {_("Parameter"): k, _("Value"): getattr(m, k), "settings.yaml": getattr(base, k)}
        for k in ("half_life_days", "house_effects", "house_min_polls", "sigma_sys_pp",
                  "sigma_horizon_pp_per_sqrt_day", "sigma_runoff_pp")
    ]), hide_index=True)

    if st.button(_("Download polls of past elections")):
        from polledge.services import update_polls

        wiki = ctx.wiki_client()
        try:
            for cfg in history.values():
                with st.spinner(cfg.name), ctx.session() as session:
                    parsed, inserted, _updated = update_polls(session, wiki, cfg)
                st.success(_("{election}: {parsed} polls, {inserted} new", election=cfg.name, parsed=parsed,
                             inserted=inserted))
        except PollEdgeError as exc:
            _error(exc)
        finally:
            wiki.close()

    st.subheader(_("Backtest"))
    if not history:
        st.info(_("There are no past elections with a result (config/elections/history/*.yaml)"))
        return
    eid = st.selectbox(_("Past election with a result"), list(history), format_func=lambda k: history[k].name)
    cfg = history[eid]
    with ctx.session() as session:
        polls = load_polls(session, eid)
    if not polls:
        st.info(_("There are no polls for election {id} in the database. Run `polledge update-polls -e {id}`.", id=eid))
    else:
        with st.spinner(_("Computing...")):
            scores = evaluate_election(cfg, polls, model_params_for(ctx.settings, cfg))
        st.dataframe(pd.DataFrame([{
            _("Days before"): s.horizon, _("Polls"): s.n_polls, _("MAE of average (pp)"): s.mae_pp,
            "CRPS (pp)": s.crps_pp, _("80% coverage"): s.coverage80, _("P(actual winner)"): s.p_actual_winner,
            _("Head-to-head error (pp)"): s.runoff_error_pp,
        } for s in scores]), hide_index=True, width="stretch", column_config={
            _("80% coverage"): PCT, _("P(actual winner)"): PCT,
            _("MAE of average (pp)"): st.column_config.NumberColumn(format="%.2f"),
            "CRPS (pp)": st.column_config.NumberColumn(format="%.2f"),
            _("Head-to-head error (pp)"): st.column_config.NumberColumn(format="%+.1f"),
        })

    st.subheader(_("Calibration"))
    st.caption(_("Grid search over the model parameters on all past elections, validated leave-one-election-out. "
                 "Takes a few minutes."))
    sims = st.select_slider(_("Simulations per grid point"), options=[500, 1000, 2000], value=1000)
    if st.button(_("Run calibration")):
        _run_calibration(ctx, history, base, sims)
    if "calibration" in st.session_state:
        _show_calibration(ctx, st.session_state["calibration"])
    if calibrated and st.button(_("Delete the calibrated parameters; the model uses settings.yaml again.")):
        from polledge.services import delete_calibration

        with ctx.session() as session:
            delete_calibration(session)
        reload_context()
        st.rerun()


def _run_calibration(ctx, history, base, sims) -> None:
    from polledge.calibration.tune import calibration_payload, tune

    data = []
    with ctx.session() as session:
        for cfg in history.values():
            polls = load_polls(session, cfg.id)
            if polls and cfg.result is not None:
                data.append((cfg, polls))
    if not data:
        st.error(_("There are no past elections with a result (config/elections/history/*.yaml)"))
        return
    with st.spinner(_("Grid search in progress...")):
        res = tune(data, base, n_sims=sims)
    st.session_state["calibration"] = (res, calibration_payload(res, base, [c.id for c, _p in data]))


def _show_calibration(ctx, stored) -> None:
    from polledge.services import save_calibration

    res, payload = stored
    st.dataframe(pd.DataFrame([{
        _("Half-life (days)"): combo["half_life_days"],
        _("House effects (min. polls)"): _("off") if combo["house"] is None else combo["house"],
        _("σ systematic"): combo["sigma_sys_pp"], _("σ horizon / √day"): combo["sigma_horizon_pp_per_sqrt_day"],
        "CRPS": score,
    } for combo, score in res.table[:8]]), hide_index=True, width="stretch")
    st.dataframe(pd.DataFrame([{_("Election"): e, _("CRPS tuned"): r["tuned"], _("CRPS default"): r["default"]}
                               for e, r in res.loeo.items()]), hide_index=True)
    st.write(_("Default parameters: CRPS {default} -> tuned: {tuned}", default=f"{res.default_score:.3f}",
               tuned=f"{res.best_score:.3f}"))
    st.write(_("Coverage of 80% intervals with the best parameters: {value} (ideal ~80 %)", value=f"{res.coverage80:.0%}"))
    if st.button(_("Save the best parameters to the database"), type="primary"):
        with ctx.session() as session:
            save_calibration(session, payload)
        del st.session_state["calibration"]
        reload_context()
        st.success(_("Calibrated parameters saved to the database:") + f" {payload['model']}")
