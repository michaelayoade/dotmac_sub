"""restore_account_services clears prepaid dunning flags (#33).

A payment/top-up that restores the account must immediately clear the prepaid
low-balance / scheduled-deactivation timestamps so a just-paid customer is not
deactivated on a pending timer (instead of waiting for the next sweep).
"""

from datetime import UTC, datetime

from app.services.collections import restore_account_services


def test_restore_clears_prepaid_dunning_flags(db_session, subscriber_account):
    subscriber_account.prepaid_low_balance_at = datetime.now(UTC)
    subscriber_account.prepaid_deactivation_at = datetime.now(UTC)
    db_session.flush()

    restore_account_services(db_session, str(subscriber_account.id))

    # restore_account_services mutates within the caller's transaction (it does
    # not commit), so assert on the session object as the caller would.
    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None


def test_restore_is_noop_when_flags_already_clear(db_session, subscriber_account):
    subscriber_account.prepaid_low_balance_at = None
    subscriber_account.prepaid_deactivation_at = None
    db_session.flush()

    restore_account_services(db_session, str(subscriber_account.id))  # must not raise

    assert subscriber_account.prepaid_low_balance_at is None
    assert subscriber_account.prepaid_deactivation_at is None
