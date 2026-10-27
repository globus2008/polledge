"""Glue between storage, model and signals, shared by the CLI and the backtest."""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from polledge.config import ElectionConfig, ModelParams, Settings, model_params_for
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

log = logging.getLogger(__name__)


def update_polls(session: Session, client: WikiClient, election: ElectionConfig, force: bool = False) -> tuple[int, int, int]:
    """Download, parse and store polls. Returns (parsed, inserted, updated)."""
    html = client.get_page_html(election.wiki_page, force=force)
    polls = parse_election_html(html, election)
    inserted, updated = upsert_polls(session, polls)
    return len(polls), inserted, updated


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
            f"Pro volby {election.id} nejsou k {as_of} žádné použitelné průzkumy. Spusť nejdřív `update-polls`."
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
    books = latest_books(quotes, model_probs)
    return [compare_market(m, model_probs[m], books, settings.signals) for m in markets if m in model_probs]


def market_fair_probs(comparisons: list[MarketComparison]) -> dict[str, dict[str, float]]:
    return {
        c.market: {o.outcome: o.p_market for o in c.outcomes if o.p_market is not None} for c in comparisons
    }


def params_payload(params: ModelParams, as_of: date) -> dict:
    return {**params.model_dump(), "as_of": as_of.isoformat(), "computed_at": datetime.now().isoformat()}
