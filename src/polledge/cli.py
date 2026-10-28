"""Command-line interface. All texts are English; translations come from polledge/locales."""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import numpy as np
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from polledge import i18n
from polledge.config import (
    OTHER,
    ElectionConfig,
    ElectionResult,
    Settings,
    discover_elections,
    get_election,
    load_settings,
    model_params_for,
    read_yaml,
)
from sqlalchemy.exc import SQLAlchemyError

from polledge.db import PaperBetRow
from polledge.services import AppContext, open_app
from polledge.errors import PollEdgeError
from polledge.i18n import _, decimal
from polledge.logging_setup import setup_logging
from polledge.model.simulate import SimulationResult
from polledge.presentation import market_label, outcome_label
from polledge.timeutil import to_local, utcnow
from polledge.odds.file_source import source_for_path
from polledge.odds.repository import load_quotes, store_quotes
from polledge.polls.repository import load_polls
from polledge.polls.wiki_client import WikiClient

for _stream in (sys.stdout, sys.stderr):
    # Windows consoles/pipes may default to a legacy code page; never crash on output.
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

# Help texts are built at import time, before settings.yaml is read: use the env var (default Czech).
i18n.set_language(os.environ.get("POLLEDGE_LANG", "cs"))

app = typer.Typer(
    help=_("PollEdge - compares a poll-based model with bookmaker odds. "
           "The application only analyses and recommends; it never places bets."),
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
log = logging.getLogger("polledge")

DEFAULT_HORIZONS = "90,60,30,14,7,1"


_state: AppContext | None = None


def state() -> AppContext:
    assert _state is not None
    return _state


def print_error(exc: Exception) -> None:
    console.print(f"[red]{_('Error:')}[/red] {exc}")


@app.callback()
def main(
    config: Annotated[Path, typer.Option("--config", "-c", help=_("Path to settings.yaml"))] = Path("config/settings.yaml"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help=_("Verbose logging"))] = False,
) -> None:
    global _state
    try:
        settings = load_settings(config)
        i18n.set_language(settings.language)
        setup_logging(settings.resolve(settings.log_dir) if settings.log_dir else None, verbose)
        _state = open_app(config)
    except PollEdgeError as exc:
        print_error(exc)
        raise typer.Exit(1) from None
    except SQLAlchemyError as exc:
        print_error(_("Cannot open the database: {error}", error=exc.__class__.__name__ + ": " + str(exc).splitlines()[0]))
        raise typer.Exit(1) from None


def handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except PollEdgeError as exc:
            print_error(exc)
            raise typer.Exit(1) from None

    return wrapper


# ---------- formatting helpers ----------

DASH = "–"


def pct(p: float | None, digits: int = 1) -> str:
    return DASH if p is None else decimal(f"{p * 100:.{digits}f} %")


def num(x: float | None, digits: int = 2) -> str:
    return DASH if x is None else decimal(f"{x:.{digits}f}")


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + f" {state().settings.signals.currency}"


def signed_pct(p: float | None) -> str:
    if p is None:
        return DASH
    color = "green" if p > 0 else "red"
    return f"[{color}]{decimal(f'{p * 100:+.1f} %')}[/{color}]"


def fmt_date(value) -> str:
    return value.strftime(_("%Y-%m-%d"))


def ok(text: str) -> None:
    console.print(f"[green]✓[/green] {text}")


def fail(text: str, exc: object) -> None:
    console.print(f"[red]✗ {text}:[/red] {exc}")


def analysis_only() -> None:
    console.print("[dim]" + _("Analysis only - the application never places bets.") + "[/dim]")


# ---------- polls ----------

