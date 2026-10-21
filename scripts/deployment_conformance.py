"""Generate and verify Sub's Compose-to-descriptor conformance inventory.

The inventory is the evidence half of the deployment-descriptor slice: every
service in the root ``docker-compose.yml`` is classified as a descriptor role,
a managed dependency, or excluded infrastructure with a named reason, and the
classification must stay exact as Compose changes. It records env var NAMES
only — never values — and reads no host, database, or network state.
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from typing import Final

import yaml

INVENTORY_SCHEMA: Final = "dotmac.sub-deployment-conformance-inventory.v1"
ROOT: Final = Path(__file__).resolve().parents[1]

#: Compose service -> (classification, descriptor code or exclusion reason).
CLASSIFICATIONS: Final[dict[str, tuple[str, str]]] = {
    "app": ("role", "app"),
    "bandwidth-poller": ("role", "bandwidth-poller"),
    "celery-beat": ("role", "celery-beat"),
    "celery-worker": ("role", "celery-worker"),
    "celery-worker-bandwidth": ("role", "celery-worker-bandwidth"),
    "celery-worker-billing": ("role", "celery-worker-billing"),
    "celery-worker-ingestion": ("role", "celery-worker-ingestion"),
    "celery-worker-inventory": ("role", "celery-worker-inventory"),
    "celery-worker-monitoring": ("role", "celery-worker-monitoring"),
    "celery-worker-notifications": ("role", "celery-worker-notifications"),
    "celery-worker-notifications-immediate": (
        "role",
        "celery-worker-notifications-immediate",
    ),
    "celery-worker-tr069": ("role", "celery-worker-tr069"),
    "syslog-listener": ("role", "syslog-listener"),
    "team-inbox-smtp": ("role", "team-inbox-smtp"),
    "postgres-local": ("managed_dependency", "postgres-local"),
    "redis-local": ("managed_dependency", "redis-local"),
    "freeradius": (
        "excluded_infrastructure",
        "third-party RADIUS server with host config bind mounts; not a product role",
    ),
    "genieacs": (
        "excluded_infrastructure",
        "third-party ACS on host networking with provision bind mounts; consumed via GENIEACS_NBI_URL",
    ),
    "genieacs-mongodb": (
        "excluded_infrastructure",
        "GenieACS-owned MongoDB; lifecycle belongs to the GenieACS service, not Sub",
    ),
    "nominatim": (
        "excluded_infrastructure",
        "third-party geocoder with its own dataset volumes and import lifecycle",
    ),
    "promtail": (
        "excluded_infrastructure",
        "host log collector mounting the Docker socket and container tree; observability host concern",
    ),
    "victoriametrics": (
        "excluded_infrastructure",
        "metrics store consumed via VICTORIAMETRICS_URL; declared as an unmanaged external dependency",
    ),
    "vmagent": (
        "excluded_infrastructure",
        "metrics scrape agent paired with VictoriaMetrics; observability host concern",
    ),
}


class ConformanceInventoryError(RuntimeError):
    """Compose and the descriptor no longer classify the same topology."""


def _env_names(service: dict[str, object]) -> list[str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return sorted(str(key) for key in environment)
    if isinstance(environment, list):
        return sorted(str(item).split("=", 1)[0] for item in environment)
    return []


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def build_inventory(
    compose_path: Path = ROOT / "docker-compose.yml",
    descriptor_path: Path = ROOT / "deploy/product.toml",
) -> dict[str, object]:
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    descriptor = tomllib.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor_roles = {
        str(role["code"])
        for role in descriptor.get("roles", [])
        if isinstance(role, dict)
    }
    managed = {
        str(dep["code"])
        for dep in descriptor.get("external_dependencies", [])
        if isinstance(dep, dict) and dep.get("image")
    }

    unclassified = sorted(set(services) - set(CLASSIFICATIONS))
    stale = sorted(set(CLASSIFICATIONS) - set(services))
    if unclassified or stale:
        raise ConformanceInventoryError(
            f"Compose services and classifications drifted: "
            f"unclassified={unclassified} stale={stale}"
        )

    records: list[dict[str, object]] = []
    for name in sorted(services):
        service = services[name] or {}
        classification, target = CLASSIFICATIONS[name]
        if classification == "role" and target not in descriptor_roles:
            raise ConformanceInventoryError(
                f"service {name!r} classifies to missing descriptor role {target!r}"
            )
        if classification == "managed_dependency" and target not in managed:
            raise ConformanceInventoryError(
                f"service {name!r} classifies to missing managed dependency {target!r}"
            )
        records.append(
            {
                "cap_add": _string_list(service.get("cap_add")),
                "classification": classification,
                "descriptor_code": target
                if classification != "excluded_infrastructure"
                else None,
                "environment_names": _env_names(service),
                "exclusion_reason": target
                if classification == "excluded_infrastructure"
                else None,
                "has_healthcheck": "healthcheck" in service,
                "image": str(service.get("image", "")),
                "network_mode": str(service.get("network_mode", "")),
                "ports": _string_list(service.get("ports")),
                "privileged": bool(service.get("privileged", False)),
                "profiles": _string_list(service.get("profiles")),
                "service": name,
                "volumes": _string_list(service.get("volumes")),
            }
        )

    orphan_roles = sorted(
        descriptor_roles
        - {target for kind, target in CLASSIFICATIONS.values() if kind == "role"}
    )
    if orphan_roles:
        raise ConformanceInventoryError(
            f"descriptor roles with no Compose service: {orphan_roles}"
        )

    return {
        "descriptor": "deploy/product.toml",
        "schema": INVENTORY_SCHEMA,
        "services": records,
        "source": "docker-compose.yml",
    }


def canonical_inventory_bytes() -> bytes:
    encoded = json.dumps(
        build_inventory(), ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return f"{encoded}\n".encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        default=ROOT / "deploy/conformance-inventory.json",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        canonical = canonical_inventory_bytes()
        if args.check:
            if args.path.read_bytes() != canonical:
                raise ConformanceInventoryError(
                    "conformance inventory is not the canonical generated document"
                )
        else:
            args.path.write_bytes(canonical)
    except (ConformanceInventoryError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"path": str(args.path), "status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
