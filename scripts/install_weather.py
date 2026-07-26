"""Install the checksummed default EPW from EnergyPlus 26.1.0.

The preferred path copies the weather file from an existing EnergyPlus
installation. With ``--download``, the script downloads the exact official
EnergyPlus release archive, verifies its published SHA-256, and extracts only
the known EPW member.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path

RELEASE_URL = (
    "https://github.com/NatLabRockies/EnergyPlus/releases/download/"
    "v26.1.0/EnergyPlus-26.1.0-6f2e40d102-Windows-x86_64.zip"
)
RELEASE_SHA256 = "0bb6932d277eed62f996b625f37c533b8c35f9af0c53710d961d8442fc4e70b3"
WEATHER_MEMBER = "WeatherData/USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.epw"
WEATHER_SHA256 = "c7d4efcf93ba316a1d874352e743df5cf137ba5c0e3459eb2dc4b5442d5b7f5c"


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def candidate_roots(explicit: Path | None) -> Iterable[Path]:
    """Yield configured and common EnergyPlus installation roots."""

    if explicit is not None:
        yield explicit.expanduser()
    configured = os.environ.get("ENERGYPLUS_HOME")
    if configured:
        yield Path(configured).expanduser()
    yield Path("C:/EnergyPlusV26-1-0")
    yield Path.home() / "EnergyPlusV26-1-0"
    yield Path("/usr/local/EnergyPlus-26-1-0")
    yield Path("/Applications/EnergyPlus-26-1-0")


def find_installed_weather(explicit: Path | None) -> Path | None:
    """Return the first exact installed EPW whose checksum matches."""

    for root in candidate_roots(explicit):
        candidate = root.resolve() / Path(WEATHER_MEMBER)
        if candidate.is_file() and sha256_file(candidate) == WEATHER_SHA256:
            return candidate
    return None


def install_from_path(source: Path, destination: Path, *, force: bool) -> Path:
    """Copy a verified EPW to the requested destination."""

    if sha256_file(source) != WEATHER_SHA256:
        raise RuntimeError(f"EPW checksum mismatch: {source}")
    destination = destination.expanduser().resolve()
    if destination.exists():
        if sha256_file(destination) == WEATHER_SHA256:
            return destination
        if not force:
            raise RuntimeError(
                f"Refusing to overwrite non-matching file: {destination}; use --force."
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copy2(source, temporary)
    if sha256_file(temporary) != WEATHER_SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Copied EPW failed checksum verification.")
    temporary.replace(destination)
    return destination


def download_release_weather(destination: Path, *, force: bool) -> Path:
    """Download, verify, and extract only the expected EPW archive member."""

    with tempfile.TemporaryDirectory(prefix="ecoloop-weather-") as work:
        archive = Path(work) / "energyplus-26.1.0.zip"
        request = urllib.request.Request(
            RELEASE_URL,
            headers={"User-Agent": "EcoLoop-Weather-Installer/0.1"},
        )
        with (
            urllib.request.urlopen(request, timeout=60) as response,  # noqa: S310
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if sha256_file(archive) != RELEASE_SHA256:
            raise RuntimeError("Official EnergyPlus release archive checksum mismatch.")
        with zipfile.ZipFile(archive) as bundle:
            matches = [
                name
                for name in bundle.namelist()
                if name.replace("\\", "/").endswith(WEATHER_MEMBER)
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    f"Expected exactly one {WEATHER_MEMBER!r} member; found {len(matches)}."
                )
            extracted = Path(work) / "default.epw"
            with bundle.open(matches[0]) as source, extracted.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        return install_from_path(extracted, destination, force=force)


def main() -> int:
    """Run the command-line weather installer."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energyplus-home", type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path("weather/default.epw"),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the exact official 26.1.0 release archive if no local EPW exists.",
    )
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()

    try:
        installed = find_installed_weather(arguments.energyplus_home)
        if installed is not None:
            result = install_from_path(installed, arguments.destination, force=arguments.force)
        elif arguments.download:
            result = download_release_weather(arguments.destination, force=arguments.force)
        else:
            parser.error(
                "The checksummed EPW was not found in EnergyPlus 26.1.0. "
                "Set ENERGYPLUS_HOME or rerun with --download."
            )
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Installed verified weather file: {result}")
    print(f"SHA-256: {sha256_file(result)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
