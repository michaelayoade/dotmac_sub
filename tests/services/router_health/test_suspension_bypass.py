"""Suspension-path bypass detection: chain order, narrowed jump, exclusion list."""

from __future__ import annotations

import pytest

from app.services.router_health.contracts import CheckId, HealthPriority
from app.services.router_health.policy import RouterHealthPolicy
from app.services.router_health.posture_checks import evaluate_posture
from tests.services.router_health.support import (
    build_export,
    default_policy,
    effective,
    parsed,
)

PORTAL = "192.0.2.3"
JUMP = (
    'add action=jump chain=forward comment="dotmac suspended quarantine" '
    "dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended "
    "src-address-list=suspended\n"
)
DNS = (
    'add action=accept chain=dotmac-suspended comment="dns" dst-port=53 protocol=udp\n'
)
PORTAL_ALLOW = (
    'add action=accept chain=dotmac-suspended comment="portal" '
    f"dst-address={PORTAL} dst-port=80,443 protocol=tcp\n"
)
REJECT = "add action=reject chain=dotmac-suspended reject-with=icmp-admin-prohibited\n"
DROP = "add action=drop chain=dotmac-suspended\n"
GOOD_CHAIN = DNS + PORTAL_ALLOW + REJECT + DROP


def findings(suspension: str, extra: str = "", eff=None):
    text = build_export(suspension=suspension, extra=extra)
    result = evaluate_posture(
        parsed(text),
        eff or effective(),
        router_label="Test",
        suspended_list="suspended",
    )
    legacy = {CheckId.splynx_legacy_list, CheckId.splynx_legacy_chain}
    return [
        (f.check, f.qualifier, f.priority)
        for f in result.findings
        if f.check not in legacy  # known low-priority legacy noise, tested elsewhere
    ]


def has(found, check: CheckId, qualifier_prefix: str = "") -> bool:
    return any(c is check and q.startswith(qualifier_prefix) for c, q, _ in found)


def test_kubwa_style_chain_has_no_finding() -> None:
    assert findings(JUMP + GOOD_CHAIN) == []


def test_unconditional_return_before_drop_is_flagged() -> None:
    chain = DNS + "add action=return chain=dotmac-suspended\n" + REJECT + DROP
    found = findings(JUMP + chain)
    assert has(found, CheckId.suspension_chain, "bypass")
    assert (
        CheckId.suspension_chain,
        "bypass-dotmac-suspended",
        HealthPriority.critical,
    ) in found


def test_conditional_return_is_flagged() -> None:
    chain = (
        DNS
        + "add action=return chain=dotmac-suspended connection-state=established\n"
        + DROP
    )
    assert has(findings(JUMP + chain), CheckId.suspension_chain, "bypass")


@pytest.mark.parametrize(
    "rule",
    [
        "add action=accept chain=dotmac-suspended protocol=tcp",
        "add action=accept chain=dotmac-suspended",
        "add action=accept chain=dotmac-suspended dst-port=53 protocol=tcp",
        "add action=accept chain=dotmac-suspended dst-port=53,443 protocol=udp",
        f"add action=accept chain=dotmac-suspended dst-address={PORTAL} dst-port=22 protocol=tcp",
        "add action=accept chain=dotmac-suspended dst-address=198.51.100.0/24 dst-port=443 protocol=tcp",
        "add action=fasttrack-connection chain=dotmac-suspended",
    ],
)
def test_unexpected_accept_before_drop_is_flagged(rule: str) -> None:
    found = findings(JUMP + DNS + rule + "\n" + DROP)
    assert has(found, CheckId.suspension_chain, "bypass")


def test_accept_after_terminal_drop_is_unreachable_and_ignored() -> None:
    assert (
        findings(JUMP + DNS + DROP + "add action=accept chain=dotmac-suspended\n") == []
    )


def test_narrow_drop_does_not_count_as_terminal() -> None:
    chain = "add action=drop chain=dotmac-suspended protocol=icmp\n" + (
        "add action=accept chain=dotmac-suspended\n" + DROP
    )
    assert has(findings(JUMP + chain), CheckId.suspension_chain, "bypass")


def test_disabled_bypass_rule_is_ignored() -> None:
    chain = DNS + "add action=return chain=dotmac-suspended disabled=yes\n" + DROP
    assert findings(JUMP + chain) == []


