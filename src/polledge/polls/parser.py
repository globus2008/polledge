"""Parse Wikipedia opinion-polling tables into Poll objects according to the election config."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from statistics import mean

from bs4 import BeautifulSoup, Tag

from polledge.config import OTHER, ElectionConfig, RoundConfig
from polledge.errors import TableStructureError
from polledge.polls.cleaning import (
    clean_pollster,
    clean_text,
    normalize_name,
    parse_fieldwork_end,
    parse_percent,
    parse_sample_size,
)
from polledge.polls.types import Poll
from polledge.i18n import _

log = logging.getLogger(__name__)

_PARTY_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
_PARTY_CODE_RE = re.compile(r"^[A-Z0-9!&/+\-]{1,8}$")


@dataclass
class WikiTable:
    index: int
    headings: list[str]
    element: Tag

    @property
    def title(self) -> str:
        return " > ".join(self.headings) or f"tabulka #{self.index}"


def extract_tables(html: str) -> list[WikiTable]:
    """Return all wikitables with the chain of section headings above each of them."""
    soup = BeautifulSoup(html, "lxml")
    path: dict[int, str] = {}
    tables: list[WikiTable] = []
    for el in soup.find_all(["h2", "h3", "h4", "h5", "h6", "table"]):
        if el.name == "table":
            if "wikitable" in (el.get("class") or []) and el.find_parent("table") is None:
                tables.append(WikiTable(len(tables), [path[k] for k in sorted(path)], el))
            continue
        level = int(el.name[1])
        path = {k: v for k, v in path.items() if k < level}
        path[level] = clean_text(el.get_text(" ", strip=True))
    return tables


def select_tables(tables: list[WikiTable], rnd: RoundConfig) -> list[WikiTable]:
    selected: dict[int, WikiTable] = {}
    for sel in rnd.tables:
        for t in tables:
            if sel.index is not None:
                if t.index == sel.index:
                    selected[t.index] = t
                continue
            path = [normalize_name(h) for h in t.headings]
            if all(any(normalize_name(want) in h for h in path) for want in sel.heading) and not any(
                normalize_name(bad) in h for bad in sel.exclude_heading for h in path
            ):
                selected[t.index] = t
    return [selected[i] for i in sorted(selected)]


def table_to_grid(table: Tag) -> tuple[list[list[str]], int]:
    """Expand rowspan/colspan into a rectangular grid of cell texts.

    Returns (grid, header_row_count). Header rows are the leading rows made only of <th>.
    """
    grid: list[list[str | None]] = []
    header_rows = 0
    in_header = True
    pending: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(table.find_all("tr")):
        cells = tr.find_all(["th", "td"], recursive=False)
        if in_header and cells and all(c.name == "th" for c in cells):
            header_rows += 1
        else:
            in_header = False
        row: list[str | None] = []
        col = 0
        for cell in cells:
            while (r, col) in pending:
                row.append(pending.pop((r, col)))
                col += 1
            text = clean_text(cell.get_text(" ", strip=True))
            rowspan = _int_attr(cell, "rowspan")
            colspan = _int_attr(cell, "colspan")
            for dc in range(colspan):
                # Header text is repeated across spanned columns (it names them); a data value
                # spanning several columns (e.g. one share for a coalition) is kept only once.
                value = text if in_header or dc == 0 else ""
                row.append(value)
                for dr in range(1, rowspan):
                    pending[(r + dr, col + dc)] = value
            col += colspan
        while (r, col) in pending:
            row.append(pending.pop((r, col)))
            col += 1
        grid.append(row)
    width = max((len(r) for r in grid), default=0)
    return [[c or "" for c in row] + [""] * (width - len(row)) for row in grid], header_rows


def _int_attr(cell: Tag, name: str) -> int:
    try:
        return max(1, int(str(cell.get(name, "1")).strip().rstrip(";")))
    except ValueError:
        return 1


def column_names(grid: list[list[str]], header_rows: int) -> list[str]:
    names = []
    for col in range(len(grid[0]) if grid else 0):
        parts: list[str] = []
        for r in range(header_rows):
            text = grid[r][col]
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        names.append(" ".join(parts))
    return names


def strip_party_label(header: str) -> str:
    """'Le Pen RN' -> 'Le Pen', 'Mélenchon (LFI)' -> 'Mélenchon'."""
    text = _PARTY_PAREN_RE.sub("", header).strip()
    tokens = text.split()
    while len(tokens) > 1 and _PARTY_CODE_RE.match(tokens[-1]):
        tokens.pop()
    return " ".join(tokens)


class _ColumnMap:
    def __init__(self, names: list[str], rnd: RoundConfig, election: ElectionConfig, table: WikiTable):
        norm = [normalize_name(n) for n in names]

        def find(wanted: str) -> int:
            w = normalize_name(wanted)
            for i, n in enumerate(norm):
                if n == w or n.startswith(w):
                    return i
            raise TableStructureError(_(
                "Table '{table}' has no column '{column}'. Columns found: {found}. "
                "The page structure has probably changed; update the election config.",
                table=table.title, column=wanted, found=names,
            ))

        self.pollster = find(rnd.columns.pollster)
        self.fieldwork = find(rnd.columns.fieldwork)
        self.sample = find(rnd.columns.sample_size) if rnd.columns.sample_size else None
        fixed = {self.pollster, self.fieldwork, self.sample} - {None}

        alias_to_id = {
            normalize_name(alias): cid for cid, cand in election.candidates.items() for alias in cand.all_names()
        }
        ignore = [normalize_name(x) for x in rnd.columns.ignore]
        self.candidates: dict[int, str] = {}
        self.other: list[int] = []
        for i, name in enumerate(names):
            if i in fixed or not name:
                continue
            n = normalize_name(name)
            if any(n == x or n.startswith((x + " ", x + ".")) for x in ignore):
                continue
            cid = alias_to_id.get(normalize_name(strip_party_label(name))) or alias_to_id.get(n)
            if cid is not None and cid not in self.candidates.values():
                self.candidates[i] = cid
            else:
                self.other.append(i)
        unmatched = [names[i] for i in self.other]
        if unmatched:
            log.debug("Table '%s': columns counted as OTHER: %s", table.title, unmatched)


def parse_round(
    tables: list[WikiTable], election: ElectionConfig, round_name: str, rnd: RoundConfig
) -> list[Poll]:
    selected = select_tables(tables, rnd)
    if not selected:
        round_label = _("first round") if round_name == "first" else _("runoff")
        raise TableStructureError(_(
            "No table found for the {round} of election {id}. Check the headings in the config (tables.heading).",
            round=round_label, id=election.id,
        ))
    polls: list[Poll] = []
    for table in selected:
        polls.extend(_parse_table(table, election, round_name, rnd))
    return merge_duplicates(polls)


def _parse_table(table: WikiTable, election: ElectionConfig, round_name: str, rnd: RoundConfig) -> list[Poll]:
    grid, header_rows = table_to_grid(table.element)
    if header_rows == 0 or len(grid) <= header_rows:
        raise TableStructureError(_("Table '{table}' has no recognisable header.", table=table.title))
    names = column_names(grid, header_rows)
    cmap = _ColumnMap(names, rnd, election, table)

    if round_name == "runoff" and len(cmap.candidates) != 2:
        log.info(
            "Skipping runoff table '%s': expected 2 known candidates, found %s",
            table.title,
            list(cmap.candidates.values()),
        )
        return []

    polls = []
    skipped = 0
    for row in grid[header_rows:]:
        poll = _parse_row(row, cmap, election, round_name, rnd)
        if poll is None:
            skipped += 1
        else:
            polls.append(poll)
    log.info("Table '%s': %d polls parsed, %d rows skipped", table.title, len(polls), skipped)
    return polls


def _parse_row(
    row: list[str], cmap: _ColumnMap, election: ElectionConfig, round_name: str, rnd: RoundConfig
) -> Poll | None:
    pollster = clean_pollster(row[cmap.pollster])
    end = parse_fieldwork_end(row[cmap.fieldwork], default_year=election.election_date.year)
    if not pollster or end is None or len(set(row)) <= 2:
        return None  # note rows, separators, rows spanning the whole width
    if any(normalize_name(s) in normalize_name(pollster) for s in rnd.skip_rows):
        return None

    shares = {cid: parse_percent(row[i]) for i, cid in cmap.candidates.items()}
    shares = {cid: v for cid, v in shares.items() if v is not None}
    other_values = [parse_percent(row[i]) for i in cmap.other]
    other = sum(v for v in other_values if v is not None)
    if not shares:
        return None

    present = set(shares)
    if any(c not in present for c in rnd.scenario.require) or any(c in present for c in rnd.scenario.exclude):
        return None

    total = sum(shares.values()) + other
    if round_name == "runoff":
        if len(shares) != 2 or total <= 0:
            return None
        shares = {cid: 100.0 * v / total for cid, v in shares.items()}
    else:
        low, high = rnd.row_total
        if not (low <= total <= high):
            log.debug("Row of %s %s skipped: shares sum to %.1f", pollster, end, total)
            return None
        # Residual up to 100 is attributed to minor candidates not listed in the table.
        other = other + max(0.0, 100.0 - total)
        if other > 0:
            shares[OTHER] = other

    sample = parse_sample_size(row[cmap.sample]) if cmap.sample is not None else None
    return Poll(
        election_id=election.id,
        round=round_name,
        pollster=pollster,
        end_date=end,
        sample_size=sample or election.default_sample_size,
        sample_imputed=sample is None,
        shares=shares,
        scenario="|".join(sorted(c for c in shares if c != OTHER)),
    )


def merge_duplicates(polls: list[Poll]) -> list[Poll]:
    """Average rows with the same pollster, date and line-up (e.g. sub-variants of a scenario)."""
    groups: dict[tuple, list[Poll]] = {}
    for p in polls:
        groups.setdefault(p.key, []).append(p)
    merged = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        base = group[0]
        keys = set().union(*(p.shares for p in group))
        base.shares = {k: mean(p.shares.get(k, 0.0) for p in group) for k in keys}
        merged.append(base)
    return merged


def parse_election_html(html: str, election: ElectionConfig) -> list[Poll]:
    tables = extract_tables(html)
    if not tables:
        raise TableStructureError(_("Page '{page}' contains no poll tables.", page=election.wiki_page))
    polls = parse_round(tables, election, "first", election.first_round)
    if election.runoff is not None:
        polls += parse_round(tables, election, "runoff", election.runoff)
    return polls
