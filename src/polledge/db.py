"""SQLAlchemy ORM schema and session factory."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class PollRow(Base):
    __tablename__ = "poll"
    __table_args__ = (UniqueConstraint("election_id", "round", "pollster", "end_date", "scenario"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[str] = mapped_column(String(64), index=True)
    round: Mapped[str] = mapped_column(String(16))  # "first" | "runoff"
    pollster: Mapped[str] = mapped_column(String(128))
    end_date: Mapped[date] = mapped_column(Date)
    sample_size: Mapped[int] = mapped_column(Integer)
    sample_imputed: Mapped[bool] = mapped_column(default=False)
    scenario: Mapped[str] = mapped_column(String(512))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    shares: Mapped[list["PollShareRow"]] = relationship(
        back_populates="poll", cascade="all, delete-orphan", lazy="selectin"
    )


class PollShareRow(Base):
    __tablename__ = "poll_share"

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("poll.id"), index=True)
    candidate: Mapped[str] = mapped_column(String(64))
    share: Mapped[float] = mapped_column(Float)
    poll: Mapped[PollRow] = relationship(back_populates="shares")


class OddsQuoteRow(Base):
    __tablename__ = "odds_quote"
    __table_args__ = (UniqueConstraint("election_id", "bookmaker", "market", "outcome", "timestamp"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[str] = mapped_column(String(64), index=True)
    bookmaker: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64))
    odds: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime)


class ModelSnapshotRow(Base):
    __tablename__ = "model_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    market: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64))
    probability: Mapped[float] = mapped_column(Float)
    p_market: Mapped[float | None] = mapped_column(Float, nullable=True)
    params: Mapped[dict] = mapped_column(JSON)


class PaperBetRow(Base):
    __tablename__ = "paper_bet"

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    market: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64))
    bookmaker: Mapped[str] = mapped_column(String(64))
    odds: Mapped[float] = mapped_column(Float)
    p_model: Mapped[float] = mapped_column(Float)
    p_market: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float] = mapped_column(Float)
    stake: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | won | lost | void
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ElectionResultRow(Base):
    __tablename__ = "election_result"

    election_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    entered_at: Mapped[datetime] = mapped_column(DateTime)


def make_session_factory(db_path: Path | str) -> sessionmaker:
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
