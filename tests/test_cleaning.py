from datetime import date

import pytest

from polledge.polls.cleaning import (
    clean_text,
    normalize_name,
    parse_fieldwork_end,
    parse_percent,
    parse_sample_size,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12–15 Oct 2026", date(2026, 10, 15)),
        ("21– 22 Oct 2026", date(2026, 10, 22)),
        ("28 Sep – 2 Oct 2026", date(2026, 10, 2)),
        ("30 Dec 2026 – 3 Jan 2027", date(2027, 1, 3)),
        ("22 Oct 2026", date(2026, 10, 22)),
        ("5 September 2026", date(2026, 9, 5)),
        ("Oct 2026", date(2026, 10, 31)),
        ("12-15 Oct 2026[a]", date(2026, 10, 15)),
        ("", None),
        ("unknown", None),
    ],
)
def test_parse_fieldwork_end(raw, expected):
    assert parse_fieldwork_end(raw) == expected


def test_parse_fieldwork_end_default_year():
    assert parse_fieldwork_end("12–15 Oct", default_year=2026) == date(2026, 10, 15)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("33.5%", 33.5),
        ("17%", 17.0),
        ("23,5", 23.5),
        ("<1", 0.5),
        ("<0.5%", 0.25),
        ("–", None),
        ("—", None),
        ("", None),
        ("12%[b]", 12.0),
        ("abc", None),
        ("150", None),
    ],
)
def test_parse_percent(raw, expected):
    assert parse_percent(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [("1,500", 1500), ("1 012", 1012), ("~1000", 1000), ("2,094[c]", 2094), ("–", None), ("", None)],
)
def test_parse_sample_size(raw, expected):
    assert parse_sample_size(raw) == expected


def test_clean_text_and_normalize():
    assert clean_text("Ifop[1]\xa0 ") == "Ifop"
    assert normalize_name("Jean-Luc Mélenchon") == "jean-luc melenchon"
    assert normalize_name("Édouard  Philippe") == "edouard philippe"
