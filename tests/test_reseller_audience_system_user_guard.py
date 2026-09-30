"""Guard: a SystemUser's identity is never reseller-audience for customer mail.

Knowledge slug ``main-reseller-customer-mail-copy-leak`` (confirmed
2026-07-21): the platform's system-admin mailbox was registered as the sole
active ``ResellerUser`` of the ``Main`` reseller (``code=SPL-1``,
``is_house=False``), so ``_reseller_addresses`` copied every eligible
customer-facing transactional notification to it -- 3,026 unwanted copies in
24 hours in the July sample.

These tests exercise ``_reseller_addresses`` directly (imported the same way
``tests/test_billing_contact_routing.py`` imports its sibling private
``_subscriber_addresses``), because the defect and its fix both live entirely
inside that function's audience-expansion query.
"""

from __future__ import annotations

import uuid

from app.models.notification import NotificationChannel
from app.models.subscriber import Reseller, ResellerUser
from app.models.system_user import SystemUser
from app.services.communication_intents import _reseller_addresses


def _reseller(db_session, *, code=None, is_house=False):
    reseller = Reseller(
        name=f"Reseller {uuid.uuid4().hex[:8]}",
        code=code,
        is_active=True,
        is_house=is_house,
    )
    db_session.add(reseller)
    db_session.flush()
    return reseller


def _system_user(db_session, *, email, is_active=True, person_party_id=None):
    user = SystemUser(
        id=uuid.uuid4(),
        first_name="Platform",
        last_name="Admin",
        email=email,
        is_active=is_active,
        person_party_id=person_party_id,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _reseller_user(
    db_session, reseller, *, email, is_active=True, person_party_id=None
):
    reseller_user = ResellerUser(
        reseller_id=reseller.id,
        email=email,
        is_active=is_active,
        full_name="Reseller Login",
        person_party_id=person_party_id,
    )
    db_session.add(reseller_user)
    db_session.flush()
    return reseller_user


def test_historical_main_reseller_system_admin_row_is_excluded(db_session):
    """The exact shape of the confirmed incident row: Main/SPL-1, active,
    email equal to a registered active SystemUser's email. Before this fix
    this email came back from ``_reseller_addresses`` for every Main
    subscriber's customer notification; it must not, regardless of whether
    the Part A data migration has retired the row in a given database."""
    admin_email = "system-admin@dotmac.example"
    _system_user(db_session, email=admin_email)
    main = _reseller(db_session, code="SPL-1", is_house=False)
    _reseller_user(db_session, main, email=admin_email, is_active=True)

    addresses = _reseller_addresses(db_session, main, NotificationChannel.email)

    assert admin_email not in addresses


def test_a_new_synthetic_system_user_linked_reseller_row_is_excluded_regardless_of_is_house(
    db_session,
):
    """The structural guard, not the historical patch: a brand-new
    ResellerUser this test constructs, under a different reseller entirely
    and even when that reseller IS flagged ``is_house``, is still excluded
    the moment its identity is also a registered active SystemUser."""
    admin_email = f"admin-{uuid.uuid4().hex}@dotmac.example"
    _system_user(db_session, email=admin_email)
    house_reseller = _reseller(db_session, code=None, is_house=True)
    _reseller_user(db_session, house_reseller, email=admin_email, is_active=True)

    addresses = _reseller_addresses(
        db_session, house_reseller, NotificationChannel.email
    )

    assert admin_email not in addresses


def test_an_ordinary_reseller_user_not_linked_to_any_system_user_still_receives_mail(
    db_session,
):
    """Sensitivity/near-miss check: a genuine reseller-portal login with no
    SystemUser identity anywhere near it is unaffected by the guard."""
    reseller = _reseller(db_session, code=None, is_house=False)
    partner_email = f"partner-{uuid.uuid4().hex}@partner.example"
    _reseller_user(db_session, reseller, email=partner_email, is_active=True)

    addresses = _reseller_addresses(db_session, reseller, NotificationChannel.email)

    assert partner_email in addresses


def test_a_reseller_user_matching_an_inactive_system_user_is_not_excluded(
    db_session,
):
    """Near-miss: a *revoked* SystemUser (is_active=False) is not a live
    platform-administrator identity, so a ResellerUser sharing its email is
    not caught by the guard -- the guard targets an active admin account
    misfiled as a reseller login, not a stale email collision."""
    shared_email = f"former-admin-{uuid.uuid4().hex}@dotmac.example"
    _system_user(db_session, email=shared_email, is_active=False)
    reseller = _reseller(db_session, code=None, is_house=False)
    _reseller_user(db_session, reseller, email=shared_email, is_active=True)

    addresses = _reseller_addresses(db_session, reseller, NotificationChannel.email)

    assert shared_email in addresses
