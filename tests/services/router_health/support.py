"""Shared builders for router-health tests (documentation-range addresses only)."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from app.services.router_health.policy import (
    EffectiveRouterPolicy,
    RouterHealthPolicy,
    built_in_defaults,
    effective_policy,
)
from app.services.router_health.routeros_export import ExportDocument, parse_export

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "router_health"
TODAY = date(2026, 10, 12)
POLLERS = ["192.0.2.1/32", "192.0.2.2/32", "192.0.2.3/32", "192.0.2.4/32"]

INPUT_V4_OK = "\n".join(
    [
        f'add action=accept chain=input comment="INPUT-PROTECT: accept {i}" protocol=tcp dst-port={1000 + i}'
        for i in range(9)
    ]
    + ['add action=drop chain=input comment="INPUT-PROTECT: drop all else"']
)
INPUT_V6_OK = "\n".join(
    [
        f'add action=accept chain=input comment="INPUT6-PROTECT: accept {i}" protocol=tcp dst-port={2000 + i}'
        for i in range(5)
    ]
    + ['add action=passthrough chain=input comment="INPUT6-PROTECT: count would-drop"']
)
SUSPENSION_OK = """\
add action=jump chain=forward comment="dotmac suspended quarantine" jump-target=dotmac-suspended src-address-list=suspended
add action=accept chain=dotmac-suspended comment="dns" dst-port=53 protocol=udp
add action=drop chain=dotmac-suspended comment="dotmac suspended drop"
"""


def build_export(
    *,
    identity: str = "Test Access",
    version: str = "7.16.2",
    v4: str | None = INPUT_V4_OK,
    v6: str | None = INPUT_V6_OK,
    suspension: str | None = SUSPENSION_OK,
    snmp: str = "/snmp\nset enabled=yes\n/snmp community\nset [ find default=yes ] addresses=192.0.2.1/32\n",
    ntp: str
    | None = "/system ntp client\nset enabled=yes\n/system ntp client servers\nadd address=192.0.2.9\n",
    radius_incoming: str | None = "/radius incoming\nset accept=yes\n",
    services: str = "/ip service\nset reverse-proxy disabled=yes\n",
    extra: str = "",
    pppoe: bool = True,
) -> str:
    parts = [f"# 2026-10-12 00:00:00 by RouterOS {version}\n"]
    if pppoe:
        parts.append(
            "/interface pppoe-server server\nadd interface=ether2 service-name=x\n"
        )
    parts.append("/ip firewall filter\n")
    if suspension:
        parts.append(suspension)
    if v4:
        parts.append(v4 + "\n")
    if v6 is not None:
        parts.append("/ipv6 firewall filter\n" + v6 + "\n")
    parts.append(services)
    parts.append(snmp)
    if ntp:
        parts.append(ntp)
    if radius_incoming:
        parts.append(radius_incoming)
    parts.append(f'/system identity\nset name="{identity}"\n')
    parts.append(extra)
    return "".join(parts)


def default_policy(**defaults_update: object) -> RouterHealthPolicy:
    base = built_in_defaults().model_copy(update={"snmp_allowed_sources": POLLERS})
    if defaults_update:
        base = base.model_copy(update=defaults_update)
    return RouterHealthPolicy(defaults=base)


def effective(
    policy: RouterHealthPolicy | None = None,
    *,
    identity: str | None = "Test Access",
    name: str = "Test",
    today: date = TODAY,
) -> EffectiveRouterPolicy:
    return effective_policy(
        policy or default_policy(),
        identity=identity,
        router_name=name,
        router_id=uuid.uuid4(),
        today=today,
    )


def parsed(text: str) -> ExportDocument:
    return parse_export(text)