@app.command("update-polls", help=_("Download polls from Wikipedia for the configured elections and store them."))
@handle_errors
def update_polls_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help=_("Election id (repeatable)"))] = None,
    history: Annotated[bool, typer.Option("--history", help=_("Also download past elections used for calibration"))] = False,
    force: Annotated[bool, typer.Option("--force", help=_("Ignore the cache and download again"))] = False,
) -> None:
    from polledge.services import update_polls

    s = state().settings
    configured = discover_elections(s, include_history=True)
    current = discover_elections(s, include_history=False)
    ids = election or (sorted(configured) if history else sorted(current))
    ids = [i for i in ids if i not in configured or configured[i].uses_polls]  # market-only elections have no polls
    if not ids:
        raise PollEdgeError(_("No elections are configured (config/elections/*.yaml)"))
    client = state().wiki_client()
    failed = 0
    try:
        for eid in ids:
            cfg = get_election(s, eid)
            try:
                with state().session() as session:
                    parsed, inserted, updated = update_polls(session, client, cfg, force=force)
            except PollEdgeError as exc:
                failed += 1
                fail(cfg.name, exc)
                continue
            ok(_("{election}: {parsed} polls read, {inserted} new, {updated} updated",
                 election=cfg.name, parsed=parsed, inserted=inserted, updated=updated))
    finally:
        client.close()
    if failed:
        raise typer.Exit(1)


# ---------- model ----------

@app.command("model", help=_("Compute and print the model probabilities."))
@handle_errors
def model_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help=_("Election id"))],
    runs: Annotated[Optional[int], typer.Option("--runs", "-n", help=_("Number of simulations"))] = None,
    as_of: Annotated[Optional[datetime], typer.Option("--as-of", help=_("Compute the model as of this date"), formats=["%Y-%m-%d"])] = None,
    market: Annotated[Optional[list[str]], typer.Option("--market", "-m", help=_("Extra market, e.g. share_over:LE_PEN:30"))] = None,
    save: Annotated[bool, typer.Option("--save", help=_("Save the probabilities to the journal (for the Brier score)"))] = False,
) -> None:
    from polledge.journal.paper import record_snapshot
    from polledge.services import model_markets, params_payload, probabilities_for, run_model_from_db

    s = state().settings
    cfg = get_election(s, election)
    if not cfg.uses_polls:
        raise PollEdgeError(_("{election} has no poll model - its probabilities come from {source}. "
                              "Use `polledge signals -e {id}`.", election=cfg.name,
                              source=cfg.reference_market.capitalize(), id=cfg.id))
    with state().session() as session:
        sim, params = run_model_from_db(session, s, cfg, as_of.date() if as_of else None, runs)
        specs = model_markets(sim, cfg, market)
        probs = probabilities_for(sim, specs)
        print_model(cfg, sim, probs)
        if save:
            n = record_snapshot(session, cfg.id, probs, params_payload(params, sim.average.as_of))
            console.print("[green]" + _("Saved {count} probabilities to the journal.", count=n) + "[/green]")


def print_model(cfg: ElectionConfig, sim: SimulationResult, probs: dict[str, dict[str, float]]) -> None:
    avg = sim.average
    days = (cfg.election_date - avg.as_of).days
    header = _(
        "As of {date} · {days} days to the election · polls in the average: {polls} · "
        "effective sample: {n_eff} · simulations: {sims}",
        date=fmt_date(avg.as_of), days=days, polls=avg.n_polls,
        n_eff=f"{avg.n_eff:,.0f}".replace(",", " "), sims=f"{sim.n_sims:,}".replace(",", " "),
    )
    console.print(Panel.fit(f"[bold]{cfg.name}[/bold]\n{header}"))
    two_round = cfg.type == "two_round"
    winner = probs.get("winner", {})
    t = Table(title=_("First-round estimate") if two_round else _("Result estimate"))
    t.add_column(_("Candidate / party"))
    t.add_column(_("Average"), justify="right")
    t.add_column(_("80% interval"), justify="right")
    t.add_column(_("± (pp)"), justify="right")
    if two_round:
        t.add_column(_("Qualifies for runoff"), justify="right")
    t.add_column(_("Wins"), justify="right")
    if not two_round:
        t.add_column("≥ 5 %", justify="right")
    lo, hi = np.quantile(sim.shares, [0.1, 0.9], axis=0)
    sd = sim.shares.std(axis=0) * 100
    order = np.argsort(-avg.mean)
    for j in order:
        cid = sim.candidates[j]
        is_other = cid == OTHER
        row = [cfg.candidate_name(cid), pct(avg.mean[j]), f"{pct(lo[j])} – {pct(hi[j])}", num(sd[j], 1)]
        if two_round:
            row.append("" if is_other else pct(probs.get(f"qualify_runoff:{cid}", {}).get("yes")))
        row.append("" if is_other else pct(winner.get(cid)))
        if not two_round:
            row.append("" if is_other else pct(probs.get(f"threshold:{cid}:5", {}).get("yes")))
        t.add_row(*row)
    console.print(t)

    if two_round and sim.finalists is not None:
        from polledge.model.markets import MarketSpec, market_probabilities

        pairs = market_probabilities(sim, MarketSpec("finalists"))
        pt = Table(title=_("Most likely runoff pairs"))
        pt.add_column(_("Pair"))
        pt.add_column(_("Probability"), justify="right")
        pt.add_column(_("Runoff estimate source"))
        source = {
            "polls": _("head-to-head polls"),
            "fallback": "[yellow]" + _("first-round ratio (no head-to-head polls)") + "[/yellow]",
        }
        for pair, p in sorted(pairs.items(), key=lambda kv: -kv[1])[:6]:
            pt.add_row(outcome_label(cfg, pair), pct(p), source.get(sim.runoff_sources.get(pair, ""), ""))
        console.print(pt)

    extra = [m for m in probs if m.split(":")[0] in ("share_over", "threshold") and not m.endswith(":5")]
    for m in extra:
        values = ", ".join(f"{outcome_label(cfg, o)} {pct(p)}" for o, p in probs[m].items())
        console.print(f"{market_label(cfg, m)}: {values}")

    if avg.house_effects:
        ht = Table(title=_("House effects (pollster deviation from the average, pp)"))
        ht.add_column(_("Pollster"))
        top = [sim.candidates[j] for j in order[:5] if sim.candidates[j] != OTHER]
        for cid in top:
            ht.add_column(cfg.candidate_name(cid), justify="right")
        for pollster, eff in sorted(avg.house_effects.items()):
            ht.add_row(pollster, *[decimal(f"{eff.get(c, 0.0):+.1f}") for c in top])
        console.print(ht)


