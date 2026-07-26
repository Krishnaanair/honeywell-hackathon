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


def main() -> None:
    """Render the six-tab EcoLoop operational dashboard."""

    st.set_page_config(
        page_title="EcoLoop Building Agents",
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

    _brand_header()
    if replay_enabled:
        st.warning(
            f"REAL RUN REPLAY - source run `{replay_run_id or 'not selected'}`. "
            "Display speed changes wall-clock playback only."
        )

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

    _run_context(run)
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
        "EnergyPlus 26.1.0 &nbsp;•&nbsp; Local inference &nbsp;•&nbsp; "
        "Audited MCP control &nbsp;•&nbsp; SQLite evidence bus"
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


def _brand_header() -> None:
    st.markdown(
        """
        <section class="ecoloop-hero">
          <div class="ecoloop-eyebrow">GUARDRAILED BUILDING INTELLIGENCE</div>
          <h1>EcoLoop Building Agents</h1>
          <p>
            A live EnergyPlus supervisory controller with local inference,
            deterministic safety and audit-ready evidence.
          </p>
          <div class="ecoloop-hero-tags">
            <span>ENERGYPLUS 26.1</span>
            <span>LOCAL MODEL</span>
            <span>MCP TOOL LOOP</span>
            <span>REAL TELEMETRY</span>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _run_context(run: dict[str, Any]) -> None:
    run_id = escape(str(run["run_id"]))
    status = escape(str(run.get("status") or "unknown"))
    run_type = escape(str(run.get("run_type") or "unknown"))
    period = escape(str(run.get("period_name") or "unspecified"))
    status_class = {
        "completed": "status-completed",
        "running": "status-running",
        "failed": "status-failed",
    }.get(status.casefold(), "status-neutral")
    st.markdown(
        f"""
        <div class="ecoloop-run-strip">
          <div>
            <span class="run-label">SELECTED EVIDENCE RUN</span>
            <strong>{run_type.upper()} · {period.upper()}</strong>
            <code>{run_id}</code>
          </div>
          <span class="run-status {status_class}">{status.upper()}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    progress = float(run.get("progress_percent") or 0.0)
    if status.casefold() == "completed":
        progress = 100.0
    st.progress(
        min(max(progress, 0.0), 100.0) / 100.0,
        text=f"Simulation progress · {progress:.0f}%",
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
    st.sidebar.button("Refresh live data", width="stretch", type="primary")
    st.sidebar.caption(f"EnergyPlus {escape(str(run.get('energyplus_version') or 'unknown'))}")


def _select_run(runs: pd.DataFrame, forced: str | None) -> str:
    st.sidebar.markdown("### Operations console")
    st.sidebar.caption("Production view · fake test runs hidden")
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
    st.sidebar.markdown("#### Evidence source")
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
    st.markdown("### Live Operations")
    st.caption("The latest physical state, active setpoints and control-path evidence.")
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
    if not latest.empty:
        row = latest.iloc[0]
        simulation_clock = _simulation_clock(row["simulation_timestamp"])
        demand = row.get("facility_demand_kw")
        heat_sp = row.get("heating_setpoint_c")
        cool_sp = row.get("cooling_setpoint_c")
        cumulative_energy = row.get("cumulative_electricity_kwh")
        outdoor = row.get("outdoor_temperature_c")
    occupancy = latest_zones["occupant_count"].sum() if not latest_zones.empty else None
    temp = latest_zones["operative_temperature_c"].mean() if not latest_zones.empty else None
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

    columns = st.columns(4)
    columns[0].metric("Simulation clock", simulation_clock)
    columns[1].metric("Progress", _format_value(run.get("progress_percent"), "%"))
    columns[2].metric("Operative temperature", _format_value(temp, " °C"))
    columns[3].metric("Facility demand", _format_value(demand, " kW"))

    status_cols = st.columns(4)
    status_cols[0].metric("Occupancy", _format_value(occupancy, " people"))
    status_cols[1].metric("Heating setpoint", _format_value(heat_sp, " °C"))
    status_cols[2].metric("Cooling setpoint", _format_value(cool_sp, " °C"))
    status_cols[3].metric("Outdoor air", _format_value(outdoor, " °C"))

    audit_cols = st.columns(2)
    audit_cols[0].metric(
        "Cumulative electricity",
        _format_value(cumulative_energy, " kWh"),
    )
    audit_cols[1].metric("Decision latency", _format_duration_ms(latency))

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
        live_frame = telemetry.copy()
        if not zones.empty:
            mean_zone = (
                zones.groupby("simulation_timestamp", as_index=False)["operative_temperature_c"]
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
            x=telemetry["simulation_timestamp"],
            y=telemetry["facility_demand_kw"],
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

    left, right = st.columns(2)
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
    st.markdown("### Baseline vs Agent")
    st.caption(
        "Official EnergyPlus totals from compatible completed real runs. "
        "Incomplete and fake runs are excluded."
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
    electricity_change = (
        (controlled_kwh - baseline_kwh) / baseline_kwh * 100
        if baseline_kwh is not None and baseline_kwh > 0 and controlled_kwh is not None
        else None
    )
    if electricity_change is not None and electricity_change <= 0:
        st.success(
            f"Measured facility electricity saving: {abs(electricity_change):.2f}% "
            "for the selected period."
        )
    elif electricity_change is not None:
        st.warning(
            f"Measured facility electricity increase: {electricity_change:.2f}% "
            "for the selected period. This is reported without adjustment."
        )

    peak_baseline = _metric_value(baseline_metrics, "peak_electrical_demand_kw")
    peak_controlled = _metric_value(controlled_metrics, "peak_electrical_demand_kw")
    hvac_baseline = _metric_value(baseline_metrics, "hvac_electricity_kwh")
    hvac_controlled = _metric_value(controlled_metrics, "hvac_electricity_kwh")
    peak_change = _percent_change(peak_baseline, peak_controlled)
    hvac_change = _percent_change(hvac_baseline, hvac_controlled)
    cards = st.columns(3)
    cards[0].metric("Baseline electricity", _format_value(baseline_kwh, " kWh"))
    cards[1].metric("Controlled electricity", _format_value(controlled_kwh, " kWh"))
    cards[2].metric(
        "Electricity change",
        _signed_percent(electricity_change),
        delta="lower is better",
        delta_color="off",
    )
    secondary_cards = st.columns(3)
    secondary_cards[0].metric(
        "Peak demand",
        _format_value(peak_controlled, " kW"),
        delta=_signed_percent(peak_change),
        delta_color="inverse",
    )
    secondary_cards[1].metric(
        "HVAC electricity",
        _format_value(hvac_controlled, " kWh"),
        delta=_signed_percent(hvac_change),
        delta_color="inverse",
    )
    secondary_cards[2].metric(
        "Temp. violation",
        _format_value(
            _metric_value(controlled_metrics, "occupied_temperature_violation_percent"),
            "%",
        ),
        delta=_delta_text(
            _metric_value(baseline_metrics, "occupied_temperature_violation_percent"),
            _metric_value(controlled_metrics, "occupied_temperature_violation_percent"),
        ),
        delta_color="inverse",
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
            delta=_delta_text(base, controlled_value),
            delta_color="inverse" if metric_name != "pmv_compliance_percent" else "normal",
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

    comfort_rows = []
    for label, metrics in (("Baseline", baseline_metrics), ("Controlled", controlled_metrics)):
        comfort_rows.append(
            {
                "case": label,
                "occupied temperature violation %": _metric_value(
                    metrics, "occupied_temperature_violation_percent"
                ),
                "violation degree-hours": _metric_value(
                    metrics, "occupied_temperature_violation_degree_hours"
                ),
                "PMV compliance %": _metric_value(metrics, "pmv_compliance_percent"),
                "mean PPD %": _metric_value(metrics, "mean_ppd_percent"),
            }
        )
    st.dataframe(pd.DataFrame(comfort_rows), hide_index=True, width="stretch")


def _comfort(database_path: Path, run_id: str, limit: int | None) -> None:
    st.markdown("### Comfort and IAQ")
    st.caption("Zone-level thermal comfort evidence from the EnergyPlus physical model.")
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
    comfort_cols = st.columns(4)
    comfort_cols[0].metric("Mean PPD", _format_value(zones["ppd_percent"].mean(), "%"))
    comfort_cols[1].metric("Max |PMV|", _format_value(zones["pmv"].abs().max()))
    comfort_cols[2].metric(
        "Mean relative humidity",
        _format_value(zones["relative_humidity_percent"].mean(), "%"),
    )
    comfort_cols[3].metric("Max CO₂", _format_value(zones["co2_ppm"].max(), " ppm"))
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
            "CO₂ is unavailable because this source model does not expose a verified "
            "contaminant-simulation point. No value is fabricated."
        )


def _decisions(database_path: Path, run_id: str) -> None:
    st.markdown("### Agent Decisions")
    st.caption(
        "Operational explanations, evaluated candidates and proposed-versus-applied actions. "
        "Private reasoning is never requested or stored."
    )
    decisions = queries.recent_decisions(database_path, run_id, limit=200)
    actions = queries.recent_actions(database_path, run_id, limit=200)
    tools = queries.recent_tool_calls(database_path, run_id, limit=500)
    if decisions.empty:
        st.info("No decision records exist for this run.")
    else:
        decision_cards = st.columns(5)
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
            "Fallbacks",
            int(decisions["fallback_status"].map(_is_active_fallback).sum()),
        )
        decision_cards[4].metric(
            "Incomplete",
            int((decisions["completed"] == 0).sum()),
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
        with st.expander("Decision history and candidate score components", expanded=True):
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
                width="stretch",
            )
    st.markdown("#### Proposed versus safety-applied actions")
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
            width="stretch",
        )
    st.markdown("#### MCP tools called")
    if tools.empty:
        st.caption("No tool-call trace exists for this run.")
    else:
        st.dataframe(
            tools[
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
    st.markdown("### Reliability and Errors")
    st.caption("Timeouts, fallbacks, safety intervention and EnergyPlus diagnostics.")
    tools = queries.recent_tool_calls(database_path, run_id, limit=10_000)
    decisions = queries.recent_decisions(database_path, run_id, limit=10_000)
    actions = queries.recent_actions(database_path, run_id, limit=10_000)
    errors = queries.errors_and_messages(database_path, run_id, limit=500)
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
    cards = st.columns(4)
    cards[0].metric("Decisions", len(decisions))
    cards[1].metric("Tool calls", len(tools))
    cards[2].metric(
        "Mean tool latency",
        _format_value(tools["duration_ms"].mean() if not tools.empty else None, " ms"),
    )
    cards[3].metric(
        "P95 decision latency",
        _format_duration_ms(
            decisions["latency_ms"].quantile(0.95) if not decisions.empty else None
        ),
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
    st.markdown("### Methodology")
    st.caption("How the comparison stays reproducible, conservative and auditable.")
    cards = st.columns(4)
    cards[0].metric("Zone timestep", "15 min")
    cards[1].metric("Normal decision cadence", "60 min")
    cards[2].metric("Maximum action hold", "120 min")
    cards[3].metric("Inference", "Local only")
    st.info(
        "Energy totals come from final EnergyPlus outputs and are independently "
        "cross-checked against Runtime API telemetry. Failed or incomplete runs never "
        "produce savings claims."
    )
    path = repository_root() / "docs" / "methodology.md"
    if path.is_file():
        st.markdown(path.read_text(encoding="utf-8"))
    else:
        st.info("Methodology document is unavailable.")


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
) -> None:
    figure.update_layout(
        title={"text": title, "font": {"size": 18, "color": "#173b32"}},
        yaxis_title=yaxis_title,
        xaxis_title=None,
        template="plotly_white",
        height=340,
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
    priorities = ("agent", "rule", "replay", "fixed_override")
    run_types = controlled["run_type"].astype(str).tolist()
    for priority in priorities:
        for index, run_type in enumerate(run_types):
            if run_type == priority:
                return index
    return 0


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


if __name__ == "__main__":
    main()
