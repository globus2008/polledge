"""Monte Carlo simulation of vote shares (logistic-normal) and of the presidential runoff.

Why logistic-normal and not Dirichlet: a Dirichlet distribution has a single
concentration parameter, so the spread of every party is tied to its mean and we
cannot set "2.5 pp systematic error per party". In the logistic-normal model we
perturb log-shares with party-specific noise and map back with softmax, which
keeps every simulated row non-negative and summing exactly to 100 %.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from polledge.config import OTHER, ModelParams
from polledge.model.aggregate import PollAverage, weighted_average
from polledge.polls.types import Poll

log = logging.getLogger(__name__)

_CALIBRATION_DRAWS = 4000
_CALIBRATION_ROUNDS = 6
# Cap on the log-scale sd. Without it, tiny parties (0.2 %) would get an extremely
# heavy right tail (a fringe candidate "reaching" the runoff in some simulations).
_MAX_SIGMA_LOG = 0.8


@dataclass
class SimulationResult:
    candidates: list[str]
    shares: np.ndarray  # (n_sims, k) first round / only round, fractions
    average: PollAverage
    sigma_pp: np.ndarray  # target marginal sd per candidate (pp)
    finalists: np.ndarray | None = None  # (n_sims, 2) candidate indices
    winner: np.ndarray | None = None  # (n_sims,) candidate index
    runoff_share: np.ndarray | None = None  # (n_sims,) share of finalists[:, 0] in runoff
    runoff_sources: dict[str, str] = field(default_factory=dict)  # "A|B" -> "polls" | "fallback"

    @property
    def n_sims(self) -> int:
        return self.shares.shape[0]

    def ranking_shares(self) -> np.ndarray:
        """Shares used for ranking: the OTHER bucket can never win or qualify."""
        ranking = self.shares.copy()
        if OTHER in self.candidates:
            ranking[:, self.candidates.index(OTHER)] = -1.0
        return ranking

    def index(self, candidate: str) -> int:
        try:
            return self.candidates.index(candidate)
        except ValueError:
            raise KeyError(f"Kandidát '{candidate}' není v modelu (nejsou pro něj průzkumy)") from None


def target_sigma_pp(mean: np.ndarray, n_eff: float, params: ModelParams, days_to_election: int) -> np.ndarray:
    """Marginal sd per party in pp: systematic + horizon + sampling error of the average."""
    p_pct = mean * 100.0
    horizon = params.sigma_horizon_pp_per_sqrt_day * np.sqrt(max(days_to_election, 0))
    # Systematic and campaign (horizon) error, shrunk for small parties.
    poll_error = np.full_like(p_pct, np.hypot(params.sigma_sys_pp, horizon))
    if params.small_party_ref_pct > 0:
        poll_error *= np.sqrt(np.minimum(1.0, p_pct / params.small_party_ref_pct))
    sampling = 100.0 * np.sqrt(mean * (1 - mean) / max(n_eff, 1.0))
    return np.sqrt(poll_error**2 + sampling**2)


def _softmax_draws(log_mean: np.ndarray, sigma_log: np.ndarray, z: np.ndarray) -> np.ndarray:
    x = log_mean + z * sigma_log
    x -= x.max(axis=1, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=1, keepdims=True)


def simulate_shares(
    mean: np.ndarray, sigma_pp: np.ndarray, n_sims: int, rng: np.random.Generator, min_share_pct: float = 0.1
) -> np.ndarray:
    """Draw share vectors whose marginal mean and sd approximate `mean` and `sigma_pp`, summing to 1.

    Starts from the delta-method approximation sigma_log = sigma / p. Because
    E[softmax(x)] != softmax(E[x]) under large noise, both the log-location and
    the log-scale are then corrected a few times on a fixed calibration sample.
    """
    p = np.clip(mean, min_share_pct / 100.0, None)
    p = p / p.sum()
    log_mean = np.log(p)
    target = sigma_pp / 100.0
    sigma_log = np.minimum(target / p, _MAX_SIGMA_LOG)

    calib_rng = np.random.default_rng(12345)
    z_cal = calib_rng.standard_normal((_CALIBRATION_DRAWS, len(p)))
    for _ in range(_CALIBRATION_ROUNDS):
        draws = _softmax_draws(log_mean, sigma_log, z_cal)
        log_mean += np.log(p / np.clip(draws.mean(axis=0), 1e-12, None))
        realised = _softmax_draws(log_mean, sigma_log, z_cal).std(axis=0)
        sigma_log *= np.where(realised > 0, target / realised, 1.0)
        sigma_log = np.minimum(sigma_log, _MAX_SIGMA_LOG)
    draws = _softmax_draws(log_mean, sigma_log, z_cal)
    log_mean += np.log(p / np.clip(draws.mean(axis=0), 1e-12, None))

    return _softmax_draws(log_mean, sigma_log, rng.standard_normal((n_sims, len(p))))


def simulate_election(
    first_round: list[Poll],
    runoff_polls: list[Poll] | None,
    as_of: date,
    election_date: date,
    params: ModelParams,
    two_round: bool,
    n_sims: int | None = None,
) -> SimulationResult | None:
    avg = weighted_average(first_round, as_of, params)
    if avg is None:
        return None
    rng = np.random.default_rng(params.seed)
    n_sims = n_sims or params.n_simulations
    sigma = target_sigma_pp(avg.mean, avg.n_eff, params, (election_date - as_of).days)
    shares = simulate_shares(avg.mean, sigma, n_sims, rng, params.min_share_pct)
    result = SimulationResult(avg.candidates, shares, avg, sigma)

    if two_round:
        horizon_pp = params.sigma_horizon_pp_per_sqrt_day * np.sqrt(max((election_date - as_of).days, 0))
        _simulate_runoff(result, runoff_polls or [], as_of, params, rng, horizon_pp)
    else:
        result.winner = result.ranking_shares().argmax(axis=1)
    return result


def _simulate_runoff(
    result: SimulationResult,
    runoff_polls: list[Poll],
    as_of: date,
    params: ModelParams,
    rng: np.random.Generator,
    horizon_pp: float = 0.0,
) -> None:
    shares = result.shares
    ranking = result.ranking_shares()
    order = np.argsort(-ranking, axis=1)
    finalists = np.sort(order[:, :2], axis=1)  # canonical order: lower index first
    result.finalists = finalists
    n = shares.shape[0]
    runoff_share = np.full(n, np.nan)
    winner = np.empty(n, dtype=int)

    by_pair: dict[str, list[Poll]] = {}
    for p in runoff_polls:
        by_pair.setdefault("|".join(sorted(p.shares)), []).append(p)

    pair_codes = finalists[:, 0] * len(result.candidates) + finalists[:, 1]
    for code in np.unique(pair_codes):
        rows = np.nonzero(pair_codes == code)[0]
        a, b = divmod(int(code), len(result.candidates))
        ca, cb = result.candidates[a], result.candidates[b]
        key = "|".join(sorted([ca, cb]))
        avg = weighted_average(by_pair.get(key, []), as_of, params)
        if avg is not None and set(avg.candidates) == {ca, cb}:
            mean_a = avg.as_dict()[ca]
            sampling_pp = 100.0 * np.sqrt(mean_a * (1 - mean_a) / avg.n_eff)
            sigma = np.sqrt(params.sigma_runoff_pp**2 + horizon_pp**2 + sampling_pp**2) / 100.0
            draws = mean_a + sigma * rng.standard_normal(len(rows))
            result.runoff_sources[key] = "polls"
        else:
            # No head-to-head polls: use the first-round ratio of the two finalists, with wider error.
            sa, sb = shares[rows, a], shares[rows, b]
            sigma = np.hypot(params.sigma_runoff_fallback_pp, horizon_pp) / 100.0
            draws = sa / (sa + sb) + sigma * rng.standard_normal(len(rows))
            result.runoff_sources[key] = "fallback"
            if len(rows) / n >= 0.01:
                log.warning("No runoff polls for %s vs %s (%.0f %% of sims), using first-round fallback",
                            ca, cb, 100 * len(rows) / n)
        draws = np.clip(draws, 0.0, 1.0)
        runoff_share[rows] = draws
        winner[rows] = np.where(draws > 0.5, a, b)

    # An absolute majority in the first round wins outright.
    outright = ranking.max(axis=1) > 0.5
    winner[outright] = order[outright, 0]
    result.runoff_share = runoff_share
    result.winner = winner
