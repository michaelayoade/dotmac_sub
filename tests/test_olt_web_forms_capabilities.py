from types import SimpleNamespace

import pytest
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.schemas.network import OLTDeviceCreate, OLTDeviceUpdate
from app.services.network.olt_web_forms import (
    build_form_model,
    create_payload,
    parse_form_values,
    validate_values,
)

templates = Jinja2Templates(directory="templates")


def _template_request():
    return SimpleNamespace(
        state=SimpleNamespace(auth={"permission_keys": {"*"}}, csrf_token=""),
        url=SimpleNamespace(path="/admin/network/olts/test"),
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


def test_olt_form_parses_config_pack_wcd_defaults():
    values = parse_form_values(
        _base_form(
            default_internet_config_ip_index="1",
            default_wan_config_profile_id="10",
            pppoe_wcd_index="2",
            mgmt_wcd_index="1",
            voip_wcd_index="3",
        )
    )

    payload = create_payload(values)

    assert payload.config_pack is not None
    assert payload.config_pack["internet_config_ip_index"] == 1
    assert payload.config_pack["wan_config_profile_id"] == 10
    assert payload.config_pack["pppoe_wcd_index"] == 2
    assert payload.config_pack["mgmt_wcd_index"] == 1
    assert payload.config_pack["voip_wcd_index"] == 3


def test_olt_form_rejects_invalid_config_pack_indexes(db_session):
    values = parse_form_values(_base_form(default_internet_config_ip_index="33"))

    assert (
        validate_values(db_session, values)
        == "Internet IP index must be between 0 and 32"
    )

    values = parse_form_values(_base_form(pppoe_wcd_index="0"))

    assert (
        validate_values(db_session, values)
        == "PPPoE WCD index must be between 1 and 32"
    )


def test_olt_form_renders_config_pack_wcd_defaults(db_session):
    from app.models.network import OLTDevice

    olt = OLTDevice(
        name="Gwarimpa Huawei OLT",
        hostname="gwarimpa-olt",
        config_pack={
            "internet_config_ip_index": 1,
            "wan_config_profile_id": 10,
            "pppoe_wcd_index": 2,
            "mgmt_wcd_index": 1,
            "voip_wcd_index": 3,
        },
    )
    db_session.add(olt)
    db_session.flush()

    form_model = build_form_model(db_session, olt)
    html = templates.get_template("admin/network/olts/form.html").render(
        request=_template_request(),
        olt=form_model,
        olt_vlans=[],
        olt_ip_pools=[],
        tr069_servers=[],
    )

    assert 'name="default_internet_config_ip_index"' in html
    assert 'name="mgmt_wcd_index"' in html
    assert 'name="pppoe_wcd_index"' in html
    assert 'name="voip_wcd_index"' in html
    assert 'name="pppoe_wcd_index" id="pppoe_wcd_index" min="1" max="32"' in html
    assert 'value="2"' in html


def test_olt_form_hides_huawei_config_pack_fields_for_non_huawei(db_session):
    from app.models.network import OLTDevice

    olt = OLTDevice(
        name="Fiber OLT",
        hostname="fiber-olt",
        vendor="Ubiquiti",
        model="UFiber",
        config_pack={
            "internet_config_ip_index": 1,
            "pppoe_wcd_index": 2,
        },
    )
    db_session.add(olt)
    db_session.flush()

    form_model = build_form_model(db_session, olt)
    html = templates.get_template("admin/network/olts/form.html").render(
        request=_template_request(),
        olt=form_model,
        olt_vlans=[],
        olt_ip_pools=[],
        tr069_servers=[],
    )

    assert form_model.show_huawei_config_pack_fields is False
    assert 'name="default_internet_config_ip_index"' not in html
    assert 'name="pppoe_wcd_index"' not in html
    assert 'name="mgmt_wcd_index"' not in html
    assert 'name="voip_wcd_index"' not in html
