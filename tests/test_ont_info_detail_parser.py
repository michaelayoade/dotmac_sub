from app.services.network.parsers import parse_ont_info_detail


def test_parse_ont_info_detail_extracts_profile_and_equipment_fields():
    entry = parse_ont_info_detail(
        """
        F/S/P               : 0/1/7
        ONT-ID              : 5
        SN                  : 48575443348F8A84
        Run state           : online
        Config state        : normal
        Match state         : mismatch
        Equipment-ID        : EG8145V5
        Main Software Version : V5R020C10S120
        Line profile ID     : 40
        Service profile ID  : 41
        Last down cause     : dying-gasp
        TR069 server profile: 2
        """
    )

    assert entry is not None
    assert entry.fsp == "0/1/7"
    assert entry.ont_id == 5
    assert entry.equipment_id == "EG8145V5"
    assert entry.service_profile_id == 41
    assert entry.line_profile_id == 40
    assert entry.match_state == "mismatch"
    assert entry.last_down_cause == "dying-gasp"
    assert entry.tr069_profile_id == 2


def test_parse_ont_info_detail_accepts_ont_sn_alias():
    entry = parse_ont_info_detail(
        """
        F/S/P               : 0/2/9
        ONT ID              : 12
        Ont SN              : HWTC600AC29C
        Run state           : online
        """
    )

    assert entry is not None
    assert entry.fsp == "0/2/9"
    assert entry.ont_id == 12
    assert entry.serial_number == "HWTC600AC29C"
