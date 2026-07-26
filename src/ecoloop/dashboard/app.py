"""Streamlit dashboard reading only the durable run database."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ecoloop.config import get_settings, repository_root
from ecoloop.dashboard import queries
from ecoloop.dashboard.queries import DashboardDataError


def main() -> None:
    """Render the six-tab EcoLoop operational dashboard."""

    st.set_page_config(
        page_title="EcoLoop Building Agents",
        page_icon="🌿",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _apply_style()
    settings = get_settings()
    database_path = settings.resolved_database_path()
    replay_run_id = os.environ.get("ECOLOOP_DEMO_REPLAY_RUN_ID")
    replay_enabled = os.environ.get("ECOLOOP_DEMO_REPLAY", "").casefold() in {
        "1",
        "true",
        "yes",
    }

    st.title("EcoLoop Building Agents")
    if replay_enabled:
        st.warning(
            f"REAL RUN REPLAY - source run `{replay_run_id or 'not selected'}`. "
            "Display speed changes wall-clock playback only."
        )
    else:
        st.caption("Live EnergyPlus supervisory control and durable decision evidence")

    try:
        runs = queries.list_runs(database_path, include_fake=False)
    except DashboardDataError as exc:
        st.info(str(exc))
        st.code("python -m ecoloop doctor\npython -m ecoloop run baseline --period smoke")
        return
    except Exception as exc:  # dashboard process boundary
        st.error(f"Could not query the run database: {exc}")
        return

    if runs.empty:
        st.info("No real runs exist. Fake test runs are intentionally hidden.")
        return

    run_id = _select_run(runs, replay_run_id if replay_enabled else None)
    run = queries.get_run(database_path, run_id)
    if run is None:
        st.error(f"Run disappeared: {run_id}")
        return

    frame_limit = _replay_controls(database_path, run_id) if replay_enabled else None

    tabs = st.tabs(
        [
            "Live Operations",
            "Baseline vs Agent",
            "Comfort and IAQ",
            "Agent Decisions",
            "Reliability and Errors",
            "Methodology",
        ]
    )
    with tabs[0]:
        _live_operations(database_path, run, frame_limit)
    with tabs[1]:
        _comparison(database_path, runs)
    with tabs[2]:
        _comfort(database_path, run_id, frame_limit)
    with tabs[3]:
        _decisions(database_path, run_id)
    with tabs[4]:
        _reliability(database_path, run_id)
    with tabs[5]:
        _methodology()

    if replay_enabled and st.session_state.get("ecoloop_replay_autoplay"):
        speed = float(st.session_state.get("ecoloop_replay_speed", 1.0))
        time.sleep(max(0.05, 1.0 / speed))
        st.session_state["ecoloop_replay_cursor"] = (
            int(st.session_state.get("ecoloop_replay_cursor", 1)) + 1
        )
        st.rerun()


def _select_run(runs: pd.DataFrame, forced: str | None) -> str:
    choices = [str(item) for item in runs["run_id"].tolist()]
    if forced:
        if forced not in choices:
            st.error(f"Replay run is not a real run in the database: {forced}")
            st.stop()
        return forced
    labels = {
        str(row.run_id): (
            f"{row.run_type} | {row.status} | {row.period_name or 'period?'} | {row.run_id}"
        )
        for row in runs.itertuples()
    }
    return st.sidebar.selectbox(
        "Current real run",
        choices,
        format_func=lambda value: labels[value],
    )


def _replay_controls(database_path: Path, run_id: str) -> int | None:
    total = len(queries.telemetry(database_path, run_id))
    if total == 0:
        return None
    st.sidebar.subheader("Replay controls")
    speed = st.sidebar.slider("Display speed", 0.25, 8.0, 1.0, 0.25)
    autoplay = st.sidebar.toggle("Autoplay", value=False)
    cursor_default = min(max(1, int(st.session_state.get("ecoloop_replay_cursor", 1))), total)
    cursor = st.sidebar.slider("Frame", 1, total, cursor_default)
    st.session_state["ecoloop_replay_speed"] = speed
    st.session_state["ecoloop_replay_autoplay"] = autoplay
    st.session_state["ecoloop_replay_cursor"] = min(cursor, total)
    return min(cursor, total)


def _live_operations(database_path: Path, run: dict[str, Any], limit: int | None) -> None:
    telemetry = queries.telemetry(database_path, str(run["run_id"]), limit=limit)
    zones = queries.zone_telemetry(database_path, str(run["run_id"]), limit=None)
    if limit is not None and not telemetry.empty:
        cutoff = telemetry["simulation_timestamp"].max()
        zones = zones[zones["simulation_timestamp"] <= cutoff]
    latest = telemetry.tail(1)
    latest_zones = (
        zones[zones["simulation_timestamp"] == zones["simulation_timestamp"].max()]
        if not zones.empty
        else zones
    )
    actions = queries.recent_actions(database_path, str(run["run_id"]), limit=5)
    tools = queries.recent_tool_calls(database_path, str(run["run_id"]), limit=8)

    simulation_clock = "Waiting for telemetry"
    demand = None
    heat_sp = None
    cool_sp = None
    if not latest.empty:
        row = latest.iloc[0]
        simulation_clock = str(row["simulation_timestamp"])
        demand = row.get("facility_demand_kw")
        heat_sp = row.get("heating_setpoint_c")
        cool_sp = row.get("cooling_setpoint_c")
    occupancy = latest_zones["occupant_count"].sum() if not latest_zones.empty else None
    temp = latest_zones["operative_temperature_c"].mean() if not latest_zones.empty else None
    pmv = latest_zones["pmv"].mean() if not latest_zones.empty else None
    co2 = latest_zones["co2_ppm"].max() if not latest_zones.empty else None

    columns = st.columns(6)
    columns[0].metric("Simulation clock", simulation_clock)
    columns[1].metric("Progress", _format_value(run.get("progress_percent"), "%"))
    columns[2].metric("Operative temp", _format_value(temp, " C"))
    columns[3].metric("Occupancy", _format_value(occupancy, " people"))
    columns[4].metric("Demand", _format_value(demand, " kW"))
    columns[5].metric("PMV / CO2", _pmv_co2(pmv, co2))

    status_cols = st.columns(3)
    status_cols[0].metric("Heating setpoint", _format_value(heat_sp, " C"))
    status_cols[1].metric("Cooling setpoint", _format_value(cool_sp, " C"))
    fallback = (
        str(actions.iloc[0].get("fallback_status") or "inactive")
        if not actions.empty
        else "no action"
    )
    status_cols[2].metric("Fallback", fallback)

    if telemetry.empty:
        st.info("The run exists but has not produced facility telemetry.")
    else:
        plot = telemetry.melt(
            id_vars=["simulation_timestamp"],
            value_vars=[
                "outdoor_temperature_c",
                "heating_setpoint_c",
                "cooling_setpoint_c",
            ],
            var_name="series",
            value_name="temperature_c",
        ).dropna()
        st.plotly_chart(
            px.line(
                plot,
                x="simulation_timestamp",
                y="temperature_c",
                color="series",
                title="Outdoor temperature and active setpoints",
            ),
            use_container_width=True,
        )
        demand_plot = px.line(
            telemetry,
            x="simulation_timestamp",
            y="facility_demand_kw",
            title="Facility electrical demand",
        )
        st.plotly_chart(demand_plot, use_container_width=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Latest proposed and applied actions")
        if actions.empty:
            st.caption("No action record yet.")
        else:
            st.dataframe(
                actions[
                    [
                        "timestamp",
                        "observation_id",
                        "proposed_values",
                        "applied_values",
                        "reason_code",
                        "latency_ms",
                        "fallback_status",
                    ]
                ],
                hide_index=True,
                use_container_width=True,
            )
    with right:
        st.subheader("Recent MCP tool calls")
        if tools.empty:
            st.caption("No MCP tool calls yet.")
        else:
            st.dataframe(tools, hide_index=True, use_container_width=True)


def _comparison(database_path: Path, runs: pd.DataFrame) -> None:
    baselines = runs[
        (runs["run_type"] == "baseline") & (runs["status"] == "completed") & (runs["is_fake"] == 0)
    ]
    controlled = runs[
        runs["run_type"].isin(["agent", "rule", "replay", "fixed_override"])
        & (runs["status"] == "completed")
        & (runs["is_fake"] == 0)
    ]
    if baselines.empty or controlled.empty:
        st.info("A completed real baseline and controlled run are both required.")
        return
    left, right = st.columns(2)
    baseline_id = left.selectbox("Baseline", baselines["run_id"].tolist())
    controlled_id = right.selectbox("Controlled", controlled["run_id"].tolist())
    compatible, message = queries.compare_status(
        database_path, str(baseline_id), str(controlled_id)
    )
    if not compatible:
        st.warning(message)
        return
    st.success(message)
    baseline_metrics = queries.verified_metrics(database_path, str(baseline_id))
    controlled_metrics = queries.verified_metrics(database_path, str(controlled_id))
    baseline_kwh = _metric_value(baseline_metrics, "facility_electricity_kwh")
    controlled_kwh = _metric_value(controlled_metrics, "facility_electricity_kwh")
    saving = (
        (baseline_kwh - controlled_kwh) / baseline_kwh * 100
        if baseline_kwh is not None and baseline_kwh > 0 and controlled_kwh is not None
        else None
    )
    cards = st.columns(5)
    cards[0].metric("Electricity saving", _format_value(saving, "%"))
    for index, metric_name in enumerate(
        [
            "peak_electrical_demand_kw",
            "hvac_electricity_kwh",
            "cost",
            "operational_carbon_kg",
        ],
        start=1,
    ):
        base = _metric_value(baseline_metrics, metric_name)
        agent = _metric_value(controlled_metrics, metric_name)
        cards[index].metric(
            metric_name.replace("_", " ").title(),
            _format_value(agent),
            delta=_delta_text(base, agent),
            delta_color="inverse",
        )

    aligned = queries.aligned_cumulative_energy(database_path, str(baseline_id), str(controlled_id))
    if not aligned.empty:
        figure = go.Figure()
        figure.add_scatter(
            x=aligned["simulation_timestamp"],
            y=aligned["cumulative_electricity_kwh_baseline"],
            name="Baseline",
        )
        figure.add_scatter(
            x=aligned["simulation_timestamp"],
            y=aligned["cumulative_electricity_kwh_controlled"],
            name="Controlled",
        )
        figure.update_layout(
            title="Cumulative telemetry cross-check (official totals shown above)",
            xaxis_title="Simulated time",
            yaxis_title="kWh",
        )
        st.plotly_chart(figure, use_container_width=True)

    comfort_rows = []
    for label, metrics in (("Baseline", baseline_metrics), ("Controlled", controlled_metrics)):
        comfort_rows.append(
            {
                "case": label,
                "occupied temperature violation %": _metric_value(
                    metrics, "occupied_temperature_violation_percent"
                ),
                "PMV compliance %": _metric_value(metrics, "pmv_compliance_percent"),
                "mean PPD %": _metric_value(metrics, "mean_ppd_percent"),
            }
        )
    st.dataframe(pd.DataFrame(comfort_rows), hide_index=True, use_container_width=True)


def _comfort(database_path: Path, run_id: str, limit: int | None) -> None:
    zones = queries.zone_telemetry(database_path, run_id)
    if zones.empty:
        st.info("No zone telemetry is available.")
        return
    if limit is not None:
        facility = queries.telemetry(database_path, run_id, limit=limit)
        if not facility.empty:
            zones = zones[zones["simulation_timestamp"] <= facility["simulation_timestamp"].max()]
    selected = st.multiselect(
        "Zones",
        sorted(zones["zone_name"].dropna().unique().tolist()),
        default=sorted(zones["zone_name"].dropna().unique().tolist()),
    )
    zones = zones[zones["zone_name"].isin(selected)]
    st.plotly_chart(
        px.line(
            zones,
            x="simulation_timestamp",
            y="operative_temperature_c",
            color="zone_name",
            title="Zone operative temperature",
        ),
        use_container_width=True,
    )
    comfort_cols = st.columns(3)
    comfort_cols[0].metric("Mean PPD", _format_value(zones["ppd_percent"].mean(), "%"))
    comfort_cols[1].metric("Max |PMV|", _format_value(zones["pmv"].abs().max()))
    comfort_cols[2].metric("Max CO2", _format_value(zones["co2_ppm"].max(), " ppm"))
    if zones["pmv"].notna().any():
        st.plotly_chart(
            px.line(
                zones.dropna(subset=["pmv"]),
                x="simulation_timestamp",
                y="pmv",
                color="zone_name",
                title="Zone Fanger PMV",
            ),
            use_container_width=True,
        )
    else:
        st.caption("PMV is unavailable for this run.")
    if zones["co2_ppm"].notna().any():
        st.plotly_chart(
            px.line(
                zones.dropna(subset=["co2_ppm"]),
                x="simulation_timestamp",
                y="co2_ppm",
                color="zone_name",
                title="Zone CO2 concentration",
            ),
            use_container_width=True,
        )
    else:
        st.caption("CO2 is unavailable because the model did not expose it.")


def _decisions(database_path: Path, run_id: str) -> None:
    decisions = queries.recent_decisions(database_path, run_id, limit=200)
    actions = queries.recent_actions(database_path, run_id, limit=200)
    if decisions.empty:
        st.info("No decision records exist for this run.")
    else:
        st.dataframe(
            decisions[
                [
                    "timestamp",
                    "observation_id",
                    "state_summary",
                    "candidate_scores",
                    "reason_code",
                    "explanation",
                    "latency_ms",
                    "fallback_status",
                    "completed",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )
    st.subheader("Proposed versus safety-applied actions")
    if actions.empty:
        st.caption("No actions exist for this run.")
    else:
        st.dataframe(
            actions[
                [
                    "timestamp",
                    "observation_id",
                    "proposed_values",
                    "applied_values",
                    "clamp_details",
                    "validation_result",
                    "reason_code",
                    "explanation",
                    "latency_ms",
                ]
            ],
            hide_index=True,
            use_container_width=True,
        )


def _reliability(database_path: Path, run_id: str) -> None:
    tools = queries.recent_tool_calls(database_path, run_id, limit=10_000)
    decisions = queries.recent_decisions(database_path, run_id, limit=10_000)
    actions = queries.recent_actions(database_path, run_id, limit=10_000)
    errors = queries.errors_and_messages(database_path, run_id, limit=500)
    cards = st.columns(6)
    cards[0].metric("Decisions", len(decisions))
    cards[1].metric("Tool calls", len(tools))
    cards[2].metric(
        "Mean tool latency",
        _format_value(tools["duration_ms"].mean() if not tools.empty else None, " ms"),
    )
    cards[3].metric(
        "Fallbacks",
        int(actions["fallback_status"].notna().sum()) if not actions.empty else 0,
    )
    cards[4].metric(
        "Safety clamps",
        int((actions.get("clamp_details", pd.Series(dtype=str)) != "[]").sum()),
    )
    cards[5].metric(
        "Failed tools",
        int((tools["success"] == 0).sum()) if not tools.empty else 0,
    )
    if errors.empty:
        st.success("No simulation messages or errors are recorded.")
    else:
        severity_counts = errors.groupby("severity").size().reset_index(name="count")
        st.plotly_chart(
            px.bar(severity_counts, x="severity", y="count", title="Message severity counts"),
            use_container_width=True,
        )
        st.dataframe(errors, hide_index=True, use_container_width=True)


def _methodology() -> None:
    path = repository_root() / "docs" / "methodology.md"
    if path.is_file():
        st.markdown(path.read_text(encoding="utf-8"))
    else:
        st.info("Methodology document is unavailable.")


def _metric_value(metrics: dict[str, dict[str, Any]], name: str) -> float | None:
    item = metrics.get(name)
    if not item or item.get("value") is None:
        return None
    return float(item["value"])


def _format_value(value: Any, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    if isinstance(value, (int, float)):
        return f"{float(value):,.2f}{suffix}"
    return f"{value}{suffix}"


def _delta_text(baseline: float | None, controlled: float | None) -> str | None:
    if baseline is None or controlled is None:
        return None
    return f"{controlled - baseline:+,.2f} vs baseline"


def _pmv_co2(pmv: Any, co2: Any) -> str:
    pmv_text = _format_value(pmv)
    co2_text = _format_value(co2, " ppm")
    return f"{pmv_text} / {co2_text}"


def _apply_style() -> None:
    st.markdown(
        """
        <style>
        .stApp { background: #f6f8f5; }
        [data-testid="stMetric"] {
            background: white;
            border: 1px solid #dfe7df;
            border-radius: 12px;
            padding: 12px;
        }
        h1, h2, h3 { color: #183a2b; }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