def test_configured_portal_address_is_enforced() -> None:
    policy = default_policy(suspension_portal_addresses=["192.0.2.77"])
    eff = effective(policy)
    assert has(findings(JUMP + GOOD_CHAIN, eff=eff), CheckId.suspension_chain, "bypass")
    ok_chain = DNS + PORTAL_ALLOW.replace(PORTAL, "192.0.2.77") + DROP
    assert findings(JUMP + ok_chain, eff=eff) == []


@pytest.mark.parametrize(
    "jump",
    [
        JUMP.replace("jump-target", "in-interface=ether99 jump-target"),
        JUMP.replace("jump-target", "connection-state=new jump-target"),
        JUMP.replace("jump-target", "protocol=tcp jump-target"),
        JUMP.replace("jump-target", "dst-address=198.51.100.0/24 jump-target"),
        JUMP.replace(
            "dst-address-list=!splynx-allowed-resources",
            "dst-address-list=splynx-allowed-resources",
        ),
    ],
)
def test_narrowed_forward_jump_is_flagged(jump: str) -> None:
    found = findings(jump + GOOD_CHAIN)
    assert (CheckId.suspension_jump, "narrowed", HealthPriority.critical) in found


def exclusion(*entries: str) -> str:
    return (
        "/ip firewall address-list\n"
        + "".join(f"add address={e} list=splynx-allowed-resources\n" for e in entries)
        + "/ip firewall filter\n"
    )


@pytest.mark.parametrize("entry", ["0.0.0.0/0", "10.0.0.0/8", "198.51.100.0/23"])
def test_broad_exclusion_prefix_is_flagged(entry: str) -> None:
    found = findings(JUMP + GOOD_CHAIN, exclusion(PORTAL, entry))
    assert (
        CheckId.suspension_chain,
        "exclusion-splynx-allowed-resources",
        HealthPriority.critical,
    ) in found


def test_narrow_exclusion_entries_pass() -> None:
    assert findings(JUMP + GOOD_CHAIN, exclusion(PORTAL, "198.51.100.0/24")) == []


def test_exclusion_prefix_limit_is_configurable() -> None:
    eff = effective(default_policy(suspension_exclusion_max_prefix_breadth=16))
    assert findings(JUMP + GOOD_CHAIN, exclusion("198.51.100.0/20"), eff=eff) == []
    assert findings(JUMP + GOOD_CHAIN, exclusion("10.0.0.0/8"), eff=eff)


def test_too_many_exclusion_entries_is_flagged() -> None:
    eff = effective(default_policy(suspension_exclusion_max_entries=3))
    many = exclusion(*(f"198.51.100.{i}" for i in range(4)))
    found = findings(JUMP + GOOD_CHAIN, many, eff=eff)
    assert (
        CheckId.suspension_chain,
        "exclusion-splynx-allowed-resources",
        HealthPriority.high,
    ) in found
    assert (
        findings(
            JUMP + GOOD_CHAIN,
            exclusion(*(f"198.51.100.{i}" for i in range(3))),
            eff=eff,
        )
        == []
    )


def test_address_range_wider_than_limit_is_flagged() -> None:
    found = findings(JUMP + GOOD_CHAIN, exclusion("10.0.0.1-10.0.255.254"))
    assert has(found, CheckId.suspension_chain, "exclusion")


def test_lokogoma_pre_fix_shape_is_critical() -> None:
    """Jump into an empty splynx-blocked chain that holds only a portal accept."""
    legacy = (
        "add action=jump chain=forward dst-address-list=!splynx-allowed-resources "
        "jump-target=splynx-blocked src-address-list=suspended\n"
        f"add action=accept chain=splynx-blocked dst-address={PORTAL} dst-port=80,443 protocol=tcp\n"
    )
    found = findings(legacy)
    assert (CheckId.suspension_jump, "0", HealthPriority.critical) in found


def test_policy_rejects_bad_portal_and_limits() -> None:
    raw = default_policy().model_dump(mode="json")
    raw["defaults"]["suspension_portal_addresses"] = ["not-an-ip"]
    with pytest.raises(ValueError):
        RouterHealthPolicy.model_validate(raw)
