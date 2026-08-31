"""Declared publish intent for host ports, and the two checks that hold it.

The declaration is ``deploy/published_ports.toml``. Two independent checks
read it:

``check-compose``
    Offline. Compares the declaration against ``docker-compose.yml``'s publish
    specs and refuses a *bare* publish -- one with no host address -- because a
    bare publish silently creates a second listener on ``[::]`` that appears in
    no file and never traverses ``DOCKER-USER``.

``check-listeners``
    Reads what is ACTUALLY listening (``docker inspect``'s ``HostIp`` values)
    and compares it against the declaration for BOTH address families. This is
    the check that catches a bare publish in production; a v4-only check does
    not, because the v4 half of a bare publish is correct.

``plan``
    Emits the environment assignments and target binds a reconcile must put in
    place, and refuses a target that would strand a declared required client.

Nothing here talks to a network or to Docker. The shell adapter collects
``docker inspect`` output and hands it in as JSON, which is what lets the
comparison logic be exercised in CI in both directions.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DECLARATION_PATH = REPO_ROOT / "deploy" / "published_ports.toml"
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"

# A compose publish spec: an optional host-address prefix, a host port (literal
# or `${VAR:-N}`), the container port, and an optional protocol.
_PUBLISH_SPEC = re.compile(
    r"^"
    r"(?P<bind>"
    r"\$\{(?P<bind_env>[A-Z0-9_]+):-(?P<bind_default>[^}]*)\}"
    r"|(?P<bind_literal>\d{1,3}(?:\.\d{1,3}){3}:|\[[0-9A-Fa-f:]+\]:)"
    r")?"
    r"(?P<host_port>\$\{(?P<host_port_env>[A-Z0-9_]+):-(?P<host_port_default>\d+)\}"
    r"|\d+)"
    r":(?P<container_port>\d+)"
    r"(?:/(?P<protocol>tcp|udp))?"
    r"$"
)

_LOOPBACK = {
    4: ipaddress.ip_network("127.0.0.0/8"),
    6: ipaddress.ip_network("::1/128"),
}


class DeclarationError(RuntimeError):
    """The declaration itself is malformed or internally inconsistent."""


# --------------------------------------------------------------------------
# address logic
# --------------------------------------------------------------------------


def normalise_bind(bind: str) -> str:
    """Turn a compose bind prefix into a bare address.

    ``"127.0.0.1:"`` -> ``"127.0.0.1"``; ``"[::1]:"`` -> ``"::1"``;
    ``""`` -> ``""`` (a bare publish, which is the ungoverned shape).
    """
    value = bind.strip()
    if value.endswith(":"):
        value = value[:-1]
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def expected_listeners(bind: str) -> set[str]:
    """The ``HostIp`` values Docker creates for a given bind prefix.

    A bare publish is the whole point of this module: it yields TWO listeners,
    one per family, and only one of them is governable by ``DOCKER-USER``.
    """
    address = normalise_bind(bind)
    if not address:
        # Not a bind: a statement of what Docker does to a bare publish. These
        # two strings are the defect this module exists to detect.
        return {"0.0.0.0", "::"}  # noqa: S104
    return {str(ipaddress.ip_address(address))}


def family_of(address: str) -> str:
    return f"ipv{ipaddress.ip_address(address).version}"


def bind_admits(bind: str, client: str) -> bool:
    """Can a client in ``client`` (a CIDR) reach a socket bound to ``bind``?

    The only binds that exclude an off-host client are a loopback bind and a
    bind in the other address family. That is deliberately the narrow, honest
    rule -- it is what makes "a loopback default strands the replication
    standby" a machine-checkable fact rather than a comment.

    ``::`` is treated as IPv6-only even though a dual-stack socket may accept
    v4-mapped traffic. Conservative in the right direction: it means an
    IPv6 listener can never be *justified* by an IPv4 client that needs it.
    """
    address = normalise_bind(bind)
    if not address:
        # A bare publish reaches everything -- but it is refused elsewhere, so
        # it never gets to count as admitting anything.
        return True
    bound = ipaddress.ip_address(address)
    network = ipaddress.ip_network(client, strict=False)
    if bound.version != network.version:
        return False
    if bound.is_unspecified:
        return True
    if bound.is_loopback:
        return network.subnet_of(_LOOPBACK[bound.version])
    return True


# --------------------------------------------------------------------------
# declaration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredPublish:
    service: str
    host_port: int
    container_port: int
    protocol: str
    bind_env: str
    default_bind: str
    reach: str
    recreated_by_deploy: bool
    reason: str | None = None
    required_clients: tuple[str, ...] = ()
    environment_bind: dict[str, str] = field(default_factory=dict)
    host_port_env: str | None = None

    @property
    def key(self) -> str:
        return f"{self.service}:{self.host_port}/{self.protocol}"

    def bind_for(self, environment: str) -> str:
        return self.environment_bind.get(environment, self.default_bind)


@dataclass(frozen=True)
class Declaration:
    publishes: tuple[DeclaredPublish, ...]
    environments: tuple[str, ...]

    def for_service(self, service: str) -> tuple[DeclaredPublish, ...]:
        return tuple(p for p in self.publishes if p.service == service)

    def services(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p.service for p in self.publishes))


def load_declaration(path: Path | None = None) -> Declaration:
    raw = tomllib.loads((path or DECLARATION_PATH).read_text())
    environments = tuple(raw.get("declared_environments", ()))
    if not environments:
        raise DeclarationError("declared_environments must name at least one.")

    publishes: list[DeclaredPublish] = []
    for entry in raw.get("publish", []):
        publish = DeclaredPublish(
            service=entry["service"],
            host_port=int(entry["host_port"]),
            container_port=int(entry["container_port"]),
            protocol=entry["protocol"],
            bind_env=entry["bind_env"],
            default_bind=entry["default_bind"],
            reach=entry["reach"],
            recreated_by_deploy=bool(entry["recreated_by_deploy"]),
            reason=entry.get("reason"),
            required_clients=tuple(entry.get("required_clients", ())),
            environment_bind=dict(entry.get("environment_bind", {})),
            host_port_env=entry.get("host_port_env"),
        )
        _validate_declared(publish, environments)
        publishes.append(publish)

    keys = [p.key for p in publishes]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise DeclarationError(f"duplicate declared publishes: {sorted(duplicates)}")
    return Declaration(publishes=tuple(publishes), environments=environments)


def _validate_declared(publish: DeclaredPublish, environments: tuple[str, ...]) -> None:
    if publish.protocol not in {"tcp", "udp"}:
        raise DeclarationError(f"{publish.key}: protocol must be tcp or udp.")
    if publish.reach not in {"loopback", "offhost"}:
        raise DeclarationError(f"{publish.key}: reach must be loopback or offhost.")

    for label, bind in [
        ("default_bind", publish.default_bind),
        *(
            (f"environment_bind.{env}", b)
            for env, b in publish.environment_bind.items()
        ),
    ]:
        if not normalise_bind(bind):
            raise DeclarationError(
                f"{publish.key}: {label} is empty. A declared bind must name an "
                "explicit address; a bare publish is the defect this file exists "
                "to prevent."
            )
        # Raises if it is not a real address.
        expected_listeners(bind)

    unknown = set(publish.environment_bind) - set(environments)
    if unknown:
        raise DeclarationError(
            f"{publish.key}: environment_bind names undeclared environments "
            f"{sorted(unknown)}."
        )

    non_loopback = [
        bind
        for bind in [publish.default_bind, *publish.environment_bind.values()]
        if not ipaddress.ip_address(normalise_bind(bind)).is_loopback
    ]
    if non_loopback and publish.reach != "offhost":
        raise DeclarationError(
            f"{publish.key}: binds {non_loopback} leave the host but reach is "
            f"'{publish.reach}'. A non-loopback bind must be declared offhost "
            "with a reason."
        )
    if publish.reach == "offhost":
        if not publish.reason:
            raise DeclarationError(f"{publish.key}: an offhost publish needs a reason.")
        if not publish.required_clients:
            raise DeclarationError(
                f"{publish.key}: an offhost publish must name required_clients, so "
                "a narrowing bind can be refused instead of stranding them."
            )
        for client in publish.required_clients:
            ipaddress.ip_network(client, strict=False)


# --------------------------------------------------------------------------
# compose
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ComposePublish:
    service: str
    spec: str
    host_port: int
    container_port: int
    protocol: str
    bind_env: str | None
    bind_default: str | None
    bind_literal: str | None
    host_port_env: str | None

    @property
    def key(self) -> str:
        return f"{self.service}:{self.host_port}/{self.protocol}"

    @property
    def is_bare(self) -> bool:
        return self.bind_env is None and self.bind_literal is None


def parse_publish_spec(service: str, spec: Any) -> ComposePublish:
    if not isinstance(spec, str):
        raise DeclarationError(
            f"{service}: long-form port mappings are not supported here ({spec!r}). "
            "Use the string short form so the declared bind is visible."
        )
    match = _PUBLISH_SPEC.match(spec.strip())
    if match is None:
        raise DeclarationError(f"{service}: unparseable publish spec {spec!r}.")
    host_port = match.group("host_port_default") or match.group("host_port")
    return ComposePublish(
        service=service,
        spec=spec.strip(),
        host_port=int(host_port),
        container_port=int(match.group("container_port")),
        protocol=match.group("protocol") or "tcp",
        bind_env=match.group("bind_env"),
        bind_default=match.group("bind_default"),
        bind_literal=match.group("bind_literal"),
        host_port_env=match.group("host_port_env"),
    )


def parse_compose_publishes(path: Path | None = None) -> tuple[ComposePublish, ...]:
    import yaml

    document = yaml.safe_load((path or COMPOSE_PATH).read_text())
    found: list[ComposePublish] = []
    for service, definition in (document.get("services") or {}).items():
        if not isinstance(definition, dict):
            continue
        for spec in definition.get("ports") or []:
            found.append(parse_publish_spec(service, spec))
    return tuple(found)


def check_compose(
    declaration: Declaration, publishes: tuple[ComposePublish, ...]
) -> list[str]:
    problems: list[str] = []
    declared = {p.key: p for p in declaration.publishes}
    observed = {p.key: p for p in publishes}

    for key, publish in sorted(observed.items()):
        if publish.is_bare:
            problems.append(
                f"{key}: bare publish {publish.spec!r} names no host address. "
                "Docker will start a second listener on [::] that no "
                "DOCKER-USER rule can reach. Give it an explicit bind knob."
            )
        if key not in declared:
            problems.append(
                f"{key}: published by compose but absent from "
                "deploy/published_ports.toml. Every publish is declared."
            )
            continue
        want = declared[key]
        if publish.bind_env != want.bind_env:
            problems.append(
                f"{key}: compose binds through {publish.bind_env!r} but the "
                f"declaration names {want.bind_env!r}."
            )
        if (
            publish.bind_default is not None
            and publish.bind_default != want.default_bind
        ):
            problems.append(
                f"{key}: compose default bind {publish.bind_default!r} != declared "
                f"default_bind {want.default_bind!r}."
            )
        if publish.container_port != want.container_port:
            problems.append(
                f"{key}: compose maps container port {publish.container_port} but "
                f"the declaration says {want.container_port}."
            )
        if publish.host_port_env != want.host_port_env:
            problems.append(
                f"{key}: compose host port env {publish.host_port_env!r} != declared "
                f"{want.host_port_env!r}."
            )

    for key in sorted(set(declared) - set(observed)):
        problems.append(
            f"{key}: declared in deploy/published_ports.toml but compose publishes "
            "no such port. A stale declaration hides a real one."
        )
    return problems


# --------------------------------------------------------------------------
# listeners
# --------------------------------------------------------------------------


def normalise_inspect(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reduce raw ``docker inspect`` output to what the comparison needs.

    Containers are keyed by their ``com.docker.compose.service`` label rather
    than by ``container_name``, so a service renamed on the host still matches
    its declaration -- and a container that carries no such label is reported
    under an empty service, which then fails as undeclared rather than being
    quietly skipped.
    """
    normalised: list[dict[str, Any]] = []
    for container in raw:
        labels = (container.get("Config") or {}).get("Labels") or {}
        normalised.append(
            {
                "service": labels.get("com.docker.compose.service", ""),
                "container": (container.get("Name") or "").lstrip("/"),
                "ports": (container.get("NetworkSettings") or {}).get("Ports") or {},
            }
        )
    return normalised


