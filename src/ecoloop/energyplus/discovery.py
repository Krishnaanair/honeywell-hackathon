"""Cross-platform discovery for the external EnergyPlus 26.1.0 installation."""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ecoloop import ENERGYPLUS_VERSION
from ecoloop.exceptions import DependencyUnavailableError

_VERSION_PATTERN = re.compile(r"(?<!\d)(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)(?!\d)")


@dataclass(frozen=True, slots=True)
class EnergyPlusInstallation:
    """Paths and version metadata for one EnergyPlus installation."""

    home: Path
    executable: Path | None
    version: str | None
    pyenergyplus_parent: Path | None
    dynamic_library: Path | None
    expand_objects: Path | None
    convert_input_format: Path | None
    schema: Path | None
    idd: Path | None
    example_files: Path | None
    source: str
    version_output: str = ""

    @property
    def pyenergyplus_package(self) -> Path | None:
        """Return the discovered package directory, if present."""

        if self.pyenergyplus_parent is None:
            return None
        package = self.pyenergyplus_parent / "pyenergyplus"
        return package if package.is_dir() else None

    @property
    def is_version_match(self) -> bool:
        """Return whether the discovered version is exactly the supported version."""

        return normalize_version(self.version) == normalize_version(ENERGYPLUS_VERSION)

    @property
    def is_runtime_complete(self) -> bool:
        """Return whether the executable, API package, and dynamic library are present."""

        return bool(
            self.executable
            and self.executable.is_file()
            and self.pyenergyplus_package
            and self.dynamic_library
            and self.dynamic_library.is_file()
            and self.is_version_match
        )

    @property
    def is_model_tooling_complete(self) -> bool:
        """Return whether version-matched structured model tooling is present."""

        return bool(
            self.is_runtime_complete
            and self.convert_input_format
            and self.convert_input_format.is_file()
            and self.schema
            and self.schema.is_file()
        )


def normalize_version(value: str | None) -> tuple[int, int, int] | None:
    """Normalize an EnergyPlus version string to a three-part tuple."""

    if not value:
        return None
    match = _VERSION_PATTERN.search(value)
    if not match:
        return None
    parts = [int(item) for item in match.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]


def parse_version_output(output: str) -> str | None:
    """Extract the first version-like value from EnergyPlus output."""

    match = _VERSION_PATTERN.search(output)
    return match.group(1) if match else None


def _executable_names() -> tuple[str, ...]:
    if os.name == "nt":
        return ("energyplus.exe", "EnergyPlus.exe")
    return ("energyplus", "EnergyPlus")


def _tool_names(base: str) -> tuple[str, ...]:
    return (f"{base}.exe", base) if os.name == "nt" else (base, f"{base}.exe")


