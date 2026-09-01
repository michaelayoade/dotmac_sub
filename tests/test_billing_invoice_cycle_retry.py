"""Bounded retry behavior for scheduled invoice-cycle database conflicts."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from app.services import billing_automation


class _PostgresFailure(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _operational_error(sqlstate: str) -> OperationalError:
    return OperationalError(
        "SELECT subscriptions FOR UPDATE",
        {},
        _PostgresFailure(sqlstate),
    )


@pytest.mark.parametrize("sqlstate", ["40P01", "40001", "55P03"])
def test_invoice_cycle_retries_only_supported_transient_sqlstates(sqlstate: str):
    session = MagicMock()
    expected = {"subscriptions_billed": 1, "errors": 0}
    transient = _operational_error(sqlstate)

    with (
        patch.object(
            billing_automation,
            "run_invoice_cycle",
            side_effect=[transient, expected],
        ) as run,
        patch("time.sleep") as sleep,
    ):
        result = billing_automation.run_invoice_cycle_with_retry(
            session,
            max_retries=3,
            retry_delay_seconds=2,
        )

    assert result == expected
    assert run.call_count == 2
    session.rollback.assert_called_once_with()
    sleep.assert_called_once_with(2)


def test_invoice_cycle_does_not_retry_non_transient_integrity_failure():
    session = MagicMock()
    failure = IntegrityError(
        "INSERT invoices",
        {},
        _PostgresFailure("23505"),
    )

    with (
        patch.object(
            billing_automation,
            "run_invoice_cycle",
            side_effect=failure,
        ) as run,
        patch("time.sleep") as sleep,
        pytest.raises(IntegrityError),
    ):
        billing_automation.run_invoice_cycle_with_retry(
            session,
            max_retries=3,
            retry_delay_seconds=2,
        )

    run.assert_called_once_with(
        db=session,
        run_at=None,
        billing_cycle=None,
        dry_run=False,
        include_pending=True,
        auto_activate_pending=True,
    )
    session.rollback.assert_called_once_with()
    sleep.assert_not_called()


def test_invoice_cycle_retry_is_bounded_and_backs_off():
    session = MagicMock()
    failure = _operational_error("40P01")

    with (
        patch.object(
            billing_automation,
            "run_invoice_cycle",
            side_effect=[failure, failure, failure],
        ) as run,
        patch("time.sleep") as sleep,
        pytest.raises(OperationalError),
    ):
        billing_automation.run_invoice_cycle_with_retry(
            session,
            max_retries=3,
            retry_delay_seconds=2,
        )

    assert run.call_count == 3
    assert session.rollback.call_count == 3
    assert [call.args for call in sleep.call_args_list] == [(2,), (4,)]
