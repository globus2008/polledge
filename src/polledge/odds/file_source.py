"""Manually entered odds from CSV or YAML files."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path

import yaml

from polledge.presentation import canonical_bookmaker
from polledge.timeutil import local_to_utc
from polledge.errors import PollEdgeError
from polledge.model.markets import BINARY_OUTCOMES, parse_market
from polledge.odds.base import OddsQuote, OddsSource
from polledge.i18n import _

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("election_id", "bookmaker", "market", "outcome", "odds", "timestamp")


def _parse_timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
                "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(_("unknown date/time format '{value}'", value=text))


def quote_from_record(record: dict, where: str) -> OddsQuote:
    missing = [f for f in REQUIRED_FIELDS if record.get(f) in (None, "")]
    if missing:
        raise ValueError(_("{where}: missing fields {fields}", where=where, fields=", ".join(missing)))
    market = str(record["market"]).strip()
    spec = parse_market(market)  # validates the market id
    outcome = str(record["outcome"]).strip()
    if spec.kind in BINARY_OUTCOMES:
        outcome = outcome.lower()
        if outcome not in BINARY_OUTCOMES[spec.kind]:
            raise ValueError(_("{where}: outcome '{outcome}' of market {market} must be {allowed}", where=where, outcome=outcome, market=market, allowed="/".join(BINARY_OUTCOMES[spec.kind])))
    try:
        odds = float(str(record["odds"]).replace(",", "."))
    except ValueError:
        raise ValueError(_("{where}: odds '{odds}' is not a number", where=where, odds=record["odds"])) from None
    if odds <= 1.0:
        raise ValueError(_("{where}: decimal odds must be > 1 (got {odds})", where=where, odds=odds))
    return OddsQuote(
        election_id=str(record["election_id"]).strip(),
        bookmaker=canonical_bookmaker(str(record["bookmaker"])),
        market=spec.id,
        outcome=outcome,
        odds=odds,
        # People write local (Prague) time; the database stores UTC.
        timestamp=local_to_utc(_parse_timestamp(record["timestamp"])),
    )


class _FileOddsSource(OddsSource):
    def __init__(self, path: Path, strict: bool = False):
        self.path = path
        self.strict = strict
        self.errors: list[str] = []

    def _records(self) -> list[tuple[str, dict]]:
        raise NotImplementedError

    def fetch(self, election_id: str | None = None) -> list[OddsQuote]:
        if not self.path.exists():
            raise PollEdgeError(_("Odds file not found: {path}", path=self.path))
        quotes = []
        self.errors = []
        for where, record in self._records():
            try:
                quote = quote_from_record(record, where)
            except ValueError as exc:
                if self.strict:
                    raise PollEdgeError(str(exc)) from exc
                log.warning("Skipping odds record: %s", exc)
                self.errors.append(str(exc))
                continue
            if election_id is None or quote.election_id == election_id:
                quotes.append(quote)
        return quotes


class CsvOddsSource(_FileOddsSource):
    """CSV with header: election_id,bookmaker,market,outcome,odds,timestamp (',' or ';' separated)."""

    name = "csv"

    def _records(self) -> list[tuple[str, dict]]:
        with self.path.open(encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(2048)
            fh.seek(0)
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
            reader = csv.DictReader((line for line in fh if line.strip() and not line.lstrip().startswith("#")),
                                    delimiter=delimiter)
            missing = [f for f in REQUIRED_FIELDS if f not in (reader.fieldnames or [])]
            if missing:
                raise PollEdgeError(_("CSV {path} lacks columns: {columns}", path=self.path, columns=", ".join(missing)))
            return [(f"{self.path.name}:{i + 2}", row) for i, row in enumerate(reader)]


class YamlOddsSource(_FileOddsSource):
    """YAML: either a list of flat records, or grouped form

        - election_id: fr_pres_2027
          bookmaker: Tipsport
          timestamp: 2026-10-27 18:00
          markets:
            winner: {LE_PEN: 1.8, PHILIPPE: 3.5}
            qualify_runoff:PHILIPPE: {yes: 1.9, no: 1.8}
    """

    name = "yaml"

    def _records(self) -> list[tuple[str, dict]]:
        with self.path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or []
        if isinstance(data, dict):
            data = data.get("quotes", [data])
        records = []
        for i, item in enumerate(data):
            where = f"{self.path.name}#{i + 1}"
            if "markets" in item:
                for market, outcomes in (item.get("markets") or {}).items():
                    for outcome, odds in (outcomes or {}).items():
                        # YAML 1.1 turns bare yes/no into booleans
                        if isinstance(outcome, bool):
                            outcome = "yes" if outcome else "no"
                        records.append((where, {**item, "market": market, "outcome": outcome, "odds": odds}))
            else:
                records.append((where, item))
        return records


def source_for_path(path: Path) -> OddsSource:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return CsvOddsSource(path)
    if suffix in (".yaml", ".yml"):
        return YamlOddsSource(path)
    raise PollEdgeError(_("Unsupported odds file format: {suffix} (use .csv or .yaml)", suffix=path.suffix))
