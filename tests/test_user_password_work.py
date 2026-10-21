"""Dormant work contract, using fake native verification only."""

import base64
from asyncio import CancelledError
from dataclasses import fields
from unittest.mock import Mock

import pytest
from argon2.exceptions import VerificationError

from app.services import user_passwords as owner

# Encoded synthetic bytes only; no native hashing is performed.
ENCODED = "$argon2id$v=19$m=32,t=1,p=1$c3ludGhldGljLXNhbHQ$ZmFrZS1kaWdlc3Q"


class Collector:
    def __init__(self, admission: owner.NativePasswordAdmission) -> None:
        self.admission = admission
        self.records: list[owner.NativePasswordObservation] = []
        self.status = owner.NativePasswordCaptureStatus.RECORDED
        self.failure: BaseException | None = None

    def observe(
        self, observation: owner.NativePasswordObservation
    ) -> owner.NativePasswordCaptureStatus:
        # k=1: this proves delivery happens outside the held native scope.
        with self.admission.admit():
            self.records.append(observation)
        if self.failure is not None:
            raise self.failure
        return self.status


@pytest.fixture
def setup_work() -> tuple[
    owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
]:
    admission = owner.NativePasswordAdmission(owner.LimiterConfiguration(1, 0, 0, 2, 1))
    collector = Collector(admission)
    legacy = Mock(return_value=True)
    work = owner.AdmittedArgon2PasswordWork(
        limits=owner.Argon2idVerificationLimits(
            owner.Argon2idParameters(1, 32, 1, 16, 16), 256
        ),
        admission=admission,
        observer=collector,
        legacy_verify=legacy,
    )
    return work, admission, collector, legacy


