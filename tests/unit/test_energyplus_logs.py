from __future__ import annotations

from ecoloop.energyplus.logs import (
    MessageSeverity,
    classify_message,
    parse_error_text,
    severity_counts,
)


def test_error_parser_joins_continuations_and_deduplicates() -> None:
    text = """
    ** Warning ** Schedule value was outside range
    **   ~~~   ** The value was clamped by EnergyPlus
    ** Warning ** Schedule value was outside range
    **   ~~~   ** The value was clamped by EnergyPlus
    ** Severe  ** Required node is missing
    **  Fatal  ** Errors occurred during input processing
    """
    messages = parse_error_text(text)

    assert [item.severity for item in messages] == [
        MessageSeverity.WARNING,
        MessageSeverity.SEVERE,
        MessageSeverity.FATAL,
    ]
    assert messages[0].occurrences == 2
    assert "clamped by EnergyPlus" in messages[0].message
    assert len(messages[0].digest) == 64
    assert severity_counts(messages) == {
        "information": 0,
        "warning": 2,
        "severe": 1,
        "fatal": 1,
    }


def test_callback_message_classification_defaults_to_information() -> None:
    assert classify_message("EnergyPlus Starting") is MessageSeverity.INFORMATION
    assert classify_message("** Severe ** bad input") is MessageSeverity.SEVERE
    assert classify_message("fatal error detected") is MessageSeverity.FATAL


def test_error_parser_hard_caps_unique_messages() -> None:
    messages = parse_error_text(
        "\n".join(f"** Warning ** warning number {index}" for index in range(20)),
        maximum_messages=3,
    )
    assert len(messages) == 3
