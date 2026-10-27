"""Walk-forward evaluation on past elections: the model only sees polls published before each snapshot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import numpy as np

from polledge.calibration.metrics import crps_samples, interval_coverage, log_loss, multiclass_brier
from polledge.config import OTHER, ElectionConfig, ElectionResult, ModelParams, Settings
from polledge.errors import PollEdgeError
from polledge.journal.paper import brier, winning_outcomes
from polledge.model.aggregate import weighted_average
from polledge.model.markets import MarketSpec, market_probabilities
from polledge.model.simulate import simulate_election
from polledge.odds.base import OddsQuote
from polledge.polls.types import Poll
from polledge.services import build_comparisons
from polledge.i18n import _

log = logging.getLogger(__name__)

DEFAULT_HORIZONS = (90, 60, 30, 14, 7, 1)


@dataclass
class HorizonScore:
    election_id: str
    horizon: int
    n_polls: int
    crps_pp: float  # mean CRPS over candidates, in percentage points
    mae_pp: float  # mean absolute error of the poll average
    coverage80: float  # share of candidates inside the 80 % interval
    winner_brier: float
    winner_logloss: float
    p_actual_winner: float
    runoff_error_pp: float | None  # actual minus averaged H2H polls for the real finalists


def polls_before(polls: list[Poll], cutoff) -> list[Poll]:
    """Lookahead guard: only polls whose fieldwork ended strictly before the cutoff date."""
    return [p for p in polls if p.end_date < cutoff]


def actual_shares(candidates: list[str], result: dict[str, float]) -> np.ndarray:
    """Actual result aligned to model candidates (fractions); unknown parties go to OTHER."""
    values = np.array([result.get(c, 0.0) for c in candidates], dtype=float)
    if OTHER in candidates:
        values[candidates.index(OTHER)] = max(0.0, 100.0 - sum(v for c, v in result.items() if c in candidates))
    return values / values.sum()


def actual_winner(election: ElectionConfig) -> str:
    res = election.result
    return winning_outcomes("winner", res).pop()


def require_result(election: ElectionConfig) -> ElectionResult:
    if election.result is None or not election.result.first_round:
        raise PollEdgeError(_("Election {id} has no result in its config (field result.first_round)", id=election.id))
    return election.result


def evaluate_election(
    election: ElectionConfig,
    polls: list[Poll],
    params: ModelParams,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    n_sims: int = 4000,
) -> list[HorizonScore]:
    result = require_result(election)
    winner = actual_winner(election)
    scores = []
    for h in horizons:
        cutoff = election.election_date - timedelta(days=h)
        visible = polls_before(polls, cutoff)
        as_of = cutoff - timedelta(days=1)
        first = [p for p in visible if p.round == "first"]
        runoff = [p for p in visible if p.round == "runoff"]
        sim = simulate_election(first, runoff, as_of, election.election_date, params,
                                two_round=election.type == "two_round", n_sims=n_sims)
        if sim is None:
            log.info("%s: no polls %d days before the election", election.id, h)
            continue
        actual = actual_shares(sim.candidates, result.first_round)
        crps = [crps_samples(sim.shares[:, j], actual[j]) for j in range(len(sim.candidates))]
        winner_probs = market_probabilities(sim, MarketSpec("winner"))
        scores.append(
            HorizonScore(
                election_id=election.id,
                horizon=h,
                n_polls=sim.average.n_polls,
                crps_pp=100 * float(np.mean(crps)),
                mae_pp=100 * float(np.mean(np.abs(sim.average.mean - actual))),
                coverage80=float(np.mean(interval_coverage(sim.shares, actual))),
                winner_brier=multiclass_brier(winner_probs, winner),
                winner_logloss=log_loss(winner_probs.get(winner, 0.0)),
                p_actual_winner=winner_probs.get(winner, 0.0),
                runoff_error_pp=_runoff_error(runoff, result, as_of, params),
            )
        )
    return scores


def _runoff_error(runoff: list[Poll], result: ElectionResult, as_of, params: ModelParams) -> float | None:
    if len(result.runoff) != 2:
        return None
    pair = sorted(result.runoff)
    polls = [p for p in runoff if sorted(p.shares) == pair]
    avg = weighted_average(polls, as_of, params)
    if avg is None:
        return None
    a = pair[0]
    actual_a = result.runoff[a] / sum(result.runoff.values())
    return 100 * (actual_a - avg.as_dict()[a])


@dataclass
class OddsBacktest:
    n_snapshots: int
    n_bets: int
    staked: float
    profit: float
    brier_model: float | None
    brier_market: float | None
    n_scored: int

    @property
    def roi(self) -> float | None:
        return self.profit / self.staked if self.staked else None


def backtest_with_odds(
    election: ElectionConfig, polls: list[Poll], quotes: list[OddsQuote], settings: Settings, params: ModelParams,
    n_sims: int = 4000,
) -> OddsBacktest:
    """Replay paper trading at every distinct odds date, using only polls and odds known at that time."""
    result = require_result(election)
    days = sorted({q.timestamp.date() for q in quotes if q.timestamp.date() < election.election_date})
    if not days:
        raise PollEdgeError(_("The file contains no odds from before the election"))
    staked = profit = 0.0
    n_bets = 0
    model_pairs: list[tuple[float, int]] = []
    market_pairs: list[tuple[float, int]] = []
    placed: set[tuple] = set()
    for day in days:
        moment = datetime.combine(day, time.max)
        visible_quotes = [q for q in quotes if q.timestamp <= moment]
        visible_polls = [p for p in polls if p.end_date < day]
        first = [p for p in visible_polls if p.round == "first"]
        runoff = [p for p in visible_polls if p.round == "runoff"]
        sim = simulate_election(first, runoff, day, election.election_date, params,
                                two_round=election.type == "two_round", n_sims=n_sims)
        if sim is None:
            continue
        for comp in build_comparisons(sim, visible_quotes, settings):
            winners = winning_outcomes(comp.market, result)
            if winners is None:
                continue
            for o in comp.outcomes:
                y = int(o.outcome in winners)
                if o.p_market is not None:
                    model_pairs.append((o.p_model, y))
                    market_pairs.append((o.p_market, y))
                bet_key = (o.market, o.outcome, o.best_bookmaker, o.best_odds)
                if o.is_signal and bet_key not in placed:
                    placed.add(bet_key)  # same price is bet once, like the live journal
                    n_bets += 1
                    staked += o.stake
                    profit += o.stake * (o.best_odds - 1) if y else -o.stake
    return OddsBacktest(len(days), n_bets, staked, profit, brier(model_pairs), brier(market_pairs), len(model_pairs))
