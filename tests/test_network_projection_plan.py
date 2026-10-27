"""Incumbent work item -> contract step input translation (shadow-ready)."""

from __future__ import annotations

import json

import pytest

from app.models.catalog import SubscriptionStatus
from app.services.integrations.network_projection_plan import (
    UntranslatableWorkItem,
    command_id_for,
    translate_work_item,
)
from app.services.radius_population import RadiusProjectionWorkItem


def _active() -> RadiusProjectionWorkItem:
    return RadiusProjectionWorkItem(
        username="jane@radius",
        cleartext_password="Sup3rSecretPw",
        check_attrs=(
            ("Cleartext-Password", ":=", "Sup3rSecretPw"),
            ("Simultaneous-Use", ":=", "1"),
        ),
        reply_attrs=(
            ("Service-Type", ":=", "Framed-User"),
            ("Framed-Protocol", ":=", "PPP"),
            ("Acct-Interim-Interval", ":=", "300"),
            ("Framed-IP-Address", ":=", "100.64.1.5"),
            ("Mikrotik-Rate-Limit", ":=", "10M/10M"),
            ("Framed-Route", "+=", "203.0.113.0/24 0.0.0.0 1"),
        ),
        blocked=False,
        status=SubscriptionStatus.active,
        mode="active",
        profile_group="residential-10m",
    )


def test_active_translation_never_carries_the_password() -> None:
    step = translate_work_item(
        _active(),
        subject_ref="sub:subscription:9f2c",
        credential_id="cred-4711",
        secret_version=3,
        projection_request_ref="projection-request:9f2c:3",
    )
    assert "Sup3rSecretPw" not in json.dumps(step)
    assert step["desired_state"] == "enabled"
    assert step["policy_code"] == "active"
    assert step["credential_id"] == "cred-4711"
    pairs = {(a["key"], a["value"]) for a in step["attributes"]}
    assert pairs == {
        ("framed_ipv4_address", "100.64.1.5"),
        ("rate_limit", "10M/10M"),
        ("framed_route", "203.0.113.0/24"),
        ("simultaneous_use", "1"),
        ("profile_group", "residential-10m"),
    }
    assert command_id_for(step) == (
        f"radius-projection:jane@radius:{step['expected_fingerprint']}"
    )


def test_reject_translation_names_no_credential() -> None:
    item = RadiusProjectionWorkItem(
        username="jane@radius",
        cleartext_password="",
        check_attrs=(("Auth-Type", ":=", "Reject"),),
        reply_attrs=(),
        blocked=True,
        status=SubscriptionStatus.suspended,
        mode="reject",
        profile_group=None,
    )
    step = translate_work_item(
        item,
        subject_ref="sub:subscription:9f2c",
        credential_id="cred-4711",
        secret_version=3,
        projection_request_ref="projection-request:9f2c:3",
    )
    assert step["desired_state"] == "enabled"
    assert step["policy_code"] == "reject"
    assert step["credential_id"] is None
    assert step["projection_request_ref"] is None


def test_test_access_is_a_named_untranslatable() -> None:
    item = _active()
    object.__setattr__(item, "test_check_attrs", (("Auth-Type", ":=", "Accept"),))
    with pytest.raises(UntranslatableWorkItem, match="test-access"):
        translate_work_item(
            item,
            subject_ref="sub:subscription:9f2c",
            credential_id="cred-4711",
            secret_version=3,
            projection_request_ref="projection-request:9f2c:3",
        )


def test_unknown_reply_attribute_is_named_not_dropped() -> None:
    item = _active()
    object.__setattr__(
        item,
        "reply_attrs",
        (*item.reply_attrs, ("Vendor-Specific", ":=", "x")),
    )
    with pytest.raises(UntranslatableWorkItem, match="Vendor-Specific"):
        translate_work_item(
            item,
            subject_ref="sub:subscription:9f2c",
            credential_id="cred-4711",
            secret_version=3,
            projection_request_ref="projection-request:9f2c:3",
        )
