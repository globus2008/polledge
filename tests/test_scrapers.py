import json
from pathlib import Path

import httpx
import pytest

from polledge.config import BookmakerConfig, ScrapingSettings, load_election
from polledge.errors import ConfigError
from polledge.odds.scrapers import PoliteFetcher, ScrapeError, make_source
from polledge.odds.scrapers import http as http_module
from polledge.odds.scrapers.betano import BetanoSource, parse_markets
from polledge.odds.scrapers.mapping import NameMapper, RawMarket

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "betano_cz_pres_2028.html"
EVENT_URL = "https://www.betano.cz/zapas-sance/ceske-prezidentske-volby-2028/31772398/"


@pytest.fixture
def election():
    return load_election(ROOT / "config" / "elections" / "cz_pres_2028.yaml")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http_module.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _fetcher(tmp_path, handler, **settings):
    cfg = ScrapingSettings(min_delay_seconds=0, **settings)
    return PoliteFetcher(cfg, tmp_path, transport=httpx.MockTransport(handler))


def _site(pages: dict[str, httpx.Response], calls: list | None = None):
    def handler(request: httpx.Request):
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return pages.get("robots", httpx.Response(404))
        return pages.get(str(request.url), httpx.Response(404))
    return handler


def test_parse_event_page():
    markets = parse_markets(FIXTURE.read_text(encoding="utf-8"))
    assert [m.name for m in markets] == ["Vítěz"]
    selections = dict(markets[0].selections)
    assert selections["Petr Pavel"] == 1.33
    assert selections["Andrej Babiš"] == 15.0


def test_parse_league_page_structure():
    state = {"data": {"blocks": [{"events": [{"name": "E", "markets": [
        {"name": "Vítěz", "selections": [{"name": "A", "price": 1.5}, {"name": "B", "price": "2.6"}]}]}]}]}}
    html = '<script>window["initial_state"]=' + json.dumps(state) + ";</script>"
    (market,) = parse_markets(html)
    assert market.selections == [("A", 1.5), ("B", 2.6)]


def test_page_without_state_raises():
    with pytest.raises(ScrapeError):
        parse_markets("<html>no data</html>")


def test_name_mapping(election):
    mapper = NameMapper(election, BookmakerConfig(pages=[], markets={"Vítěz": "winner"}, selections={"P. Pavel": "PAVEL"}))
    mapped = mapper.map_market(RawMarket("vitez", [("Petr Pavel", 1.4), ("Babiš", 9.0), ("Jaromír Jágr", 500.0),
                                                   ("P. Pavel", 1.5), ("Bad", 1.0)]))
    assert mapped.market == "winner"
    assert mapped.odds["PAVEL"] == 1.5  # explicit alias wins as the later selection
    assert mapped.odds["BABIS"] == 9.0  # surname alias
    assert mapped.odds["Jaromír Jágr"] == 500.0 and mapped.unmatched == ["Jaromír Jágr"]
    assert "Bad" not in mapped.odds  # odds <= 1 are ignored
    assert mapper.map_market(RawMarket("Počet hlasů", [])) is None
    binary = NameMapper(election, BookmakerConfig(pages=[], markets={"Postup Babiše": "qualify_runoff:BABIS"}))
    assert binary.map_market(RawMarket("Postup Babiše", [("Ano", 1.3), ("Ne", 3.2)])).odds == {"yes": 1.3, "no": 3.2}


def test_betano_source_end_to_end(tmp_path, election):
    html = FIXTURE.read_text(encoding="utf-8")
    fetcher = _fetcher(tmp_path, _site({EVENT_URL: httpx.Response(200, text=html)}))
    quotes = BetanoSource(fetcher, election, election.bookmakers["betano"]).fetch()
    by_outcome = {q.outcome: q for q in quotes}
    assert by_outcome["PAVEL"].odds == 1.33 and by_outcome["PAVEL"].market == "winner"
    assert all(q.bookmaker == "Betano" and q.election_id == "cz_pres_2028" for q in quotes)
    assert len({q.timestamp for q in quotes}) == 1


def test_cached_page_keeps_original_timestamp(tmp_path, election):
    html = FIXTURE.read_text(encoding="utf-8")
    fetcher = _fetcher(tmp_path, _site({EVENT_URL: httpx.Response(200, text=html)}))
    source = BetanoSource(fetcher, election, election.bookmakers["betano"])
    first = source.fetch()
    cache = fetcher._cache_path(EVENT_URL)
    data = json.loads(cache.read_text(encoding="utf-8"))
    data["fetched"] -= 600  # pretend the page was downloaded 10 minutes ago
    cache.write_text(json.dumps(data), encoding="utf-8")
    second = source.fetch()
    assert second[0].timestamp < first[0].timestamp


def test_missing_market_raises(tmp_path, election):
    html = FIXTURE.read_text(encoding="utf-8")
    fetcher = _fetcher(tmp_path, _site({EVENT_URL: httpx.Response(200, text=html)}))
    book = BookmakerConfig(pages=[EVENT_URL], markets={"Postup do 2. kola": "finalists"})
    with pytest.raises(ScrapeError, match="Postup do 2. kola"):
        BetanoSource(fetcher, election, book).fetch()


def test_fetcher_caches_pages(tmp_path):
    calls = []
    fetcher = _fetcher(tmp_path, _site({"https://x.cz/a": httpx.Response(200, text="page")}, calls))
    assert fetcher.get("https://x.cz/a") == "page"
    assert fetcher.get("https://x.cz/a") == "page"
    assert calls.count("https://x.cz/a") == 1
    assert fetcher.get("https://x.cz/a", force=True) == "page"
    assert calls.count("https://x.cz/a") == 2


def test_fetcher_respects_robots_txt(tmp_path):
    robots = httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
    fetcher = _fetcher(tmp_path, _site({"robots": robots, "https://x.cz/private/p": httpx.Response(200, text="x")}))
    with pytest.raises(ScrapeError, match="robots.txt"):
        fetcher.get("https://x.cz/private/p")


def test_fetcher_honours_crawl_delay(tmp_path, no_sleep):
    robots = httpx.Response(200, text="User-agent: *\nCrawl-delay: 20\n")
    pages = {"robots": robots, "https://x.cz/a": httpx.Response(200, text="a"), "https://x.cz/b": httpx.Response(200, text="b")}
    fetcher = _fetcher(tmp_path, _site(pages))
    fetcher.get("https://x.cz/a")
    fetcher.get("https://x.cz/b")
    assert no_sleep and max(no_sleep) > 15


def test_blocked_site_is_not_bypassed(tmp_path):
    calls = []
    fetcher = _fetcher(tmp_path, _site({"https://x.cz/a": httpx.Response(403)}, calls))
    with pytest.raises(ScrapeError, match="403"):
        fetcher.get("https://x.cz/a")
    assert calls.count("https://x.cz/a") == 1  # no retries on 403


def test_unsupported_bookmaker(tmp_path, election):
    fetcher = _fetcher(tmp_path, _site({}))
    with pytest.raises(ConfigError, match="403"):
        make_source("Tipsport", fetcher, election, BookmakerConfig(pages=[], markets={}))
    with pytest.raises(ConfigError):
        make_source("nobody", fetcher, election, BookmakerConfig(pages=[], markets={}))
