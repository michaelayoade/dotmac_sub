"""Contract for the new-public-IP-literal CI guard (added lines only)."""

from __future__ import annotations

import ipaddress
import subprocess
from pathlib import Path

import pytest

from scripts.ci.check_new_ip_literals import find_public_literals, main, scan_diff

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


def test_url_path_and_short_prefix_judge_the_host() -> None:
    assert find_public_literals(f"http://{PUBLIC_V4}/123/x")
    assert find_public_literals(f"http://{PUBLIC_V4}:8080/status")
    assert find_public_literals(f"{PUBLIC_V4}/1")
    assert find_public_literals(f"{PUBLIC_V4}/0")


def test_mapped_forms_checked_via_ipv4_mapped() -> None:
    mapped = ipaddress.IPv6Address(f"::ffff:{PUBLIC_V4}")
    assert find_public_literals(f"a = {mapped}")  # hex form
    assert find_public_literals(f"a = ::ffff:{PUBLIC_V4}")  # dotted form
    assert not find_public_literals("a = ::ffff:192.0.2.1")
    assert not find_public_literals("a = ::ffff:10.1.2.3")


def test_zero_padded_octets_are_normalised() -> None:
    padded = ".".join(f"{int(o):03d}" for o in PUBLIC_V4.split("."))
    assert find_public_literals(f"a = {padded}")
    assert not find_public_literals("a = 010.001.002.003")
    assert not find_public_literals("a = 999.1.1.1")


def test_ipv6_seven_groups_with_trailing_double_colon() -> None:
    assert find_public_literals("a = 2606:4700:1:2:3:4:5::")


def test_marker_requires_a_reason() -> None:
    assert find_public_literals(f"{PUBLIC_V4}  # ip-literal-ok:")
    assert find_public_literals(f"{PUBLIC_V4}  # ip-literal-ok:   ")
    assert not find_public_literals(f"{PUBLIC_V4}  # ip-literal-ok: vendor")


def test_added_line_starting_with_plus_plus_is_still_scanned() -> None:
    diff = _diff(f"+++ {PUBLIC_V4}", f"+x = '{PUBLIC_V4}'")
    assert [f.line for f in scan_diff(diff)] == [5, 6]


def test_svg_and_multi_file_path_reset() -> None:
    diff = _diff(f"+{PUBLIC_V4}", path="a.svg") + _diff(f"+{PUBLIC_V4}", path="b.py")
    assert [f.path for f in scan_diff(diff)] == ["b.py"]


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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "old.txt").write_text(f"legacy {PUBLIC_V4}\nline2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _commit(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD~1")


def test_main_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "new.py").write_text("x = '192.0.2.1'\n")
    base = _commit(repo)
    assert main(["--base", base]) == 0
    (repo / "bad.py").write_text(f"x = '{PUBLIC_V4}'\n")
    base = _commit(repo)
    assert main(["--base", base]) == 1
    out = capsys.readouterr().out
    assert "bad.py:1" in out and PUBLIC_V4 not in out


def test_main_fails_closed_on_empty_or_unknown_base(repo: Path) -> None:
    assert main(["--base", ""]) == 2
    assert main(["--base", "   "]) == 2
    assert main(["--base", "0" * 40]) == 2


def test_main_scans_files_git_treats_as_binary(repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.cfg binary\n")
    _commit(repo)
    (repo / "x.cfg").write_text(f"host = {PUBLIC_V4}\n")
    (repo / "nul.txt").write_bytes(b"\0" + f"host = {PUBLIC_V4}\n".encode())
    base = _commit(repo)
    assert main(["--base", base]) == 1


def test_main_rename_of_file_with_existing_address_passes(repo: Path) -> None:
    _git(repo, "mv", "old.txt", "moved.txt")
    base = _commit(repo)
    assert main(["--base", base]) == 0
    (repo / "moved.txt").write_text(f"legacy {PUBLIC_V4}\nline2\nnew {PUBLIC_V4}\n")
    base = _commit(repo)
    assert main(["--base", base]) == 1
