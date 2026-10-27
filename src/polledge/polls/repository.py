"""Persisting polls with de-duplication (election + round + pollster + end date + scenario)."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from polledge.db import PollRow, PollShareRow
from polledge.polls.types import Poll


def upsert_polls(session: Session, polls: list[Poll]) -> tuple[int, int]:
    """Insert new polls, update shares of already stored ones. Returns (inserted, updated).

    Stored rows of the same poll (election, round, pollster, date) whose scenario no longer
    appears in the new data are removed - they are stale versions left after a config change.
    Polls that are missing from the new data altogether are kept.
    """
    inserted = updated = 0
    now = datetime.now()
    fresh: dict[tuple, set[str]] = {}
    for poll in polls:
        fresh.setdefault((poll.election_id, poll.round, poll.pollster, poll.end_date), set()).add(poll.scenario)
    for (election_id, round_name, pollster, end_date), scenarios in fresh.items():
        stale = session.scalars(
            select(PollRow).where(
                PollRow.election_id == election_id,
                PollRow.round == round_name,
                PollRow.pollster == pollster,
                PollRow.end_date == end_date,
                PollRow.scenario.not_in(scenarios),
            )
        ).all()
        for row in stale:
            session.delete(row)
            updated += 1
    session.flush()
    for poll in polls:
        row = session.scalars(
            select(PollRow).where(
                PollRow.election_id == poll.election_id,
                PollRow.round == poll.round,
                PollRow.pollster == poll.pollster,
                PollRow.end_date == poll.end_date,
                PollRow.scenario == poll.scenario,
            )
        ).one_or_none()
        new_shares = {k: round(v, 4) for k, v in poll.shares.items()}
        if row is None:
            row = PollRow(
                election_id=poll.election_id,
                round=poll.round,
                pollster=poll.pollster,
                end_date=poll.end_date,
                scenario=poll.scenario,
                sample_size=poll.sample_size,
                sample_imputed=poll.sample_imputed,
                fetched_at=now,
            )
            session.add(row)
            inserted += 1
        else:
            old_shares = {s.candidate: s.share for s in row.shares}
            if old_shares == new_shares and row.sample_size == poll.sample_size:
                continue
            row.shares.clear()
            row.sample_size = poll.sample_size
            row.sample_imputed = poll.sample_imputed
            row.fetched_at = now
            updated += 1
        row.shares.extend(PollShareRow(candidate=c, share=v) for c, v in new_shares.items())
    session.commit()
    return inserted, updated


def load_polls(
    session: Session, election_id: str, round_name: str | None = None, before: date | None = None
) -> list[Poll]:
    query = select(PollRow).where(PollRow.election_id == election_id)
    if round_name is not None:
        query = query.where(PollRow.round == round_name)
    if before is not None:
        query = query.where(PollRow.end_date < before)
    return [
        Poll(
            election_id=row.election_id,
            round=row.round,
            pollster=row.pollster,
            end_date=row.end_date,
            sample_size=row.sample_size,
            sample_imputed=row.sample_imputed,
            scenario=row.scenario,
            shares={s.candidate: s.share for s in row.shares},
        )
        for row in session.scalars(query.order_by(PollRow.end_date))
    ]
