from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.models.network import OLTDevice
from app.services.network.huawei_cli_response import (
    HuaweiCliErrorCode,
    classify_huawei_cli_response,
)
from app.services.network.olt_ssh_ont.status import (
    get_registered_ont_serials,
    parse_registered_ont_inventory,
)

MA5800_SUMMARY = """
  In port 0/2/1, the total of ONTs are: 3, online: 2
  ONT  Run     Last                Last                Last
  ID   State   UpTime              DownTime            DownCause
  0    online  2026-07-15 07:30:17 2026-07-15 07:29:44 dying-gasp
  1    offline -                   -                   -
  2    online  2026-07-15 09:16:08 2026-07-15 09:15:30 dying-gasp
  ONT        SN        Type          Distance Rx/Tx power  Description
  ID                                    (m)      (dBm)
---- More ( Press 'Q' to break ) ----\x1b[37D  ------------------------------
  0   48575443FAA65084 EG8145V5         206   -17.30/2.34  Customer A
  1   48575443ABCDEF01 -                -     -/-          Customer B
  2   HWTC-ABCDEF02    EG8145V5         153   -21.30/2.03  Customer C
MA5800-X2#
"""


MA5608_INVENTORY = """
F/S/P   ONT-ID  SN                Control Run     Config Match  Description
0/1/12  5       48575443A31A3673  active  online  normal normal Customer A
0/1/12  6       HWTCABCDEF02      active  offline normal normal Customer B
boi-olt#
"""


def test_parse_ma5800_split_state_and_serial_tables() -> None:
    entries = parse_registered_ont_inventory(MA5800_SUMMARY, "0/2/1")

    assert [(item.onu_id, item.real_serial, item.run_state) for item in entries] == [
        (0, "48575443FAA65084", "online"),
        (1, "48575443ABCDEF01", "offline"),
        (2, "HWTC-ABCDEF02", "online"),
    ]
    assert {item.fsp for item in entries} == {"0/2/1"}


def test_parse_ma5608_legacy_inventory_table() -> None:
    entries = parse_registered_ont_inventory(MA5608_INVENTORY, "0/1/12")

    assert [(item.onu_id, item.real_serial, item.run_state) for item in entries] == [
        (5, "48575443A31A3673", "online"),
        (6, "HWTCABCDEF02", "offline"),
    ]


def test_registered_inventory_uses_explicit_profile_scoped_ports(monkeypatch) -> None:
    sent: list[tuple[str, int]] = []

    class _Transport:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (
            _Transport(),
            SimpleNamespace(),
            SimpleNamespace(prompt_regex=r"#\s*$"),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._prepare_huawei_read_shell",
        lambda _channel, prompt, **_k: prompt,
    )

    def _read(_channel, command, *, prompt, timeout_sec):
        assert prompt == r"#\s*$"
        sent.append((command, timeout_sec))
        return MA5800_SUMMARY

    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        _read,
    )

    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["gpon-0/2/1", "0/2/1"],
    )

    assert ok is True
    assert message == "Found 3 registered ONTs on 1 ports"
    assert len(entries) == 3
    assert sent == [("display ont info 0 2 1 all", 30)]


def test_registered_inventory_rejects_cli_errors(monkeypatch) -> None:
    class _Transport:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (
            _Transport(),
            SimpleNamespace(),
            SimpleNamespace(prompt_regex=r"#\s*$"),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._prepare_huawei_read_shell",
        lambda _channel, prompt, **_k: prompt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        lambda *_args, **_kwargs: "% Parameter error, the error locates at '^'",
    )

    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["0/2/1"],
    )

    assert ok is False
    # The rejection uses the canonical envelope owned by huawei_cli_response,
    # so the wrapped message still classifies; the port stays named in the
    # annotation that follows the device text.
    assert message.startswith("OLT rejected: ")
    assert "inventory read 0/2/1" in message
    assert (
        classify_huawei_cli_response(message).error_code
        == HuaweiCliErrorCode.PARAMETER_ERROR
    )
    assert entries == []


def _huawei_reader(monkeypatch, output):
    class _Transport:
        def close(self) -> None:
            pass

    monkeypatch.setattr(
        "app.services.network.olt_ssh._open_shell",
        lambda _olt: (
            _Transport(),
            SimpleNamespace(),
            SimpleNamespace(prompt_regex=r"#\s*$"),
        ),
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._prepare_huawei_read_shell",
        lambda _channel, prompt, **_k: prompt,
    )
    monkeypatch.setattr(
        "app.services.network.olt_ssh._run_huawei_paged_cmd",
        lambda *_args, **_kwargs: output,
    )


