"""Building the review context from PollEdge data, storing reviews and recording reviewed bets."""

from __future__ import annotations

from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from polledge.ai.review import ReviewResult, Reviewer
from polledge.config import OTHER, ElectionConfig, Settings, model_params_for
from polledge.db import AiReviewRow, PaperBetRow
from polledge.model.simulate import SimulationResult
from polledge.presentation import market_label, outcome_label
from polledge.signals.compare import MarketComparison, OutcomeComparison
from polledge.signals.kelly import kelly_fraction
from polledge.timeutil import utcnow


def _r(x, digits=4):
    return None if x is None else round(float(x), digits)


def build_context(election: ElectionConfig, comp: MarketComparison, outcome: OutcomeComparison,
                  sim: SimulationResult | None, settings: Settings) -> dict:
    """Everything the reviewer needs about one signal, in plain JSON-able data."""
    sig = settings.signals
    params = model_params_for(settings, election)
    days = (election.election_date - date.today()).days
    ctx: dict = {
        "election": {"name": election.name, "id": election.id, "date": election.election_date.isoformat(),
                     "days_to_election": days, "type": election.type,
                     "probability_source": "poll model" if election.uses_polls
                     else f"reference market ({election.reference_market})"},
        "market": {"name": market_label(election, comp.market), "outcome": outcome_label(election, outcome.outcome)},
        "signal": {
            "model_probability": _r(outcome.p_model),
            "bookmaker_fair_probability_avg": _r(outcome.p_market),
            "prediction_market_probability": _r(outcome.p_reference),
            "best_odds": outcome.best_odds, "best_bookmaker": outcome.best_bookmaker,
            "odds_by_bookmaker": outcome.odds_by_bookmaker,
            "bookmaker_margins": {k: _r(v) for k, v in comp.margins.items()},
            "edge": _r(outcome.edge),
        },
        "stake": {
            "full_kelly_fraction": _r(kelly_fraction(outcome.p_model, outcome.best_odds)) if outcome.best_odds else None,
            "kelly_multiplier_used": sig.kelly_fraction, "bankroll": sig.bankroll,
            "max_stake_pct": sig.max_stake_pct, "min_stake": sig.min_stake,
            "recommended_stake": round(outcome.stake, 2), "currency": sig.currency,
            "edge_threshold": sig.edge_threshold,
        },
        "other_outcomes_in_market": [
            {"outcome": outcome_label(election, o.outcome), "model": _r(o.p_model, 3), "market": _r(o.p_market, 3),
             "prediction_market": _r(o.p_reference, 3), "best_odds": o.best_odds}
            for o in sorted(comp.outcomes, key=lambda o: -(o.p_model or 0))[:10] if o.outcome != outcome.outcome
        ],
    }
    if sim is None:
        return ctx

    avg = sim.average
    lo, hi = np.quantile(sim.shares, [0.1, 0.9], axis=0)
    order = np.argsort(-avg.mean)
    ctx["model"] = {
        "as_of": avg.as_of.isoformat(), "polls_in_average": avg.n_polls, "effective_sample": round(avg.n_eff),
        "first_round_average": [
            {"candidate": election.candidate_name(sim.candidates[j]), "share": _r(avg.mean[j], 3),
             "interval_80": [_r(lo[j], 3), _r(hi[j], 3)]}
            for j in order[:10]
        ],
        "scenario_weights": {k: _r(v, 3) for k, v in sim.scenario_weights.items()},
        "candidacy_not_standing_share": {election.candidate_name(k): _r(v, 3) for k, v in sim.absent_share.items()},
        "runoff_corrections_pp": sim.runoff_adjustments,
        "runoff_estimate_sources": sim.runoff_sources,
        "parameters": {"half_life_days": params.half_life_days, "sigma_sys_pp": params.sigma_sys_pp,
                       "sigma_horizon_pp_per_sqrt_day": params.sigma_horizon_pp_per_sqrt_day,
                       "sigma_runoff_pp": params.sigma_runoff_pp, "runoff_bias_pp": params.runoff_bias_pp},
        "known_limitations": [
            "Calibrated on 4 past elections (FR 2017/2022, DE 2021/2025); 80% intervals covered ~75% of results.",
            "Runoff poll bias estimated from only two French elections.",
            "Scenario weights are a manual judgement.",
        ],
    }
    if sim.finalists is not None and outcome.outcome in sim.candidates and outcome.outcome != OTHER:
        i = sim.candidates.index(outcome.outcome)
        qualify = float(np.mean((sim.finalists == i).any(axis=1)))
        win = float(np.mean(sim.winner == i))
        ctx["model"]["candidate"] = {"qualify_for_runoff": _r(qualify), "win": _r(win),
                                     "win_if_qualified": _r(win / qualify) if qualify else None}
    return ctx


def run_review(session: Session, reviewer: Reviewer, election: ElectionConfig, comp: MarketComparison,
               outcome: OutcomeComparison, sim: SimulationResult | None, settings: Settings) -> AiReviewRow:
    context = build_context(election, comp, outcome, sim, settings)
    result: ReviewResult = reviewer.review(context)
    row = AiReviewRow(
        election_id=election.id, created_at=utcnow(), market=comp.market, outcome=outcome.outcome,
        bookmaker=outcome.best_bookmaker or "", odds=outcome.best_odds or 0.0, kelly_stake=round(outcome.stake, 2),
        provider=result.provider, model=result.model, verdict=result.verdict,
        stake_multiplier=result.stake_multiplier, probability_estimate=result.probability_estimate,
        result=result.to_dict(), context=context,
    )
    session.add(row)
    session.commit()
    return row


def reviews_for(session: Session, election_id: str, market: str, outcome: str) -> list[AiReviewRow]:
    query = (select(AiReviewRow)
             .where(AiReviewRow.election_id == election_id, AiReviewRow.market == market,
                    AiReviewRow.outcome == outcome)
             .order_by(AiReviewRow.created_at.desc()))
    return list(session.scalars(query))


def reviewed_stake(review: AiReviewRow) -> float:
    return round(review.kelly_stake * review.stake_multiplier, 0)


def record_reviewed_bet(session: Session, review: AiReviewRow, outcome: OutcomeComparison) -> PaperBetRow:
    """Store the bet in the journal with the AI-adjusted stake and link it to the review."""
    bet = PaperBetRow(
        election_id=review.election_id, created_at=utcnow(), market=review.market, outcome=review.outcome,
        bookmaker=review.bookmaker, odds=review.odds, p_model=outcome.p_model, p_market=outcome.p_market,
        edge=outcome.edge or 0.0, stake=reviewed_stake(review), status="open",
    )
    session.add(bet)
    session.flush()
    review.bet_id = bet.id
    session.commit()
    return bet
