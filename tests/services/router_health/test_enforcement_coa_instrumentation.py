"""Behaviour-preservation + evidence tests for the CoA refactor in enforcement.py."""

from __future__ import annotations

import types
from uuid import uuid4

import pytest
from pyrad.client import Timeout
from pyrad.packet import CoARequest, DisconnectACK, DisconnectNAK

from app.services import enforcement as enf
from app.services.enforcement import CoADisconnectDisposition as D


class FakeReply:
    def __init__(self, code, cause=None):
        self.code = code
        self._cause = cause

    def __getitem__(self, key):
        if key == "Error-Cause" and self._cause is not None:
            return [self._cause]
        raise KeyError(key)


class FakeClient:
    reply: object = None
    sent: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.retries = 0
        self.timeout = 0

    def CreateCoAPacket(self, code):
        return {"code": code}

    def SendPacket(self, req):
        FakeClient.sent.append(req)
        if isinstance(FakeClient.reply, BaseException):
            raise FakeClient.reply
        return FakeClient.reply


@pytest.fixture()
def wired(monkeypatch):
    recorded: list[tuple] = []
    monkeypatch.setattr(enf, "Client", FakeClient)
    monkeypatch.setattr(enf, "Dictionary", lambda path: object())
    monkeypatch.setattr(enf, "_coa_enabled", lambda db: True)
    monkeypatch.setattr(enf, "_radius_dictionary_path", lambda db: "/dict")
    monkeypatch.setattr(enf, "_resolve_coa_secret", lambda db, nas: "s3cret")
    monkeypatch.setattr(enf, "_radius_timeout_sec", lambda db: 1.0)
    monkeypatch.setattr(enf, "_coa_retries", lambda db: 1)
    monkeypatch.setattr(
        enf._coa_evidence,
        "record_coa_outcome",
        lambda nas_id, kind, disposition, **kw: recorded.append(
            (nas_id, kind.value, disposition)
        ),
    )
    enf.reset_coa_cache()
    FakeClient.sent = []
    yield recorded
    enf.reset_coa_cache()


def nas(host="192.0.2.10"):
    return types.SimpleNamespace(
        id=uuid4(),
        nas_ip=host,
        management_ip=None,
        ip_address=None,
        coa_port=3799,
        name="n",
    )


@pytest.mark.parametrize(
    "reply,expected,marks",
    [
        (FakeReply(DisconnectACK), D.disconnected, "supported"),
        (FakeReply(DisconnectNAK, 503), D.session_not_found, "supported"),
        (FakeReply(DisconnectNAK, 500), D.rejected, "supported"),
        (FakeReply(99), D.unexpected_response, "supported"),
        (Timeout(), D.timeout, "unsupported"),
        (RuntimeError("boom"), D.transport_error, "none"),
    ],
)
def test_disconnect_dispositions_cache_behaviour_and_one_evidence_call(
    wired, reply, expected, marks
) -> None:
    FakeClient.reply = reply
    device = nas()
    outcome = enf._send_coa_disconnect(None, device, "user", None, None)
    assert outcome.disposition is expected
    assert enf._coa_disabled_for_nas(device.id) is (marks == "unsupported")
    assert wired == [(device.id, "disconnect", expected.value)]


def test_suppressed_attempt_is_recorded_and_sends_nothing(wired) -> None:
    device = nas()
    enf._mark_coa_unsupported(device.id)
    outcome = enf._send_coa_disconnect(None, device, "u", None, None)
    assert outcome.disposition is D.temporarily_suppressed
    assert FakeClient.sent == []
    assert wired == [(device.id, "disconnect", "temporarily_suppressed")]


def test_missing_host_and_secret_are_recorded(wired, monkeypatch) -> None:
    device = nas(host=None)
    assert (
        enf._send_coa_disconnect(None, device, "u", None, None).disposition
        is D.missing_nas_host
    )
    device2 = nas()
    monkeypatch.setattr(enf, "_resolve_coa_secret", lambda db, n: None)
    assert (
        enf._send_coa_disconnect(None, device2, "u", None, None).disposition
        is D.missing_secret
    )
    assert [r[2] for r in wired] == ["missing_nas_host", "missing_secret"]


def test_disabled_is_not_recorded(wired, monkeypatch) -> None:
    monkeypatch.setattr(enf, "_coa_enabled", lambda db: False)
    out = enf._send_coa_disconnect(None, nas(), "u", None, None)
    assert out.disposition is D.disabled
    assert wired == []


def test_evidence_failure_does_not_change_the_result(wired, monkeypatch) -> None:
    def explode(*a, **k):
        raise RuntimeError("evidence down")

    monkeypatch.setattr(enf._coa_evidence, "record_coa_outcome", explode)
    FakeClient.reply = FakeReply(DisconnectACK)
    assert enf._send_coa_disconnect(None, nas(), "u", None, None).disconnected


def test_update_keeps_bool_contract_and_records(wired) -> None:
    profile = types.SimpleNamespace(
        name="p",
        code="p",
        download_speed=0,
        upload_speed=0,
        mikrotik_rate_limit=None,
        burst_download=0,
        burst_upload=0,
        burst_threshold=0,
        burst_time=0,
    )
    device = nas()
    FakeClient.reply = FakeReply(99)
    assert enf._send_coa_update(None, device, "u", None, None, profile) is True
    FakeClient.reply = Timeout()
    assert enf._send_coa_update(None, device, "u", None, None, profile) is False
    assert [r[1:] for r in wired] == [("update", "sent"), ("update", "timeout")]
    assert enf._coa_disabled_for_nas(device.id)
    assert FakeClient.sent[0]["code"] == CoARequest


def test_probe_never_touches_the_negative_cache_or_evidence(wired) -> None:
    device = nas()
    for reply in (FakeReply(DisconnectNAK, 503), Timeout(), RuntimeError("x")):
        FakeClient.reply = reply
        enf.send_coa_probe_disconnect(None, device, username="dotmac-coa-canary-aa")
    assert enf._coa_disabled_for_nas(device.id) is False
    assert wired == []
    # only User-Name is on the packet
    assert all(set(req) == {"code", "User-Name"} for req in FakeClient.sent)
