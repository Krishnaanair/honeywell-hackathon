"""EcoLoop Building Agents package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ecoloop-building-agents")
except PackageNotFoundError:
    __version__ = "0.1.0"

ENERGYPLUS_VERSION = "26.1.0"
