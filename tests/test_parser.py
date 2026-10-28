from datetime import date
from pathlib import Path

import pytest

from polledge.config import OTHER, load_election
from polledge.errors import TableStructureError
from polledge.polls.parser import extract_tables, parse_election_html, strip_party_label, table_to_grid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "fr_pres_2027_snippet.html"


@pytest.fixture(scope="module")
def election():
    return load_election(ROOT / "config" / "elections" / "fr_pres_2027.yaml")


@pytest.fixture(scope="module")
def html():
    return FIXTURE.read_text(encoding="utf-8")


def test_extract_tables_heading_paths(html):
    titles = [t.headings for t in extract_tables(html)]
    assert ["First round", "October 2026"] in titles
    assert ["Second round", "Declined to be candidates", "Philippe vs. Bardella"] in titles


def test_rowspan_is_expanded(html):
    table = extract_tables(html)[0]
    grid, header_rows = table_to_grid(table.element)
    assert header_rows == 2
    # Scenario rows share the pollster/date/sample cells through rowspan.
    assert grid[2][0] == grid[3][0] == "Ipsos"
    assert grid[3][1] == "21– 22 Oct 2026"


def test_first_round_scenario_filter(html, election):
    from polledge.polls.parser import scenario_name

    polls = [p for p in parse_election_html(html, election) if p.round == "first"]
    base = [p for p in polls if scenario_name(p) == "philippe"]
    with_attal = [p for p in polls if scenario_name(p) == "philippe_attal"]
    assert base and with_attal, "expected polls in the base and in the Attal scenario"
    for p in base:
        assert {"LE_PEN", "PHILIPPE", "MELENCHON"} <= set(p.shares)
        assert "ATTAL" not in p.shares
        assert sum(p.shares.values()) == pytest.approx(100.0, abs=0.01)
        assert p.end_date <= date(2026, 10, 31)
    assert all("ATTAL" in p.shares for p in with_attal)
    ipsos = next(p for p in base if p.pollster == "Ipsos")
    assert ipsos.end_date == date(2026, 10, 22)
    assert ipsos.sample_size == 1500
    assert ipsos.shares["LE_PEN"] == 33.5


def test_scenario_polling_section_excluded(html, election):
    polls = parse_election_html(html, election)
    assert not any("MACRON" in p.shares for p in polls)


def test_runoff_tables(html, election):
    runoff = [p for p in parse_election_html(html, election) if p.round == "runoff"]
    pairs = {p.scenario for p in runoff}
    # Bardella tables ("Declined to be candidates") are kept for the case that Le Pen cannot stand.
    assert pairs == {"LE_PEN|PHILIPPE", "LE_PEN|MELENCHON", "BARDELLA|PHILIPPE"}
    for p in runoff:
        assert sum(p.shares.values()) == pytest.approx(100.0)
        assert OTHER not in p.shares


def test_missing_column_raises(html, election):
    broken = election.model_copy(deep=True)
    broken.first_round.columns.sample_size = "Respondents"
    with pytest.raises(TableStructureError, match="Respondents"):
        parse_election_html(html, broken)


def test_no_matching_table_raises(html, election):
    broken = election.model_copy(deep=True)
    broken.first_round.tables[0].heading = ["Nonexistent section"]
    with pytest.raises(TableStructureError):
        parse_election_html(html, broken)


@pytest.mark.parametrize(
    "header, expected",
    [("Le Pen RN", "Le Pen"), ("Mélenchon (LFI)", "Mélenchon"), ("Philippe HOR", "Philippe"),
     ("Marine Le Pen", "Marine Le Pen"), ("SPD", "SPD")],
)
def test_strip_party_label(header, expected):
    assert strip_party_label(header) == expected
