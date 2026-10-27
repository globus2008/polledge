"""Paper-trading journal: virtual bets, model snapshots, settlement and scoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from polledge.config import ElectionResult
from polledge.db import ElectionResultRow, ModelSnapshotRow, PaperBetRow
from polledge.model.markets import BINARY_OUTCOMES, parse_market
from polledge.signals.compare import MarketComparison, OutcomeComparison

log = logging.getLogger(__name__)


def record_signals(
    session: Session, election_id: str, comparisons: list[MarketComparison]
) -> list[OutcomeComparison]:
    """Store every current signal as a virtual bet; the same open bet at the same odds is not duplicated.

    Returns the signals that were newly recorded (used for notifications).
    """
    created: list[OutcomeComparison] = []
    now = datetime.now()
    for comp in comparisons:
        for o in comp.signals:
            exists = session.scalars(
                select(PaperBetRow).where(
                    PaperBetRow.election_id == election_id,
                    PaperBetRow.market == o.market,
                    PaperBetRow.outcome == o.outcome,
                    PaperBetRow.bookmaker == o.best_bookmaker,
                    PaperBetRow.odds == o.best_odds,
                    PaperBetRow.status == "open",
                )
            ).first()
            if exists:
                continue
            session.add(
                PaperBetRow(
                    election_id=election_id,
                    created_at=now,
                    market=o.market,
                    outcome=o.outcome,
                    bookmaker=o.best_bookmaker,
                    odds=o.best_odds,
                    p_model=o.p_model,
                    p_market=o.p_market,
                    edge=o.edge,
                    stake=round(o.stake, 2),
                    status="open",
                )
            )
            created.append(o)
    session.commit()
    return created


def record_snapshot(
    session: Session,
    election_id: str,
    probabilities: dict[str, dict[str, float]],
    params: dict,
    market_probs: dict[str, dict[str, float]] | None = None,
    created_at: datetime | None = None,
) -> int:
    created_at = created_at or datetime.now()
    count = 0
    for market, outcomes in probabilities.items():
        for outcome, p in outcomes.items():
            p_market = (market_probs or {}).get(market, {}).get(outcome)
            session.add(ModelSnapshotRow(election_id=election_id, created_at=created_at, market=market,
                                         outcome=outcome, probability=p, p_market=p_market, params=params))
            count += 1
    session.commit()
    return count


def winning_outcomes(market: str, result: ElectionResult) -> set[str] | None:
    """Outcomes that won, or None if the result does not decide the market (bet is void)."""
    spec = parse_market(market)
    first = result.first_round
    if not first:
        return None
    ranking = sorted(first, key=first.get, reverse=True)
    finalists = set(result.runoff) if result.runoff else set(ranking[:2])

    if spec.kind == "first_round_winner":
        return {ranking[0]}
    if spec.kind == "winner":
        if result.runoff:
            return {max(result.runoff, key=result.runoff.get)}
        return {ranking[0]}
    if spec.kind == "finalists":
        return {"|".join(sorted(finalists))}
    yes, no = BINARY_OUTCOMES[spec.kind]
    if spec.kind == "qualify_runoff":
        return {yes if spec.candidate in finalists else no}
    if spec.candidate not in first:
        return None
    share = first[spec.candidate]
    hit = share >= spec.value if spec.kind == "threshold" else share > spec.value
    return {yes if hit else no}


def store_result(session: Session, election_id: str, result: ElectionResult) -> None:
    row = session.get(ElectionResultRow, election_id)
    payload = result.model_dump()
    if row is None:
        session.add(ElectionResultRow(election_id=election_id, payload=payload, entered_at=datetime.now()))
    else:
        row.payload = payload
        row.entered_at = datetime.now()
    session.commit()


@dataclass
class SettlementReport:
    n_bets: int
    n_won: int
    n_lost: int
    n_void: int
    staked: float
    profit: float
    brier_model: float | None
    brier_market: float | None
    brier_model_same_rows: float | None  # model Brier on the rows that have a market probability
    n_scored: int
    n_scored_market: int

    @property
    def roi(self) -> float | None:
        return self.profit / self.staked if self.staked else None


def settle(session: Session, election_id: str, result: ElectionResult) -> SettlementReport:
    store_result(session, election_id, result)
    now = datetime.now()
    bets = session.scalars(select(PaperBetRow).where(PaperBetRow.election_id == election_id)).all()
    for bet in bets:
        if bet.status != "open":
            continue
        winners = winning_outcomes(bet.market, result)
        if winners is None:
            bet.status, bet.profit = "void", 0.0
        elif bet.outcome in winners:
            bet.status, bet.profit = "won", round(bet.stake * (bet.odds - 1.0), 2)
        else:
            bet.status, bet.profit = "lost", -bet.stake
        bet.settled_at = now
    session.commit()

    settled = [b for b in bets if b.status in ("won", "lost")]
    brier = score_snapshots(session, election_id, result)
    return SettlementReport(
        n_bets=len(bets),
        n_won=sum(b.status == "won" for b in bets),
        n_lost=sum(b.status == "lost" for b in bets),
        n_void=sum(b.status == "void" for b in bets),
        staked=sum(b.stake for b in settled),
        profit=sum(b.profit or 0.0 for b in settled),
        **brier,
    )


def brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def score_snapshots(session: Session, election_id: str, result: ElectionResult) -> dict:
    rows = session.scalars(select(ModelSnapshotRow).where(ModelSnapshotRow.election_id == election_id)).all()
    model_pairs, market_pairs, model_same = [], [], []
    for r in rows:
        winners = winning_outcomes(r.market, result)
        if winners is None:
            continue
        y = int(r.outcome in winners)
        model_pairs.append((r.probability, y))
        if r.p_market is not None:
            market_pairs.append((r.p_market, y))
            model_same.append((r.probability, y))
    return {
        "brier_model": brier(model_pairs),
        "brier_market": brier(market_pairs),
        "brier_model_same_rows": brier(model_same),
        "n_scored": len(model_pairs),
        "n_scored_market": len(market_pairs),
    }
