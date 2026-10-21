"""Helpers shared by modules: export reads, CIDR coverage, versions."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from app.services.router_health.routeros_export import (
    ExportDocument,
    ExportItem,
    is_true,
    parse_version,
)

__all__ = [
    "ExportDocument",
    "ExportItem",
    "covered_by",
    "is_true",
    "parse_version",
    "set_props",
]


def set_props(doc: ExportDocument, section: str) -> dict[str, str]:
    """Merged properties of every ``set`` line of a singleton section."""
    props: dict[str, str] = {}
    for item in doc.section(section):
        if item.verb == "set":
            props.update(item.props)
    return props


def covered_by(
    address: str,
    allowed: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """True when ``address`` (CIDR or IP) is inside one allowed network."""
    try:
        network = ipaddress.ip_network(address.strip(), strict=False)
    except ValueError:
        return False
    return any(
        network.version == ok.version and network.subnet_of(ok)  # type: ignore[arg-type]
        for ok in allowed
    )
