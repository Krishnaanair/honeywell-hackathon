"""Actionable environment diagnostics for EcoLoop's external dependencies."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ecoloop import ENERGYPLUS_VERSION
from ecoloop.config import Settings, get_settings, repository_root
from ecoloop.energyplus.discovery import EnergyPlusInstallation, discover_energyplus


class CheckStatus(StrEnum):
    """Doctor check outcome."""

    PASS = "PASS"  # noqa: S105 - status label, not a credential
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One diagnostic with an actionable remediation."""

    name: str
    status: CheckStatus
    detail: str
    fix: str = ""
    required: bool = True
    path: Path | None = None

    @property
    def ok(self) -> bool:
        """Return whether this check does not block a real run."""

        return self.status is not CheckStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible diagnostic."""

        payload = asdict(self)
        payload["status"] = self.status.value
        if self.path is not None:
            payload["path"] = str(self.path)
        return payload


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Complete dependency and write-access report."""

    checks: tuple[DoctorCheck, ...]
    installation: EnergyPlusInstallation | None

    @property
    def ok(self) -> bool:
        """Return whether all required checks pass."""

        return all(check.ok or not check.required for check in self.checks)

    @property
    def exit_code(self) -> int:
        """Return a CLI-compatible exit code."""

        return 0 if self.ok else 1

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible report."""

        return {
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "energyplus_home": (
                str(self.installation.home) if self.installation is not None else None
            ),
        }

    def render_text(self) -> str:
        """Render deterministic plain text with fixes next to failures."""

        lines: list[str] = []
        for check in self.checks:
            lines.append(f"[{check.status.value}] {check.name}: {check.detail}")
            if check.fix and check.status is not CheckStatus.PASS:
                lines.append(f"  Fix: {check.fix}")
        lines.append("Doctor result: READY" if self.ok else "Doctor result: BLOCKED")
        return "\n".join(lines)


def _pass(name: str, detail: str, path: Path | None = None) -> DoctorCheck:
    return DoctorCheck(name, CheckStatus.PASS, detail, path=path)


def _fail(
    name: str,
    detail: str,
    fix: str,
    *,
    path: Path | None = None,
    required: bool = True,
) -> DoctorCheck:
    return DoctorCheck(name, CheckStatus.FAIL, detail, fix, required, path)


def _warn(name: str, detail: str, fix: str = "") -> DoctorCheck:
    return DoctorCheck(name, CheckStatus.WARN, detail, fix, required=False)


def _write_check(name: str, path: Path) -> DoctorCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".ecoloop-write-check-{os.getpid()}"
        probe.write_text("write check\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return _fail(
            name,
            f"Cannot write to {path}: {exc}",
            f"Grant the current user write access to {path} or configure a writable directory.",
            path=path,
        )
    return _pass(name, f"Writable: {path}", path)


def _find_uv() -> Path | None:
    executable = shutil.which("uv")
    if executable:
        return Path(executable).resolve()
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / ("uv.exe" if os.name == "nt" else "uv"),
        home / ".cargo" / "bin" / ("uv.exe" if os.name == "nt" else "uv"),
    ]
    if platform.system().casefold() == "windows":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates.extend(
            (
                local_app_data / "Microsoft" / "WinGet" / "Links" / "uv.exe",
                local_app_data / "Programs" / "uv" / "uv.exe",
            )
        )
        package_root = local_app_data / "Microsoft" / "WinGet" / "Packages"
        if package_root.is_dir():
            candidates.extend(package_root.glob("astral-sh.uv_*/*/uv.exe"))
            candidates.extend(package_root.glob("astral-sh.uv_*/uv.exe"))
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _ollama_tags(host: str) -> tuple[tuple[str, ...] | None, str]:
    host_error = _local_ollama_host_error(host)
    if host_error is not None:
        return None, host_error
    endpoint = host.rstrip("/") + "/api/tags"
    request = urllib.request.Request(  # noqa: S310 - scheme restricted above
        endpoint,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return None, str(exc)
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return (), "Ollama returned no model list"
    names: list[str] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        name = model.get("name") or model.get("model")
        if isinstance(name, str):
            names.append(name)
    return tuple(names), ""


def _local_ollama_host_error(host: str) -> str | None:
    """Return why an Ollama URL violates the local-only runtime boundary."""

    try:
        parsed = urllib.parse.urlparse(host)
        _ = parsed.port
    except ValueError:
        return "OLLAMA_HOST contains an invalid port"
    if parsed.scheme not in {"http", "https"}:
        return "OLLAMA_HOST must use http:// or https://"
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "OLLAMA_HOST must target a loopback address"
    if parsed.username or parsed.password:
        return "OLLAMA_HOST must not contain credentials"
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return "OLLAMA_HOST must not contain a path, query, or fragment"
    return None


def _path_from_setting(settings: Settings, method: str, fallback: Path) -> Path:
    value = getattr(settings, method, None)
    return Path(value()) if callable(value) else fallback


def run_doctor(settings: Settings | None = None) -> DoctorReport:
    """Inspect EnergyPlus, assets, Ollama, and writable output directories."""

    resolved_settings = settings or get_settings()
    checks: list[DoctorCheck] = []
    if sys.version_info[:2] == (3, 11):
        checks.append(_pass("Python", f"Python {sys.version.split()[0]}"))
    else:
        checks.append(
            _fail(
                "Python",
                f"Python {sys.version.split()[0]} is active; Python 3.11 is required.",
                "Install Python 3.11 and create the environment with "
                "`py -3.11 -m venv .venv` (Windows) or `python3.11 -m venv .venv`.",
            )
        )

    uv_path = _find_uv()
    if uv_path:
        checks.append(_pass("uv", f"Found {uv_path}", Path(uv_path)))
    else:
        checks.append(
            _warn(
                "uv",
                "uv is not installed; pip-compatible setup remains available.",
                "Install uv from https://docs.astral.sh/uv/ or use the documented pip commands.",
            )
        )

    installation = discover_energyplus(resolved_settings)
    if installation is None:
        energyplus_fix = (
            "Install the official EnergyPlus 26.1.0 distribution, then set "
            "ENERGYPLUS_HOME to its installation directory or add energyplus to PATH."
        )
        for name in (
            "EnergyPlus executable",
            "EnergyPlus version",
            "pyenergyplus package",
            "EnergyPlus dynamic library",
            "ExpandObjects",
            "ConvertInputFormat and schema",
        ):
            checks.append(_fail(name, "EnergyPlus was not discovered.", energyplus_fix))
    else:
        if installation.executable:
            checks.append(
                _pass(
                    "EnergyPlus executable",
                    f"Found via {installation.source}: {installation.executable}",
                    installation.executable,
                )
            )
        else:
            checks.append(
                _fail(
                    "EnergyPlus executable",
                    f"No executable was found below {installation.home}.",
                    "Reinstall the complete official EnergyPlus 26.1.0 distribution.",
                    path=installation.home,
                )
            )
        if installation.is_version_match:
            checks.append(_pass("EnergyPlus version", f"EnergyPlus {installation.version}"))
        else:
            checks.append(
                _fail(
                    "EnergyPlus version",
                    f"Found {installation.version or 'unknown'}; required {ENERGYPLUS_VERSION}.",
                    "Install EnergyPlus 26.1.0 and point ENERGYPLUS_HOME to that release.",
                )
            )
        if installation.pyenergyplus_package:
            checks.append(
                _pass(
                    "pyenergyplus package",
                    f"Found {installation.pyenergyplus_package}",
                    installation.pyenergyplus_package,
                )
            )
        else:
            checks.append(
                _fail(
                    "pyenergyplus package",
                    f"Not found below {installation.home}.",
                    "Use the complete EnergyPlus 26.1.0 installer/archive; pyenergyplus is "
                    "distributed with EnergyPlus and is not a normal pip dependency.",
                )
            )
        if installation.dynamic_library:
            checks.append(
                _pass(
                    "EnergyPlus dynamic library",
                    f"Found {installation.dynamic_library}",
                    installation.dynamic_library,
                )
            )
        else:
            checks.append(
                _fail(
                    "EnergyPlus dynamic library",
                    f"EnergyPlusAPI library not found below {installation.home}.",
                    "Reinstall the official 26.1.0 distribution for this operating system.",
                )
            )
        if installation.expand_objects:
            checks.append(
                _pass(
                    "ExpandObjects",
                    f"Found {installation.expand_objects}",
                    installation.expand_objects,
                )
            )
        else:
            checks.append(
                _fail(
                    "ExpandObjects",
                    f"Not found below {installation.home}.",
                    "Reinstall the complete EnergyPlus 26.1.0 distribution.",
                )
            )
        if installation.convert_input_format and installation.schema:
            checks.append(
                _pass(
                    "ConvertInputFormat and schema",
                    f"Found {installation.convert_input_format} and {installation.schema}",
                    installation.convert_input_format,
                )
            )
        else:
            checks.append(
                _fail(
                    "ConvertInputFormat and schema",
                    f"Structured model tooling is incomplete below {installation.home}.",
                    "Reinstall EnergyPlus 26.1.0; EcoLoop will not patch IDF text with regex.",
                )
            )

    root = repository_root()
    model_path = _path_from_setting(
        resolved_settings,
        "resolved_model_path",
        root / "models" / "base" / "building.idf",
    )
    if model_path.is_file():
        checks.append(_pass("Model file", f"Found {model_path}", model_path))
    elif installation is not None and installation.example_files is not None:
        checks.append(
            _warn(
                "Model file",
                f"No base model yet at {model_path}; official examples are available.",
                "Run `python -m ecoloop prepare-model` to select and copy a version-matched "
                "official example.",
            )
        )
    else:
        checks.append(
            _fail(
                "Model file",
                f"Missing {model_path} and no official example inventory is available.",
                "Install EnergyPlus 26.1.0, then run `python -m ecoloop prepare-model`, or "
                "set ECOLOOP_MODEL_PATH to a verified 26.1.0 IDF.",
                path=model_path,
            )
        )

    weather_path = _path_from_setting(
        resolved_settings,
        "resolved_weather_path",
        root / "weather" / "default.epw",
    )
    if weather_path.is_file():
        checks.append(_pass("Weather file", f"Found {weather_path}", weather_path))
    else:
        checks.append(
            _fail(
                "Weather file",
                f"Missing {weather_path}.",
                "Set ECOLOOP_WEATHER_PATH to a verified EPW. Weather files with unclear "
                "licensing are intentionally not bundled.",
                path=weather_path,
            )
        )

    ollama_executable = shutil.which("ollama")
    names, ollama_error = _ollama_tags(resolved_settings.ollama_host)
    if ollama_executable or names is not None:
        detail_parts = []
        if ollama_executable:
            detail_parts.append(f"executable {ollama_executable}")
        if names is not None:
            detail_parts.append(f"API {resolved_settings.ollama_host}")
        checks.append(_pass("Ollama executable or API", "Found " + " and ".join(detail_parts)))
    else:
        checks.append(
            _fail(
                "Ollama executable or API",
                f"Ollama is unavailable ({ollama_error}).",
                "Install Ollama, run `ollama serve`, and confirm OLLAMA_HOST is reachable.",
            )
        )
    if names is None:
        checks.append(
            _fail(
                "Configured Ollama model",
                f"Could not query models from {resolved_settings.ollama_host}.",
                f"Start Ollama, then run `ollama pull {resolved_settings.ollama_model}`.",
            )
        )
    elif resolved_settings.ollama_model in names:
        checks.append(
            _pass(
                "Configured Ollama model",
                f"Found {resolved_settings.ollama_model}",
            )
        )
    else:
        checks.append(
            _fail(
                "Configured Ollama model",
                f"{resolved_settings.ollama_model} is not installed; available: "
                f"{', '.join(names) if names else 'none'}.",
                f"Run `ollama pull {resolved_settings.ollama_model}`.",
            )
        )

    runs_dir = _path_from_setting(resolved_settings, "resolved_runs_dir", root / "runs")
    submission_dir = _path_from_setting(
        resolved_settings,
        "resolved_submission_dir",
        root / "submission",
    )
    checks.append(_write_check("Run directory write access", runs_dir))
    checks.append(_write_check("Submission directory write access", submission_dir))
    return DoctorReport(tuple(checks), installation)
