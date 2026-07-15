from app.services.network.ont_provision_steps import _bootstrap_poll_error_result
from app.tasks.tr069 import _bootstrap_wait_idempotency_key


def test_idle_transaction_timeout_is_retryable() -> None:
    result = _bootstrap_poll_error_result(
        RuntimeError(
            "terminating connection due to idle-in-transaction timeout; "
            "server closed the connection unexpectedly"
        ),
        120_000,
    )

    assert result.success is False
    assert result.waiting is True
    assert result.critical is False
    assert result.data == {"failure_class": "retryable_db_connection"}


def test_other_bootstrap_poll_errors_remain_terminal() -> None:
    result = _bootstrap_poll_error_result(RuntimeError("ACS authentication failed"), 50)

    assert result.success is False
    assert result.waiting is False
    assert result.critical is True
    assert result.data == {"failure_class": "acs_bootstrap_poll_error"}


def test_legacy_bootstrap_retry_attempts_have_distinct_idempotency_keys() -> None:
    first = _bootstrap_wait_idempotency_key("ont-1", "op-1", 0)
    retry = _bootstrap_wait_idempotency_key("ont-1", "op-1", 1)

    assert first == "ont-1:op-1:0:legacy"
    assert retry == "ont-1:op-1:1:legacy"
    assert first != retry
