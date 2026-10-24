"""Offline policy rendering checks; no kernel/resolver acceptance is claimed."""

import json
import sys
from pathlib import Path

import pytest

from deploy.egress.render import EgressConfiguration, main, render


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "control\nserver=evil.example",
        "-control.example",
        "control..example",
        "a" * 64 + ".example",
    ],
)
def test_configuration_rejects_address_or_directive_injection(host: str) -> None:
    with pytest.raises(ValueError):
        EgressConfiguration(host, "secrets.example")


def test_log_only_candidate_cannot_drop_traffic() -> None:
    files = render(EgressConfiguration("control.example", "secrets.example"))
    rules = files["dotmac-egress.nft"]
    assert "drop" not in rules
    assert "counter accept" in rules
    assert "hook forward" not in rules
    assert "flush ruleset" not in rules


def test_enforcing_drop_is_independent_of_logging_rate_limit() -> None:
    rules = render(EgressConfiguration("control.example", "secrets.example", True))[
        "dotmac-egress.nft"
    ]
    lines = rules.splitlines()
    log_index = next(i for i, line in enumerate(lines) if "limit rate" in line)
    assert lines[log_index + 1].strip() == "counter drop"


def test_expired_answers_and_ttl_floor_are_not_enabled() -> None:
    resolver = render(EgressConfiguration("control.example", "secrets.example"))[
        "smartdns.conf"
    ]
    assert "nftset-timeout yes" in resolver
    assert "serve-expired no" in resolver
    assert "prefetch-domain no" in resolver
    assert "rr-ttl-min" not in resolver
    assert "bind 127.0.0.1:53" in resolver
    assert "bind [::]" not in resolver


def test_renderer_never_replaces_existing_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = tmp_path / "input.json"
    configuration.write_text(
        json.dumps(
            {
                "cp_hostname": "control.example",
                "openbao_hostname": "secrets.example",
                "enforce": False,
            }
        )
    )
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", ["render", str(configuration), str(output)])
    main()
    original = (output / "dotmac-egress.nft").read_bytes()
    with pytest.raises(FileExistsError):
        main()
    assert (output / "dotmac-egress.nft").read_bytes() == original


# --- container_forward mode and host_output fixes ---------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402

from deploy.egress.render import (  # noqa: E402
    AllowEntry,
    ContainerForwardConfiguration,
    render_container_forward,
)


def _mapping(**over):
    base = {
        "internal_cidrs": ["198.51.100.0/24", "192.0.2.0/24"],
        "exempt_src_cidrs": [],
        "allow": [
            {"ip": "8.8.8.8", "proto": "udp", "port": 53},
            {"ip": "8.8.8.8", "proto": "tcp", "port": 53},
        ],
        "enforce": True,
    }
    base.update(over)
    return base


def _cf(**over) -> dict[str, str]:
    return render_container_forward(
        ContainerForwardConfiguration.from_mapping(_mapping(**over), "seabone")
    )


def test_cf_table_is_own_atomic_and_drop_only() -> None:
    nft = _cf()["staging-egress.nft"]
    assert "flush ruleset" not in nft
    lines = nft.splitlines()
    i = lines.index("table inet dotmac_staging_egress")
    assert lines[i + 1] == "delete table inet dotmac_staging_egress"
    assert lines[i + 2] == "table inet dotmac_staging_egress {"
    assert "accept" not in nft.replace("policy accept", "")
    assert nft.count("policy accept") == 2
    assert "counter drop" in nft


def test_cf_no_private_range_acceptance() -> None:
    nft = _cf()["staging-egress.nft"]
    for blanket in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert blanket not in nft
    for blanket in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "0.0.0.0/0"):
        with pytest.raises(ValueError):
            _cf(internal_cidrs=[blanket])
    with pytest.raises(ValueError):
        _cf(internal_cidrs=["10.0.0.0/15"])
    with pytest.raises(ValueError):
        _cf(internal_cidrs=["192.0.2.0/24", "192.0.2.128/25"])


