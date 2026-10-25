"""Paged-output handling for ``_run_huawei_cmd``, ``OltSession.run_command``,
the state-import port reader and the TR-069 auto-bind. No OLT is contacted."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.network import olt_ssh
from app.services.network.olt_state_import import _read_port_registrations

CORPUS = Path(__file__).parent / "transcripts" / "huawei"
PAGER = "---- More ( Press 'Q' to break ) ----"
ERASE = "\x1b[37D" + " " * 37 + "\x1b[37D"
PROMPT = "boi-olt#"


class PagedChannel:
    """Sends ``first`` (ending at a pager) and releases ``rest`` on a space."""

    def __init__(self, lines: list[str], page_after: int = 10) -> None:
        self.sent: list[str] = []
        self.queue: list[str] = []
        self._rest = ""
        self.lines = lines
        self.page_after = page_after
        self.closed = False

    def settimeout(self, _v: float) -> None:
        return None

    def send(self, value: str) -> None:
        self.sent.append(value)
        if value == " ":
            self.queue.append(self._rest)
            return
        head = "\r\n".join(self.lines[: self.page_after])
        tail = "\r\n".join(self.lines[self.page_after :])
        self.queue.append(f"{value.strip()}\r\n{head}\r\n{PAGER}{ERASE}")
        self._rest = f"{tail}\r\n{PROMPT}"

    def recv(self, _n: int) -> bytes:
        if self.queue:
            return self.queue.pop(0).encode()
        time.sleep(0.01)
        raise TimeoutError


LINES = [f"line {i}" for i in range(25)]


def test_run_huawei_cmd_answers_pager_and_strips_it() -> None:
    ch = PagedChannel(LINES)
    out = olt_ssh._run_huawei_cmd(ch, "display version", prompt=r"boi-olt#\s*$")
    assert ch.sent == ["display version\n", " "]
    assert "line 0" in out and "line 24" in out
    assert "More" not in out and "\x1b" not in out


def test_run_huawei_cmd_raises_when_no_prompt_returns(monkeypatch) -> None:
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *_a, **_k: "partial")
    with pytest.raises(TimeoutError):
        olt_ssh._run_huawei_cmd(PagedChannel(LINES), "display x", prompt=PROMPT)


def test_session_run_command_answers_pager(monkeypatch) -> None:
    from app.services.network import olt_ssh_session
    from app.services.network.olt_ssh_session import CliMode, OltSession

    ch = PagedChannel(LINES)
    monkeypatch.setattr(
        olt_ssh_session, "_send", lambda _o, c, cmd, **_k: c.send(cmd + "\n")
    )
    session = OltSession(
        olt=SimpleNamespace(name="BOI"),
        transport=None,
        channel=ch,
        prompt_regex=r"boi-olt#\s*$",
        current_mode=CliMode.ENABLE,
    )
    result = session.run_command("display version")
    assert result.success is True
    assert ch.sent == ["display version\n", " "]
    assert "line 24" in result.output and "More" not in result.output


# --- state-import port reader -------------------------------------------------


class _Profile:
    def display_ont_info_all(self, fsp: str) -> str:
        return f"summary {fsp}"

    def display_ont_info(self, fsp: str, ont_id: int) -> str:
        return f"detail {fsp} {ont_id}"


def _detail(ont_id: int) -> str:
    return (
        f"  F/S/P                   : 0/2/1\n  ONT-ID                  : {ont_id}\n"
        "  Control flag            : active\n  Run state               : online\n"
        "  Config state            : normal\n  Match state             : match\n"
        "  Description             : CUSTOMER_x\n  Line profile ID         : 10\n"
        "  Service profile ID      : 20\n"
    )


GARKI = (CORPUS / "MA5800-X2" / "display_ont_info_port_all.txt").read_text()


def _runner(summary: str, detail=_detail):
    def run(command: str) -> str:
        if command.startswith("summary"):
            return summary
        return detail(int(command.rsplit(" ", 1)[1]))

    return run


def test_full_garki_port_is_complete() -> None:
    warnings: list[str] = []
    reads, complete = _read_port_registrations(
        _runner(GARKI), _Profile(), "0/2/1", warnings
    )
    assert complete is True and len(reads) == 16 and warnings == []


def test_garki_truncated_at_page_break_is_incomplete() -> None:
    cut = GARKI[: GARKI.index("---- More")]
    warnings: list[str] = []
    reads, complete = _read_port_registrations(
        _runner(cut), _Profile(), "0/2/1", warnings
    )
    assert complete is False and reads == []
    assert warnings and "incomplete or unreadable" in warnings[0]


def test_trailer_count_mismatch_is_incomplete() -> None:
    bad = GARKI.replace("total of ONTs are: 16", "total of ONTs are: 17")
    reads, complete = _read_port_registrations(_runner(bad), _Profile(), "0/2/1", [])
    assert complete is False and reads == []


def test_command_timeout_is_incomplete() -> None:
    def run(_c: str) -> str:
        raise TimeoutError("no prompt")

    warnings: list[str] = []
    assert _read_port_registrations(run, _Profile(), "0/2/1", warnings) == ([], False)
    assert "Skipped 0/2/1" in warnings[0]


def test_paged_boi_detail_never_overwrites_with_none() -> None:
    boi = (CORPUS / "MA5608T" / "display_ont_info_by_sn.txt").read_text()
    cut_detail = boi[: boi.index("---- More")]  # profile ids missing
    reads, complete = _read_port_registrations(
        _runner(GARKI, detail=lambda _i: cut_detail), _Profile(), "0/2/1", []
    )
    assert complete is False
    assert reads == []  # nothing is written, so no None lands on good data


def test_incomplete_read_deactivates_nothing(db_session) -> None:
    from datetime import UTC, datetime

    from app.models.network import OLTDevice, OltOntRegistration
    from app.services.network.olt_state_import import _deactivate_unseen_registrations

    olt = OLTDevice(name="OLT-PG", mgmt_ip="192.0.2.5", is_active=True)
    db_session.add(olt)
    db_session.flush()
    reg = OltOntRegistration(
        olt_id=olt.id, fsp="0/2/1", ont_id_on_olt=3, serial_number="S", is_active=True
    )
    db_session.add(reg)
    db_session.flush()
    cut = GARKI[: GARKI.index("---- More")]
    reads, complete = _read_port_registrations(_runner(cut), _Profile(), "0/2/1", [])
    n = _deactivate_unseen_registrations(
        db_session,
        olt.id,
        {(r.fsp, r.ont_id) for r in reads},
        datetime.now(UTC),
        read_complete=complete,
    )
    assert n <= 0  # nothing deactivated (empty seen set or skipped sweep)
    db_session.refresh(reg)
    assert reg.is_active is True


# --- TR-069 auto-bind ----------------------------------------------------------


def _acs_olt():
    return SimpleNamespace(
        name="OLT-AB",
        tr069_acs_server=SimpleNamespace(
            name="Primary ACS",
            cwmp_url="http://acs.example.com/cwmp/",
            cwmp_username="u",
            cwmp_password=None,
            periodic_inform_interval=300,
        ),
    )


def test_auto_bind_does_not_create_when_profile_list_unreadable(monkeypatch) -> None:
    created: list[object] = []
    monkeypatch.setattr(
        olt_ssh,
        "get_tr069_server_profiles",
        lambda _o: (False, "tr069 profile list unreadable", []),
    )
    monkeypatch.setattr(
        olt_ssh, "create_tr069_server_profile", lambda *_a, **_k: created.append(1)
    )
    olt_ssh._auto_bind_tr069_after_authorize(_acs_olt(), "0/2/1", 6)
    assert created == []


def test_profile_list_with_paged_cut_detail_fails_closed(monkeypatch) -> None:
    from app.services.network import olt_ssh_profiles

    ch = PagedChannel(["x"], page_after=1)
    monkeypatch.setattr(olt_ssh, "_open_shell", lambda _o: (_T(), ch, _Pol()))
    monkeypatch.setattr(olt_ssh, "_read_until_prompt", lambda *_a, **_k: "")
    outputs = iter(["  1  ACS-A\n", "  Profile-name : ACS-A\n  Binding times : 0\n"])
    monkeypatch.setattr(olt_ssh, "_run_huawei_cmd", lambda *_a, **_k: next(outputs))
    ok, msg, profiles = olt_ssh_profiles.get_tr069_server_profiles(SimpleNamespace())
    assert ok is False and "unreadable" in msg and profiles == []


class _T:
    def close(self) -> None:
        return None


class _Pol:
    prompt_regex = r"#\s*$"
    paging_off_command = None


# --- iphost priority with unknown line profile ---------------------------------


def test_unknown_line_profile_never_picks_majority_priority(db_session) -> None:
    import uuid

    from app.models.network import (
        OLTDevice,
        OltLineProfile,
        OltLineProfileGemMapping,
    )
    from app.services.network.iphost_priority import _resolve_mapping_priority

    olt = OLTDevice(name="OLT-IP", mgmt_ip="192.0.2.6", is_active=True)
    db_session.add(olt)
    db_session.flush()
    assert uuid  # keep import used for clarity of uuid-keyed models
    for line_profile in (1, 2, 3):
        db_session.add(OltLineProfile(olt_id=olt.id, profile_id=line_profile))
    db_session.flush()
    for line_profile, priority in ((1, 5), (2, 5), (3, 2)):
        db_session.add(
            OltLineProfileGemMapping(
                olt_id=olt.id,
                line_profile_id=line_profile,
                source="test",
                source_key=f"k{line_profile}",
                gem_index=2,
                priority=priority,
            )
        )
    db_session.flush()
    # Majority would say 5; with the profile unknown that is a guess -> None.
    assert (
        _resolve_mapping_priority(
            db_session, olt_id=olt.id, gem_index=2, line_profile_id=None
        )
        is None
    )
    assert (
        _resolve_mapping_priority(
            db_session, olt_id=olt.id, gem_index=2, line_profile_id=3
        )
        == 2
    )
