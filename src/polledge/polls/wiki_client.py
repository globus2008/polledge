"""MediaWiki API client with on-disk cache, identifiable User-Agent and retries."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

from polledge.config import WikipediaSettings
from polledge.errors import FetchError

log = logging.getLogger(__name__)


class WikiClient:
    def __init__(self, settings: WikipediaSettings, cache_dir: Path, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        if "CHANGE-ME" in settings.user_agent:
            log.warning("Doplň kontakt do wikipedia.user_agent v settings.yaml (pravidla Wikipedie pro roboty)")
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip"},
            timeout=settings.timeout_seconds,
            transport=transport,
        )

    def _cache_path(self, page: str) -> Path:
        digest = hashlib.sha1(page.encode("utf-8")).hexdigest()[:16]
        safe = "".join(ch if ch.isalnum() else "_" for ch in page)[:80]
        return self.cache_dir / f"{safe}_{digest}.json"

    def get_page_html(self, page: str, force: bool = False) -> str:
        """Return rendered HTML of a page; served from cache when fresh enough."""
        cache = self._cache_path(page)
        if cache.exists() and not force:
            age_hours = (time.time() - cache.stat().st_mtime) / 3600
            if age_hours < self.settings.cache_ttl_hours:
                log.info("Cache hit for '%s' (%.1f h old)", page, age_hours)
                return json.loads(cache.read_text(encoding="utf-8"))["html"]

        try:
            html = self._fetch(page)
        except FetchError:
            if cache.exists():
                log.warning("Download of '%s' failed, using stale cache", page)
                return json.loads(cache.read_text(encoding="utf-8"))["html"]
            raise
        cache.write_text(json.dumps({"page": page, "fetched": time.time(), "html": html}), encoding="utf-8")
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
                    raise FetchError(f"Wikipedie vrátila chybu pro stránku '{page}': {data['error'].get('info')}")
                return data["parse"]["text"]
            except FetchError:
                raise
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                last_error = exc
                log.warning("Fetch of '%s' failed: %s", page, exc)
                if attempt < self.settings.max_retries:
                    time.sleep(min(2**attempt, 10))
        raise FetchError(f"Nepodařilo se stáhnout stránku '{page}' z Wikipedie: {last_error}")

    def close(self) -> None:
        self._client.close()
