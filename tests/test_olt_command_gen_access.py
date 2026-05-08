from __future__ import annotations

import pytest


def test_generate_native_vlan_commands_for_bridged_service() -> None:
    from app.services.network.olt_command_gen import (
        HuaweiCommandGenerator,
        OntProvisioningContext,
        ProvisioningSpec,
        WanServiceSpec,
    )

    spec = ProvisioningSpec(
        wan_services=[
            WanServiceSpec(
                service_type="internet",
                vlan_id=203,
                gem_index=1,
                connection_type="bridged",
                cos_priority=3,
                bridge_eth_ports=[1, 2],
            )
        ]
    )
    context = OntProvisioningContext(frame=0, slot=2, port=11, ont_id=13)

    command_sets = HuaweiCommandGenerator.generate_native_vlan_commands(
        spec,
        context,
    )

    assert command_sets[0].commands == [
        "interface gpon 0/2",
        "ont port native-vlan 11 13 eth 1 vlan 203 priority 3",
        "ont port native-vlan 11 13 eth 2 vlan 203 priority 3",
        "quit",
    ]


def test_profile_creation_command_generators_validate_and_render() -> None:
    from app.services.network.olt_command_gen import (
        generate_line_profile_commands,
        generate_service_profile_commands,
    )

    assert generate_service_profile_commands(
        profile_id=41,
        name="HG8546M bridge",
        eth_ports=4,
        vlan=203,
    ) == [
        'ont-srvprofile gpon profile-id 41 profile-name "HG8546M bridge"',
        "ont-port eth 4",
        "port vlan eth 1 203",
        "commit",
        "quit",
    ]

    assert generate_line_profile_commands(
        profile_id=40,
        name="SMARTOLT_FLEXIBLE_GPON",
        tcont_id=1,
        dba_profile_id=50,
        gem_id=1,
        vlan=203,
    ) == [
        'ont-lineprofile gpon profile-id 40 profile-name "SMARTOLT_FLEXIBLE_GPON"',
        "tcont 1 dba-profile-id 50",
        "gem add 1 eth tcont 1",
        "gem mapping 1 0 vlan 203",
        "commit",
        "quit",
    ]

    with pytest.raises(Exception):
        generate_service_profile_commands(profile_id=41, name='bad"name')


def test_service_port_command_validates_vlan_and_gem() -> None:
    from app.services.network.olt_command_gen import build_service_port_command

    with pytest.raises(Exception):
        build_service_port_command(
            fsp="0/2/11",
            ont_id=13,
            gem_index=1,
            vlan_id=4095,
        )

    with pytest.raises(Exception):
        build_service_port_command(
            fsp="0/2/11",
            ont_id=13,
            gem_index=256,
            vlan_id=203,
        )
