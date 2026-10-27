"""Bookmaker scrapers. Add a new one by implementing OddsSource and registering it in SCRAPERS."""

from __future__ import annotations

from polledge.config import BookmakerConfig, ElectionConfig
from polledge.errors import ConfigError
from polledge.odds.base import OddsSource
from polledge.odds.scrapers.betano import BetanoSource
from polledge.odds.scrapers.polymarket import PolymarketSource
from polledge.odds.scrapers.http import PoliteFetcher, ScrapeError
from polledge.i18n import _

SCRAPERS = {
    "betano": BetanoSource,
    "polymarket": PolymarketSource,
}

# Bookmakers checked and not supported, with the reason shown to the user.
UNSUPPORTED = {
    "tipsport": "the site blocks automated access (HTTP 403)",
    "chance": "the site blocks automated access (HTTP 403)",
    "fortuna": "the offer is loaded by JavaScript from an internal API; not supported",
}


def make_source(name: str, fetcher: PoliteFetcher, election: ElectionConfig, config: BookmakerConfig,
                force: bool = False) -> OddsSource:
    key = name.lower()
    if key in UNSUPPORTED:
        raise ConfigError(_("Bookmaker '{name}' is not supported: {reason}. Enter odds manually (import-odds).", name=name, reason=_(UNSUPPORTED[key])))
    if key not in SCRAPERS:
        raise ConfigError(_("Unknown bookmaker '{name}' (supported: {supported})", name=name, supported=", ".join(sorted(SCRAPERS))))
    return SCRAPERS[key](fetcher, election, config, force=force)


__all__ = ["SCRAPERS", "UNSUPPORTED", "PoliteFetcher", "ScrapeError", "make_source"]