# ---------- odds ----------

@app.command("import-odds", help=_("Import manually entered odds from a file, print margins and store the odds."))
@handle_errors
def import_odds_cmd(
    path: Annotated[Path, typer.Argument(help=_("Odds file (.csv or .yaml)"))],
    election: Annotated[Optional[str], typer.Option("--election", "-e", help=_("Import only this election"))] = None,
) -> None:
    source = source_for_path(path)
    quotes = source.fetch(election)
    if source.errors:
        console.print("[yellow]" + _("Skipped {count} invalid records:", count=len(source.errors)) + "[/yellow]")
        for err in source.errors:
            console.print(f"  [yellow]•[/yellow] {err}")
    known = discover_elections(state().settings, include_history=True)
    unknown = sorted({q.election_id for q in quotes} - set(known))
    if unknown:
        console.print("[yellow]" + _("Warning: odds for elections that are not configured: {ids}",
                                     ids=", ".join(unknown)) + "[/yellow]")
    with state().session() as session:
        inserted, updated = store_quotes(session, quotes, source.name)
    ok(_("{count} odds read, {inserted} new, {updated} updated.", count=len(quotes), inserted=inserted, updated=updated))
    print_margins(quotes, known)


def print_margins(quotes, known: dict[str, ElectionConfig]) -> None:
    from polledge.odds.margin import latest_books

    t = Table(title=_("Bookmaker margins (latest odds)"))
    right = {_("Outcomes"), _("Sum of 1/odds"), _("Margin")}
    for col in (_("Election"), _("Bookmaker"), _("Market"), _("Outcomes"), _("Sum of 1/odds"), _("Margin")):
        t.add_column(col, justify="right" if col in right else "left")
    for b in latest_books(quotes):
        cfg = known.get(b.election_id)
        label = market_label(cfg, b.market) if cfg else b.market
        note = "" if b.complete else " [yellow](" + _("incomplete market") + ")[/yellow]"
        t.add_row(b.election_id, b.bookmaker, label + note, str(len(b.odds)), num(1 + b.margin, 3), pct(b.margin))
    console.print(t)


def _elections_with_bookmakers(ids: list[str] | None) -> list[ElectionConfig]:
    s = state().settings
    if ids:
        cfgs = [get_election(s, i) for i in ids]
    else:
        cfgs = [c for c in discover_elections(s, include_history=False).values() if c.bookmakers]
    if not cfgs:
        raise PollEdgeError(_("No election has bookmakers configured (section 'bookmakers' in the election YAML)"))
    return cfgs


