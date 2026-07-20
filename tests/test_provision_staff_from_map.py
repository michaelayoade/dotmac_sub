from pathlib import Path

import pytest

from scripts.migration.provision_staff_from_map import (
    ACTION_ALREADY_EXISTS,
    ACTION_CREATE,
    ACTION_DUPLICATE_EMAIL,
    ACTION_INACTIVE,
    ACTION_NO_EMAIL,
    ACTION_SKIPPED,
    Decision,
    candidate_email,
    decide_row,
    load_rows,
    load_skip_ids,
    parse_bool,
    plan_rows,
    split_name,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "crm_person_id": "person-1",
        "crm_name": "Jane Doe",
        "crm_email": "jane@dotmac.io",
        "credential_usernames": "",
        "person_is_active": "True",
        "credential_is_active": "True",
    }
    row.update(overrides)
    return row


def test_parse_bool_csv_strings() -> None:
    assert parse_bool("True") is True
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool("False") is False
    assert parse_bool("") is False
    assert parse_bool(None) is False
    assert parse_bool(True) is True


def test_split_name_first_and_rest() -> None:
    assert split_name("Jane Doe") == ("Jane", "Doe")
    assert split_name("Jane A. van Doe") == ("Jane", "A. van Doe")
    assert split_name("Chinedu") == ("Chinedu", "")
    assert split_name("  ") == ("", "")


def test_split_name_truncates_to_model_width() -> None:
    first, last = split_name("A" * 100 + " " + "B" * 100)
    assert len(first) == 80
    assert len(last) == 80


def test_candidate_email_prefers_person_email() -> None:
    row = _row(crm_email="Jane@Dotmac.IO", credential_usernames="other@dotmac.io")
    assert candidate_email(row) == "jane@dotmac.io"


def test_candidate_email_falls_back_to_credential_username() -> None:
    row = _row(crm_email="", credential_usernames="jdoe;jane.doe@dotmac.io")
    assert candidate_email(row) == "jane.doe@dotmac.io"


def test_candidate_email_none_when_no_email_anywhere() -> None:
    row = _row(crm_email="", credential_usernames="jdoe;jdoe2")
    assert candidate_email(row) is None


def test_decide_row_skip_list_wins() -> None:
    decision = decide_row(_row(), {}, skip_ids={"person-1"})
    assert decision == Decision(ACTION_SKIPPED, "in_skip_list")


def test_decide_row_skip_list_is_case_insensitive() -> None:
    decision = decide_row(_row(crm_person_id="PERSON-1"), {}, skip_ids={"person-1"})
    assert decision.action == ACTION_SKIPPED


def test_decide_row_inactive_staff() -> None:
    decision = decide_row(_row(credential_is_active="False"), {}, set())
    assert decision.action == ACTION_INACTIVE

    decision = decide_row(_row(person_is_active="False"), {}, set())
    assert decision.action == ACTION_INACTIVE


def test_decide_row_no_usable_email() -> None:
    decision = decide_row(_row(crm_email="", credential_usernames="jdoe"), {}, set())
    assert decision.action == ACTION_NO_EMAIL


def test_decide_row_already_exists_by_normalized_email() -> None:
    decision = decide_row(
        _row(crm_email="Jane@Dotmac.IO"),
        {"jane@dotmac.io": "sub-user-1"},
        set(),
    )
    assert decision.action == ACTION_ALREADY_EXISTS
    assert decision.system_user_id == "sub-user-1"
    assert decision.email == "jane@dotmac.io"


def test_decide_row_create() -> None:
    decision = decide_row(_row(), {}, set())
    assert decision == Decision(
        ACTION_CREATE, "unmatched_active_crm_staff", email="jane@dotmac.io"
    )


def test_plan_rows_downgrades_duplicate_emails_within_csv() -> None:
    rows = [
        _row(crm_person_id="person-1"),
        _row(crm_person_id="person-2", crm_name="Jane Duplicate"),
        _row(crm_person_id="person-3", crm_email="unique@dotmac.io"),
    ]
    planned = plan_rows(rows, {}, set())
    actions = [decision.action for _row_, decision in planned]
    assert actions == [ACTION_CREATE, ACTION_DUPLICATE_EMAIL, ACTION_CREATE]
    assert planned[1][1].email == "jane@dotmac.io"


def test_plan_rows_idempotent_rerun_reports_already_exists() -> None:
    rows = [_row()]
    planned = plan_rows(rows, {"jane@dotmac.io": "sub-user-9"}, set())
    assert planned[0][1].action == ACTION_ALREADY_EXISTS
    assert planned[0][1].system_user_id == "sub-user-9"


def test_load_rows_rejects_wrong_csv(tmp_path: Path) -> None:
    path = tmp_path / "wrong.csv"
    path.write_text("crm_person_id,name\nx,y\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing required columns"):
        load_rows(path)


def test_load_rows_reads_unmatched_active_layout(tmp_path: Path) -> None:
    path = tmp_path / "unmatched_active.csv"
    path.write_text(
        "crm_person_id,crm_name,crm_email,credential_usernames,"
        "person_is_active,credential_is_active\n"
        "person-1,Jane Doe,jane@dotmac.io,,True,True\n",
        encoding="utf-8",
    )
    rows = load_rows(path)
    assert len(rows) == 1
    assert rows[0]["crm_person_id"] == "person-1"


def test_load_skip_ids(tmp_path: Path) -> None:
    path = tmp_path / "skip.csv"
    path.write_text(
        "crm_person_id,note\nPERSON-1,stale login\n,blank ignored\n",
        encoding="utf-8",
    )
    assert load_skip_ids(path) == {"person-1"}


def test_load_skip_ids_none_path() -> None:
    assert load_skip_ids(None) == set()


def test_load_skip_ids_requires_column(tmp_path: Path) -> None:
    path = tmp_path / "skip.csv"
    path.write_text("id\nx\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="crm_person_id column"):
        load_skip_ids(path)
