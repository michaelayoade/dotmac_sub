from scripts.migration.build_crm_staff_map import (
    MATCH_VIA_CREDENTIAL_USERNAME,
    MATCH_VIA_PERSON_EMAIL,
    CrmStaffRow,
    SystemUserRow,
    build_staff_map,
    candidate_emails,
    display_name,
    normalize_email,
)


def _staff(
    person_id: str,
    person_email: str | None = None,
    credential_usernames: tuple[str, ...] = (),
    name: str = "Staff Person",
    person_is_active: bool = True,
    credential_is_active: bool = True,
) -> CrmStaffRow:
    return CrmStaffRow(
        person_id=person_id,
        name=name,
        person_email=person_email,
        credential_usernames=credential_usernames,
        person_is_active=person_is_active,
        credential_is_active=credential_is_active,
    )


def _user(
    user_id: str,
    email: str | None,
    name: str = "Sub User",
    is_active: bool = True,
) -> SystemUserRow:
    return SystemUserRow(
        id=user_id,
        email=email,
        name=name,
        user_type="system_user",
        is_active=is_active,
    )


def test_normalize_email_lowercases_strips_and_rejects_non_emails() -> None:
    assert normalize_email("  Jane.Doe@Dotmac.IO ") == "jane.doe@dotmac.io"
    assert normalize_email(None) is None
    assert normalize_email("   ") is None
    assert normalize_email("jdoe") is None  # local username, not an email


def test_candidate_emails_orders_person_first_and_dedupes() -> None:
    staff = _staff(
        "p1",
        person_email="Jane@dotmac.io",
        credential_usernames=("jane@dotmac.io", "jdoe", "j.doe@dotmac.ng"),
    )

    assert candidate_emails(staff) == [
        ("jane@dotmac.io", MATCH_VIA_PERSON_EMAIL),
        ("j.doe@dotmac.ng", MATCH_VIA_CREDENTIAL_USERNAME),
    ]


def test_display_name_prefers_display_then_names_then_email() -> None:
    assert display_name("Ops Jane", "Jane", "Doe", "j@d.io") == "Ops Jane"
    assert display_name("  ", "Jane", "Doe", "j@d.io") == "Jane Doe"
    assert display_name(None, None, " ", "j@d.io") == "j@d.io"
    assert display_name(None, None, None, None) == "User"


def test_matches_on_person_email_case_insensitively() -> None:
    result = build_staff_map(
        [_staff("p1", person_email="Jane@Dotmac.io")],
        [_user("u1", "jane@dotmac.io ")],
    )

    assert result.stats.matched == 1
    assert result.stats.matched_via_person_email == 1
    assert result.reports["staff_map"] == [
        {
            "crm_person_id": "p1",
            "crm_name": "Staff Person",
            "crm_email": "jane@dotmac.io",
            "system_user_id": "u1",
            "match_via": MATCH_VIA_PERSON_EMAIL,
        }
    ]


def test_matches_on_credential_username_when_person_email_misses() -> None:
    result = build_staff_map(
        [
            _staff(
                "p1",
                person_email="personal@gmail.com",
                credential_usernames=("jane@dotmac.io",),
            )
        ],
        [_user("u1", "jane@dotmac.io")],
    )

    assert result.stats.matched == 1
    assert result.stats.matched_via_credential_username == 1
    row = result.reports["staff_map"][0]
    assert row["system_user_id"] == "u1"
    assert row["crm_email"] == "jane@dotmac.io"
    assert row["match_via"] == MATCH_VIA_CREDENTIAL_USERNAME


def test_unique_credential_match_beats_ambiguous_person_email() -> None:
    result = build_staff_map(
        [
            _staff(
                "p1",
                person_email="shared@dotmac.io",
                credential_usernames=("jane@dotmac.io",),
            )
        ],
        [
            _user("u1", "shared@dotmac.io"),
            _user("u2", "Shared@dotmac.io"),
            _user("u3", "jane@dotmac.io"),
        ],
    )

    assert result.stats.matched == 1
    assert result.stats.ambiguous == 0
    assert result.reports["staff_map"][0]["system_user_id"] == "u3"


def test_ambiguous_when_email_hits_multiple_sub_users() -> None:
    result = build_staff_map(
        [_staff("p1", person_email="shared@dotmac.io")],
        [_user("u2", "Shared@dotmac.io"), _user("u1", "shared@dotmac.io")],
    )

    assert result.stats.matched == 0
    assert result.stats.ambiguous == 1
    assert result.reports["ambiguous"] == [
        {
            "crm_person_id": "p1",
            "crm_name": "Staff Person",
            "crm_email": "shared@dotmac.io",
            "candidate_system_user_ids": "u1;u2",
        }
    ]


def test_unmatched_split_by_crm_staff_activity() -> None:
    result = build_staff_map(
        [
            _staff("p-active", person_email="gone@dotmac.io"),
            _staff(
                "p-left",
                person_email="left@dotmac.io",
                person_is_active=False,
            ),
            _staff(
                "p-locked",
                person_email="locked@dotmac.io",
                credential_is_active=False,
            ),
        ],
        [],
    )

    assert result.stats.unmatched_active == 1
    assert result.stats.unmatched_inactive == 2
    assert [r["crm_person_id"] for r in result.reports["unmatched_active"]] == [
        "p-active"
    ]
    assert sorted(r["crm_person_id"] for r in result.reports["unmatched_inactive"]) == [
        "p-left",
        "p-locked",
    ]


def test_directory_lists_all_staff_including_unmatched_and_inactive() -> None:
    result = build_staff_map(
        [
            _staff("p1", person_email="jane@dotmac.io", name="Jane Doe"),
            _staff(
                "p2",
                person_email="left@dotmac.io",
                name="Left Person",
                person_is_active=False,
            ),
        ],
        [_user("u1", "jane@dotmac.io")],
    )

    assert result.reports["crm_people_directory"] == [
        {"id": "p1", "name": "Jane Doe", "email": "jane@dotmac.io"},
        {"id": "p2", "name": "Left Person", "email": "left@dotmac.io"},
    ]


def test_matched_inactive_sub_user_and_many_to_one_are_counted() -> None:
    result = build_staff_map(
        [
            _staff("p1", person_email="jane@dotmac.io"),
            _staff(
                "p2",
                person_email="old@gmail.com",
                credential_usernames=("jane@dotmac.io",),
            ),
        ],
        [_user("u1", "jane@dotmac.io", is_active=False)],
    )

    assert result.stats.matched == 2
    assert result.stats.matched_inactive_sub_user == 2
    assert result.stats.sub_users_matched_by_multiple_crm_staff == 1


def test_staff_without_any_usable_email_is_unmatched() -> None:
    result = build_staff_map(
        [_staff("p1", person_email=None, credential_usernames=("jdoe",))],
        [_user("u1", "jane@dotmac.io")],
    )

    assert result.stats.matched == 0
    assert result.stats.unmatched_active == 1
    assert result.reports["unmatched_active"][0]["credential_usernames"] == "jdoe"
