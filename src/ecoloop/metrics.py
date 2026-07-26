"""Transparent metric formulas for verified EnergyPlus run outputs."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean

from ecoloop.schemas import (
    ComparisonMetrics,
    EnergyCrossCheck,
    FinalRunMetrics,
    RunType,
)

JOULES_PER_KWH = 3_600_000.0
WATTS_PER_KW = 1_000.0


def joules_to_kwh(joules: float) -> float:
    """Convert a finite energy value in joules to kWh."""

    _require_finite(joules, "joules")
    return joules / JOULES_PER_KWH


def watts_to_kw(watts: float) -> float:
    """Convert a finite demand value in watts to kW."""

    _require_finite(watts, "watts")
    return watts / WATTS_PER_KW


def electricity_saving_percent(baseline_kwh: float, controlled_kwh: float) -> float:
    """Return signed electricity savings relative to a positive baseline."""

    return _reduction_percent(baseline_kwh, controlled_kwh, "baseline_kwh")


def peak_reduction_percent(baseline_kw: float, controlled_kw: float) -> float:
    """Return signed peak-demand reduction relative to a positive baseline."""

    return _reduction_percent(baseline_kw, controlled_kw, "baseline_kw")


def energy_cost(energy_kwh: float, tariff_per_kwh: float) -> float:
    """Calculate energy cost for a non-negative energy and tariff."""

    _require_non_negative(energy_kwh, "energy_kwh")
    _require_non_negative(tariff_per_kwh, "tariff_per_kwh")
    return energy_kwh * tariff_per_kwh


def operational_carbon(energy_kwh: float, carbon_kg_per_kwh: float) -> float:
    """Calculate operational carbon for a non-negative grid factor."""

    _require_non_negative(energy_kwh, "energy_kwh")
    _require_non_negative(carbon_kg_per_kwh, "carbon_kg_per_kwh")
    return energy_kwh * carbon_kg_per_kwh


def energy_cross_check(
    official_kwh: float,
    telemetry_kwh: float,
    tolerance_percent: float,
) -> EnergyCrossCheck:
    """Cross-check API telemetry against the official EnergyPlus output total."""

    _require_non_negative(official_kwh, "official_kwh")
    _require_non_negative(telemetry_kwh, "telemetry_kwh")
    if not math.isfinite(tolerance_percent) or tolerance_percent <= 0:
        raise ValueError("tolerance_percent must be finite and positive")
    difference = abs(official_kwh - telemetry_kwh)
    if official_kwh == 0:
        difference_percent = 0.0 if telemetry_kwh == 0 else math.inf
    else:
        difference_percent = difference / official_kwh * 100.0
    if not math.isfinite(difference_percent):
        raise ValueError(
            "cannot express telemetry difference percent when official energy is zero "
            "and telemetry is non-zero"
        )
    return EnergyCrossCheck(
        official_kwh=official_kwh,
        telemetry_kwh=telemetry_kwh,
        absolute_difference_kwh=difference,
        difference_percent=difference_percent,
        tolerance_percent=tolerance_percent,
        passed=difference_percent <= tolerance_percent,
    )


@dataclass(frozen=True, slots=True)
class ComfortSample:
    """One duration-weighted comfort sample from real or explicit test telemetry."""

    occupied: bool
    operative_temperature_c: float
    duration_hours: float
    pmv: float | None = None
    ppd_percent: float | None = None
    co2_ppm: float | None = None

    def __post_init__(self) -> None:
        _require_finite(self.operative_temperature_c, "operative_temperature_c")
        if not math.isfinite(self.duration_hours) or self.duration_hours <= 0:
            raise ValueError("duration_hours must be finite and positive")
        for value, name in (
            (self.pmv, "pmv"),
            (self.ppd_percent, "ppd_percent"),
            (self.co2_ppm, "co2_ppm"),
        ):
            if value is not None:
                _require_finite(value, name)
        if self.ppd_percent is not None and not 0 <= self.ppd_percent <= 100:
            raise ValueError("ppd_percent must be in 0..100")
        if self.co2_ppm is not None and self.co2_ppm < 0:
            raise ValueError("co2_ppm must be non-negative")


@dataclass(frozen=True, slots=True)
class ComfortMetrics:
    """Duration-weighted occupied comfort and IAQ metrics."""

    occupied_hours: float
    temperature_violation_percent: float
    temperature_violation_degree_hours: float
    pmv_compliance_percent: float | None
    mean_ppd_percent: float | None
    maximum_occupied_co2_ppm: float | None


def calculate_comfort_metrics(
    samples: Iterable[ComfortSample],
    *,
    operative_min_c: float = 22.0,
    operative_max_c: float = 26.0,
    absolute_pmv_max: float = 0.7,
) -> ComfortMetrics:
    """Calculate occupied comfort metrics without filling missing PMV/IAQ data."""

    if operative_min_c >= operative_max_c:
        raise ValueError("operative_min_c must be below operative_max_c")
    if absolute_pmv_max <= 0 or not math.isfinite(absolute_pmv_max):
        raise ValueError("absolute_pmv_max must be finite and positive")
    occupied = [sample for sample in samples if sample.occupied]
    occupied_hours = sum(sample.duration_hours for sample in occupied)
    if occupied_hours <= 0:
        raise ValueError("comfort metrics require at least one occupied sample")

    violation_hours = 0.0
    degree_hours = 0.0
    pmv_hours = 0.0
    pmv_compliant_hours = 0.0
    ppd_weighted_sum = 0.0
    ppd_hours = 0.0
    co2_values: list[float] = []
    for sample in occupied:
        below = max(0.0, operative_min_c - sample.operative_temperature_c)
        above = max(0.0, sample.operative_temperature_c - operative_max_c)
        violation = below + above
        if violation > 0:
            violation_hours += sample.duration_hours
            degree_hours += violation * sample.duration_hours
        if sample.pmv is not None:
            pmv_hours += sample.duration_hours
            if abs(sample.pmv) <= absolute_pmv_max:
                pmv_compliant_hours += sample.duration_hours
        if sample.ppd_percent is not None:
            ppd_weighted_sum += sample.ppd_percent * sample.duration_hours
            ppd_hours += sample.duration_hours
        if sample.co2_ppm is not None:
            co2_values.append(sample.co2_ppm)

    return ComfortMetrics(
        occupied_hours=occupied_hours,
        temperature_violation_percent=violation_hours / occupied_hours * 100.0,
        temperature_violation_degree_hours=degree_hours,
        pmv_compliance_percent=(pmv_compliant_hours / pmv_hours * 100.0 if pmv_hours else None),
        mean_ppd_percent=ppd_weighted_sum / ppd_hours if ppd_hours else None,
        maximum_occupied_co2_ppm=max(co2_values) if co2_values else None,
    )


@dataclass(frozen=True, slots=True)
class LatencyMetrics:
    """Decision latency summary."""

    count: int
    average_ms: float | None
    p95_ms: float | None


def calculate_latency_metrics(latencies_ms: Iterable[float]) -> LatencyMetrics:
    """Return mean and linearly interpolated p95 latency."""

    values = list(latencies_ms)
    for value in values:
        _require_non_negative(value, "latency_ms")
    if not values:
        return LatencyMetrics(count=0, average_ms=None, p95_ms=None)
    ordered = sorted(values)
    return LatencyMetrics(
        count=len(ordered),
        average_ms=fmean(ordered),
        p95_ms=_percentile(ordered, 0.95),
    )


def compare_completed_runs(
    baseline: FinalRunMetrics,
    controlled: FinalRunMetrics,
    *,
    timestamp: datetime,
    allow_fake: bool = False,
) -> ComparisonMetrics:
    """Compare compatible complete runs while protecting production claims."""

    if baseline.run_type is not RunType.BASELINE:
        raise ValueError("baseline metrics must come from a baseline run")
    if controlled.run_type not in {
        RunType.AGENT,
        RunType.RULE,
        RunType.REPLAY,
        RunType.FIXED_OVERRIDE,
    }:
        raise ValueError("controlled metrics must come from a controlled run type")
    if baseline.is_fake or controlled.is_fake:
        if not allow_fake:
            raise ValueError("fake runs cannot produce a production comparison")
        if baseline.is_fake != controlled.is_fake:
            raise ValueError("cannot compare a fake run with a real run")
    if (
        baseline.simulation_start != controlled.simulation_start
        or baseline.simulation_end != controlled.simulation_end
    ):
        raise ValueError("run periods are not aligned")
    if not baseline.energy_cross_check.passed or not controlled.energy_cross_check.passed:
        raise ValueError("both runs must pass the official-output energy cross-check")

    baseline_fuels = set(baseline.other_fuels_kwh)
    controlled_fuels = set(controlled.other_fuels_kwh)
    if baseline_fuels != controlled_fuels:
        raise ValueError(
            "other-fuel outputs do not contain the same fuel names; "
            "missing fuel data cannot be treated as zero"
        )
    other_fuels = {
        name: (
            float(baseline.other_fuels_kwh[name]),
            float(controlled.other_fuels_kwh[name]),
        )
        for name in sorted(baseline_fuels)
    }
    return ComparisonMetrics(
        baseline_run_id=baseline.run_id,
        agent_run_id=controlled.run_id,
        timestamp=timestamp,
        electricity_saving_percent=electricity_saving_percent(
            baseline.facility_electricity_kwh,
            controlled.facility_electricity_kwh,
        ),
        peak_reduction_percent=peak_reduction_percent(
            baseline.peak_electrical_demand_kw,
            controlled.peak_electrical_demand_kw,
        ),
        cost_saving=baseline.cost - controlled.cost,
        cost_saving_percent=_reduction_percent(baseline.cost, controlled.cost, "baseline cost"),
        carbon_saving_kg=(baseline.operational_carbon_kg - controlled.operational_carbon_kg),
        carbon_saving_percent=_reduction_percent(
            baseline.operational_carbon_kg,
            controlled.operational_carbon_kg,
            "baseline operational carbon",
        ),
        baseline_facility_electricity_kwh=baseline.facility_electricity_kwh,
        agent_facility_electricity_kwh=controlled.facility_electricity_kwh,
        baseline_peak_demand_kw=baseline.peak_electrical_demand_kw,
        agent_peak_demand_kw=controlled.peak_electrical_demand_kw,
        baseline_hvac_electricity_kwh=baseline.hvac_electricity_kwh,
        agent_hvac_electricity_kwh=controlled.hvac_electricity_kwh,
        baseline_comfort_compliance_percent=(
            100.0 - baseline.occupied_temperature_violation_percent
        ),
        agent_comfort_compliance_percent=(
            100.0 - controlled.occupied_temperature_violation_percent
        ),
        other_fuels=other_fuels,
    )


def sum_energy_by_fuel(
    rows: Iterable[Mapping[str, float]],
) -> dict[str, float]:
    """Sum independently named fuel values without merging fuels."""

    totals: dict[str, float] = {}
    for row in rows:
        for fuel, value in row.items():
            _require_non_negative(value, f"{fuel} energy")
            totals[fuel] = totals.get(fuel, 0.0) + value
    return totals


def _reduction_percent(baseline: float, controlled: float, label: str) -> float:
    _require_finite(baseline, label)
    _require_finite(controlled, "controlled value")
    if baseline <= 0:
        raise ValueError(f"{label} must be positive to calculate a percentage")
    if controlled < 0:
        raise ValueError("controlled value must be non-negative")
    return (baseline - controlled) / baseline * 100.0


def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= percentile <= 1:
        raise ValueError("percentile must be in 0..1")
    position = (len(sorted_values) - 1) * percentile
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return sorted_values[lower_index]
    fraction = position - lower_index
    return sorted_values[lower_index] * (1.0 - fraction) + sorted_values[upper_index] * fraction


def _require_finite(value: float, name: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")


def _require_non_negative(value: float, name: str) -> None:
    _require_finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
