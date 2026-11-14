#!/usr/bin/env python3
"""Refuse NEW public IP address literals added by a pull request.

Policy (Michael, 2026-11-14): an IP address is not a secret, so existing
content is left alone ("fix going forward"). Only lines ADDED by the diff
``git diff --unified=0 <base>...HEAD`` are inspected; context and removed lines
are never flagged, and nothing is rewritten.

A candidate is flagged only when ``ipaddress`` accepts it and it is globally
routable (``is_global``). Private, loopback, link-local, CGNAT (100.64/10),
unspecified, multicast, reserved and documentation ranges (192.0.2.0/24,
198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) pass. A CIDR network written
with a prefix is judged by its network address: private networks pass, public
networks are flagged and need the inline marker.

Opt out on a single line with an inline marker and a reason::

    ip-literal-ok: <reason>

Use a role name in code and docs and a documentation range in tests; deploy
addresses are supplied at deploy time. Stdlib only.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass

MARKER = "ip-literal-ok:"

SKIPPED_NAMES = frozenset(
    {
        "poetry.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "uv.lock",
        "Pipfile.lock",
        "Cargo.lock",
        "composer.lock",
        "Gemfile.lock",
    }
)
SKIPPED_SUFFIXES = (
    ".lock",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tgz", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".so", ".pyc", ".db",
    ".sqlite", ".xlsx", ".mp4", ".mov", ".svg",
)  # fmt: skip

# Dotted quad not glued to other word characters or extra dotted numbers, so
# "1.2.3.4.5" and "v1.2.3.4" do not match. Optional /prefix for networks.
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\w)(?!\.\d)(?:/(\d{1,3})(?!\d))?")
# Colon-hex candidate; real validation is done by ``ipaddress``.
_IPV6 = re.compile(
    r"(?<![\w:.])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![\w:])(?:/(\d{1,3})(?!\d))?"
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    masked: str


def _mask(address: str) -> str:
    if ":" in address:
        return address.split(":", 1)[0] + "::x"
    return address.split(".", 1)[0] + ".x.x.x"


def _is_public(text: str, prefix: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(text)
        if prefix is not None:
            net = ipaddress.ip_network(f"{text}/{prefix}", strict=False)
            addr = net.network_address
    except ValueError:
        return False
    if addr.is_multicast or addr.is_reserved:
        return False
    return bool(addr.is_global)


def find_public_literals(line: str) -> list[str]:
    """Return masked forms of public addresses found in one line of text."""
    if MARKER in line:
        return []
    found: list[str] = []
    for pattern in (_IPV4, _IPV6):
        for match in pattern.finditer(line):
            if _is_public(match.group(1), match.group(2)):
                found.append(_mask(match.group(1)))
    return found


def skipped_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in SKIPPED_NAMES or name.lower().endswith(SKIPPED_SUFFIXES)


def scan_diff(diff_text: str) -> list[Finding]:
    """Scan unified diff text; only added lines are inspected."""
    findings: list[Finding] = []
    path: str | None = None
    lineno = 0
    in_hunk = False
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path, in_hunk = None, False
        elif not in_hunk and raw.startswith("+++ "):
            target = raw[4:].split("\t", 1)[0]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif raw.startswith("@@"):
            hunk = _HUNK.match(raw)
            if hunk:
                lineno = int(hunk.group(1))
                in_hunk = True
        elif in_hunk and raw.startswith("+"):
            if path and not skipped_path(path):
                for masked in find_public_literals(raw[1:]):
                    findings.append(Finding(path, lineno, masked))
            lineno += 1
        # "-" lines, context and "\ No newline" markers never advance or flag.
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base commit SHA or ref")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    result = subprocess.run(
        [
            "git", "-c", "core.quotepath=false", "diff", "--unified=0",
            "--no-color", "--no-ext-diff", "--no-renames",
            f"{args.base}...{args.head}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )  # fmt: skip
    if result.returncode != 0:
        print(f"git diff failed: {result.stderr.strip()}", file=sys.stderr)
        return 2
    findings = scan_diff(result.stdout)
    for f in findings:
        print(f"{f.path}:{f.line}: new public IP address literal ({f.masked})")
    if findings:
        print(
            f"\n{len(findings)} new public IP literal(s). Use a role name "
            "(code, docs) or a documentation range (tests: 192.0.2.0/24, "
            "198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32); deploy addresses "
            f"are supplied at deploy time. If a public address is genuinely "
            f"required, add the inline marker '{MARKER} <reason>' to that line."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
