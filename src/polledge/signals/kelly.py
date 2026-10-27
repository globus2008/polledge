"""Fractional Kelly stake sizing."""

from __future__ import annotations


def kelly_fraction(p: float, odds: float) -> float:
    """Full-Kelly fraction of bankroll: f* = (p*b - (1-p)) / b, with b = odds - 1. Can be negative."""
    if odds <= 1.0:
        raise ValueError("Desetinný kurz musí být > 1")
    if not 0.0 <= p <= 1.0:
        raise ValueError("Pravděpodobnost musí být v intervalu 0–1")
    b = odds - 1.0
    return (p * b - (1.0 - p)) / b


def kelly_stake(
    p: float, odds: float, bankroll: float, fraction: float = 0.25, max_stake_pct: float | None = None
) -> float:
    """Recommended stake; 0 when there is no positive expected value."""
    stake = max(0.0, kelly_fraction(p, odds)) * fraction * bankroll
    if max_stake_pct is not None:
        stake = min(stake, max_stake_pct * bankroll)
    return stake
