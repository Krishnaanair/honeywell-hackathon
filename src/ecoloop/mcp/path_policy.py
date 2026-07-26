"""Path confinement for diagnostic MCP tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when an MCP diagnostic path escapes an allowed root or type."""


@dataclass(frozen=True, slots=True)
class PathPolicy:
    """Resolve diagnostic files only beneath explicit trusted roots."""

    repository_root: Path
    energyplus_roots: tuple[Path, ...] = ()
    maximum_read_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        """Store canonical absolute roots."""

        object.__setattr__(self, "repository_root", self.repository_root.resolve())
        object.__setattr__(
            self,
            "energyplus_roots",
            tuple(root.expanduser().resolve() for root in self.energyplus_roots),
        )
        if self.maximum_read_bytes <= 0:
            raise ValueError("maximum_read_bytes must be positive")

    @property
    def roots(self) -> tuple[Path, ...]:
        """Return all roots from which diagnostic reads are permitted."""

        return (self.repository_root, *self.energyplus_roots)

    def resolve_existing(
        self,
        raw_path: str,
        *,
        suffixes: Iterable[str],
        repository_only: bool = False,
    ) -> Path:
        """Resolve an existing regular file and enforce root, type, and size limits."""

        path = self._resolve(raw_path, repository_only=repository_only)
        allowed_suffixes = {item.casefold() for item in suffixes}
        if path.suffix.casefold() not in allowed_suffixes:
            expected = ", ".join(sorted(allowed_suffixes))
            raise PathPolicyError(f"unsupported file type; expected one of: {expected}")
        if not path.exists():
            raise PathPolicyError("requested file does not exist")
        if not path.is_file():
            raise PathPolicyError("requested path is not a regular file")
        size = path.stat().st_size
        if size > self.maximum_read_bytes:
            raise PathPolicyError(
                f"requested file is too large ({size} bytes; limit {self.maximum_read_bytes})"
            )
        return path

    def resolve_output(
        self,
        raw_path: str,
        *,
        suffixes: Iterable[str],
    ) -> Path:
        """Resolve a repository-confined output without creating it."""

        path = self._resolve(raw_path, repository_only=True)
        allowed_suffixes = {item.casefold() for item in suffixes}
        if path.suffix.casefold() not in allowed_suffixes:
            expected = ", ".join(sorted(allowed_suffixes))
            raise PathPolicyError(f"unsupported output type; expected one of: {expected}")
        return path

    def _resolve(self, raw_path: str, *, repository_only: bool) -> Path:
        if not raw_path or "\x00" in raw_path:
            raise PathPolicyError("path must be a non-empty text value")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.repository_root / candidate
        resolved = candidate.resolve()
        roots = (self.repository_root,) if repository_only else self.roots
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise PathPolicyError("path is outside the configured diagnostic roots")
        return resolved
