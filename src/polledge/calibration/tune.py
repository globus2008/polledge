"""Grid search of model parameters on past elections with leave-one-election-out validation.

Objective: mean CRPS of first-round vote shares (pp), each election weighted equally.
Averages depend only on (half-life, house effects) and are computed once per
combination; the uncertainty parameters are then varied on top of them.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np

from polledge.calibration.backtest import DEFAULT_HORIZONS, actual_shares, polls_before, require_result
from polledge.calibration.metrics import crps_samples, interval_coverage
from polledge.config import ElectionConfig, ModelParams
from polledge.model.aggregate import PollAverage, weighted_average
from polledge.model.simulate import simulate_shares, target_sigma_pp
from polledge.polls.types import Poll

log = logging.getLogger(__name__)

DEFAULT_GRID = {
    # Below ~3 days the average degenerates to "the last poll", so 3 is a deliberate lower bound.
    "half_life_days": [3, 5, 7, 10, 14, 21],
    "house": [None, 3, 6],  # None = house effects off, else house_min_polls
    "sigma_sys_pp": [1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
    "sigma_horizon_pp_per_sqrt_day": [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0],
}
EDGE_OK = {"half_life_days": "min"}  # an optimum at this edge is accepted, not reported


def grid_edges(best: dict, grid: dict | None = None) -> list[str]:
    """Numeric parameters whose best value lies on the border of the grid (grid may be too small)."""
    grid = grid or DEFAULT_GRID
    edges = []
    for name, values in grid.items():
        numeric = [v for v in values if v is not None]
        if name == "house" or len(numeric) < 2:
            continue
        value = best.get(name)
        if (value == min(numeric) and EDGE_OK.get(name) != "min") or value == max(numeric):
            edges.append(name)
    return edges


@dataclass
class TuneResult:
    best: dict
    best_score: float
    default_score: float
    loeo: dict[str, dict[str, float]]  # election -> {tuned, default, params}
    coverage80: float
    sigma_runoff_pp: float | None
    table: list[tuple[dict, float]] = field(default_factory=list)  # top combinations


def _combo_params(base: ModelParams, combo: dict) -> ModelParams:
    update = {k: v for k, v in combo.items() if k != "house"}
    if combo.get("house") is None:
        update["house_effects"] = False
    else:
        update["house_effects"] = True
        update["house_min_polls"] = combo["house"]
    return base.model_copy(update=update)


def tune(
    elections: list[tuple[ElectionConfig, list[Poll]]],
    base: ModelParams,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    grid: dict | None = None,
    n_sims: int = 2000,
) -> TuneResult:
    grid = grid or DEFAULT_GRID
    for election, _ in elections:
        require_result(election)

    avg_keys = list(itertools.product(grid["half_life_days"], grid["house"]))
    sigma_keys = list(itertools.product(grid["sigma_sys_pp"], grid["sigma_horizon_pp_per_sqrt_day"]))

    # scores[(avg_key, sigma_key)][election_id] = list of CRPS per horizon
    scores: dict[tuple, dict[str, list[float]]] = {}
    coverage: dict[tuple, list[float]] = {}
    for hl, house in avg_keys:
        p_avg = _combo_params(base, {"half_life_days": hl, "house": house})
        cases = _averages(elections, p_avg, horizons)
        for s_sys, s_hor in sigma_keys:
            params = p_avg.model_copy(update={"sigma_sys_pp": s_sys, "sigma_horizon_pp_per_sqrt_day": s_hor})
            key = ((hl, house), (s_sys, s_hor))
            per_election: dict[str, list[float]] = {}
            covs = []
            for election_id, days, avg, actual in cases:
                crps, cov = _score_case(avg, actual, days, params, n_sims)
                per_election.setdefault(election_id, []).append(crps)
                covs.append(cov)
            scores[key] = per_election
            coverage[key] = covs
        log.info("Tuning: half-life %s, house %s done", hl, house)

    def combo_of(key) -> dict:
        (hl, house), (s_sys, s_hor) = key
        return {"half_life_days": hl, "house": house, "sigma_sys_pp": s_sys, "sigma_horizon_pp_per_sqrt_day": s_hor}

    def objective(key, ids) -> float:
        vals = [np.mean(scores[key][e]) for e in ids if e in scores[key]]
        return float(np.mean(vals)) if vals else float("inf")

    all_ids = sorted({e for s in scores.values() for e in s})
    ranked = sorted(scores, key=lambda k: objective(k, all_ids))
    best_key = ranked[0]
    default_key = _default_key(base, scores)

    loeo = {}
    for held_out in all_ids:
        train = [e for e in all_ids if e != held_out]
        if not train:
            break
        k = min(scores, key=lambda key: objective(key, train))
        loeo[held_out] = {
            "tuned": objective(k, [held_out]),
            "default": objective(default_key, [held_out]) if default_key else float("nan"),
            "params": combo_of(k),
        }

    return TuneResult(
        best=combo_of(best_key),
        best_score=objective(best_key, all_ids),
        default_score=objective(default_key, all_ids) if default_key else float("nan"),
        loeo=loeo,
        coverage80=float(np.mean(coverage[best_key])),
        sigma_runoff_pp=estimate_runoff_sigma(elections, _combo_params(base, combo_of(best_key)), horizons),
        table=[(combo_of(k), objective(k, all_ids)) for k in ranked[:10]],
    )


def _default_key(base: ModelParams, scores: dict) -> tuple | None:
    """Grid point closest to the current default parameters (used as the baseline)."""
    house = base.house_min_polls if base.house_effects else None

    def dist(key):
        (hl, h), (s_sys, s_hor) = key
        return (abs(hl - base.half_life_days) + abs(s_sys - base.sigma_sys_pp) * 10
                + abs(s_hor - base.sigma_horizon_pp_per_sqrt_day) * 10 + (0 if h == house else 5))

    return min(scores, key=dist) if scores else None


def _averages(elections, params: ModelParams, horizons) -> list[tuple[str, int, PollAverage, np.ndarray]]:
    cases = []
    for election, polls in elections:
        first = [p for p in polls if p.round == "first"]
        for h in horizons:
            cutoff = election.election_date - timedelta(days=h)
            avg = weighted_average(polls_before(first, cutoff), cutoff - timedelta(days=1), params)
            if avg is None:
                continue
            cases.append((election.id, h, avg, actual_shares(avg.candidates, election.result.first_round)))
    return cases


def _score_case(avg: PollAverage, actual: np.ndarray, days: int, params: ModelParams, n_sims: int) -> tuple[float, float]:
    rng = np.random.default_rng(2024)
    sigma = target_sigma_pp(avg.mean, avg.n_eff, params, days)
    draws = simulate_shares(avg.mean, sigma, n_sims, rng, params.min_share_pct)
    crps = np.mean([crps_samples(draws[:, j], actual[j]) for j in range(len(actual))])
    return 100 * float(crps), float(np.mean(interval_coverage(draws, actual)))


def estimate_runoff_sigma(elections, params: ModelParams, horizons) -> float | None:
    """RMS error of averaged head-to-head polls for the real finalists, minus sampling noise."""
    errors, sampling = [], []
    for election, polls in elections:
        res = election.result
        if res is None or len(res.runoff) != 2:
            continue
        pair = sorted(res.runoff)
        h2h = [p for p in polls if p.round == "runoff" and sorted(p.shares) == pair]
        actual_a = res.runoff[pair[0]] / sum(res.runoff.values())
        for h in horizons:
            cutoff = election.election_date - timedelta(days=h)
            avg = weighted_average(polls_before(h2h, cutoff), cutoff - timedelta(days=1), params)
            if avg is None:
                continue
            m = avg.as_dict()[pair[0]]
            errors.append(actual_a - m)
            sampling.append(m * (1 - m) / avg.n_eff)
    if not errors:
        return None
    var = float(np.mean(np.square(errors)) - np.mean(sampling))
    return 100 * float(np.sqrt(max(var, 0.0)))


def calibration_payload(result: TuneResult, base: ModelParams, election_ids: list[str],
                        runoff_bounds: tuple[float, float] = (2.0, 8.0)) -> dict:
    """Best parameters plus metadata, as stored in the database (app_setting 'calibrated_model')."""
    params = _combo_params(base, result.best)
    out = {
        "half_life_days": params.half_life_days,
        "house_effects": params.house_effects,
        "house_min_polls": params.house_min_polls,
        "sigma_sys_pp": params.sigma_sys_pp,
        "sigma_horizon_pp_per_sqrt_day": params.sigma_horizon_pp_per_sqrt_day,
    }
    if result.sigma_runoff_pp is not None:
        # Very few runoffs to learn from: keep the estimate within sane bounds.
        out["sigma_runoff_pp"] = round(float(np.clip(result.sigma_runoff_pp, *runoff_bounds)), 2)
    return {
        "elections": election_ids,
        "score_crps_pp": round(result.best_score, 4),
        "baseline_crps_pp": round(result.default_score, 4),
        "model": out,
    }
