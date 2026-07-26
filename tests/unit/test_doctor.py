from __future__ import annotations

from pathlib import Path

from ecoloop.config import Settings
from ecoloop.doctor import CheckStatus, run_doctor
from ecoloop.energyplus.discovery import EnergyPlusInstallation


def _installation(tmp_path: Path) -> EnergyPlusInstallation:
    home = tmp_path / "EnergyPlusV26-1-0"
    package = home / "pyenergyplus"
    examples = home / "ExampleFiles"
    package.mkdir(parents=True)
    examples.mkdir()
    paths = {
        "executable": home / "energyplus.exe",
        "dynamic_library": home / "energyplusapi.dll",
        "expand_objects": home / "ExpandObjects.exe",
        "convert_input_format": home / "ConvertInputFormat.exe",
        "schema": home / "Energy+.schema.epJSON",
        "idd": home / "Energy+.idd",
    }
    for path in paths.values():
        path.write_text("fixture\n")
    (package / "api.py").write_text("fixture\n")
    return EnergyPlusInstallation(
        home=home,
        executable=paths["executable"],
        version="26.1.0",
        pyenergyplus_parent=home,
        dynamic_library=paths["dynamic_library"],
        expand_objects=paths["expand_objects"],
        convert_input_format=paths["convert_input_format"],
        schema=paths["schema"],
        idd=paths["idd"],
        example_files=examples,
        source="test",
    )


def test_doctor_reports_actionable_ready_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    model = tmp_path / "building.idf"
    weather = tmp_path / "weather.epw"
    model.write_text("Version,26.1;\n")
    weather.write_text("LOCATION,fixture\n")
    settings = Settings(
        ENERGYPLUS_HOME=tmp_path,
        ECOLOOP_MODEL_PATH=model,
        ECOLOOP_WEATHER_PATH=weather,
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ECOLOOP_SUBMISSION_DIR=tmp_path / "submission",
        OLLAMA_MODEL="qwen3:8b",
    )
    monkeypatch.setattr("ecoloop.doctor.discover_energyplus", lambda _: _installation(tmp_path))
    monkeypatch.setattr("ecoloop.doctor._ollama_tags", lambda _: (("qwen3:8b",), ""))
    monkeypatch.setattr("ecoloop.doctor.shutil.which", lambda _: "C:/tools/tool.exe")
    report = run_doctor(settings)
    assert report.ok
    assert report.exit_code == 0
    assert "Doctor result: READY" in report.render_text()


def test_doctor_missing_energyplus_and_weather_is_blocked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = Settings(
        ENERGYPLUS_HOME=tmp_path / "missing",
        ECOLOOP_MODEL_PATH=tmp_path / "missing.idf",
        ECOLOOP_WEATHER_PATH=tmp_path / "missing.epw",
        ECOLOOP_RUNS_DIR=tmp_path / "runs",
        ECOLOOP_SUBMISSION_DIR=tmp_path / "submission",
    )
    monkeypatch.setattr("ecoloop.doctor.discover_energyplus", lambda _: None)
    monkeypatch.setattr("ecoloop.doctor._ollama_tags", lambda _: (None, "connection refused"))
    monkeypatch.setattr("ecoloop.doctor.shutil.which", lambda _: None)
    report = run_doctor(settings)
    assert not report.ok
    assert any(
        check.name == "EnergyPlus executable"
        and check.status is CheckStatus.FAIL
        and "ENERGYPLUS_HOME" in check.fix
        for check in report.checks
    )
    assert any(check.name == "Weather file" and check.fix for check in report.checks)
