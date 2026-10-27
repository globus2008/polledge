"""Polymarket (public Gamma API): prediction-market prices, used as a market reference only.

Each Polymarket "event" (e.g. next-french-presidential-election) groups binary markets, one per
candidate; the price of "Yes" is the market's probability. It is stored as decimal odds 1/price so
the rest of PollEdge treats it like any book; SignalSettings.reference_bookmakers keeps it out of
best odds, signals and stakes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from polledge.config import BookmakerConfig, ElectionConfig
from polledge.i18n import _
from polledge.odds.base import OddsQuote, OddsSource
from polledge.odds.scrapers.http import PoliteFetcher, ScrapeError
from polledge.odds.scrapers.mapping import NameMapper, RawMarket

log = logging.getLogger(__name__)

API_URL = "https://gamma-api.polymarket.com/events?slug={slug}"
MIN_PRICE = 0.001  # below this the "odds" are meaningless (1000+)


def parse_event(body: str) -> tuple[str, list[tuple[str, float]]]:
    """(event title, [(candidate name, yes price)]) for the open candidate markets of an event."""
    try:
        events = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ScrapeError(_("Cannot read Polymarket data: {error}", error=exc)) from exc
    if not events:
        raise ScrapeError(_("Polymarket event not found - check the slug in the election config"))
    event = events[0]
    selections = []
    for market in event.get("markets") or []:
        if market.get("closed") or not market.get("active", True):
            continue
        try:
            outcomes = json.loads(market["outcomes"])
            prices = [float(p) for p in json.loads(market["outcomePrices"])]
        except (KeyError, ValueError, TypeError):
            continue
        if "Yes" not in outcomes:
            continue
        price = prices[outcomes.index("Yes")]
        name = market.get("groupItemTitle") or market.get("question", "")
        if price >= MIN_PRICE and name:
            selections.append((name, price))
    return event.get("title", ""), selections


class PolymarketSource(OddsSource):
    name = "Polymarket"

    def __init__(self, fetcher: PoliteFetcher, election: ElectionConfig, config: BookmakerConfig, force: bool = False):
        self.fetcher = fetcher
        self.election = election
        self.config = config
        self.force = force
        self.mapper = NameMapper(election, config)

    def fetch(self, election_id: str | None = None) -> list[OddsQuote]:
        if election_id is not None and election_id != self.election.id:
            return []
        quotes: list[OddsQuote] = []
        for slug in self.config.pages:
            body, fetched_at = self.fetcher.get_with_time(API_URL.format(slug=slug), force=self.force)
            title, selections = parse_event(body)
            market_id = self.config.markets.get(slug)
            if market_id is None:
                log.warning("Polymarket event %s has no market mapping in the config", slug)
                continue
            # The mapper maps the event slug (used as the "market name") to our market id.
            mapped = self.mapper.map_market(RawMarket(slug, [(n, round(1.0 / p, 4)) for n, p in selections]))
            if mapped is None:
                continue
            timestamp: datetime = fetched_at.replace(second=0, microsecond=0)
            quotes += [OddsQuote(self.election.id, self.name, mapped.market, outcome, odds, timestamp)
                       for outcome, odds in mapped.odds.items()]
            log.info("Polymarket '%s': %d prices", title, len(mapped.odds))
        return quotes
