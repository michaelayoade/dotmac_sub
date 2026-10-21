"""Sentinel secrets planted in a snapshot must never reach findings, logs or details."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.services.router_health import findings
from app.services.router_health.policy import parse_policy
from app.services.router_health.posture import (
    PostureThresholds,
    RouterInput,
    SnapshotInput,
    compute_posture,
)
from tests.services.router_health.support import TODAY, build_export, default_policy

SENTINELS = (
    "SENTINEL-COMMUNITY-xyzzy",
    "SENTINEL-IPSEC-SECRET-plugh",
    "SENTINEL-PASSWORD-frobozz",
    "SENTINEL-ROMON-SECRET-quux",
)


def test_sentinels_never_appear_in_findings_details_or_logs(caplog) -> None:
    extra = (
        "/ip ipsec identity\nadd secret=SENTINEL-IPSEC-SECRET-plugh peer=p\n"
        "/user\nadd name=a password=SENTINEL-PASSWORD-frobozz group=full\n"
        "/tool romon\nset enabled=yes secrets=SENTINEL-ROMON-SECRET-quux\n"
    )
    snmp = (
        "/snmp\nset enabled=yes\n/snmp community\n"
        "add name=SENTINEL-COMMUNITY-xyzzy addresses=203.0.113.0/24 write-access=yes\n"
    )
    text = build_export(snmp=snmp, extra=extra, ntp=None)
    now = datetime(2026, 10, 12, tzinfo=UTC)
    caplog.set_level(logging.DEBUG)
    comp = compute_posture(
        routers=[
            RouterInput(
                uuid.uuid4(), "r", "192.0.2.5", SnapshotInput(uuid.uuid4(), now, text)
            )
        ],
        loaded_policy=parse_policy(
            default_policy().model_dump(mode="json"), today=TODAY
        ),
        suspended_list="suspended",
        thresholds=PostureThresholds(),
        now=now,
        today=TODAY,
    )
    assert comp.findings
    rendered = repr(comp.findings) + caplog.text
    for finding in comp.findings:
        obs = findings.to_observability_finding(finding, mode="shadow")
        rendered += repr(obs)
    for sentinel in SENTINELS:
        assert sentinel not in rendered
