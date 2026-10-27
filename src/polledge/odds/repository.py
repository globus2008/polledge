"""Storing odds quotes in the database (dedup on bookmaker + market + outcome + timestamp)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from polledge.timeutil import utcnow
from polledge.db import OddsQuoteRow
from polledge.odds.base import OddsQuote


def store_quotes(session: Session, quotes: list[OddsQuote], source: str) -> tuple[int, int]:
    """Returns (inserted, updated)."""
    inserted = updated = 0
    now = utcnow()
    for q in quotes:
        row = session.scalars(
            select(OddsQuoteRow).where(
                OddsQuoteRow.election_id == q.election_id,
                OddsQuoteRow.bookmaker == q.bookmaker,
                OddsQuoteRow.market == q.market,
                OddsQuoteRow.outcome == q.outcome,
                OddsQuoteRow.timestamp == q.timestamp,
            )
        ).one_or_none()
        if row is None:
            session.add(OddsQuoteRow(election_id=q.election_id, bookmaker=q.bookmaker, market=q.market,
                                     outcome=q.outcome, odds=q.odds, timestamp=q.timestamp, source=source,
                                     imported_at=now))
            inserted += 1
        elif row.odds != q.odds:
            row.odds = q.odds
            row.imported_at = now
            updated += 1
    session.commit()
    return inserted, updated


def quote_rows(session: Session, election_id: str, limit: int = 500) -> list[OddsQuoteRow]:
    """Stored quotes, newest first (for listing and deleting in the web app)."""
    query = (
        select(OddsQuoteRow)
        .where(OddsQuoteRow.election_id == election_id)
        .order_by(OddsQuoteRow.timestamp.desc(), OddsQuoteRow.id.desc())
        .limit(limit)
    )
    return list(session.scalars(query))


def delete_quotes(session: Session, ids: list[int]) -> int:
    rows = session.scalars(select(OddsQuoteRow).where(OddsQuoteRow.id.in_(ids))).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def load_quotes(session: Session, election_id: str, until: datetime | None = None) -> list[OddsQuote]:
    query = select(OddsQuoteRow).where(OddsQuoteRow.election_id == election_id)
    if until is not None:
        query = query.where(OddsQuoteRow.timestamp <= until)
    return [
        OddsQuote(r.election_id, r.bookmaker, r.market, r.outcome, r.odds, r.timestamp)
        for r in session.scalars(query)
    ]
