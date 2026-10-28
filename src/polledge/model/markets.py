"""Betting markets and their probabilities computed from a simulation.

Market identifiers (used in odds files too):
    winner                     outcome = candidate id (overall winner)
    first_round_winner         outcome = candidate id (most votes in round 1)
    finalists                  outcome = "A|B" (sorted ids of the runoff pair)
    qualify_runoff:<ID>        outcome = yes | no
    threshold:<ID>:<pct>       outcome = yes | no   (share >= pct)
    share_over:<ID>:<pct>      outcome = over | under (share > pct)
    outcome:<ID>               outcome = yes | no   (<ID> wins the "winner" market, e.g. "Democrats sweep")
    seats:<ID>[:<tag>]         outcome = seat bracket as listed ("≤47", "48", "190-194", "230+");
                               a reference-market distribution only, the input for seats_over
    seats_over:<ID>:<n>        outcome = over | under (seats > n)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from polledge.model.simulate import SimulationResult
from polledge.i18n import _, decimal

BINARY_OUTCOMES = {
    "qualify_runoff": ("yes", "no"),
    "threshold": ("yes", "no"),
    "share_over": ("over", "under"),
    "outcome": ("yes", "no"),
    "seats_over": ("over", "under"),
}
MARKET_TYPES = {"winner", "first_round_winner", "finalists", "seats", *BINARY_OUTCOMES}
# Only a reference market (e.g. Polymarket brackets) provides these; the poll model does not.
REFERENCE_ONLY = {"seats", "seats_over"}


@dataclass(frozen=True)
class MarketSpec:
    kind: str
    candidate: str | None = None
    value: float | None = None
    tag: str | None = None  # seats:<ID>:<tag> - several bracket sets of one party (e.g. "low")

    @property
    def id(self) -> str:
        parts = [self.kind]
        if self.candidate is not None:
            parts.append(self.candidate)
        if self.value is not None:
            parts.append(f"{self.value:g}")
        if self.tag is not None:
            parts.append(self.tag)
        return ":".join(parts)

    def describe(self, names: dict[str, str] | None = None) -> str:
        names = names or {}
        cand = names.get(self.candidate, self.candidate) if self.candidate else ""
        if self.kind == "winner":
            return _("Election winner")
        if self.kind == "first_round_winner":
            return _("First-round winner")
        if self.kind == "finalists":
            return _("Runoff pair")
        if self.kind == "qualify_runoff":
            return _("{candidate}: qualifies for the runoff", candidate=cand)
        if self.kind == "outcome":
            return _("{candidate}: yes/no", candidate=cand)
        if self.kind == "seats":
            return _("{candidate}: number of seats", candidate=cand) + (f" ({self.tag})" if self.tag else "")
        value = decimal(f"{self.value:g}")
        if self.kind == "threshold":
            return _("{candidate}: at least {value} %", candidate=cand, value=value)
        if self.kind == "seats_over":
            return _("{candidate}: seats over/under {value}", candidate=cand, value=value)
        return _("{candidate}: share over/under {value} %", candidate=cand, value=value)


def parse_market(market: str) -> MarketSpec:
    parts = market.strip().split(":")
    kind = parts[0]
    if kind not in MARKET_TYPES:
        raise ValueError(_("Unknown market type '{kind}' (allowed: {allowed})", kind=kind, allowed=", ".join(sorted(MARKET_TYPES))))
    if kind in ("winner", "first_round_winner", "finalists"):
        if len(parts) != 1:
            raise ValueError(_("Market '{market}' takes no parameters", market=market))
        return MarketSpec(kind)
    if kind in ("qualify_runoff", "outcome"):
        if len(parts) != 2:
            raise ValueError(_("Market '{market}': expected {kind}:<ID>", market=market, kind=kind))
        return MarketSpec(kind, parts[1])
    if kind == "seats":
        if len(parts) not in (2, 3):
            raise ValueError(_("Market '{market}': expected seats:<ID> or seats:<ID>:<tag>", market=market))
        return MarketSpec(kind, parts[1], tag=parts[2] if len(parts) == 3 else None)
    if len(parts) != 3:
        raise ValueError(_("Market '{market}': expected {kind}:<ID>:<percent>", market=market, kind=kind))
    try:
        value = float(parts[2].replace(",", "."))
    except ValueError:
        raise ValueError(_("Market '{market}': '{value}' is not a number", market=market, value=parts[2])) from None
    return MarketSpec(kind, parts[1], value)


def market_probabilities(sim: SimulationResult, spec: MarketSpec) -> dict[str, float]:
    if spec.kind in REFERENCE_ONLY:
        raise ValueError(_("Market '{market}' needs a reference market, the poll model does not model seats",
                           market=spec.id))
    k = len(sim.candidates)
    if spec.kind in ("winner", "first_round_winner"):
        if spec.kind == "first_round_winner" or sim.winner is None:
            idx = sim.ranking_shares().argmax(axis=1)
        else:
            idx = sim.winner
        counts = np.bincount(idx, minlength=k) / sim.n_sims
        return dict(zip(sim.candidates, counts.tolist()))
    if spec.kind == "finalists":
        if sim.finalists is None:
            raise ValueError(_("Market '{market}' only exists in two-round elections", market="finalists"))
        codes, counts = np.unique(sim.finalists[:, 0] * k + sim.finalists[:, 1], return_counts=True)
        return {
            "|".join(sorted([sim.candidates[c // k], sim.candidates[c % k]])): n / sim.n_sims
            for c, n in zip(codes.tolist(), counts.tolist())
        }

    i = sim.index(spec.candidate)
    yes_label, no_label = BINARY_OUTCOMES[spec.kind]
    if spec.kind == "qualify_runoff":
        if sim.finalists is None:
            raise ValueError(_("Market '{market}' only exists in two-round elections", market="qualify_runoff"))
        p = float(np.mean((sim.finalists == i).any(axis=1)))
    elif spec.kind == "outcome":
        winner = sim.winner if sim.winner is not None else sim.ranking_shares().argmax(axis=1)
        p = float(np.mean(winner == i))
    elif spec.kind == "threshold":
        p = float(np.mean(sim.shares[:, i] * 100 >= spec.value))
    else:
        p = float(np.mean(sim.shares[:, i] * 100 > spec.value))
    return {yes_label: p, no_label: 1.0 - p}


def default_markets(sim: SimulationResult, two_round: bool, thresholds: tuple[float, ...] = (5.0,)) -> list[MarketSpec]:
    specs = [MarketSpec("winner")]
    if two_round:
        specs.append(MarketSpec("first_round_winner"))
        specs += [MarketSpec("qualify_runoff", c) for c in sim.candidates if c != "OTHER"]
    else:
        specs += [MarketSpec("threshold", c, t) for c in sim.candidates if c != "OTHER" for t in thresholds]
    return specs
