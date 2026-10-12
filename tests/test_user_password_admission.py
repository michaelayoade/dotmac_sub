"""Synthetic bounded admission tests; no native password or database work."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier, Condition, Event, Lock

import pytest

from app.services import user_passwords as owner


@pytest.fixture
def configuration() -> owner.LimiterConfiguration:
    return owner.LimiterConfiguration(
        k=1,
        maximum_waiters=1,
        admission_timeout=2.0,
        threadpool_tokens=4,
        reserved_tokens=2,
    )


@pytest.mark.parametrize(
    ("k", "maximum_waiters", "timeout", "tokens", "reserved"),
    [
        (0, 0, 1.0, 2, 0),
        (-1, 0, 1.0, 2, 0),
        (True, 0, 1.0, 2, 0),
        (1.5, 0, 1.0, 2, 0),
        (1, -1, 1.0, 2, 0),
        (1, True, 1.0, 2, 0),
        (1, 0, -1.0, 2, 0),
        (1, 0, float("nan"), 2, 0),
        (1, 0, float("inf"), 2, 0),
        (1, 0, float("-inf"), 2, 0),
        (1, 0, True, 2, 0),
        (1, 0, "1", 2, 0),
        (1, 0, 1.0, 0, 0),
        (1, 0, 1.0, True, 0),
        (1, 0, 1.0, 2, -1),
        (1, 0, 1.0, 2, True),
        (1, 1, 1.0, 2, 1),
        (1, 0, 1.0, 2, 3),
    ],
)
def test_invalid_explicit_configuration(
    k: int, maximum_waiters: int, timeout: float, tokens: int, reserved: int
) -> None:
    with pytest.raises(owner.InvalidLimiterConfigurationError) as error:
        owner.LimiterConfiguration(k, maximum_waiters, timeout, tokens, reserved)
    assert error.value.code is owner.PasswordAdmissionErrorCode.INVALID_CONFIGURATION


def test_configuration_has_no_defaults_and_is_immutable(
    configuration: owner.LimiterConfiguration,
) -> None:
    with pytest.raises(TypeError):
        owner.LimiterConfiguration()  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        configuration.k = 2  # type: ignore[misc]
    assert configuration.k + configuration.maximum_waiters == (
        configuration.threadpool_tokens - configuration.reserved_tokens
    )


def test_explicit_zero_waiter_zero_timeout_budget() -> None:
    limiter = owner.NativePasswordAdmission(owner.LimiterConfiguration(1, 0, 0.0, 2, 1))
    with limiter.admit() as outcome:
        assert isinstance(outcome, owner.PasswordAdmissionOutcome)
        assert outcome.queue_wait_seconds >= 0
        with pytest.raises(owner.PasswordAdmissionBusyError) as error:
            with limiter.admit():
                pytest.fail("Saturated admission entered")
        assert error.value.code is owner.PasswordAdmissionErrorCode.SATURATED
    with limiter.admit():
        pass


def test_active_native_scope_capacity_is_bounded(
    configuration: owner.LimiterConfiguration,
) -> None:
    limiter = owner.NativePasswordAdmission(
        replace(configuration, k=2, maximum_waiters=0)
    )
    active = Barrier(3)
    release = Event()

    def occupy() -> owner.PasswordAdmissionOutcome:
        with limiter.admit() as outcome:
            active.wait(timeout=2)
            assert release.wait(timeout=2)
            return outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(occupy) for _ in range(2)]
        try:
            active.wait(timeout=2)
            with pytest.raises(owner.PasswordAdmissionBusyError) as error:
                with limiter.admit():
                    pytest.fail("Exceeded native capacity")
            assert error.value.code is owner.PasswordAdmissionErrorCode.SATURATED
        finally:
            release.set()
        assert all(
            future.result(timeout=2).queue_wait_seconds >= 0 for future in futures
        )
    with limiter.admit():
        pass


class ObservedCondition(Condition):
    def __init__(self, waiting: Event) -> None:
        super().__init__()
        self.waiting = waiting

    def wait(self, timeout: float | None = None) -> bool:
        self.waiting.set()
        return super().wait(timeout)


def test_waiters_are_bounded_and_released_scope_wakes_waiter(
    configuration: owner.LimiterConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = owner.NativePasswordAdmission(configuration)
    waiting = Event()
    monkeypatch.setattr(limiter, "_condition", ObservedCondition(waiting))

    def wait_for_scope() -> owner.PasswordAdmissionOutcome:
        with limiter.admit() as outcome:
            return outcome

    with ThreadPoolExecutor(max_workers=1) as pool:
        with limiter.admit():
            future = pool.submit(wait_for_scope)
            assert waiting.wait(timeout=2)
            with pytest.raises(owner.PasswordAdmissionBusyError) as error:
                with limiter.admit():
                    pytest.fail("Exceeded waiter budget")
            assert error.value.code is owner.PasswordAdmissionErrorCode.SATURATED
        assert future.result(timeout=2).queue_wait_seconds >= 0
    with limiter.admit():
        pass


def test_wait_timeout_returns_waiter_budget(
    configuration: owner.LimiterConfiguration,
) -> None:
    limiter = owner.NativePasswordAdmission(
        replace(configuration, admission_timeout=0.01)
    )
    with limiter.admit():
        for _ in range(2):
            with pytest.raises(owner.PasswordAdmissionBusyError) as error:
                with limiter.admit():
                    pytest.fail("Timed-out admission entered")
            assert error.value.code is owner.PasswordAdmissionErrorCode.TIMED_OUT
    with limiter.admit():
        pass


class ControlledClock:
    def __init__(self) -> None:
        self._now = 100.0
        self._lock = Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class WakeupCondition(Condition):
    def __init__(self, waits: tuple[Event, ...]) -> None:
        super().__init__()
        self._waits = waits
        self._wait_count = 0

    def wait(self, timeout: float | None = None) -> bool:
        if self._wait_count < len(self._waits):
            self._waits[self._wait_count].set()
        self._wait_count += 1
        return super().wait(timeout)


def test_spurious_wakeup_keeps_waiter_out_until_capacity_is_released(
    configuration: owner.LimiterConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = owner.NativePasswordAdmission(configuration)
    clock = ControlledClock()
    first_wait, second_wait = Event(), Event()
    condition = WakeupCondition((first_wait, second_wait))
    monkeypatch.setattr(owner, "time", clock)
    monkeypatch.setattr(limiter, "_condition", condition)

    def wait_for_scope() -> owner.PasswordAdmissionOutcome:
        with limiter.admit() as outcome:
            return outcome

    with ThreadPoolExecutor(max_workers=1) as pool:
        with limiter.admit():
            future = pool.submit(wait_for_scope)
            assert first_wait.wait(timeout=2)
            with condition:
                clock.advance(0.25)
                condition.notify_all()  # Capacity remains occupied.
            assert second_wait.wait(timeout=2)
            assert not future.done()
            clock.advance(0.25)
        assert future.result(timeout=2).queue_wait_seconds == 0.5
    with limiter.admit():
        pass


def test_expired_wakeup_refuses_even_when_capacity_becomes_available(
    configuration: owner.LimiterConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    limiter = owner.NativePasswordAdmission(configuration)
    clock = ControlledClock()
    waiting, entered = Event(), Event()
    condition = WakeupCondition((waiting,))
    monkeypatch.setattr(owner, "time", clock)
    monkeypatch.setattr(limiter, "_condition", condition)

    def wait_for_scope() -> None:
        with limiter.admit():
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with limiter.admit():
            future = pool.submit(wait_for_scope)
            assert waiting.wait(timeout=2)
            with condition:
                clock.advance(configuration.admission_timeout + 1.0)
        with pytest.raises(owner.PasswordAdmissionBusyError) as error:
            future.result(timeout=2)
        assert error.value.code is owner.PasswordAdmissionErrorCode.TIMED_OUT
        assert not entered.is_set()
        # Both the waiter budget and native capacity remain reusable.
        next_waiting = Event()
        monkeypatch.setattr(limiter, "_condition", ObservedCondition(next_waiting))
        with limiter.admit():
            next_future = pool.submit(wait_for_scope)
            assert next_waiting.wait(timeout=2)
        next_future.result(timeout=2)
        assert entered.is_set()


class ScopeCancelled(BaseException):
    """Synchronous cancellation unwinding, without an async/native worker."""


@pytest.mark.parametrize("failure", [RuntimeError, ScopeCancelled])
def test_scope_failure_always_releases_active_budget(
    configuration: owner.LimiterConfiguration, failure: type[BaseException]
) -> None:
    limiter = owner.NativePasswordAdmission(configuration)
    with pytest.raises(failure):
        with limiter.admit():
            raise failure()
    with limiter.admit():
        pass


@pytest.mark.parametrize("failure", [RuntimeError, ScopeCancelled])
def test_wait_failure_always_releases_waiter_budget(
    configuration: owner.LimiterConfiguration,
    failure: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = owner.NativePasswordAdmission(configuration)

    def interrupted_wait(timeout: float | None = None) -> bool:
        raise failure()

    with limiter.admit():
        with monkeypatch.context() as patch:
            patch.setattr(limiter._condition, "wait", interrupted_wait)
            for _ in range(2):
                with pytest.raises(failure):
                    with limiter.admit():
                        pytest.fail("Interrupted wait entered")
    with limiter.admit():
        pass


def test_verify_and_rehash_scopes_are_separate_and_instances_are_local(
    configuration: owner.LimiterConfiguration,
) -> None:
    first = owner.NativePasswordAdmission(configuration)
    second = owner.NativePasswordAdmission(configuration)
    with first.admit() as verified:
        with second.admit():
            pass
    with first.admit() as rehashed:
        pass
    assert verified is not rehashed
    assert first.configuration is configuration
