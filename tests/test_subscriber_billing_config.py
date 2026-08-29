import datetime as _dt
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.catalog import BillingMode, SubscriptionStatus
from app.models.subscriber import UserType
from app.schemas.subscriber import SubscriberUpdate
from app.services import customer_tax_policies
from app.services import subscriber as subscriber_service
from app.services import web_customer_actions as web_customer_actions_service
from app.services import web_customer_details as web_customer_details_service
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.subscriber import _apply_billing_defaults
from app.services.web_subscriber_details import build_subscriber_detail_page_context


def test_subscriber_category_is_persisted_in_metadata(db_session, subscriber):
    updated = subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(category="business"),
    )
    assert updated.category.value == "business"
    assert (updated.metadata_ or {}).get("subscriber_category") == "business"


def test_subscriber_detail_includes_billing_config_snapshot(db_session, subscriber):
    metadata = dict(subscriber.metadata_ or {})
    metadata.update(
        {
            "auto_create_invoices": False,
            "send_billing_notifications": True,
        }
    )
    subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(
            billing_day=3,
            payment_due_days=7,
            grace_period_days=2,
            min_balance=Decimal("100.00"),
            metadata_=metadata,
        ),
    )

    context = build_subscriber_detail_page_context(db_session, str(subscriber.id))
    cfg = context["billing_config"]

    assert cfg["billing_day"] == 3
    assert cfg["payment_due_days"] == 7
    assert cfg["auto_create_invoices"] is False
    assert "blocking_period_days" not in cfg
    assert "deactivation_period_days" not in cfg
    assert "next_block_at" not in cfg
    assert "next_block_label" not in cfg


def test_subscriber_detail_marks_paused_billing_notifications(db_session, subscriber):
    metadata = dict(subscriber.metadata_ or {})
    metadata["send_billing_notifications"] = False
    subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(metadata_=metadata),
    )

    context = build_subscriber_detail_page_context(db_session, str(subscriber.id))

    assert context["billing_config"]["send_billing_notifications"] is False


def test_customer_detail_snapshot_includes_billing_notification_preference(
    db_session, subscriber
):
    subscriber.user_type = UserType.customer
    metadata = dict(subscriber.metadata_ or {})
    metadata["send_billing_notifications"] = False
    subscriber_service.subscribers.update(
        db_session,
        subscriber_id=str(subscriber.id),
        payload=SubscriberUpdate(metadata_=metadata),
    )

    context = web_customer_details_service.build_customer_detail_snapshot(
        db_session, str(subscriber.id)
    )

    assert context["billing_config"]["send_billing_notifications"] is False


def test_billing_notification_preference_updates_customer_metadata(
    db_session, subscriber
):
    before, after = web_customer_actions_service.update_billing_notification_preference(
        db_session,
        str(subscriber.id),
        send_billing_notifications=False,
    )

    assert (before.metadata_ or {}).get("send_billing_notifications") is None
    assert (after.metadata_ or {}).get("send_billing_notifications") is False
    assert (after.metadata_ or {}).get(
        "subscriber_category"
    ) == subscriber.category.value


