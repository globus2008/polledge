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
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool


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


class AiReviewRow(Base):
    """AI second opinion on one signal (outcome at given odds), optionally linked to the recorded bet."""

    __tablename__ = "ai_review"

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    market: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(64))
    bookmaker: Mapped[str] = mapped_column(String(64))
    odds: Mapped[float] = mapped_column(Float)
    kelly_stake: Mapped[float] = mapped_column(Float)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    verdict: Mapped[str] = mapped_column(String(16))
    stake_multiplier: Mapped[float] = mapped_column(Float)
    probability_estimate: Mapped[float] = mapped_column(Float)
    result: Mapped[dict] = mapped_column(JSON)  # full review incl. reasons, risks, sources
    context: Mapped[dict] = mapped_column(JSON)  # what the AI was given
    bet_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class HttpCacheRow(Base):
    """Downloaded pages (Wikipedia, bookmakers), so repeated runs do not hit the sites again."""

    __tablename__ = "http_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(1024))
    body: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)


class AppSettingRow(Base):
    """Small key/value store, e.g. calibrated model parameters."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


def make_engine(url: str) -> Engine:
    """Engine for a SQLAlchemy URL; creates the SQLite directory and the schema if needed."""
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"pool_pre_ping": True} if not url.startswith("sqlite") else {}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(db: Path | str) -> sessionmaker:
    """Session factory for a SQLAlchemy URL, a SQLite file path or ":memory:"."""
    text = str(db)
    if text == ":memory:":
        url = "sqlite:///:memory:"
    elif "://" in text:
        url = text
    else:
        url = f"sqlite:///{text}"
    if url == "sqlite:///:memory:":
        # One shared connection, otherwise every session would see a new empty database.
        engine = create_engine(url, poolclass=StaticPool, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
    else:
        engine = make_engine(url)
    return sessionmaker(engine, expire_on_commit=False)