def _make_fetcher():
    return state().fetcher()


def _print_fetch_reports(cfg: ElectionConfig, reports) -> int:
    failed = 0
    if not reports:
        console.print("[yellow]" + _("{election}: no bookmaker configured", election=cfg.name) + "[/yellow]")
    for r in reports:
        if r.error:
            failed += 1
            fail(f"{cfg.name} – {r.bookmaker}", r.error)
        else:
            ok(_("{election} – {bookmaker}: {count} odds, {inserted} new",
                 election=cfg.name, bookmaker=r.bookmaker, count=r.quotes, inserted=r.inserted))
    return failed


@app.command("fetch-odds", help=_("Download current odds from the public pages of the configured bookmakers."))
@handle_errors
def fetch_odds_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help=_("Election id (default: all with bookmakers)"))] = None,
    bookmaker: Annotated[Optional[list[str]], typer.Option("--bookmaker", "-b", help=_("Only this bookmaker"))] = None,
    force: Annotated[bool, typer.Option("--force", help=_("Ignore the page cache"))] = False,
) -> None:
    from polledge.services import fetch_odds

    fetcher = _make_fetcher()
    failed = 0
    try:
        for cfg in _elections_with_bookmakers(election):
            with state().session() as session:
                failed += _print_fetch_reports(cfg, fetch_odds(session, fetcher, cfg, bookmaker, force))
    finally:
        fetcher.close()
    if failed:
        raise typer.Exit(1)


@app.command("watch", help=_("One monitoring run: polls + odds -> model -> new signals to the journal and "
                             "notifications. Meant to be scheduled (Windows Task Scheduler, cron)."))
@handle_errors
def watch_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help=_("Election id (default: all with bookmakers)"))] = None,
    no_polls: Annotated[bool, typer.Option("--no-polls", help=_("Do not download polls, only odds"))] = False,
    every: Annotated[Optional[int], typer.Option("--every", help=_("Repeat every N seconds and keep running (e.g. in a container)"))] = None,
) -> None:
    if every is None:
        _watch_once(election, no_polls)
        return
    if every < 60:
        raise PollEdgeError(_("--every must be at least 60 seconds"))
    # In a container this is PID 1, for which Linux ignores SIGTERM without a handler: "docker stop"
    # (Synology: Stop / Build) would hang until the kill timeout. Exit cleanly instead.
    import signal

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        started = time.monotonic()
        console.rule(to_local(utcnow()).strftime(_("%Y-%m-%d %H:%M")))
        try:
            _watch_once(election, no_polls)
        except Exception as exc:  # keep the service alive; the next run may succeed
            log.exception("Watch run failed")
            print_error(exc)
        time.sleep(max(60.0, every - (time.monotonic() - started)))


def _watch_once(election: list[str] | None, no_polls: bool) -> None:
    from polledge.journal.paper import record_signals, record_snapshot
    from polledge.notify.base import Signal
    from polledge.notify.channels import build_notifiers
    from polledge.services import compare_election, fetch_odds, market_fair_probs, params_payload, update_polls

    s = state().settings
    notifiers = build_notifiers(s.notify, console)
    fetcher = _make_fetcher()
    wiki = state().wiki_client()
    try:
        for cfg in _elections_with_bookmakers(election):
            with state().session() as session:
                if not no_polls and cfg.uses_polls:
                    try:
                        parsed, inserted, _updated = update_polls(session, wiki, cfg)
                        ok(_("{election}: {parsed} polls, {inserted} new", election=cfg.name, parsed=parsed, inserted=inserted))
                    except PollEdgeError as exc:
                        console.print("[yellow]" + _("Polls of {election} could not be updated:", election=cfg.name)
                                      + f"[/yellow] {exc}")
                _print_fetch_reports(cfg, fetch_odds(session, fetcher, cfg))
                quotes = load_quotes(session, cfg.id)
                if not quotes:
                    continue
                result = compare_election(session, s, cfg, quotes)
                comparisons = result.comparisons
                created = record_signals(session, cfg.id, comparisons)
                probs = {c.market: {o.outcome: o.p_model for o in c.outcomes if o.in_model} for c in comparisons}
                record_snapshot(session, cfg.id, probs, params_payload(result.params, result.as_of),
                                market_fair_probs(comparisons))
                for c in comparisons:
                    if c.is_arbitrage:
                        console.print("[bold magenta]" + _("Arbitrage") + "[/bold magenta] "
                                      + _("{market}: sum of 1/odds {value}", market=market_label(cfg, c.market),
                                          value=num(c.arbitrage_sum, 4)))
                if not created:
                    console.print(_("{election}: no new signals.", election=cfg.name))
                    continue
                signals = [
                    Signal(o.market, market_label(cfg, o.market), o.outcome, outcome_label(cfg, o.outcome),
                           o.best_bookmaker, o.best_odds, o.p_final, o.edge, o.stake, s.signals.currency)
                    for o in created
                ]
                for n in notifiers:
                    try:
                        n.send(cfg.name, signals)
                    except Exception as exc:  # a failing channel must not stop the run
                        log.error("Notifier %s failed: %s", type(n).__name__, exc)
                        fail(_("Notification {channel} failed", channel=type(n).__name__), exc)
    finally:
        fetcher.close()
        wiki.close()
    analysis_only()


