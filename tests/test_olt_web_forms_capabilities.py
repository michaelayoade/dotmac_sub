import pytest
from pydantic import ValidationError

from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services.network.olt_web_forms import (
    build_form_model,
    create_payload,
    parse_form_values,
)


def _base_form(**extra):
    data = {
        "name": "BOI",
        "hostname": "boi-olt",
        "mgmt_ip": "10.0.0.1",
        "vendor": "Huawei",
        "model": "MA5608T",
        "firmware_version": "V800R013C00 SPC105",
        "ssh_port": "22",
        "snmp_port": "161",
        "is_active": "false",
    }
    data.update(extra)
    return data


def test_olt_form_does_not_submit_capabilities_without_manual_override():
    values = parse_form_values(
        _base_form(
            supports_ont_internet_config="true",
            supports_ont_wan_config="true",
            supports_ont_home_gateway_config="true",
            wan_provisioning_mode="omci_wan_config",
        )
    )

    payload = create_payload(values)

    assert payload.firmware_version == "V800R013C00 SPC105"
    assert "firmware_version" in payload.model_fields_set
    assert "supports_ont_internet_config" not in payload.model_fields_set
    assert "supports_ont_wan_config" not in payload.model_fields_set
    assert "supports_ont_home_gateway_config" not in payload.model_fields_set
    assert "wan_provisioning_mode" not in payload.model_fields_set
    assert payload.capabilities_source == "auto"
    assert "capabilities_source" in payload.model_fields_set


def test_olt_form_submits_capabilities_with_manual_override():
    values = parse_form_values(
        _base_form(
            manual_capability_override="true",
            supports_ont_internet_config="true",
            supports_ont_wan_config="true",
            supports_ont_home_gateway_config="true",
            wan_provisioning_mode="omci_wan_config",
        )
    )

    payload = create_payload(values)

    assert payload.firmware_version == "V800R013C00 SPC105"
    assert payload.supports_ont_internet_config is True
    assert payload.supports_ont_wan_config is True
    assert payload.supports_ont_home_gateway_config is True
    assert payload.wan_provisioning_mode == "omci_wan_config"
    assert payload.capabilities_source == "manual"
    assert "wan_provisioning_mode" in payload.model_fields_set


def test_olt_form_model_marks_manual_capability_override(db_session):
    from app.models.network import OLTDevice

    olt = OLTDevice(
        name="Manual OLT",
        hostname="manual-olt",
        capabilities_source="manual",
    )
    db_session.add(olt)
    db_session.flush()

    form_model = build_form_model(db_session, olt)

    assert form_model.manual_capability_override is True


def test_olt_schema_rejects_invalid_capability_values():
    with pytest.raises(ValidationError):
        OLTDeviceCreate(name="Bad OLT", wan_provisioning_mode="omci")

    with pytest.raises(ValidationError):
        OLTDeviceUpdate(capabilities_source="manual_override")