def test_recognized_empty_port_is_authoritative_not_a_failure(monkeypatch) -> None:
    """A device that reports no ONTs is trusted, not treated as a broken read."""
    _huawei_reader(monkeypatch, "No ONT is configured\nMA5800-X2#")

    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["0/2/1"],
    )

    assert ok is True
    assert entries == []
    assert message == "Found 0 registered ONTs on 1 ports"


def test_unrecognized_inventory_response_fails_closed(monkeypatch) -> None:
    """Output with no known table header or empty marker cannot be trusted."""
    _huawei_reader(monkeypatch, "garbled partial line without a header\nMA5800-X2#")

    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["0/2/1"],
    )

    assert ok is False
    assert "was not recognized" in message
    assert entries == []


def test_ont_description_containing_error_word_is_not_rejected(monkeypatch) -> None:
    """A customer description with 'error'/'invalid' must not fail the read."""
    output = MA5800_SUMMARY.replace("Customer A", "Error Systems Ltd").replace(
        "Customer B", "Invalid Address Holdings"
    )
    _huawei_reader(monkeypatch, output)

    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["0/2/1"],
    )

    assert ok is True
    assert len(entries) == 3


def test_paged_reader_has_one_cumulative_deadline(monkeypatch) -> None:
    from app.services.network import olt_ssh

    channel = SimpleNamespace(send=lambda _value: None)
    monotonic_values = iter([0.0, 0.2, 1.1])
    monkeypatch.setattr(olt_ssh.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(olt_ssh, "_send_huawei_command", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        olt_ssh,
        "_read_until_prompt",
        lambda *_args, **_kwargs: "{ <cr>||<K> }:",
    )

    with pytest.raises(TimeoutError, match="within 1s"):
        olt_ssh._run_huawei_paged_cmd(channel, "display ont info", timeout_sec=1)


def test_paged_reader_accepts_prompt_after_inline_continuation_marker(
    monkeypatch,
) -> None:
    from app.services.network import olt_ssh

    sent: list[str] = []
    channel = SimpleNamespace(send=sent.append)
    monkeypatch.setattr(olt_ssh, "_send_huawei_command", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        olt_ssh,
        "_read_until_prompt",
        lambda *_args, **_kwargs: "{ <cr>||<K> }:\nrows\nMA5800-X2#",
    )

    output = olt_ssh._run_huawei_paged_cmd(
        channel,
        "display ont info summary 0/2/1",
        prompt=r"MA5800-X2#\s*$",
    )

    assert output.endswith("MA5800-X2#")
    assert sent == []


def test_header_only_inventory_is_a_truncated_read_not_an_empty_port(
    monkeypatch,
) -> None:
    """A table header with no rows (cut-off read) must fail closed, never
    report an authoritative empty port."""
    _huawei_reader(
        monkeypatch,
        "  F/S/P   ONT         SN         Type\n  -----------------\nMA5800-X2#",
    )
    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"),
        ["0/2/1"],
    )
    assert ok is False
    assert "truncated" in message
    assert entries == []


GARKI_RAW = (
    "display ont info 0 2 1 all \n{ <cr>||<K> }: \n\n  Command:\n"
    "          display ont info 0 2 1 all\n"
    "  -----------------------------------------------------------------------------\n"
    "  F/S/P   ONT         SN         Control     Run      Config   Match    Protect\n"
    "          ID                     flag        state    state    state    side\n"
    "  -----------------------------------------------------------------------------\n"
    "  0/ 2/1    0  48575443FAA65084  active      online   normal   mismatch no \n"
    "  0/ 2/1    1  48575443FAA65085  active      offline  normal   match    no \n"
    "  ---- More ( Press 'Q' to break ) ----\x1b[37D                    \x1b[37D"
    "  0/ 2/1    2  48575443FAA65086  active      online   normal   match    no \n"
    "  -----------------------------------------------------------------------------\n"
    "  In port 0/2/1, the total of ONTs are: 3, online: 2\n"
    "MA5800-X2#"
)

OLD_FORMAT = (
    "  F/S/P   ONT         SN         Control     Run      Config   Match\n"
    "          ID                     flag        state    state    state\n"
    "  -------------------------------------------------------------------\n"
    "  0/2/1   0   48575443FAA65084   active      online   normal   mismatch\n"
    "  0/2/1   1   48575443FAA65085   active      offline  normal   match\n"
    "  0/2/1   2   48575443FAA65086   active      online   normal   match\n"
    "MA5800-X2#"
)