# ---------- signals and journal ----------

@app.command("signals", help=_("Compare the model with the best odds; print value signals, arbitrage and stakes."))
@handle_errors
def signals_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help=_("Election id"))],
    record: Annotated[bool, typer.Option("--record", help=_("Save the signals as virtual bets in the journal"))] = False,
    runs: Annotated[Optional[int], typer.Option("--runs", "-n", help=_("Number of simulations"))] = None,
    only_signals: Annotated[bool, typer.Option("--only-signals", help=_("Show only rows with a signal"))] = False,
) -> None:
    from polledge.journal.paper import record_signals, record_snapshot
    from polledge.services import compare_election, market_fair_probs, params_payload

    s = state().settings
    cfg = get_election(s, election)
    with state().session() as session:
        quotes = load_quotes(session, cfg.id)
        if not quotes:
            raise PollEdgeError(_("There are no odds for election {id} in the database. Use `import-odds` or "
                                  "`fetch-odds`.", id=cfg.id))
        result = compare_election(session, s, cfg, quotes, runs)
        comparisons, params = result.comparisons, result.params

        sig = s.signals
        subtitle = _("Edge threshold {threshold} · Kelly {kelly} · bankroll {bankroll}",
                     threshold=pct(sig.edge_threshold), kelly=num(sig.kelly_fraction, 2), bankroll=money(sig.bankroll))
        source = (_("model as of {date}", date=fmt_date(result.as_of)) if cfg.uses_polls
                  else _("probabilities from {source}", source=cfg.reference_market.capitalize()))
        console.print(Panel.fit(f"[bold]{cfg.name}[/bold] – {source}\n{subtitle}"))
        columns = (_("Outcome"), _("Model"), _("Market (no margin)"), _("Final"), _("Best odds"), _("Bookmaker"),
                   _("Edge"), _("Stake"), "")
        left = {_("Outcome"), _("Bookmaker"), ""}
        for comp in comparisons:
            if only_signals and not comp.signals:
                continue
            t = Table(title=market_label(cfg, comp.market), title_justify="left")
            for col in columns:
                t.add_column(col, justify="left" if col in left else "right")
            for o in sorted(comp.outcomes, key=lambda o: -o.p_final):
                if o.best_odds is None or (only_signals and not o.is_signal):
                    continue
                model_cell = pct(o.p_model) if o.in_model else "[dim]" + _("not in model") + "[/dim]"
                t.add_row(
                    outcome_label(cfg, o.outcome), model_cell, pct(o.p_market),
                    pct(o.p_final) if o.in_model else DASH, num(o.best_odds),
                    o.best_bookmaker or "", signed_pct(o.edge), money(o.stake) if o.stake > 0 else DASH,
                    "[bold green]" + _("SIGNAL") + "[/bold green]" if o.is_signal
                    else "[dim]" + _("stake below minimum") + "[/dim]" if o.below_min_stake else "",
                )
            console.print(t)
            margins = ", ".join(f"{bm} {pct(m)}" for bm, m in comp.margins.items())
            console.print("  " + _("Margins: {margins}", margins=margins))
            if comp.arbitrage_sum is not None:
                value = num(comp.arbitrage_sum, 4)
                if comp.is_arbitrage:
                    console.print("  [bold magenta]" + _("ARBITRAGE:") + "[/bold magenta] "
                                  + _("sum of 1/best odds = {value} < 1", value=value))
                else:
                    console.print("  " + _("Sum of 1/best odds: {value} (no arbitrage)", value=value))
            console.print()

        n_signals = sum(len(c.signals) for c in comparisons)
        n_arb = sum(c.is_arbitrage for c in comparisons)
        console.print("[bold]" + _("Signals: {signals}, arbitrages: {arbs}", signals=n_signals, arbs=n_arb) + "[/bold]")
        if record:
            created = record_signals(session, cfg.id, comparisons)
            probs = {c.market: {o.outcome: o.p_model for o in c.outcomes if o.in_model} for c in comparisons}
            record_snapshot(session, cfg.id, probs, params_payload(params, result.as_of), market_fair_probs(comparisons))
            console.print("[green]" + _("Saved {count} new virtual bets to the journal.", count=len(created)) + "[/green]")
        analysis_only()


