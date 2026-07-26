"""Verified energy, comfort, reliability, and comparison formula tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ecoloop.metrics import (
    ComfortSample,
    calculate_comfort_metrics,
    calculate_latency_metrics,
    compare_completed_runs,
    electricity_saving_percent,
    energy_cost,
    energy_cross_check,
    joules_to_kwh,
    operational_carbon,
    peak_reduction_percent,
    sum_energy_by_fuel,
    watts_to_kw,
)
from ecoloop.schemas import EnergyCrossCheck, FinalRunMetrics, RunStatus, RunType
from tests.unit._factories import NOW


def _run_metrics(
    *,
    run_id: str,
    run_type: RunType,
    electricity_kwh: float,
    peak_kw: float,
    cost: float,
    carbon: float,
    comfort_violation_percent: float,
    is_fake: bool = False,
    simulation_end=NOW + timedelta(days=1),
) -> FinalRunMetrics:
    return FinalRunMetrics(
        run_id=run_id,
        timestamp=NOW,
        run_type=run_type,
        status=RunStatus.COMPLETED,
        is_fake=is_fake,
        simulation_start=NOW,
        simulation_end=simulation_end,
        facility_electricity_kwh=electricity_kwh,
        hvac_electricity_kwh=electricity_kwh * 0.6,
        other_fuels_kwh={"NaturalGas": 10.0},
        peak_electrical_demand_kw=peak_kw,
        cost=cost,
        operational_carbon_kg=carbon,
        occupied_temperature_violation_percent=comfort_violation_percent,
        occupied_temperature_violation_degree_hours=0.5,
        pmv_compliance_percent=95.0,
        mean_ppd_percent=8.0,
        maximum_occupied_co2_ppm=900.0,
        llm_decision_count=4 if run_type is RunType.AGENT else 0,
        tool_call_count=16 if run_type is RunType.AGENT else 0,
        average_decision_latency_ms=100.0 if run_type is RunType.AGENT else None,
        p95_decision_latency_ms=150.0 if run_type is RunType.AGENT else None,
        timeout_count=0,
        fallback_count=0,
        invalid_action_count=0,
        safety_clamp_count=1,
        warning_count=2,
        severe_count=0,
        fatal_count=0,
        energy_cross_check=EnergyCrossCheck(
            official_kwh=electricity_kwh,
            telemetry_kwh=electricity_kwh * 1.001,
            absolute_difference_kwh=electricity_kwh * 0.001,
            difference_percent=0.1,
            tolerance_percent=2.0,
            passed=True,
        ),
        source_artifacts=("eplusout.csv", "eplusout.sql"),
    )


def test_unit_conversions_and_simple_formulas() -> None:
    assert joules_to_kwh(3_600_000.0) == 1.0
    assert watts_to_kw(75_000.0) == 75.0
    assert electricity_saving_percent(100.0, 90.0) == pytest.approx(10.0)
    assert peak_reduction_percent(50.0, 45.0) == pytest.approx(10.0)
    assert energy_cost(100.0, 0.12) == pytest.approx(12.0)
    assert operational_carbon(100.0, 0.7) == pytest.approx(70.0)


@pytest.mark.parametrize(
    ("baseline", "controlled"),
    [(0.0, 0.0), (-1.0, 1.0), (100.0, -1.0)],
)
def test_savings_reject_non_physical_denominators(baseline: float, controlled: float) -> None:
    with pytest.raises(ValueError):
        electricity_saving_percent(baseline, controlled)


def test_energy_cross_check_uses_official_total_as_denominator() -> None:
    check = energy_cross_check(100.0, 101.5, 2.0)
    assert check.absolute_difference_kwh == pytest.approx(1.5)
    assert check.difference_percent == pytest.approx(1.5)
    assert check.passed
    assert not energy_cross_check(100.0, 103.0, 2.0).passed
    with pytest.raises(ValueError, match="official energy is zero"):
        energy_cross_check(0.0, 1.0, 2.0)


def test_comfort_metrics_are_duration_weighted_and_ignore_unoccupied() -> None:
    result = calculate_comfort_metrics(
        [
            ComfortSample(True, 21.0, 1.0, pmv=0.2, ppd_percent=10.0, co2_ppm=600.0),
            ComfortSample(True, 24.0, 1.0, pmv=0.8, ppd_percent=20.0, co2_ppm=800.0),
            ComfortSample(True, 27.0, 2.0, pmv=0.5, ppd_percent=30.0, co2_ppm=1200.0),
            ComfortSample(False, 10.0, 10.0, pmv=3.0, ppd_percent=100.0, co2_ppm=5000.0),
        ]
    )
    assert result.occupied_hours == 4.0
    assert result.temperature_violation_percent == pytest.approx(75.0)
    assert result.temperature_violation_degree_hours == pytest.approx(3.0)
    assert result.pmv_compliance_percent == pytest.approx(75.0)
    assert result.mean_ppd_percent == pytest.approx(22.5)
    assert result.maximum_occupied_co2_ppm == 1200.0


def test_comfort_metrics_do_not_invent_missing_pmv_or_iaq() -> None:
    result = calculate_comfort_metrics([ComfortSample(True, 24.0, 0.25)])
    assert result.pmv_compliance_percent is None
    assert result.mean_ppd_percent is None
    assert result.maximum_occupied_co2_ppm is None
    with pytest.raises(ValueError, match="occupied"):
        calculate_comfort_metrics([ComfortSample(False, 24.0, 1.0)])


def test_latency_metrics_are_deterministic_for_small_samples() -> None:
    empty = calculate_latency_metrics([])
    assert empty.average_ms is None and empty.p95_ms is None
    result = calculate_latency_metrics([0.0, 100.0])
    assert result.count == 2
    assert result.average_ms == 50.0
    assert result.p95_ms == pytest.approx(95.0)


def test_completed_run_comparison_preserves_other_fuels() -> None:
    baseline = _run_metrics(
        run_id="baseline",
        run_type=RunType.BASELINE,
        electricity_kwh=100.0,
        peak_kw=50.0,
        cost=12.0,
        carbon=70.0,
        comfort_violation_percent=5.0,
    )
    agent = _run_metrics(
        run_id="agent",
        run_type=RunType.AGENT,
        electricity_kwh=90.0,
        peak_kw=45.0,
        cost=10.8,
        carbon=63.0,
        comfort_violation_percent=4.0,
    )
    comparison = compare_completed_runs(baseline, agent, timestamp=NOW)
    assert comparison.electricity_saving_percent == pytest.approx(10.0)
    assert comparison.peak_reduction_percent == pytest.approx(10.0)
    assert comparison.cost_saving_percent == pytest.approx(10.0)
    assert comparison.carbon_saving_percent == pytest.approx(10.0)
    assert comparison.baseline_comfort_compliance_percent == 95.0
    assert comparison.agent_comfort_compliance_percent == 96.0
    assert comparison.other_fuels == {"NaturalGas": (10.0, 10.0)}


def test_comparison_rejects_fake_or_misaligned_runs() -> None:
    baseline = _run_metrics(
        run_id="baseline",
        run_type=RunType.BASELINE,
        electricity_kwh=100.0,
        peak_kw=50.0,
        cost=12.0,
        carbon=70.0,
        comfort_violation_percent=5.0,
        is_fake=True,
    )
    agent = _run_metrics(
        run_id="agent",
        run_type=RunType.AGENT,
        electricity_kwh=90.0,
        peak_kw=45.0,
        cost=10.8,
        carbon=63.0,
        comfort_violation_percent=4.0,
        is_fake=True,
    )
    with pytest.raises(ValueError, match="fake"):
        compare_completed_runs(baseline, agent, timestamp=NOW)
    real_baseline = baseline.model_copy(update={"is_fake": False})
    misaligned = agent.model_copy(
        update={
            "is_fake": False,
            "simulation_end": NOW + timedelta(days=2),
        }
    )
    with pytest.raises(ValueError, match="not aligned"):
        compare_completed_runs(real_baseline, misaligned, timestamp=NOW)


def test_comparison_does_not_treat_missing_fuel_as_zero() -> None:
    baseline = _run_metrics(
        run_id="baseline",
        run_type=RunType.BASELINE,
        electricity_kwh=100.0,
        peak_kw=50.0,
        cost=12.0,
        carbon=70.0,
        comfort_violation_percent=5.0,
    )
    agent = _run_metrics(
        run_id="agent",
        run_type=RunType.AGENT,
        electricity_kwh=90.0,
        peak_kw=45.0,
        cost=10.8,
        carbon=63.0,
        comfort_violation_percent=4.0,
    ).model_copy(update={"other_fuels_kwh": {}})
    with pytest.raises(ValueError, match="missing fuel"):
        compare_completed_runs(baseline, agent, timestamp=NOW)


def test_fuels_are_summed_independently() -> None:
    result = sum_energy_by_fuel(
        [
            {"Electricity": 10.0, "NaturalGas": 3.0},
            {"Electricity": 5.0, "DistrictHeating": 2.0},
        ]
    )
    assert result == {
        "Electricity": 15.0,
        "NaturalGas": 3.0,
        "DistrictHeating": 2.0,
    }