def normalise_inspect_lines(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Normalise the JSON-lines the shell collector emits.

    The collector asks ``docker inspect`` for exactly three fields, so a
    container's environment block -- which holds every secret -- never leaves
    the daemon. This turns those lines into the list ``check_listeners`` wants.
    Having it here rather than inline in a shell or workflow means there is one
    copy of it, and it is covered by tests.
    """
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        row["container"] = (row.get("container") or "").lstrip("/")
        row["service"] = row.get("service") or ""
        row["ports"] = row.get("ports") or {}
        rows.append(row)
    return rows


def check_listeners(
    declaration: Declaration,
    environment: str,
    observed: list[dict[str, Any]],
    service: str | None = None,
) -> list[str]:
    """Compare ACTUAL listeners against declared intent, both families.

    ``observed`` is the normalised output of ``docker inspect``:
    ``[{"service": ..., "container": ..., "ports": {"5432/tcp": [{"HostIp":
    ..., "HostPort": ...}]}}]``.

    ``service`` narrows the comparison to one service. The reconcile uses that
    to prove the service it just recreated; the standalone sweep passes None
    and holds the whole project, including containers with no declaration.
    """
    if environment not in declaration.environments:
        return [
            f"environment {environment!r} is not declared in "
            f"deploy/published_ports.toml (declared: "
            f"{list(declaration.environments)}). Refusing to assume it takes "
            "the defaults."
        ]

    if service is not None and service not in declaration.services():
        return [
            f"service {service!r} declares no published ports (declared: "
            f"{list(declaration.services())})."
        ]

    problems: list[str] = []
    seen: set[str] = set()
    relevant = [p for p in declaration.publishes if service in (None, p.service)]

    for container in observed:
        container_service = container.get("service") or ""
        if service is not None and container_service != service:
            continue
        name = container.get("container") or container_service
        for port_spec, bindings in (container.get("ports") or {}).items():
            if not bindings:
                continue  # exposed, not published
            container_port, _, protocol = port_spec.partition("/")
            protocol = protocol or "tcp"
            by_host_port: dict[str, set[str]] = {}
            for binding in bindings:
                by_host_port.setdefault(binding["HostPort"], set()).add(
                    binding["HostIp"]
                )
            for host_port, host_ips in sorted(by_host_port.items()):
                key = f"{container_service}:{int(host_port)}/{protocol}"
                declared = next((p for p in relevant if p.key == key), None)
                if declared is None:
                    problems.append(
                        f"{name}: publishes {host_port}/{protocol} on "
                        f"{sorted(host_ips)} with no declaration in "
                        "deploy/published_ports.toml. An undeclared listener is "
                        "an ungoverned listener."
                    )
                    continue
                seen.add(key)
                if declared.container_port != int(container_port):
                    problems.append(
                        f"{key}: listening for container port {container_port}, "
                        f"declared {declared.container_port}."
                    )
                want = expected_listeners(declared.bind_for(environment))
                excess = host_ips - want
                missing = want - host_ips
                for address in sorted(excess):
                    problems.append(
                        f"{key}: listening on {address} ({family_of(address)}) "
                        f"which is NOT declared. Declared listeners for "
                        f"{environment}: {sorted(want)}."
                    )
                for address in sorted(missing):
                    problems.append(
                        f"{key}: declared listener {address} "
                        f"({family_of(address)}) is absent. Declared clients "
                        f"{list(declared.required_clients) or 'n/a'} may have no "
                        "path."
                    )

    for key in sorted({p.key for p in relevant} - seen):
        problems.append(
            f"{key}: declared but no running container publishes it. Either the "
            "service is stopped or the declaration is stale; both need a human."
        )
    return problems


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------


def plan(declaration: Declaration, service: str, environment: str) -> dict[str, Any]:
    if environment not in declaration.environments:
        raise DeclarationError(
            f"environment {environment!r} is not declared (declared: "
            f"{list(declaration.environments)})."
        )
    publishes = declaration.for_service(service)
    if not publishes:
        raise DeclarationError(
            f"service {service!r} declares no published ports (declared services: "
            f"{list(declaration.services())})."
        )

    assignments: dict[str, str] = {}
    targets = []
    for publish in publishes:
        bind = publish.bind_for(environment)
        existing = assignments.get(publish.bind_env)
        if existing is not None and existing != bind:
            raise DeclarationError(
                f"{service}: {publish.bind_env} is declared as both {existing!r} "
                f"and {bind!r} for {environment}. One knob, one value."
            )
        assignments[publish.bind_env] = bind

        stranded = [
            client
            for client in publish.required_clients
            if not bind_admits(bind, client)
        ]
        if stranded:
            raise DeclarationError(
                f"{publish.key}: bind {bind!r} for {environment} does not admit "
                f"required clients {stranded}. Refusing to plan a recreate that "
                "would cut them off."
            )
        targets.append(
            {
                "key": publish.key,
                "host_port": publish.host_port,
                "container_port": publish.container_port,
                "protocol": publish.protocol,
                "bind": bind,
                "expected_listeners": sorted(expected_listeners(bind)),
                "required_clients": list(publish.required_clients),
            }
        )

    return {
        "service": service,
        "environment": environment,
        "assignments": assignments,
        "targets": targets,
        "recreated_by_deploy": any(p.recreated_by_deploy for p in publishes),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _report(problems: list[str], ok_message: str) -> int:
    if problems:
        print(f"REFUSED: {len(problems)} problem(s).", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(ok_message)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declaration", type=Path, default=DECLARATION_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    compose_cmd = sub.add_parser("check-compose")
    compose_cmd.add_argument("--compose", type=Path, default=COMPOSE_PATH)

    listeners_cmd = sub.add_parser("check-listeners")
    listeners_cmd.add_argument("--environment", required=True)
    source = listeners_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--observed", type=Path, help="already-normalised listener JSON"
    )
    source.add_argument(
        "--docker-inspect", type=Path, help="raw `docker inspect` JSON array"
    )
    listeners_cmd.add_argument(
        "--service", default=None, help="narrow the check to one service"
    )

    plan_cmd = sub.add_parser("plan")
    plan_cmd.add_argument("--service", required=True)
    plan_cmd.add_argument("--environment", required=True)

    sub.add_parser("list")
    sub.add_parser("normalise", help="normalise the collector's JSON lines from stdin")

    args = parser.parse_args(argv)
    try:
        declaration = load_declaration(args.declaration)
    except DeclarationError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1

    if args.command == "check-compose":
        try:
            publishes = parse_compose_publishes(args.compose)
        except DeclarationError as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 1
        return _report(
            check_compose(declaration, publishes),
            f"{len(publishes)} compose publish(es) match the declaration.",
        )

    if args.command == "normalise":
        json.dump(normalise_inspect_lines(sys.stdin), sys.stdout)
        return 0

    if args.command == "list":
        for service in declaration.services():
            ports = ", ".join(
                f"{p.host_port}/{p.protocol} via {p.bind_env}"
                for p in declaration.for_service(service)
            )
            print(f"{service}: {ports}")
        print(f"environments: {list(declaration.environments)}")
        return 0

    if args.command == "check-listeners":
        if args.docker_inspect is not None:
            observed = normalise_inspect(json.loads(args.docker_inspect.read_text()))
        else:
            observed = json.loads(args.observed.read_text())
        scope = args.service or "every declared service"
        return _report(
            check_listeners(declaration, args.environment, observed, args.service),
            f"Actual listeners match declared intent for {scope} in "
            f"{args.environment}, in both address families.",
        )

    try:
        print(json.dumps(plan(declaration, args.service, args.environment), indent=2))
    except DeclarationError as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
