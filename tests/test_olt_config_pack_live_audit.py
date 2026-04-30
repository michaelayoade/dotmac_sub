from __future__ import annotations

from app.services.network.olt_config_pack_live_audit import (
    parse_line_profile_detail,
    parse_tr069_profile_detail,
)

LINE_PROFILE_DETAIL = """
  Profile-ID          :1
  Profile-name        :SPL_1_Unlimited_3
  TR069 management    :Enable
  TR069 IP index      :0
  <T-CONT   0>          DBA Profile-ID:10
   <Gem Index 0>
  <T-CONT   1>          DBA Profile-ID:11
   <Gem Index 1>
  <T-CONT   2>          DBA Profile-ID:12
   <Gem Index 2>
"""


def test_parse_line_profile_detail_extracts_gems_and_tr069() -> None:
    detail = parse_line_profile_detail(LINE_PROFILE_DETAIL, profile_id=1)

    assert detail.profile_id == 1
    assert detail.gem_indexes == {0, 1, 2}
    assert detail.tr069_management_enabled is True
    assert detail.tr069_ip_index == 0


def test_parse_line_profile_detail_handles_missing_tr069() -> None:
    detail = parse_line_profile_detail(
        """
        Profile-ID          :2
        TR069 management    :Disable
        <Gem Index 1>
        """,
        profile_id=2,
    )

    assert detail.gem_indexes == {1}
    assert detail.tr069_management_enabled is False
    assert detail.tr069_ip_index is None


def test_parse_tr069_profile_detail_detects_missing_profile() -> None:
    detail = parse_tr069_profile_detail(
        "Failure: The profile does not exist",
        profile_id=2,
    )

    assert detail.exists is False


def test_parse_tr069_profile_detail_extracts_profile_fields() -> None:
    detail = parse_tr069_profile_detail(
        """
        Profile-ID          :2
        Profile-name        :GenieACS
        URL                 :http://acs.example/cwmp
        """,
        profile_id=2,
    )

    assert detail.exists is True
    assert detail.name == "GenieACS"
    assert detail.acs_url == "http://acs.example/cwmp"
