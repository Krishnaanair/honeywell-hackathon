"""Streamlit dashboard reading only the durable run database."""

from __future__ import annotations

import json
import os
import time
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from ecoloop.config import get_settings, repository_root
from ecoloop.dashboard import queries
from ecoloop.dashboard.queries import DashboardDataError
from ecoloop.dashboard.styles import DASHBOARD_CSS


def main() -> None:
    """Render the six-tab EcoLoop operational dashboard."""

    st.set_page_config(
        page_title="EcoLoop Control Room",
        page_icon="🌿",
        layout="wide",
        initial_sidebar_state="auto",
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

    _brand_header(run, replay_enabled=replay_enabled)
    _run_context(run, replay_enabled=replay_enabled)
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

    st.markdown(
        "<div class='ecoloop-footer'>"
        "<strong>EcoLoop evidence console</strong><br>"
        "EnergyPlus 26.1.0 &nbsp;·&nbsp; Local inference &nbsp;·&nbsp; "
        "Audited MCP control &nbsp;·&nbsp; SQLite evidence bus"
        "</div>",
        unsafe_allow_html=True,
    )

    if replay_enabled and st.session_state.get("ecoloop_replay_autoplay"):
        speed = float(st.session_state.get("ecoloop_replay_speed", 1.0))
        time.sleep(max(0.05, 1.0 / speed))
        st.session_state["ecoloop_replay_cursor"] = (
            int(st.session_state.get("ecoloop_replay_cursor", 1)) + 1
        )
        st.rerun()


def _brand_header(run: dict[str, Any], *, replay_enabled: bool) -> None:
    status = str(run.get("status") or "unknown").casefold()
    if replay_enabled:
        mode_label = "REAL RUN REPLAY"
        mode_detail = "Recorded physical telemetry · visual playback only"
        mode_class = "mode-replay"
    elif status == "running":
        mode_label = "LIVE SIMULATION"
        mode_detail = "EnergyPlus telemetry · control loop active"
        mode_class = "mode-live"
    elif status == "completed":
        mode_label = "COMPLETED EVIDENCE"
        mode_detail = "Immutable run record · verified outputs"
        mode_class = "mode-complete"
    else:
        mode_label = status.upper()
        mode_detail = "Run record · inspect status below"
        mode_class = "mode-neutral"
    st.markdown(
        f"""
        <section class="ecoloop-hero">
          <div class="hero-copy">
            <div class="ecoloop-eyebrow">BUILDING SUPERVISORY CONTROL</div>
            <h1>EcoLoop Control Room</h1>
            <p>
              Physical simulation, local decision-making and deterministic
              guardrails in one audit-ready operations view.
            </p>
          </div>
          <div class="hero-mode {mode_class}">
            <span class="mode-signal"></span>
            <div>
              <strong>{escape(mode_label)}</strong>
              <small>{escape(mode_detail)}</small>
            </div>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _run_context(run: dict[str, Any], *, replay_enabled: bool) -> None:
    run_id = escape(str(run["run_id"]))
    status = escape(str(run.get("status") or "unknown"))
    run_type = escape(str(run.get("run_type") or "unknown"))
    period = escape(str(run.get("period_name") or "unspecified"))
    evidence_label = "Replay source" if replay_enabled else "Evidence source"
    status_class = {
        "completed": "status-completed",
        "running": "status-running",
        "failed": "status-failed",
    }.get(status.casefold(), "status-neutral")
    st.markdown(
        f"""
        <div class="ecoloop-run-strip">
          <div class="run-primary">
            <span class="run-label">{escape(evidence_label.upper())}</span>
            <div class="run-title">
              <strong>{run_type.upper()} · {period.upper()}</strong>
              <code>{run_id}</code>
            </div>
          </div>
          <div class="run-secondary">
            <span class="data-origin">REAL DATABASE RECORD</span>
            <span class="run-status {status_class}">{status.upper()}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    progress = float(run.get("progress_percent") or 0.0)
    if status.casefold() == "completed":
        progress = 100.0
    st.progress(
        min(max(progress, 0.0), 100.0) / 100.0,
        text=f"Simulation lifecycle · {progress:.0f}%",
    )
    st.sidebar.markdown(
        f"""
        <div class="sidebar-run-card">
          <span>{run_type.upper()}</span>
          <strong>{period.title()}</strong>
          <small>{status.upper()} · {progress:.0f}%</small>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.button("Refresh evidence", width="stretch", type="primary")
    st.sidebar.caption(f"EnergyPlus {escape(str(run.get('energyplus_version') or 'unknown'))}")


def _select_run(runs: pd.DataFrame, forced: str | None) -> str:
    st.sidebar.markdown(
        """
        <div class="sidebar-brand">
          <span>ECOLOOP</span>
          <strong>Evidence console</strong>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.caption("Real runs only · test fixtures hidden")
    choices = [str(item) for item in runs["run_id"].tolist()]
    if forced:
        if forced not in choices:
            st.error(f"Replay run is not a real run in the database: {forced}")
            st.stop()
        return forced
    labels = {
        str(row.run_id): (
            f"{str(row.run_type).upper()} · {str(row.status).upper()} · "
            f"{str(row.period_name or 'period').title()} · {str(row.run_id)[-8:]}"
        )
        for row in runs.itertuples()
    }
    st.sidebar.markdown("#### Select evidence run")
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


@st.fragment(run_every=3.0)
def _live_operations(database_path: Path, run: dict[str, Any], limit: int | None) -> None:
    refreshed_run = queries.get_run(database_path, str(run["run_id"]))
    if refreshed_run is not None:
        run = refreshed_run
    run_stats = queries.run_statistics(database_path, str(run["run_id"]))
    _section_header(
        "Live Operations",
        eyebrow="PHYSICAL STATE",
        description=(
            "Latest persisted EnergyPlus telemetry, active setpoints and the "
            "control path that produced them."
        ),
    )
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
    actions = queries.recent_actions(database_path, str(run["run_id"]), limit=25)
    tools = queries.recent_tool_calls(database_path, str(run["run_id"]), limit=8)
    decisions = queries.recent_decisions(database_path, str(run["run_id"]), limit=5)

    simulation_clock = "Waiting for telemetry"
    demand = None
    heat_sp = None
    cool_sp = None
    cumulative_energy = None
    outdoor = None
    timestep_energy = None
    recorded_at = None
    if not latest.empty:
        row = latest.iloc[0]
        simulation_clock = _simulation_clock(row["simulation_timestamp"])
        demand = row.get("facility_demand_kw")
        heat_sp = row.get("heating_setpoint_c")
        cool_sp = row.get("cooling_setpoint_c")
        cumulative_energy = row.get("cumulative_electricity_kwh")
        outdoor = row.get("outdoor_temperature_c")
        timestep_energy = row.get("timestep_electricity_kwh")
        recorded_at = row.get("timestamp")
    occupancy = latest_zones["occupant_count"].sum() if not latest_zones.empty else None
    temp = latest_zones["operative_temperature_c"].mean() if not latest_zones.empty else None
    temp_min = latest_zones["operative_temperature_c"].min() if not latest_zones.empty else None
    temp_max = latest_zones["operative_temperature_c"].max() if not latest_zones.empty else None
    pmv_abs = latest_zones["pmv"].abs().max() if not latest_zones.empty else None
    latency = decisions.iloc[0].get("latency_ms") if not decisions.empty else None
    applied_actions = (
        actions[actions["applied"] == 1] if not actions.empty and "applied" in actions else actions
    )
    fallback = (
        str(applied_actions.iloc[0].get("fallback_status") or "inactive")
        if not applied_actions.empty
        else "no action"
    )
    fallback_active = fallback.casefold() not in {"", "none", "inactive", "no action"}

    _telemetry_banner(
        run_status=str(run.get("status") or "unknown"),
        simulation_clock=simulation_clock,
        recorded_at=recorded_at,
        row_count=len(telemetry) if limit is not None else run_stats["telemetry_steps"],
        replay=limit is not None,
    )

    columns = st.columns(4)
    columns[0].metric("Simulation clock", simulation_clock)
    columns[1].metric("Progress", _format_value(run.get("progress_percent"), "%"))
    columns[2].metric("Facility demand", _format_value(demand, " kW"))
    columns[3].metric("Cumulative electricity", _format_value(cumulative_energy, " kWh"))

    status_cols = st.columns(4)
    status_cols[0].metric(
        "Mean operative temperature",
        _format_value(temp, " °C"),
        delta=_range_text(temp_min, temp_max, " °C"),
        delta_color="off",
    )
    status_cols[1].metric("Outdoor air", _format_value(outdoor, " °C"))
    status_cols[2].metric("Occupancy", _format_value(occupancy, " people"))
    status_cols[3].metric("Latest timestep energy", _format_value(timestep_energy, " kWh"))

    control_cols = st.columns(4)
    control_cols[0].metric("Heating setpoint", _format_value(heat_sp, " °C"))
    control_cols[1].metric("Cooling setpoint", _format_value(cool_sp, " °C"))
    control_cols[2].metric("Maximum |PMV|", _format_value(pmv_abs))
    control_cols[3].metric("Latest decision latency", _format_duration_ms(latency))

    if fallback_active and str(run.get("status")).casefold() == "running":
        st.warning(f"Deterministic fallback active · {fallback.replace('_', ' ').title()}")
    elif fallback_active:
        st.info(
            "The final physically applied action used deterministic fallback · "
            f"{fallback.replace('_', ' ').title()}."
        )
    elif str(run.get("status")).casefold() == "running":
        st.success("Control loop healthy · live telemetry and audited actions are arriving.")
    elif str(run.get("status")).casefold() == "failed":
        st.error("This run failed. Savings and comparison results are intentionally suppressed.")

    if telemetry.empty:
        st.info("The run exists but has not produced facility telemetry.")
    else:
        chart_window = st.selectbox(
            "Chart window",
            ("Last 24 simulated hours", "Last 72 simulated hours", "Full run"),
            index=0 if str(run.get("status")).casefold() == "running" else 2,
            key=f"operations_window_{run['run_id']}",
        )
        chart_telemetry = _select_simulated_window(telemetry, chart_window)
        chart_zones = zones[
            zones["simulation_timestamp"].isin(chart_telemetry["simulation_timestamp"])
        ]
        live_frame = telemetry.copy()
        live_frame = chart_telemetry.copy()
        if not chart_zones.empty:
            mean_zone = (
                chart_zones.groupby("simulation_timestamp", as_index=False)[
                    "operative_temperature_c"
                ]
                .mean()
                .rename(columns={"operative_temperature_c": "mean_operative_temperature_c"})
            )
            live_frame = live_frame.merge(
                mean_zone,
                on="simulation_timestamp",
                how="left",
            )
        else:
            live_frame["mean_operative_temperature_c"] = pd.NA
        temperature_figure = go.Figure()
        for column, label, color, dash in (
            ("mean_operative_temperature_c", "Mean operative", "#12b981", "solid"),
            ("outdoor_temperature_c", "Outdoor", "#6b7f75", "dot"),
            ("heating_setpoint_c", "Heating setpoint", "#e59b2f", "dash"),
            ("cooling_setpoint_c", "Cooling setpoint", "#168aad", "dash"),
        ):
            if column in live_frame and live_frame[column].notna().any():
                temperature_figure.add_scatter(
                    x=live_frame["simulation_timestamp"],
                    y=live_frame[column],
                    name=label,
                    mode="lines",
                    line={"color": color, "width": 2.4, "dash": dash},
                )
        _style_chart(
            temperature_figure,
            title="Temperature envelope",
            yaxis_title="Temperature (°C)",
        )
        demand_figure = go.Figure()
        demand_figure.add_scatter(
            x=chart_telemetry["simulation_timestamp"],
            y=chart_telemetry["facility_demand_kw"],
            name="Demand",
            mode="lines",
            fill="tozeroy",
            line={"color": "#0d766e", "width": 2.4},
            fillcolor="rgba(13,118,110,0.14)",
        )
        _style_chart(
            demand_figure,
            title="Electrical demand",
            yaxis_title="Demand (kW)",
        )
        st.plotly_chart(temperature_figure, width="stretch")
        st.plotly_chart(demand_figure, width="stretch")

    if not latest_zones.empty:
        st.markdown("#### Current zone snapshot")
        zone_snapshot = latest_zones[
            [
                "zone_name",
                "operative_temperature_c",
                "relative_humidity_percent",
                "occupant_count",
                "pmv",
                "ppd_percent",
                "co2_ppm",
            ]
        ].rename(
            columns={
                "zone_name": "Zone",
                "operative_temperature_c": "Operative °C",
                "relative_humidity_percent": "RH %",
                "occupant_count": "People",
                "pmv": "PMV",
                "ppd_percent": "PPD %",
                "co2_ppm": "CO₂ ppm",
            }
        )
        st.dataframe(
            zone_snapshot,
            hide_index=True,
            width="stretch",
            column_config={
                "Operative °C": st.column_config.NumberColumn(format="%.2f"),
                "RH %": st.column_config.NumberColumn(format="%.1f"),
                "People": st.column_config.NumberColumn(format="%.1f"),
                "PMV": st.column_config.NumberColumn(format="%.2f"),
                "PPD %": st.column_config.NumberColumn(format="%.1f"),
                "CO₂ ppm": st.column_config.NumberColumn(format="%.0f"),
            },
        )

    _subsection_header(
        "Control audit",
        "The latest controller proposal, physically applied values and MCP calls.",
    )
    left, right = st.columns((0.9, 1.1), gap="large")
    with left:
        st.markdown("#### Latest control action")
        if actions.empty:
            st.caption("No action record yet.")
        else:
            proposed_action = actions.iloc[0]
            st.markdown(
                _action_card(
                    title="Proposed by controller",
                    values=proposed_action.get("proposed_values"),
                    accent="proposed",
                ),
                unsafe_allow_html=True,
            )
            if int(proposed_action.get("applied") or 0) == 0:
                st.caption("Latest proposal was not physically applied.")
            if applied_actions.empty:
                st.caption("No action has been physically applied.")
            else:
                applied_action = applied_actions.iloc[0]
                st.markdown(
                    _action_card(
                        title="Latest physically applied action",
                        values=applied_action.get("applied_values"),
                        accent="applied",
                    ),
                    unsafe_allow_html=True,
                )
                reason = (
                    str(applied_action.get("reason_code") or "unspecified")
                    .replace("_", " ")
                    .title()
                )
                explanation = (
                    applied_action.get("explanation") or "No operational explanation recorded."
                )
                st.caption(f"{reason} · {explanation!s}")
    with right:
        st.markdown("#### Recent MCP tool trace")
        if tools.empty:
            st.caption("No MCP tool calls yet.")
        else:
            tool_view = tools[
                ["timestamp", "tool_name", "success", "duration_ms", "control_affecting"]
            ].rename(
                columns={
                    "timestamp": "Timestamp",
                    "tool_name": "Tool",
                    "success": "OK",
                    "duration_ms": "Latency ms",
                    "control_affecting": "Control-affecting",
                }
            )
            st.dataframe(
                tool_view,
                hide_index=True,
                width="stretch",
                column_config={
                    "OK": st.column_config.CheckboxColumn(),
                    "Control-affecting": st.column_config.CheckboxColumn(),
                    "Latency ms": st.column_config.NumberColumn(format="%.1f"),
                },
            )


def _comparison(database_path: Path, runs: pd.DataFrame) -> None:
    _section_header(
        "Baseline vs Agent",
        eyebrow="MEASURED OUTCOMES",
        description=(
            "Official EnergyPlus totals from compatible completed real runs. "
            "Incomplete runs and test fixtures are excluded."
        ),
    )
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
    controlled_id = left.selectbox(
        "Controlled run",
        controlled["run_id"].tolist(),
        index=_preferred_controlled_index(controlled),
    )
    controlled_row = controlled[controlled["run_id"] == controlled_id].iloc[0]
    matching = baselines[
        (baselines["period_name"] == controlled_row["period_name"])
        & (baselines["energyplus_version"] == controlled_row["energyplus_version"])
        & (baselines["weather_path"] == controlled_row["weather_path"])
    ]
    baseline_options = baselines["run_id"].tolist()
    default_baseline = (
        baseline_options.index(matching.iloc[0]["run_id"]) if not matching.empty else 0
    )
    baseline_id = right.selectbox(
        "Reference baseline",
        baseline_options,
        index=default_baseline,
    )
    comparison_stats = queries.comparison_statistics(
        database_path, str(baseline_id), str(controlled_id)
    )
    if not comparison_stats["compatible"]:
        st.warning(comparison_stats["message"])
        return
    _comparison_provenance(
        baseline_run=queries.get_run(database_path, str(baseline_id)),
        controlled_run=queries.get_run(database_path, str(controlled_id)),
    )
    baseline_metrics = queries.verified_metrics(database_path, str(baseline_id))
    controlled_metrics = queries.verified_metrics(database_path, str(controlled_id))
    electricity_stats = comparison_stats["metrics"]["facility_electricity_kwh"]
    baseline_kwh = electricity_stats["baseline"]
    controlled_kwh = electricity_stats["controlled"]
    electricity_change = electricity_stats["percent_delta"]
    if electricity_change is not None and electricity_change <= 0:
        _outcome_banner(
            tone="positive",
            title=f"{abs(electricity_change):.2f}% lower facility electricity",
            detail="Measured over the complete selected period; lower is better.",
        )
    elif electricity_change is not None:
        _outcome_banner(
            tone="caution",
            title=f"{electricity_change:.2f}% higher facility electricity",
            detail=(
                "The controlled case used more electricity. The dashboard reports "
                "this measured result without adjustment."
            ),
        )
    else:
        _outcome_banner(
            tone="neutral",
            title="Electricity change unavailable",
            detail="The selected pair does not contain both verified facility totals.",
        )

    peak_baseline = _metric_value(baseline_metrics, "peak_electrical_demand_kw")
    peak_controlled = _metric_value(controlled_metrics, "peak_electrical_demand_kw")
    baseline_violation = _metric_value(baseline_metrics, "occupied_temperature_violation_percent")
    controlled_violation = _metric_value(
        controlled_metrics, "occupied_temperature_violation_percent"
    )
    baseline_compliance = 100.0 - baseline_violation if baseline_violation is not None else None
    controlled_compliance = (
        100.0 - controlled_violation if controlled_violation is not None else None
    )
    cards = st.columns(4)
    cards[0].metric(
        "Facility electricity",
        _format_value(controlled_kwh, " kWh"),
        delta=_comparison_delta(baseline_kwh, controlled_kwh, " kWh"),
        delta_color="inverse",
    )
    cards[1].metric(
        "Peak demand",
        _format_value(peak_controlled, " kW"),
        delta=_comparison_delta(peak_baseline, peak_controlled, " kW"),
        delta_color="inverse",
    )
    cards[2].metric(
        "Occupied comfort",
        _format_value(controlled_compliance, "%"),
        delta=_points_delta(baseline_compliance, controlled_compliance),
        delta_color="normal",
    )
    cards[3].metric(
        "PMV compliance",
        _format_value(
            _metric_value(controlled_metrics, "pmv_compliance_percent"),
            "%",
        ),
        delta=_points_delta(
            _metric_value(baseline_metrics, "pmv_compliance_percent"),
            _metric_value(controlled_metrics, "pmv_compliance_percent"),
        ),
        delta_color="normal",
    )

    impact_cards = st.columns(4)
    for card, metric_name, label, suffix in (
        (impact_cards[0], "cost", "Operating cost", ""),
        (impact_cards[1], "operational_carbon_kg", "Operational carbon", " kg"),
        (
            impact_cards[2],
            "occupied_temperature_violation_degree_hours",
            "Violation degree-hours",
            " °C·h",
        ),
        (impact_cards[3], "pmv_compliance_percent", "PMV compliance", "%"),
    ):
        base = _metric_value(baseline_metrics, metric_name)
        controlled_value = _metric_value(controlled_metrics, metric_name)
        card.metric(
            label,
            _format_value(controlled_value, suffix),
            delta=_comparison_delta(base, controlled_value, suffix),
            delta_color="inverse" if metric_name != "pmv_compliance_percent" else "normal",
        )

    _subsection_header(
        "Energy trajectory",
        "Cumulative Runtime API telemetry aligned at identical simulated timestamps.",
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
        _style_chart(
            figure,
            title="Cumulative electricity · aligned simulated time",
            yaxis_title="Electricity (kWh)",
        )
        st.plotly_chart(figure, width="stretch")

    _subsection_header(
        "Verified outcome scorecard",
        "Every row comes from metrics explicitly marked verified in the run database.",
    )
    scorecard = _comparison_scorecard(baseline_metrics, controlled_metrics)
    st.dataframe(
        scorecard,
        hide_index=True,
        width="stretch",
        column_config={
            "Baseline": st.column_config.NumberColumn(format="%.2f"),
            "Controlled": st.column_config.NumberColumn(format="%.2f"),
            "Absolute change": st.column_config.NumberColumn(format="%+.2f"),
            "Relative change": st.column_config.NumberColumn(format="%+.2f%%"),
        },
    )


def _comfort(database_path: Path, run_id: str, limit: int | None) -> None:
    _section_header(
        "Comfort and IAQ",
        eyebrow="ZONE EVIDENCE",
        description=(
            "Thermal comfort and air-quality points reported by the EnergyPlus "
            "physical model, without imputation."
        ),
    )
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
    if zones.empty:
        st.info("Select at least one zone to display comfort evidence.")
        return
    occupied = zones[zones["occupant_count"].fillna(0) > 0]
    comfort_basis = occupied if not occupied.empty else zones.iloc[0:0]
    occupied_operative = comfort_basis["operative_temperature_c"].dropna()
    occupied_pmv = comfort_basis["pmv"].dropna()
    occupied_compliance = (
        float(occupied_operative.between(22.0, 26.0, inclusive="both").mean() * 100.0)
        if not occupied_operative.empty
        else None
    )
    pmv_compliance = (
        float((occupied_pmv.abs() <= 0.7).mean() * 100.0) if not occupied_pmv.empty else None
    )
    comfort_cols = st.columns(4)
    comfort_cols[0].metric("Selected zones", len(selected))
    comfort_cols[1].metric(
        "Occupied temperature compliance",
        _format_value(occupied_compliance, "%"),
    )
    comfort_cols[2].metric("Occupied PMV compliance", _format_value(pmv_compliance, "%"))
    comfort_cols[3].metric(
        "Mean occupied PPD",
        _format_value(
            comfort_basis["ppd_percent"].mean() if not comfort_basis.empty else None,
            "%",
        ),
    )

    replay_end = (
        zones["simulation_timestamp"].max() if limit is not None and not zones.empty else None
    )
    zone_summary = queries.comfort_distribution(
        database_path,
        run_id,
        simulation_end=replay_end,
    )
    zone_summary = zone_summary[zone_summary["zone_name"].isin(selected)].rename(
        columns={
            "zone_name": "Zone",
            "samples": "Samples",
            "occupied_samples": "Occupied samples",
            "operative_temperature_mean_c": "Mean operative °C",
            "operative_temperature_p05_c": "P05 operative °C",
            "operative_temperature_p95_c": "P95 operative °C",
            "occupied_mean_ppd_percent": "Mean PPD %",
            "occupied_pmv_max_abs": "Max |PMV|",
            "relative_humidity_mean_percent": "Mean RH %",
            "co2_max_ppm": "Max CO₂ ppm",
        }
    )
    zone_summary = zone_summary[
        [
            "Zone",
            "Samples",
            "Occupied samples",
            "Mean operative °C",
            "P05 operative °C",
            "P95 operative °C",
            "Mean PPD %",
            "Max |PMV|",
            "Mean RH %",
            "Max CO₂ ppm",
        ]
    ]
    st.dataframe(
        zone_summary,
        hide_index=True,
        width="stretch",
        column_config={
            "Mean operative °C": st.column_config.NumberColumn(format="%.2f"),
            "P05 operative °C": st.column_config.NumberColumn(format="%.2f"),
            "P95 operative °C": st.column_config.NumberColumn(format="%.2f"),
            "Mean PPD %": st.column_config.NumberColumn(format="%.2f"),
            "Max |PMV|": st.column_config.NumberColumn(format="%.2f"),
            "Mean RH %": st.column_config.NumberColumn(format="%.1f"),
            "Max CO₂ ppm": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    _subsection_header(
        "Temperature distribution",
        "The shaded band is the configured occupied operative-temperature target.",
    )
    distribution_figure = px.box(
        zones,
        x="zone_name",
        y="operative_temperature_c",
        color="zone_name",
        points=False,
        color_discrete_sequence=px.colors.qualitative.Safe,
    )
    distribution_figure.add_hrect(
        y0=22,
        y1=26,
        fillcolor="rgba(17,167,125,0.10)",
        line_width=0,
    )
    _style_chart(
        distribution_figure,
        title="Operative temperature distribution by zone",
        yaxis_title="Temperature (°C)",
        height=390,
    )
    distribution_figure.update_layout(showlegend=False)
    st.plotly_chart(distribution_figure, width="stretch")

    _subsection_header(
        "Thermal trajectory",
        "Zone traces remain aligned to their original simulated timestamps.",
    )
    temperature_figure = px.line(
        zones,
        x="simulation_timestamp",
        y="operative_temperature_c",
        color="zone_name",
        color_discrete_sequence=px.colors.qualitative.Safe,
    )
    temperature_figure.add_hrect(
        y0=22,
        y1=26,
        fillcolor="rgba(18,185,129,0.10)",
        line_width=0,
        annotation_text="Occupied target",
        annotation_position="top left",
    )
    _style_chart(
        temperature_figure,
        title="Zone operative temperature",
        yaxis_title="Temperature (°C)",
    )
    st.plotly_chart(temperature_figure, width="stretch")
    if zones["pmv"].notna().any():
        pmv_figure = px.line(
            zones.dropna(subset=["pmv"]),
            x="simulation_timestamp",
            y="pmv",
            color="zone_name",
            color_discrete_sequence=px.colors.qualitative.Safe,
        )
        pmv_figure.add_hrect(
            y0=-0.7,
            y1=0.7,
            fillcolor="rgba(18,185,129,0.10)",
            line_width=0,
        )
        _style_chart(
            pmv_figure,
            title="Fanger PMV · target |PMV| ≤ 0.7",
            yaxis_title="PMV",
        )
        st.plotly_chart(pmv_figure, width="stretch")
    else:
        st.caption("PMV is unavailable for this run.")
    if zones["co2_ppm"].notna().any():
        co2_figure = px.line(
            zones.dropna(subset=["co2_ppm"]),
            x="simulation_timestamp",
            y="co2_ppm",
            color="zone_name",
            color_discrete_sequence=px.colors.qualitative.Safe,
        )
        co2_figure.add_hline(
            y=1000,
            line_dash="dash",
            line_color="#d97706",
            annotation_text="1000 ppm target",
        )
        _style_chart(
            co2_figure,
            title="Zone CO₂ concentration",
            yaxis_title="CO₂ (ppm)",
        )
        st.plotly_chart(co2_figure, width="stretch")
    else:
        st.info(
            "CO₂ is not available for this run because the source model does not expose "
            "a verified contaminant-simulation point. The dashboard leaves it blank."
        )


def _decisions(database_path: Path, run_id: str) -> None:
    _section_header(
        "Agent Decisions",
        eyebrow="CONTROL TRACE",
        description=(
            "Operational explanations, candidate evaluations and deterministic "
            "validation from the audited tool loop."
        ),
    )
    decisions = queries.recent_decisions(database_path, run_id, limit=10_000)
    actions = queries.recent_actions(database_path, run_id, limit=10_000)
    tools = queries.recent_tool_calls(database_path, run_id, limit=10_000)
    if decisions.empty:
        st.info("No decision records exist for this run.")
    else:
        applied_count = (
            int(actions["applied"].fillna(0).sum())
            if not actions.empty and "applied" in actions
            else 0
        )
        tool_success = float(tools["success"].fillna(0).mean() * 100.0) if not tools.empty else None
        decision_cards = st.columns(4)
        decision_cards[0].metric("Decisions", len(decisions))
        decision_cards[1].metric(
            "Average latency",
            _format_duration_ms(decisions["latency_ms"].mean()),
        )
        decision_cards[2].metric(
            "P95 latency",
            _format_duration_ms(decisions["latency_ms"].quantile(0.95)),
        )
        decision_cards[3].metric(
            "Tool success",
            _format_value(tool_success, "%"),
        )
        pathway_cards = st.columns(4)
        pathway_cards[0].metric(
            "Physically applied",
            applied_count,
            delta=_rate_text(applied_count, len(actions)),
            delta_color="off",
        )
        pathway_cards[1].metric(
            "Fallback decisions",
            int(decisions["fallback_status"].map(_is_active_fallback).sum()),
        )
        pathway_cards[2].metric(
            "Safety-modified actions",
            int(actions["clamp_details"].map(_has_clamp).sum()) if not actions.empty else 0,
        )
        pathway_cards[3].metric(
            "Cached actions",
            int(actions["cache_hit"].fillna(0).sum())
            if not actions.empty and "cache_hit" in actions
            else 0,
        )
        latest = decisions.iloc[0]
        outcome = (
            str(latest.get("fallback_status")).replace("_", " ").title()
            if _is_active_fallback(latest.get("fallback_status"))
            else "Validated action"
        )
        latest_reason = escape(
            str(latest.get("reason_code") or "unspecified").replace("_", " ").title()
        )
        latest_explanation = escape(
            str(latest.get("explanation") or "No operational explanation recorded.")
        )
        latest_observation = escape(str(latest.get("observation_id")))
        st.markdown(
            f"""
            <div class="decision-summary">
              <span class="decision-kicker">LATEST DECISION · {escape(outcome.upper())}</span>
              <strong>{latest_reason}</strong>
              <p>{latest_explanation}</p>
              <small>Observation {latest_observation} ·
              {_format_duration_ms(latest.get("latency_ms"))}</small>
            </div>
            """,
            unsafe_allow_html=True,
        )

        latency_figure = go.Figure()
        ordered_decisions = decisions.sort_values("timestamp")
        latency_figure.add_scatter(
            x=ordered_decisions["timestamp"],
            y=ordered_decisions["latency_ms"] / 1_000.0,
            mode="lines+markers",
            name="Decision latency",
            line={"color": "#167d91", "width": 2.2},
            marker={"size": 5},
        )
        _style_chart(
            latency_figure,
            title="Decision latency over the run",
            yaxis_title="Latency (seconds)",
        )
        st.plotly_chart(latency_figure, width="stretch")

        with st.expander("Decision history and candidate score components", expanded=False):
            st.dataframe(
                decisions.head(250)[
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
                width="stretch",
            )
    st.markdown("#### Proposed versus safety-applied actions")
    if actions.empty:
        st.caption("No actions exist for this run.")
    else:
        st.dataframe(
            actions.head(250)[
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
            width="stretch",
        )
    st.markdown("#### MCP tools called")
    if tools.empty:
        st.caption("No tool-call trace exists for this run.")
    else:
        tool_counts = (
            tools.groupby("tool_name", as_index=False)
            .agg(
                calls=("tool_name", "size"),
                success_rate=("success", "mean"),
                mean_latency_ms=("duration_ms", "mean"),
            )
            .sort_values("calls", ascending=True)
        )
        tool_counts["success_rate"] = tool_counts["success_rate"] * 100.0
        tool_figure = px.bar(
            tool_counts,
            x="calls",
            y="tool_name",
            orientation="h",
            color="mean_latency_ms",
            color_continuous_scale=["#dff3ed", "#0d766e"],
            hover_data={"success_rate": ":.1f", "mean_latency_ms": ":.1f"},
        )
        _style_chart(
            tool_figure,
            title="MCP tool usage",
            yaxis_title="Tool",
            height=max(340, min(560, 120 + 38 * len(tool_counts))),
        )
        tool_figure.update_layout(coloraxis_colorbar_title="Mean ms")
        st.plotly_chart(tool_figure, width="stretch")
        with st.expander("Complete recent MCP trace", expanded=False):
            st.dataframe(
                tools.head(500)[
                    [
                        "timestamp",
                        "sequence",
                        "tool_name",
                        "success",
                        "duration_ms",
                        "control_affecting",
                        "error",
                    ]
                ],
                hide_index=True,
                width="stretch",
                column_config={
                    "success": st.column_config.CheckboxColumn(),
                    "control_affecting": st.column_config.CheckboxColumn(),
                    "duration_ms": st.column_config.NumberColumn(format="%.1f"),
                },
            )


def _reliability(database_path: Path, run_id: str) -> None:
    _section_header(
        "Reliability and Errors",
        eyebrow="EVIDENCE HEALTH",
        description=(
            "Timeout recovery, safety intervention, protocol health and EnergyPlus "
            "diagnostics kept in separate audit channels."
        ),
    )
    tools = queries.recent_tool_calls(database_path, run_id, limit=10_000)
    decisions = queries.recent_decisions(database_path, run_id, limit=10_000)
    actions = queries.recent_actions(database_path, run_id, limit=10_000)
    errors = queries.errors_and_messages(database_path, run_id, limit=500)
    run_stats = queries.run_statistics(database_path, run_id)
    applied_actions = (
        actions[actions["applied"] == 1] if not actions.empty and "applied" in actions else actions
    )
    verified = queries.verified_metrics(database_path, run_id)
    fallback_count = _verified_count(
        verified,
        "fallback_count",
        int(applied_actions["fallback_status"].map(_is_active_fallback).sum())
        if not applied_actions.empty
        else 0,
    )
    clamp_count = _verified_count(
        verified,
        "safety_clamp_count",
        int(applied_actions["clamp_details"].map(_has_clamp).sum())
        if not applied_actions.empty
        else 0,
    )
    timeout_count = _verified_count(
        verified,
        "timeout_count",
        int(decisions["timeout_count"].fillna(0).sum()) if "timeout_count" in decisions else 0,
    )
    invalid_count = _verified_count(verified, "invalid_action_count", 0)
    energyplus_diagnostics, controller_diagnostics = _diagnostic_groups(errors)
    cross_check = _structured_metric(verified, "energy_cross_check")
    finalization = _structured_metric(verified, "finalization_verification")
    tool_success_rate = (
        run_stats["successful_tool_call_count"] / run_stats["tool_call_count"] * 100.0
        if run_stats["tool_call_count"]
        else None
    )
    _integrity_banner(
        run_verified=bool(finalization.get("verified_for_comparison")),
        cross_check_passed=bool(cross_check.get("passed")),
        run_id=run_id,
    )
    cards = st.columns(4)
    cards[0].metric("Decisions", run_stats["decision_count"])
    cards[1].metric("Tool calls", run_stats["tool_call_count"])
    cards[2].metric(
        "Tool success",
        _format_value(tool_success_rate, "%"),
    )
    cards[3].metric(
        "P95 decision latency",
        _format_duration_ms(run_stats["p95_decision_latency_ms"]),
    )
    recovery_cards = st.columns(5)
    recovery_cards[0].metric("Timeouts", timeout_count)
    recovery_cards[1].metric("Fallbacks", fallback_count)
    recovery_cards[2].metric("Invalid actions", invalid_count)
    recovery_cards[3].metric("Safety clamps", clamp_count)
    recovery_cards[4].metric(
        "Failed tools",
        int((tools["success"] == 0).sum()) if not tools.empty else 0,
    )

    evidence_cards = st.columns(4)
    evidence_cards[0].metric(
        "Physical application rate",
        _format_value(run_stats["action_application_percent"], "%"),
    )
    evidence_cards[1].metric(
        "Energy cross-check",
        (
            "PASS"
            if cross_check.get("passed") is True
            else "FAIL"
            if cross_check.get("passed") is False
            else "Unavailable"
        ),
    )
    evidence_cards[2].metric(
        "Cross-check difference",
        _format_value(cross_check.get("difference_percent"), "%"),
    )
    evidence_cards[3].metric(
        "Verified final metrics",
        "YES" if finalization.get("verified_for_comparison") else "NO",
    )

    _subsection_header(
        "Diagnostics",
        "EnergyPlus engine messages and controller diagnostics are counted independently.",
    )
    if energyplus_diagnostics.empty:
        st.success("No EnergyPlus warning, severe or fatal diagnostics are recorded.")
    else:
        severe_count = int(
            energyplus_diagnostics["severity"].str.casefold().isin({"severe", "fatal"}).sum()
        )
        if severe_count:
            st.error(
                f"EnergyPlus recorded {severe_count} severe or fatal diagnostics; "
                "comparison claims are suppressed for failed runs."
            )
        else:
            st.warning(f"EnergyPlus recorded {len(energyplus_diagnostics)} warning diagnostics.")
    if not controller_diagnostics.empty:
        occurrences = int(
            controller_diagnostics["occurrence_count"].fillna(1).sum()
            if "occurrence_count" in controller_diagnostics
            else len(controller_diagnostics)
        )
        st.warning(
            f"{len(controller_diagnostics)} controller diagnostic records "
            f"({occurrences} occurrences) are preserved separately from EnergyPlus "
            "diagnostics and require review."
        )
    if not errors.empty:
        severity_counts = (
            errors.assign(
                diagnostic_source=errors["source"].map(
                    lambda value: (
                        "EnergyPlus" if str(value) == "energyplus-message" else "Controller"
                    )
                )
            )
            .groupby(["diagnostic_source", "severity"])
            .size()
            .reset_index(name="count")
        )
        severity_figure = px.bar(
            severity_counts,
            x="severity",
            y="count",
            color="diagnostic_source",
            barmode="group",
            color_discrete_map={
                "EnergyPlus": "#168aad",
                "Controller": "#d97706",
            },
        )
        _style_chart(
            severity_figure,
            title="Recorded diagnostics by source",
            yaxis_title="Deduplicated records",
        )
        st.plotly_chart(severity_figure, width="stretch")
        with st.expander("Diagnostic log records"):
            st.dataframe(errors, hide_index=True, width="stretch")


def _methodology() -> None:
    _section_header(
        "Methodology",
        eyebrow="HOW TO READ THE EVIDENCE",
        description=(
            "The controls, compatibility gates and result sources behind every "
            "number shown in this dashboard."
        ),
    )
    cards = st.columns(4)
    cards[0].metric("Zone timestep", "15 min")
    cards[1].metric("Normal decision cadence", "60 min")
    cards[2].metric("Maximum action hold", "120 min")
    cards[3].metric("Inference", "Local only")
    st.markdown(
        """
        <div class="method-grid">
          <article>
            <span>01</span>
            <strong>Observe</strong>
            <p>EnergyPlus callbacks persist facility and zone telemetry once per zone timestep.</p>
          </article>
          <article>
            <span>02</span>
            <strong>Decide</strong>
            <p>The local model uses narrowly scoped MCP tools and bounded candidate actions.</p>
          </article>
          <article>
            <span>03</span>
            <strong>Validate</strong>
            <p>A deterministic layer checks freshness, capability, deadband, rate and expiry.</p>
          </article>
          <article>
            <span>04</span>
            <strong>Verify</strong>
            <p>
              Official EnergyPlus totals are cross-checked against the persisted
              telemetry trail.
            </p>
          </article>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _outcome_banner(
        tone="neutral",
        title="Conservative publication gate",
        detail=(
            "Failed, incomplete, mismatched or test-fixture runs cannot produce a comparison claim."
        ),
    )
    path = repository_root() / "docs" / "methodology.md"
    if path.is_file():
        with st.expander("Read the complete evaluation methodology", expanded=False):
            st.markdown(path.read_text(encoding="utf-8"))
    else:
        st.info("Methodology document is unavailable.")


def _section_header(title: str, *, eyebrow: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="section-heading">
          <span>{escape(eyebrow)}</span>
          <h2>{escape(title)}</h2>
          <p>{escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _subsection_header(title: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="subsection-heading">
          <h3>{escape(title)}</h3>
          <p>{escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _telemetry_banner(
    *,
    run_status: str,
    simulation_clock: str,
    recorded_at: Any,
    row_count: int,
    replay: bool,
) -> None:
    if replay:
        tone = "replay"
        label = "REAL-RUN REPLAY"
        detail = f"Showing persisted frame {row_count:,} · simulated {simulation_clock}"
    elif run_status.casefold() == "running":
        tone = "live"
        label = "LIVE DATA INGEST"
        detail = (
            f"Latest persisted sample · simulated {simulation_clock} · "
            f"recorded {_timestamp_text(recorded_at)}"
        )
    elif run_status.casefold() == "completed":
        tone = "complete"
        label = "COMPLETED RUN"
        detail = f"{row_count:,} persisted facility samples · final frame {simulation_clock}"
    else:
        tone = "neutral"
        label = run_status.upper()
        detail = f"{row_count:,} persisted facility samples"
    st.markdown(
        f"""
        <div class="telemetry-banner banner-{escape(tone)}">
          <div><span class="mode-signal"></span><strong>{escape(label)}</strong></div>
          <p>{escape(detail)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _timestamp_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return "time unavailable"
    timestamp = pd.Timestamp(value)
    return str(timestamp.strftime("%H:%M:%S UTC"))


def _comparison_provenance(
    *,
    baseline_run: dict[str, Any] | None,
    controlled_run: dict[str, Any] | None,
) -> None:
    if baseline_run is None or controlled_run is None:
        return
    period = str(controlled_run.get("period_name") or "unspecified").title()
    version = str(controlled_run.get("energyplus_version") or "unknown")
    weather = Path(str(controlled_run.get("weather_path") or "weather unavailable")).name
    st.markdown(
        f"""
        <div class="pair-strip">
          <div>
            <span>REFERENCE</span>
            <strong>{escape(str(baseline_run["run_id"]))}</strong>
          </div>
          <div class="pair-arrow">→</div>
          <div>
            <span>CONTROLLED</span>
            <strong>{escape(str(controlled_run["run_id"]))}</strong>
          </div>
          <div class="pair-meta">
            <span>{escape(period)}</span>
            <span>EnergyPlus {escape(version)}</span>
            <span>{escape(weather)}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _outcome_banner(*, tone: str, title: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="outcome-banner outcome-{escape(tone)}">
          <span>{escape(title)}</span>
          <p>{escape(detail)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _integrity_banner(
    *,
    run_verified: bool,
    cross_check_passed: bool,
    run_id: str,
) -> None:
    passed = run_verified and cross_check_passed
    tone = "positive" if passed else "caution"
    title = "Publication checks passed" if passed else "Publication checks incomplete"
    detail = (
        "Final metrics are verified and official EnergyPlus electricity agrees with "
        "the Runtime API telemetry cross-check."
        if passed
        else "One or more final verification gates are unavailable for this run."
    )
    _outcome_banner(
        tone=tone,
        title=title,
        detail=f"{detail} Run {run_id}.",
    )


def _select_simulated_window(frame: pd.DataFrame, selection: str) -> pd.DataFrame:
    if frame.empty or selection == "Full run":
        return frame
    hours = 24 if selection == "Last 24 simulated hours" else 72
    latest = frame["simulation_timestamp"].max()
    return frame[frame["simulation_timestamp"] >= latest - pd.Timedelta(hours=hours)]


def _comparison_scorecard(
    baseline: dict[str, dict[str, Any]],
    controlled: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    definitions = (
        ("facility_electricity_kwh", "Facility electricity", "Lower"),
        ("hvac_electricity_kwh", "HVAC electricity", "Lower"),
        ("peak_electrical_demand_kw", "Peak electrical demand", "Lower"),
        ("cost", "Operating cost", "Lower"),
        ("operational_carbon_kg", "Operational carbon", "Lower"),
        (
            "occupied_temperature_violation_percent",
            "Occupied temperature violation",
            "Lower",
        ),
        (
            "occupied_temperature_violation_degree_hours",
            "Violation degree-hours",
            "Lower",
        ),
        ("pmv_compliance_percent", "PMV compliance", "Higher"),
        ("mean_ppd_percent", "Mean PPD", "Lower"),
    )
    rows: list[dict[str, Any]] = []
    for metric_name, label, direction in definitions:
        baseline_value = _metric_value(baseline, metric_name)
        controlled_value = _metric_value(controlled, metric_name)
        if baseline_value is None and controlled_value is None:
            continue
        units = str(
            controlled.get(metric_name, {}).get("units")
            or baseline.get(metric_name, {}).get("units")
            or ""
        )
        rows.append(
            {
                "Metric": label,
                "Unit": units,
                "Baseline": baseline_value,
                "Controlled": controlled_value,
                "Absolute change": (
                    controlled_value - baseline_value
                    if baseline_value is not None and controlled_value is not None
                    else None
                ),
                "Relative change": _percent_change(baseline_value, controlled_value),
                "Preferred direction": direction,
            }
        )
    return pd.DataFrame(rows)


def _zone_summary(zones: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for zone_name, frame in zones.groupby("zone_name", sort=True):
        occupied = frame["occupant_count"].fillna(0) > 0
        rows.append(
            {
                "Zone": zone_name,
                "Samples": len(frame),
                "Occupied samples": int(occupied.sum()),
                "Mean operative °C": frame["operative_temperature_c"].mean(),
                "P05 operative °C": frame["operative_temperature_c"].quantile(0.05),
                "P95 operative °C": frame["operative_temperature_c"].quantile(0.95),
                "Mean PPD %": frame["ppd_percent"].mean(),
                "Max |PMV|": frame["pmv"].abs().max(),
                "Mean RH %": frame["relative_humidity_percent"].mean(),
                "Max CO₂ ppm": frame["co2_ppm"].max(),
            }
        )
    return pd.DataFrame(rows)


def _structured_metric(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    value = metrics.get(metric_name, {}).get("structured_value")
    return value if isinstance(value, dict) else {}


def _simulation_clock(value: Any) -> str:
    if value is None or pd.isna(value):
        return "Waiting for telemetry"
    timestamp = pd.Timestamp(value)
    return str(timestamp.strftime("%b %d · %H:%M"))


def _style_chart(
    figure: go.Figure,
    *,
    title: str,
    yaxis_title: str,
    height: int = 340,
) -> None:
    figure.update_layout(
        title={"text": title, "font": {"size": 18, "color": "#173b32"}},
        yaxis_title=yaxis_title,
        xaxis_title=None,
        template="plotly_white",
        height=height,
        margin={"l": 24, "r": 18, "t": 76, "b": 24},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        font={"family": "Inter, Segoe UI, sans-serif", "color": "#35554b"},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.08,
            "xanchor": "left",
            "x": 0,
        },
    )
    figure.update_xaxes(showgrid=False, linecolor="#dce8e2")
    figure.update_yaxes(gridcolor="#edf3ef", zeroline=False, linecolor="#dce8e2")


def _action_card(*, title: str, values: Any, accent: str) -> str:
    return (
        f"<div class='action-card action-{escape(accent)}'>"
        f"<span>{escape(title.upper())}</span>"
        f"<strong>{_action_values_text(values)}</strong>"
        "</div>"
    )


def _action_values_text(value: Any) -> str:
    parsed: Any = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return escape(value or "Unavailable")
    if not isinstance(parsed, dict):
        return escape(str(parsed or "Unavailable"))
    labels = {
        "heating_setpoint_c": ("Heat", "°C"),
        "cooling_setpoint_c": ("Cool", "°C"),
        "hold_minutes": ("Hold", "min"),
        "hold_duration_minutes": ("Hold", "min"),
    }
    parts: list[str] = []
    for key, item in parsed.items():
        label, unit = labels.get(key, (key.replace("_", " ").title(), ""))
        formatted = f"{float(item):.1f}" if isinstance(item, (int, float)) else str(item)
        parts.append(f"{label} {formatted}{unit}")
    return escape(" · ".join(parts) if parts else "Unavailable")


def _percent_change(baseline: float | None, controlled: float | None) -> float | None:
    if baseline is None or controlled is None or baseline == 0:
        return None
    return (controlled - baseline) / baseline * 100


def _comparison_delta(
    baseline: float | None,
    controlled: float | None,
    suffix: str,
) -> str | None:
    if baseline is None or controlled is None:
        return None
    difference = controlled - baseline
    return f"{difference:+,.2f}{suffix} vs baseline"


def _points_delta(baseline: float | None, controlled: float | None) -> str | None:
    if baseline is None or controlled is None:
        return None
    return f"{controlled - baseline:+,.2f} points"


def _range_text(low: float | None, high: float | None, suffix: str) -> str | None:
    if low is None or high is None or pd.isna(low) or pd.isna(high):
        return None
    return f"{low:,.1f}-{high:,.1f}{suffix} zone range"


def _rate_text(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "No proposals"
    return f"{numerator / denominator * 100.0:.1f}% of proposals"


def _signed_percent(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    return f"{value:+.2f}%"


def _is_active_fallback(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    return str(value).strip().casefold() not in {"", "none", "inactive", "false"}


def _has_clamp(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    text = str(value).strip()
    if text.casefold() in {"", "[]", "{}", "null", "none"}:
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return True
    return bool(parsed)


def _metric_value(metrics: dict[str, dict[str, Any]], name: str) -> float | None:
    item = metrics.get(name)
    if not item or item.get("value") is None:
        return None
    return float(item["value"])


def _verified_count(
    metrics: dict[str, dict[str, Any]],
    name: str,
    fallback: int,
) -> int:
    value = _metric_value(metrics, name)
    return fallback if value is None else int(value)


def _format_duration_ms(value: Any) -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    milliseconds = float(value)
    if abs(milliseconds) >= 1_000:
        return f"{milliseconds / 1_000:,.2f} s"
    return f"{milliseconds:,.2f} ms"


def _preferred_controlled_index(controlled: pd.DataFrame) -> int:
    type_rank = {"agent": 0, "rule": 1, "replay": 2, "fixed_override": 3}
    period_rank = {"evaluation": 0, "demo": 1, "smoke": 2}
    ranked: list[tuple[int, int, int]] = []
    for index, row in controlled.reset_index(drop=True).iterrows():
        run_type = str(row.get("run_type") or "")
        period = str(row.get("period_name") or "")
        ranked.append(
            (
                type_rank.get(run_type, len(type_rank)),
                period_rank.get(period, 1),
                int(index),
            )
        )
    return min(ranked)[2] if ranked else 0


def _diagnostic_groups(errors: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if errors.empty:
        return errors.copy(), errors.copy()
    actionable = errors[
        errors["severity"].astype(str).str.casefold().isin({"warning", "severe", "fatal"})
    ]
    energyplus = actionable[actionable["source"] == "energyplus-message"]
    controller = actionable[actionable["source"] != "energyplus-message"]
    return energyplus, controller


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
        :root {
            --eco-ink: #12362d;
            --eco-muted: #5e766e;
            --eco-green: #0d766e;
            --eco-lime: #a3e635;
            --eco-line: #dce8e2;
            --eco-canvas: #f3f7f4;
        }
        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(circle at 88% 4%, rgba(163, 230, 53, .11), transparent 25rem),
                linear-gradient(180deg, #f7faf8 0%, var(--eco-canvas) 100%);
        }
        [data-testid="stSidebar"] {
            background: rgba(255, 255, 255, .96);
            border-right: 1px solid var(--eco-line);
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: var(--eco-muted);
        }
        .block-container {
            max-width: 1500px;
            padding-top: 1.5rem;
            padding-bottom: 2.5rem;
        }
        .ecoloop-hero {
            position: relative;
            overflow: hidden;
            padding: 2rem 2.25rem 1.8rem;
            margin-bottom: 1rem;
            border: 1px solid rgba(255, 255, 255, .12);
            border-radius: 24px;
            background:
                radial-gradient(circle at 88% 20%, rgba(163, 230, 53, .26), transparent 20rem),
                linear-gradient(120deg, #0c3028 0%, #0d5b52 58%, #0a756a 100%);
            box-shadow: 0 24px 70px rgba(10, 56, 47, .18);
        }
        .ecoloop-hero::after {
            content: "";
            position: absolute;
            right: -3rem;
            bottom: -8rem;
            width: 22rem;
            height: 22rem;
            border: 1px solid rgba(255, 255, 255, .15);
            border-radius: 50%;
            box-shadow:
                0 0 0 3rem rgba(255, 255, 255, .035),
                0 0 0 6rem rgba(255, 255, 255, .025);
        }
        .ecoloop-eyebrow {
            position: relative;
            z-index: 1;
            color: #c6f68d;
            font-size: .74rem;
            font-weight: 800;
            letter-spacing: .16em;
        }
        .ecoloop-hero h1 {
            position: relative;
            z-index: 1;
            margin: .42rem 0 .32rem;
            color: #ffffff !important;
            font-size: clamp(2.1rem, 4vw, 3.45rem);
            line-height: 1.03;
            letter-spacing: -.045em;
        }
        .ecoloop-hero p {
            position: relative;
            z-index: 1;
            max-width: 760px;
            margin: 0;
            color: #d9eee7;
            font-size: 1.05rem;
        }
        .ecoloop-hero-tags {
            position: relative;
            z-index: 1;
            display: flex;
            flex-wrap: wrap;
            gap: .55rem;
            margin-top: 1.25rem;
        }
        .ecoloop-hero-tags span {
            padding: .32rem .62rem;
            color: #e7f7f1;
            border: 1px solid rgba(255, 255, 255, .22);
            border-radius: 999px;
            background: rgba(255, 255, 255, .08);
            font-size: .67rem;
            font-weight: 750;
            letter-spacing: .07em;
        }
        .ecoloop-run-strip {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: .55rem 0 .7rem;
            padding: .9rem 1.05rem;
            border: 1px solid var(--eco-line);
            border-radius: 16px;
            background: rgba(255, 255, 255, .86);
            box-shadow: 0 8px 30px rgba(29, 71, 59, .06);
        }
        .ecoloop-run-strip > div {
            display: flex;
            align-items: center;
            gap: .8rem;
            min-width: 0;
        }
        .ecoloop-run-strip code {
            overflow: hidden;
            color: var(--eco-muted);
            text-overflow: ellipsis;
            white-space: nowrap;
            background: transparent;
        }
        .run-label {
            color: var(--eco-muted);
            font-size: .66rem;
            font-weight: 800;
            letter-spacing: .1em;
        }
        .run-status {
            flex: 0 0 auto;
            padding: .35rem .65rem;
            border-radius: 999px;
            font-size: .69rem;
            font-weight: 850;
            letter-spacing: .08em;
        }
        .status-completed { color: #0b6b43; background: #dff8e9; }
        .status-running { color: #0b6280; background: #dff4fb; }
        .status-failed { color: #991b1b; background: #fee2e2; }
        .status-neutral { color: #4b5563; background: #e5e7eb; }
        [data-testid="stMetric"] {
            min-height: 112px;
            padding: .95rem 1rem;
            border: 1px solid var(--eco-line);
            border-radius: 16px;
            background: rgba(255, 255, 255, .94);
            box-shadow: 0 8px 24px rgba(29, 71, 59, .055);
        }
        [data-testid="stMetricLabel"] {
            color: var(--eco-muted);
            font-weight: 650;
        }
        [data-testid="stMetricValue"] {
            color: var(--eco-ink);
            font-weight: 780;
            letter-spacing: -.025em;
        }
        [data-testid="stTabs"] [role="tablist"] {
            gap: .35rem;
            padding: .3rem;
            border: 1px solid var(--eco-line);
            border-radius: 14px;
            background: rgba(255, 255, 255, .86);
        }
        [data-testid="stTabs"] [role="tab"] {
            height: 2.6rem;
            padding: 0 .85rem;
            border-radius: 10px;
        }
        [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
            color: #ffffff !important;
            background: var(--eco-green) !important;
        }
        [data-testid="stTabs"] [role="tab"][aria-selected="true"] p {
            color: #ffffff !important;
        }
        [data-testid="stTabs"] .react-aria-SelectionIndicator {
            display: none;
        }
        [data-testid="stSidebar"] .stButton > button {
            color: #ffffff !important;
            border-color: var(--eco-green) !important;
            background: var(--eco-green) !important;
        }
        [data-testid="stSidebar"] .stButton > button p {
            color: #ffffff !important;
        }
        [data-baseweb="tag"] {
            background-color: var(--eco-green) !important;
        }
        [data-testid="stDataFrame"] {
            overflow: hidden;
            border: 1px solid var(--eco-line);
            border-radius: 14px;
            background: #ffffff;
        }
        .action-card {
            margin-bottom: .65rem;
            padding: .85rem 1rem;
            border: 1px solid var(--eco-line);
            border-left-width: 5px;
            border-radius: 12px;
            background: #ffffff;
        }
        .action-card span,
        .decision-kicker {
            display: block;
            margin-bottom: .25rem;
            color: var(--eco-muted);
            font-size: .66rem;
            font-weight: 800;
            letter-spacing: .09em;
        }
        .action-card strong {
            color: var(--eco-ink);
            font-size: 1rem;
        }
        .action-proposed { border-left-color: #168aad; }
        .action-applied { border-left-color: #12b981; }
        .decision-summary {
            margin: .85rem 0 1rem;
            padding: 1.05rem 1.2rem;
            border: 1px solid #cfe9de;
            border-radius: 16px;
            background: linear-gradient(135deg, #f4fcf8, #ffffff);
        }
        .decision-summary strong {
            display: block;
            color: var(--eco-ink);
            font-size: 1.2rem;
        }
        .decision-summary p {
            margin: .35rem 0;
            color: #365b50;
        }
        .decision-summary small { color: var(--eco-muted); }
        .sidebar-run-card {
            display: grid;
            gap: .18rem;
            margin: .6rem 0 1rem;
            padding: .85rem;
            border: 1px solid var(--eco-line);
            border-radius: 14px;
            background: linear-gradient(145deg, #f8fcfa, #edf7f2);
        }
        .sidebar-run-card span,
        .sidebar-run-card small {
            color: var(--eco-muted);
            font-size: .7rem;
            letter-spacing: .05em;
        }
        .sidebar-run-card strong { color: var(--eco-ink); }
        .ecoloop-footer {
            margin-top: 2.5rem;
            padding-top: 1rem;
            border-top: 1px solid var(--eco-line);
            color: var(--eco-muted);
            font-size: .78rem;
            text-align: center;
        }
        h1, h2, h3, h4 { color: var(--eco-ink); }
        h3 { margin-top: 1rem; letter-spacing: -.025em; }
        [data-testid="stAlert"] { border-radius: 14px; }
        .stButton > button {
            border-radius: 10px;
            font-weight: 700;
        }
        @media (max-width: 900px) {
            .ecoloop-hero { padding: 1.5rem; border-radius: 18px; }
            .ecoloop-run-strip,
            .ecoloop-run-strip > div { align-items: flex-start; flex-direction: column; }
            .ecoloop-run-strip code { max-width: 78vw; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(f"<style>{DASHBOARD_CSS}</style>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
