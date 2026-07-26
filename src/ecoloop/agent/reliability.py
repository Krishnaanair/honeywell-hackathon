"""Deterministic timeout, circuit-breaker, and action-cache primitives."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


class FailureCircuitBreaker:
    """Open per run, skip one interval, then permit one half-open probe."""

    def __init__(self, threshold: int) -> None:
        if threshold < 1:
            raise ValueError("threshold must be positive")
        self._threshold = threshold
        self._failures: dict[str, int] = {}
        self._open_skips_remaining: dict[str, int] = {}

    def record_success(self, run_id: str) -> None:
        """Reset a run's consecutive failure counter."""

        self._failures.pop(run_id, None)
        self._open_skips_remaining.pop(run_id, None)

    def record_failure(self, run_id: str) -> int:
        """Increment and return a run's consecutive failure count."""

        count = self._failures.get(run_id, 0) + 1
        self._failures[run_id] = count
        if count >= self._threshold:
            self._open_skips_remaining[run_id] = 1
        return count

    def is_open(self, run_id: str) -> bool:
        """Return whether local-model control is disabled for this run."""

        return self._failures.get(run_id, 0) >= self._threshold

    def failure_count(self, run_id: str) -> int:
        """Return the current consecutive failure count."""

        return self._failures.get(run_id, 0)

    def should_attempt(self, run_id: str) -> bool:
        """Return whether inference may run, consuming one open skip if needed."""

        if not self.is_open(run_id):
            return True
        remaining = self._open_skips_remaining.get(run_id, 0)
        if remaining > 0:
            self._open_skips_remaining[run_id] = remaining - 1
            return False
        return True


@dataclass(frozen=True, slots=True)
class CachedCandidate:
    """Candidate selection cached for a quantized state."""

    action: dict[str, Any]
    created_at: datetime


class ActionCache:
    """Cache candidate selections for nearly identical quantized states."""

    def __init__(self, ttl: timedelta = timedelta(hours=2), max_entries: int = 256) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._ttl = ttl
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], CachedCandidate] = {}

    def get(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return a copied candidate when the quantized state is still fresh."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        key = (run_id, quantized_state_key(state))
        cached = self._entries.get(key)
        if cached is None:
            return None
        if current - cached.created_at > self._ttl:
            self._entries.pop(key, None)
            return None
        return dict(cached.action)

    def put(
        self,
        run_id: str,
        state: dict[str, Any],
        action: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        """Cache a copied candidate and evict the oldest entry when bounded."""

        current = (now or datetime.now(UTC)).astimezone(UTC)
        key = (run_id, quantized_state_key(state))
        self._entries[key] = CachedCandidate(dict(action), current)
        if len(self._entries) > self._max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item].created_at)
            self._entries.pop(oldest, None)

    def discard(self, run_id: str, state: dict[str, Any]) -> bool:
        """Remove the candidate for a quantized state after revalidation fails."""

        key = (run_id, quantized_state_key(state))
        return self._entries.pop(key, None) is not None


def quantized_state_key(state: dict[str, Any]) -> str:
    """Hash decision-relevant state after stable numeric quantization."""

    relevant = {
        key: _quantize(value)
        for key, value in state.items()
        if key
        in {
            "occupied",
            "occupancy_count",
            "zone_temperature_mean_c",
            "operative_temperature_mean_c",
            "pmv_mean",
            "co2_max_ppm",
            "outdoor_temperature_c",
            "facility_demand_kw",
            "heating_setpoint_c",
            "cooling_setpoint_c",
            "tariff",
            "carbon_intensity",
        }
    }
    payload = json.dumps(relevant, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quantize(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return "non-finite"
        return round(number * 2.0) / 2.0
    return str(value)[:80]
