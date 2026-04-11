"""Tests for scripts/bulk_tr069_rebind.py helper functions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from scripts.bulk_tr069_rebind import (
    _build_fsp,
    _parse_ont_id_from_external,
    _resolve_linked_acs_profile,
)

# ── ONT-ID parsing ───────────────────────────────────────────────────


def test_parse_ont_id_plain_digit():
    assert _parse_ont_id_from_external("5") == 5


def test_parse_ont_id_huawei_format():
    assert _parse_ont_id_from_external("huawei:4194320640.5") == 5


def test_parse_ont_id_smartolt_format_returns_none():
    assert _parse_ont_id_from_external("smartolt:HWTC93A47984") is None


def test_parse_ont_id_none_input():
    assert _parse_ont_id_from_external(None) is None


def test_parse_ont_id_empty_string():
    assert _parse_ont_id_from_external("") is None


# ── FSP building ─────────────────────────────────────────────────────


def test_build_fsp_normal():
    assert _build_fsp("0/2", "1") == "0/2/1"


def test_build_fsp_missing_board():
    assert _build_fsp(None, "1") is None


def test_build_fsp_missing_port():
    assert _build_fsp("0/2", None) is None


# ── Profile resolution ──────────────────────────────────────────────


def _make_profile(profile_id: int, name: str, acs_url: str = "") -> SimpleNamespace:
    return SimpleNamespace(profile_id=profile_id, name=name, acs_url=acs_url)


_PROFILE_PATCH = "app.services.network.olt_ssh_profiles.get_tr069_server_profiles"


def test_resolve_finds_profile_by_linked_acs_url_and_username():
    profiles = [
        _make_profile(1, "SmartOLT", "http://smartolt.example.com:7547"),
        _make_profile(3, "Primary ACS", "http://acs.example.com/cwmp"),
    ]
    olt = SimpleNamespace(
        name="Test OLT",
        tr069_acs_server=SimpleNamespace(
            name="Primary ACS",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            cwmp_password=None,
        ),
    )
    with patch(_PROFILE_PATCH, return_value=(True, "OK", profiles)):
        pid, msg = _resolve_linked_acs_profile(olt)
    assert pid == 3
    assert "Primary ACS" in msg


def test_resolve_finds_profile_when_username_not_parsed():
    profiles = [
        _make_profile(5, "GenieACS", "http://acs.example.com/cwmp"),
    ]
    olt = SimpleNamespace(
        name="Test OLT",
        tr069_acs_server=SimpleNamespace(
            name="Primary ACS",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            cwmp_password=None,
        ),
    )
    with patch(_PROFILE_PATCH, return_value=(True, "OK", profiles)):
        pid, msg = _resolve_linked_acs_profile(olt)
    assert pid == 5


def test_resolve_returns_none_when_no_match():
    profiles = [
        _make_profile(1, "SmartOLT", "http://smartolt.example.com:7547"),
    ]
    olt = SimpleNamespace(
        name="Test OLT",
        tr069_acs_server=SimpleNamespace(
            name="Primary ACS",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            cwmp_password=None,
        ),
    )
    with patch(_PROFILE_PATCH, return_value=(True, "OK", profiles)):
        pid, msg = _resolve_linked_acs_profile(olt, auto_create=False)
    assert pid is None
    assert "No linked ACS profile found" in msg


def test_resolve_returns_none_when_ssh_fails():
    olt = SimpleNamespace(
        name="Test OLT",
        tr069_acs_server=SimpleNamespace(
            name="Primary ACS",
            cwmp_url="http://acs.example.com/cwmp",
            cwmp_username="cwmp-user",
            cwmp_password=None,
        ),
    )
    with patch(_PROFILE_PATCH, return_value=(False, "SSH connection failed", [])):
        pid, msg = _resolve_linked_acs_profile(olt)
    assert pid is None
    assert "Cannot list profiles" in msg


def test_resolve_returns_none_without_linked_acs():
    olt = SimpleNamespace(name="Test OLT", tr069_acs_server=None)

    pid, msg = _resolve_linked_acs_profile(olt)

    assert pid is None
    assert "No linked ACS configured" in msg
