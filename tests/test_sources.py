"""Polymarket reference source, Betano discovery and reference handling in the comparison."""

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from polledge.cache import PageCache
from polledge.config import BookmakerConfig, ScrapingSettings, Settings, SignalSettings, load_election
from polledge.db import make_session_factory
from polledge.odds.base import OddsQuote
from polledge.odds.margin import latest_books
from polledge.odds.scrapers import PoliteFetcher, ScrapeError
from polledge.odds.scrapers import http as http_module
from polledge.odds.scrapers.betano import POLITICS_URL, BetanoSource, find_leagues
from polledge.odds.scrapers.polymarket import API_URL, PolymarketSource, parse_event
from polledge.signals.compare import compare_market

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SLUG = "next-french-presidential-election"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda s: None)


@pytest.fixture
def fr():
    return load_election(ROOT / "config" / "elections" / "fr_pres_2027.yaml")


def _fetcher(pages: dict[str, str]):
    def handler(request: httpx.Request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        body = pages.get(str(request.url))
        return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

    cache = PageCache(make_session_factory(":memory:"), "bookmakers")
    return PoliteFetcher(ScrapingSettings(min_delay_seconds=0), cache, transport=httpx.MockTransport(handler))


def test_parse_polymarket_event():
    title, selections = parse_event((FIXTURES / "polymarket_fr_pres.json").read_text(encoding="utf-8"))
    assert title == "Next French Presidential Election"
    prices = dict(selections)
    assert prices["Marine Le Pen"] == pytest.approx(0.4435)
    assert 0.9 < sum(prices.values()) < 1.05


def test_parse_polymarket_skips_closed_and_tiny_markets():
    event = [{"title": "E", "markets": [
        {"groupItemTitle": "A", "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]', "active": True},
        {"groupItemTitle": "B", "outcomes": '["Yes","No"]', "outcomePrices": '["0.3","0.7"]', "closed": True},
        {"groupItemTitle": "C", "outcomes": '["Yes","No"]', "outcomePrices": '["0.0001","0.9999"]'},
    ]}]
    assert parse_event(json.dumps(event))[1] == [("A", 0.6)]
    with pytest.raises(ScrapeError):
        parse_event("[]")


def test_polymarket_source(fr):
    body = (FIXTURES / "polymarket_fr_pres.json").read_text(encoding="utf-8")
    fetcher = _fetcher({API_URL.format(slug=SLUG): body})
    quotes = PolymarketSource(fetcher, fr, fr.bookmakers["polymarket"]).fetch()
    by_outcome = {q.outcome: q for q in quotes}
    assert by_outcome["LE_PEN"].odds == pytest.approx(1 / 0.4435, rel=1e-3)
    assert by_outcome["LE_PEN"].market == "winner"
    assert all(q.bookmaker == "Polymarket" for q in quotes)


def _politics_page(extra_league: dict | None = None) -> str:
    html = (FIXTURES / "betano_politika.html").read_text(encoding="utf-8")
    if extra_league is None:
        return html
    marker = 'window["initial_state"]='
    start = html.index(marker) + len(marker)
    state, end = json.JSONDecoder().raw_decode(html[start:])
    state["data"]["regionGroups"][0]["regions"].append({"name": "Francie", "leagues": [extra_league]})
    return html[:start] + json.dumps(state, ensure_ascii=False) + html[start + end:]


def test_find_leagues():
    html = _politics_page()
    assert find_leagues(html, ["Česko", "prezidentské"]) == [
        "https://www.betano.cz/sport/politika/cesko/prezidentske-volby/189391/"]
    assert find_leagues(html, ["Francie", "prezident"]) == []


def test_betano_discovery_waits_until_listed(fr):
    fetcher = _fetcher({POLITICS_URL: _politics_page()})
    assert BetanoSource(fetcher, fr, fr.bookmakers["betano"]).fetch() == []  # not listed yet: no error


def test_betano_discovery_finds_new_market(fr):
    league_url = "https://www.betano.cz/sport/politika/francie/prezidentske-volby/999/"
    league = {"id": "999", "name": "Prezidentské volby", "url": "/sport/politika/francie/prezidentske-volby/999/"}
    state = {"data": {"blocks": [{"events": [{"name": "Francouzské prezidentské volby 2027", "markets": [
        {"name": "Vítěz", "selections": [{"name": "Marine Le Pen", "price": 1.9}, {"name": "Édouard Philippe", "price": 3.6}]}
    ]}]}]}}
    pages = {POLITICS_URL: _politics_page(league),
             league_url: '<script>window["initial_state"]=' + json.dumps(state) + ";</script>"}
    quotes = BetanoSource(_fetcher(pages), fr, fr.bookmakers["betano"]).fetch()
    assert {q.outcome: q.odds for q in quotes} == {"LE_PEN": 1.9, "PHILIPPE": 3.6}


def test_reference_source_never_signals():
    t = datetime(2026, 10, 27, 12)
    quotes = [OddsQuote("e", "Polymarket", "winner", "A", 1 / 0.30, t), OddsQuote("e", "Polymarket", "winner", "B", 1 / 0.69, t)]
    model = {"A": 0.6, "B": 0.4}
    settings = SignalSettings()
    comp = compare_market("winner", model, latest_books(quotes, {"winner": model}, ["Polymarket"]), settings)
    a = next(o for o in comp.outcomes if o.outcome == "A")
    assert a.p_reference == pytest.approx(0.30)  # exchange price is the probability, not renormalised
    assert a.best_odds is None and a.edge is None and not a.is_signal and a.stake == 0
    assert comp.margins == {} and comp.arbitrage_sum is None

    # With a real bookmaker in the market, only its odds count for signals.
    quotes.append(OddsQuote("e", "Tipsport", "winner", "A", 2.2, t))
    quotes.append(OddsQuote("e", "Tipsport", "winner", "B", 1.6, t))
    comp = compare_market("winner", model, latest_books(quotes, {"winner": model}, ["Polymarket"]), settings)
    a = next(o for o in comp.outcomes if o.outcome == "A")
    assert a.best_bookmaker == "Tipsport" and a.best_odds == 2.2 and a.is_signal
    assert set(comp.margins) == {"Tipsport"}


def test_market_mode_compares_bookmaker_with_reference():
    from polledge.services import build_market_comparisons

    us = load_election(ROOT / "config" / "elections" / "us_pres_2028.yaml")
    t = datetime(2026, 10, 28, 9)
    quotes = [
        OddsQuote(us.id, "Polymarket", "winner", "VANCE", 1 / 0.18, t),
        OddsQuote(us.id, "Polymarket", "winner", "HARRIS", 1 / 0.043, t),
        OddsQuote(us.id, "Betano", "winner", "VANCE", 5.0, t),
        OddsQuote(us.id, "Betano", "winner", "HARRIS", 25.0, t),
        OddsQuote(us.id, "Betano", "winner", "Celebrity", 500.0, t),
    ]
    (comp,) = build_market_comparisons(us, quotes, Settings())
    by = {o.outcome: o for o in comp.outcomes}
    assert by["VANCE"].p_model == pytest.approx(0.18) and by["VANCE"].edge == pytest.approx(0.18 * 5 - 1)
    assert by["HARRIS"].is_signal and by["HARRIS"].best_bookmaker == "Betano"
    assert not by["Celebrity"].in_model and not by["Celebrity"].is_signal


def test_market_mode_config_validation():
    from pydantic import ValidationError

    from polledge.config import ElectionConfig

    base = {"id": "x", "name": "x", "election_date": "2028-11-07", "candidates": {"A": {"name": "A"}}}
    with pytest.raises(ValidationError):
        ElectionConfig(**base)  # polls mode needs wiki_page and first_round
    with pytest.raises(ValidationError):
        ElectionConfig(**base, probability_source="market")  # reference market must be configured
    cfg = ElectionConfig(**base, probability_source="market",
                         bookmakers={"polymarket": {"pages": ["slug"], "markets": {"slug": "winner"}}})
    assert not cfg.uses_polls
