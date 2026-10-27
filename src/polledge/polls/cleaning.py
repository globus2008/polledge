"""Cleaning of raw Wikipedia table cells: fieldwork dates, percentages, sample sizes."""

from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9

_FOOTNOTE_RE = re.compile(r"\[[^\]]*\]")
_DASH_RE = re.compile(r"\s*[–—‒−-]\s*")
_MISSING = {"", "-", "–", "—", "n/a", "na", "?", "tba", "tbd"}


def clean_text(value: object) -> str:
    """Strip footnote markers like [a] or [12], non-breaking spaces and surrounding whitespace."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = _FOOTNOTE_RE.sub("", text)
    return " ".join(text.split())


def _parse_day_month_year(part: str, default_year: int | None) -> tuple[int | None, int | None, int | None]:
    day = month = year = None
    for token in re.split(r"[\s,.]+", part):
        if not token:
            continue
        low = token.lower()
        if low in MONTHS:
            month = MONTHS[low]
        elif token.isdigit():
            num = int(token)
            if num > 31:
                year = num
            elif day is None:
                day = num
    return day, month, year if year is not None else default_year


def parse_fieldwork_end(value: object, default_year: int | None = None) -> date | None:
    """Return the end date of a fieldwork period.

    Handles "12–15 Oct 2026", "28 Sep – 2 Oct 2026", "30 Dec 2026 – 3 Jan 2027",
    "22 Oct 2026" and "Oct 2026" (month only -> last day of month).
    Missing parts of the end date are inherited from the start of the range
    (e.g. "12 Oct – 15" is not typical but resolves to 15 Oct).
    """
    text = clean_text(value)
    if not text:
        return None
    parts = [p for p in _DASH_RE.split(text) if p]
    if not parts:
        return None

    end_day, end_month, end_year = _parse_day_month_year(parts[-1], None)
    if len(parts) > 1:
        start_day, start_month, start_year = _parse_day_month_year(parts[0], None)
        end_month = end_month or start_month
        end_year = end_year or start_year
    end_year = end_year or default_year
    if end_month is None or end_year is None:
        return None
    if end_day is None:
        end_day = calendar.monthrange(end_year, end_month)[1]
    try:
        return date(end_year, end_month, end_day)
    except ValueError:
        return None


def parse_percent(value: object) -> float | None:
    """Convert "23.5%", "23,5", "<1" (-> 0.5) or "–" (-> None) to a float in percent."""
    text = clean_text(value).replace("%", "").replace(" ", "")
    if text.lower() in _MISSING:
        return None
    less_than = text.startswith("<")
    text = text.lstrip("<>~≈")
    text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0 or number > 100:
        return None
    return number / 2 if less_than else number


def parse_sample_size(value: object) -> int | None:
    """Convert "1,500", "1 012" or "~1000" to int; missing values give None."""
    text = clean_text(value)
    if text.lower() in _MISSING:
        return None
    match = re.search(r"\d[\d,.\s]*", text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    number = int(digits)
    return number if number > 0 else None


_ARCHIVED_RE = re.compile(r"\s*Archived\b.*?(Wayback Machine|$)", re.IGNORECASE)


def clean_pollster(value: object) -> str:
    """Pollster name without footnotes and "Archived ... at the Wayback Machine" link texts."""
    return _ARCHIVED_RE.sub("", clean_text(value)).strip()


def normalize_name(value: object) -> str:
    """Case- and accent-insensitive form of a header/candidate name for matching."""
    text = clean_text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s*([\-–])\s*", r"\1", text)  # "Dupont- Aignan" -> "Dupont-Aignan"
    return " ".join(text.lower().split())
