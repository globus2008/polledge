"""Notification interface - implementations (e-mail, Telegram, ...) come in a later phase."""

from __future__ import annotations

from abc import ABC, abstractmethod

from polledge.signals.compare import OutcomeComparison


class Notifier(ABC):
    @abstractmethod
    def send(self, election_id: str, signals: list[OutcomeComparison]) -> None:
        """Deliver new value signals to the user. Never places bets."""
