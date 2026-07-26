"""Managed live-demo orchestration for the real EcoLoop control pipeline."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from ecoloop import ENERGYPLUS_VERSION
from ecoloop.config import Settings, load_file_config, repository_root
from ecoloop.db.store import SQLiteStore
from ecoloop.doctor import run_doctor
from ecoloop.evaluation import EvaluationError, load_verified_final_metrics
from ecoloop.schemas import RunRecord, RunStatus, RunType

_RUN_DISCOVERY_TIMEOUT_SECONDS = 60.0
_PROCESS_POLL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class DemoResult:
    """Identities and endpoints created by one managed demo session."""

    baseline_run_id: str
    agent_run_id: str
    dashboard_url: str
    interrupted: bool


def run_demo(
    *,
    settings: Settings | None = None,
    dashboard_port: int = 8501,
    display_delay_seconds: float | None = None,
    period_name: str = "demo",
    hold_dashboard_after_run: bool = True,
    console: Console | None = None,
) -> DemoResult:
    """Run preflight, a verified baseline, dashboard, and live agent simulation.

    The controlled run is always a real EnergyPlus run. Its agent host starts
    the confined MCP server as a private stdio child and communicates with it
    through the official client protocol.
    """

    if not 1 <= dashboard_port <= 65_535:
        raise ValueError("dashboard_port must be in 1..65535")
    configured_periods = load_file_config().periods
    if period_name not in configured_periods:
        choices = ", ".join(sorted(configured_periods))
        raise ValueError(f"unknown demo period {period_name!r}; choose one of: {choices}")

    runtime_settings = settings or Settings()
    delay = (
        runtime_settings.demo_display_delay_seconds
        if display_delay_seconds is None
        else display_delay_seconds
    )
    if not 0 <= delay <= 10:
        raise ValueError("display_delay_seconds must be in 0..10")
    output = console or Console()

    report = run_doctor(runtime_settings)
    output.print(report.render_text())
    if not report.ok:
        raise RuntimeError("live demo is blocked by failed doctor checks")

    store = SQLiteStore(runtime_settings.resolved_database_path())
    baseline = _find_reusable_baseline(store, runtime_settings, period_name)
    if baseline is None:
        output.print(f"Running a verified real baseline for period {period_name!r}...")
        from ecoloop.coordinator import run_case

        baseline_result = run_case(
            "baseline",
            period_name=period_name,
            settings=runtime_settings,
            fake=False,
        )
        if baseline_result.get("status") != "completed" or not bool(
            (baseline_result.get("evaluation") or {}).get(
                "verified_for_comparison",
                False,
            )
        ):
            raise RuntimeError("real baseline did not complete with verified official metrics")
        baseline_run_id = str(baseline_result["run_id"])
    else:
        baseline_run_id = baseline.run_id
        output.print(f"Reusing verified baseline: {baseline_run_id}")

    previous_agent_ids = {
        run.run_id
        for run in store.list_runs(
            run_type=RunType.AGENT,
            include_fake=False,
            limit=10_000,
        )
    }
    dashboard_url = f"http://127.0.0.1:{dashboard_port}"
    dashboard_process = _start_dashboard(
        runtime_settings,
        dashboard_port=dashboard_port,
    )
    agent_process: subprocess.Popen[bytes] | None = None
    agent_run_id: str | None = None
    interrupted = False
    try:
        _wait_for_dashboard_start(dashboard_process)
        agent_process = _start_agent_run(
            runtime_settings,
            period_name=period_name,
            display_delay_seconds=delay,
        )
        agent_run = _wait_for_new_agent_run(
            store,
            previous_agent_ids=previous_agent_ids,
            process=agent_process,
        )
        agent_run_id = agent_run.run_id
        _write_current_run(runtime_settings.resolved_runs_dir(), agent_run_id)

        output.print(f"Dashboard: {dashboard_url}")
        output.print(f"Baseline run: {baseline_run_id}")
        output.print(f"Controlled run: {agent_run_id}")
        output.print("MCP: private stdio server connected by the local supervisory host")
        output.print(f"Ollama: {runtime_settings.ollama_host} ({runtime_settings.ollama_model})")
        output.print("Press Ctrl+C to stop the demo and all child processes.")

        return_code = _wait_for_agent(agent_process, dashboard_process)
        if return_code != 0:
            raise RuntimeError(
                f"controlled EnergyPlus agent process exited with code {return_code}"
            )
        completed = store.get_run(agent_run_id)
        if completed is None or completed.status is not RunStatus.COMPLETED:
            status = completed.status.value if completed is not None else "missing"
            raise RuntimeError(f"controlled run did not complete cleanly ({status})")

        if hold_dashboard_after_run:
            output.print("Controlled run completed. Dashboard remains available until Ctrl+C.")
            _hold_dashboard(dashboard_process)
    except KeyboardInterrupt:
        interrupted = True
        output.print("Stopping EcoLoop demo...")
    finally:
        if agent_process is not None:
            _stop_process_tree(agent_process)
        _stop_process_tree(dashboard_process)
        if interrupted and agent_run_id is not None:
            _mark_cancelled_if_active(store, agent_run_id)

    if agent_run_id is None:
        raise RuntimeError("agent run ended before a run ID was registered")
    return DemoResult(
        baseline_run_id=baseline_run_id,
        agent_run_id=agent_run_id,
        dashboard_url=dashboard_url,
        interrupted=interrupted,
    )


def _find_reusable_baseline(
    store: SQLiteStore,
    settings: Settings,
    period_name: str,
) -> RunRecord | None:
    expected_weather = str(settings.resolved_weather_path())
    for run in store.list_runs(
        status=RunStatus.COMPLETED,
        run_type=RunType.BASELINE,
        include_fake=False,
        limit=10_000,
    ):
        if (
            run.period_name != period_name
            or run.energyplus_version != ENERGYPLUS_VERSION
            or run.weather_path != expected_weather
        ):
            continue
        try:
            load_verified_final_metrics(store, run.run_id)
        except EvaluationError:
            continue
        return run
    return None


def _child_environment(settings: Settings) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "ECOLOOP_DATABASE_PATH": str(settings.resolved_database_path()),
            "ECOLOOP_RUNS_DIR": str(settings.resolved_runs_dir()),
            "ECOLOOP_MODEL_PATH": str(settings.resolved_model_path()),
            "ECOLOOP_WEATHER_PATH": str(settings.resolved_weather_path()),
            "OLLAMA_HOST": settings.ollama_host,
            "OLLAMA_MODEL": settings.ollama_model,
            "ECOLOOP_DASHBOARD_INCLUDE_FAKE": "0",
        }
    )
    if settings.energyplus_home is not None:
        environment["ENERGYPLUS_HOME"] = str(settings.energyplus_home)
    return environment


def _start_dashboard(
    settings: Settings,
    *,
    dashboard_port: int,
) -> subprocess.Popen[bytes]:
    dashboard_path = repository_root() / "src" / "ecoloop" / "dashboard" / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(dashboard_port),
        "--browser.gatherUsageStats",
        "false",
        "--server.headless",
        "true",
    ]
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
            command,
            cwd=repository_root(),
            env=_child_environment(settings),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
        command,
        cwd=repository_root(),
        env=_child_environment(settings),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _start_agent_run(
    settings: Settings,
    *,
    period_name: str,
    display_delay_seconds: float,
) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "ecoloop",
        "run",
        "agent",
        "--period",
        period_name,
        "--display-delay-seconds",
        str(display_delay_seconds),
    ]
    if os.name == "nt":
        return subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
            command,
            cwd=repository_root(),
            env=_child_environment(settings),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 - fixed local executable and arguments
        command,
        cwd=repository_root(),
        env=_child_environment(settings),
        start_new_session=True,
    )


def _wait_for_dashboard_start(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 2.0,
) -> None:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"dashboard exited during startup with code {return_code}")
        time.sleep(0.05)


def _wait_for_new_agent_run(
    store: SQLiteStore,
    *,
    previous_agent_ids: set[str],
    process: subprocess.Popen[bytes],
    timeout_seconds: float = _RUN_DISCOVERY_TIMEOUT_SECONDS,
) -> RunRecord:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        candidates = store.list_runs(
            run_type=RunType.AGENT,
            include_fake=False,
            limit=10_000,
        )
        match = next(
            (run for run in candidates if run.run_id not in previous_agent_ids),
            None,
        )
        if match is not None:
            return match
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(
                f"agent process exited before registering a real run (code {return_code})"
            )
        time.sleep(_PROCESS_POLL_SECONDS)
    raise RuntimeError("timed out waiting for the controlled run ID")


def _write_current_run(runs_directory: Path, run_id: str) -> Path:
    runs_directory.mkdir(parents=True, exist_ok=True)
    destination = runs_directory / "current_run.txt"
    temporary = runs_directory / f".current-run-{os.getpid()}.tmp"
    temporary.write_text(run_id + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def _wait_for_agent(
    agent_process: subprocess.Popen[bytes],
    dashboard_process: subprocess.Popen[bytes],
) -> int:
    while True:
        dashboard_code = dashboard_process.poll()
        if dashboard_code is not None:
            raise RuntimeError(f"dashboard exited unexpectedly with code {dashboard_code}")
        agent_code = agent_process.poll()
        if agent_code is not None:
            return agent_code
        time.sleep(_PROCESS_POLL_SECONDS)


def _hold_dashboard(process: subprocess.Popen[bytes]) -> None:
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"dashboard exited unexpectedly with code {return_code}")
        time.sleep(_PROCESS_POLL_SECONDS)


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            subprocess.run(  # noqa: S603 - exact child PID created above
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except OSError:
            process.terminate()
    else:
        try:
            os.kill(-process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _mark_cancelled_if_active(store: SQLiteStore, run_id: str) -> None:
    run = store.get_run(run_id)
    if run is not None and run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
        store.set_run_status(
            run_id,
            RunStatus.CANCELLED,
            error_summary="demo interrupted by operator",
        )


__all__ = ["DemoResult", "run_demo"]
