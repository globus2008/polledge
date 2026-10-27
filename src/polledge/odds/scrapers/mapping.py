"""Mapping bookmaker market and selection names onto our market ids and candidate ids."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from polledge.config import BookmakerConfig, ElectionConfig
from polledge.model.markets import BINARY_OUTCOMES, parse_market
from polledge.polls.cleaning import normalize_name

log = logging.getLogger(__name__)

# Selection names of binary markets as bookmakers write them (Czech and English),
# compared after normalize_name(), i.e. lower case without diacritics.
BINARY_NAMES = {
    "ano": "yes", "yes": "yes", "ne": "no", "no": "no",
    "vice": "over", "over": "over", "nad": "over",
    "mene": "under", "under": "under", "pod": "under",
}


@dataclass
class RawMarket:
    """A market as the bookmaker shows it."""

    name: str
    selections: list[tuple[str, float]]  # (selection name, decimal odds)


@dataclass
class MappedMarket:
    market: str
    odds: dict[str, float]
    unmatched: list[str] = field(default_factory=list)


class NameMapper:
    def __init__(self, election: ElectionConfig, book: BookmakerConfig):
        self.markets = {normalize_name(k): v for k, v in book.markets.items()}
        for market in self.markets.values():
            parse_market(market)  # fail early on a typo in the config
        self.candidates = {
            normalize_name(alias): cid for cid, cand in election.candidates.items() for alias in cand.all_names()
        }
        self.candidates.update({normalize_name(k): v for k, v in book.selections.items()})

    def map_market(self, raw: RawMarket) -> MappedMarket | None:
        market = self.markets.get(normalize_name(raw.name))
        if market is None:
            return None
        kind = parse_market(market).kind
        mapped = MappedMarket(market, {})
        for name, price in raw.selections:
            if price is None or price <= 1.0:
                continue
            if kind in BINARY_OUTCOMES:
                outcome = BINARY_NAMES.get(normalize_name(name))
            else:
                outcome = self.candidates.get(normalize_name(name))
            if outcome is None:
                # Keep the raw name so the book stays complete for margin removal (model p = 0).
                mapped.unmatched.append(name)
                outcome = name
            mapped.odds[outcome] = price
        if mapped.unmatched:
            log.info("Market %s: selections without candidate id: %s", market, mapped.unmatched)
        return mapped
