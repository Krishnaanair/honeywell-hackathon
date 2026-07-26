"""Streamlit command-center dashboard reading only the durable run database."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ecoloop.config import get_settings
from ecoloop.dashboard import queries
from ecoloop.dashboard.queries import DashboardDataError, RunStatistics


def main() -> None:
    """Render the EcoLoop dark operations command center."""

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
    if replay_enabled and not replay_run_id:
        _disconnected_header("Replay mode is enabled without a source run.")
        st.error(
            "Replay mode requires ECOLOOP_DEMO_REPLAY_RUN_ID to identify a "
            "completed, verified real controlled run."
        )
        return

    try:
        runs = queries.list_runs(database_path, include_fake=False)
    except DashboardDataError as exc:
        _disconnected_header("The run database is unavailable.")
        st.info(str(exc))
        st.code("python -m ecoloop doctor\npython -m ecoloop run baseline --period smoke")
        return
    except Exception as exc:  # dashboard process boundary
        _disconnected_header("The run database could not be read.")
        st.error(f"Could not query the run database: {exc}")
        return

    if runs.empty:
        _disconnected_header("No completed or running real run exists yet.")
        st.info("No real runs exist. Fake test runs are intentionally hidden.")
        return

    run_id = _select_run(
        runs,
        replay_run_id if replay_enabled else None,
        preferred=_read_current_run_id(settings.resolved_runs_dir()),
    )
    run = queries.get_run(database_path, run_id)
    if run is None:
        _disconnected_header("The selected run record is missing.")
        st.error(f"Run disappeared: {run_id}")
        return
    verified = queries.verified_metrics(database_path, run_id)
    verified_evidence = _has_verified_evidence(verified)
    if replay_enabled and not (_is_completed_controlled_run(run) and verified_evidence):
        _disconnected_header("The replay source failed the verification gate.")
        st.error(
            "Replay is restricted to a completed, verified real controlled run "
            "with a passing official-energy cross-check."
        )
        return

    _sidebar_run_panel(run)
    frame_limit = _replay_controls(database_path, run_id) if replay_enabled else None

    _command_center(
        database_path,
        run,
        frame_limit,
        replay_enabled=replay_enabled,
        verified_evidence=verified_evidence,
    )
    _performance_section(database_path, runs)
    _comfort_section(database_path, run_id, frame_limit)
    _pipeline_section(database_path, run_id)

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


@st.fragment(run_every=3.0)
def _command_center(
    database_path: Path,
    run: dict[str, Any],
    limit: int | None,
    *,
    replay_enabled: bool,
    verified_evidence: bool,
) -> None:
    """Render the live panels: header, simulation, zones, agent, MCP, console."""

    refreshed_run = queries.get_run(database_path, str(run["run_id"]))
    if refreshed_run is not None:
        run = refreshed_run
    run_id = str(run["run_id"])
    run_type = str(run.get("run_type") or "").casefold()
    run_stats = queries.run_statistics(database_path, run_id)
    telemetry = queries.telemetry(database_path, run_id, limit=limit)
    zones = queries.zone_telemetry(database_path, run_id)
    if limit is not None and not telemetry.empty:
        zones = zones[zones["simulation_timestamp"] <= telemetry["simulation_timestamp"].max()]
    decisions = queries.recent_decisions(database_path, run_id, limit=8)
    actions = queries.action_log(database_path, run_id, limit=12)
    latest_apply = queries.latest_tool_call(
        database_path,
        run_id,
        tool_name="apply_control_action",
    )
    console = queries.system_console_events(database_path, run_id, limit=30)
    message_counts = queries.simulation_message_counts(database_path, run_id)

    _command_header(
        run,
        run_stats,
        telemetry,
        decisions,
        replay_enabled=replay_enabled,
        verified_evidence=verified_evidence,
        frame_limit=limit,
    )
    _telemetry_banner(
        run_status=str(run.get("status") or "unknown"),
        simulation_clock=_latest_simulated_text(run_stats, telemetry, limit),
        recorded_at=telemetry.iloc[-1].get("timestamp") if not telemetry.empty else None,
        row_count=len(telemetry) if limit is not None else run_stats["telemetry_steps"],
        replay=limit is not None,
    )
    _simulation_panel(run, run_stats, message_counts)
    _zone_panel(run, telemetry, zones)
    agent_column, control_column = st.columns((1.0, 1.0), gap="large")
    with agent_column:
        _agent_panel(run, run_stats, decisions, run_type=run_type)
    with control_column:
        _control_panel(latest_apply, actions, run_type=run_type)
    _console_panel(console)


def _mode_badge(
    status: str,
    *,
    replay_enabled: bool,
    verified_evidence: bool,
) -> tuple[str, str, str]:
    """Map run state to the fail-closed data-mode badge (label, detail, class)."""

    if replay_enabled:
        return (
            "VERIFIED RUN REPLAY",
            "Recorded physical telemetry · visual playback only · not active control",
            "mode-replay",
        )
    if status == "running":
        return (
            "LIVE SIMULATION",
            "EnergyPlus telemetry · control loop active",
            "mode-live",
        )
    if status == "completed":
        if verified_evidence:
            return (
                "EVIDENCE CONSOLE",
                "Finalized audit record · official energy cross-check passed",
                "mode-complete",
            )
        return (
            "COMPLETED RUN",
            "Final evidence verification unavailable or failed",
            "mode-neutral",
        )
    return (status.upper(), "Run record · inspect status below", "mode-neutral")


def _command_header(
    run: dict[str, Any],
    run_stats: RunStatistics,
    telemetry: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    replay_enabled: bool,
    verified_evidence: bool,
    frame_limit: int | None,
) -> None:
    status = str(run.get("status") or "unknown").casefold()
    mode_label, mode_detail, mode_class = _mode_badge(
        status,
        replay_enabled=replay_enabled,
        verified_evidence=verified_evidence,
    )
    simulated = _latest_simulated_text(run_stats, telemetry, frame_limit)
    status_class = {
        "completed": "status-completed",
        "running": "status-running",
        "failed": "status-failed",
    }.get(status, "status-neutral")
    model_name = _decision_model(decisions, run_stats)
    tool_calls = int(run_stats["tool_call_count"])
    action_total = int(run_stats["proposed_action_count"]) + int(run_stats["applied_action_count"])
    chips = [
        ("ENERGYPLUS", status.upper(), status != "failed"),
        (
            "MCP",
            f"{tool_calls:,} tool calls" if tool_calls else "no tool calls",
            tool_calls > 0,
        ),
        ("MODEL", model_name or "no decisions", model_name is not None),
        (
            "SAFETY",
            (
                f"validator · {int(run_stats['safety_clamp_count'])} clamps"
                if action_total
                else "no actions"
            ),
            action_total > 0,
        ),
    ]
    chip_html = "".join(
        (
            f"<div class='cc-chip {'chip-on' if active else 'chip-off'}'>"
            f"<span>{escape(label)}</span><strong>{escape(value)}</strong></div>"
        )
        for label, value, active in chips
    )
    st.markdown(
        f"""
        <section class="cc-header">
          <div class="cc-brand">
            <span class="cc-eyebrow">BUILDING SUPERVISORY CONTROL</span>
            <h1>EcoLoop Control Room</h1>
          </div>
          <div class="cc-mode {mode_class}">
            <span class="mode-signal"></span>
            <div>
              <strong>{escape(mode_label)}</strong>
              <small>{escape(mode_detail)}</small>
            </div>
          </div>
        </section>
        <div class="cc-idbar">
          <div class="cc-id cc-id-wide">
            <span>RUN</span>
            <code>{escape(str(run["run_id"]))}</code>
          </div>
          <div class="cc-id">
            <span>TYPE · PERIOD</span>
            <strong>{escape(str(run.get("run_type") or "unknown").upper())} ·
            {escape(str(run.get("period_name") or "unspecified").upper())}</strong>
          </div>
          <div class="cc-id">
            <span>STATUS</span>
            <strong class="{status_class}">{escape(status.upper())}</strong>
          </div>
          <div class="cc-id">
            <span>SIMULATED CLOCK</span>
            <strong>{escape(simulated)}</strong>
          </div>
          <div class="cc-id">
            <span>ENERGYPLUS</span>
            <strong>{escape(str(run.get("energyplus_version") or "unknown"))}</strong>
          </div>
          <div class="cc-id">
            <span>DATA ORIGIN</span>
            <strong class="origin">REAL DATABASE RECORD</strong>
          </div>
        </div>
        <div class="cc-chips">{chip_html}</div>
        """,
        unsafe_allow_html=True,
    )


def _disconnected_header(reason: str) -> None:
    """Render the DISCONNECTED data-mode header when no evidence is usable."""

    st.markdown(
        f"""
        <section class="cc-header">
          <div class="cc-brand">
            <span class="cc-eyebrow">BUILDING SUPERVISORY CONTROL</span>
            <h1>EcoLoop Control Room</h1>
          </div>
          <div class="cc-mode mode-neutral">
            <span class="mode-signal"></span>
            <div>
              <strong>DISCONNECTED</strong>
              <small>{escape(reason)} Missing values stay unavailable; nothing is
              substituted.</small>
            </div>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _sidebar_run_panel(run: dict[str, Any]) -> None:
    """Keep the existing sidebar run card, refresh button and version caption."""

    status = str(run.get("status") or "unknown")
    run_type = str(run.get("run_type") or "unknown")
    period = str(run.get("period_name") or "unspecified")
    progress = float(run.get("progress_percent") or 0.0)
    if status.casefold() == "completed":
        progress = 100.0
    st.sidebar.markdown(
        f"""
        <div class="sidebar-run-card">
          <span>{escape(run_type.upper())}</span>
          <strong>{escape(period.title())}</strong>
          <small>{escape(status.upper())} · {progress:.0f}%</small>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.button("Refresh evidence", width="stretch", type="primary")
    st.sidebar.caption(f"EnergyPlus {escape(str(run.get('energyplus_version') or 'unknown'))}")


def _select_run(
    runs: pd.DataFrame,
    forced: str | None,
    *,
    preferred: str | None,
) -> str:
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
        index=_preferred_run_index(runs, preferred),
        format_func=lambda value: labels[value],
    )


def _read_current_run_id(runs_directory: Path) -> str | None:
    """Read the demo-selected run ID without trusting stale or empty content."""

    try:
        run_id = (runs_directory / "current_run.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return run_id or None


def _preferred_run_index(runs: pd.DataFrame, preferred: str | None) -> int:
    """Choose current evidence, then the latest completed controlled run."""

    normalized = runs.reset_index(drop=True)
    if preferred:
        matching = normalized[
            (normalized["run_id"].astype(str) == preferred)
            & (normalized["is_fake"].fillna(1).astype(int) == 0)
        ]
        if not matching.empty:
            return int(matching.index[0])

    completed = normalized[normalized["status"].astype(str).str.casefold().eq("completed")]
    for run_type in ("agent", "rule", "replay", "fixed_override", "baseline"):
        matching = completed[completed["run_type"].astype(str).str.casefold().eq(run_type)]
        if not matching.empty:
            return int(matching.index[0])
    return 0


def _is_completed_controlled_run(run: dict[str, Any]) -> bool:
    """Return whether a run is real, completed, and physically controlled."""

    controlled_types = {"agent", "rule", "replay", "fixed_override"}
    return (
        not bool(run.get("is_fake"))
        and str(run.get("status") or "").casefold() == "completed"
        and str(run.get("run_type") or "").casefold() in controlled_types
    )


def _has_verified_evidence(metrics: dict[str, dict[str, Any]]) -> bool:
    """Require both run finalization and the official energy cross-check."""

    finalization = _structured_metric(metrics, "finalization_verification")
    cross_check = _structured_metric(metrics, "energy_cross_check")
    return finalization.get("verified_for_comparison") is True and cross_check.get("passed") is True


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


def _simulation_panel(
    run: dict[str, Any],
    run_stats: RunStatistics,
    message_counts: dict[str, int],
) -> None:
    """Provenance, simulated window, diagnostics and exit status of the engine."""

    _panel_header(
        "EnergyPlus simulation",
        eyebrow="PHYSICS ENGINE",
        description=(
            "Model provenance, simulated window and engine diagnostics recorded "
            "for the selected run."
        ),
    )
    metadata = _run_metadata(run)
    status = str(run.get("status") or "unknown")
    model_name = Path(str(run.get("model_path") or "")).name or "unavailable"
    weather_name = Path(str(run.get("weather_path") or "")).name or "unavailable"
    error_summary = str(run.get("error_summary") or "")
    if message_counts:
        diagnostics_value = " · ".join(
            f"{message_counts.get(severity, 0)} {severity}"
            for severity in ("warning", "severe", "fatal")
        )
        diagnostics_note = "recorded engine messages"
    else:
        diagnostics_value = "no engine messages recorded"
        diagnostics_note = ""
    cross_check = run_stats["energy_cross_check_passed"]
    finalized = run_stats["finalization_verified_for_comparison"]
    verification_value = (
        "cross-check PASS"
        if cross_check is True
        else "cross-check FAIL"
        if cross_check is False
        else "cross-check unavailable"
    )
    verification_note = (
        "finalized for comparison"
        if finalized is True
        else "not finalized for comparison"
        if finalized is False
        else "finalization unavailable"
    )
    cells = [
        ("MODEL (IDF)", model_name, _hash_text(metadata.get("input_model_sha256")), ""),
        ("WEATHER (EPW)", weather_name, _hash_text(metadata.get("weather_sha256")), ""),
        (
            "PREPARATION",
            "run-local manifest",
            _hash_text(run_stats["preparation_fingerprint"]),
            "",
        ),
        (
            "PERIOD",
            str(run.get("period_name") or "unspecified").title(),
            _window_text(run_stats),
            "",
        ),
        (
            "DATA ORIGIN",
            str(metadata.get("data_origin") or "not recorded"),
            "",
            "",
        ),
        ("EXIT STATUS", status.upper(), _truncate(error_summary, 90), ""),
        ("DIAGNOSTICS", diagnostics_value, diagnostics_note, ""),
        ("VERIFICATION", verification_value, verification_note, ""),
    ]
    cell_html = "".join(
        (
            "<div class='cc-cell'>"
            f"<span>{escape(label)}</span>"
            f"<strong{_title_attribute(title)}>{escape(value)}</strong>"
            f"<small>{escape(note)}</small>"
            "</div>"
        )
        for label, value, note, title in cells
    )
    st.markdown(f"<div class='cc-grid'>{cell_html}</div>", unsafe_allow_html=True)
    progress = float(run.get("progress_percent") or 0.0)
    if status.casefold() == "completed":
        progress = 100.0
    st.progress(
        min(max(progress, 0.0), 100.0) / 100.0,
        text=f"Simulation lifecycle · {progress:.0f}%",
    )


def _zone_panel(run: dict[str, Any], telemetry: pd.DataFrame, zones: pd.DataFrame) -> None:
    """Latest per-zone physical state plus facility electrical summary."""

    status = str(run.get("status") or "unknown").casefold()
    if status == "running":
        title = "Live zone state"
        description = "Latest persisted EnergyPlus zone conditions from the active run."
    else:
        title = "Zone state · latest persisted frame"
        description = "Final persisted EnergyPlus zone conditions; this run is not live."
    _panel_header(title, eyebrow="PHYSICAL STATE", description=description)

    latest = telemetry.tail(1)
    demand = latest.iloc[0].get("facility_demand_kw") if not latest.empty else None
    cumulative = latest.iloc[0].get("cumulative_electricity_kwh") if not latest.empty else None
    outdoor = latest.iloc[0].get("outdoor_temperature_c") if not latest.empty else None
    latest_zones = (
        zones[zones["simulation_timestamp"] == zones["simulation_timestamp"].max()]
        if not zones.empty
        else zones
    )
    occupancy = latest_zones["occupant_count"].sum() if not latest_zones.empty else None

    chips = st.columns(4)
    chips[0].metric("Facility demand", _format_value(demand, " kW"))
    chips[1].metric("Cumulative electricity", _format_value(cumulative, " kWh"))
    chips[2].metric("Outdoor air", _format_value(outdoor, " °C"))
    chips[3].metric("Occupancy", _format_value(occupancy, " people"))

    if latest_zones.empty:
        st.caption("No zone telemetry has been persisted for this run.")
    else:
        rows = [
            [
                str(row.zone_name),
                _cell_number(row.operative_temperature_c, " °C"),
                _cell_number(row.occupant_count, "", precision=1),
                _cell_number(row.pmv, "", precision=2),
                _cell_number(row.heating_setpoint_c, " °C", precision=1),
                _cell_number(row.cooling_setpoint_c, " °C", precision=1),
            ]
            for row in latest_zones.itertuples()
        ]
        st.markdown(
            _html_table(
                ("Zone", "Operative", "People", "PMV", "Heat SP", "Cool SP"),
                rows,
            ),
            unsafe_allow_html=True,
        )

    if telemetry.empty:
        st.info("The run exists but has not produced facility telemetry.")
        return
    temperature_column, demand_column = st.columns(2, gap="large")
    with temperature_column:
        frame = telemetry.copy()
        if not zones.empty:
            mean_zone = (
                zones.groupby("simulation_timestamp", as_index=False)["operative_temperature_c"]
                .mean()
                .rename(columns={"operative_temperature_c": "mean_operative_temperature_c"})
            )
            frame = frame.merge(mean_zone, on="simulation_timestamp", how="left")
        else:
            frame["mean_operative_temperature_c"] = pd.NA
        temperature_figure = go.Figure()
        for column, label, color, dash in (
            ("mean_operative_temperature_c", "Mean operative", "#34d399", "solid"),
            ("outdoor_temperature_c", "Outdoor", "#77918a", "dot"),
            ("heating_setpoint_c", "Heating setpoint", "#e0a33e", "dash"),
            ("cooling_setpoint_c", "Cooling setpoint", "#38bdf8", "dash"),
        ):
            if column in frame and frame[column].notna().any():
                temperature_figure.add_scatter(
                    x=frame["simulation_timestamp"],
                    y=frame[column],
                    name=label,
                    mode="lines",
                    line={"color": color, "width": 2.2, "dash": dash},
                )
        _style_chart(
            temperature_figure,
            title="Zone temperature and setpoints",
            yaxis_title="Temperature (°C)",
        )
        st.plotly_chart(temperature_figure, width="stretch")
    with demand_column:
        demand_figure = go.Figure()
        demand_figure.add_scatter(
            x=telemetry["simulation_timestamp"],
            y=telemetry["facility_demand_kw"],
            name="Demand",
            mode="lines",
            fill="tozeroy",
            line={"color": "#2dd4a7", "width": 2.2},
            fillcolor="rgba(45,212,167,0.10)",
        )
        _style_chart(
            demand_figure,
            title="Facility electrical demand",
            yaxis_title="Demand (kW)",
        )
        st.plotly_chart(demand_figure, width="stretch")


def _agent_panel(
    run: dict[str, Any],
    run_stats: RunStatistics,
    decisions: pd.DataFrame,
    *,
    run_type: str,
) -> None:
    """Latest local-model decisions, latency and stored agent configuration."""

    model_name = _decision_model(decisions, run_stats)
    title = f"AI agent ({model_name} · local)" if model_name else "AI agent"
    _panel_header(
        title,
        eyebrow="DECISION LOOP",
        description="Persisted local-model decisions with rationale and latency.",
    )
    if run_type == "rule":
        st.info(
            "Deterministic rule-controller mode · bounded setpoint actions are "
            "generated without model inference."
        )
    if decisions.empty:
        if run_type not in {"rule"}:
            st.caption("No model decisions are persisted for this run.")
    else:
        cards = st.columns(3)
        cards[0].metric("Decisions", int(run_stats["decision_count"]))
        cards[1].metric(
            "Mean latency",
            _format_duration_ms(run_stats["average_decision_latency_ms"]),
        )
        cards[2].metric(
            "P95 latency",
            _format_duration_ms(run_stats["p95_decision_latency_ms"]),
        )
        latest = decisions.iloc[0]
        outcome = (
            str(latest.get("fallback_status")).replace("_", " ").title()
            if _is_active_fallback(latest.get("fallback_status"))
            else "Validated action"
        )
        st.markdown(
            f"""
            <div class="decision-summary">
              <span class="decision-kicker">LATEST DECISION · {escape(outcome.upper())}</span>
              <strong>{
                escape(str(latest.get("reason_code") or "unspecified").replace("_", " ").title())
            }</strong>
              <p>{
                escape(str(latest.get("explanation") or "No operational explanation recorded."))
            }</p>
              <small>Observation {escape(str(latest.get("observation_id")))} ·
              {escape(_format_duration_ms(latest.get("latency_ms")))}</small>
            </div>
            """,
            unsafe_allow_html=True,
        )
        rows = [
            [
                _wall_time_text(row.timestamp),
                str(row.reason_code or "—").replace("_", " "),
                _format_duration_ms(row.latency_ms),
                (
                    str(row.fallback_status).replace("_", " ")
                    if _is_active_fallback(row.fallback_status)
                    else "—"
                ),
                _truncate(str(row.explanation or "—"), 90),
            ]
            for row in decisions.itertuples()
        ]
        st.markdown(
            _html_table(
                ("Time", "Reason", "Latency", "Fallback", "Explanation"),
                rows,
            ),
            unsafe_allow_html=True,
        )
    configuration = _agent_configuration(run)
    if configuration:
        with st.expander("Agent configuration · run record", expanded=False):
            for label, value in configuration:
                st.markdown(
                    f"<div class='cc-config-row'><span>{escape(label)}</span>"
                    f"<strong>{escape(value)}</strong></div>",
                    unsafe_allow_html=True,
                )
            st.caption(
                "Only configuration persisted in the run record is shown; "
                "model token budgets are not stored."
            )


def _control_panel(
    latest_apply: dict[str, Any] | None,
    actions: pd.DataFrame,
    *,
    run_type: str,
) -> None:
    """Latest apply_control_action MCP call and the recent action audit trail."""

    _panel_header(
        "Control actions (MCP)",
        eyebrow="AUDITED ACTUATION",
        description=(
            "The most recent apply_control_action tool call and the persisted "
            "proposed-versus-applied audit trail."
        ),
    )
    if latest_apply is None:
        if run_type == "rule":
            st.caption("Rule-controller mode does not invoke model-facing MCP tools.")
        else:
            st.caption("No apply_control_action MCP call is persisted for this run.")
    else:
        state = "success" if int(latest_apply.get("success") or 0) == 1 else "failed"
        duration = _format_duration_ms(latest_apply.get("duration_ms"))
        called_at = _wall_time_text(latest_apply.get("timestamp"))
        st.markdown(
            f"<div class='cc-callmeta'><span>LATEST apply_control_action</span>"
            f"<strong class='{'call-ok' if state == 'success' else 'call-error'}'>"
            f"{escape(state)}</strong>"
            f"<small>{escape(duration)} · {escape(called_at)}</small></div>",
            unsafe_allow_html=True,
        )
        st.code(_pretty_json(latest_apply.get("arguments_json")), language="json")
        error = latest_apply.get("error")
        if error:
            st.caption(f"Error: {error}")
    if actions.empty:
        st.caption("No control actions are persisted for this run.")
        return
    rows = []
    for row in actions.itertuples():
        applied = int(getattr(row, "applied", 0) or 0)
        if applied == 0:
            validation = "not applied"
        elif _has_clamp(row.clamp_details):
            validation = "clamped"
        else:
            validation = "valid"
        rows.append(
            [
                _simulation_clock(row.simulation_timestamp),
                _setpoint_text(row.proposed_values),
                _setpoint_text(row.applied_values) if applied else "—",
                validation,
                (
                    str(row.fallback_status).replace("_", " ")
                    if _is_active_fallback(row.fallback_status)
                    else "—"
                ),
                str(row.reason_code or "—").replace("_", " "),
            ]
        )
    st.markdown(
        _html_table(
            ("Simulated time", "Proposed", "Applied", "Validation", "Fallback", "Reason"),
            rows,
        ),
        unsafe_allow_html=True,
    )


def _console_panel(events: pd.DataFrame) -> None:
    """Merged wall-clock log of persisted tool calls, actions and messages."""

    _panel_header(
        "System console",
        eyebrow="MERGED EVENT LOG",
        description=(
            "Most recent persisted MCP tool calls, physically applied actions and "
            "EnergyPlus messages, in wall-clock order."
        ),
    )
    if events.empty:
        st.caption("No events are persisted for this run.")
        return
    source_labels = {"mcp": "MCP", "action": "ACTUATE", "energyplus": "ENGINE"}
    rows: list[str] = []
    for row in events.itertuples():
        source = str(row.source)
        label = str(row.label or "")
        status = str(row.status or "")
        detail = str(row.detail or "")
        if source == "action":
            detail = _setpoint_text(detail)
        if source == "energyplus":
            status_class = {
                "severe": "c-error",
                "fatal": "c-error",
                "warning": "c-warn",
            }.get(label.casefold(), "c-dim")
            status_text = label.casefold()
        else:
            status_class = {
                "ok": "c-ok",
                "applied": "c-ok",
                "error": "c-error",
                "fallback": "c-warn",
            }.get(status.casefold(), "c-dim")
            status_text = status
        rows.append(
            "<div class='console-row'>"
            f"<span class='c-time'>{escape(_wall_time_text(row.timestamp))}</span>"
            f"<span class='c-src src-{escape(source)}'>"
            f"{escape(source_labels.get(source, source.upper()))}</span>"
            f"<span class='c-label'>{escape(label if source != 'energyplus' else 'message')}"
            "</span>"
            f"<span class='c-status {status_class}'>{escape(status_text)}</span>"
            f"<span class='c-detail'>{escape(_truncate(detail, 110))}</span>"
            "</div>"
        )
    st.markdown(
        f"<div class='console'>{''.join(rows)}</div>",
        unsafe_allow_html=True,
    )


def _performance_section(database_path: Path, runs: pd.DataFrame) -> None:
    """Verified baseline comparison; fails closed for incompatible pairs."""

    _panel_header(
        "Performance vs baseline",
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
            title=f"{electricity_change:+.2f}% facility electricity vs baseline",
            detail="Measured over the complete selected period; lower is better.",
        )
    elif electricity_change is not None:
        _outcome_banner(
            tone="caution",
            title=(
                f"{electricity_change:+.2f}% facility electricity vs baseline — no energy saving"
            ),
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
    ):
        base = _metric_value(baseline_metrics, metric_name)
        controlled_value = _metric_value(controlled_metrics, metric_name)
        card.metric(
            label,
            _format_value(controlled_value, suffix),
            delta=_comparison_delta(base, controlled_value, suffix),
            delta_color="inverse",
        )
    impact_cards[2].metric(
        "Fallback actions · controlled",
        _format_count(_metric_value(controlled_metrics, "fallback_count")),
    )
    impact_cards[3].metric(
        "Safety clamps · controlled",
        _format_count(_metric_value(controlled_metrics, "safety_clamp_count")),
    )

    _subpanel_header(
        "Demand and energy trajectory",
        "Runtime API telemetry aligned at identical simulated timestamps; official "
        "totals are the cards above.",
    )
    demand_frame = queries.aligned_demand(database_path, str(baseline_id), str(controlled_id))
    if demand_frame.empty:
        st.caption("No aligned demand telemetry is available for this pair.")
    else:
        demand_figure = go.Figure()
        demand_figure.add_scatter(
            x=demand_frame["simulation_timestamp"],
            y=demand_frame["facility_demand_kw_baseline"],
            name="Baseline",
            mode="lines",
            line={"color": "#7f938c", "width": 1.8},
        )
        demand_figure.add_scatter(
            x=demand_frame["simulation_timestamp"],
            y=demand_frame["facility_demand_kw_controlled"],
            name="Controlled",
            mode="lines",
            line={"color": "#2dd4a7", "width": 2.2},
        )
        _style_chart(
            demand_figure,
            title="Facility demand · aligned simulated time",
            yaxis_title="Demand (kW)",
        )
        st.plotly_chart(demand_figure, width="stretch")
    aligned = queries.aligned_cumulative_energy(database_path, str(baseline_id), str(controlled_id))
    if not aligned.empty:
        figure = go.Figure()
        figure.add_scatter(
            x=aligned["simulation_timestamp"],
            y=aligned["cumulative_electricity_kwh_baseline"],
            name="Baseline",
            mode="lines",
            line={"color": "#7f938c", "width": 1.8},
        )
        figure.add_scatter(
            x=aligned["simulation_timestamp"],
            y=aligned["cumulative_electricity_kwh_controlled"],
            name="Controlled",
            mode="lines",
            line={"color": "#38bdf8", "width": 2.2},
        )
        _style_chart(
            figure,
            title="Cumulative electricity · aligned simulated time",
            yaxis_title="Electricity (kWh)",
        )
        st.plotly_chart(figure, width="stretch")

    _subpanel_header(
        "Verified outcome scorecard",
        "Every row comes from metrics explicitly marked verified in the run database.",
    )
    scorecard = _comparison_scorecard(baseline_metrics, controlled_metrics)
    if scorecard.empty:
        st.caption("No verified scorecard metrics exist for this pair.")
    else:
        rows = [
            [
                str(record["Metric"]),
                str(record["Unit"]),
                _cell_number(record["Baseline"], ""),
                _cell_number(record["Controlled"], ""),
                _cell_signed(record["Absolute change"]),
                _cell_signed(record["Relative change"], "%"),
                str(record["Preferred direction"]),
            ]
            for record in scorecard.to_dict("records")
        ]
        st.markdown(
            _html_table(
                (
                    "Metric",
                    "Unit",
                    "Baseline",
                    "Controlled",
                    "Absolute change",
                    "Relative change",
                    "Preferred",
                ),
                rows,
            ),
            unsafe_allow_html=True,
        )


def _comfort_section(database_path: Path, run_id: str, limit: int | None) -> None:
    """Occupied PMV compliance per zone from the persisted distribution query."""

    _panel_header(
        "Comfort distribution",
        eyebrow="ZONE EVIDENCE",
        description=(
            "Occupied PMV compliance per zone from persisted samples against the "
            "|PMV| ≤ 0.7 target."
        ),
    )
    simulation_end = None
    if limit is not None:
        limited = queries.telemetry(database_path, run_id, limit=limit)
        if not limited.empty:
            simulation_end = limited["simulation_timestamp"].max()
    distribution = queries.comfort_distribution(
        database_path,
        run_id,
        simulation_end=simulation_end,
    )
    if distribution.empty:
        st.caption("No zone telemetry is persisted; comfort distribution is unavailable.")
        return
    occupied = distribution[distribution["occupied_samples"] > 0]
    if occupied.empty or occupied["occupied_pmv_compliance_percent"].isna().all():
        st.caption("No occupied PMV samples are persisted; compliance distribution is unavailable.")
        return
    figure = go.Figure(
        go.Bar(
            x=occupied["occupied_pmv_compliance_percent"],
            y=occupied["zone_name"],
            orientation="h",
            marker={"color": "#2dd4a7"},
            customdata=occupied["occupied_samples"],
            hovertemplate=(
                "%{y}: %{x:.1f}% compliant · %{customdata} occupied samples<extra></extra>"
            ),
        )
    )
    figure.update_xaxes(range=[0, 100])
    _style_chart(
        figure,
        title="Occupied PMV compliance by zone",
        yaxis_title="Zone",
        height=max(260, 140 + 44 * len(occupied)),
    )
    st.plotly_chart(figure, width="stretch")


def _pipeline_section(database_path: Path, run_id: str) -> None:
    """Closed-loop stage counts derived only from persisted records."""

    run_stats = queries.run_statistics(database_path, run_id)
    artifacts = queries.run_artifacts(database_path, run_id)
    _panel_header(
        "Closed-loop pipeline",
        eyebrow="EVIDENCE TRAIL",
        description=(
            "Persisted record counts for each control-loop stage of the selected "
            "run; a stage with no records reads none."
        ),
    )
    evidence_count = int(run_stats["verified_metric_count"]) + len(artifacts)
    steps = [
        ("OBSERVE", int(run_stats["observation_count"]), "observations"),
        ("DECIDE", int(run_stats["decision_count"]), "model decisions"),
        ("MCP TOOLS", int(run_stats["tool_call_count"]), "tool calls"),
        (
            "VALIDATE",
            int(run_stats["proposed_action_count"]),
            f"proposals · {int(run_stats['safety_clamp_count'])} clamps",
        ),
        ("ACTUATE", int(run_stats["applied_action_count"]), "applied actions"),
        (
            "EVIDENCE",
            evidence_count,
            (
                f"{int(run_stats['verified_metric_count'])} verified metrics · "
                f"{len(artifacts)} artifacts"
            ),
        ),
    ]
    chip_html = "<div class='pipe-arrow'>→</div>".join(
        (
            f"<div class='pipe-step {'step-on' if count > 0 else 'step-off'}'>"
            f"<span>{escape(label)}</span>"
            f"<strong>{count:,}</strong>"
            f"<small>{escape(note)}</small>"
            f"<em>{'recorded' if count > 0 else 'none'}</em>"
            "</div>"
        )
        for label, count, note in steps
    )
    st.markdown(f"<div class='pipeline'>{chip_html}</div>", unsafe_allow_html=True)
    if not artifacts.empty:
        with st.expander("Recorded run artifacts", expanded=False):
            st.dataframe(
                artifacts,
                hide_index=True,
                width="stretch",
            )


def _panel_header(title: str, *, eyebrow: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="cc-panel-head">
          <span>{escape(eyebrow)}</span>
          <h2>{escape(title)}</h2>
          <p>{escape(description)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _subpanel_header(title: str, description: str) -> None:
    st.markdown(
        f"""
        <div class="cc-subpanel-head">
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
        label = "VERIFIED RUN REPLAY"
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


def _structured_metric(
    metrics: dict[str, dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    value = metrics.get(metric_name, {}).get("structured_value")
    return value if isinstance(value, dict) else {}


def _run_metadata(run: dict[str, Any]) -> dict[str, Any]:
    raw = run.get("metadata_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _agent_configuration(run: dict[str, Any]) -> list[tuple[str, str]]:
    """Read only the control configuration persisted in the run record."""

    metadata = _run_metadata(run)
    rows: list[tuple[str, str]] = []
    for key, label, suffix in (
        ("controller_mode", "Controller mode", ""),
        ("decision_interval_minutes", "Decision interval", " min"),
        ("maximum_action_hold_minutes", "Maximum action hold", " min"),
        ("simulation_timeout_seconds", "Simulation timeout", " s"),
        ("display_delay_seconds", "Display delay", " s"),
        ("data_origin", "Data origin", ""),
    ):
        value = metadata.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            rows.append((label, f"{value:g}{suffix}"))
        else:
            rows.append((label, f"{value}{suffix}"))
    return rows


def _decision_model(decisions: pd.DataFrame, run_stats: RunStatistics) -> str | None:
    if not decisions.empty and "model" in decisions:
        models = decisions["model"].dropna()
        if not models.empty:
            return str(models.iloc[0])
    return run_stats["latest_action_model"]


def _latest_simulated_text(
    run_stats: RunStatistics,
    telemetry: pd.DataFrame,
    limit: int | None,
) -> str:
    if limit is not None and not telemetry.empty:
        return _simulation_clock(telemetry["simulation_timestamp"].max())
    latest = run_stats["latest_simulation_timestamp"]
    if latest is None:
        return "No telemetry"
    return _simulation_clock(pd.Timestamp(latest))


def _window_text(run_stats: RunStatistics) -> str:
    start = run_stats["telemetry_start"]
    end = run_stats["telemetry_end"]
    if start is None or end is None:
        return "no telemetry window"
    return f"{_simulation_clock(pd.Timestamp(start))} → {_simulation_clock(pd.Timestamp(end))}"


def _hash_text(value: Any) -> str:
    if not value:
        return "hash unavailable"
    text = str(value)
    return f"sha256 {text[:12]}…" if len(text) > 12 else f"sha256 {text}"


def _title_attribute(value: str) -> str:
    return f' title="{escape(value)}"' if value else ""


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Build an escaped dark-theme table from preformatted cell text."""

    head = "".join(f"<th>{escape(str(header))}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(str(cell))}</td>" for cell in row) + "</tr>" for row in rows
    )
    return (
        '<div class="cc-tablewrap"><table class="cc-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _cell_number(value: Any, suffix: str, *, precision: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    if isinstance(value, (int, float)):
        return f"{float(value):,.{precision}f}{suffix}"
    return f"{value}{suffix}"


def _cell_signed(value: Any, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):+,.2f}{suffix}"


def _pretty_json(value: Any) -> str:
    if value is None or value == "":
        return "unavailable"
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return str(value)
    return json.dumps(parsed, ensure_ascii=True, indent=2, sort_keys=True)


def _setpoint_text(value: Any) -> str:
    """Format stored action values as plain text; callers escape for HTML."""

    parsed: Any = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value or "unavailable"
    if not isinstance(parsed, dict):
        return str(parsed) if parsed else "unavailable"
    labels = {
        "heating_setpoint_c": ("Heat", "°C", 1),
        "cooling_setpoint_c": ("Cool", "°C", 1),
        "hold_minutes": ("Hold", "min", 0),
        "hold_duration_minutes": ("Hold", "min", 0),
    }
    parts: list[str] = []
    for key, (label, unit, precision) in labels.items():
        item = parsed.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            parts.append(f"{label} {float(item):.{precision}f}{unit}")
    if parts:
        return " · ".join(parts)
    extras = [
        f"{key.replace('_', ' ')} {item}"
        for key, item in parsed.items()
        if item is not None and key != "candidate_id"
    ]
    return " · ".join(extras) if extras else "unavailable"


def _truncate(text: str, limit: int = 110) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wall_time_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return "--:--:--"
    return str(pd.Timestamp(value).strftime("%H:%M:%S"))


def _timestamp_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return "time unavailable"
    timestamp = pd.Timestamp(value)
    return str(timestamp.strftime("%H:%M:%S UTC"))


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
    height: int = 320,
) -> None:
    figure.update_layout(
        title={"text": title, "font": {"size": 15, "color": "#d5e4dd"}},
        yaxis_title=yaxis_title,
        xaxis_title=None,
        template="plotly_dark",
        height=height,
        margin={"l": 24, "r": 18, "t": 64, "b": 24},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, Segoe UI, sans-serif", "size": 12, "color": "#9db4ab"},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.06,
            "xanchor": "left",
            "x": 0,
        },
    )
    figure.update_xaxes(showgrid=False, linecolor="rgba(157,180,171,.25)", zeroline=False)
    figure.update_yaxes(
        gridcolor="rgba(157,180,171,.10)",
        zeroline=False,
        linecolor="rgba(157,180,171,.25)",
    )


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


def _format_duration_ms(value: Any) -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    milliseconds = float(value)
    if abs(milliseconds) >= 1_000:
        return f"{milliseconds / 1_000:,.2f} s"
    return f"{milliseconds:,.2f} ms"


def _format_count(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    return f"{int(value):,}"


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


def _format_value(value: Any, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "Unavailable"
    if isinstance(value, (int, float)):
        return f"{float(value):,.2f}{suffix}"
    return f"{value}{suffix}"


def _apply_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --cc-bg: #0a100e;
            --cc-panel: #101815;
            --cc-panel-2: #0d1412;
            --cc-line: #1e2b26;
            --cc-line-2: #2a3a33;
            --cc-ink: #e5f0ea;
            --cc-dim: #8ba49a;
            --cc-faint: #5d736b;
            --cc-teal: #2dd4a7;
            --cc-cyan: #38bdf8;
            --cc-amber: #e0a33e;
            --cc-red: #e2685f;
            --cc-mono: ui-monospace, SFMono-Regular, Consolas, "Cascadia Mono", monospace;
        }
        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(circle at 85% -8%, rgba(45, 212, 167, .07), transparent 30rem),
                linear-gradient(180deg, #0c1210 0%, var(--cc-bg) 100%);
            color: var(--cc-ink);
        }
        [data-testid="stHeader"] { background: transparent; }
        .block-container {
            max-width: 1560px;
            padding-top: 1.1rem;
            padding-bottom: 2.6rem;
        }
        h1, h2, h3, h4 { color: var(--cc-ink); }
        [data-testid="stMarkdownContainer"] { color: var(--cc-ink); }
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p { color: var(--cc-faint); }
        [data-testid="stWidgetLabel"] p { color: var(--cc-dim); }

        [data-testid="stSidebar"] {
            background: #0c1311;
            border-right: 1px solid var(--cc-line);
        }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] h4 {
            color: var(--cc-dim);
        }
        .sidebar-brand { display: grid; gap: .08rem; padding: .3rem 0 .8rem; }
        .sidebar-brand span {
            color: var(--cc-teal);
            font-size: .68rem;
            font-weight: 850;
            letter-spacing: .17em;
        }
        .sidebar-brand strong { color: var(--cc-ink); font-size: 1.16rem; }
        .sidebar-run-card {
            display: grid;
            gap: .18rem;
            margin: .6rem 0 1rem;
            padding: .85rem;
            border: 1px solid var(--cc-line-2);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        .sidebar-run-card span,
        .sidebar-run-card small {
            color: var(--cc-dim);
            font-size: .7rem;
            letter-spacing: .05em;
        }
        .sidebar-run-card strong { color: var(--cc-ink); }

        .cc-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1.4rem;
            padding: 1.35rem 1.5rem;
            margin-bottom: .65rem;
            border: 1px solid var(--cc-line-2);
            border-radius: 16px;
            background:
                linear-gradient(rgba(255,255,255,.022) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,.022) 1px, transparent 1px),
                linear-gradient(120deg, #0d1a16 0%, #0f201b 60%, #0d1a17 100%);
            background-size: 26px 26px, 26px 26px, auto;
        }
        .cc-eyebrow {
            color: var(--cc-teal);
            font-size: .68rem;
            font-weight: 850;
            letter-spacing: .18em;
        }
        .cc-header h1 {
            margin: .28rem 0 0;
            color: var(--cc-ink) !important;
            font-size: clamp(1.7rem, 3vw, 2.5rem);
            letter-spacing: -.035em;
            line-height: 1.05;
        }
        .cc-mode {
            display: flex;
            align-items: center;
            gap: .7rem;
            min-width: 250px;
            padding: .85rem 1rem;
            border: 1px solid var(--cc-line-2);
            border-radius: 12px;
            background: rgba(6, 14, 12, .55);
        }
        .cc-mode > div { display: grid; gap: .14rem; }
        .cc-mode strong { color: var(--cc-ink); font-size: .76rem; letter-spacing: .1em; }
        .cc-mode small { color: var(--cc-dim); line-height: 1.35; font-size: .72rem; }
        .mode-signal {
            display: inline-block;
            flex: 0 0 auto;
            width: .62rem;
            height: .62rem;
            border: 2px solid rgba(255,255,255,.35);
            border-radius: 50%;
            background: var(--cc-teal);
            box-shadow: 0 0 0 .26rem rgba(45,212,167,.12);
        }
        .mode-replay .mode-signal,
        .banner-replay .mode-signal {
            background: var(--cc-cyan);
            box-shadow: 0 0 0 .26rem rgba(56,189,248,.13);
        }
        .mode-neutral .mode-signal,
        .banner-neutral .mode-signal {
            background: var(--cc-faint);
            box-shadow: 0 0 0 .26rem rgba(93,115,107,.14);
        }

        .cc-idbar {
            display: grid;
            grid-template-columns: minmax(0, 1.7fr) repeat(5, minmax(0, 1fr));
            gap: .55rem;
            margin-bottom: .55rem;
        }
        .cc-id {
            display: grid;
            gap: .16rem;
            min-width: 0;
            padding: .6rem .75rem;
            border: 1px solid var(--cc-line);
            border-radius: 10px;
            background: var(--cc-panel-2);
        }
        .cc-id span {
            color: var(--cc-faint);
            font-size: .6rem;
            font-weight: 800;
            letter-spacing: .12em;
        }
        .cc-id strong {
            overflow: hidden;
            color: var(--cc-ink);
            font-size: .78rem;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .cc-id code {
            overflow: hidden;
            color: var(--cc-teal);
            text-overflow: ellipsis;
            white-space: nowrap;
            background: transparent;
            font-family: var(--cc-mono);
            font-size: .76rem;
        }
        .cc-id .origin { color: var(--cc-teal); }
        .status-completed { color: var(--cc-teal); }
        .status-running { color: var(--cc-cyan); }
        .status-failed { color: var(--cc-red); }
        .status-neutral { color: var(--cc-dim); }

        .cc-chips {
            display: flex;
            flex-wrap: wrap;
            gap: .55rem;
            margin-bottom: .8rem;
        }
        .cc-chip {
            display: flex;
            align-items: baseline;
            gap: .55rem;
            padding: .45rem .75rem;
            border: 1px solid var(--cc-line);
            border-radius: 999px;
            background: var(--cc-panel-2);
        }
        .cc-chip span {
            color: var(--cc-faint);
            font-size: .6rem;
            font-weight: 800;
            letter-spacing: .12em;
        }
        .cc-chip strong { color: var(--cc-ink); font-size: .74rem; font-weight: 650; }
        .chip-on { border-color: rgba(45,212,167,.36); }
        .chip-on span { color: var(--cc-teal); }
        .chip-off strong { color: var(--cc-dim); }

        .cc-panel-head { margin: 1.55rem 0 .8rem; }
        .cc-panel-head span {
            color: var(--cc-teal);
            font-size: .64rem;
            font-weight: 850;
            letter-spacing: .17em;
        }
        .cc-panel-head h2 {
            margin: .16rem 0 .2rem;
            color: var(--cc-ink) !important;
            font-size: clamp(1.3rem, 2.2vw, 1.7rem);
            letter-spacing: -.03em;
        }
        .cc-panel-head p {
            max-width: 900px;
            margin: 0;
            color: var(--cc-dim);
            font-size: .86rem;
            line-height: 1.5;
        }
        .cc-subpanel-head { margin: 1.35rem 0 .6rem; }
        .cc-subpanel-head h3 {
            margin: 0 0 .14rem;
            color: var(--cc-ink) !important;
            font-size: 1.06rem;
        }
        .cc-subpanel-head p { margin: 0; color: var(--cc-dim); font-size: .8rem; }

        .cc-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: .55rem;
            margin-bottom: .7rem;
        }
        .cc-cell {
            display: grid;
            gap: .2rem;
            min-width: 0;
            padding: .7rem .8rem;
            border: 1px solid var(--cc-line);
            border-radius: 10px;
            background: var(--cc-panel);
        }
        .cc-cell span {
            color: var(--cc-faint);
            font-size: .6rem;
            font-weight: 800;
            letter-spacing: .12em;
        }
        .cc-cell strong {
            overflow: hidden;
            color: var(--cc-ink);
            font-size: .82rem;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .cc-cell small {
            overflow: hidden;
            color: var(--cc-dim);
            font-family: var(--cc-mono);
            font-size: .68rem;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .cc-tablewrap {
            overflow-x: auto;
            margin: .5rem 0 .8rem;
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        .cc-table { width: 100%; border-collapse: collapse; font-size: .8rem; }
        .cc-table th {
            padding: .55rem .75rem;
            border-bottom: 1px solid var(--cc-line-2);
            color: var(--cc-faint);
            font-size: .64rem;
            font-weight: 800;
            letter-spacing: .1em;
            text-align: left;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .cc-table td {
            padding: .5rem .75rem;
            border-bottom: 1px solid var(--cc-line);
            color: var(--cc-ink);
            font-family: var(--cc-mono);
            font-size: .76rem;
            white-space: nowrap;
        }
        .cc-table tr:last-child td { border-bottom: none; }

        .cc-config-row {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            padding: .3rem 0;
            border-bottom: 1px solid var(--cc-line);
        }
        .cc-config-row span { color: var(--cc-dim); font-size: .8rem; }
        .cc-config-row strong {
            color: var(--cc-ink);
            font-family: var(--cc-mono);
            font-size: .8rem;
        }
        .cc-callmeta {
            display: flex;
            align-items: baseline;
            gap: .6rem;
            margin: .2rem 0 .4rem;
        }
        .cc-callmeta span {
            color: var(--cc-faint);
            font-size: .64rem;
            font-weight: 800;
            letter-spacing: .1em;
        }
        .cc-callmeta strong { font-size: .78rem; letter-spacing: .06em; }
        .cc-callmeta small { color: var(--cc-dim); font-family: var(--cc-mono); }
        .call-ok { color: var(--cc-teal); }
        .call-error { color: var(--cc-red); }

        .console {
            margin: .4rem 0 .9rem;
            padding: .6rem .4rem;
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: #0b1110;
            font-family: var(--cc-mono);
            font-size: .74rem;
        }
        .console-row {
            display: grid;
            grid-template-columns: 5.4rem 5.6rem minmax(7rem, .8fr) 6rem minmax(0, 2fr);
            gap: .7rem;
            align-items: baseline;
            padding: .22rem .6rem;
        }
        .console-row:hover { background: rgba(45,212,167,.05); }
        .c-time { color: var(--cc-faint); }
        .c-src { font-size: .62rem; font-weight: 800; letter-spacing: .1em; }
        .src-mcp { color: var(--cc-cyan); }
        .src-action { color: var(--cc-teal); }
        .src-energyplus { color: var(--cc-amber); }
        .c-label { overflow: hidden; color: var(--cc-ink); text-overflow: ellipsis; }
        .c-status { font-size: .68rem; letter-spacing: .05em; }
        .c-ok { color: var(--cc-teal); }
        .c-warn { color: var(--cc-amber); }
        .c-error { color: var(--cc-red); }
        .c-dim { color: var(--cc-faint); }
        .c-detail {
            overflow: hidden;
            color: var(--cc-dim);
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .pipeline {
            display: flex;
            align-items: stretch;
            gap: .45rem;
            margin: .4rem 0 .9rem;
            overflow-x: auto;
            padding-bottom: .25rem;
        }
        .pipe-step {
            display: grid;
            flex: 1 1 0;
            gap: .12rem;
            min-width: 138px;
            padding: .7rem .8rem;
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        .pipe-step span {
            color: var(--cc-faint);
            font-size: .6rem;
            font-weight: 800;
            letter-spacing: .12em;
        }
        .pipe-step strong { color: var(--cc-ink); font-size: 1.15rem; letter-spacing: -.02em; }
        .pipe-step small {
            overflow: hidden;
            color: var(--cc-dim);
            font-size: .66rem;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .pipe-step em {
            color: var(--cc-faint);
            font-size: .62rem;
            font-style: normal;
            letter-spacing: .08em;
            text-transform: uppercase;
        }
        .step-on { border-color: rgba(45,212,167,.3); }
        .step-on span, .step-on em { color: var(--cc-teal); }
        .pipe-arrow {
            align-self: center;
            flex: 0 0 auto;
            color: var(--cc-faint);
            font-size: 1rem;
        }

        .telemetry-banner {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin: 0 0 .9rem;
            padding: .66rem .9rem;
            border: 1px solid var(--cc-line);
            border-radius: 10px;
            background: var(--cc-panel-2);
        }
        .telemetry-banner > div { display: flex; align-items: center; gap: .6rem; }
        .telemetry-banner strong {
            color: var(--cc-ink);
            font-size: .7rem;
            letter-spacing: .09em;
        }
        .telemetry-banner p { margin: 0; color: var(--cc-dim); font-size: .78rem; }
        .banner-live { border-color: rgba(45,212,167,.35); }
        .banner-replay { border-color: rgba(56,189,248,.35); }

        .pair-strip {
            display: grid;
            grid-template-columns: minmax(0,1fr) auto minmax(0,1fr) auto;
            align-items: center;
            gap: 1rem;
            margin: .3rem 0 .8rem;
            padding: .9rem 1rem;
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        .pair-strip > div:not(.pair-arrow):not(.pair-meta) {
            display: grid;
            gap: .18rem;
            min-width: 0;
        }
        .pair-strip span {
            color: var(--cc-faint);
            font-size: .62rem;
            font-weight: 800;
            letter-spacing: .1em;
        }
        .pair-strip strong {
            overflow: hidden;
            color: var(--cc-ink);
            font-family: var(--cc-mono);
            font-size: .76rem;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .pair-arrow { color: var(--cc-teal); font-size: 1.3rem; }
        .pair-meta { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .4rem; }
        .pair-meta span {
            padding: .28rem .5rem;
            border: 1px solid var(--cc-line);
            border-radius: 999px;
            background: var(--cc-panel-2);
            letter-spacing: .03em;
        }

        .outcome-banner {
            margin: .5rem 0 .9rem;
            padding: .85rem 1rem;
            border: 1px solid var(--cc-line);
            border-left: 4px solid var(--cc-cyan);
            border-radius: 10px;
            background: var(--cc-panel);
        }
        .outcome-banner span {
            display: block;
            color: var(--cc-ink);
            font-size: .98rem;
            font-weight: 750;
        }
        .outcome-banner p { margin: .2rem 0 0; color: var(--cc-dim); font-size: .82rem; }
        .outcome-positive { border-left-color: var(--cc-teal); }
        .outcome-caution { border-left-color: var(--cc-amber); }

        .decision-summary {
            margin: .7rem 0 .8rem;
            padding: .95rem 1.05rem;
            border: 1px solid var(--cc-line-2);
            border-left: 4px solid var(--cc-teal);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        .decision-kicker {
            display: block;
            margin-bottom: .25rem;
            color: var(--cc-faint);
            font-size: .62rem;
            font-weight: 800;
            letter-spacing: .1em;
        }
        .decision-summary strong { display: block; color: var(--cc-ink); font-size: 1.05rem; }
        .decision-summary p { margin: .3rem 0; color: var(--cc-dim); font-size: .84rem; }
        .decision-summary small { color: var(--cc-faint); font-family: var(--cc-mono); }

        [data-testid="stMetric"] {
            min-height: 96px;
            padding: .8rem .9rem;
            border: 1px solid var(--cc-line);
            border-top: 2px solid rgba(45,212,167,.35);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        [data-testid="stMetricLabel"],
        [data-testid="stMetricLabel"] p {
            color: var(--cc-dim) !important;
            font-size: .74rem;
            font-weight: 650;
        }
        [data-testid="stMetricValue"] {
            color: var(--cc-ink) !important;
            font-weight: 760;
            letter-spacing: -.03em;
        }
        [data-testid="stMetricDelta"] { font-size: .72rem; }

        [data-testid="stDataFrame"],
        [data-testid="stPlotlyChart"] {
            overflow: hidden;
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        [data-testid="stExpander"] details {
            border: 1px solid var(--cc-line);
            border-radius: 12px;
            background: var(--cc-panel);
        }
        [data-testid="stExpander"] summary { color: var(--cc-dim); }
        [data-testid="stAlert"] {
            border: 1px solid var(--cc-line-2);
            border-radius: 10px;
            background: var(--cc-panel);
            color: var(--cc-ink);
        }
        [data-testid="stAlert"] p { color: var(--cc-ink) !important; }
        [data-baseweb="select"] > div {
            border-color: var(--cc-line-2);
            border-radius: 8px;
            background: var(--cc-panel);
        }
        [data-baseweb="select"] div { color: var(--cc-ink); }
        [data-baseweb="select"] svg { fill: var(--cc-dim); }
        .stButton > button { border-radius: 8px; font-weight: 700; }
        .stButton > button[kind="primary"] {
            color: #04211a !important;
            border-color: var(--cc-teal) !important;
            background: var(--cc-teal) !important;
        }
        .stButton > button[kind="primary"] p { color: #04211a !important; }
        [data-testid="stProgress"] > div > div > div > div {
            background-color: var(--cc-teal);
        }

        .ecoloop-footer {
            margin-top: 2.2rem;
            padding-top: 1rem;
            border-top: 1px solid var(--cc-line);
            color: var(--cc-faint);
            font-size: .76rem;
            line-height: 1.6;
            text-align: center;
        }
        .ecoloop-footer strong { color: var(--cc-dim); }

        @media (max-width: 1150px) {
            .cc-idbar { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .cc-id-wide { grid-column: 1 / -1; }
            .cc-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .cc-header { align-items: flex-start; flex-direction: column; }
        }
        @media (max-width: 760px) {
            .block-container { padding: .8rem; }
            .cc-idbar, .cc-grid { grid-template-columns: minmax(0, 1fr); }
            .console-row {
                grid-template-columns: 5rem 5rem minmax(0, 1fr);
            }
            .c-status, .c-detail { display: none; }
            .pair-strip { grid-template-columns: minmax(0,1fr); }
            .pair-arrow { transform: rotate(90deg); }
            .pair-meta { justify-content: flex-start; }
            [data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
            [data-testid="stHorizontalBlock"] > div { min-width: min(100%,280px); flex: 1 1 100%; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
