"""reconcile_phantom_invoices one-off: flag → void → restore phases."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal

from app.models.billing import Invoice, InvoiceStatus
from app.models.catalog import SubscriptionStatus

recon = importlib.import_module("scripts.one_off.reconcile_phantom_invoices")

_CUTOVER = datetime(2026, 1, 1, tzinfo=UTC)


def _phantom(db, account_id, *, splynx=None, period_start, balance="100.00"):
    inv = Invoice(
        account_id=account_id,
        status=InvoiceStatus.overdue,
        total=Decimal(balance),
        balance_due=Decimal(balance),
        splynx_invoice_id=splynx,
        billing_period_start=period_start,
        is_active=True,
        metadata_={},
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


def test_flag_then_void_targets_only_phantoms(
    db_session, subscription, subscriber_account
):
    # Make the subscriber Splynx-origin.
    subscription.splynx_service_id = 10146
    db_session.commit()
    acct = subscriber_account.id

    phantom = _phantom(
        db_session, acct, splynx=None, period_start=datetime(2025, 3, 1, tzinfo=UTC)
    )
    # A real Splynx invoice (has splynx id) and a post-cutover local invoice
    # must be left alone.
    real_splynx = _phantom(
        db_session, acct, splynx=999, period_start=datetime(2025, 3, 1, tzinfo=UTC)
    )
    post_cutover = _phantom(
        db_session, acct, splynx=None, period_start=datetime(2026, 6, 1, tzinfo=UTC)
    )

    candidates = recon._candidate_query(db_session, _CUTOVER).all()
    assert {c.id for c in candidates} == {phantom.id}

    from collections import Counter

    recon._phase_flag(db_session, candidates, apply=True, stats=Counter())
    db_session.refresh(phantom)
    assert phantom.metadata_.get("reconciliation_hold") is True

    candidates = recon._candidate_query(db_session, _CUTOVER).all()
    recon._phase_void(db_session, candidates, apply=True, stats=Counter())

    db_session.refresh(phantom)
    db_session.refresh(real_splynx)
    db_session.refresh(post_cutover)
    assert phantom.status == InvoiceStatus.void
    assert phantom.balance_due == Decimal("0.00")
    assert real_splynx.status == InvoiceStatus.overdue  # untouched
    assert post_cutover.status == InvoiceStatus.overdue  # untouched


def test_restore_only_when_no_real_debt(db_session, subscription, subscriber_account):
    from collections import Counter

    subscription.splynx_service_id = 10146
    subscription.status = SubscriptionStatus.blocked
    db_session.commit()

    # Only phantom debt → eligible for restore once voided.
    recon._phase_restore(db_session, _CUTOVER, apply=False, stats=(s := Counter()))
    assert s["to_restore"] == 1

    # Add a real (post-cutover) open invoice → no longer eligible.
    _phantom(
        db_session,
        subscriber_account.id,
        splynx=None,
        period_start=datetime(2026, 6, 1, tzinfo=UTC),
    )
    recon._phase_restore(db_session, _CUTOVER, apply=False, stats=(s2 := Counter()))
    assert s2["to_restore"] == 0
    assert s2["restore_skipped_real_debt"] == 1
