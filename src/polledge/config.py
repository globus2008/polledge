"""Configuration models and loaders (settings.yaml, per-election YAML, calibrated overrides)."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from polledge.errors import ConfigError

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
    seed: int | None = None
    min_share_pct: float = 0.1


class SignalSettings(BaseModel):
    edge_threshold: float = 0.05
    kelly_fraction: float = 0.25
    bankroll: float = 10_000.0
    max_stake_pct: float | None = 0.05
    currency: str = "Kč"


class Settings(BaseModel):
    database: Path = Path("data/polledge.db")
    cache_dir: Path = Path("data/cache")
    log_dir: Path = Path("logs")
    elections_dir: Path = Path("config/elections")
    calibrated_file: Path = Path("config/calibrated.yaml")
    wikipedia: WikipediaSettings = WikipediaSettings()
    model: ModelParams = ModelParams()
    signals: SignalSettings = SignalSettings()

    base_dir: Path = Path(".")
    # Model params from settings.yaml before calibrated.yaml overrides (baseline for calibration).
    uncalibrated_model: ModelParams | None = None

    def resolve(self, path: Path) -> Path:
        return path if path.is_absolute() else self.base_dir / path


class Candidate(BaseModel):
    name: str
    aliases: list[str] = []

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
    sample_size: str = "Sample size"
    # Non-candidate columns to drop (matched by prefix, case/accent-insensitive).
    ignore: list[str] = [
        "Lead", "Abs", "Undecided", "Margin", "Ref", "Spread", "Turnout", "No vote", "Don't know",
        "Govt", "Opp",
    ]


class Scenario(BaseModel):
    """Which rows (hypothetical candidate line-ups) to keep."""

    require: list[str] = []
    exclude: list[str] = []


class RoundConfig(BaseModel):
    tables: list[TableSelector]
    columns: ColumnNames = ColumnNames()
    scenario: Scenario = Scenario()
    # Rows whose pollster cell contains one of these (e.g. the official result row) are skipped.
    skip_rows: list[str] = ["election", "result", "exit poll"]


class ElectionResult(BaseModel):
    first_round: dict[str, float] = {}
    runoff: dict[str, float] = {}


class ElectionConfig(BaseModel):
    id: str
    name: str
    type: Literal["single_round", "two_round"] = "single_round"
    election_date: date
    wiki_page: str
    default_sample_size: int = 1000
    candidates: dict[str, Candidate]
    first_round: RoundConfig
    runoff: RoundConfig | None = None
    model: dict[str, Any] = {}
    result: ElectionResult | None = None

    @model_validator(mode="after")
    def _check(self) -> "ElectionConfig":
        if OTHER in self.candidates:
            raise ValueError(f"'{OTHER}' je rezervované ID")
        known = set(self.candidates)
        for rnd in filter(None, [self.first_round, self.runoff]):
            unknown = (set(rnd.scenario.require) | set(rnd.scenario.exclude)) - known
            if unknown:
                raise ValueError(f"scénář odkazuje na neznámé kandidáty: {sorted(unknown)}")
        return self

    def candidate_name(self, cid: str) -> str:
        if cid == OTHER:
            return "Ostatní"
        return self.candidates[cid].name if cid in self.candidates else cid


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Soubor nenalezen: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Chybný YAML v souboru {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Soubor {path} musí obsahovat YAML slovník")
    return data


def format_validation(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path("config/settings.yaml")
    data = read_yaml(path) if path.exists() else {}
    try:
        settings = Settings(**data)
    except ValidationError as exc:
        raise ConfigError(f"Neplatné nastavení v {path}: {format_validation(exc)}") from exc
    settings.base_dir = path.parent.parent if path.parent.name == "config" else path.parent
    settings.uncalibrated_model = settings.model
    calibrated = settings.resolve(settings.calibrated_file)
    if calibrated.exists():
        overrides = read_yaml(calibrated).get("model", {})
        if overrides:
            log.info("Using calibrated model parameters from %s", calibrated)
            try:
                settings.model = ModelParams(**{**settings.model.model_dump(), **overrides})
            except ValidationError as exc:
                raise ConfigError(f"Neplatné parametry v {calibrated}: {format_validation(exc)}") from exc
    return settings


def load_election(path: Path) -> ElectionConfig:
    try:
        return ElectionConfig(**read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(f"Neplatná konfigurace voleb v {path}: {format_validation(exc)}") from exc


def discover_elections(settings: Settings, include_history: bool = False) -> dict[str, ElectionConfig]:
    root = settings.resolve(settings.elections_dir)
    pattern = "**/*.yaml" if include_history else "*.yaml"
    found: dict[str, ElectionConfig] = {}
    paths: dict[str, Path] = {}
    for path in sorted(root.glob(pattern)):
        cfg = load_election(path)
        if cfg.id in found:
            raise ConfigError(f"Duplicitní ID voleb '{cfg.id}' ({path} a {paths[cfg.id]})")
        found[cfg.id] = cfg
        paths[cfg.id] = path
    return found


def get_election(settings: Settings, election_id: str) -> ElectionConfig:
    elections = discover_elections(settings, include_history=True)
    if election_id not in elections:
        available = ", ".join(sorted(elections)) or "žádné"
        raise ConfigError(f"Volby '{election_id}' nejsou nakonfigurované. Dostupné: {available}")
    return elections[election_id]


def model_params_for(settings: Settings, election: ElectionConfig) -> ModelParams:
    """Global model params with per-election overrides applied."""
    if not election.model:
        return settings.model
    try:
        return ModelParams(**{**settings.model.model_dump(), **election.model})
    except ValidationError as exc:
        raise ConfigError(f"Neplatné parametry modelu u voleb {election.id}: {format_validation(exc)}") from exc
