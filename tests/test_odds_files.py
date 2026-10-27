from datetime import datetime

import pytest

from polledge.errors import PollEdgeError
from polledge.odds.file_source import CsvOddsSource, YamlOddsSource, source_for_path


def test_csv_source(tmp_path):
    path = tmp_path / "odds.csv"
    path.write_text(
        "# comment\n"
        "election_id,bookmaker,market,outcome,odds,timestamp\n"
        "e,Tipsport,winner,A,1.8,2026-10-27 10:00\n"
        "e,Tipsport,qualify_runoff:A,YES,\"1,25\",27.10.2026 10:00\n"
        "e,Tipsport,winner,B,0.9,2026-10-27 10:00\n"
        "x,Fortuna,winner,A,2.0,2026-10-27\n",
        encoding="utf-8",
    )
    src = CsvOddsSource(path)
    quotes = src.fetch("e")
    assert len(quotes) == 2
    assert quotes[1].outcome == "yes" and quotes[1].odds == 1.25
    assert quotes[1].timestamp == datetime(2026, 10, 27, 10, 0)
    assert len(src.errors) == 1 and "> 1" in src.errors[0]


def test_yaml_grouped_source(tmp_path):
    path = tmp_path / "odds.yaml"
    path.write_text(
        "- election_id: e\n"
        "  bookmaker: Fortuna\n"
        "  timestamp: 2026-10-27 18:00\n"
        "  markets:\n"
        "    winner: {A: 1.8, B: 2.1}\n"
        "    qualify_runoff:A: {yes: 1.2, no: 4.0}\n",
        encoding="utf-8",
    )
    quotes = YamlOddsSource(path).fetch()
    assert {(q.market, q.outcome) for q in quotes} == {
        ("winner", "A"), ("winner", "B"), ("qualify_runoff:A", "yes"), ("qualify_runoff:A", "no")
    }


def test_invalid_market_and_format(tmp_path):
    path = tmp_path / "odds.csv"
    path.write_text("election_id,bookmaker,market,outcome,odds,timestamp\ne,T,bogus,A,2,2026-10-27\n", encoding="utf-8")
    src = CsvOddsSource(path)
    assert src.fetch() == [] and "bogus" in src.errors[0]
    with pytest.raises(PollEdgeError):
        source_for_path(tmp_path / "odds.xlsx")
