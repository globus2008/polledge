"""Extract (selection, odds) pairs from text copied from a bookmaker page (Ctrl+A, Ctrl+C).

Layouts differ, so the parser is deliberately simple: an odds value is a number like 1.45 / 1,45 / 25
(between 1.01 and 1000) and belongs to the nearest preceding text line that looks like a name.
Both "Name 1.45" on one line and "Name" followed by "1.45" on the next line are understood, as are
table rows from Markdown ("| Name | 1.45 |", e.g. a screenshot converted by an AI chat) or a spreadsheet (tabs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from polledge.polls.cleaning import normalize_name

_ODDS = r"(\d{1,4}(?:[.,]\d{1,3})?)"
_ODDS_ONLY = re.compile(rf"^\s*{_ODDS}\s*$")
_NAME_THEN_ODDS = re.compile(rf"^\s*(.*?[^\d\s.,:+\-%].*?)\s+{_ODDS}\s*$")
_NOISE = re.compile(r"^[\d\s.,:+\-%/()]*$")
MIN_ODDS, MAX_ODDS = 1.01, 10000.0  # Betano lists celebrity long shots at 5000

# Words that appear next to odds on bookmaker pages but are never a selection.
_SKIP_WORDS = {
    "vsadit", "kurz", "kurzy", "tiket", "sazka", "sazky", "vitez", "dlouhodobe sazky", "vice", "zobrazit",
    "live", "politika", "cash out", "bet", "odds", "winner", "show more", "vsechny", "trh", "trhy",
    "kandidat", "kandidat kurz", "jmeno", "jmeno kurz", "candidate", "candidate odds", "name odds",
}
_MARKUP = re.compile(r"^(?:[#>]+\s*|[-*+]\s+)+|[*_`]+")
# Over/under lines are written as signed numbers: "+47.5" (over) and "-47.5" (under).
_SIGNED = re.compile(r"^[+-]\d{1,4}(?:[.,]\d{1,2})?$")
_SIGNED_THEN_ODDS = re.compile(rf"^\s*([+-]\d{{1,4}}(?:[.,]\d{{1,2}})?)\s+{_ODDS}\s*$")


def _clean_lines(raw: str) -> list[str]:
    """Plain text of a line; a table row ("| Ano | 1.36 | Ne | 2.76 |" or tab-separated) gives one line per
    cell, so it reads like the stacked layout. Markdown headings, bullets and emphasis are removed."""
    cells = re.split(r"[|\t]", raw) if "|" in raw or "\t" in raw else [raw]
    return [text for text in (_MARKUP.sub("", c.strip()).strip() for c in cells) if text]


def signed_line(name: str) -> tuple[str, float] | None:
    """("over" | "under", line) for a selection named "+47.5" / "-47.5"."""
    text = name.strip()
    if not _SIGNED.match(text):
        return None
    return ("over" if text[0] == "+" else "under"), float(text[1:].replace(",", "."))


@dataclass
class PastedSelection:
    name: str
    odds: float


def _to_odds(text: str) -> float | None:
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None
    if text.isdigit() and 1900 <= value <= 2100:  # a year in a heading ("volby 2028"), not odds
        return None
    return value if MIN_ODDS <= value <= MAX_ODDS else None


def _is_name(line: str) -> bool:
    text = line.strip()
    if _SIGNED.match(text):
        return True
    if len(text) < 2 or len(text) > 80 or _NOISE.match(text):
        return False
    return normalize_name(text) not in _SKIP_WORDS


def parse_pasted_odds(text: str) -> list[PastedSelection]:
    """Selections in page order; the first occurrence of a name wins (pages often repeat favourites)."""
    found: dict[str, PastedSelection] = {}
    last_name: str | None = None
    for line in (cell for raw in text.splitlines() for cell in _clean_lines(raw)):
        same_line = _SIGNED_THEN_ODDS.match(line) or _NAME_THEN_ODDS.match(line)
        if same_line and _is_name(same_line.group(1)) and _to_odds(same_line.group(2)):
            name, odds = same_line.group(1).strip(), _to_odds(same_line.group(2))
        elif _ODDS_ONLY.match(line) and last_name is not None and _to_odds(line):
            name, odds = last_name, _to_odds(line)
        else:
            last_name = line if _is_name(line) else last_name
            continue
        key = normalize_name(name)
        if key not in found:
            found[key] = PastedSelection(name, odds)
        last_name = None  # one odds value per name
    return list(found.values())
