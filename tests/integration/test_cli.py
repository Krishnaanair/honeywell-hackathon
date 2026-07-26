"""CLI surface tests that do not require external simulation dependencies."""

from typer.testing import CliRunner

from ecoloop import __version__
from ecoloop.cli import app

runner = CliRunner()


def test_cli_help_lists_required_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "doctor",
        "prepare-model",
        "run",
        "compare",
        "dashboard",
        "demo",
        "replay",
        "export",
        "package-submission",
    ):
        assert command in result.stdout


def test_cli_version_reports_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_prepare_model_rejects_unknown_period_before_external_work() -> None:
    result = runner.invoke(app, ["prepare-model", "--period", "unknown"])
    assert result.exit_code == 1
    assert "Unknown period" in result.stderr
