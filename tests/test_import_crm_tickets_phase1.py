import re
from datetime import UTC, datetime

from scripts.migration.import_crm_tickets_phase1 import (
    DEFAULT_EXCLUDE_TITLE_REGEX,
    UnmappedDecision,
    _format_datetime,
    _parse_datetime,
    decide_unmapped_ticket,
)


def test_unmapped_override_map_wins() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Anything", "status": "open"},
        overrides={"ticket-1": UnmappedDecision("map", "manual_override", "sub-1")},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("map", "manual_override", "sub-1")


def test_unmapped_override_skip_wins() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Real open ticket", "status": "open"},
        overrides={"ticket-1": UnmappedDecision("skip", "known_test")},
        exclude_title_re=None,
        allow_unmapped_closed=False,
    )

    assert decision == UnmappedDecision("skip", "known_test")


def test_unmapped_test_probe_title_is_skipped() -> None:
    decision = decide_unmapped_ticket(
        {
            "id": "ticket-1",
            "title": "Codex production Selfcare webhook probe b73fd71d",
            "status": "open",
        },
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("skip", "exclude_title_regex")


def test_unmapped_closed_history_imports_unlinked() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Slow browsing", "status": "closed"},
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("unlink", "unmapped_closed_history")


def test_unmapped_open_real_ticket_blocks() -> None:
    decision = decide_unmapped_ticket(
        {"id": "ticket-1", "title": "Slow browsing", "status": "open"},
        overrides={},
        exclude_title_re=re.compile(DEFAULT_EXCLUDE_TITLE_REGEX),
        allow_unmapped_closed=True,
    )

    assert decision == UnmappedDecision("block", "unmapped_subscriber")


def test_parse_datetime_normalizes_zulu_to_utc() -> None:
    parsed = _parse_datetime("2026-07-08T12:34:56Z")

    assert parsed == datetime(2026, 7, 8, 12, 34, 56, tzinfo=UTC)
    assert _format_datetime(parsed) == "2026-07-08T12:34:56+00:00"
