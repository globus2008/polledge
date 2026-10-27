"""Betano.cz: public offer pages embed the offer as JSON in window["initial_state"]."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from polledge.config import BookmakerConfig, ElectionConfig
from polledge.odds.base import OddsQuote, OddsSource
from polledge.odds.scrapers.http import PoliteFetcher, ScrapeError
from polledge.odds.scrapers.mapping import NameMapper, RawMarket
from polledge.i18n import _

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r'window\[\s*["\']initial_state["\']\s*\]\s*=\s*')


def extract_state(html: str) -> dict:
    match = _STATE_RE.search(html)
    if not match:
        raise ScrapeError(_("The Betano page has no offer data (initial_state) - the site structure has changed"))
    try:
        state, _end = json.JSONDecoder().raw_decode(html[match.end():])
    except json.JSONDecodeError as exc:
        raise ScrapeError(_("Cannot read Betano offer data: {error}", error=exc)) from exc
    return state.get("data", {})


def parse_markets(html: str) -> list[RawMarket]:
    """Markets from an event page (data.event) or a league page (data.blocks[].events[])."""
    data = extract_state(html)
    events = []
    if isinstance(data.get("event"), dict):
        events.append(data["event"])
    for block in data.get("blocks") or []:
        events.extend(block.get("events") or [])
    markets = []
    for event in events:
        for m in event.get("markets") or []:
            selections = [(s.get("name", ""), _price(s.get("price"))) for s in m.get("selections") or []]
            markets.append(RawMarket(m.get("name", ""), selections))
    return markets


def _price(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class BetanoSource(OddsSource):
    name = "Betano"

    def __init__(self, fetcher: PoliteFetcher, election: ElectionConfig, config: BookmakerConfig, force: bool = False):
        self.fetcher = fetcher
        self.election = election
        self.config = config
        self.force = force
        self.mapper = NameMapper(election, config)

    def fetch(self, election_id: str | None = None) -> list[OddsQuote]:
        if election_id is not None and election_id != self.election.id:
            return []
        books: dict[str, dict[str, float]] = {}
        fetched: list[datetime] = []
        for url in self.config.pages:
            html, fetched_at = self.fetcher.get_with_time(url, force=self.force)
            fetched.append(fetched_at)
            raw = parse_markets(html)
            if not raw:
                log.warning("No markets found on %s", url)
            for market in raw:
                mapped = self.mapper.map_market(market)
                if mapped is not None:
                    books.setdefault(mapped.market, {}).update(mapped.odds)
        if not books:
            raise ScrapeError(_(
                "No configured market ({markets}) found on the Betano pages for {id}. "
                "The market may have closed or been renamed.",
                markets=", ".join(self.config.markets), id=self.election.id,
            ))
        # The time the offer was downloaded, so a cached page never creates "new" quotes.
        timestamp = min(fetched).replace(second=0, microsecond=0)
        return [
            OddsQuote(self.election.id, self.name, market, outcome, price, timestamp)
            for market, odds in books.items()
            for outcome, price in odds.items()
        ]