@app.command("journal", help=_("List the virtual bets in the journal."))
@handle_errors
def journal_cmd(election: Annotated[str, typer.Option("--election", "-e", help=_("Election id"))]) -> None:
    from sqlalchemy import select

    cfg = get_election(state().settings, election)
    with state().session() as session:
        bets = session.scalars(
            select(PaperBetRow).where(PaperBetRow.election_id == cfg.id).order_by(PaperBetRow.created_at)
        ).all()
    if not bets:
        console.print(_("The journal is empty."))
        return
    t = Table(title=_("Journal - {election}", election=cfg.name))
    for col in (_("Time"), _("Market"), _("Outcome"), _("Bookmaker"), _("Odds"), _("Model"), _("Edge"),
                _("Stake"), _("Status"), _("Profit")):
        t.add_column(col)
    status = {"open": _("open"), "won": "[green]" + _("won") + "[/green]",
              "lost": "[red]" + _("lost") + "[/red]", "void": _("void")}
    for b in bets:
        t.add_row(to_local(b.created_at).strftime(_("%Y-%m-%d %H:%M")), market_label(cfg, b.market), outcome_label(cfg, b.outcome),
                  b.bookmaker, num(b.odds), pct(b.p_model), signed_pct(b.edge), money(b.stake),
                  status.get(b.status, b.status), money(b.profit) if b.profit is not None else DASH)
    console.print(t)


@app.command("settle", help=_("Enter the actual election result and evaluate the journal: profit/loss, ROI, Brier score."))
@handle_errors
def settle_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help=_("Election id"))],
    result: Annotated[Optional[Path], typer.Option("--result", "-r", help=_("YAML with the result (first_round, runoff)"))] = None,
) -> None:
    from pydantic import ValidationError

    from polledge.journal.paper import settle

    cfg = get_election(state().settings, election)
    if result is not None:
        try:
            res = ElectionResult(**read_yaml(result))
        except ValidationError as exc:
            raise PollEdgeError(_("Invalid result file {path}: {error}", path=result, error=exc)) from exc
    elif cfg.result is not None:
        res = cfg.result
    else:
        raise PollEdgeError(_("Pass the result with --result file.yaml (keys first_round and optionally runoff)"))
    unknown = (set(res.first_round) | set(res.runoff)) - set(cfg.candidates) - {OTHER}
    if unknown:
        raise PollEdgeError(_("The result contains unknown candidates: {ids}", ids=", ".join(sorted(unknown))))

    with state().session() as session:
        report = settle(session, cfg.id, res)
    t = Table(title=_("Settlement - {election}", election=cfg.name), show_header=False)
    t.add_row(_("Bets in total"), str(report.n_bets))
    t.add_row(_("Won / lost / void"), f"{report.n_won} / {report.n_lost} / {report.n_void}")
    t.add_row(_("Staked"), money(report.staked))
    t.add_row(_("Profit / loss"), money(report.profit))
    t.add_row("ROI", pct(report.roi))
    t.add_row(_("Model Brier score (all snapshots)"), num(report.brier_model, 4) + f" (n={report.n_scored})")
    t.add_row(_("Model Brier score (rows with odds)"), num(report.brier_model_same_rows, 4))
    t.add_row(_("Market Brier score (no margin)"), num(report.brier_market, 4) + f" (n={report.n_scored_market})")
    console.print(t)
    if report.brier_market is not None and report.brier_model_same_rows is not None:
        better = _("model") if report.brier_model_same_rows < report.brier_market else _("market")
        console.print(_("Better calibrated: {who} (a lower Brier score is better).", who=f"[bold]{better}[/bold]"))


