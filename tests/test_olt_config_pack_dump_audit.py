from collections import Counter

from app.services.network.olt_config_pack_dump_audit import parse_olt_dump_profiles


def test_parse_olt_dump_profiles_reads_line_profiles_and_usage():
    parsed = parse_olt_dump_profiles(
        """
 ont tr069-server-profile add profile-id 2 profile-name "DotMac-ACS" url "http://10.10.41.1:7547" user "acs" "secret"
 ont-lineprofile gpon profile-id 1 profile-name "Broken"
  gem add 1 eth tcont 1
  commit
  quit
 ont-lineprofile gpon profile-id 40 profile-name "SMARTOLT_FLEXIBLE_GPON"
  tr069-management enable
  tcont 1 dba-profile-id 50
  gem add 1 eth tcont 1
  gem add 2 eth tcont 2
  gem mapping 3 1 priority 5
  commit
  quit
 interface gpon 0/2
  ont add 0 1 sn-auth "4857544312345678" omci ont-lineprofile-id 40 ont-srvprofile-id 41
  ont add 0 2 sn-auth "4857544387654321" omci ont-lineprofile-id 40 ont-srvprofile-id 41
  ont add 0 3 sn-auth "4857544387654322" omci ont-lineprofile-id 1 ont-srvprofile-id 1
"""
    )

    assert parsed.tr069_profiles[2].name == "DotMac-ACS"
    assert parsed.line_profiles[1].gem_indexes == {1}
    assert not parsed.line_profiles[1].tr069_management_enabled
    assert parsed.line_profiles[40].gem_indexes == {1, 2, 3}
    assert parsed.line_profiles[40].tr069_management_enabled
    assert parsed.ont_line_profile_counts == Counter({40: 2, 1: 1})
