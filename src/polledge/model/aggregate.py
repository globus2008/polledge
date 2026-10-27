"""Poll averaging: recency and sample-size weights, house-effect correction."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from polledge.config import ModelParams
from polledge.polls.types import Poll

log = logging.getLogger(__name__)


@dataclass
class PollAverage:
    candidates: list[str]
    mean: np.ndarray  # fractions, sums to 1
    n_eff: float  # effective sample size of the weighted average
    n_polls: int
    as_of: date
    house_effects: dict[str, dict[str, float]] = field(default_factory=dict)  # pollster -> cand -> pp

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.candidates, self.mean.tolist()))


def collapse_scenarios(polls: list[Poll]) -> list[Poll]:
    """One record per (pollster, end date): average of its scenario rows (missing candidate = 0)."""
    groups: dict[tuple, list[Poll]] = defaultdict(list)
    for p in polls:
        groups[(p.pollster, p.end_date)].append(p)
    out = []
    for (pollster, end), group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        keys = set().union(*(p.shares for p in group))
        shares = {k: float(np.mean([p.shares.get(k, 0.0) for p in group])) for k in keys}
        sample = int(np.mean([p.sample_size for p in group]))
        out.append(
            Poll(group[0].election_id, group[0].round, pollster, end, sample, shares, scenario="*merged*")
        )
    return out


def poll_weights(ages_days: np.ndarray, samples: np.ndarray, half_life_days: float) -> np.ndarray:
    """w = 0.5^(age / half_life) * sqrt(n)."""
    return np.power(0.5, ages_days / half_life_days) * np.sqrt(samples)


def weighted_average(polls: list[Poll], as_of: date, params: ModelParams) -> PollAverage | None:
    usable = [
        p for p in polls if p.end_date <= as_of and (as_of - p.end_date).days <= params.max_poll_age_days
    ]
    usable = collapse_scenarios(usable)
    if not usable:
        return None

    candidates = sorted(set().union(*(p.shares for p in usable)))
    X = np.array([[p.shares.get(c, 0.0) for c in candidates] for p in usable], dtype=float)
    X = 100.0 * X / X.sum(axis=1, keepdims=True)
    ages = np.array([(as_of - p.end_date).days for p in usable], dtype=float)
    n = np.array([p.sample_size for p in usable], dtype=float)
    w = poll_weights(ages, n, params.half_life_days)

    mean = w @ X / w.sum()
    house: dict[str, dict[str, float]] = {}
    if params.house_effects:
        X, house = _correct_house_effects(X, w, [p.pollster for p in usable], candidates, mean, params)
        mean = w @ X / w.sum()

    mean = np.clip(mean, 0.0, None)
    mean = mean / mean.sum()
    n_eff = float(w.sum() ** 2 / np.sum(w**2 / n))
    return PollAverage(candidates, mean, n_eff, len(usable), as_of, house)


def _correct_house_effects(
    X: np.ndarray,
    w: np.ndarray,
    pollsters: list[str],
    candidates: list[str],
    mean: np.ndarray,
    params: ModelParams,
) -> tuple[np.ndarray, dict[str, dict[str, float]]]:
    """Subtract each pollster's weighted mean deviation from the overall average.

    Only pollsters with at least `house_min_polls` polls are corrected; with
    `house_shrinkage` = m > 0 the effect is shrunk by k / (k + m).
    """
    X = X.copy()
    effects: dict[str, dict[str, float]] = {}
    idx_by_pollster: dict[str, list[int]] = defaultdict(list)
    for i, name in enumerate(pollsters):
        idx_by_pollster[name].append(i)
    # A house effect is only defined relative to other pollsters.
    if len(idx_by_pollster) < 2:
        return X, effects
    for name, idx in idx_by_pollster.items():
        k = len(idx)
        if k < params.house_min_polls:
            continue
        wi = w[idx]
        effect = (wi @ (X[idx] - mean)) / wi.sum()
        if params.house_shrinkage > 0:
            effect *= k / (k + params.house_shrinkage)
        X[idx] -= effect
        effects[name] = dict(zip(candidates, effect.tolist()))
    X = np.clip(X, 0.0, None)
    X = 100.0 * X / X.sum(axis=1, keepdims=True)
    return X, effects
