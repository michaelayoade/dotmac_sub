"""Contract for the new-public-IP-literal CI guard (added lines only)."""

from __future__ import annotations

import ipaddress

from scripts.ci.check_new_ip_literals import find_public_literals, scan_diff

# Globally routable but synthetic: built from integers so no literal is stored.
PUBLIC_V4 = str(ipaddress.IPv4Address((8 << 24) | (4 << 16) | (4 << 8) | 4))
PUBLIC_V6 = str(ipaddress.IPv6Address((0x2606 << 112) | 0x4700 << 96 | 1))


def _diff(*body: str, path: str = "app/x.py") -> str:
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,0 +5,{len(body)} @@\n" + "\n".join(body) + "\n"
    )


def test_added_public_v4_flagged_with_masked_output() -> None:
    (finding,) = scan_diff(_diff(f"+host = '{PUBLIC_V4}'"))
    assert (finding.path, finding.line) == ("app/x.py", 5)
    assert finding.masked == "8.x.x.x"
    assert PUBLIC_V4 not in finding.masked


def test_added_public_v6_flagged() -> None:
    (finding,) = scan_diff(_diff(f"+addr = {PUBLIC_V6}"))
    assert finding.masked.endswith("::x")


def test_public_network_flagged_unless_marked() -> None:
    assert find_public_literals(f"allow {PUBLIC_V4}/22")
    assert not find_public_literals(f"allow {PUBLIC_V4}/22  # ip-literal-ok: partner")


def test_non_public_addresses_pass() -> None:
    for ok in (
        "10.1.2.3", "172.16.0.9", "192.168.1.1", "127.0.0.1", "169.254.1.1",
        "100.64.0.1", "100.127.255.254", "0.0.0.0", "192.0.2.10",
        "198.51.100.7", "203.0.113.200", "::1", "::", "fe80::1", "fd00::5",
        "2001:db8::1", "10.0.0.0/8", "192.0.2.0/24", "0.0.0.0/0",
    ):  # fmt: skip
        assert not find_public_literals(f"x = {ok}"), ok


def test_version_strings_do_not_match() -> None:
    for text in ("1.2.3.4.5", "10.0.0", "v1.2.3.4", "version 8.4.4.4.1", "12:30:45"):
        assert not find_public_literals(text), text


def test_marker_opts_line_out() -> None:
    assert not find_public_literals(f"{PUBLIC_V4}  # ip-literal-ok: vendor docs")


def test_removed_and_context_lines_ignored() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        f"@@ -1,2 +1,2 @@\n-old = '{PUBLIC_V4}'\n context {PUBLIC_V4}\n+new = 1\n"
    )
    assert scan_diff(diff) == []


def test_lockfiles_and_binary_paths_skipped() -> None:
    assert scan_diff(_diff(f"+{PUBLIC_V4}", path="poetry.lock")) == []
    assert scan_diff(_diff(f"+{PUBLIC_V4}", path="static/logo.png")) == []


def test_line_numbers_follow_hunk_offsets() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        f"@@ -0,0 +10,3 @@\n+ok = 1\n+ok2 = 2\n+bad = '{PUBLIC_V4}'\n"
    )
    assert [f.line for f in scan_diff(diff)] == [12]