def test_exact_native_interval_and_scope(
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, admission, collector, legacy = setup_work
    clock = Mock(side_effect=[10.0, 10.25, 11.0, 13.0, 14.0, 14.0])
    monkeypatch.setattr(owner.time, "monotonic", clock)

    def native(stored: bytes, password: bytes, kind: object) -> bool:
        assert stored == ENCODED.encode("ascii")
        assert password == b"private input"
        with pytest.raises(owner.PasswordAdmissionBusyError):
            with admission.admit():
                pytest.fail("capacity released before native completion")
        return True

    # Refusal also reads monotonic: use a clock that explicitly covers it.
    clock.side_effect = [10.0, 10.25, 11.0, 12.0, 13.0, 14.0, 14.0]
    monkeypatch.setattr(owner, "verify_secret", native)
    assert work.verify(owner.UserPasswordWorkRequest("private input", ENCODED)).accepted
    assert collector.records == [
        owner.NativePasswordObservation(
            owner.NativePasswordOperationKind.ARGON2ID_VERIFY, 11.0, 13.0, 0.25
        )
    ]
    legacy.assert_not_called()
    work.require_complete_capture()


@pytest.mark.parametrize(
    "stored",
    [
        "$argon2id$bad",
        "$argon2id$v=19$m=32,t=1,p=1$!!!!!!!!!!!!!!!!!!!$???????????????",
        ENCODED.replace("c3ludGhldGljLXNhbHQ", "c3ludGhldGljLXNhbHQ="),
        ENCODED + "=",
        ENCODED.replace("c3ludGhldGljLXNhbHQ", "c3ludGhldGljLXNhbHR"),
        ENCODED.replace("ZmFrZS1kaWdlc3Q", "ZmFrZS1kaWdlc3R"),
        ENCODED.replace("m=32,t=1,p=1", "t=1,m=32,p=1"),
        ENCODED.replace("m=32,t=1,p=1", "m=32,t=1,p=1,p=1"),
        ENCODED.replace("m=32,t=1,p=1", "m=32,t=1,p=1,"),
        ENCODED.replace("m=32", "m=032"),
        ENCODED.replace("m=32", "m=+32"),
        ENCODED.replace("m=32", "m= 32"),
        ENCODED.replace("v=19", "v=019"),
        ENCODED + "$trailing",
        ENCODED + "\n",
        ENCODED.replace("c3ludGhldGljLXNhbHQ", "A"),
        ENCODED.replace("m=32", "m=64"),
        ENCODED.replace("v=19", "v=16"),
        ENCODED.replace("p=1", "p=2"),
        ENCODED.replace("t=1", "t=2"),
        ENCODED.replace("c3ludGhldGljLXNhbHQ", "c3ludGhldGljLXNhbHQ" * 4),
        ENCODED.replace("ZmFrZS1kaWdlc3Q", "ZmFrZS1kaWdlc3Q" * 4),
        ENCODED + "x" * 256,
    ],
)
def test_rejection_never_admits_or_records(
    stored: str,
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, admission, collector, _ = setup_work
    admit = Mock(side_effect=AssertionError("admission reached"))
    native = Mock(side_effect=AssertionError("native reached"))
    monkeypatch.setattr(admission, "admit", admit)
    monkeypatch.setattr(owner, "verify_secret", native)
    assert not work.verify(owner.UserPasswordWorkRequest("input", stored)).accepted
    admit.assert_not_called()
    native.assert_not_called()
    assert not collector.records


def test_refused_admission_never_calls_native(
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, admission, collector, _ = setup_work
    native = Mock(side_effect=AssertionError("native reached"))
    monkeypatch.setattr(owner, "verify_secret", native)
    with admission.admit(), pytest.raises(owner.PasswordAdmissionBusyError):
        work.verify(owner.UserPasswordWorkRequest("input", ENCODED))
    native.assert_not_called()
    assert not collector.records


@pytest.mark.parametrize(
    "failure",
    [
        VerificationError("private error"),
        RuntimeError("private error"),
        KeyboardInterrupt(),
        CancelledError(),
        SystemExit(),
    ],
)
def test_native_failure_records_releases_and_preserves_error(
    failure: BaseException,
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, admission, collector, _ = setup_work
    monkeypatch.setattr(owner, "verify_secret", Mock(side_effect=failure))
    if isinstance(failure, VerificationError):
        assert not work.verify(owner.UserPasswordWorkRequest("input", ENCODED)).accepted
    else:
        with pytest.raises(type(failure)) as caught:
            work.verify(owner.UserPasswordWorkRequest("input", ENCODED))
        assert caught.value is failure
    assert len(collector.records) == 1
    with admission.admit():
        pass
    work.require_complete_capture()


@pytest.mark.parametrize(
    "failure", [None, RuntimeError("private observer error"), KeyboardInterrupt()]
)
def test_capture_loss_is_sticky_and_preserves_result(
    failure: BaseException | None,
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, _, collector, _ = setup_work
    collector.status = owner.NativePasswordCaptureStatus.INCOMPLETE
    collector.failure = failure
    monkeypatch.setattr(owner, "verify_secret", Mock(return_value=True))
    assert work.verify(owner.UserPasswordWorkRequest("input", ENCODED)).accepted
    collector.status = owner.NativePasswordCaptureStatus.RECORDED
    collector.failure = None
    assert work.verify(owner.UserPasswordWorkRequest("input", ENCODED)).accepted
    with pytest.raises(owner.IncompletePasswordCaptureError):
        work.require_complete_capture()


def test_observer_failure_never_masks_native_error(
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, _, collector, _ = setup_work
    native_error = KeyboardInterrupt()
    collector.failure = SystemExit()
    monkeypatch.setattr(owner, "verify_secret", Mock(side_effect=native_error))
    with pytest.raises(KeyboardInterrupt) as caught:
        work.verify(owner.UserPasswordWorkRequest("input", ENCODED))
    assert caught.value is native_error
    with pytest.raises(owner.IncompletePasswordCaptureError):
        work.require_complete_capture()


def test_compat_and_secret_free_contracts(
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, _, collector, legacy = setup_work
    native = Mock(side_effect=AssertionError("native reached"))
    monkeypatch.setattr(owner, "verify_secret", native)
    for path in (work, owner.LegacyPasswordWork(legacy)):
        assert path.verify(
            owner.UserPasswordWorkRequest("private input", "plain:private stored")
        ).accepted
    assert (
        not owner.LegacyPasswordWork(legacy)
        .verify(owner.UserPasswordWorkRequest("input", ENCODED))
        .accepted
    )
    native.assert_not_called()
    assert not collector.records
    request = owner.UserPasswordWorkRequest("private input", "private stored")
    assert "private" not in repr(request)
    assert {field.name for field in fields(owner.NativePasswordObservation)} == {
        "kind",
        "started_monotonic",
        "ended_monotonic",
        "admission_wait_seconds",
    }


@pytest.mark.parametrize("size", [8, 9, 10, 16])
def test_canonical_base64_padding_lengths_accept_without_hashing(
    size: int,
    setup_work: tuple[
        owner.AdmittedArgon2PasswordWork, owner.NativePasswordAdmission, Collector, Mock
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work, _, collector, _ = setup_work
    encoded = base64.b64encode(bytes([251, 255]) * 8)  # Includes + and / alphabet.
    salt = base64.b64encode((bytes([251, 255]) * 8)[:size]).decode("ascii").rstrip("=")
    digest = encoded.decode("ascii").rstrip("=")
    stored = f"$argon2id$v=19$m=32,t=1,p=1${salt}${digest}"
    native = Mock(return_value=True)
    monkeypatch.setattr(owner, "verify_secret", native)
    assert work.verify(owner.UserPasswordWorkRequest("input", stored)).accepted
    native.assert_called_once()
    assert len(collector.records) == 1
