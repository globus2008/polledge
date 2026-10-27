"""MediaWiki API client with a database page cache, identifiable User-Agent and retries."""

from __future__ import annotations

import logging
import time

import httpx

from polledge.cache import PageCache
from polledge.config import WikipediaSettings
from polledge.errors import FetchError
from polledge.i18n import _

log = logging.getLogger(__name__)


class WikiClient:
    def __init__(self, settings: WikipediaSettings, cache: PageCache, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        if "CHANGE-ME" in settings.user_agent:
            log.warning(_("Add a contact to wikipedia.user_agent in settings.yaml (Wikipedia bot policy)"))
        self.cache = cache
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def get_page_html(self, page: str, force: bool = False) -> str:
        """Return rendered HTML of a page; served from cache when fresh enough."""
        cached = self.cache.get(page)
        if cached is not None and not force and cached.age_hours() < self.settings.cache_ttl_hours:
            log.info("Cache hit for '%s' (%.1f h old)", page, cached.age_hours())
            return cached.body

        try:
            html = self._fetch(page)
        except FetchError:
            if cached is not None:
                log.warning("Download of '%s' failed, using stale cache", page)
                return cached.body
            raise
        self.cache.put(page, html)
        return html

    def _fetch(self, page: str) -> str:
        params = {
            "action": "parse",
            "page": page,
            "prop": "text",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                log.info("Fetching '%s' from MediaWiki API (attempt %d)", page, attempt)
                resp = self._client.get(self.settings.api_url, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    # API-level errors (missing page etc.) are not retried.
                    raise FetchError(_("Wikipedia returned an error for page '{page}': {error}", page=page, error=data["error"].get("info")))
                return data["parse"]["text"]
            except FetchError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = exc
                log.warning("Fetch of '%s' failed: %s", page, exc)
                if attempt < self.settings.max_retries:
                    time.sleep(min(2**attempt, 10))
        raise FetchError(_("Failed to download page '{page}' from Wikipedia: {error}", page=page, error=last_error))

    def close(self) -> None:
        self._client.close()
