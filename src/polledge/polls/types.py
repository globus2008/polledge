from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Poll:
    """One poll (one scenario row) with shares in percent, keyed by candidate id."""

    election_id: str
    round: str  # "first" | "runoff"
    pollster: str
    end_date: date
    sample_size: int
    shares: dict[str, float]
    scenario: str = ""
    sample_imputed: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        return (self.election_id, self.round, self.pollster, self.end_date, self.scenario)
