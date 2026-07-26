"""Typed configuration and repository path policy."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ecoloop import ENERGYPLUS_VERSION


def repository_root() -> Path:
    """Return the repository root without depending on the process directory."""

    return Path(__file__).resolve().parents[2]


def resolve_repo_path(value: Path | str, root: Path | None = None) -> Path:
    """Resolve a configured path relative to the repository root."""

    base = (root or repository_root()).resolve()
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def ensure_path_allowed(path: Path, extra_roots: tuple[Path, ...] = ()) -> Path:
    """Resolve a path and require it to be under the repository or allowed roots."""

    candidate = path.expanduser().resolve()
    allowed = (repository_root().resolve(), *(item.resolve() for item in extra_roots))
    if not any(candidate == root or candidate.is_relative_to(root) for root in allowed):
        raise ValueError(f"Path is outside configured roots: {candidate}")
    return candidate


class PeriodConfig(BaseModel):
    """Calendar bounds for an EnergyPlus run period."""

    start_month: int = Field(ge=1, le=12)
    start_day: int = Field(ge=1, le=31)
    end_month: int = Field(ge=1, le=12)
    end_day: int = Field(ge=1, le=31)


class LimitConfig(BaseModel):
    """Heating, cooling, and optional occupied comfort constraints."""

    heating_min_c: float
    heating_max_c: float
    cooling_min_c: float
    cooling_max_c: float
    minimum_deadband_c: float = Field(gt=0)
    maximum_change_c: float = Field(gt=0)
    operative_min_c: float | None = None
    operative_max_c: float | None = None
    absolute_pmv_max: float | None = None
    co2_max_ppm: float | None = None

    @field_validator("heating_max_c")
    @classmethod
    def validate_heating_bounds(cls, value: float, info: Any) -> float:
        """Require ordered heating limits."""

        minimum = info.data.get("heating_min_c")
        if minimum is not None and value < minimum:
            raise ValueError("heating_max_c must be at least heating_min_c")
        return value

    @field_validator("cooling_max_c")
    @classmethod
    def validate_cooling_bounds(cls, value: float, info: Any) -> float:
        """Require ordered cooling limits."""

        minimum = info.data.get("cooling_min_c")
        if minimum is not None and value < minimum:
            raise ValueError("cooling_max_c must be at least cooling_min_c")
        return value


class ControlConfig(BaseModel):
    """Cadence, failure, and setpoint constraints."""

    normal_decision_interval_minutes: int = Field(gt=0)
    maximum_action_hold_minutes: int = Field(gt=0, le=24 * 60)
    maximum_tool_rounds: int = Field(gt=0, le=32)
    maximum_consecutive_timeouts: int = Field(gt=0)
    state_token_budget: int = Field(ge=256)
    demand_threshold_kw: float = Field(gt=0)
    occupied: LimitConfig
    unoccupied: LimitConfig


class EvaluationConfig(BaseModel):
    """Tariff, carbon, and cross-check settings."""

    tariff_per_kwh: float = Field(ge=0)
    carbon_kg_per_kwh: float = Field(ge=0)
    telemetry_energy_tolerance_percent: float = Field(gt=0)


class FileConfig(BaseModel):
    """Configuration loaded from `config/default.toml`."""

    periods: dict[str, PeriodConfig]
    control: ControlConfig
    evaluation: EvaluationConfig
    dashboard: dict[str, float]


class Settings(BaseSettings):
    """Environment-specific runtime settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    energyplus_home: Path | None = Field(default=None, alias="ENERGYPLUS_HOME")
    model_path: Path = Field(default=Path("models/base/building.idf"), alias="ECOLOOP_MODEL_PATH")
    weather_path: Path = Field(default=Path("weather/default.epw"), alias="ECOLOOP_WEATHER_PATH")
    database_path: Path = Field(default=Path("runs/ecoloop.db"), alias="ECOLOOP_DATABASE_PATH")
    runs_dir: Path = Field(default=Path("runs"), alias="ECOLOOP_RUNS_DIR")
    submission_dir: Path = Field(default=Path("submission"), alias="ECOLOOP_SUBMISSION_DIR")
    ollama_host: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_HOST")
    ollama_model: str = Field(default="qwen3:8b", alias="OLLAMA_MODEL")
    ollama_keep_alive: str = Field(default="30m", alias="OLLAMA_KEEP_ALIVE")
    llm_timeout_seconds: float = Field(default=30.0, gt=0, alias="LLM_TIMEOUT_SECONDS")
    simulation_timeout_seconds: float = Field(
        default=7200.0,
        gt=0,
        alias="ECOLOOP_SIMULATION_TIMEOUT_SECONDS",
    )
    max_tool_rounds: int = Field(default=8, gt=0, alias="ECOLOOP_MAX_TOOL_ROUNDS")
    max_consecutive_timeouts: int = Field(default=3, gt=0, alias="ECOLOOP_MAX_CONSECUTIVE_TIMEOUTS")
    state_token_budget: int = Field(default=1800, ge=256, alias="ECOLOOP_STATE_TOKEN_BUDGET")
    zone_timestep_minutes: int = Field(
        default=15, gt=0, le=60, alias="ECOLOOP_ZONE_TIMESTEP_MINUTES"
    )
    decision_interval_minutes: int = Field(
        default=60, gt=0, alias="ECOLOOP_DECISION_INTERVAL_MINUTES"
    )
    max_action_hold_minutes: int = Field(default=120, gt=0, alias="ECOLOOP_MAX_ACTION_HOLD_MINUTES")
    tariff_per_kwh: float = Field(default=0.12, ge=0, alias="ECOLOOP_TARIFF_PER_KWH")
    carbon_kg_per_kwh: float = Field(default=0.70, ge=0, alias="ECOLOOP_CARBON_KG_PER_KWH")
    demand_threshold_kw: float = Field(default=75.0, gt=0, alias="ECOLOOP_DEMAND_THRESHOLD_KW")
    demo_display_delay_seconds: float = Field(
        default=0.0, ge=0, le=10, alias="ECOLOOP_DEMO_DISPLAY_DELAY_SECONDS"
    )

    @property
    def expected_energyplus_version(self) -> str:
        """Return the only supported EnergyPlus version."""

        return ENERGYPLUS_VERSION

    def resolved_model_path(self) -> Path:
        """Return the absolute configured model path."""

        return resolve_repo_path(self.model_path)

    def resolved_weather_path(self) -> Path:
        """Return the absolute configured weather path."""

        return resolve_repo_path(self.weather_path)

    def resolved_database_path(self) -> Path:
        """Return the absolute database path."""

        return resolve_repo_path(self.database_path)

    def resolved_runs_dir(self) -> Path:
        """Return the absolute run root."""

        return resolve_repo_path(self.runs_dir)

    def resolved_submission_dir(self) -> Path:
        """Return the absolute submission root."""

        return resolve_repo_path(self.submission_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache environment settings."""

    return Settings()


@lru_cache(maxsize=1)
def load_file_config(path: Path | None = None) -> FileConfig:
    """Load and validate the version-controlled TOML configuration."""

    config_path = path or repository_root() / "config" / "default.toml"
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)
    return FileConfig.model_validate(data)


def sanitized_environment() -> dict[str, str]:
    """Return the small environment subset permitted for MCP child processes."""

    settings = get_settings()
    python_path = os.environ.get("PYTHONPATH")
    values = {
        "ECOLOOP_DATABASE_PATH": str(settings.resolved_database_path()),
        "ECOLOOP_RUNS_DIR": str(settings.resolved_runs_dir()),
        "ECOLOOP_MODEL_PATH": str(settings.resolved_model_path()),
        "ECOLOOP_WEATHER_PATH": str(settings.resolved_weather_path()),
        "OLLAMA_HOST": settings.ollama_host,
        "OLLAMA_MODEL": settings.ollama_model,
    }
    if python_path:
        values["PYTHONPATH"] = python_path
    return values
