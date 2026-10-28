"""Human-readable labels shared by the CLI and the web app."""

from __future__ import annotations

from polledge.config import ElectionConfig
from polledge.i18n import _
from polledge.model.markets import parse_market


_BOOKMAKER_NAMES = {
    "ifortuna": "Fortuna", "ifortuna.cz": "Fortuna", "fortuna": "Fortuna", "fortuna.cz": "Fortuna",
    "tipsport": "Tipsport", "tipsport.cz": "Tipsport", "betano": "Betano", "betano.cz": "Betano",
    "chance": "Chance", "chance.cz": "Chance", "synot": "Synot Tip", "synottip": "Synot Tip",
    "synot tip": "Synot Tip", "allwyn": "Allwyn", "sazka": "Allwyn", "sazka bet": "Allwyn",
    "betx": "BetX", "forbet": "Forbet", "merkur": "Merkur X-Tip", "merkurxtip": "Merkur X-Tip",
    "kingsbet": "Kingsbet", "mr bit": "Mr Bit", "mrbit": "Mr Bit",
}


def canonical_bookmaker(name: str) -> str:
    """One spelling per bookmaker ("iFortuna" -> "Fortuna"), so they compare as one book."""
    text = name.strip()
    key = text.lower().removeprefix("www.")
    return _BOOKMAKER_NAMES.get(key, text)


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