def test_garki_raw_transcript_parses_all_rows() -> None:
    from app.services.network.olt_ssh_ont.status import parse_registered_ont_inventory

    entries = parse_registered_ont_inventory(GARKI_RAW, "0/2/1")
    assert [(e.onu_id, e.real_serial, e.run_state) for e in entries] == [
        (0, "48575443FAA65084", "online"),
        (1, "48575443FAA65085", "offline"),
        (2, "48575443FAA65086", "online"),
    ]


def test_old_and_new_format_parse_identically() -> None:
    from app.services.network.olt_ssh_ont.status import parse_registered_ont_inventory

    assert parse_registered_ont_inventory(
        GARKI_RAW, "0/2/1"
    ) == parse_registered_ont_inventory(OLD_FORMAT, "0/2/1")


def test_garki_raw_through_get_registered_serials_is_not_zero(monkeypatch) -> None:
    _huawei_reader(monkeypatch, GARKI_RAW)
    ok, _message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"), ["0/2/1"]
    )
    assert ok is True
    assert len(entries) == 3


def test_rows_present_but_unparseable_never_returns_ok_with_zero(monkeypatch) -> None:
    from app.services.network.olt_ssh_ont import status as status_mod

    _huawei_reader(monkeypatch, GARKI_RAW)
    monkeypatch.setattr(status_mod, "parse_registered_ont_inventory", lambda *_a: [])
    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"), ["0/2/1"]
    )
    assert ok is False
    assert "could not be parsed" in message
    assert entries == []


def test_real_garki_capture_parses_exactly_16_with_4_online() -> None:
    from pathlib import Path

    raw = (
        Path(__file__).parent
        / "transcripts/huawei/MA5800-X2/display_ont_info_port_all.txt"
    ).read_text()
    entries = parse_registered_ont_inventory(raw, "0/2/1")
    assert len(entries) == 16
    assert [e.onu_id for e in entries] == list(range(16))
    assert entries[0].real_serial == "48575443FAA65084"
    assert entries[15].real_serial == "485754431DA6B7D1"
    assert {e.onu_id for e in entries if e.run_state == "online"} == {0, 4, 6, 10}
    assert sum(e.run_state == "online" for e in entries) == 4
    assert all(e.fsp == "0/2/1" for e in entries)


def test_real_garki_capture_through_get_registered_serials(monkeypatch) -> None:
    from pathlib import Path

    raw = (
        Path(__file__).parent
        / "transcripts/huawei/MA5800-X2/display_ont_info_port_all.txt"
    ).read_text()
    _huawei_reader(monkeypatch, raw)
    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"), ["0/2/1"]
    )
    assert ok is True and len(entries) == 16, message


_SUMMARY_PAGED = (
    "display ont info summary 0/ 2/1\\n"
    "  In port 0/ 2/1 , the total of ONTs are: 2, online: 1\\n"
    "  ONT  Run     Last                Last                Last\\n"
    "  ID   State   UpTime              DownTime            DownCause\\n"
    "  0    online  2026-07-15 07:30:17 2026-07-15 07:29:44 dying-gasp\\n"
    "  1    offline -                   -                   -\\n"
    "  ONT        SN        Type          Distance Rx/Tx power  Description\\n"
    "  ID                                    (m)      (dBm)\\n"
    "---- More ( Press 'Q' to break ) ----\\x1b[37D                     \\x1b[37D"
    "  0   48575443FAA65084 EG8145V5         206   -17.30/2.34  CUSTOMER_00\\n"
    "  1   485754437D4733C3 -                -     -/-          CUSTOMER_01\\n"
    "MA5800-X2#"
)


def test_ma5800_summary_paged_ansi_parses_with_states() -> None:
    entries = parse_registered_ont_inventory(
        _SUMMARY_PAGED.encode().decode("unicode_escape"), "0/2/1"
    )
    assert [(e.onu_id, e.real_serial, e.run_state) for e in entries] == [
        (0, "48575443FAA65084", "online"),
        (1, "485754437D4733C3", "offline"),
    ]


def test_ma5800_summary_cut_off_after_state_table_fails_closed(monkeypatch) -> None:
    cut = _SUMMARY_PAGED.encode().decode("unicode_escape").split("  ONT        SN")[0]
    _huawei_reader(monkeypatch, cut + "MA5800-X2#")
    ok, message, entries = get_registered_ont_serials(
        OLTDevice(name="Garki", vendor="Huawei", model="MA5800-X2"), ["0/2/1"]
    )
    assert ok is False and entries == []
