"""Shared builders (documentation-range addresses and invented names only)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.services.composition.layers import Layer, LayerRevision
from app.services.router_control.plugin import (
    ROUTER_PLUGIN,
    RouterBinding,
    build_subject,
)
from app.services.router_control.report import SnapshotInput, build_router_report
from app.services.router_control.seed_layers import seed_layer_revisions
from app.services.router_health.routeros_export import ExportDocument, parse_export
from tests.services.router_health.support import FIXTURE_DIR, build_export

TODAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
LOOPBACK = "198.51.100.97"
POLLER_SNMP = (
    "/snmp\nset enabled=yes trap-target=192.0.2.1 src-address=198.51.100.97\n"
    "/snmp community\nset [ find default=yes ] addresses=192.0.2.1/32,192.0.2.2/32,"
    "192.0.2.3/32,192.0.2.4/32\n"
)
SERVICES_OK = (
    "/ip service\nset telnet disabled=yes\nset ftp disabled=yes\nset www disabled=yes\n"
    "set api-ssl disabled=yes\nset reverse-proxy disabled=yes\n"
    "/tool bandwidth-server\nset authenticate=yes enabled=no\n"
)


def binding(name: str = "r-a", **kw: Any) -> RouterBinding:
    defaults: dict[str, Any] = {
        "router_id": f"id-{name}",
        "name": name,
        "site": "site-a",
        "profiles": ("access",),
        "loopback_v4": LOOPBACK,
    }
    defaults.update(kw)
    return RouterBinding(**defaults)


def fixture_text(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.rsc").read_text()


def export(
    version: str = "7.20.8",
    *,
    snmp: str = POLLER_SNMP,
    services: str = SERVICES_OK,
    ntp: str | None = None,
    **kw: Any,
) -> str:
    ntp_text = (
        ntp
        if ntp is not None
        else "/system ntp client\nset enabled=yes\n/system ntp client servers\nadd address=192.0.2.92\n"
    )
    return build_export(
        version=version, snmp=snmp, services=services, ntp=ntp_text, **kw
    )


def snapshot(text: str, captured_at: datetime = NOW) -> SnapshotInput:
    return SnapshotInput("snap-1", captured_at, text)


def report_for(
    bnd: RouterBinding,
    text: str | None,
    *,
    revisions: list[LayerRevision] | None = None,
    exceptions: list[Any] | None = None,
    captured_at: datetime = NOW,
):
    return build_router_report(
        bnd,
        snapshot(text, captured_at) if text is not None else None,
        revisions if revisions is not None else seed_layer_revisions(),
        exceptions or [],
        today=TODAY,
        now=NOW,
    )


def compose_for(
    bnd: RouterBinding,
    revisions: list[LayerRevision] | None = None,
    exceptions: list[Any] | None = None,
):
    from app.services.composition.compose import compose

    return compose(
        ROUTER_PLUGIN,
        build_subject(bnd),
        revisions if revisions is not None else seed_layer_revisions(),
        exceptions or [],
        TODAY,
    )


def doc_of(text: str) -> ExportDocument:
    return parse_export(text)


def extra_layer(
    tier: str,
    name: str,
    modules: dict[str, Any],
    *,
    weight: int = 0,
    criteria: dict[str, tuple[str, ...]] | None = None,
    kind: str | None = None,
) -> LayerRevision:
    crit = criteria
    if crit is None:
        crit = {
            "site": {"sites": (name,)},
            "profile": {"profiles": (name,)},
            "router": {"router_ids": (name,)},
            "group": {"tags": (name,)},
            "global": {},
        }[tier]
    return LayerRevision(
        Layer(f"{tier}:{name}", tier, name, weight, crit, True, kind),
        1,
        {"schema": 1, "modules": modules},
    )
