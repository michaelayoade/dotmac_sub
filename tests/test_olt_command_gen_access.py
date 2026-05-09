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
        generate_dba_profile_commands,
        generate_line_profile_commands,
        generate_service_profile_commands,
        generate_traffic_table_commands,
    )

    assert generate_dba_profile_commands(
        profile_id=50,
        name="DOTMAC_100M",
        profile_type="type3",
        assured_bw=50000,
        max_bw=100000,
    ) == [
        'dba-profile add profile-id 50 profile-name "DOTMAC_100M" type3 assure 50000 max 100000'
    ]

    assert generate_traffic_table_commands(
        index=6,
        name="DOTMAC_100M_IN",
        cir=50000,
        pir=100000,
        priority=0,
    ) == [
        'traffic table ip index 6 name "DOTMAC_100M_IN" cir 50000 pir 100000 priority 0'
    ]

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


def test_dba_profile_generator_validates_type_requirements() -> None:
    from app.services.network.olt_command_gen import generate_dba_profile_commands

    assert generate_dba_profile_commands(
        profile_id=51,
        name="FIXED",
        profile_type="type1",
        fixed_bw=10000,
    ) == ['dba-profile add profile-id 51 profile-name "FIXED" type1 fix 10000']

    assert generate_dba_profile_commands(
        profile_id=52,
        name="MIXED",
        profile_type="type5",
        fixed_bw=1000,
        assured_bw=5000,
        max_bw=10000,
    ) == [
        'dba-profile add profile-id 52 profile-name "MIXED" type5 fix 1000 assure 5000 max 10000'
    ]

    with pytest.raises(ValueError, match="type3 DBA profile requires"):
        generate_dba_profile_commands(
            profile_id=50,
            name="BAD",
            profile_type="type3",
            assured_bw=50000,
        )

    with pytest.raises(ValueError, match="max_bw"):
        generate_dba_profile_commands(
            profile_id=50,
            name="BAD",
            profile_type="type3",
            assured_bw=100000,
            max_bw=50000,
        )


def test_traffic_table_generator_validates_rates_and_priority() -> None:
    from app.services.network.olt_command_gen import generate_traffic_table_commands

    with pytest.raises(ValueError, match="pir"):
        generate_traffic_table_commands(
            index=6,
            name="BAD",
            cir=100000,
            pir=50000,
        )

    with pytest.raises(ValueError, match="priority"):
        generate_traffic_table_commands(
            index=6,
            name="BAD",
            cir=0,
            pir=100000,
            priority=8,
        )


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
