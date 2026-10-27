from datetime import datetime

import pytest

from polledge.config import SignalSettings
from polledge.odds.base import OddsQuote
from polledge.odds.margin import latest_books
from polledge.signals.compare import arbitrage_sum, compare_market, edge, is_arbitrage
from polledge.signals.kelly import kelly_fraction, kelly_stake

T = datetime(2026, 10, 27, 10)


def test_edge():
    assert edge(0.5, 2.2) == pytest.approx(0.10)
    assert edge(0.4, 2.0) == pytest.approx(-0.20)


def test_kelly_fraction_and_stake():
    # p = 0.55, odds 2.0 -> b = 1, f* = 0.55 - 0.45 = 0.10
    assert kelly_fraction(0.55, 2.0) == pytest.approx(0.10)
    assert kelly_stake(0.55, 2.0, bankroll=10_000, fraction=0.25) == pytest.approx(250.0)
    # cap at 1 % of bankroll
    assert kelly_stake(0.55, 2.0, bankroll=10_000, fraction=0.25, max_stake_pct=0.01) == pytest.approx(100.0)


def test_kelly_no_bet_without_edge():
    assert kelly_fraction(0.4, 2.0) < 0
    assert kelly_stake(0.4, 2.0, bankroll=10_000) == 0.0


def test_kelly_validates_input():
    with pytest.raises(ValueError):
        kelly_fraction(0.5, 1.0)
    with pytest.raises(ValueError):
        kelly_fraction(1.5, 2.0)


def test_arbitrage_detection():
    assert is_arbitrage({"yes": 2.1, "no": 2.05})  # 0.476 + 0.488 = 0.964
    assert not is_arbitrage({"yes": 1.9, "no": 1.9})
    assert arbitrage_sum({"yes": 2.0, "no": 2.0}) == pytest.approx(1.0)
    assert not is_arbitrage({})


def _quotes(market, rows):
    return [OddsQuote("e", bm, market, outcome, odds, T) for bm, outcome, odds in rows]


def test_compare_market_picks_best_odds_and_signals():
    market = "qualify_runoff:X"
    quotes = _quotes(market, [("A", "yes", 2.0), ("A", "no", 1.8), ("B", "yes", 2.3), ("B", "no", 1.6)])
    settings = SignalSettings(edge_threshold=0.05, kelly_fraction=0.25, bankroll=1000, max_stake_pct=None)
    comp = compare_market(market, {"yes": 0.5, "no": 0.5}, latest_books(quotes), settings)
    yes = next(o for o in comp.outcomes if o.outcome == "yes")
    assert yes.best_odds == 2.3 and yes.best_bookmaker == "B"
    assert yes.edge == pytest.approx(0.15)
    assert yes.is_signal
    assert yes.stake == pytest.approx(0.25 * 1000 * kelly_fraction(0.5, 2.3))
    no = next(o for o in comp.outcomes if o.outcome == "no")
    assert not no.is_signal and no.stake == 0
    # p_market is the average of the two bookmakers' margin-free probabilities
    fair_a = (1 / 2.0) / (1 / 2.0 + 1 / 1.8)
    fair_b = (1 / 2.3) / (1 / 2.3 + 1 / 1.6)
    assert yes.p_market == pytest.approx((fair_a + fair_b) / 2)
    assert comp.margins["A"] == pytest.approx(1 / 2.0 + 1 / 1.8 - 1)
    # best odds 2.3 / 1.8 -> 0.435 + 0.556 = 0.990 -> arbitrage across bookmakers
    assert comp.is_arbitrage


def test_no_arbitrage_if_market_not_covered():
    market = "qualify_runoff:X"
    comp = compare_market(market, {"yes": 0.5, "no": 0.5}, latest_books(_quotes(market, [("A", "yes", 3.0)])),
                          SignalSettings())
    assert comp.arbitrage_sum is None
    assert not comp.is_arbitrage
