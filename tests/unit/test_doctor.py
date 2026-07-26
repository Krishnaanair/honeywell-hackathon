from __future__ import annotations

import io
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from ecoloop.config import Settings
from ecoloop.doctor import (
    CheckStatus,
    _local_only_ollama_opener,
    _NoOllamaRedirectHandler,
    _ollama_tags,
    run_doctor,
)
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


@pytest.mark.parametrize(
    "host",
    [
        "https://ollama.com",
        "http://example.com:11434",
        "ftp://127.0.0.1:11434",
        "http://" + "user" + ":" + "pass" + "@127.0.0.1:11434",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:invalid",
    ],
)
def test_doctor_rejects_non_local_or_ambiguous_ollama_host_without_network(
    host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_network(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid OLLAMA_HOST must be rejected before network access")

    monkeypatch.setattr("ecoloop.doctor.urllib.request.urlopen", unexpected_network)

    names, error = _ollama_tags(host)

    assert names is None
    assert "OLLAMA_HOST" in error


def test_doctor_opener_ignores_proxy_environment_and_rejects_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.delenv("NO_PROXY", raising=False)

    opener = _local_only_ollama_opener()
    proxy_handlers = [
        handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)
    ]
    redirect_handlers = [
        handler for handler in opener.handlers if isinstance(handler, _NoOllamaRedirectHandler)
    ]

    assert urllib.request.getproxies()["http"] == "http://proxy.example.invalid:8080"
    assert proxy_handlers == []
    assert len(redirect_handlers) == 1

    request = urllib.request.Request("http://127.0.0.1:11434/api/tags")
    with pytest.raises(urllib.error.HTTPError, match="redirect refused"):
        redirect_handlers[0].redirect_request(
            request,
            io.BytesIO(),
            307,
            "Temporary Redirect",
            Message(),
            "https://example.invalid/escaped",
        )


def test_doctor_uses_local_only_opener_without_global_urlopen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class JsonResponse:
        def __enter__(self) -> JsonResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"models":[{"name":"qwen3:8b"}]}'

    class RecordingOpener:
        def __init__(self) -> None:
            self.requests: list[tuple[str, float]] = []

        def open(
            self,
            request: urllib.request.Request,
            *,
            timeout: float,
        ) -> JsonResponse:
            self.requests.append((request.full_url, timeout))
            return JsonResponse()

    opener = RecordingOpener()

    def unexpected_urlopen(*args: Any, **kwargs: Any) -> None:
        pytest.fail(f"global urlopen must not be used: {args!r} {kwargs!r}")

    monkeypatch.setattr("ecoloop.doctor._local_only_ollama_opener", lambda: opener)
    monkeypatch.setattr("ecoloop.doctor.urllib.request.urlopen", unexpected_urlopen)

    names, error = _ollama_tags("http://127.0.0.1:11434")

    assert names == ("qwen3:8b",)
    assert error == ""
    assert opener.requests == [("http://127.0.0.1:11434/api/tags", 2.0)]
