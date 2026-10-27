"""Human-readable labels shared by the CLI and the web app."""

from __future__ import annotations

from polledge.config import ElectionConfig
from polledge.i18n import _
from polledge.model.markets import parse_market


def outcome_label(election: ElectionConfig, outcome: str) -> str:
    if "|" in outcome:
        return " vs. ".join(election.candidate_name(c) for c in outcome.split("|"))
    labels = {"yes": _("yes"), "no": _("no"), "over": _("over"), "under": _("under")}
    return labels.get(outcome) or election.candidate_name(outcome)


def market_label(election: ElectionConfig | None, market: str) -> str:
    if election is None:
        return market
    names = {cid: election.candidate_name(cid) for cid in election.candidates}
    return parse_market(market).describe(names)