# ---------- calibration ----------

@app.command("calibrate", help=_("Calibrate the model parameters on past elections (grid search, leave-one-election-out)."))
@handle_errors
def calibrate_cmd(
    election: Annotated[Optional[list[str]], typer.Option("--election", "-e", help=_("Past elections (default: all with a result)"))] = None,
    horizons: Annotated[str, typer.Option("--horizons", help=_("Days before the election, comma separated"))] = DEFAULT_HORIZONS,
    sims: Annotated[int, typer.Option("--sims", help=_("Simulations per grid point"))] = 2000,
    write: Annotated[bool, typer.Option("--write/--no-write", help=_("Save the best parameters to the database"))] = True,
) -> None:
    from polledge.calibration.tune import calibration_payload, grid_edges, tune
    from polledge.services import save_calibration

    s = state().settings
    hz = _parse_horizons(horizons)
    data = _history_data(election)
    ids = [cfg.id for cfg, _polls in data]
    console.print(_("Calibrating on {count} elections ({ids}), horizons {horizons} days...",
                    count=len(data), ids=", ".join(ids), horizons=list(hz)))
    base = s.uncalibrated_model or s.model
    with console.status(_("Grid search in progress...")):
        res = tune(data, base, hz, n_sims=sims)

    t = Table(title=_("Best parameter combinations (mean CRPS of shares, pp; lower is better)"))
    for col in (_("Half-life (days)"), _("House effects (min. polls)"), _("σ systematic"), _("σ horizon / √day"), "CRPS"):
        t.add_column(col, justify="right")
    for combo, score in res.table[:8]:
        t.add_row(str(combo["half_life_days"]), _("off") if combo["house"] is None else str(combo["house"]),
                  num(combo["sigma_sys_pp"], 1), num(combo["sigma_horizon_pp_per_sqrt_day"], 2), num(score, 3))
    console.print(t)

    lt = Table(title=_("Leave-one-election-out (tuned without the election vs. settings.yaml)"))
    lt.add_column(_("Election"))
    lt.add_column(_("CRPS tuned"), justify="right")
    lt.add_column(_("CRPS default"), justify="right")
    for eid, row in res.loeo.items():
        lt.add_row(eid, num(row["tuned"], 3), num(row["default"], 3))
    console.print(lt)
    console.print(_("Coverage of 80% intervals with the best parameters: {value} (ideal ~80 %)", value=pct(res.coverage80)))
    if res.sigma_runoff_pp is not None:
        console.print(_("Estimated error of runoff head-to-head polls: {value} pp (very small sample)",
                        value=num(res.sigma_runoff_pp, 1)))
    console.print(_("Default parameters: CRPS {default} -> tuned: {tuned}",
                    default=num(res.default_score, 3), tuned=num(res.best_score, 3)))
    edges = grid_edges(res.best)
    if edges:
        console.print("[yellow]" + _("The optimum lies on the edge of the grid for: {params} - consider widening "
                                     "DEFAULT_GRID.", params=", ".join(edges)) + "[/yellow]")

    if write:
        payload = calibration_payload(res, base, ids)
        with state().session() as session:
            save_calibration(session, payload)
        console.print("[green]" + _("Calibrated parameters saved to the database:") + f"[/green] {payload['model']}")
        console.print("[dim]" + _("`polledge calibrate-reset` returns to the values from settings.yaml.") + "[/dim]")


@app.command("calibrate-reset", help=_("Delete the calibrated parameters; the model uses settings.yaml again."))
@handle_errors
def calibrate_reset_cmd() -> None:
    from polledge.services import delete_calibration

    with state().session() as session:
        removed = delete_calibration(session)
    console.print(_("Calibrated parameters deleted.") if removed else _("There were no calibrated parameters."))