def test_generic_account_update_rejects_mode_change_with_collectible_service(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.prepaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        subscriber_service.subscribers.update(
            db_session,
            str(subscriber_account.id),
            SubscriberUpdate(billing_mode=BillingMode.postpaid),
        )

    assert exc_info.value.status_code == 409
    assert "collectible" in exc_info.value.detail


def test_generic_account_update_can_repair_mode_to_collectible_service(
    db_session, subscriber_account, subscription
):
    subscriber_account.billing_mode = BillingMode.postpaid
    subscription.billing_mode = BillingMode.prepaid
    subscription.status = SubscriptionStatus.active
    db_session.commit()

    updated = subscriber_service.subscribers.update(
        db_session,
        str(subscriber_account.id),
        SubscriberUpdate(billing_mode=BillingMode.prepaid),
    )

    assert updated.billing_mode == BillingMode.prepaid


def test_billing_defaults_do_not_materialize_inherited_grace(
    db_session, subscriber, monkeypatch
):
    subscriber.grace_period_days = None
    monkeypatch.setattr(
        "app.services.subscriber.settings_spec.resolve_value",
        lambda _db, _domain, key: {
            "prepaid_default_billing_day": "1",
            "prepaid_default_payment_due_days": "0",
            "prepaid_default_min_balance": "0",
        }.get(key),
    )

    _apply_billing_defaults(db_session, subscriber)

    assert subscriber.grace_period_days is None


def test_billing_form_preserves_explicit_zero_grace(subscriber):
    subscriber.grace_period_days = 0

    values = web_customer_actions_service.billing_form_defaults(subscriber)

    assert values["grace_period_days"] == "0"


def test_customer_withholding_tax_policy_defaults_disabled(db_session, subscriber):
    policy = customer_tax_policies.get_customer_withholding_tax_policy(
        db_session,
        account_id=subscriber.id,
    )

    assert policy.account_id == subscriber.id
    assert policy.withholding_tax_enabled is False
    assert policy.version == 0


def test_customer_withholding_tax_policy_can_be_enabled_and_disabled(
    db_session,
    subscriber,
):
    # The owner command requires a transaction-free session at entry; the
    # subscriber fixture leaves an open read transaction. Capture the id before
    # releasing — reading subscriber.id afterwards would re-expire the row and
    # reopen a transaction.
    account_id = subscriber.id
    db_session_adapter.release_read_transaction(db_session)
    enabled = customer_tax_policies.set_customer_withholding_tax_policy(
        db_session,
        customer_tax_policies.SetCustomerWithholdingTaxPolicyCommand(
            account_id=account_id,
            withholding_tax_enabled=True,
            updated_by="admin-1",
        ),
        context=CommandContext.system(
            actor="admin-1",
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Enable customer WHT policy",
            idempotency_key=f"enable-customer-wht:{account_id}",
        ),
    )
    db_session_adapter.release_read_transaction(db_session)
    disabled = customer_tax_policies.set_customer_withholding_tax_policy(
        db_session,
        customer_tax_policies.SetCustomerWithholdingTaxPolicyCommand(
            account_id=account_id,
            withholding_tax_enabled=False,
            updated_by="admin-1",
        ),
        context=CommandContext.system(
            actor="admin-1",
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Disable customer WHT policy",
            idempotency_key=f"disable-customer-wht:{account_id}",
        ),
    )

    assert enabled.withholding_tax_enabled is True
    assert enabled.version == 1
    assert disabled.withholding_tax_enabled is False
    assert disabled.version == 2

    persisted = customer_tax_policies.get_customer_withholding_tax_policy(
        db_session,
        account_id=subscriber.id,
    )
    assert persisted.withholding_tax_enabled is False
    assert persisted.version == 2


def test_customer_vat_exemption_defaults_disabled(db_session, subscriber):
    policy = customer_tax_policies.get_customer_vat_exemption_policy(
        db_session,
        account_id=subscriber.id,
    )

    assert policy.account_id == subscriber.id
    assert policy.vat_exempt is False
    assert policy.version == 0


def test_customer_vat_exemption_can_be_enabled_without_enabling_wht(
    db_session,
    subscriber,
):
    account_id = subscriber.id
    db_session_adapter.release_read_transaction(db_session)
    enabled = customer_tax_policies.set_customer_vat_exemption_policy(
        db_session,
        customer_tax_policies.SetCustomerVatExemptionPolicyCommand(
            account_id=account_id,
            vat_exempt=True,
            updated_by="admin-1",
        ),
        context=CommandContext.system(
            actor="admin-1",
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Enable customer VAT exemption",
            idempotency_key=f"enable-customer-vat-exemption:{account_id}",
        ),
    )

    assert enabled.vat_exempt is True
    assert enabled.version == 1
    wht_policy = customer_tax_policies.get_customer_withholding_tax_policy(
        db_session,
        account_id=account_id,
    )
    assert wht_policy.withholding_tax_enabled is False


# --- billing_day must stay inside its declared domain ----------------------
#
# "0 = day of activation" used to write datetime.now(UTC).day straight onto the
# subscriber. On the 29th, 30th or 31st that stores a day OUTSIDE the domain the
# setting spec declares (max_value=28) and the admin customer form enforces
# (max="28"). The consequence was not cosmetic: the browser refused to submit
# that customer's edit form at all, and because the offending control sits in a
# collapsed tab it could not be focused, so Chromium reported only
# "An invalid form control with name='billing_day' is not focusable" to the
# console -- no server error, no message to the admin, the Update button simply
# did nothing. It reached CI as a browser test that had been green on the 28th
# and red from the 29th, on every branch, with no code change in between.


def _billing_day_after_activation(db_session, subscriber, monkeypatch, activation_day):
    """Apply prepaid defaults as if the subscriber activated on a given day."""

    import app.services.subscriber as subscriber_module

    subscriber.billing_day = None
    monkeypatch.setattr(
        "app.services.subscriber.settings_spec.resolve_value",
        lambda _db, _domain, key: {
            "prepaid_default_billing_day": "0",
            "prepaid_default_payment_due_days": "0",
            "prepaid_default_min_balance": "0",
        }.get(key),
    )

    class _FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, activation_day, 12, 0, tzinfo=tz)

    monkeypatch.setattr(subscriber_module, "datetime", _FrozenDatetime)
    _apply_billing_defaults(db_session, subscriber)
    return subscriber.billing_day


