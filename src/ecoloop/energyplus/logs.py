"""EnergyPlus message and ``eplusout.err`` parsing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

_PREFIX = re.compile(r"^\s*\*+\s*(?P<level>warning|severe|fatal|info(?:rmation)?)\s*\*+\s*", re.I)
_CONTINUATION = re.compile(r"^\s*\*+\s*~+\s*\*+\s*")
_SPACE = re.compile(r"\s+")


class MessageSeverity(StrEnum):
    """Normalized EnergyPlus diagnostic severity."""

    INFORMATION = "information"
    WARNING = "warning"
    SEVERE = "severe"
    FATAL = "fatal"


@dataclass(frozen=True, slots=True)
class EnergyPlusMessage:
    """A de-duplicated EnergyPlus diagnostic."""

    severity: MessageSeverity
    message: str
    digest: str
    occurrences: int = 1


def classify_message(message: str) -> MessageSeverity:
    """Classify one callback or error-file message."""

    match = _PREFIX.match(message)
    if match:
        level = match.group("level").casefold()
        if level == "warning":
            return MessageSeverity.WARNING
        if level == "severe":
            return MessageSeverity.SEVERE
        if level == "fatal":
            return MessageSeverity.FATAL
    folded = message.casefold()
    if "** fatal **" in folded or "fatal error" in folded:
        return MessageSeverity.FATAL
    if "** severe **" in folded:
        return MessageSeverity.SEVERE
    if "** warning **" in folded:
        return MessageSeverity.WARNING
    return MessageSeverity.INFORMATION


def normalize_message(message: str) -> str:
    """Normalize a diagnostic for hashing while retaining its meaning."""

    stripped = _PREFIX.sub("", message)
    stripped = _CONTINUATION.sub("", stripped)
    return _SPACE.sub(" ", stripped).strip()


def message_digest(severity: MessageSeverity, message: str) -> str:
    """Return a stable SHA-256 digest for de-duplication."""

    payload = f"{severity.value}\0{normalize_message(message).casefold()}".encode()
    return hashlib.sha256(payload).hexdigest()


def parse_error_text(text: str, *, maximum_messages: int = 500) -> tuple[EnergyPlusMessage, ...]:
    """Parse and de-duplicate EnergyPlus error-file content.

    Continuation lines are attached to the preceding diagnostic. The hard cap
    prevents an invalid model from flooding the communication bus.
    """

    raw_messages: list[tuple[MessageSeverity, str]] = []
    current_severity: MessageSeverity | None = None
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_severity, current_parts
        if current_severity is not None and current_parts:
            raw_messages.append((current_severity, " ".join(current_parts)))
        current_severity = None
        current_parts = []

    for line in text.splitlines():
        prefix = _PREFIX.match(line)
        if prefix:
            flush()
            current_severity = classify_message(line)
            current_parts = [normalize_message(line)]
            continue
        if _CONTINUATION.match(line) and current_severity is not None:
            content = normalize_message(line)
            if content:
                current_parts.append(content)
            continue
        stripped = line.strip()
        if stripped and current_severity is not None:
            current_parts.append(_SPACE.sub(" ", stripped))
    flush()

    by_digest: dict[str, EnergyPlusMessage] = {}
    order: list[str] = []
    for severity, message in raw_messages:
        digest = message_digest(severity, message)
        existing = by_digest.get(digest)
        if existing is None:
            if len(order) >= maximum_messages:
                break
            order.append(digest)
            by_digest[digest] = EnergyPlusMessage(severity, message, digest)
        else:
            by_digest[digest] = EnergyPlusMessage(
                severity=existing.severity,
                message=existing.message,
                digest=existing.digest,
                occurrences=existing.occurrences + 1,
            )
    return tuple(by_digest[item] for item in order)


def parse_error_file(path: Path, *, maximum_messages: int = 500) -> tuple[EnergyPlusMessage, ...]:
    """Parse an ``eplusout.err`` file without interpreting its content as instructions."""

    return parse_error_text(
        path.read_text(encoding="utf-8", errors="replace"),
        maximum_messages=maximum_messages,
    )


def severity_counts(messages: tuple[EnergyPlusMessage, ...]) -> dict[str, int]:
    """Count occurrences by normalized severity."""

    counts = {severity.value: 0 for severity in MessageSeverity}
    for message in messages:
        counts[message.severity.value] += message.occurrences
    return counts
