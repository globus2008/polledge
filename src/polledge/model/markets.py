"""Betting markets and their probabilities computed from a simulation.

Market identifiers (used in odds files too):
    winner                     outcome = candidate id (overall winner)
    first_round_winner         outcome = candidate id (most votes in round 1)
    finalists                  outcome = "A|B" (sorted ids of the runoff pair)
    qualify_runoff:<ID>        outcome = yes | no
    threshold:<ID>:<pct>       outcome = yes | no   (share >= pct)
    share_over:<ID>:<pct>      outcome = over | under (share > pct)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from polledge.model.simulate import SimulationResult

BINARY_OUTCOMES = {
    "qualify_runoff": ("yes", "no"),
    "threshold": ("yes", "no"),
    "share_over": ("over", "under"),
}
MARKET_TYPES = {"winner", "first_round_winner", "finalists", *BINARY_OUTCOMES}


@dataclass(frozen=True)
class MarketSpec:
    kind: str
    candidate: str | None = None
    value: float | None = None

    @property
    def id(self) -> str:
        parts = [self.kind]
        if self.candidate is not None:
            parts.append(self.candidate)
        if self.value is not None:
            parts.append(f"{self.value:g}")
        return ":".join(parts)

    def describe(self, names: dict[str, str] | None = None) -> str:
        names = names or {}
        cand = names.get(self.candidate, self.candidate) if self.candidate else ""
        if self.kind == "winner":
            return "Vítěz voleb"
        if self.kind == "first_round_winner":
            return "Vítěz 1. kola"
        if self.kind == "finalists":
            return "Dvojice ve 2. kole"
        if self.kind == "qualify_runoff":
            return f"{cand}: postup do 2. kola"
        value = f"{self.value:g}".replace(".", ",")
        if self.kind == "threshold":
            return f"{cand}: alespoň {value} %"
        return f"{cand}: podíl nad/pod {value} %"


def parse_market(market: str) -> MarketSpec:
    parts = market.strip().split(":")
    kind = parts[0]
    if kind not in MARKET_TYPES:
        raise ValueError(f"Neznámý typ trhu '{kind}' (povolené: {', '.join(sorted(MARKET_TYPES))})")
    if kind in ("winner", "first_round_winner", "finalists"):
        if len(parts) != 1:
            raise ValueError(f"Trh '{market}' nemá mít další parametry")
        return MarketSpec(kind)
    if kind == "qualify_runoff":
        if len(parts) != 2:
            raise ValueError(f"Trh '{market}': očekávám qualify_runoff:<ID>")
        return MarketSpec(kind, parts[1])
    if len(parts) != 3:
        raise ValueError(f"Trh '{market}': očekávám {kind}:<ID>:<procenta>")
    try:
        value = float(parts[2].replace(",", "."))
    except ValueError:
        raise ValueError(f"Trh '{market}': '{parts[2]}' není číslo") from None
    return MarketSpec(kind, parts[1], value)


def market_probabilities(sim: SimulationResult, spec: MarketSpec) -> dict[str, float]:
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
            raise ValueError("Trh 'finalists' je jen pro dvoukolové volby")
        codes, counts = np.unique(sim.finalists[:, 0] * k + sim.finalists[:, 1], return_counts=True)
        return {
            "|".join(sorted([sim.candidates[c // k], sim.candidates[c % k]])): n / sim.n_sims
            for c, n in zip(codes.tolist(), counts.tolist())
        }

    i = sim.index(spec.candidate)
    yes_label, no_label = BINARY_OUTCOMES[spec.kind]
    if spec.kind == "qualify_runoff":
        if sim.finalists is None:
            raise ValueError("Trh 'qualify_runoff' je jen pro dvoukolové volby")
        p = float(np.mean((sim.finalists == i).any(axis=1)))
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
