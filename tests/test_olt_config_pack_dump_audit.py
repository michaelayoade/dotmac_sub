from collections import Counter

from app.models.network import OLTDevice
from app.services.network.olt_config_pack_dump_audit import (
    apply_dump_audit_suggestions,
    audit_olt_config_pack_dump,
    parse_olt_dump_profiles,
)


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


def test_dump_audit_does_not_require_legacy_line_profile_defaults(
    db_session,
    tmp_path,
):
    olt = OLTDevice(
        name="Dump Audit OLT",
        is_active=True,
        config_pack={"tr069_olt_profile_id": 2},
    )
    db_session.add(olt)
    db_session.flush()
    dump_path = tmp_path / "dump.cfg"
    dump_path.write_text(
        """
 ont tr069-server-profile add profile-id 2 profile-name "DotMac-ACS" url "http://10.10.41.1:7547" user "acs" "secret"
 ont-lineprofile gpon profile-id 40 profile-name "SMARTOLT_FLEXIBLE_GPON"
  tr069-management enable
  gem add 1 eth tcont 1
  commit
  quit
 interface gpon 0/2
  ont add 0 1 sn-auth "4857544312345678" omci ont-lineprofile-id 40 ont-srvprofile-id 41
""",
        encoding="utf-8",
    )

    report = audit_olt_config_pack_dump(
        db_session,
        str(olt.id),
        dump_roots=(tmp_path,),
    )

    assert report.is_valid is True
    assert report.suggested_updates == {}
    assert "line_profile_id" not in report.observed
    assert apply_dump_audit_suggestions(db_session, [report]) == 0