def _chain(nft: str, name: str) -> list[str]:
    lines = nft.splitlines()
    start = lines.index(f" chain {name} {{")
    end = lines.index(" }", start)
    return [line.strip() for line in lines[start + 1 : end]]


def test_cf_new_traffic_denied_unless_allowlisted() -> None:
    nft = _cf()["staging-egress.nft"]
    chain = _chain(nft, "staging_out")
    assert chain[0] == "ct state established,related return"
    assert chain[-1] == "counter drop"
    allow_at = chain.index(
        "ip daddr . meta l4proto . th dport @allow_v4 counter return"
    )
    record_at = next(i for i, c in enumerate(chain) if "update @dropped_v4" in c)
    assert allow_at < record_at < len(chain) - 1
    assert "elements = { 8.8.8.8 . tcp . 53, 8.8.8.8 . udp . 53 }" in nft


def test_cf_scope_jumps_per_interface_and_uid_chain() -> None:
    nft = _cf()["staging-egress.nft"]
    for name in ("br-*", "docker0", "docker_gwbridge"):
        assert f'iifname "{name}" jump staging_out' in nft
    assert "meta skuid { 999 } jump genieacs_out" in nft
    gen = _chain(nft, "genieacs_out")
    assert 'oifname "lo" return' in gen
    assert gen[-1] == "counter drop"
    assert not any("@allow_v4" in line for line in gen)


def test_cf_log_only_cannot_drop() -> None:
    nft = _cf(enforce=False)["staging-egress.nft"]
    assert "drop" not in nft.replace("dropped", "").replace("stg-egress-drop", "")
    assert "counter return" in nft


def test_cf_rejects_bad_allow_and_placeholder_enforce() -> None:
    bad = [
        {"ip": "203.0.113.0/24", "proto": "tcp", "port": 53},
        {"ip": "203.0.113.10", "proto": "icmp", "port": 53},
        {"ip": "203.0.113.10", "proto": "tcp", "port": 0},
        {"ip": "203.0.113.10", "proto": "tcp", "port": True},
        {"ip": "0.0.0.0", "proto": "tcp", "port": 1},
        {"ip": "224.0.0.1", "proto": "udp", "port": 5353},
        {"ip": "203.0.113.10", "proto": "tcp", "port": 1, "x": 1},
    ]
    for entry in bad:
        with pytest.raises(ValueError):
            _cf(allow=[entry])
    # documentation range is accepted for log-only, refused when enforcing
    placeholder = [{"ip": "203.0.113.10", "proto": "udp", "port": 53}]
    _cf(allow=placeholder, enforce=False)
    with pytest.raises(ValueError):
        _cf(allow=placeholder, enforce=True)
    assert "8.8.8.8 . udp . 53" in _cf()["staging-egress.nft"]


def test_cf_profile_fixes_structure_and_rejects_overrides() -> None:
    with pytest.raises(ValueError):
        ContainerForwardConfiguration.from_mapping(
            _mapping(scope_ifnames=["eth0"]), "seabone"
        )
    with pytest.raises(ValueError):
        ContainerForwardConfiguration.from_mapping(_mapping(), "unknown")
    with pytest.raises(ValueError):
        ContainerForwardConfiguration(("eth0",), ("192.0.2.0/24",), (), (), (0,), False)
    with pytest.raises(ValueError):
        ContainerForwardConfiguration(
            ("bad name",), ("192.0.2.0/24",), (), (), (999,), False
        )


def test_cf_output_is_deterministic_and_hash_matches() -> None:
    import hashlib

    a = _cf()
    b = _cf(allow=list(reversed(_mapping()["allow"])))
    assert a == b
    digest = hashlib.sha256(a["staging-egress.nft"].encode()).hexdigest()
    assert a["staging-egress.nft.sha256"] == (
        f"{digest}  /etc/dotmac-staging-egress/staging-egress.nft\n"
    )


