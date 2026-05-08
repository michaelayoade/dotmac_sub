from scripts.migration.import_olt_running_config_intent import parse_config


def test_parse_config_preserves_and_validates_internet_stack_indices(tmp_path):
    config_path = tmp_path / "garki.cfg"
    config_path.write_text(
        """
interface gpon 0/2
 ont add 11 13 sn-auth "4857544306351E9C" omci ont-lineprofile-id 40 ont-srvprofile-id 13 desc "Dr Ezike"
 ont ipconfig 11 13 static ip-address 172.16.201.12 mask 255.255.255.0 vlan 201 priority 2 gateway 172.16.201.1
 ont ipconfig 11 13 ip-index 1 pppoe vlan 203 priority 5 user-account username "100025868" password "redacted"
 ont tr069-server-config 11 13 profile-id 2
 ont internet-config 11 13 ip-index 1
 ont wan-config 11 13 ip-index 0 profile-id 0
""",
        encoding="utf-8",
    )

    parsed = parse_config(config_path)[0]
    snapshot = parsed.snapshot()

    assert parsed.external_id == "0/2/11.13"
    assert snapshot["internet_stack"] == {
        "internet_ip_index": None,
        "pppoe_ip_index": 1,
        "internet_config_ip_index": 1,
        "wan_config_ip_index": 0,
        "wan_config_profile_id": 0,
        "validation_status": "invalid",
        "validation_errors": [
            "Misaligned internet ip-index values: pppoe=1, "
            "internet-config=1, wan-config=0"
        ],
    }
