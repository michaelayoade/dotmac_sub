"""Static contract for migration 665 (Splynx billing email -> billing contact).

The PostgreSQL behaviour is proven in
``tests/integration/test_splynx_billing_email_migration.py``; this file pins
the pure classification, its parity with the contact owner's normalisation,
and the shape of the audit evidence.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.services import validation_api
from app.services.customer_identity_normalization import normalize_email_identifier
from app.services.customer_portal_contacts import validated_contact_email

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/665_backfill_splynx_billing_email_contacts.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("m665_static", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _owner(value: object) -> str | None:
    """What the contact owner would store, or None where it would refuse."""

    if not isinstance(value, str):
        return None
    try:
        validated = validated_contact_email(value)
    except ValueError:
        return None
    return normalize_email_identifier(validated)


def test_chained_onto_664() -> None:
    migration = _load()
    assert migration.revision == "665_backfill_splynx_billing_email_contacts"
    assert migration.down_revision == "664_purge_retired_splynx_metadata_keys"
    assert migration.KEY == "splynx_billing_email"


def test_email_pattern_is_the_owners() -> None:
    assert _load().EMAIL_PATTERN.pattern == validation_api.EMAIL_PATTERN.pattern


@pytest.mark.parametrize(
    "value",
    [
        "Billing@Example.com",
        "  billing@example.com  ",
        "a.b+c@sub.example.co",
        "two@example.com, three@example.com",
        "two@example.com;three@example.com",
        "not-an-email",
        "missing-tld@example",
        "space in@example.com",
        "",
        "   ",
        None,
        42,
        ["billing@example.com"],
    ],
)
def test_normalisation_matches_the_contact_owner(value: object) -> None:
    assert _load().normalize_billing_email(value) == _owner(value)


def test_overlong_value_is_invalid_not_truncated() -> None:
    value = "a" * 250 + "@example.com"
    assert _load().normalize_billing_email(value) is None


@pytest.mark.parametrize(
    ("value", "account_email", "billing_contacts", "expected"),
    [
        # No billing contact: backfill, normalised.
        (
            " Billing@Example.com ",
            "owner@example.com",
            [],
            ("backfill", "billing@example.com"),
        ),
        # A billing contact without an email is not a typed value.
        ("billing@example.com", "owner@example.com", ["", None], ("backfill",)),
        # Equal to the account email (case-insensitive): already typed.
        ("Owner@Example.com", "owner@example.com", [], ("typed",)),
        # Equal to an existing billing contact: already typed.
        (
            "billing@example.com",
            "owner@example.com",
            ["other@example.com", "BILLING@example.com"],
            ("typed",),
        ),
        # A different non-empty billing contact: conflict, never overwritten.
        (
            "billing@example.com",
            "owner@example.com",
            ["finance@example.com"],
            ("conflict",),
        ),
        ("nope", "owner@example.com", [], ("invalid", None)),
        ("a@example.com,b@example.com", "", [], ("invalid", None)),
    ],
)
def test_classification(value, account_email, billing_contacts, expected) -> None:
    outcome = _load().classify(value, account_email, billing_contacts)
    assert outcome[: len(expected)] == expected


def test_audit_payload_carries_counts_and_ids_not_emails() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    payload = source[source.index('"metadata": json.dumps(') :]
    payload = payload[: payload.index("sort_keys=True")]
    for field in (
        '"counts"',
        '"conflict_subscriber_ids"',
        '"invalid_subscriber_ids"',
        '"reason"',
    ):
        assert field in payload
    counts = source[source.index("counts = {") :]
    counts = counts[: counts.index("}")]
    for name in ('"backfilled"', '"removed"', '"conflicts"', '"invalid"'):
        assert name in counts
    assert "email" not in payload.replace("billing_email", "")


def test_backfilled_contacts_do_not_change_delivery() -> None:
    """is_billing_contact=true, is_authorized=false, receives_notifications=false."""

    source = MIGRATION.read_text(encoding="utf-8")
    assert "is_billing_contact, is_authorized, receives_notifications, " in source
    assert "'billing', " in source
    assert '"true, false, false, :notes, now(), now()"' in source


def test_downgrade_is_an_explicit_forward_fix_no_op() -> None:
    migration = _load()
    assert "forward-fix" in (migration.__doc__ or "")
    assert migration.downgrade() is None