def _common_roots() -> tuple[Path, ...]:
    """Return version-specific conventional installation roots."""

    roots: list[Path] = []
    system = platform.system().lower()
    names: tuple[str, ...]
    if system == "windows":
        drives = {Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\")}
        program_files = os.environ.get("PROGRAMFILES")
        if program_files:
            drives.add(Path(program_files))
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            drives.add(Path(user_profile))
        local_programs = os.environ.get("LOCALAPPDATA")
        if local_programs:
            drives.add(Path(local_programs) / "Programs")
        names = ("EnergyPlusV26-1-0", "EnergyPlus-26-1-0", "EnergyPlus26-1-0")
        roots.extend(parent / name for parent in drives for name in names)
    elif system == "darwin":
        names = ("EnergyPlus-26-1-0", "EnergyPlusV26-1-0")
        roots.extend(Path("/Applications") / name for name in names)
        roots.extend(Path("/usr/local") / name for name in names)
        roots.extend(Path("/opt") / name for name in names)
    else:
        names = ("EnergyPlus-26-1-0", "EnergyPlusV26-1-0", "EnergyPlus26-1-0")
        roots.extend(Path("/usr/local") / name for name in names)
        roots.extend(Path("/opt") / name for name in names)
    return tuple(roots)


def candidate_energyplus_roots(explicit_home: Path | None = None) -> tuple[tuple[Path, str], ...]:
    """Build an ordered, de-duplicated list of installation root candidates."""

    raw: list[tuple[Path, str]] = []
    if explicit_home is not None:
        raw.append((explicit_home, "configured ENERGYPLUS_HOME"))
    environment_home = os.environ.get("ENERGYPLUS_HOME")
    if environment_home:
        raw.append((Path(environment_home), "environment ENERGYPLUS_HOME"))

    for name in _executable_names():
        executable = shutil.which(name)
        if executable:
            executable_path = Path(executable)
            raw.extend(
                (
                    (executable_path.parent, "system PATH"),
                    (executable_path.parent.parent, "system PATH"),
                )
            )

    raw.extend((path, "common platform path") for path in _common_roots())
    result: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, source in raw:
        resolved = path.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append((resolved, source))
    return tuple(result)


def _first_existing(paths: Iterable[Path], *, directory: bool = False) -> Path | None:
    for path in paths:
        if path.is_dir() if directory else path.is_file():
            return path.resolve()
    return None


def _find_executable(home: Path) -> Path | None:
    candidates = [
        *(home / name for name in _executable_names()),
        *(home / "bin" / name for name in _executable_names()),
    ]
    return _first_existing(candidates)


def _find_tool(home: Path, name: str) -> Path | None:
    candidates = [
        *(home / item for item in _tool_names(name)),
        *(home / "bin" / item for item in _tool_names(name)),
        *(home / "PreProcess" / item for item in _tool_names(name)),
    ]
    return _first_existing(candidates)


def _find_pyenergyplus_parent(home: Path) -> Path | None:
    candidates = (
        home,
        home / "Python",
        home / "python",
        home / "python_lib",
        home / "api",
    )
    for parent in candidates:
        if (parent / "pyenergyplus" / "api.py").is_file():
            return parent.resolve()
    return None


def _imported_pyenergyplus_parent() -> Path | None:
    try:
        spec = importlib.util.find_spec("pyenergyplus")
    except (ImportError, AttributeError, ValueError):
        return None
    if spec is None or spec.origin is None:
        return None
    package = Path(spec.origin).resolve().parent
    return package.parent if package.name.casefold() == "pyenergyplus" else None


def _dynamic_library_candidates(home: Path) -> tuple[Path, ...]:
    names = (
        "EnergyPlusAPI.dll",
        "energyplusapi.dll",
        "libenergyplusapi.so",
        "libenergyplusapi.dylib",
    )
    parents = (home, home / "bin", home / "lib")
    return tuple(parent / name for parent in parents for name in names)


def _version_for_executable(executable: Path | None) -> tuple[str | None, str]:
    if executable is None:
        return None, ""
    try:
        completed = subprocess.run(  # noqa: S603 - executable is a discovered fixed path
            [str(executable), "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Unable to execute --version: {exc}"
    output = "\n".join(value for value in (completed.stdout, completed.stderr) if value).strip()
    return parse_version_output(output), output


def inspect_energyplus_root(home: Path, source: str = "explicit path") -> EnergyPlusInstallation:
    """Inspect one candidate root without mutating import state."""

    resolved = home.expanduser().resolve()
    executable = _find_executable(resolved)
    version, version_output = _version_for_executable(executable)
    pyenergyplus_parent = _find_pyenergyplus_parent(resolved)
    if pyenergyplus_parent is None:
        pyenergyplus_parent = _imported_pyenergyplus_parent()
    return EnergyPlusInstallation(
        home=resolved,
        executable=executable,
        version=version,
        pyenergyplus_parent=pyenergyplus_parent,
        dynamic_library=_first_existing(_dynamic_library_candidates(resolved)),
        expand_objects=_find_tool(resolved, "ExpandObjects"),
        convert_input_format=_find_tool(resolved, "ConvertInputFormat"),
        schema=_first_existing(
            (
                resolved / "Energy+.schema.epJSON",
                resolved / "EnergyPlus.schema.epJSON",
                resolved / "schema" / "Energy+.schema.epJSON",
            )
        ),
        idd=_first_existing((resolved / "Energy+.idd", resolved / "EnergyPlus.idd")),
        example_files=_first_existing(
            (resolved / "ExampleFiles", resolved / "examples", resolved / "Examples"),
            directory=True,
        ),
        source=source,
        version_output=version_output,
    )


def discover_energyplus(settings: object | None = None) -> EnergyPlusInstallation | None:
    """Discover the best EnergyPlus installation from config, common paths, and PATH."""

    configured = getattr(settings, "energyplus_home", None) if settings is not None else None
    explicit_home = Path(configured) if configured is not None else None
    candidates = candidate_energyplus_roots(explicit_home)
    inspected: list[EnergyPlusInstallation] = []
    for root, source in candidates:
        if not root.is_dir():
            continue
        installation = inspect_energyplus_root(root, source)
        inspected.append(installation)
        if installation.is_runtime_complete:
            return installation

    expected = normalize_version(ENERGYPLUS_VERSION)
    for installation in inspected:
        if normalize_version(installation.version) == expected:
            return installation
    return inspected[0] if inspected else None


def require_energyplus(
    settings: object | None = None,
    *,
    model_tooling: bool = False,
) -> EnergyPlusInstallation:
    """Return a complete, version-matched installation or raise an actionable error."""

    installation = discover_energyplus(settings)
    if installation is None:
        raise DependencyUnavailableError(
            "EnergyPlus 26.1.0 was not found. Set ENERGYPLUS_HOME to the extracted "
            "EnergyPlus 26.1.0 installation directory or add its executable to PATH."
        )
    if not installation.is_version_match:
        actual = installation.version or "unknown"
        raise DependencyUnavailableError(
            f"EnergyPlus {actual} was found at {installation.home}, but 26.1.0 is required. "
            "Install the pinned release and update ENERGYPLUS_HOME."
        )
    missing: list[str] = []
    if installation.executable is None:
        missing.append("EnergyPlus executable")
    if installation.pyenergyplus_package is None:
        missing.append("pyenergyplus package")
    if installation.dynamic_library is None:
        missing.append("EnergyPlus API dynamic library")
    if model_tooling:
        if installation.convert_input_format is None:
            missing.append("ConvertInputFormat")
        if installation.schema is None:
            missing.append("Energy+.schema.epJSON")
    if missing:
        raise DependencyUnavailableError(
            f"EnergyPlus installation at {installation.home} is incomplete: "
            f"{', '.join(missing)}. Reinstall the official EnergyPlus 26.1.0 distribution."
        )
    return installation


def add_pyenergyplus_to_path(installation: EnergyPlusInstallation) -> None:
    """Add the official installation package parent to Python's import path."""

    parent = installation.pyenergyplus_parent
    if parent is None:
        raise DependencyUnavailableError(
            f"pyenergyplus was not found below {installation.home}; reinstall EnergyPlus 26.1.0."
        )
    parent_text = str(parent)
    if parent_text not in sys.path:
        sys.path.insert(0, parent_text)
