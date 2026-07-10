import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.migration.import_crm_tickets_phase1 import (
    DEFAULT_EXCLUDE_TITLE_REGEX,
    UnmappedDecision,
    _format_datetime,
    _load_staff_map,
    _parse_datetime,
    decide_unmapped_ticket,
    derive_comment_author,
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


def _write_staff_map_csv(path: Path, rows: list[dict[str, str]]) -> Path:
    fieldnames = [
        "crm_person_id",
        "crm_name",
        "crm_email",
        "system_user_id",
        "match_via",
    ]
    lines = [",".join(fieldnames)]
    for row in rows:
        lines.append(",".join(row.get(name, "") for name in fieldnames))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_staff_map_lowercases_ids_and_skips_blank_rows(tmp_path: Path) -> None:
    csv_path = _write_staff_map_csv(
        tmp_path / "staff_map.csv",
        [
            {
                "crm_person_id": "PERSON-1",
                "crm_name": "Ada",
                "crm_email": "ada@dotmac.io",
                "system_user_id": "USER-1",
                "match_via": "person_email",
            },
            {
                "crm_person_id": "person-2",
                "crm_name": "No sub account",
                "crm_email": "gone@dotmac.io",
                "system_user_id": "",
                "match_via": "person_email",
            },
        ],
    )

    assert _load_staff_map(str(csv_path)) == {"person-1": "user-1"}


def test_load_staff_map_rejects_conflicting_rows(tmp_path: Path) -> None:
    csv_path = _write_staff_map_csv(
        tmp_path / "staff_map.csv",
        [
            {"crm_person_id": "person-1", "system_user_id": "user-1"},
            {"crm_person_id": "person-1", "system_user_id": "user-2"},
        ],
    )

    with pytest.raises(SystemExit):
        _load_staff_map(str(csv_path))


def test_load_staff_map_without_path_is_empty() -> None:
    assert _load_staff_map(None) == {}


def test_derive_comment_author_null_author_is_system() -> None:
    derived = derive_comment_author(
        None, staff_map={"person-1": "user-1"}, person_subscriber_map={}
    )

    assert derived == ("system", None, None)


def test_derive_comment_author_staff_map_hit() -> None:
    derived = derive_comment_author(
        "PERSON-1",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={},
    )

    assert derived == ("staff", None, "user-1")


def test_derive_comment_author_subscriber_linked_person_is_customer() -> None:
    derived = derive_comment_author(
        "person-9",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={"person-9": "sub-9"},
    )

    assert derived == ("customer", "sub-9", None)


def test_derive_comment_author_staff_map_wins_over_subscriber_link() -> None:
    derived = derive_comment_author(
        "person-1",
        staff_map={"person-1": "user-1"},
        person_subscriber_map={"person-1": "sub-1"},
    )

    assert derived == ("staff", None, "user-1")


def test_derive_comment_author_unmapped_stays_staff_without_system_user() -> None:
    derived = derive_comment_author(
        "person-404", staff_map={}, person_subscriber_map={}
    )

    assert derived == ("staff", None, None)
