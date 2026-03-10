"""Tests for RADIUS reject rule command generation."""

import ipaddress

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
    assert "dotmac-negative-allow-portal" in joined
    assert "dotmac-negative-redirect-http" in joined
    assert "dotmac-negative-drop-https" in joined
    assert "to-addresses=203.0.113.10" in joined

    allow_idx = next(i for i, cmd in enumerate(commands) if "dotmac-negative-allow-portal" in cmd)
    drop_idx = next(
        i
        for i, cmd in enumerate(commands)
        if 'action=drop comment="dotmac-reject-drop-negative"' in cmd
    )
    assert allow_idx < drop_idx


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
    assert "dotmac-negative-allow-portal" not in joined
    assert "dotmac-negative-redirect-http" not in joined
    assert "dotmac-negative-drop-https" not in joined
    assert 'comment="dotmac-reject-drop-negative"' in joined
