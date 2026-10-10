"""Customer edit forms change the captive opt-in only when the field is sent.

Regression: ``POST /admin/customers/{person,business}/{id}/edit`` used to treat
a missing ``captive_redirect_enabled`` field as False, so any edit that did not
carry the checkbox silently cleared the opt-in. Absence now means "unchanged";
the person form pairs its checkbox with a hidden ``false`` so an explicit
uncheck still reaches the server.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import get_db
from app.models.subscriber import Reseller, Subscriber, SubscriberCategory
from app.web.admin import customers as admin_customers

ABSENT = object()
FORM = {"content-type": "application/x-www-form-urlencoded"}


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(admin_customers.router, prefix="/admin")
    app.dependency_overrides[get_db] = lambda: db_session
    for route in admin_customers.router.routes:
        for dependency in getattr(route, "dependencies", ()):
            if dependency.dependency is not None:
                app.dependency_overrides[dependency.dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(db_session):
    # An edit without "managed by reseller" resolves to the House reseller,
    # which migration 116 guarantees on PostgreSQL but SQLite does not seed.
    if not db_session.query(Reseller).filter(Reseller.is_house.is_(True)).first():
        db_session.add(
            Reseller(
                name="House",
                contact_email="house@example.com",
                is_active=True,
                is_house=True,
            )
        )
        db_session.commit()
    with (
        patch("app.web.admin.get_current_user", return_value=None),
        patch("app.web.admin.get_sidebar_stats", return_value={}),
    ):
        yield _client(db_session)


def _business(db_session, *, opted_in: bool) -> Subscriber:
    organization = Subscriber(
        first_name="Acme",
        last_name="Business",
        email="billing@acme-captive.example.com",
        company_name="Acme Corp",
        captive_redirect_enabled=opted_in,
    )
    organization.category = SubscriberCategory.business
    db_session.add(organization)
    db_session.commit()
    return organization


def _opt_in(db_session, subscriber: Subscriber, value: bool) -> None:
    subscriber.captive_redirect_enabled = value
    db_session.commit()


def _person_form(subscriber: Subscriber, captive: object) -> list[tuple[str, str]]:
    form = [
        ("first_name", "Test"),
        ("last_name", "User"),
        ("email", subscriber.email or ""),
        ("gender", "unknown"),
        ("billing_enabled_override", ""),
    ]
    if captive is not ABSENT:
        form.extend(("captive_redirect_enabled", str(item)) for item in captive)
    return form


def _business_form(captive: object) -> list[tuple[str, str]]:
    form = [("name", "Acme Corp")]
    if captive is not ABSENT:
        form.extend(("captive_redirect_enabled", str(item)) for item in captive)
    return form


# Each case: (stored before, submitted field values, expected after).
# ("false",) is the hidden marker alone (checkbox unchecked);
# ("false", "true") is the marker followed by the checked checkbox.
CASES = [
    pytest.param(True, ABSENT, True, id="absent-leaves-true-unchanged"),
    pytest.param(False, ABSENT, False, id="absent-leaves-false-unchanged"),
    pytest.param(True, ("false",), False, id="explicit-unchecked-sets-false"),
    pytest.param(False, ("false", "true"), True, id="checked-sets-true"),
    pytest.param(False, ("true",), True, id="checked-without-marker-sets-true"),
]


@pytest.mark.parametrize(("before", "captive", "after"), CASES)
def test_person_edit_changes_opt_in_only_when_submitted(
    client, db_session, subscriber, before, captive, after
) -> None:
    _opt_in(db_session, subscriber, before)

    response = client.post(
        f"/admin/customers/person/{subscriber.id}/edit",
        content=urlencode(_person_form(subscriber, captive)),
        headers=FORM,
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text[:500]
    db_session.expire_all()
    assert db_session.get(Subscriber, subscriber.id).captive_redirect_enabled is after


@pytest.mark.parametrize(("before", "captive", "after"), CASES)
def test_business_edit_changes_opt_in_only_when_submitted(
    client, db_session, before, captive, after
) -> None:
    organization = _business(db_session, opted_in=before)

    response = client.post(
        f"/admin/customers/business/{organization.id}/edit",
        content=urlencode(_business_form(captive)),
        headers=FORM,
        follow_redirects=False,
    )

    assert response.status_code == 303, response.text[:500]
    db_session.expire_all()
    assert db_session.get(Subscriber, organization.id).captive_redirect_enabled is after


def test_person_form_sends_explicit_false_for_an_unchecked_opt_in() -> None:
    template = Path("templates/admin/customers/form.html").read_text(encoding="utf-8")
    marker = template.index(
        '<input type="hidden" name="captive_redirect_enabled" value="false"'
    )
    checkbox = template.index('<input type="checkbox" name="captive_redirect_enabled"')

    # The hidden marker precedes the checkbox so a checked box (last value)
    # wins, and it is disabled with the checkbox outside the person form.
    assert marker < checkbox
    assert ":disabled=\"customerType !== 'person'\"" in template[marker:checkbox]
