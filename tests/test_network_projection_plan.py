"""Incumbent work item -> contract step input translation (shadow-ready)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.catalog import AccessCredential, SubscriptionStatus
from app.models.projection_secret_request import ProjectionSecretRequest
from app.services.integrations import network_projection_plan as owner
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
    command_id = command_id_for(step)
    assert command_id.startswith("radius-projection:jane@radius:")
    assert len(command_id.rsplit(":", 1)[1]) == 64
    assert "Sup3rSecretPw" not in command_id
    assert "projection-request:9f2c:3" not in command_id


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


@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.0/24 0.0.0.0 1",
        "  203.0.113.0/24\t0.0.0.0   1\n",
    ],
)
def test_representable_framed_route_preserves_cidr(value: str) -> None:
    item = replace(_active(), reply_attrs=(("Framed-Route", "+=", value),))
    step = translate_work_item(
        item,
        subject_ref="sub:subscription:9f2c",
        credential_id="cred-4711",
        secret_version=3,
        projection_request_ref="projection-request:9f2c:3",
    )
    assert {"key": "framed_route", "value": "203.0.113.0/24"} in step["attributes"]


@pytest.mark.parametrize(
    ("op", "value"),
    [
        ("+=", "203.0.113.0/24 0.0.0.0 2"),
        ("+=", "203.0.113.0/24 192.0.2.1 1"),
        (":=", "203.0.113.0/24 0.0.0.0 1"),
        ("=", "203.0.113.0/24 0.0.0.0 1"),
        ("+=", ""),
        ("+=", "203.0.113.0/24"),
        ("+=", "203.0.113.0/24 0.0.0.0"),
        ("+=", "203.0.113.0/24 0.0.0.0 1 extra-dummy-secret"),
        ("+=", "dummy-secret-not-a-cidr 0.0.0.0 1"),
        ("+=", "203.0.113.0/33 0.0.0.0 1"),
        ("+=", "203.0.113.1 0.0.0.0 1"),
    ],
)
def test_unrepresentable_framed_route_refuses_without_echo(op: str, value: str) -> None:
    item = replace(_active(), reply_attrs=(("Framed-Route", op, value),))
    with pytest.raises(UntranslatableWorkItem) as refusal:
        translate_work_item(
            item,
            subject_ref="sub:subscription:9f2c",
            credential_id="cred-4711",
            secret_version=3,
            projection_request_ref="projection-request:9f2c:3",
        )
    assert str(refusal.value) == "jane@radius: Framed-Route has no v1 expression"
    assert "dummy-secret" not in str(refusal.value)
    assert "Sup3rSecretPw" not in str(refusal.value)
    if value:
        assert value not in str(refusal.value)


def _step() -> dict[str, object]:
    return translate_work_item(
        _active(),
        subject_ref="sub:subscription:9f2c",
        credential_id="cred-4711",
        secret_version=3,
        projection_request_ref="projection-request:9f2c:3",
    )


def test_exact_plan_command_id_is_stable_across_mapping_order() -> None:
    step = _step()
    reordered = dict(reversed(tuple(step.items())))
    reordered["attributes"] = [
        dict(reversed(tuple(attribute.items())))
        for attribute in cast(list[dict[str, str]], step["attributes"])
    ]
    assert command_id_for(step) == command_id_for(dict(step))
    assert command_id_for(step) == command_id_for(reordered)
    assert "projection-request:9f2c:3" not in command_id_for(step)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("projection_request_ref", "projection-request:fresh"),
        ("policy_code", "captive"),
        ("policy_version", "v2"),
        ("previous_username", "old@radius"),
        ("subject_ref", "sub:subscription:other"),
        ("valid_until", "2026-10-30T12:00:00Z"),
    ],
)
def test_changed_plan_material_changes_command_id(field: str, value: str) -> None:
    step = _step()
    changed = {**step, field: value}
    assert changed["expected_fingerprint"] == step["expected_fingerprint"]
    assert command_id_for(changed) != command_id_for(step)


@pytest.mark.parametrize("unsupported", ["test_access", "unknown_attribute", "route"])
def test_unsupported_plan_opens_no_authorization(
    monkeypatch: pytest.MonkeyPatch,
    unsupported: str,
) -> None:
    item = _active()
    if unsupported == "test_access":
        item = replace(item, test_check_attrs=(("Auth-Type", ":=", "Accept"),))
    elif unsupported == "unknown_attribute":
        item = replace(item, reply_attrs=(("Vendor-Specific", ":=", "dummy"),))
    else:
        item = replace(
            item, reply_attrs=(("Framed-Route", "+=", "203.0.113.0/24 0.0.0.0 2"),)
        )

    def unexpected_open(
        db: Session, *, credential: AccessCredential
    ) -> ProjectionSecretRequest:
        pytest.fail("unsupported plan must not open an authorization")

    monkeypatch.setattr(owner, "open_projection_request", unexpected_open)
    with pytest.raises(UntranslatableWorkItem):
        owner.open_and_translate(
            cast(Session, object()),
            item,
            subject_ref="sub:subscription:9f2c",
            credential=AccessCredential(id=uuid4()),
        )


@pytest.mark.parametrize("mode", ["active", "captive"])
def test_supported_plan_opens_once_and_uses_authorized_version(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    item = replace(_active(), mode=mode)
    credential = AccessCredential(id=uuid4())
    db = cast(Session, object())
    calls: list[AccessCredential] = []

    def open_request(
        db_arg: Session, *, credential: AccessCredential
    ) -> ProjectionSecretRequest:
        assert db_arg is db
        calls.append(credential)
        return ProjectionSecretRequest(
            request_ref="projection-request:issued", secret_version=321
        )

    monkeypatch.setattr(owner, "open_projection_request", open_request)
    step = owner.open_and_translate(
        db, item, subject_ref="sub:subscription:9f2c", credential=credential
    )
    assert calls == [credential]
    assert step["projection_request_ref"] == "projection-request:issued"
    assert step["credential_id"] == str(credential.id)
    assert step["secret_version"] == 321
    assert step == translate_work_item(
        item,
        subject_ref="sub:subscription:9f2c",
        credential_id=str(credential.id),
        secret_version=321,
        projection_request_ref="projection-request:issued",
    )


@pytest.mark.parametrize("mode", ["reject", "none"])
def test_non_authenticating_plan_opens_no_authorization(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    item = replace(
        _active(), mode=mode, reply_attrs=(), check_attrs=(), profile_group=None
    )

    def unexpected_open(
        db: Session, *, credential: AccessCredential
    ) -> ProjectionSecretRequest:
        pytest.fail("non-authenticating plan must not open an authorization")

    monkeypatch.setattr(owner, "open_projection_request", unexpected_open)
    step = owner.open_and_translate(
        cast(Session, object()),
        item,
        subject_ref="sub:subscription:9f2c",
        credential=None,
    )
    assert step["projection_request_ref"] is None
    assert step["credential_id"] is None
    assert step["secret_version"] is None


def test_authenticating_plan_without_credential_is_named_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_open(
        db: Session, *, credential: AccessCredential
    ) -> ProjectionSecretRequest:
        pytest.fail("missing credential must refuse before opening authorization")

    monkeypatch.setattr(owner, "open_projection_request", unexpected_open)
    with pytest.raises(
        UntranslatableWorkItem,
        match="jane@radius: authenticating login without a credential",
    ):
        owner.open_and_translate(
            cast(Session, object()),
            _active(),
            subject_ref="sub:subscription:9f2c",
            credential=None,
        )
