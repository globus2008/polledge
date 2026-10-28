"""Scenario mixing, candidacy risk and the runoff poll-bias correction."""

from datetime import date, timedelta

import numpy as np
import pytest

from polledge.config import ModelParams, RoundConfig, NamedScenario, TableSelector
from polledge.model.markets import market_probabilities, parse_market
from polledge.model.simulate import CandidacyRisk, ElectionStructure, runoff_adjustment_pp, simulate_election
from polledge.polls.parser import assign_scenarios, scenario_name
from polledge.polls.types import Poll

AS_OF = date(2027, 3, 1)


def _poll(pollster, days_ago, shares, scenario="", rnd="first"):
    return Poll("t", rnd, pollster, AS_OF - timedelta(days=days_ago), 1000, shares, scenario=scenario)


def test_assign_scenarios_row_can_belong_to_several():
    rnd = RoundConfig(tables=[TableSelector(heading=["x"])], scenarios=[
        NamedScenario(name="base", weight=0.7, require=["A"], exclude=["C"]),
        NamedScenario(name="wide", weight=0.3, require=["A", "B"]),
    ])
    rows = [_poll("P", 1, {"A": 50, "B": 40, "OTHER": 10}), _poll("Q", 1, {"A": 45, "B": 35, "C": 20})]
    out = assign_scenarios(rows, rnd)
    assert sorted((p.pollster, scenario_name(p)) for p in out) == [("P", "base"), ("P", "wide"), ("Q", "wide")]
    assert out[0].scenario == "base:A|B"


def test_scenarios_are_mixed_by_weight():
    params = ModelParams(seed=1, n_simulations=20_000, sigma_sys_pp=1.0)
    polls = [_poll(f"P{i}", i, {"A": 60, "B": 40}, "s1:A|B") for i in range(5)]
    polls += [_poll(f"P{i}", i, {"A": 30, "B": 70}, "s2:A|B") for i in range(5)]
    structure = ElectionStructure(scenario_weights={"s1": 0.75, "s2": 0.25})
    sim = simulate_election(polls, None, AS_OF, AS_OF, params, two_round=False, structure=structure)
    assert sim.scenario_weights == pytest.approx({"s1": 0.75, "s2": 0.25})
    a = sim.candidates.index("A")
    assert sim.shares[:, a].mean() == pytest.approx(0.75 * 0.60 + 0.25 * 0.30, abs=0.01)
    assert market_probabilities(sim, parse_market("winner"))["A"] == pytest.approx(0.75, abs=0.02)


def test_candidacy_risk_moves_votes_to_substitute():
    params = ModelParams(seed=2, n_simulations=10_000, sigma_sys_pp=1.0)
    polls = [_poll(f"P{i}", i, {"LP": 40, "PH": 35, "ME": 25}) for i in range(5)]
    risk = {"LP": CandidacyRisk(probability=0.7, substitute="JB", transfer=0.9)}
    sim = simulate_election(polls, None, AS_OF, AS_OF, params, two_round=False,
                            structure=ElectionStructure(candidacy=risk))
    lp, jb = sim.candidates.index("LP"), sim.candidates.index("JB")
    out = sim.shares[:, lp] == 0
    assert out.mean() == pytest.approx(0.3, abs=0.02) and sim.absent_share["LP"] == pytest.approx(out.mean())
    assert (sim.shares[out, jb] > 0.3).all() and (sim.shares[~out, jb] == 0).all()
    assert np.allclose(sim.shares.sum(axis=1), 1.0)


def test_runoff_adjustment_pp():
    parties = {"LP": "RN", "JB": "RN"}
    assert runoff_adjustment_pp("LP", "PH", parties, {"RN": -3.0}) == -3.0
    assert runoff_adjustment_pp("PH", "LP", parties, {"RN": -3.0}) == 3.0
    assert runoff_adjustment_pp("PH", "ME", parties, {"RN": -3.0}) == 0.0
    assert runoff_adjustment_pp("LP", "X", {"LP": "RN", "X": "EN"}, {"RN": -3.0, "EN": 1.0}) == -2.0


def test_runoff_bias_lowers_overstated_candidate():
    polls = [_poll(f"P{i}", i, {"LP": 36, "PH": 22, "ME": 18, "OTHER": 24}) for i in range(6)]
    runoff = [_poll(f"P{i}", i, {"LP": 55, "PH": 45}, rnd="runoff") for i in range(6)]
    structure = ElectionStructure(parties={"LP": "RN"})

    def p_lp(bias):
        params = ModelParams(seed=3, n_simulations=10_000, sigma_sys_pp=1.0, sigma_runoff_pp=2.0, runoff_bias_pp=bias)
        sim = simulate_election(polls, runoff, AS_OF, AS_OF, params, two_round=True, structure=structure)
        return market_probabilities(sim, parse_market("winner"))["LP"], sim

    plain, _sim = p_lp({})
    corrected, sim = p_lp({"RN": -4.0})
    assert plain > 0.9 and corrected < 0.75
    assert sim.runoff_adjustments == {"LP|PH": -4.0}


def test_estimate_runoff_bias_by_party():
    from polledge.calibration.tune import estimate_runoff_bias
    from polledge.config import Candidate, ElectionConfig, ElectionResult

    def election(eid, actual_lp, poll_lp):
        cfg = ElectionConfig(
            id=eid, name=eid, type="two_round", election_date=AS_OF, wiki_page="-",
            candidates={"LP": Candidate(name="L P", party="RN"), "MA": Candidate(name="M A")},
            first_round=RoundConfig(tables=[TableSelector(heading=["x"])]),
            result=ElectionResult(first_round={"LP": 30, "MA": 30}, runoff={"LP": actual_lp, "MA": 100 - actual_lp}),
        )
        polls = [Poll(eid, "runoff", f"P{i}", AS_OF - timedelta(days=40 + i), 1000, {"LP": poll_lp, "MA": 100 - poll_lp})
                 for i in range(5)]
        return cfg, polls

    bias = estimate_runoff_bias([election("a", 34, 38), election("b", 41, 43)], ModelParams(), (30,))
    assert bias["RN"] == pytest.approx(-3.0, abs=0.01)  # mean of -4 and -2, one value per election
