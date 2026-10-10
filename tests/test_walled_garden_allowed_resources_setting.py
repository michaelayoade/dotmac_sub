"""Named walled-garden allowed resources are a typed, validated setting."""

from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.schemas.walled_garden import (
    DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES,
    PAYSTACK_PRESET,
    WalledGardenAllowedResources,
    normalize_walled_garden_hostname,
    walled_garden_allowed_resources_error,
)
from app.services import settings_spec
from app.services.domain_settings import DomainSettings

KEY = "walled_garden_allowed_resources"


def _entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "key": "flutterwave",
        "label": "Flutterwave",
        "kind": "payment",
        "hosts": ["checkout.flutterwave.com"],
        "enabled": True,
    }
    entry.update(overrides)
    return entry


def test_spec_is_typed_json_with_a_disabled_paystack_preset() -> None:
    spec = settings_spec.get_spec(SettingDomain.radius, KEY)

    assert spec is not None
    assert spec.value_type is SettingValueType.json
    parsed = WalledGardenAllowedResources.from_setting_value(spec.default)
    assert parsed.entries == (PAYSTACK_PRESET,)
    assert PAYSTACK_PRESET.enabled is False
    assert PAYSTACK_PRESET.hosts == (
        "checkout.paystack.com",
        "api.paystack.co",
        "js.paystack.co",
        "standard.paystack.co",
    )


def test_resolved_default_is_the_preset(db_session) -> None:
    value = settings_spec.resolve_value(db_session, SettingDomain.radius, KEY)

    assert WalledGardenAllowedResources.from_setting_value(value).entries == (
        PAYSTACK_PRESET,
    )


@pytest.mark.parametrize(
    "host",
    ["checkout.paystack.com", "API.Paystack.CO.", "a-b.example.ng"],
)
def test_valid_hostnames_normalise(host: str) -> None:
    assert normalize_walled_garden_hostname(host) == host.lower().rstrip(".")


@pytest.mark.parametrize(
    "host",
    [
        "",
        "localhost",
        "1.2.3.4",
        "*.paystack.com",
        "https://checkout.paystack.com",
        "checkout.paystack.com:443",
        "-bad.example.com",
        "bad_.example.com",
        'a.com" ; /system reset',
        "example.123",
    ],
)
def test_invalid_hostnames_are_rejected(host: str) -> None:
    with pytest.raises(ValueError):
        normalize_walled_garden_hostname(host)


@pytest.mark.parametrize(
    "entries",
    [
        [_entry(), _entry()],  # duplicate keys
        [_entry(key="portal")],  # reserved, derived from captive_portal_url
        [_entry(kind="portal")],
        [_entry(key="Bad Key")],
        [_entry(hosts=[])],
        [_entry(hosts=["a.example.com", "A.example.com"])],
        [_entry(label='quote"label')],
        [_entry(unknown=True)],
    ],
)
def test_invalid_entry_sets_are_refused(entries: list[dict[str, object]]) -> None:
    assert walled_garden_allowed_resources_error({"entries": entries}) is not None


def test_value_must_be_an_object_with_entries() -> None:
    assert walled_garden_allowed_resources_error([_entry()]) is not None
    assert walled_garden_allowed_resources_error("not json") is not None
    assert walled_garden_allowed_resources_error({"entries": [_entry()]}) is None


def test_settings_owner_refuses_invalid_values_on_write(db_session) -> None:
    service = DomainSettings(SettingDomain.radius)
    bad = {"entries": [_entry(hosts=["1.2.3.4"])]}

    with pytest.raises(HTTPException) as excinfo:
        service.upsert_by_key(
            db_session,
            KEY,
            DomainSettingUpdate(value_type=SettingValueType.json, value_json=bad),
        )
    assert excinfo.value.status_code == 400
    assert "walled-garden" in str(excinfo.value.detail)


def test_settings_owner_accepts_a_toggled_preset(db_session) -> None:
    value = copy.deepcopy(DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES)
    value["entries"][0]["enabled"] = True  # type: ignore[index]
    service = DomainSettings(SettingDomain.radius)

    service.stage_upsert_by_key(
        db_session,
        KEY,
        DomainSettingUpdate(value_type=SettingValueType.json, value_json=value),
    )
    resolved = settings_spec.resolve_value(db_session, SettingDomain.radius, KEY)

    entries = WalledGardenAllowedResources.from_setting_value(resolved).entries
    assert entries[0].key == "paystack" and entries[0].enabled is True
