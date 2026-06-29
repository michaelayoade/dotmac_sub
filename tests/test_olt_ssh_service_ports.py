from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services.network import olt_ssh_service_ports as service_ports
from app.services.network.olt_ssh import ServicePortEntry


def _matching_port(index: int = 123) -> ServicePortEntry:
    return ServicePortEntry(
        index=index,
        vlan_id=203,
        ont_id=5,
        gem_index=1,
        flow_type="vlan",
        flow_para="203",
        state="up",
        fsp="0/1/7",
        tag_transform="translate",
    )


def _patch_successful_shell(monkeypatch, command_output: str) -> None:
    transport = MagicMock()
    channel = MagicMock()

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (transport, channel, SimpleNamespace(prompt_regex=r"#\s*$")),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._read_until_prompt",
        lambda *_args, **_kwargs: "",
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_cmd",
        lambda _channel, command, **_kwargs: (
            command_output if command.startswith("service-port") else ""
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh.is_error_output",
        lambda output: "service virtual port has existed already" in output.casefold(),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._invalidate_olt_read_cache",
        lambda *_args, **_kwargs: None,
    )


def test_create_single_service_port_accepts_verified_duplicate(monkeypatch) -> None:
    output = (
        "Failure: The service virtual port has existed already. "
        "Conflicted service virtual port index: 123"
    )
    _patch_successful_shell(monkeypatch, output)
    monkeypatch.setattr(
        service_ports,
        "get_service_port_by_index",
        lambda _olt, index: (True, "found", _matching_port(index)),
    )

    ok, message, assigned_index = service_ports.create_single_service_port(
        SimpleNamespace(id="olt-a", name="OLT-A"),
        "0/1/7",
        5,
        1,
        203,
    )

    assert ok is True
    assert assigned_index == 123
    assert "already exists" in message


def test_create_single_service_port_rejects_mismatched_duplicate(monkeypatch) -> None:
    output = (
        "Failure: The service virtual port has existed already. "
        "Conflicted service virtual port index: 123"
    )
    _patch_successful_shell(monkeypatch, output)
    mismatched = _matching_port()
    mismatched.ont_id = 9
    monkeypatch.setattr(
        service_ports,
        "get_service_port_by_index",
        lambda _olt, _index: (True, "found", mismatched),
    )

    ok, message, assigned_index = service_ports.create_single_service_port(
        SimpleNamespace(id="olt-a", name="OLT-A"),
        "0/1/7",
        5,
        1,
        203,
    )

    assert ok is False
    assert assigned_index is None
    assert "different ONT/VLAN/GEM" in message
