from types import SimpleNamespace

from app.services.network.olt_ssh_ont.omci_config import (
    _parse_wan_config_display,
    _wan_config_display_commands,
)


def test_wan_config_display_commands_use_full_fsp_for_ma5800():
    olt = SimpleNamespace(model="MA5800-X2")

    commands = _wan_config_display_commands(olt, "0/2/6", 5)

    assert commands[0] == "display ont wan-config 0/2/6 5"
    assert commands[1] == "display ont wan-config 6 5"


def test_wan_config_display_commands_keep_short_form_first_for_older_huawei():
    olt = SimpleNamespace(model="MA5608T")

    commands = _wan_config_display_commands(olt, "0/2/6", 5)

    assert commands[0] == "display ont wan-config 6 5"
    assert commands[1] == "display ont wan-config 0/2/6 5"


def test_parse_wan_config_display_extracts_binding():
    output = """
      WAN IP index      : 1
      WAN profile ID    : 10
      WAN profile name  : wan-profile_10
      Connection type   : Route
      NAT switch        : Enable
    """

    assert _parse_wan_config_display(output) == {
        "ip_index": 1,
        "profile_id": 10,
        "profile_name": "wan-profile_10",
    }