def test_cf_unit_has_no_stop_and_dropin_has_no_requires() -> None:
    files = _cf()
    unit = files["dotmac-staging-egress.service"]
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert "ExecStop" not in unit.replace("no ExecStop", "")
    assert (
        "ExecStart=/usr/sbin/nft -f /etc/dotmac-staging-egress/staging-egress.nft"
        in unit
    )
    assert "ExecStartPre=/usr/bin/sha256sum -c" in unit
    assert "Before=network-pre.target docker.service" in unit
    dropin = files["10-staging-egress.conf"]
    assert "Requires" not in dropin
    assert "Wants=dotmac-staging-egress.service" in dropin
    assert "ExecStartPre=/usr/sbin/nft list table inet dotmac_staging_egress" in dropin


@pytest.mark.skipif(shutil.which("nft") is None, reason="nft not installed")
def test_cf_nft_syntax_check() -> None:  # pragma: no cover - needs nft
    for enforce in (True, False):
        nft = _cf(
            allow=[{"ip": "8.8.8.8", "proto": "udp", "port": 53}], enforce=enforce
        )
        result = subprocess.run(
            ["nft", "-c", "-f", "-"],
            input=nft["staging-egress.nft"],
            text=True,
            capture_output=True,
            check=False,
        )
        if "Operation not permitted" in result.stderr:
            pytest.skip("nft needs netlink privileges to check")
        assert result.returncode == 0, result.stderr


def test_cf_cli_profile_with_allowlist_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "in.json"
    cfg.write_text(
        json.dumps(
            {
                "mode": "container_forward",
                "internal_cidrs": ["192.0.2.0/24"],
                "exempt_src_cidrs": [],
                "allow": [],
                "enforce": True,
            }
        )
    )
    allow = tmp_path / "allow.json"
    allow.write_text(
        json.dumps({"allow": [{"ip": "8.8.4.4", "proto": "tcp", "port": 53}]})
    )
    out = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "render",
            str(cfg),
            str(out),
            "--profile",
            "seabone",
            "--allowlist",
            str(allow),
        ],
    )
    main()
    assert "8.8.4.4 . tcp . 53" in (out / "staging-egress.nft").read_text()
    assert (out / "dotmac-staging-egress.service").exists()


def test_host_output_has_no_blanket_private_accept_or_open_ntp() -> None:
    rules = render(EgressConfiguration("control.example", "secrets.example", True))[
        "dotmac-egress.nft"
    ]
    for blanket in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "udp dport 123"):
        assert blanket not in rules
    scoped = render(
        EgressConfiguration(
            "control.example",
            "secrets.example",
            True,
            local_cidrs=("192.0.2.0/24",),
            ntp_servers=("203.0.113.123",),
        )
    )["dotmac-egress.nft"]
    assert "ip daddr { 192.0.2.0/24 } accept" in scoped
    assert "ip daddr { 203.0.113.123 } udp dport 123 accept" in scoped
    with pytest.raises(ValueError):
        EgressConfiguration(
            "control.example", "secrets.example", local_cidrs=("10.0.0.0/8",)
        )
    with pytest.raises(ValueError):
        EgressConfiguration(
            "control.example", "secrets.example", ntp_servers=("pool.ntp.example",)
        )


def test_allow_entry_is_immutable_value() -> None:
    assert (
        AllowEntry("203.0.113.10", "tcp", 53).key
        == AllowEntry("203.0.113.10", "tcp", 53).key
    )


def test_cf_example_input_renders_log_only_with_profile() -> None:
    example = (
        Path(__file__).parents[1]
        / "deploy/egress/container-forward.profile-input.example.json"
    )
    config = ContainerForwardConfiguration.from_mapping(
        json.loads(example.read_text()), "seabone"
    )
    assert config.enforce is False
    assert "counter drop" not in render_container_forward(config)["staging-egress.nft"]
