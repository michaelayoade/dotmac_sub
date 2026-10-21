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
