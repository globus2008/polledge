"""Configuration models and loaders (settings.yaml, per-election YAML, calibrated overrides)."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from polledge.errors import ConfigError
from polledge.i18n import _

log = logging.getLogger(__name__)

OTHER = "OTHER"


class WikipediaSettings(BaseModel):
    api_url: str = "https://en.wikipedia.org/w/api.php"
    user_agent: str = "PollEdge/0.1 (election poll research tool)"
    cache_ttl_hours: float = 6.0
    timeout_seconds: float = 20.0
    max_retries: int = 3


class ModelParams(BaseModel):
    half_life_days: float = 14.0
    max_poll_age_days: int = 180
    house_effects: bool = True
    house_min_polls: int = 3
    house_shrinkage: float = 0.0  # m in k/(k+m); 0 disables shrinkage
    sigma_sys_pp: float = 2.5
    sigma_horizon_pp_per_sqrt_day: float = 0.0  # extra error: b * sqrt(days to election)
    sigma_runoff_pp: float = 3.0
    sigma_runoff_fallback_pp: float = 6.0
    # Below this share the systematic error shrinks with sqrt(share / ref); 0 disables it.
    small_party_ref_pct: float = Field(10.0, ge=0.0)
    n_simulations: int = Field(10_000, ge=100)
    # Systematic error of runoff head-to-head polls by party (actual minus polls, pp), e.g. {"RN": -3.0}:
    # head-to-head polls overstated Le Pen in 2017 and 2022. Estimated by `polledge calibrate`.
    runoff_bias_pp: dict[str, float] = {}
    seed: int | None = None
    min_share_pct: float = 0.1


class SignalSettings(BaseModel):
    edge_threshold: float = 0.05
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_stake_pct: float | None = 0.05
    # A signal needs a recommended stake of at least this amount. Kelly stakes shrink with the odds,
    # so this also filters fragile long-shot "edges" built on a fraction of a percentage point.
    min_stake: float = 0.0
    currency: str = "CZK"
    # Sources used only as a market reference (fair probability), never for best odds, signals or stakes.
    reference_bookmakers: list[str] = ["Polymarket"]


class ScrapingSettings(BaseModel):
    """Polite HTTP access to bookmaker pages (public offer pages only, never accounts)."""

    user_agent: str = "Mozilla/5.0 (compatible; PollEdge/0.2; personal odds research)"
    cache_ttl_minutes: float = 30.0
    min_delay_seconds: float = 5.0  # between requests to one host; robots.txt Crawl-delay wins if longer
    timeout_seconds: float = 20.0
    max_retries: int = 2
    respect_robots_txt: bool = True


class EmailSettings(BaseModel):
    smtp_host: str = ""
    smtp_port: int = 587
    use_tls: bool = True
    username: str = ""
    password_env: str = "POLLEDGE_SMTP_PASSWORD"  # the password is read from this environment variable
    sender: str = ""
    recipients: list[str] = []


class NotifySettings(BaseModel):
    channels: list[Literal["console", "email"]] = ["console"]
    email: EmailSettings = EmailSettings()


class Settings(BaseModel):
    language: Literal["en", "cs"] = "cs"  # language of the output (catalog in polledge/locales)
    # Local SQLite file, used when no database URL is given (env POLLEDGE_DATABASE_URL or database_url).
    database: Path = Path("data/polledge.db")
    database_url: str | None = None  # e.g. postgresql://user:pass@host/db; prefer the env variable for secrets
    log_dir: Path | None = Path("logs")  # None: log to the console only
    elections_dir: Path = Path("config/elections")
    # Legacy file with calibrated parameters; imported into the database once if it exists.
    calibrated_file: Path = Path("config/calibrated.yaml")
    wikipedia: WikipediaSettings = WikipediaSettings()
    model: ModelParams = ModelParams()
    signals: SignalSettings = SignalSettings()
    scraping: ScrapingSettings = ScrapingSettings()
    notify: NotifySettings = NotifySettings()

    base_dir: Path = Path(".")
    # Model params from settings.yaml before calibrated overrides (baseline for calibration).
    uncalibrated_model: ModelParams | None = None

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.base_dir / path

    def db_url(self) -> str:
        """SQLAlchemy URL: env POLLEDGE_DATABASE_URL > database_url > local SQLite file."""
        url = os.environ.get("POLLEDGE_DATABASE_URL") or self.database_url
        if not url:
            return f"sqlite:///{self.resolve(self.database)}"
        # Hosted Postgres providers give postgres:// or postgresql:// URLs; use the psycopg 3 driver.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


class Candidate(BaseModel):
    name: str
    aliases: list[str] = []
    party: str | None = None  # party family, e.g. "RN"; used for the runoff poll-bias correction

    def all_names(self) -> list[str]:
        """Full name, explicit aliases and the surname (name without the first word)."""
        names = [self.name, *self.aliases]
        parts = self.name.split(maxsplit=1)
        if len(parts) == 2:
            names.append(parts[1])
        return names


class TableSelector(BaseModel):
    """Select tables by the chain of section headings above them (substring match)."""

    heading: list[str] = []
    exclude_heading: list[str] = []
    index: int | None = None  # alternative: absolute index among wikitables


class ColumnNames(BaseModel):
    pollster: str = "Polling firm"
    fieldwork: str = "Fieldwork date"
    sample_size: str | None = "Sample size"  # None: the table has no sample column
    # Non-candidate columns to drop (matched by prefix, case/accent-insensitive).
    ignore: list[str] = [
        "Lead", "Abs", "Undecided", "Margin", "Ref", "Spread", "Turnout", "No vote", "Don't know",
        "Govt", "Opp",
    ]


class Scenario(BaseModel):
    """Which rows (hypothetical candidate line-ups) to keep."""

    require: list[str] = []
    exclude: list[str] = []


class NamedScenario(Scenario):
    """One of several candidate line-ups; the model mixes them by weight."""

    name: str
    weight: float = Field(1.0, gt=0)


class RoundConfig(BaseModel):
    tables: list[TableSelector]
    columns: ColumnNames = ColumnNames()
    scenario: Scenario = Scenario()
    # Several line-ups instead of `scenario`, e.g. "Philippe alone" 0.6, "Philippe + Attal" 0.25 ...
    scenarios: list[NamedScenario] = []
    # Rows whose pollster cell contains one of these (e.g. the official result row) are skipped.
    skip_rows: list[str] = ["election", "result", "exit poll"]
    # Accepted sum of shares in one row (%). Rows outside are skipped; rows above 100 are rescaled
    # by the model. Widen only for multi-choice "could vote for" polls - they are a rough proxy.
    row_total: tuple[float, float] = (85.0, 110.0)

    def all_scenarios(self) -> list[NamedScenario]:
        if self.scenarios:
            return self.scenarios
        return [NamedScenario(name="", weight=1.0, require=self.scenario.require, exclude=self.scenario.exclude)]


class Candidacy(BaseModel):
    """A candidate who may not stand: with probability 1 - p their votes go to `substitute`."""

    probability: float = Field(1.0, ge=0.0, le=1.0)
    substitute: str | None = None
    transfer: float = Field(0.9, ge=0.0, le=1.0)  # share of the votes the substitute keeps; rest -> Others


class BookmakerConfig(BaseModel):
    """Where a bookmaker lists this election and how its names map to ours."""

    pages: list[str] = []  # fixed pages (Betano: event/league URLs, Polymarket: event slugs)
    # Find pages automatically: every keyword must appear in the bookmaker's competition name
    # (case/accent-insensitive), e.g. ["Francie", "prezident"]. Used in addition to `pages`.
    discover: list[str] = []
    # Bookmaker market name -> our market id, e.g. {"Vítěz": "winner"}. Unlisted markets are ignored.
    markets: dict[str, str]
    # Extra selection-name aliases, e.g. {"Pavel P.": PAVEL}; candidate names/aliases are used automatically.
    selections: dict[str, str] = {}


class ElectionResult(BaseModel):
    first_round: dict[str, float] = {}
    runoff: dict[str, float] = {}


class ElectionConfig(BaseModel):
    id: str
    name: str
    type: Literal["single_round", "two_round"] = "single_round"
    election_date: date
    # Where the probabilities come from: "polls" = the poll model, "market" = prices of a reference
    # market (e.g. Polymarket) when polls cannot be modelled (US primaries + electoral college, seats).
    probability_source: Literal["polls", "market"] = "polls"
    reference_market: str = "polymarket"  # bookmaker key whose prices act as the model in "market" mode
    wiki_page: str | None = None
    default_sample_size: int = 1000
    candidates: dict[str, Candidate]
    first_round: RoundConfig | None = None
    runoff: RoundConfig | None = None
    model: dict[str, Any] = {}
    # Candidates who may not stand (e.g. an ineligibility appeal), with a substitute who takes the votes.
    candidacy: dict[str, Candidacy] = {}
    # Polymarket event with "will X be on the ballot?" markets; its prices update candidacy probabilities.
    candidacy_market: str | None = None
    bookmakers: dict[str, BookmakerConfig] = {}
    result: ElectionResult | None = None

    @property
    def uses_polls(self) -> bool:
        return self.probability_source == "polls"

    @model_validator(mode="after")
    def _check(self) -> "ElectionConfig":
        if OTHER in self.candidates:
            raise ValueError(f"'{OTHER}' is a reserved id")
        if self.uses_polls and (not self.wiki_page or self.first_round is None):
            raise ValueError("probability_source 'polls' needs wiki_page and first_round")
        if not self.uses_polls and self.reference_market not in self.bookmakers:
            raise ValueError(f"reference_market '{self.reference_market}' must be one of the bookmakers")
        known = set(self.candidates)
        for rnd in filter(None, [self.first_round, self.runoff]):
            for sc in rnd.all_scenarios():
                unknown = (set(sc.require) | set(sc.exclude)) - known
                if unknown:
                    raise ValueError(f"scenario refers to unknown candidates: {sorted(unknown)}")
            names = [sc.name for sc in rnd.scenarios]
            if len(names) != len(set(names)) or any(not n or "|" in n or ":" in n for n in names):
                raise ValueError("scenario names must be unique, non-empty and without '|' or ':'")
        for cid, cand in self.candidacy.items():
            bad = {cid, cand.substitute} - {None} - known
            if bad:
                raise ValueError(f"candidacy refers to unknown candidates: {sorted(bad)}")
        for name, book in self.bookmakers.items():
            bad = set(book.selections.values()) - known
            if bad:
                raise ValueError(f"bookmakers.{name}.selections refers to unknown candidates: {sorted(bad)}")
        return self

    def candidate_name(self, cid: str) -> str:
        if cid == OTHER:
            return _("Others")
        return self.candidates[cid].name if cid in self.candidates else cid


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(_("File not found: {path}", path=path)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(_("Invalid YAML in {path}: {error}", path=path, error=exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError(_("File {path} must contain a YAML mapping", path=path))
    return data


def format_validation(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path("config/settings.yaml")
    data = read_yaml(path) if path.exists() else {}
    try:
        settings = Settings(**data)
    except ValidationError as exc:
        raise ConfigError(_("Invalid settings in {path}: {error}", path=path, error=format_validation(exc))) from exc
    settings.base_dir = path.parent.parent if path.parent.name == "config" else path.parent
    settings.uncalibrated_model = settings.model
    return settings


def apply_calibration(settings: Settings, overrides: dict[str, Any] | None, source: str) -> None:
    """Overlay calibrated model parameters (stored in the database) on settings.yaml values."""
    if not overrides:
        return
    base = settings.uncalibrated_model or settings.model
    try:
        settings.model = ModelParams(**{**base.model_dump(), **overrides})
    except ValidationError as exc:
        raise ConfigError(_("Invalid parameters in {path}: {error}", path=source, error=format_validation(exc))) from exc
    log.info("Using calibrated model parameters from %s", source)


def load_election(path: Path) -> ElectionConfig:
    try:
        return ElectionConfig(**read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(_("Invalid election config in {path}: {error}", path=path, error=format_validation(exc))) from exc


def discover_elections(settings: Settings, include_history: bool = False) -> dict[str, ElectionConfig]:
    root = settings.resolve(settings.elections_dir)
    pattern = "**/*.yaml" if include_history else "*.yaml"
    found: dict[str, ElectionConfig] = {}
    paths: dict[str, Path] = {}
    for path in sorted(root.glob(pattern)):
        cfg = load_election(path)
        if cfg.id in found:
            raise ConfigError(_("Duplicate election id '{id}' ({path} and {other})", id=cfg.id, path=path, other=paths[cfg.id]))
        found[cfg.id] = cfg
        paths[cfg.id] = path
    return found


def get_election(settings: Settings, election_id: str) -> ElectionConfig:
    elections = discover_elections(settings, include_history=True)
    if election_id not in elections:
        available = ", ".join(sorted(elections)) or _("none")
        raise ConfigError(_("Election '{id}' is not configured. Available: {available}", id=election_id, available=available))
    return elections[election_id]


def model_params_for(settings: Settings, election: ElectionConfig) -> ModelParams:
    """Global model params with per-election overrides applied."""
    if not election.model:
        return settings.model
    try:
        return ModelParams(**{**settings.model.model_dump(), **election.model})
    except ValidationError as exc:
        raise ConfigError(_("Invalid model parameters for election {id}: {error}", id=election.id, error=format_validation(exc))) from exc
