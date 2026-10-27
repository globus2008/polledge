from datetime import date, datetime

import pytest

from polledge.config import ElectionResult, SignalSettings
from polledge.db import PaperBetRow, make_session_factory
from polledge.journal.paper import record_signals, record_snapshot, settle, winning_outcomes
from polledge.odds.base import OddsQuote
from polledge.odds.margin import latest_books
from polledge.polls.repository import load_polls, upsert_polls
from polledge.polls.types import Poll
from polledge.signals.compare import compare_market

RESULT = ElectionResult(
    first_round={"A": 30.0, "B": 25.0, "C": 20.0, "D": 4.0},
    runoff={"A": 45.0, "B": 55.0},
)


@pytest.fixture
def session():
    factory = make_session_factory(":memory:")
    with factory() as s:
        yield s


def test_winning_outcomes():
    assert winning_outcomes("winner", RESULT) == {"B"}
    assert winning_outcomes("first_round_winner", RESULT) == {"A"}
    assert winning_outcomes("finalists", RESULT) == {"A|B"}
    assert winning_outcomes("qualify_runoff:C", RESULT) == {"no"}
    assert winning_outcomes("threshold:D:5", RESULT) == {"no"}
    assert winning_outcomes("share_over:A:29.5", RESULT) == {"over"}
    assert winning_outcomes("threshold:Z:5", RESULT) is None  # candidate did not run -> void


def test_poll_dedup(session):
    p = Poll("e", "first", "Ifop", date(2026, 10, 1), 1000, {"A": 50.0, "B": 50.0}, scenario="A|B")
    assert upsert_polls(session, [p]) == (1, 0)
    assert upsert_polls(session, [p]) == (0, 0)
    changed = Poll("e", "first", "Ifop", date(2026, 10, 1), 1000, {"A": 52.0, "B": 48.0}, scenario="A|B")
    assert upsert_polls(session, [changed]) == (0, 1)
    (loaded,) = load_polls(session, "e")
    assert loaded.shares == {"A": 52.0, "B": 48.0}


def test_stale_scenario_replaced(session):
    old = Poll("e", "first", "Ifop", date(2026, 10, 1), 1000, {"A": 50.0, "OTHER": 50.0}, scenario="A")
    other_poll = Poll("e", "first", "Elabe", date(2026, 9, 1), 1000, {"A": 40.0, "B": 60.0}, scenario="A|B")
    upsert_polls(session, [old, other_poll])
    # After a config change the same poll is parsed with a different line-up key.
    new = Poll("e", "first", "Ifop", date(2026, 10, 1), 1000, {"A": 50.0, "B": 50.0}, scenario="A|B")
    upsert_polls(session, [new])
    polls = {(p.pollster, p.scenario) for p in load_polls(session, "e")}
    assert polls == {("Ifop", "A|B"), ("Elabe", "A|B")}  # stale Ifop row gone, unrelated poll kept


def _comparison():
    market = "qualify_runoff:C"
    quotes = [OddsQuote("e", "Tipsport", market, "yes", 3.0, datetime(2026, 10, 1)),
              OddsQuote("e", "Tipsport", market, "no", 1.35, datetime(2026, 10, 1))]
    return compare_market(market, {"yes": 0.45, "no": 0.55}, latest_books(quotes),
                          SignalSettings(bankroll=1000, kelly_fraction=0.25, max_stake_pct=None))


def test_record_and_settle(session):
    comp = _comparison()
    assert len(record_signals(session, "e", [comp])) == 1
    assert record_signals(session, "e", [comp]) == []  # same open bet is not duplicated
    record_snapshot(session, "e", {"qualify_runoff:C": {"yes": 0.45, "no": 0.55}}, {},
                    {"qualify_runoff:C": {"yes": 0.30, "no": 0.70}})

    report = settle(session, "e", RESULT)
    bet = session.query(PaperBetRow).one()
    assert bet.status == "lost" and bet.profit == pytest.approx(-bet.stake)
    assert report.n_lost == 1 and report.profit == pytest.approx(-bet.stake)
    assert report.roi == pytest.approx(-1.0)
    # C did not qualify (y=0 for yes, 1 for no)
    assert report.brier_model == pytest.approx(((0.45 - 0) ** 2 + (0.55 - 1) ** 2) / 2)
    assert report.brier_market == pytest.approx(((0.30 - 0) ** 2 + (0.70 - 1) ** 2) / 2)


def test_winning_bet_profit(session):
    comp = _comparison()
    record_signals(session, "e", [comp])
    won = ElectionResult(first_round={"A": 30.0, "C": 28.0, "B": 25.0})
    report = settle(session, "e", won)
    bet = session.query(PaperBetRow).one()
    assert bet.status == "won"
    assert bet.profit == pytest.approx(round(bet.stake * 2.0, 2))
    assert report.roi == pytest.approx(2.0, rel=1e-3)
