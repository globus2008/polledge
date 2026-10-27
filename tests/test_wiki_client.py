from datetime import datetime, timedelta

import httpx
import pytest

from polledge.timeutil import utcnow
from polledge.cache import PageCache
from polledge.config import WikipediaSettings
from polledge.db import make_session_factory
from polledge.errors import FetchError
from polledge.polls import wiki_client
from polledge.polls.wiki_client import WikiClient


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(wiki_client.time, "sleep", lambda s: None)


def _client(tmp_path, handler, ttl=6.0):
    settings = WikipediaSettings(max_retries=2, cache_ttl_hours=ttl, user_agent="PollEdge-test/0.1")
    cache = PageCache(make_session_factory(":memory:"), "wikipedia")
    return WikiClient(settings, cache, transport=httpx.MockTransport(handler))


def test_fetch_sends_user_agent_and_caches(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"parse": {"text": "<p>ok</p>"}})

    client = _client(tmp_path, handler)
    assert client.get_page_html("Some page") == "<p>ok</p>"
    assert client.get_page_html("Some page") == "<p>ok</p>"
    assert len(calls) == 1  # second call served from cache
    assert calls[0].headers["User-Agent"] == "PollEdge-test/0.1"
    assert calls[0].url.params["action"] == "parse"


def test_server_errors_raise_fetch_error(tmp_path):
    client = _client(tmp_path, lambda r: httpx.Response(503))
    with pytest.raises(FetchError):
        client.get_page_html("Page")


def test_network_failure_falls_back_to_stale_cache(tmp_path):
    def broken(request):
        raise httpx.ConnectError("offline", request=request)

    client = _client(tmp_path, broken, ttl=1.0)
    client.cache.put("Page", "<p>old</p>", fetched_at=utcnow() - timedelta(hours=5))
    assert client.get_page_html("Page") == "<p>old</p>"


def test_api_error_is_not_retried(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error": {"code": "missingtitle", "info": "The page does not exist."}})

    with pytest.raises(FetchError, match="does not exist"):
        _client(tmp_path, handler).get_page_html("Nope")
    assert len(calls) == 1
