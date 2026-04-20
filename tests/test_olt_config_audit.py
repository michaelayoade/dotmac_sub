from app.services.network.olt_config_audit import parse_huawei_running_config


def test_parse_huawei_running_config_extracts_ont_and_service_port() -> None:
    config = """
#
[gpon]
  <gpon>
 interface gpon 0/2
 ont add 1 7 sn-auth "4857544328201B9A" omci ont-lineprofile-id 10 ont-srvprofile-id 20 desc "TechSquad Africa"
 quit
#
service-port 401 vlan 201 gpon 0/2/1 ont 7 gemport 2 multi-service user-vlan 101 tag-transform translate
#
return
"""

    parsed = parse_huawei_running_config(config)

    assert len(parsed.ont_registrations) == 1
    registration = parsed.ont_registrations[0]
    assert registration.fsp == "0/2/1"
    assert registration.ont_id == 7
    assert registration.serial_number == "HWTC28201B9A"
    assert registration.raw_serial == "4857544328201B9A"
    assert registration.line_profile_id == 10
    assert registration.service_profile_id == 20
    assert registration.description == "TechSquad Africa"

    assert len(parsed.service_ports) == 1
    service_port = parsed.service_ports[0]
    assert service_port.index == 401
    assert service_port.vlan_id == 201
    assert service_port.fsp == "0/2/1"
    assert service_port.ont_id == 7
    assert service_port.gem_index == 2
    assert service_port.user_vlan == "101"
    assert service_port.tag_transform == "translate"


def test_parse_huawei_running_config_ignores_ont_add_without_interface() -> None:
    parsed = parse_huawei_running_config(
        'ont add 1 7 sn-auth "4857544328201B9A" omci ont-lineprofile-id 10'
    )

    assert parsed.ont_registrations == []
