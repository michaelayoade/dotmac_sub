"""The compose bridge subnet and the RADIUS probe client range are coupled.

FreeRADIUS only accepts probe requests from clients inside
``RADIUS_PROBE_CLIENT_SUBNET`` (``app/services/radius_population.py``). The
app/worker containers live on the compose bridge, so that bridge's subnet must
fall inside it -- otherwise RADIUS silently stops recognising the workers. No
error, no log; the probe just stops working.

That coupling is invisible: the two values sit in different files, and moving one
looks harmless. This test makes it visible.

It has already been broken once in production-adjacent config: seabone's bridge
was moved to 172.80.11.0/24 to dodge a subnet collision with the CRM's stack,
which took the workers outside the RADIUS range.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _compose_default_subnet() -> str:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    # `- subnet: ${DOCKER_SUBNET:-172.20.255.0/24}` -> the default after `:-`
    match = re.search(
        r"-\s*subnet:\s*\$\{DOCKER_SUBNET:-([0-9./]+)\}", text
    )
    assert match, "compose default network must declare `subnet: ${DOCKER_SUBNET:-...}`"
    return match.group(1)


def _radius_probe_client_subnet() -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(r"^RADIUS_PROBE_CLIENT_SUBNET=([0-9./]+)", text, re.M)
    assert match, "RADIUS_PROBE_CLIENT_SUBNET missing from .env.example"
    return match.group(1)


def test_compose_subnet_stays_inside_the_radius_probe_client_range() -> None:
    bridge = ipaddress.ip_network(_compose_default_subnet())
    radius = ipaddress.ip_network(_radius_probe_client_subnet())

    assert bridge.subnet_of(radius), (
        f"compose bridge {bridge} is outside RADIUS_PROBE_CLIENT_SUBNET {radius}. "
        "FreeRADIUS will not accept probe requests from the workers, and it will "
        "fail silently. Pick a /24 inside the RADIUS range."
    )


def test_the_subnet_is_overridable_for_co_hosted_stacks() -> None:
    """dotmac_crm's compose claims the same 172.20.255.0/24. A host running both
    must be able to move one of them without editing tracked files -- otherwise
    `docker compose up` tears the network down, orphaning the database."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "${DOCKER_SUBNET:-" in text, (
        "the compose bridge subnet must be overridable via DOCKER_SUBNET"
    )
