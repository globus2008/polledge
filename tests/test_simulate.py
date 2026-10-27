from datetime import date, timedelta

import numpy as np
import pytest

from polledge.config import ModelParams
from polledge.model.aggregate import poll_weights, weighted_average
from polledge.model.markets import market_probabilities, parse_market
from polledge.model.simulate import simulate_election, simulate_shares, target_sigma_pp
from polledge.polls.types import Poll

AS_OF = date(2027, 3, 1)


def _poll(pollster, days_ago, shares, n=1000, rnd="first"):
    return Poll("test", rnd, pollster, AS_OF - timedelta(days=days_ago), n, shares)


def test_simulated_shares_sum_to_one_and_nonnegative():
    mean = np.array([0.33, 0.18, 0.15, 0.14, 0.08, 0.05, 0.04, 0.02, 0.01])
    mean = mean / mean.sum()
    sigma = np.full(len(mean), 2.5)
    draws = simulate_shares(mean, sigma, 10_000, np.random.default_rng(1))
    assert draws.shape == (10_000, len(mean))
    assert np.allclose(draws.sum(axis=1), 1.0, atol=1e-9)
    assert (draws >= 0).all()


def test_simulated_spread_matches_target():
    mean = np.array([0.40, 0.35, 0.25])
    sigma = np.array([2.5, 2.5, 2.5])
    draws = simulate_shares(mean, sigma, 20_000, np.random.default_rng(2))
    assert np.allclose(draws.mean(axis=0), mean, atol=0.005)
    assert np.allclose(draws.std(axis=0) * 100, sigma, rtol=0.1)


def test_mean_preserved_under_large_uncertainty():
    # softmax of noisy logs is biased; the location correction must keep the mean.
    mean = np.array([0.34, 0.17, 0.16, 0.14, 0.08, 0.04, 0.03, 0.02, 0.02])
    mean = mean / mean.sum()
    sigma = np.array([6.8, 6.8, 6.8, 6.8, 6.0, 4.4, 3.6, 3.0, 3.0])
    draws = simulate_shares(mean, sigma, 20_000, np.random.default_rng(3))
    assert np.allclose(draws.mean(axis=0), mean, atol=0.004)
    assert draws[:, 0].std() * 100 == pytest.approx(6.8, rel=0.1)


def test_small_party_sigma_is_scaled_down():
    params = ModelParams(sigma_sys_pp=2.5, small_party_ref_pct=10)
    sigma = target_sigma_pp(np.array([0.30, 0.025]), n_eff=1e9, params=params, days_to_election=0)
    assert sigma[0] == pytest.approx(2.5)
    assert sigma[1] == pytest.approx(2.5 * 0.5)


def test_weights_halve_after_half_life():
    w = poll_weights(np.array([0.0, 14.0]), np.array([1000.0, 1000.0]), 14.0)
    assert w[1] / w[0] == pytest.approx(0.5)
    w = poll_weights(np.array([0.0, 0.0]), np.array([400.0, 1600.0]), 14.0)
    assert w[1] / w[0] == pytest.approx(2.0)


def test_house_effect_is_removed():
    params = ModelParams(house_min_polls=3)
    polls = []
    for d in range(0, 30, 3):
        polls.append(_poll("Neutral", d, {"A": 40, "B": 60}))
        polls.append(_poll("Biased", d + 1, {"A": 46, "B": 54}))
    with_he = weighted_average(polls, AS_OF, params)
    without = weighted_average(polls, AS_OF, params.model_copy(update={"house_effects": False}))
    assert without.as_dict()["A"] == pytest.approx(0.43, abs=0.005)
    # Both pollsters are corrected towards the common mean, the gap between them disappears.
    assert with_he.house_effects["Biased"]["A"] - with_he.house_effects["Neutral"]["A"] == pytest.approx(6.0, abs=0.3)


def test_house_effects_need_minimum_polls():
    params = ModelParams(house_min_polls=5)
    polls = [_poll("X", d, {"A": 40, "B": 60}) for d in range(3)] + [_poll("Y", d, {"A": 50, "B": 50}) for d in range(3)]
    assert weighted_average(polls, AS_OF, params).house_effects == {}


def test_future_and_stale_polls_ignored():
    params = ModelParams(max_poll_age_days=60)
    polls = [_poll("X", 10, {"A": 50, "B": 50}), _poll("X", -5, {"A": 90, "B": 10}), _poll("X", 100, {"A": 10, "B": 90})]
    avg = weighted_average(polls, AS_OF, params)
    assert avg.n_polls == 1
    assert avg.as_dict()["A"] == pytest.approx(0.5)


def _two_round_sim(runoff=True):
    params = ModelParams(seed=7, n_simulations=5000)
    first = [_poll(f"P{i}", i, {"LP": 34, "PH": 18, "ME": 16, "GL": 13, "OTHER": 19}) for i in range(6)]
    run = [_poll(f"P{i}", i, {"LP": 55, "PH": 45}, rnd="runoff") for i in range(4)] if runoff else []
    return simulate_election(first, run, AS_OF, date(2027, 4, 18), params, two_round=True)


def test_two_round_markets():
    sim = _two_round_sim()
    winner = market_probabilities(sim, parse_market("winner"))
    assert sum(winner.values()) == pytest.approx(1.0)
    assert winner["LP"] > 0.5
    qualify = market_probabilities(sim, parse_market("qualify_runoff:LP"))
    assert qualify["yes"] > 0.99
    # Exactly two finalists in every simulation.
    total_qualify = sum(market_probabilities(sim, parse_market(f"qualify_runoff:{c}"))["yes"] for c in sim.candidates)
    assert total_qualify == pytest.approx(2.0)
    assert sim.runoff_sources["LP|PH"] == "polls"
    finalists = market_probabilities(sim, parse_market("finalists"))
    assert sum(finalists.values()) == pytest.approx(1.0)


def test_runoff_fallback_without_h2h_polls():
    sim = _two_round_sim(runoff=False)
    assert set(sim.runoff_sources.values()) == {"fallback"}
    assert market_probabilities(sim, parse_market("winner"))["LP"] > 0.5


def test_threshold_and_over_under():
    params = ModelParams(seed=3, n_simulations=5000)
    polls = [_poll("X", i, {"A": 45, "B": 50, "C": 5}) for i in range(5)]
    sim = simulate_election(polls, None, AS_OF, AS_OF, params, two_round=False)
    thr = market_probabilities(sim, parse_market("threshold:C:5"))
    assert 0.3 < thr["yes"] < 0.7
    over = market_probabilities(sim, parse_market("share_over:A:40"))
    assert over["over"] > 0.9 and over["over"] + over["under"] == pytest.approx(1.0)


@pytest.mark.parametrize("bad", ["foo", "winner:X", "threshold:A", "threshold:A:abc", "qualify_runoff"])
def test_parse_market_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_market(bad)
