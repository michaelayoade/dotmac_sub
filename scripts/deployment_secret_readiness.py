"""Select exact candidate services without exposing resolved Compose secrets.

The deploy adapter supplies its existing active service inventory. This parser
does not own a second service list or grant access to any secret. Compose JSON
is consumed in memory; only validated component names leave this boundary.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass


class SecretReadinessPlanError(ValueError):
    """The candidate cannot be proved to use the requested service graph."""


@dataclass(frozen=True, slots=True)
class SecretReadinessPlanQuery:
    image: str
    components: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SecretReadinessComponent:
    name: str
    image: str


_PROTECTED_CODE_TARGETS = (
    "/app/app",
    "/app/scripts",
    "/app/alembic",
    "/app/alembic.ini",
    "/opt/venv",
    "/opt/poetry",
    "/usr/local/lib",
    "/usr/local/bin",
)


def _validate_candidate_runtime(service: dict[str, object]) -> None:
    """Keep the probe and service code in the immutable candidate image."""
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise SecretReadinessPlanError("candidate component mount graph is invalid")
    for volume in volumes:
        if not isinstance(volume, dict):
            raise SecretReadinessPlanError("candidate component mount graph is invalid")
        target = volume.get("target")
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or "\x00" in target
        ):
            raise SecretReadinessPlanError(
                "candidate component mount target is invalid"
            )
        # POSIX lexical normalization requires no host/container filesystem
        # access. Collapse dotdot and repeated leading slashes before checking
        # either direction of overlap, including whole-root mounts.
        normalized = posixpath.normpath("/" + target.lstrip("/"))
        if normalized == "/" or any(
            normalized == protected
            or normalized.startswith(protected + "/")
            or protected.startswith(normalized + "/")
            for protected in _PROTECTED_CODE_TARGETS
        ):
            raise SecretReadinessPlanError(
                "candidate component code paths are overridden"
            )
    if "working_dir" in service and service["working_dir"] not in ("", "/app"):
        raise SecretReadinessPlanError(
            "candidate component working directory is unsafe"
        )
    environment = service.get("environment", {})
    if not isinstance(environment, dict):
        raise SecretReadinessPlanError(
            "candidate component environment shape is invalid"
        )
    if "PYTHONPATH" in environment and environment["PYTHONPATH"] not in ("", "/app"):
        raise SecretReadinessPlanError("candidate component Python path is unsafe")
    if "PYTHONHOME" in environment and environment["PYTHONHOME"] != "":
        raise SecretReadinessPlanError("candidate component Python home is unsafe")


def candidate_components(
    raw_compose: str, query: SecretReadinessPlanQuery
) -> tuple[SecretReadinessComponent, ...]:
    """Refuse unproved service images or candidate-code/runtime overrides."""
    if not query.image or not query.components:
        raise SecretReadinessPlanError("candidate component inventory is empty")
    if len(set(query.components)) != len(query.components):
        raise SecretReadinessPlanError("candidate component inventory repeats a name")
    if any(
        re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name) is None for name in query.components
    ):
        raise SecretReadinessPlanError("candidate component name is invalid")
    try:
        document: object = json.loads(raw_compose)
    except (ValueError, TypeError) as exc:
        raise SecretReadinessPlanError("resolved Compose document is invalid") from exc
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise SecretReadinessPlanError("resolved Compose service graph is unavailable")
    services = document["services"]
    planned: list[SecretReadinessComponent] = []
    for name in query.components:
        service = services.get(name)
        if not isinstance(service, dict) or service.get("image") != query.image:
            raise SecretReadinessPlanError("candidate component image does not match")
        _validate_candidate_runtime(service)
        planned.append(SecretReadinessComponent(name=name, image=query.image))
    return tuple(planned)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--component", action="append", required=True)
    args = parser.parse_args(argv)
    query = SecretReadinessPlanQuery(image=args.image, components=tuple(args.component))
    try:
        components = candidate_components(sys.stdin.read(), query)
    except SecretReadinessPlanError:
        # Never print the document, decoder error, overridden image, or values
        # that arrived through host-controlled Compose configuration.
        print(
            "Deployment secret readiness refused: candidate service graph unavailable.",
            file=sys.stderr,
        )
        return 1
    for component in components:
        print(component.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
