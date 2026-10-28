"""Glue between storage, model and signals, shared by the CLI, the web app and the backtest."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from polledge.timeutil import utcnow
from polledge.cache import PageCache
from polledge.config import (
    ElectionConfig,
    ModelParams,
    Settings,
    apply_calibration,
    load_settings,
    model_params_for,
    read_yaml,
)
from polledge.db import AppSettingRow, make_session_factory
from polledge.errors import PollEdgeError
from polledge.model.markets import MarketSpec, default_markets, market_probabilities, parse_market
from polledge.model.simulate import SimulationResult, simulate_election
from polledge.odds.base import OddsQuote
from polledge.odds.margin import latest_books
from polledge.polls.parser import parse_election_html
from polledge.polls.repository import load_polls, upsert_polls
from polledge.polls.types import Poll
from polledge.polls.wiki_client import WikiClient
from polledge.signals.compare import MarketComparison, compare_market
from polledge.i18n import _

log = logging.getLogger(__name__)

CALIBRATION_KEY = "calibrated_model"


@dataclass
class AppContext:
    """Settings + database, the entry point for every front end (CLI, web, scheduled job)."""

    settings: Settings
    sessions: sessionmaker

    def session(self) -> Session:
        return self.sessions()

    def wiki_client(self) -> WikiClient:
        return WikiClient(self.settings.wikipedia, PageCache(self.sessions, "wikipedia"))

    def fetcher(self):
        from polledge.odds.scrapers import PoliteFetcher

        return PoliteFetcher(self.settings.scraping, PageCache(self.sessions, "bookmakers"))


def open_app(config: Path | None = None) -> AppContext:
    settings = load_settings(config)
    ctx = AppContext(settings, make_session_factory(settings.db_url()))
    with ctx.sessions() as session:
        calibration = load_calibration(session)
        if calibration is None:
            calibration = import_legacy_calibration(session, settings)
    if calibration:
        apply_calibration(settings, calibration.get("model"), "database (app_setting)")
    return ctx


def load_calibration(session: Session) -> dict | None:
    row = session.get(AppSettingRow, CALIBRATION_KEY)
    return row.value if row is not None else None


def save_calibration(session: Session, payload: dict) -> None:
    row = session.get(AppSettingRow, CALIBRATION_KEY)
    if row is None:
        session.add(AppSettingRow(key=CALIBRATION_KEY, value=payload, updated_at=utcnow()))
    else:
        row.value, row.updated_at = payload, utcnow()
    session.commit()


def delete_calibration(session: Session) -> bool:
    row = session.get(AppSettingRow, CALIBRATION_KEY)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def import_legacy_calibration(session: Session, settings: Settings) -> dict | None:
    """One-time import of config/calibrated.yaml (older versions kept calibration in a file)."""
    path = settings.resolve(settings.calibrated_file)
    if not path.exists():
        return None
    payload = read_yaml(path)
    if not payload.get("model"):
        return None
    save_calibration(session, payload)
    log.info("Imported calibrated parameters from %s into the database", path)
    return payload


def update_polls(session: Session, client: WikiClient, election: ElectionConfig, force: bool = False) -> tuple[int, int, int]:
    """Download, parse and store polls. Returns (parsed, inserted, updated).

    Elections in "market" mode have no polls: (0, 0, 0).
    """
    if not election.uses_polls:
        return 0, 0, 0
    html = client.get_page_html(election.wiki_page, force=force)
    polls = parse_election_html(html, election)
    inserted, updated = upsert_polls(session, polls)
    return len(polls), inserted, updated


@dataclass
class FetchReport:
    bookmaker: str
    quotes: int = 0
    inserted: int = 0
    error: str | None = None


def skipped_sources() -> set[str]:
    """Sources not to contact from this machine, e.g. POLLEDGE_SKIP_SOURCES=betano on cloud servers
    that the bookmaker blocks (they are fetched from a home network instead)."""
    return {s.strip().lower() for s in os.environ.get("POLLEDGE_SKIP_SOURCES", "").split(",") if s.strip()}


def fetch_odds(
    session: Session,
    fetcher,
    election: ElectionConfig,
    bookmakers: list[str] | None = None,
    force: bool = False,
) -> list[FetchReport]:
    """Download odds from the configured bookmaker pages and store them with the fetch time."""
    from polledge.odds.repository import store_quotes
    from polledge.odds.scrapers import make_source

    reports = []
    skipped = skipped_sources()
    for name, book in election.bookmakers.items():
        if bookmakers and name.lower() not in {b.lower() for b in bookmakers}:
            continue
        if name.lower() in skipped:
            log.info("Source %s skipped on this machine (POLLEDGE_SKIP_SOURCES)", name)
            continue
        report = FetchReport(name)
        try:
            source = make_source(name, fetcher, election, book, force=force)
            quotes = source.fetch(election.id)
            report.quotes = len(quotes)
            report.inserted, _updated = store_quotes(session, quotes, f"scraper:{source.name}")
        except PollEdgeError as exc:
            log.warning("Odds from %s for %s failed: %s", name, election.id, exc)
            report.error = str(exc)
        reports.append(report)
    return reports


def run_model(
    polls: list[Poll],
    election: ElectionConfig,
    params: ModelParams,
    as_of: date | None = None,
    n_sims: int | None = None,
) -> SimulationResult:
    as_of = as_of or min(date.today(), election.election_date)
    first = [p for p in polls if p.round == "first"]
    runoff = [p for p in polls if p.round == "runoff"]
    sim = simulate_election(first, runoff, as_of, election.election_date, params,
                            two_round=election.type == "two_round", n_sims=n_sims)
    if sim is None:
        raise PollEdgeError(
            _("No usable polls for election {id} as of {date}. Run `update-polls` first.", id=election.id, date=as_of)
        )
    return sim


def run_model_from_db(session: Session, settings: Settings, election: ElectionConfig,
                      as_of: date | None = None, n_sims: int | None = None) -> tuple[SimulationResult, ModelParams]:
    params = model_params_for(settings, election)
    return run_model(load_polls(session, election.id), election, params, as_of, n_sims), params


def probabilities_for(sim: SimulationResult, specs: list[MarketSpec]) -> dict[str, dict[str, float]]:
    out = {}
    for spec in specs:
        try:
            out[spec.id] = market_probabilities(sim, spec)
        except (KeyError, ValueError) as exc:
            log.warning("Market %s skipped: %s", spec.id, exc)
    return out


def model_markets(sim: SimulationResult, election: ElectionConfig, extra: list[str] | None = None) -> list[MarketSpec]:
    specs = default_markets(sim, election.type == "two_round")
    known = {s.id for s in specs}
    for m in extra or []:
        spec = parse_market(m)
        if spec.id not in known:
            specs.append(spec)
            known.add(spec.id)
    return specs


def build_comparisons(sim: SimulationResult, quotes: list[OddsQuote], settings: Settings) -> list[MarketComparison]:
    markets = sorted({q.market for q in quotes})
    model_probs = probabilities_for(sim, [parse_market(m) for m in markets])
    books = latest_books(quotes, model_probs, settings.signals.reference_bookmakers)
    return [compare_market(m, model_probs[m], books, settings.signals) for m in markets if m in model_probs]


def reference_probabilities(quotes: list[OddsQuote], settings: Settings, source: str) -> dict[str, dict[str, float]]:
    """{market: {outcome: p}} from the latest prices of the reference market (e.g. Polymarket)."""
    books = latest_books([q for q in quotes if q.bookmaker.lower() == source.lower()], None,
                         settings.signals.reference_bookmakers)
    return {b.market: b.fair for b in books if b.fair}


def build_market_comparisons(election: ElectionConfig, quotes: list[OddsQuote], settings: Settings) -> list[MarketComparison]:
    """Comparisons for an election without a poll model: the reference market plays the model."""
    model_probs = reference_probabilities(quotes, settings, election.reference_market)
    books = latest_books(quotes, model_probs, settings.signals.reference_bookmakers)
    return [compare_market(m, probs, books, settings.signals) for m, probs in sorted(model_probs.items())]


@dataclass
class ElectionComparison:
    comparisons: list[MarketComparison]
    sim: SimulationResult | None  # None in "market" mode
    params: ModelParams
    as_of: date


def compare_election(session: Session, settings: Settings, election: ElectionConfig, quotes: list[OddsQuote],
                     n_sims: int | None = None) -> ElectionComparison:
    """Model (or reference market) vs. stored odds - the single entry point for CLI, watch and web."""
    params = model_params_for(settings, election)
    if not election.uses_polls:
        return ElectionComparison(build_market_comparisons(election, quotes, settings), None, params, date.today())
    sim, params = run_model_from_db(session, settings, election, None, n_sims)
    return ElectionComparison(build_comparisons(sim, quotes, settings), sim, params, sim.average.as_of)


def market_fair_probs(comparisons: list[MarketComparison]) -> dict[str, dict[str, float]]:
    return {
        c.market: {o.outcome: o.p_market for o in c.outcomes if o.p_market is not None} for c in comparisons
    }


def params_payload(params: ModelParams, as_of: date) -> dict:
    return {**params.model_dump(), "as_of": as_of.isoformat(), "computed_at": utcnow().isoformat()}
