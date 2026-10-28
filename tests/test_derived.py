from datetime import datetime
from pathlib import Path

import math
import pytest

from polledge.config import Settings, load_election
from polledge.model.derived import derive_market, parse_bracket, seats_at_most
from polledge.model.markets import parse_market
from polledge.odds.base import OddsQuote
from polledge.odds.paste import parse_pasted_odds, signed_line
from polledge.services import build_market_comparisons

ROOT = Path(__file__).resolve().parents[1]
T = datetime(2026, 10, 28, 12)

# Polymarket prices as of 28 Oct 2026 (Republican seat brackets and balance of power).
SENATE = {"≤47": 0.4, "48": 0.155, "49": 0.115, "50": 0.125, "51": 0.075, "52": 0.0605, "53": 0.0465,
          "54": 0.0075, "56": 0.0025, "57+": 0.0025}
SENATE_LOW = {"≤40": 0.011, "41": 0.0165, "42": 0.041, "43": 0.049, "44": 0.08, "45": 0.089, "46": 0.12,
              "47": 0.14, "48+": 0.57}
HOUSE = {"Below 190": 0.315, "190-194": 0.115, "195-199": 0.16, "200-204": 0.138, "205-209": 0.148,
         "210-214": 0.043, "215-219": 0.0705, "220-224": 0.0425, "225-229": 0.012, "230+": 0.003}
BALANCE = {"Democrats Sweep": 0.655, "D Senate, R House": 0.009, "R Senate, D House": 0.265,
           "Republicans Sweep": 0.075}


def test_parse_bracket_labels():
    assert parse_bracket("48") == (48, 48)
    assert parse_bracket("190-194") == (190, 194)
    assert parse_bracket("≤47") == (-math.inf, 47)
    assert parse_bracket("Below 190") == (-math.inf, 189)
    assert parse_bracket("57+") == (57, math.inf)
    assert parse_bracket("Above 230") == (231, math.inf)
    assert parse_bracket("Democrats") is None


def test_seats_at_most_exact_interpolated_and_undecidable():
    total = sum(SENATE.values())
    p, exact = seats_at_most(SENATE, 47)
    assert exact and p == pytest.approx(0.4 / total)
    p, exact = seats_at_most(HOUSE, 205)  # splits 205-209: one fifth of it
    assert not exact and p == pytest.approx((0.315 + 0.115 + 0.16 + 0.138 + 0.148 / 5) / sum(HOUSE.values()))
    assert seats_at_most(SENATE, 45) is None  # inside the open "≤47" bracket


def test_market_ids_round_trip():
    for market in ("outcome:DEM_SWEEP", "seats:REP", "seats:REP:low", "seats_over:REP:47.5"):
        assert parse_market(market).id == market


def _election(name):
    return load_election(ROOT / "config" / "elections" / f"{name}.yaml")


def test_derive_seats_over_uses_the_finer_bracket_set():
    cfg = _election("us_senate_2026")
    reference = {"seats:REP": SENATE, "seats:REP:low": SENATE_LOW}
    over = derive_market("seats_over:REP:47.5", reference, cfg)
    assert over["over"] == pytest.approx(1 - 0.4 / sum(SENATE.values()))
    # 45.5 lies inside "≤47" of the main set, the lower-bracket set decides it
    low_total = sum(SENATE_LOW.values())
    assert derive_market("seats_over:REP:45.5", reference, cfg)["under"] == pytest.approx(
        (0.011 + 0.0165 + 0.041 + 0.049 + 0.08 + 0.089) / low_total)


def test_derive_seats_of_the_complement_party():
    cfg = _election("us_house_2026")
    # DEM > 229.5  <=>  REP <= 205
    p = derive_market("seats_over:DEM:229.5", {"seats:REP": HOUSE}, cfg)["over"]
    assert p == pytest.approx(seats_at_most(HOUSE, 205)[0])
    assert derive_market("seats_over:DEM:229.5", {}, cfg) is None


def test_market_mode_compares_bookmaker_markets_derived_from_reference():
    cfg = _election("us_congress_2026")
    quotes = [OddsQuote(cfg.id, "Polymarket", "winner", o, 1 / p, T) for o, p in
              {"DEM_SWEEP": 0.655, "D_SENATE_R_HOUSE": 0.009, "R_SENATE_D_HOUSE": 0.265, "REP_SWEEP": 0.075}.items()]
    quotes += [OddsQuote(cfg.id, "Fortuna", "outcome:REP_SWEEP", "yes", 10.0, T),
               OddsQuote(cfg.id, "Fortuna", "outcome:REP_SWEEP", "no", 1.02, T)]
    comps = {c.market: c for c in build_market_comparisons(cfg, quotes, Settings())}
    yes = next(o for o in comps["outcome:REP_SWEEP"].outcomes if o.outcome == "yes")
    assert yes.p_model == pytest.approx(0.075) and yes.best_odds == 10.0 and yes.best_bookmaker == "Fortuna"
    assert yes.edge == pytest.approx(0.075 * 10 - 1)


def test_polymarket_names_map_to_congress_combinations():
    cfg = _election("us_congress_2026")
    from polledge.odds.scrapers.mapping import NameMapper, RawMarket

    mapped = NameMapper(cfg, cfg.bookmakers["polymarket"]).map_market(
        RawMarket("balance-of-power-2026-midterms", [(n, 1 / p) for n, p in BALANCE.items()]))
    assert set(mapped.odds) == {"DEM_SWEEP", "D_SENATE_R_HOUSE", "R_SENATE_D_HOUSE", "REP_SWEEP"}


# Fortuna, converted from a screenshot: yes/no side by side, then the seats over/under lines.
FORTUNA = """
| Republikáni většina v Senátu i ve Sněmovně reprezentantů | |
| Ano | 10.00 | Ne | 1.02 |

| -47.5 | 2.35 | +47.5 | 1.50 |
| -48.5 | 1.53 | +48.5 | 2.28 |
"""


def test_paste_reads_side_by_side_cells_and_signed_lines():
    pairs = [(s.name, s.odds) for s in parse_pasted_odds(FORTUNA)]
    assert pairs[:2] == [("Ano", 10.0), ("Ne", 1.02)]
    assert ("-47.5", 2.35) in pairs and ("+47.5", 1.5) in pairs and ("+48.5", 2.28) in pairs
    assert signed_line("+47.5") == ("over", 47.5) and signed_line("-48,5") == ("under", 48.5)
    assert signed_line("Ano") is None
    plain = [(s.name, s.odds) for s in parse_pasted_odds("-47.5 2.35\n+47.5\n1.50\n")]
    assert plain == [("-47.5", 2.35), ("+47.5", 1.5)]