@pytest.mark.parametrize("activation_day", [29, 30, 31])
def test_activation_day_past_the_declared_maximum_is_clamped(
    db_session, subscriber, monkeypatch, activation_day
):
    stored = _billing_day_after_activation(
        db_session, subscriber, monkeypatch, activation_day
    )

    assert stored == 28, (
        f"activating on the {activation_day}th stored billing_day={stored}, "
        "outside the 1..28 domain the setting spec declares and the admin "
        "form enforces -- the customer's edit form becomes unsubmittable"
    )


@pytest.mark.parametrize("activation_day", [1, 15, 28])
def test_an_in_domain_activation_day_is_stored_unchanged(
    db_session, subscriber, monkeypatch, activation_day
):
    """The negative half of the control.

    A clamp that flattened every activation day to 28 would pass the test
    above and destroy the feature. The 28th is included deliberately: it is
    the boundary, and it must survive untouched.
    """

    stored = _billing_day_after_activation(
        db_session, subscriber, monkeypatch, activation_day
    )

    assert stored == activation_day


def test_the_clamp_is_read_from_the_setting_spec_not_hardcoded():
    """Writer and validator must share ONE declaration of the domain.

    If someone widens the spec to 31, this fails and forces the writer, the
    admin form's ``max`` and this expectation to be revisited together --
    rather than letting the writer silently keep clamping to a bound the spec
    no longer states.
    """

    from app.models.domain_settings import SettingDomain as _Domain
    from app.services import settings_spec as _spec
    from app.services.subscriber import _declared_max_billing_day

    for key in ("prepaid_default_billing_day", "postpaid_default_billing_day"):
        spec = _spec.get_spec(_Domain.billing, key)
        assert spec is not None, f"{key} is no longer a registered setting"
        assert spec.max_value == 28, (
            f"{key} now declares max_value={spec.max_value}; the admin customer "
            'form still renders max="28" for billing_day. Change both or '
            "neither -- a writer and a validator that disagree about a field's "
            "domain produce a value that is legal to store and impossible to "
            "edit."
        )
        assert _declared_max_billing_day(key) == spec.max_value


def test_the_admin_form_still_constrains_billing_day_to_the_declared_domain():
    """Sensitivity proof for the test above.

    The clamp only matters because the rendered form rejects anything past
    28. If that ``max`` were ever dropped from the template, the tests above
    would keep passing while the constraint they protect had quietly gone.
    """

    from pathlib import Path

    template = Path("templates/admin/customers/form.html").read_text(encoding="utf-8")
    person_field = [
        line
        for line in template.splitlines()
        if 'name="billing_day"' in line and 'id="billing_day"' in line
    ]
    assert person_field, "the person billing_day input is gone from the form"
    assert 'min="1"' in person_field[0] and 'max="28"' in person_field[0]
