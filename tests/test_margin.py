from datetime import datetime

import pytest

from polledge.odds.base import OddsQuote
from polledge.odds.margin import implied_probability, is_complete, latest_books, overround, remove_margin


def test_implied_probability():
    assert implied_probability(2.0) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        implied_probability(1.0)


def test_overround_two_way():
    # 1.90 / 1.90 -> 2 * 0.5263 = 1.0526 -> margin 5.26 %
    assert overround([1.9, 1.9]) == pytest.approx(0.05263, abs=1e-4)


def test_remove_margin_normalises_to_one():
    fair = remove_margin({"A": 1.5, "B": 2.6, "C": 8.0})
    assert sum(fair.values()) == pytest.approx(1.0)
    implied = {"A": 1 / 1.5, "B": 1 / 2.6, "C": 1 / 8.0}
    total = sum(implied.values())
    for k in fair:
        assert fair[k] == pytest.approx(implied[k] / total)
    assert fair["A"] > fair["B"] > fair["C"]


def test_is_complete():
    assert is_complete("qualify_runoff:X", {"yes": 1.8, "no": 1.9})
    assert not is_complete("qualify_runoff:X", {"yes": 1.8})
    # Implied sum below 1 for a multi-outcome market means outcomes are missing.
    assert not is_complete("winner", {"A": 2.5, "B": 4.0})
    model = {"A": 0.6, "B": 0.3, "C": 0.1}
    assert not is_complete("winner", {"A": 1.5, "B": 2.9}, set(model), model)
    assert is_complete("winner", {"A": 1.5, "B": 2.9, "C": 8.0}, set(model), model)


def test_latest_books_keeps_newest_quote():
    t1, t2 = datetime(2026, 10, 1, 10), datetime(2026, 10, 2, 10)
    quotes = [
        OddsQuote("e", "Tipsport", "qualify_runoff:X", "yes", 2.0, t1),
        OddsQuote("e", "Tipsport", "qualify_runoff:X", "yes", 1.7, t2),
        OddsQuote("e", "Tipsport", "qualify_runoff:X", "no", 2.0, t2),
    ]
    (book,) = latest_books(quotes)
    assert book.odds == {"yes": 1.7, "no": 2.0}
    assert book.complete
    assert book.margin == pytest.approx(1 / 1.7 + 0.5 - 1)
    assert sum(book.fair.values()) == pytest.approx(1.0)
