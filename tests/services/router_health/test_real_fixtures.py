"""Redacted real snapshots (Lokogoma, Gudu, Lagos Medallion) as parser/check fixtures.

The fixtures were extracted read-only from production on 2026-10-12 and
redacted on the host before saving: secrets removed, estate addresses mapped to
documentation ranges, and only the sections the checks need kept.
"""

from __future__ import annotations

import ipaddress
import re

import pytest

from app.services.router_health.contracts import CheckId
from app.services.router_health.posture_checks import evaluate_posture
from app.services.router_health.routeros_export import parse_export
from tests.services.router_health.support import FIXTURE_DIR, default_policy, effective

NAMES = ("Lokogoma", "Gudu", "Lagos_Medallion")
SECRET_KEYS = re.compile(
    r"(password|secret|psk|private-key|authentication-password|encryption-password)=(\S+)",
    re.I,
)


def read(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.rsc").read_text()


@pytest.mark.parametrize("name", NAMES)
def test_fixtures_contain_no_secret_values(name: str) -> None:
    text = read(name)
    for match in SECRET_KEYS.finditer(text):
        assert match.group(2) in {"REDACTED", '"REDACTED"'}, match.group(1)
    assert "community" not in re.sub(r"/snmp community|community-\d", "", text)


@pytest.mark.parametrize("name", NAMES)
def test_fixtures_contain_no_estate_addresses(name: str) -> None:
    allowed = [
        ipaddress.ip_network(n)
        for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "198.18.0.0/15")
    ]
    for token in re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", read(name)):
        address = ipaddress.ip_address(token)
        assert address.is_unspecified or any(address in net for net in allowed), token
    assert not re.search(r"\b[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}\b", read(name))
    assert not re.search(r"@(?!example\.invalid)\w", read(name))


@pytest.mark.parametrize("name", NAMES)
def test_fixtures_parse_fully(name: str) -> None:
    doc = parse_export(read(name))
    assert doc.parsed
    assert doc.coverage == 1.0
    assert doc.version is not None


def _findings(name: str):
    doc = parse_export(read(name))
    eff = effective(default_policy(), identity=doc.identity, name=name)
    return evaluate_posture(
        doc, eff, router_label=name, suspended_list="suspended"
    ).findings


def test_lokogoma_has_only_legacy_splynx_residue() -> None:
    found = {f.check for f in _findings("Lokogoma")}
    assert found == {CheckId.splynx_legacy_chain, CheckId.splynx_legacy_list}


def test_lokogoma_prefix_fix_state_is_critical_when_jump_points_at_legacy() -> None:
    text = read("Lokogoma").replace(
        "jump-target=dotmac-suspended src-address-list=suspended",
        "jump-target=splynx-blocked src-address-list=suspended",
    )
    doc = parse_export(text)
    eff = effective(default_policy(), identity=doc.identity, name="Lokogoma")
    result = evaluate_posture(
        doc, eff, router_label="Lokogoma", suspended_list="suspended"
    )
    critical = {f.check for f in result.findings if f.priority.value == "critical"}
    assert CheckId.suspension_jump in critical


def test_gudu_passes_input_chains_and_suspension() -> None:
    found = {f.check for f in _findings("Gudu")}
    assert CheckId.input_v4_missing not in found
    assert CheckId.input_v6_missing not in found
    assert CheckId.suspension_jump not in found
    assert CheckId.suspension_chain not in found


def test_lagos_medallion_real_gaps_are_reported() -> None:
    found = {f.check for f in _findings("Lagos_Medallion")}
    assert CheckId.input_v4_missing in found
    assert CheckId.reverse_proxy_enabled in found
    assert CheckId.snmp_exposed in found
