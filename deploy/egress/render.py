#!/usr/bin/env python3
"""Render host-only FQDN egress candidates; never install or execute them."""

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

    def __post_init__(self) -> None:
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
            "  udp dport 123 accept",
            "  ip daddr @egress_v4 accept",
            "  ip6 daddr @egress_v6 accept",
            "  ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept",
            '  limit rate 10/second log prefix "dotmac-egress-unmatched: "',
            f"  {terminal}",
            " }",
            "}",
            "",
        )
    )
    return {"smartdns.conf": resolver, "dotmac-egress.nft": rules}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    raw = json.loads(args.configuration.read_text())
    if not isinstance(raw, dict) or set(raw) != {
        "cp_hostname",
        "openbao_hostname",
        "enforce",
    }:
        parser.error(
            "configuration must have exactly cp_hostname, openbao_hostname, enforce"
        )
    if type(raw["enforce"]) is not bool or not all(
        isinstance(raw[k], str) for k in ("cp_hostname", "openbao_hostname")
    ):
        parser.error("invalid configuration types")
    configuration = EgressConfiguration(**raw)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, body in render(configuration).items():
        (args.output / name).write_text(body)


if __name__ == "__main__":
    main()
