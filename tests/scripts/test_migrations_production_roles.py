"""Pure assertions of the production-roles migration lane."""

from __future__ import annotations

from scripts.ci.migrations_production_roles import (
    RELAY_DEFINERS,
    edge_violations,
    migrator_owned_violations,
)

NEW_EDGE = ("dotmac_app", "app_admin", False, True, True)
OLD_EDGE = ("app_admin", "dotmac_app", False, True, True)


def test_new_direction_without_admin_is_clean() -> None:
    assert edge_violations([NEW_EDGE]) == ()


def test_missing_edge_is_named() -> None:
    assert edge_violations([]) == ("app_admin is not a member of dotmac_app",)


def test_old_direction_is_refused_even_next_to_the_new_one() -> None:
    assert edge_violations([NEW_EDGE, OLD_EDGE]) == (
        "dotmac_app is a member of app_admin (old direction)",
    )
    assert "old direction" in edge_violations([OLD_EDGE])[1]


def test_admin_inherit_and_set_are_each_checked() -> None:
    row = ("dotmac_app", "app_admin", True, False, False)
    assert edge_violations([row]) == (
        "app_admin holds ADMIN OPTION on dotmac_app",
        "app_admin lacks INHERIT on dotmac_app",
        "app_admin lacks SET on dotmac_app",
    )


def test_only_the_four_relay_definers_may_stay_with_the_migrator() -> None:
    definers = [("pg_proc", name) for name in sorted(RELAY_DEFINERS)]
    assert migrator_owned_violations(definers) == ()


def test_any_other_migrator_owned_object_is_reported() -> None:
    owned = [("pg_proc", name) for name in sorted(RELAY_DEFINERS)]
    owned += [("pg_class", "public.invoices"), ("pg_proc", "helper")]
    assert migrator_owned_violations(owned) == (
        "migrator owns pg_class public.invoices",
        "migrator owns pg_proc helper",
    )


def test_a_missing_relay_definer_is_reported() -> None:
    owned = [("pg_proc", "claim_outbox_batch")]
    violations = migrator_owned_violations(owned)
    assert len(violations) == 3
    assert all("is not owned by app_admin" in v for v in violations)
