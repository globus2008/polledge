"""Implied probabilities, bookmaker margin (overround) and margin removal."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from polledge.model.markets import BINARY_OUTCOMES, parse_market
from polledge.odds.base import OddsQuote
from polledge.i18n import _


def implied_probability(odds: float) -> float:
    if odds <= 1.0:
        raise ValueError(_("Decimal odds must be > 1"))
    return 1.0 / odds


def overround(odds: list[float]) -> float:
    """Bookmaker margin: sum of implied probabilities minus 1 (0.08 = 8 %)."""
    return sum(implied_probability(o) for o in odds) - 1.0


def remove_margin(odds: dict[str, float]) -> dict[str, float]:
    """Normalise implied probabilities of one bookmaker's market to sum to 1."""
    implied = {k: implied_probability(v) for k, v in odds.items()}
    total = sum(implied.values())
    return {k: v / total for k, v in implied.items()}


@dataclass
class BookMarket:
    """Latest odds of one bookmaker in one market."""

    election_id: str
    bookmaker: str
    market: str
    odds: dict[str, float]
    timestamp: datetime
    complete: bool

    @property
    def margin(self) -> float:
        return overround(list(self.odds.values()))

    @property
    def fair(self) -> dict[str, float] | None:
        """Margin-free probabilities; None when the market is incomplete."""
        return remove_margin(self.odds) if self.complete else None


def is_complete(market: str, odds: dict[str, float], expected: set[str] | None = None,
                model_probs: dict[str, float] | None = None) -> bool:
    """Whether a bookmaker quotes all outcomes of the market (needed for normalisation).

    Binary markets need both sides. Multi-outcome markets must have implied
    probabilities summing to at least 1 and, if model probabilities are given,
    the outcomes missing from the book may carry at most 2 % of model mass.
    """
    kind = parse_market(market).kind
    if kind in BINARY_OUTCOMES:
        return set(BINARY_OUTCOMES[kind]) <= set(odds)
    if overround(list(odds.values())) < 0:
        return False
    if expected is not None and model_probs is not None:
        missing = expected - set(odds)
        return sum(model_probs.get(o, 0.0) for o in missing) <= 0.02
    return True


def latest_books(quotes: list[OddsQuote], model_probs: dict[str, dict[str, float]] | None = None) -> list[BookMarket]:
    """Group quotes into (bookmaker, market) books, keeping the latest quote per outcome."""
    latest: dict[tuple[str, str, str], dict[str, OddsQuote]] = {}
    for q in quotes:
        book = latest.setdefault((q.election_id, q.bookmaker, q.market), {})
        if q.outcome not in book or q.timestamp > book[q.outcome].timestamp:
            book[q.outcome] = q
    books = []
    for (election_id, bookmaker, market), by_outcome in sorted(latest.items()):
        odds = {o: q.odds for o, q in by_outcome.items()}
        probs = (model_probs or {}).get(market)
        complete = is_complete(market, odds, set(probs) if probs else None, probs)
        books.append(
            BookMarket(election_id, bookmaker, market, odds, max(q.timestamp for q in by_outcome.values()), complete)
        )
    return books
