"""Odds source interface. File import now; bookmaker scrapers implement the same class later."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OddsQuote:
    election_id: str
    bookmaker: str
    market: str  # market id, see polledge.model.markets
    outcome: str
    odds: float  # decimal odds
    timestamp: datetime

    @property
    def implied_probability(self) -> float:
        return 1.0 / self.odds


class OddsSource(ABC):
    """A provider of odds quotes (a file, later a bookmaker scraper or an API)."""

    name: str = "abstract"

    @abstractmethod
    def fetch(self, election_id: str | None = None) -> list[OddsQuote]:
        """Return quotes, optionally only for one election."""
