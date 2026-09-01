"""A customer with a legacy billing day must still be editable.

This is the browser half of the 2026-08-29 incident. A subscriber activated on
the 29th, 30th or 31st of a month was stored with a ``billing_day`` outside the
domain the admin form renders. The control lives in the edit form's collapsed
Billing tab, so Chromium failed constraint validation on an element it could
not focus, refused to submit the WHOLE form, and issued no request at all --
logging only ``An invalid form control with name='billing_day' is not
focusable`` to the console. An admin trying to correct that customer's phone
number pressed Update and watched nothing happen, with nothing in any server
log to explain it.

So the assertion here is deliberately end-to-end and about a DIFFERENT field:
change the phone number, and prove both that the save actually happened and
that the legacy billing day was neither cleared nor silently corrected on the
way through. A markup assertion would not have caught the original defect --
the markup was correct; the stored value was not -- and a server-only test
cannot see a form the browser declines to send.

This spec fails against the unfixed code: the navigation never happens.
"""

from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from tests.playwright.helpers.auth import ensure_person
from tests.playwright.pages.admin.subscriber_form_page import SubscriberFormPage

#: Outside the declared 1..28 domain on purpose. 31 is the value a subscriber
#: activated on the 31st actually received.
LEGACY_BILLING_DAY = 31


@pytest.fixture()
def legacy_billing_day_customer(api_context, admin_token: str, e2e_db) -> dict:
    """A person whose stored billing day predates the domain being enforced.

    Written straight to the database on purpose. The point of the fixture is a
    row that the current rules would refuse to create -- exactly the rows real
    deployments are already carrying -- so seeding it through the API would
    test the wrong thing, and once server-side enforcement lands it would not
    work at all.
    """

    from sqlalchemy import update

    from app.models.subscriber import Subscriber

    email = f"e2e.legacy.billing.{uuid.uuid4().hex[:8]}@example.com"
    person = ensure_person(api_context, admin_token, "Legacy", "BillingDay", email)

    # A Core UPDATE, deliberately, not an ORM assignment: the model guard now
    # refuses to CREATE an out-of-domain value, and rightly so. The row this
    # fixture needs is one that predates that rule -- exactly the rows real
    # deployments are already carrying -- so it has to be written underneath
    # the guard rather than through it.
    result = e2e_db.execute(
        update(Subscriber)
        .where(Subscriber.id == person["id"])
        .values(billing_day=LEGACY_BILLING_DAY)
    )
    assert result.rowcount == 1, "seeded person is not a subscriber row"
    e2e_db.commit()

    return {"id": str(person["id"]), "email": email}


def _stored_billing_day(db, subscriber_id: str) -> int | None:
    from app.models.subscriber import Subscriber

    db.expire_all()
    subscriber = db.get(Subscriber, subscriber_id)
    assert subscriber is not None
    return subscriber.billing_day


class TestLegacyBillingDayCustomerStaysEditable:
    def test_editing_a_phone_number_saves_and_preserves_the_legacy_billing_day(
        self, admin_page: Page, settings, legacy_billing_day_customer, e2e_db
    ):
        subscriber_id = legacy_billing_day_customer["id"]
        assert _stored_billing_day(e2e_db, subscriber_id) == LEGACY_BILLING_DAY

        new_phone = "+2348000000042"
        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_edit(subscriber_id)
        form.expect_loaded()

        admin_page.locator("#phone").fill(new_phone)
        form.submit()

        # The save must actually reach the server. Before the fix the browser
        # never sent the request and this navigation never happened.
        admin_page.wait_for_url(f"**/customers/person/{subscriber_id}")

        assert _stored_billing_day(e2e_db, subscriber_id) == LEGACY_BILLING_DAY, (
            "an unrelated edit rewrote or cleared the legacy billing day; it "
            "must be preserved, because changing it changes when a real "
            "customer is billed"
        )

    def test_the_legacy_value_is_shown_and_not_a_hidden_validation_blocker(
        self, admin_page: Page, settings, legacy_billing_day_customer
    ):
        """The control must render the legacy value without blocking the form.

        The negative half matters more than the positive one: it is not enough
        that the value is displayed, it must also carry no constraint that
        makes the form unsubmittable while the field is out of sight.
        """

        subscriber_id = legacy_billing_day_customer["id"]
        form = SubscriberFormPage(admin_page, settings.base_url)
        form.goto_edit(subscriber_id)
        form.expect_loaded()

        field = admin_page.locator("#billing_day")
        expect(field).to_have_value(str(LEGACY_BILLING_DAY))

        # A `max` below the rendered value is precisely what bricked the form.
        maximum = field.get_attribute("max")
        assert maximum is None or int(maximum) >= LEGACY_BILLING_DAY, (
            f'billing_day renders max="{maximum}" while holding '
            f"{LEGACY_BILLING_DAY}: the browser will refuse to submit this "
            "form and, because the control sits in a collapsed tab, will not "
            "be able to tell the user why"
        )

        assert admin_page.evaluate(
            "() => document.getElementById('billing_day').checkValidity()"
        ), "the legacy billing_day control reports itself invalid"

        assert admin_page.evaluate(
            "() => document.querySelector('form').checkValidity()"
        ), "the edit form is unsubmittable while showing a legacy billing day"
