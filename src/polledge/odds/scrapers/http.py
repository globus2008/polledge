"""Polite HTTP fetching of public bookmaker pages: robots.txt, per-host delay, cache, no evasion.

A 403/blocked response is reported as is - the scraper never tries to get around bot protection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from polledge.config import ScrapingSettings
from polledge.errors import PollEdgeError
from polledge.i18n import _

log = logging.getLogger(__name__)


class ScrapeError(PollEdgeError):
    pass


class PoliteFetcher:
    def __init__(self, settings: ScrapingSettings, cache_dir: Path, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent, "Accept-Language": "cs-CZ,cs;q=0.9"},
            timeout=settings.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode('utf-8')).hexdigest()[:20]}.json"

    def get(self, url: str, force: bool = False) -> str:
        return self.get_with_time(url, force)[0]

    def get_with_time(self, url: str, force: bool = False) -> tuple[str, datetime]:
        """Page body and the time it was actually downloaded (older when served from the cache)."""
        cache = self._cache_path(url)
        if cache.exists() and not force:
            age_min = (time.time() - cache.stat().st_mtime) / 60
            if age_min < self.settings.cache_ttl_minutes:
                log.info("Cache hit for %s (%.0f min old)", url, age_min)
                data = json.loads(cache.read_text(encoding="utf-8"))
                return data["body"], datetime.fromtimestamp(data["fetched"])

        host = urlsplit(url).netloc
        robots = self._robots_for(url)
        if robots is not None and not robots.can_fetch(self.settings.user_agent, url):
            raise ScrapeError(_("robots.txt of {host} does not allow fetching {url}", host=host, url=url))
        self._wait_for(host, robots)

        body = self._request(url)
        fetched = time.time()
        cache.write_text(json.dumps({"url": url, "fetched": fetched, "body": body}), encoding="utf-8")
        return body, datetime.fromtimestamp(fetched)

    def _robots_for(self, url: str) -> RobotFileParser | None:
        if not self.settings.respect_robots_txt:
            return None
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            parser = RobotFileParser()
            try:
                self._wait_for(host, None)
                resp = self._client.get(f"{parts.scheme}://{host}/robots.txt")
                self._last_request[host] = time.monotonic()
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    parser.parse([])  # no robots.txt -> everything allowed
            except httpx.HTTPError as exc:
                log.warning("robots.txt of %s not available (%s), assuming allowed", host, exc)
                parser.parse([])
            self._robots[host] = parser
        return self._robots[host]

    def _wait_for(self, host: str, robots: RobotFileParser | None) -> None:
        delay = self.settings.min_delay_seconds
        if robots is not None:
            crawl_delay = robots.crawl_delay(self.settings.user_agent)
            if crawl_delay:
                delay = max(delay, float(crawl_delay))
        last = self._last_request.get(host)
        if last is not None:
            remaining = delay - (time.monotonic() - last)
            if remaining > 0:
                log.debug("Waiting %.1f s before next request to %s", remaining, host)
                time.sleep(remaining)

    def _request(self, url: str) -> str:
        host = urlsplit(url).netloc
        last_error: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                log.info("Fetching %s (attempt %d)", url, attempt)
                resp = self._client.get(url)
                self._last_request[host] = time.monotonic()
                if resp.status_code in (401, 403):
                    raise ScrapeError(_(
                        "{host} refused access (HTTP {status}). The bookmaker blocks automated access; "
                        "PollEdge does not bypass it - import the odds manually.",
                        host=host, status=resp.status_code,
                    ))
                if resp.status_code == 404:
                    raise ScrapeError(_("Page {url} does not exist (HTTP 404) - fix the address in the election config", url=url))
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.text
            except ScrapeError:
                raise
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Request to %s failed: %s", url, exc)
                if attempt < self.settings.max_retries:
                    time.sleep(min(5 * attempt, 30))
        raise ScrapeError(_("Failed to download {url}: {error}", url=url, error=last_error))

    def close(self) -> None:
        self._client.close()
