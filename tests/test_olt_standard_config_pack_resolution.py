from __future__ import annotations

from types import SimpleNamespace

from app.services.network.config_pack_resolution import (
    resolve_effective_config_pack_stage,
)
from app.services.network.olt_config_pack import (
    VlanConfig,
    resolve_standard_olt_config_pack_profile,
)


class _ResolvedConfigPack(SimpleNamespace):
    def to_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def _huawei_olt(*, model: str, firmware_version: str) -> SimpleNamespace:
    return SimpleNamespace(
        id="olt-1",
        name=f"{model} OLT",
        vendor="Huawei",
        model=model,
        firmware_version=firmware_version,
        software_version=None,
        config_pack={},
        mgmt_ip_pool_id="pool-1",
        supports_ont_wan_config=True,
    )


def test_ma5800_x2_auto_selects_standard_config_pack():
    profile = resolve_standard_olt_config_pack_profile(
        _huawei_olt(
            model="MA5800-X2",
            firmware_version="MA5800V100R019C11 SPH216",
        )
    )

    assert profile is not None
    assert profile.key == "huawei-ma5800-x2-standard"
    assert profile.name == "Huawei MA5800-X2 standard pack"
    assert profile.command_profile_name == "huawei-ma5800-v100r019"
    assert profile.firmware_standardized is True


def test_ma5608t_auto_selects_standard_config_pack_on_target_firmware():
    profile = resolve_standard_olt_config_pack_profile(
        _huawei_olt(
            model="MA5608T",
            firmware_version="MA5600V800R018C10 SPH212",
        )
    )

    assert profile is not None
    assert profile.key == "huawei-ma5608t-standard"
    assert profile.name == "Huawei MA5608T standard pack"
    assert profile.command_profile_name == "huawei-ma5608t-v800r018"
    assert profile.firmware_standardized is True


def test_ma5608t_legacy_firmware_keeps_pack_key_but_marks_not_standardized():
    profile = resolve_standard_olt_config_pack_profile(
        _huawei_olt(
            model="MA5608T",
            firmware_version="MA5600V800R013C00 SPC105",
        )
    )

    assert profile is not None
    assert profile.key == "huawei-ma5608t-standard"
    assert profile.command_profile_name == "huawei-ma5608t-v800r013"
    assert profile.firmware_standardized is False
    assert profile.standardization_note


def test_effective_config_pack_stage_accepts_detected_pack_with_empty_raw_json(
    db_session,
):
    olt = _huawei_olt(
        model="MA5800-X2",
        firmware_version="MA5800V100R019C11 SPH216",
    )
    ont = SimpleNamespace(olt_device_id=olt.id)
    config_pack = _ResolvedConfigPack(
        internet_vlan=VlanConfig(tag=200, id="internet-vlan"),
        management_vlan=VlanConfig(tag=201, id="management-vlan"),
        tr069_vlan=VlanConfig(),
        tr069_acs_server_id="acs-1",
        tr069_olt_profile_id=2,
        wan_provisioning_mode="omci_wan_config",
        wan_config_profile_id=1,
        mgmt_traffic_table_inbound=None,
        mgmt_traffic_table_outbound=None,
        internet_traffic_table_inbound=None,
        internet_traffic_table_outbound=None,
    )
    effective_config = {
        "config_pack": config_pack,
        "desired_config_keys": [],
        "values": {
            "wan_mode": "dhcp",
            "wan_vlan": 200,
            "mgmt_vlan": 201,
            "tr069_acs_server_id": "acs-1",
            "tr069_olt_profile_id": 2,
            "internet_config_ip_index": 1,
            "wan_config_profile_id": 1,
            "wan_provisioning_mode": "omci_wan_config",
            "mgmt_wcd_index": 1,
            "pppoe_wcd_index": 2,
            "standard_pack_key": "huawei-ma5800-x2-standard",
            "standard_pack_family": "MA5800-X2",
            "command_profile_name": "huawei-ma5800-v100r019",
            "firmware_standardized": True,
        },
    }

    resolved, result = resolve_effective_config_pack_stage(
        db_session,
        ont,
        effective_config=effective_config,
        olt=olt,
    )

    assert resolved == effective_config
    assert result.success is True
    assert (
        result.data["effective_values"]["standard_pack_key"]
        == "huawei-ma5800-x2-standard"
    )
