"""Notification interface. Implementations live in polledge.notify.channels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    """A new value signal, already labelled for humans."""

    market: str
    market_label: str
    outcome: str
    outcome_label: str
    bookmaker: str
    odds: float
    p_model: float
    edge: float
    stake: float
    currency: str


class Notifier(ABC):
    @abstractmethod
    def send(self, election_name: str, signals: list[Signal]) -> None:
        """Deliver new value signals to the user. Never places bets."""