@app.command("migrate-db", help=_("Copy all data from this database to another one (e.g. local SQLite -> PostgreSQL)."))
@handle_errors
def migrate_db_cmd(
    target: Annotated[str, typer.Option("--target", help=_("Target database URL (postgresql://...)"))],
    replace: Annotated[bool, typer.Option("--replace", help=_("Overwrite data already in the target"))] = False,
) -> None:
    from polledge.config import Settings
    from polledge.migrate import copy_database

    target_url = Settings(database_url=target).db_url()
    with console.status(_("Copying data...")):
        counts = copy_database(state().settings.db_url(), target_url, replace=replace)
    for table, n in counts.items():
        console.print(f"  {table}: {n}")
    ok(_("Data copied to the target database."))


@app.command("backtest", help=_("Evaluate the model on past elections; with --odds also replay virtual betting."))
@handle_errors
def backtest_cmd(
    election: Annotated[str, typer.Option("--election", "-e", help=_("Past election with a result"))],
    odds: Annotated[Optional[Path], typer.Option("--odds", help=_("Historical odds (CSV/YAML) for simulated betting"))] = None,
    horizons: Annotated[str, typer.Option("--horizons", help=_("Days before the election, comma separated"))] = DEFAULT_HORIZONS,
) -> None:
    from polledge.calibration.backtest import backtest_with_odds, evaluate_election

    s = state().settings
    (cfg, polls), = _history_data([election])
    params = model_params_for(s, cfg)
    scores = evaluate_election(cfg, polls, params, _parse_horizons(horizons))
    t = Table(title=_("Backtest - {election}", election=cfg.name))
    for col in (_("Days before"), _("Polls"), _("MAE of average (pp)"), "CRPS (pp)", _("80% coverage"),
                _("P(actual winner)"), _("Winner Brier"), _("Head-to-head error (pp)")):
        t.add_column(col, justify="right")
    for sc in scores:
        t.add_row(str(sc.horizon), str(sc.n_polls), num(sc.mae_pp), num(sc.crps_pp), pct(sc.coverage80, 0),
                  pct(sc.p_actual_winner), num(sc.winner_brier, 3),
                  decimal(f"{sc.runoff_error_pp:+.1f}") if sc.runoff_error_pp is not None else DASH)
    console.print(t)

    if odds is not None:
        quotes = source_for_path(odds).fetch(cfg.id)
        bt = backtest_with_odds(cfg, polls, quotes, s, params)
        r = Table(title=_("Simulated betting on historical odds"), show_header=False)
        r.add_row(_("Days with odds"), str(bt.n_snapshots))
        r.add_row(_("Bets"), str(bt.n_bets))
        r.add_row(_("Staked"), money(bt.staked))
        r.add_row(_("Profit / loss"), money(bt.profit))
        r.add_row("ROI", pct(bt.roi))
        r.add_row(_("Brier model / market"), f"{num(bt.brier_model, 4)} / {num(bt.brier_market, 4)} (n={bt.n_scored})")
        console.print(r)


def _parse_horizons(text: str) -> tuple[int, ...]:
    try:
        values = tuple(int(x) for x in text.split(",") if x.strip())
    except ValueError:
        raise PollEdgeError(_("Invalid horizons '{value}', expected e.g. 60,30,7", value=text)) from None
    if not values or min(values) < 1:
        raise PollEdgeError(_("Horizons must be positive integers"))
    return values


def _history_data(ids: list[str] | None) -> list[tuple[ElectionConfig, list]]:
    s = state().settings
    all_cfg = discover_elections(s, include_history=True)
    if ids:
        cfgs = [get_election(s, i) for i in ids]
    else:
        cfgs = [c for c in all_cfg.values() if c.result is not None and c.result.first_round]
    if not cfgs:
        raise PollEdgeError(_("There are no past elections with a result (config/elections/history/*.yaml)"))
    data = []
    with state().session() as session:
        for cfg in cfgs:
            if cfg.result is None:
                raise PollEdgeError(_("Election {id} has no result in its config (field result.first_round)", id=cfg.id))
            polls = load_polls(session, cfg.id)
            if not polls:
                raise PollEdgeError(_("There are no polls for election {id} in the database. Run "
                                      "`polledge update-polls -e {id}`.", id=cfg.id))
            data.append((cfg, polls))
    return data


if __name__ == "__main__":
    app()
