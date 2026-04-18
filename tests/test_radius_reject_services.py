"""Tests for RADIUS reject rule command generation."""

import ipaddress

from app.models.catalog import SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import radius_reject


def test_firewall_commands_include_negative_captive_rules_when_enabled():
    networks = {
        "negative": ipaddress.ip_network("10.12.0.0/16"),
    }
    commands = radius_reject._firewall_commands(
        networks,
        captive_enabled=True,
        captive_portal_ip="203.0.113.10/32",
    )

    joined = "\n".join(commands)
    assert 'chain="dotmac-block-chain"' in joined
    assert "dotmac-block-allow-dns" in joined
    assert "dotmac-block-allow-oss" in joined
    assert "dotmac-block-reject-non-oss-tcp" in joined
    assert "dotmac-block-reject-non-oss-udp" in joined
    assert "dotmac-block-drop-non-oss" in joined
    assert "dotmac-reject-allow-dns-negative" in joined
    assert "dotmac-reject-allow-oss-negative" in joined
    assert "dotmac-negative-redirect-http-negative" in joined
    assert "to-addresses=203.0.113.10" in joined
    assert (
        'action=jump jump-target="dotmac-block-chain" comment="dotmac-reject-jump-negative"'
        not in joined
    )

    allow_idx = next(
        i for i, cmd in enumerate(commands) if "dotmac-reject-allow-oss-negative" in cmd
    )
    redirect_idx = next(
        i
        for i, cmd in enumerate(commands)
        if "dotmac-negative-redirect-http-negative" in cmd
    )
    assert allow_idx < redirect_idx


def test_firewall_commands_skip_captive_rules_when_disabled():
    networks = {
        "negative": ipaddress.ip_network("10.12.0.0/16"),
    }
    commands = radius_reject._firewall_commands(
        networks,
        captive_enabled=False,
        captive_portal_ip="203.0.113.10",
    )

    joined = "\n".join(commands)
    assert (
        '/ip firewall nat add chain=dstnat src-address-list="dotmac-reject-negative"'
        not in joined
    )
    assert "dotmac-reject-allow-dns-negative" in joined
    assert "dotmac-reject-allow-oss-negative" in joined
    assert (
        'action=jump jump-target="dotmac-block-chain" comment="dotmac-reject-jump-negative"'
        not in joined
    )


def test_firewall_commands_non_negative_reasons_jump_to_block_chain():
    networks = {
        "blocked": ipaddress.ip_network("10.11.0.0/16"),
    }
    commands = radius_reject._firewall_commands(
        networks,
        captive_enabled=True,
        captive_portal_ip="149.102.158.144",
    )

    joined = "\n".join(commands)
    assert "dotmac-reject-allow-dns-blocked" in joined
    assert "dotmac-reject-allow-oss-blocked" in joined
    assert 'jump-target="dotmac-block-chain"' in joined
    assert "dotmac-reject-jump-blocked" in joined


def test_block_chain_rejects_tcp_udp_then_drops_rest():
    networks = {
        "blocked": ipaddress.ip_network("10.11.0.0/16"),
    }
    commands = radius_reject._firewall_commands(
        networks,
        captive_enabled=True,
        captive_portal_ip="149.102.158.144",
    )

    tcp_idx = next(
        i
        for i, cmd in enumerate(commands)
        if "dotmac-block-reject-non-oss-tcp" in cmd and " protocol=tcp " in cmd
    )
    udp_idx = next(
        i
        for i, cmd in enumerate(commands)
        if "dotmac-block-reject-non-oss-udp" in cmd and " protocol=udp " in cmd
    )
    drop_idx = next(
        i
        for i, cmd in enumerate(commands)
        if "dotmac-block-drop-non-oss" in cmd and " action=drop " in cmd
    )
    assert tcp_idx < drop_idx
    assert udp_idx < drop_idx


def test_enforce_subscription_reject_ip_treats_blocked_subscription_as_captive_redirect(
    db_session, subscription, subscriber
):
    subscriber.captive_redirect_enabled = True
    subscription.status = SubscriptionStatus.blocked
    db_session.add_all(
        [
            DomainSetting(
                domain=SettingDomain.radius,
                key="reject_ip_negative",
                value_text="10.12.0.0/16",
                value_type=SettingValueType.string,
                is_active=True,
            ),
            DomainSetting(
                domain=SettingDomain.radius,
                key="reject_ip_blocked",
                value_text="10.11.0.0/16",
                value_type=SettingValueType.string,
                is_active=True,
            ),
        ]
    )
    db_session.commit()

    result = radius_reject.enforce_subscription_reject_ip(
        db_session, str(subscription.id)
    )

    assert result["ok"] is True
    assert result["mode"] == "block"
    assigned_ip = ipaddress.ip_address(result["ip"])
    assert assigned_ip in ipaddress.ip_network("10.12.0.0/16")
    assert assigned_ip not in ipaddress.ip_network("10.11.0.0/16")
