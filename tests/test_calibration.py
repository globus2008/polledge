from datetime import date, timedelta

import numpy as np
import pytest

from polledge.calibration.backtest import actual_shares, evaluate_election, polls_before
from polledge.calibration.metrics import crps_samples
from polledge.calibration.tune import tune
from polledge.config import Candidate, ElectionConfig, ElectionResult, ModelParams, RoundConfig, TableSelector
from polledge.polls.types import Poll

ELECTION_DAY = date(2025, 6, 1)


def test_crps_of_point_mass_is_absolute_error():
    assert crps_samples(np.full(100, 0.3), 0.25) == pytest.approx(0.05)
    # A spread ensemble centred on the truth beats a sharp but biased one.
    rng = np.random.default_rng(0)
    assert crps_samples(rng.normal(0.3, 0.02, 2000), 0.3) < crps_samples(np.full(2000, 0.33), 0.3)


def test_lookahead_guard():
    polls = [Poll("e", "first", "X", ELECTION_DAY - timedelta(days=d), 1000, {"A": 50, "B": 50}) for d in (1, 10, 30)]
    visible = polls_before(polls, ELECTION_DAY - timedelta(days=10))
    assert [p.end_date for p in visible] == [ELECTION_DAY - timedelta(days=30)]


def test_actual_shares_assigns_rest_to_other():
    vec = actual_shares(["A", "B", "OTHER"], {"A": 40.0, "B": 35.0, "C": 5.0})
    assert vec.tolist() == pytest.approx([0.40, 0.35, 0.25])


def _synthetic_election(eid: str, true_sigma_pp: float, seed: int) -> tuple[ElectionConfig, list[Poll]]:
    """Polls are unbiased noisy measurements of a 'campaign mean'; the result deviates by true_sigma."""
    rng = np.random.default_rng(seed)
    mean = np.array([0.40, 0.30, 0.20, 0.10])
    result = np.clip(mean + rng.normal(0, true_sigma_pp / 100, 4), 0.01, None)
    result = 100 * result / result.sum()
    polls = []
    for d in range(2, 120, 2):
        for pollster in ("P1", "P2"):
            noisy = np.clip(mean + rng.normal(0, np.sqrt(mean * (1 - mean) / 1000)), 0.001, None)
            noisy = 100 * noisy / noisy.sum()
            polls.append(Poll(eid, "first", pollster, ELECTION_DAY - timedelta(days=d), 1000,
                              dict(zip("ABCD", noisy.tolist()))))
    cfg = ElectionConfig(
        id=eid, name=eid, election_date=ELECTION_DAY, wiki_page="-",
        candidates={c: Candidate(name=c) for c in "ABCD"},
        first_round=RoundConfig(tables=[TableSelector(heading=["x"])]),
        result=ElectionResult(first_round=dict(zip("ABCD", result.tolist()))),
    )
    return cfg, polls


def test_tune_recovers_systematic_error():
    data = [_synthetic_election(f"s{i}", 3.0, seed=i) for i in range(12)]
    grid = {"half_life_days": [14], "house": [None], "sigma_sys_pp": [0.5, 3.0, 8.0],
            "sigma_horizon_pp_per_sqrt_day": [0.0]}
    res = tune(data, ModelParams(small_party_ref_pct=0), horizons=(30,), grid=grid, n_sims=1000)
    assert res.best["sigma_sys_pp"] == 3.0
    assert len(res.loeo) == 12


def test_evaluate_election_reports_each_horizon():
    cfg, polls = _synthetic_election("s", 2.0, seed=1)
    scores = evaluate_election(cfg, polls, ModelParams(seed=1), horizons=(60, 7), n_sims=1000)
    assert [s.horizon for s in scores] == [60, 7]
    for s in scores:
        assert 0 <= s.coverage80 <= 1 and s.crps_pp > 0 and 0 <= s.p_actual_winner <= 1
