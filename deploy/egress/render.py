#!/usr/bin/env python3
"""Render egress candidates (host output, container forward); never install or execute them."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EgressConfiguration:
    cp_hostname: str
    openbao_hostname: str
    enforce: bool = False
    # Explicit local destinations. No blanket private-range acceptance exists;
    # empty means nothing local is accepted beyond loopback and established flows.
    local_cidrs: tuple[str, ...] = ()
    # Named time servers. Empty means no NTP exception.
    ntp_servers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "local_cidrs", _networks(self.local_cidrs, "local_cidrs")
        )
        object.__setattr__(self, "ntp_servers", _hosts(self.ntp_servers, "ntp_servers"))
        for hostname in (self.cp_hostname, self.openbao_hostname):
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", hostname):
                raise ValueError("invalid FQDN")
            labels = hostname.split(".")
            if len(labels) < 2 or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                for label in labels
            ):
                raise ValueError("invalid FQDN")
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                continue
            raise ValueError("use a hostname, not an IP address")


_BLANKET = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


def _networks(values: object, field: str) -> tuple[str, ...]:
    """Validate explicit IPv4 networks; broad or blanket ranges are refused."""
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field} entries must be strings")
        try:
            net = ipaddress.IPv4Network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"{field}: invalid IPv4 network") from exc
        if net.prefixlen < 16 or net in _BLANKET:
            raise ValueError(f"{field}: network too broad")
        if net.is_multicast or net.is_unspecified or net.is_loopback:
            raise ValueError(f"{field}: network not allowed")
        out.append(str(net))
    nets = sorted({ipaddress.IPv4Network(n) for n in out})
    for first, second in zip(nets, nets[1:], strict=False):
        if first.overlaps(second):
            raise ValueError(f"{field}: overlapping networks")
    return tuple(str(n) for n in nets)


def _hosts(values: object, field: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field} entries must be strings")
        try:
            addr = ipaddress.IPv4Address(value)
        except ValueError as exc:
            raise ValueError(f"{field}: invalid IPv4 address") from exc
        if addr.is_multicast or addr.is_unspecified or addr.is_loopback:
            raise ValueError(f"{field}: address not allowed")
        if addr == ipaddress.IPv4Address("255.255.255.255"):
            raise ValueError(f"{field}: address not allowed")
        out.append(str(addr))
    return tuple(sorted(set(out), key=lambda a: ipaddress.IPv4Address(a)))


BASE_HOSTS = (
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
    "api.github.com",
    "github.com",
    "token.actions.githubusercontent.com",
    "fulcio.sigstore.dev",
    "rekor.sigstore.dev",
    "registry.dotmac.io",
)


def render(configuration: EgressConfiguration) -> dict[str, str]:
    hosts = sorted(
        {*BASE_HOSTS, configuration.cp_hostname, configuration.openbao_hostname}
    )
    resolver = "\n".join(
        (
            "# Candidate only. Root-owned host config; never derived from desired-state.",
            "bind 127.0.0.1:53",
            "bind-tcp 127.0.0.1:53",
            "server 1.1.1.1",
            "server 9.9.9.9",
            "speed-check-mode none",
            "dualstack-ip-selection no",
            "serve-expired no",
            "prefetch-domain no",
            "cache-persist no",
            "nftset-timeout yes",
            "nftset-debug yes",
            *(
                f"nftset /{host}/#4:inet#dotmac_egress#egress_v4,#6:inet#dotmac_egress#egress_v6"
                for host in hosts
            ),
            "",
        )
    )
    terminal = "counter drop" if configuration.enforce else "counter accept"
    rules = "\n".join(
        (
            "# Host output only; container forwarding is a separate cutover.",
            "table inet dotmac_egress {",
            " set egress_v4 { type ipv4_addr; flags timeout; }",
            " set egress_v6 { type ipv6_addr; flags timeout; }",
            " chain out {",
            "  type filter hook output priority 0; policy accept;",
            "  ct state established,related accept",
            '  oifname "lo" accept',
            "  ip daddr { 1.1.1.1, 9.9.9.9 } udp dport 53 accept",
            "  ip daddr { 1.1.1.1, 9.9.9.9 } tcp dport 53 accept",
            *(
                (
                    f"  ip daddr {{ {', '.join(configuration.ntp_servers)} }} udp dport 123 accept",
                )
                if configuration.ntp_servers
                else ()
            ),
            "  ip daddr @egress_v4 accept",
            "  ip6 daddr @egress_v6 accept",
            *(
                (f"  ip daddr {{ {', '.join(configuration.local_cidrs)} }} accept",)
                if configuration.local_cidrs
                else ()
            ),
            '  limit rate 10/second log prefix "dotmac-egress-unmatched: "',
            f"  {terminal}",
            " }",
            "}",
            "",
        )
    )
    return {"smartdns.conf": resolver, "dotmac-egress.nft": rules}


# ---------------------------------------------------------------------------
# container_forward mode: default-deny for Docker bridge egress.
#
# Drops only, in a dedicated table. Base chains keep ``policy accept``; an
# accept in one base chain does not bypass another, so the container runtime's
# own chains are untouched and a runtime restart never rewrites this table.
# There is no private-range acceptance in this mode: internal destinations are
# an explicit list, and everything else must be an exact allowlist entry.
# ---------------------------------------------------------------------------

TABLE = "dotmac_staging_egress"
NFT_PATH = "/etc/dotmac-staging-egress/staging-egress.nft"
SHA_PATH = "/etc/dotmac-staging-egress/staging-egress.nft.sha256"
UNIT_PATH = "/etc/systemd/system/dotmac-staging-egress.service"
DROPIN_PATH = "/etc/systemd/system/docker.service.d/10-staging-egress.conf"
MAX_ALLOW = 256

_IFNAME = re.compile(r"(?:[A-Za-z0-9_.-]{1,15}|[A-Za-z0-9_.-]{1,14}\*)")
_DOC_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)

# Structure that is not environment data. Addresses always come from input.
PROFILES: dict[str, dict[str, object]] = {
    "seabone": {
        "scope_ifnames": ("br-*", "docker0", "docker_gwbridge"),
        "hostnet_uids": (999,),
    },
}


@dataclass(frozen=True)
class AllowEntry:
    ip: str
    proto: str
    port: int

    def __post_init__(self) -> None:
        try:
            addr = ipaddress.IPv4Address(self.ip)
        except ValueError as exc:
            raise ValueError("allow: single IPv4 address required") from exc
        if (
            addr.is_multicast
            or addr.is_unspecified
            or addr.is_loopback
            or addr.is_link_local
            or addr == ipaddress.IPv4Address("255.255.255.255")
        ):
            raise ValueError("allow: address not allowed")
        if self.proto not in ("tcp", "udp"):
            raise ValueError("allow: proto must be tcp or udp")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("allow: invalid port")

    @property
    def key(self) -> tuple[int, str, int]:
        return (int(ipaddress.IPv4Address(self.ip)), self.proto, self.port)


@dataclass(frozen=True)
class ContainerForwardConfiguration:
    scope_ifnames: tuple[str, ...]
    internal_cidrs: tuple[str, ...]
    exempt_src_cidrs: tuple[str, ...]
    allow: tuple[AllowEntry, ...]
    hostnet_uids: tuple[int, ...]
    enforce: bool = False

    def __post_init__(self) -> None:
        if type(self.enforce) is not bool:
            raise ValueError("enforce must be a boolean")
        names = self.scope_ifnames
        if (
            isinstance(names, str)
            or not isinstance(names, (list, tuple))
            or not names
            or any(not isinstance(n, str) or not _IFNAME.fullmatch(n) for n in names)
        ):
            raise ValueError("scope_ifnames: invalid")
        object.__setattr__(self, "scope_ifnames", tuple(sorted(set(names))))
        internal = _networks(self.internal_cidrs, "internal_cidrs")
        if not internal:
            raise ValueError("internal_cidrs: at least one network required")
        object.__setattr__(self, "internal_cidrs", internal)
        object.__setattr__(
            self,
            "exempt_src_cidrs",
            _networks(self.exempt_src_cidrs, "exempt_src_cidrs"),
        )
        uids = self.hostnet_uids
        if isinstance(uids, str) or not isinstance(uids, (list, tuple)):
            raise ValueError("hostnet_uids: must be a list")
        for uid in uids:
            if type(uid) is not int or not 1 <= uid <= 65533:
                raise ValueError("hostnet_uids: invalid uid (root is never scoped)")
        object.__setattr__(self, "hostnet_uids", tuple(sorted(set(uids))))
        allow = self.allow
        if isinstance(allow, str) or not isinstance(allow, (list, tuple)):
            raise ValueError("allow: must be a list")
        if len(allow) > MAX_ALLOW:
            raise ValueError("allow: too many entries")
        entries = {e.key: e for e in allow}
        object.__setattr__(self, "allow", tuple(entries[k] for k in sorted(entries)))
        if self.enforce and any(
            ipaddress.IPv4Address(e.ip) in net for e in self.allow for net in _DOC_NETS
        ):
            raise ValueError("allow: documentation-range placeholder with enforce")

    @classmethod
    def from_mapping(
        cls, raw: dict[str, object], profile: str | None = None
    ) -> ContainerForwardConfiguration:
        fixed: dict[str, object] = {}
        if profile is not None:
            if profile not in PROFILES:
                raise ValueError("unknown profile")
            fixed = dict(PROFILES[profile])
        required = {"internal_cidrs", "exempt_src_cidrs", "allow", "enforce"}
        structural = {"scope_ifnames", "hostnet_uids"}
        if profile is None:
            required |= structural
        keys = set(raw)
        if keys != required:
            raise ValueError(f"input keys must be exactly {sorted(required)}")
        allow_raw = raw["allow"]
        if not isinstance(allow_raw, list):
            raise ValueError("allow: must be a list")
        entries = []
        for item in allow_raw:
            if not isinstance(item, dict) or set(item) != {"ip", "proto", "port"}:
                raise ValueError("allow entries need exactly ip, proto, port")
            entries.append(AllowEntry(item["ip"], item["proto"], item["port"]))
        merged = {**raw, **fixed}
        return cls(
            scope_ifnames=merged["scope_ifnames"],  # type: ignore[arg-type]
            internal_cidrs=merged["internal_cidrs"],  # type: ignore[arg-type]
            exempt_src_cidrs=merged["exempt_src_cidrs"],  # type: ignore[arg-type]
            allow=tuple(entries),
            hostnet_uids=merged["hostnet_uids"],  # type: ignore[arg-type]
            enforce=merged["enforce"],  # type: ignore[arg-type]
        )


def _set_elements(items: list[str]) -> list[str]:
    if not items:
        return []
    return [f"  elements = {{ {', '.join(items)} }}"]


def render_container_forward(
    configuration: ContainerForwardConfiguration,
) -> dict[str, str]:
    import hashlib

    c = configuration
    terminal = "counter drop" if c.enforce else "counter return"
    lines: list[str] = [
        "# Drops only. Never flushes the ruleset; replaces only its own table.",
        "# enforce=false records and logs but lets traffic through.",
        "# The terminal rule of each chain also covers IPv6 (no address match).",
        f"table inet {TABLE}",
        f"delete table inet {TABLE}",
        f"table inet {TABLE} {{",
        " set internal_v4 {",
        "  type ipv4_addr; flags interval;",
        *_set_elements(list(c.internal_cidrs)),
        " }",
        " set exempt_src_v4 {",
        "  type ipv4_addr; flags interval;",
        *_set_elements(list(c.exempt_src_cidrs)),
        " }",
        " set allow_v4 {",
        "  type ipv4_addr . inet_proto . inet_service;",
        *_set_elements([f"{e.ip} . {e.proto} . {e.port}" for e in c.allow]),
        " }",
        " set dropped_v4 {",
        "  type ipv4_addr . ipv4_addr . inet_proto . inet_service;",
        "  flags dynamic, timeout; timeout 1h; size 4096;",
        " }",
        " set dropped_out_v4 {",
        "  type ipv4_addr . inet_proto . inet_service;",
        "  flags dynamic, timeout; timeout 1h; size 1024;",
        " }",
        " chain staging_out {",
        "  ct state established,related return",
        "  ip saddr @exempt_src_v4 counter return",
        "  ip daddr @internal_v4 return",
        "  ip daddr . meta l4proto . th dport @allow_v4 counter return",
        "  meta nfproto ipv4 meta l4proto { tcp, udp } update @dropped_v4 "
        "{ ip saddr . ip daddr . meta l4proto . th dport }",
        '  limit rate 5/second log prefix "stg-egress-drop: "',
        f"  {terminal}",
        " }",
        " chain genieacs_out {",
        "  ct state established,related return",
        '  oifname "lo" return',
        "  ip daddr @internal_v4 return",
        "  meta nfproto ipv4 meta l4proto { tcp, udp } update @dropped_out_v4 "
        "{ ip daddr . meta l4proto . th dport }",
        '  limit rate 5/second log prefix "stg-egress-drop-host: "',
        f"  {terminal}",
        " }",
        " chain forward {",
        "  type filter hook forward priority filter - 10; policy accept;",
        *(f'  iifname "{n}" jump staging_out' for n in c.scope_ifnames),
        " }",
        " chain output {",
        "  type filter hook output priority filter - 10; policy accept;",
        *(
            [
                "  meta skuid { "
                + ", ".join(str(u) for u in c.hostnet_uids)
                + " } jump genieacs_out"
            ]
            if c.hostnet_uids
            else []
        ),
        " }",
        "}",
        "",
    ]
    nft = "\n".join(lines)
    digest = hashlib.sha256(nft.encode()).hexdigest()
    unit = "\n".join(
        (
            "[Unit]",
            "Description=Staging container egress default-deny (drops only)",
            "DefaultDependencies=no",
            "After=local-fs.target",
            "Before=network-pre.target docker.service",
            "Wants=network-pre.target",
            "",
            "[Service]",
            "Type=oneshot",
            "RemainAfterExit=yes",
            f"ExecStartPre=/usr/bin/sha256sum -c {SHA_PATH}",
            f"ExecStart=/usr/sbin/nft -f {NFT_PATH}",
            "# Deliberately no ExecStop: stopping this unit must never open egress.",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )
    dropin = "\n".join(
        (
            "[Unit]",
            "After=dotmac-staging-egress.service",
            "Wants=dotmac-staging-egress.service",
            "",
            "[Service]",
            "# Fail closed at boot: the runtime does not start without the table.",
            f"ExecStartPre=/usr/sbin/nft list table inet {TABLE}",
            "",
        )
    )
    return {
        "staging-egress.nft": nft,
        "staging-egress.nft.sha256": f"{digest}  {NFT_PATH}\n",
        "dotmac-staging-egress.service": unit,
        "10-staging-egress.conf": dropin,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILES),
        help="container_forward only: fixed structure; addresses come from input",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        help="container_forward only: JSON list (or {allow: [...]}) of "
        "{ip, proto, port}; replaces the allow key of the configuration",
    )
    args = parser.parse_args()
    raw = json.loads(args.configuration.read_text())
    if not isinstance(raw, dict):
        parser.error("configuration must be an object")
    mode = raw.pop("mode", "host_output")
    if mode == "container_forward":
        if args.allowlist is not None:
            extra = json.loads(args.allowlist.read_text())
            if isinstance(extra, dict) and set(extra) == {"allow"}:
                extra = extra["allow"]
            raw["allow"] = extra
        try:
            files = render_container_forward(
                ContainerForwardConfiguration.from_mapping(raw, args.profile)
            )
        except (ValueError, KeyError) as exc:
            parser.error(f"invalid container_forward input: {exc}")
    elif mode == "host_output":
        if args.profile or args.allowlist:
            parser.error("--profile and --allowlist apply to container_forward")
        base = {"cp_hostname", "openbao_hostname", "enforce"}
        if not base <= set(raw) <= base | {"local_cidrs", "ntp_servers"}:
            parser.error(
                "configuration must have cp_hostname, openbao_hostname, enforce "
                "and optionally local_cidrs, ntp_servers"
            )
        if type(raw["enforce"]) is not bool or not all(
            isinstance(raw[k], str) for k in ("cp_hostname", "openbao_hostname")
        ):
            parser.error("invalid configuration types")
        files = render(EgressConfiguration(**raw))
    else:
        parser.error("unknown mode")
    args.output.mkdir(parents=True, exist_ok=False)
    for name, body in files.items():
        (args.output / name).write_text(body)


if __name__ == "__main__":
    main()
