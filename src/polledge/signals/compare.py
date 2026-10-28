"""Model vs. market comparison: best odds, edge, value signals and arbitrage."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean

from polledge.config import SignalSettings
from polledge.odds.margin import BookMarket, is_complete
from polledge.signals.kelly import kelly_stake


def edge(p_model: float, odds: float) -> float:
    """Expected return per unit staked: p * odds - 1."""
    return p_model * odds - 1.0


_EPS = 1e-4


def blend_probability(p_model: float, p_reference: float | None, model_weight: float) -> float:
    """Logit-weighted average of the model and a reference market (model_weight 1 = model only).

    Averaging log-odds keeps extreme probabilities sensible (0.98 and 0.5 do not average to 0.74
    in probability space but to about 0.88) and is the usual way to pool two forecasts.
    """
    if p_reference is None or model_weight >= 1.0:
        return p_model
    pm = min(1 - _EPS, max(_EPS, p_model))
    pr = min(1 - _EPS, max(_EPS, p_reference))
    logit = model_weight * math.log(pm / (1 - pm)) + (1 - model_weight) * math.log(pr / (1 - pr))
    return 1.0 / (1.0 + math.exp(-logit))


def arbitrage_sum(best_odds: dict[str, float]) -> float:
    """Sum of 1/best odds over all outcomes; < 1 means a guaranteed profit across bookmakers."""
    return sum(1.0 / o for o in best_odds.values())


def is_arbitrage(best_odds: dict[str, float]) -> bool:
    return bool(best_odds) and arbitrage_sum(best_odds) < 1.0


@dataclass
class OutcomeComparison:
    market: str
    outcome: str
    p_model: float
    p_market: float | None  # average margin-free probability over complete books (incl. reference sources)
    p_reference: float | None  # margin-free probability from reference sources only (e.g. Polymarket)
    best_odds: float | None
    best_bookmaker: str | None
    odds_by_bookmaker: dict[str, float]
    edge: float | None
    stake: float
    is_signal: bool
    in_model: bool = True  # False: the bookmaker lists it, but the model has no polls for it
    below_min_stake: bool = False  # edge above the threshold, but the Kelly stake is below min_stake
    p_final: float | None = None  # probability behind edge and stake: model blended with the reference market

    def __post_init__(self) -> None:
        if self.p_final is None:
            self.p_final = self.p_model


@dataclass
class MarketComparison:
    market: str
    outcomes: list[OutcomeComparison]
    books: list[BookMarket]
    arbitrage_sum: float | None  # None if the best-odds set does not cover the whole market
    margins: dict[str, float] = field(default_factory=dict)

    @property
    def is_arbitrage(self) -> bool:
        return self.arbitrage_sum is not None and self.arbitrage_sum < 1.0

    @property
    def signals(self) -> list[OutcomeComparison]:
        return [o for o in self.outcomes if o.is_signal]


def compare_market(
    market: str, model_probs: dict[str, float], books: list[BookMarket], settings: SignalSettings
) -> MarketComparison:
    books = [b for b in books if b.market == market]
    reference = {name.lower() for name in settings.reference_bookmakers}
    tradable = [b for b in books if b.bookmaker.lower() not in reference]
    outcomes = []
    best_odds: dict[str, float] = {}
    for outcome in sorted(set(model_probs) | {o for b in books for o in b.odds}):
        in_model = outcome in model_probs
        p_model = model_probs.get(outcome, 0.0)
        # Reference sources (e.g. prediction markets) inform the market probability only.
        quoted = {b.bookmaker: b.odds[outcome] for b in tradable if outcome in b.odds}
        fair = [b.fair[outcome] for b in books if b.fair is not None and outcome in b.fair]
        reference_fair = [b.fair[outcome] for b in books
                          if b.bookmaker.lower() in reference and b.fair is not None and outcome in b.fair]
        p_reference = mean(reference_fair) if reference_fair else None
        # Edge and stake use the model shrunk towards the reference market where one exists.
        p = blend_probability(p_model, p_reference, settings.model_weight) if in_model else p_model
        if quoted and not in_model:
            bookmaker, best = max(quoted.items(), key=lambda kv: kv[1])
            best_odds[outcome] = best
            e, stake = None, 0.0
        elif quoted:
            bookmaker, best = max(quoted.items(), key=lambda kv: kv[1])
            best_odds[outcome] = best
            e = edge(p, best)
            stake = kelly_stake(p, best, settings.bankroll, settings.kelly_fraction, settings.max_stake_pct)
        else:
            bookmaker = best = e = None
            stake = 0.0
        outcomes.append(
            OutcomeComparison(
                market=market,
                outcome=outcome,
                p_model=p_model,
                p_market=mean(fair) if fair else None,
                p_reference=p_reference,
                best_odds=best,
                best_bookmaker=bookmaker,
                odds_by_bookmaker=quoted,
                edge=e,
                stake=stake,
                is_signal=(e is not None and e > settings.edge_threshold and stake > 0
                           and stake >= settings.min_stake),
                below_min_stake=(e is not None and e > settings.edge_threshold and 0 < stake < settings.min_stake),
                in_model=in_model,
                p_final=p,
            )
        )
    covered = bool(best_odds) and is_complete(market, best_odds, set(model_probs), model_probs)
    return MarketComparison(
        market=market,
        outcomes=outcomes,
        books=books,
        arbitrage_sum=arbitrage_sum(best_odds) if covered else None,
        margins={b.bookmaker: b.margin for b in tradable},
    )
