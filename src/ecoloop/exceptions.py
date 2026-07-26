"""Project-specific exception hierarchy."""


class EcoLoopError(Exception):
    """Base class for actionable EcoLoop failures."""


class ConfigurationError(EcoLoopError):
    """Raised when configuration is incomplete or invalid."""


class DependencyUnavailableError(EcoLoopError):
    """Raised when a required external dependency cannot be discovered."""


class EnergyPlusIntegrationError(EcoLoopError):
    """Raised for EnergyPlus model, API, callback, or result failures."""


class HandleDiscoveryError(EnergyPlusIntegrationError):
    """Raised when a required EnergyPlus exchange handle is unavailable."""


class MCPToolError(EcoLoopError):
    """Raised for a rejected MCP tool request."""


class RunStateError(EcoLoopError):
    """Raised when a run or run pair is in an incompatible state."""
