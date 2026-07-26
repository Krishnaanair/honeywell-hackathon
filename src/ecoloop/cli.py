"""Command-line interface for EcoLoop operations and reproducibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import fields
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from ecoloop import __version__
from ecoloop.config import Settings, load_file_config, repository_root
from ecoloop.doctor import run_doctor
from ecoloop.energyplus.model import prepare_models
from ecoloop.reporting import export_run, package_submission

app = typer.Typer(
    name="ecoloop",
    help="Guardrailed local supervisory control for EnergyPlus 26.1.0 buildings.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()
error_console = Console(stderr=True)


class RunMode(StrEnum):
    """Run modes exposed by the public CLI."""

    BASELINE = "baseline"
    RULE = "rule"
    AGENT = "agent"


def _settings() -> Settings:
    return Settings()


def _emit_json(payload: Any) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=True, default=str))


def _fail(message: str, *, code: int = 1) -> None:
    error_console.print(f"[red]ERROR:[/red] {message}")
    raise typer.Exit(code)


@app.command()
def doctor(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a machine-readable report.")
    ] = False,
) -> None:
    """Check every required local dependency and configured asset."""

    report = run_doctor(_settings())
    if as_json:
        _emit_json(report.to_dict())
    else:
        console.print(report.render_text())
    if not report.ok:
        raise typer.Exit(report.exit_code)


@app.command("prepare-model")
def prepare_model(
    period: Annotated[str, typer.Option(help="Configured simulation period.")] = "smoke",
    source: Annotated[
        Path | None,
        typer.Option(
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
            help="Optional explicit EnergyPlus 26.1.0 source IDF.",
        ),
    ] = None,
) -> None:
    """Generate schema-validated baseline, controlled, and replay models."""

    periods = load_file_config().periods
    if period not in periods:
        _fail(f"Unknown period {period!r}; choose one of: {', '.join(sorted(periods))}")
    try:
        artifacts = prepare_models(_settings(), periods[period], source)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    table = Table(title=f"Prepared EnergyPlus models ({period})")
    table.add_column("Artifact")
    table.add_column("Path")
    for field in fields(artifacts):
        table.add_row(field.name.replace("_", " "), str(getattr(artifacts, field.name)))
    console.print(table)


@app.command("run")
def run_case_command(
    mode: Annotated[RunMode, typer.Argument(help="Controller mode.")],
    period: Annotated[str, typer.Option(help="Configured simulation period.")] = "smoke",
    fake: Annotated[
        bool,
        typer.Option(
            "--fake",
            help="Run the explicitly labelled deterministic test plant, never production data.",
        ),
    ] = False,
    display_delay_seconds: Annotated[
        float,
        typer.Option(
            min=0.0,
            max=10.0,
            help="Wall-clock display delay; simulated timestamps and energy are unchanged.",
        ),
    ] = 0.0,
) -> None:
    """Run a baseline, deterministic-rule, or local-model EnergyPlus case."""

    try:
        from ecoloop.coordinator import run_case

        result = run_case(
            mode.value,
            period_name=period,
            settings=_settings(),
            fake=fake,
            display_delay_seconds=display_delay_seconds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    _emit_json(result)
    if str(result.get("status")) != "completed":
        raise typer.Exit(1)


@app.command()
def compare(
    baseline_run_id: Annotated[str, typer.Argument(help="Completed real baseline run ID.")],
    controlled_run_id: Annotated[str, typer.Argument(help="Completed real agent/rule run ID.")],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Optional comparison export directory.",
        ),
    ] = None,
) -> None:
    """Compare two compatible completed real runs from verified official results."""

    try:
        from ecoloop.db.store import SQLiteStore
        from ecoloop.evaluation import compare_and_write

        result = compare_and_write(
            SQLiteStore(_settings().resolved_database_path()),
            baseline_run_id,
            controlled_run_id,
            output_directory=output_dir,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    _emit_json(
        {
            "comparison": result.comparison.model_dump(mode="json"),
            "comparison_json": str(result.json_path),
            "comparison_csv": str(result.csv_path),
        }
    )


@app.command()
def dashboard(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8501,
    address: Annotated[str, typer.Option()] = "127.0.0.1",
    include_fake: Annotated[
        bool,
        typer.Option(
            "--include-fake",
            help="Expose explicitly labelled test runs in the dashboard selector.",
        ),
    ] = False,
) -> None:
    """Launch the real-data Streamlit operations dashboard."""

    environment = os.environ.copy()
    environment["ECOLOOP_DASHBOARD_INCLUDE_FAKE"] = "1" if include_fake else "0"
    dashboard_path = repository_root() / "src" / "ecoloop" / "dashboard" / "app.py"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(dashboard_path),
        "--server.address",
        address,
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    console.print(f"Dashboard: http://{address}:{port}")
    completed = subprocess.run(command, env=environment, check=False)  # noqa: S603
    if completed.returncode:
        raise typer.Exit(completed.returncode)


@app.command()
def demo(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8501,
    display_delay_seconds: Annotated[
        float | None,
        typer.Option(
            min=0.0,
            max=10.0,
            help="Optional visual pacing without changing simulated time.",
        ),
    ] = None,
) -> None:
    """Run checks, baseline reuse, dashboard, MCP, Ollama, and controlled simulation."""

    try:
        from ecoloop.demo import run_demo

        run_demo(
            settings=_settings(),
            dashboard_port=port,
            display_delay_seconds=display_delay_seconds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))


@app.command()
def replay(
    run_id: Annotated[str, typer.Argument(help="Completed real controlled run to replay.")],
) -> None:
    """Replay a validated real action schedule without local model inference."""

    try:
        from ecoloop.coordinator import replay_run

        result = replay_run(run_id, settings=_settings())
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    _emit_json(result)
    if str(result.get("status")) != "completed":
        raise typer.Exit(1)


@app.command("export")
def export_command(
    run_id: Annotated[str, typer.Argument(help="Run ID to export.")],
    output_dir: Annotated[
        Path | None,
        typer.Option(file_okay=False, dir_okay=True, resolve_path=True),
    ] = None,
) -> None:
    """Export telemetry, decisions, metrics, replay inputs, and artifact manifest."""

    settings = _settings()
    try:
        output = export_run(
            settings.resolved_database_path(),
            settings.resolved_runs_dir(),
            run_id,
            output_dir=output_dir,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    console.print(str(output))


@app.command("package-submission")
def package_submission_command() -> None:
    """Build the source ZIP, verified PDFs, checksums, and submission manifest."""

    try:
        result = package_submission(_settings())
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(str(exc))
    _emit_json(result)


@app.command("mcp-server", hidden=True)
def mcp_server() -> None:
    """Run the confined production FastMCP server over stdio."""

    from ecoloop.mcp.server import main

    main()


@app.command()
def version() -> None:
    """Print the EcoLoop package version."""

    console.print(__version__)


if __name__ == "__main__":
    app()
