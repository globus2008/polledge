"""Page cache stored in the database (table http_cache), shared by the Wikipedia and bookmaker clients."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from polledge.db import HttpCacheRow


@dataclass
class CachedPage:
    body: str
    fetched_at: datetime

    def age_hours(self, now: datetime | None = None) -> float:
        return ((now or datetime.now()) - self.fetched_at).total_seconds() / 3600


class PageCache:
    def __init__(self, sessions: sessionmaker, namespace: str):
        self.sessions = sessions
        self.namespace = namespace

    def _key(self, url: str) -> str:
        return hashlib.sha256(f"{self.namespace}|{url}".encode("utf-8")).hexdigest()

    def get(self, url: str) -> CachedPage | None:
        with self.sessions() as session:
            row = session.get(HttpCacheRow, self._key(url))
            return CachedPage(row.body, row.fetched_at) if row is not None else None

    def put(self, url: str, body: str, fetched_at: datetime | None = None) -> datetime:
        fetched_at = fetched_at or datetime.now()
        with self.sessions() as session:
            row = session.get(HttpCacheRow, self._key(url))
            if row is None:
                session.add(HttpCacheRow(key=self._key(url), url=url[:1024], body=body, fetched_at=fetched_at))
            else:
                row.body, row.fetched_at = body, fetched_at
            session.commit()
        return fetched_at
