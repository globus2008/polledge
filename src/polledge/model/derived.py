"""Probabilities of bookmaker markets derived from reference-market distributions (market mode).

Bookmakers slice a market differently from Polymarket: a yes/no bet on one outcome of the winner
market, or seats over/under a line where Polymarket lists seat brackets. These are computed from
the reference prices so that such quotes can be compared too.
"""

from __future__ import annotations

import math
import re

from polledge.config import ElectionConfig
from polledge.model.markets import parse_market

_RANGE = re.compile(r"^(\d+)\s*[-–]\s*(\d+)$")
_AT_MOST = re.compile(r"^(?:≤|<=|at most|up to)\s*(\d+)$", re.IGNORECASE)
_BELOW = re.compile(r"^(?:<|below|under|fewer than|less than)\s*(\d+)$", re.IGNORECASE)
_AT_LEAST = re.compile(r"^(?:(\d+)\s*\+|(?:≥|>=|at least)\s*(\d+))$", re.IGNORECASE)
_ABOVE = re.compile(r"^(?:>|above|over|more than)\s*(\d+)$", re.IGNORECASE)


def parse_bracket(label: str) -> tuple[float, float] | None:
    """Seat bracket label -> inclusive (low, high), open ends as -inf / inf; None if not a bracket."""
    text = label.strip()
    if text.isdigit():
        return int(text), int(text)
    if m := _RANGE.match(text):
        return int(m[1]), int(m[2])
    if m := _AT_MOST.match(text):
        return -math.inf, int(m[1])
    if m := _BELOW.match(text):
        return -math.inf, int(m[1]) - 1
    if m := _AT_LEAST.match(text):
        return int(m[1] or m[2]), math.inf
    if m := _ABOVE.match(text):
        return int(m[1]) + 1, math.inf
    return None


def seats_at_most(brackets: dict[str, float], k: int) -> tuple[float, bool] | None:
    """P(seats <= k) from one bracket set: (probability, exact).

    A closed bracket split by k counts in proportion to its seat numbers (uniform within the
    bracket, so not exact); an open bracket split by k makes the set unusable (None).
    """
    parsed = [(parse_bracket(label), p) for label, p in brackets.items()]
    total = sum(p for b, p in parsed if b is not None)
    if total <= 0 or any(b is None for b, _ in parsed):
        return None
    below, exact = 0.0, True
    for (lo, hi), p in parsed:
        if hi <= k:
            below += p
        elif lo <= k:
            if math.isinf(lo):
                return None
            below += p * (k - lo + 1) / (hi - lo + 1)
            exact = False
    return below / total, exact


def _seat_sets(reference: dict[str, dict[str, float]], party: str) -> list[dict[str, float]]:
    sets = []
    for market, probs in sorted(reference.items()):
        spec = parse_market(market)
        if spec.kind == "seats" and spec.candidate == party:
            sets.append(probs)
    return sets


def _best_at_most(sets: list[dict[str, float]], k: int) -> float | None:
    answers = [a for a in (seats_at_most(s, k) for s in sets) if a is not None]
    if not answers:
        return None
    exact = [p for p, is_exact in answers if is_exact]
    return exact[0] if exact else answers[0][0]


def seats_over(reference: dict[str, dict[str, float]], election: ElectionConfig, party: str,
               line: float) -> float | None:
    """P(seats of party > line) from the party's brackets or from the complement party's."""
    sets = _seat_sets(reference, party)
    if sets:
        at_most = _best_at_most(sets, math.floor(line))
        return None if at_most is None else 1.0 - at_most
    complement = election.seat_complements.get(party)
    if complement is None:
        return None
    # party > line  <=>  other = total - party < total - line  <=>  other <= ceil(total - line) - 1
    at_most = _best_at_most(_seat_sets(reference, complement.of), math.ceil(complement.total - line) - 1)
    return at_most


def derive_market(market: str, reference: dict[str, dict[str, float]], election: ElectionConfig) -> dict[str, float] | None:
    """{outcome: p} for a market the reference does not list directly, or None if it cannot be derived."""
    spec = parse_market(market)
    if spec.kind == "outcome":
        p = reference.get("winner", {}).get(spec.candidate)
    elif spec.kind == "seats_over":
        p = seats_over(reference, election, spec.candidate, spec.value)
    else:
        return None
    if p is None:
        return None
    p = min(1.0, max(0.0, p))
    yes, no = ("over", "under") if spec.kind == "seats_over" else ("yes", "no")
    return {yes: p, no: 1.0 - p}
