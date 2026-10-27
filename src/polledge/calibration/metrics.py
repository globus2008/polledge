"""Scoring rules for calibration: CRPS of vote shares, interval coverage, Brier/log-loss of markets."""

from __future__ import annotations

import numpy as np


def crps_samples(samples: np.ndarray, actual: float) -> float:
    """Continuous ranked probability score from an ensemble: E|X - y| - 0.5 E|X - X'| (lower is better).

    Uses the sorted-sample identity E|X - X'| = 2/n^2 * sum_i (2i - n - 1) x_(i).
    """
    x = np.sort(np.asarray(samples, dtype=float))
    n = len(x)
    term1 = np.mean(np.abs(x - actual))
    i = np.arange(1, n + 1)
    term2 = np.sum((2 * i - n - 1) * x) / (n * n)
    return float(term1 - term2)


def interval_coverage(samples: np.ndarray, actual: np.ndarray, level: float = 0.8) -> np.ndarray:
    """Per column: whether the actual value lies in the central `level` interval of the samples."""
    lo, hi = np.quantile(samples, [(1 - level) / 2, (1 + level) / 2], axis=0)
    return (actual >= lo) & (actual <= hi)


def multiclass_brier(probs: dict[str, float], winner: str) -> float:
    outcomes = set(probs) | {winner}
    return sum((probs.get(o, 0.0) - (1.0 if o == winner else 0.0)) ** 2 for o in outcomes)


def log_loss(p_true_outcome: float, eps: float = 1e-3) -> float:
    return float(-np.log(max(p_true_outcome, eps)))
