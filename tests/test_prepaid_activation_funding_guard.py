"""Prepaid activation is refused for accounts the funding quarantine excludes.

Regression for legacy account 25448: created 2025-03-03 (before the legacy
financial handoff), given its first prepaid subscription on 2026-10-07 with no
reviewed baseline or subledger opening, it silently joined the prepaid funding
quarantine and fired ``SubPrepaidFundingQuarantineGrowing``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.customer_subledger import CustomerSubledgerAuthorityCutover
from app.models.event_store import EventStore
from app.models.prepaid_funding import (
    PrepaidActivationFundingOverride,
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.catalog import SubscriptionCreate, SubscriptionUpdate
from app.services import catalog as catalog_service
from app.services.account_lifecycle import activate_subscription
from app.services.billing_mode_transitions import (
    BillingModeTransitionIssue,
    PreviewBillingModeTransitionRequest,
    preview_billing_mode_transition,
)
from app.services.prepaid_activation_funding_guard import (
    OVERRIDE_PERMISSION,
    GrantPrepaidActivationFundingOverrideCommand,
    PrepaidActivationEntryPoint,
    PrepaidActivationFundingError,
    PrepaidActivationFundingQuarantinedError,
    PrepaidFundingQuarantineReason,
    PrepaidFundingRemediationRunbook,
    RevokePrepaidActivationFundingOverrideCommand,
    assess_prepaid_funding_quarantine,
    grant_prepaid_activation_funding_override,
    require_prepaid_activation_funding_admitted,
    revoke_prepaid_activation_funding_override,
)
from app.services.prepaid_funding_reconstruction import (
    prepaid_funding_incomplete_source_account_ids,
)
from app.services.web_prepaid_activation_funding import (
    override_command_context,
    prepaid_funding_quarantine_banner,
    safe_return_path,
)

SUBLEDGER_CUTOVER_AT = datetime(2026, 8, 2, 20, 15, tzinfo=UTC)
LEGACY_CREATED_AT = datetime(2025, 3, 3, 9, 0, tzinfo=UTC)


def _activate_subledger_authority(db) -> None:  # noqa: ANN001
    command_id = uuid4()
    run = BillingCutoverVerificationRun(
        phase="customer_subledger_phase3",
        cohort_name="pytest-activation-guard",
        evidence_schema_version=1,
        policy_version="pytest",
        cutoff_at=SUBLEDGER_CUTOVER_AT,
        observation_started_at=SUBLEDGER_CUTOVER_AT,
        observation_ended_at=SUBLEDGER_CUTOVER_AT,
        cohort_count=0,
        covered_count=0,
        unresolved_count=0,
        ambiguous_count=0,
        unexpected_unlinked_count=0,
        duplicate_count=0,
        shadow_variance_count=0,
        expected_difference_count=0,
        gap_count=0,
        overlap_count=0,
        source_fingerprint="a" * 64,
        result_fingerprint="b" * 64,
        currency_totals={},
        cohort_classification={},
        event_outcomes={},
        code_version="pytest",
        database_schema_version="652",
        idempotency_key=f"pytest-activation-guard-run:{command_id}",
        command_id=command_id,
        correlation_id=command_id,
        actor="pytest:operator",
        reason="Reviewed test cutover",
        operator_approved_by="pytest:operator",
        operator_approved_at=SUBLEDGER_CUTOVER_AT,
        finance_approved_by="pytest:finance",
        finance_approved_at=SUBLEDGER_CUTOVER_AT,
        created_at=SUBLEDGER_CUTOVER_AT,
    )
    db.add(run)
    db.flush()
    cutover_command = uuid4()
    db.add(
        CustomerSubledgerAuthorityCutover(
            verification_run_id=run.id,
            result_fingerprint="e" * 64,
            review_reference="pytest:approved-cutover",
            activated_by="pytest:operator",
            command_id=cutover_command,
            correlation_id=cutover_command,
            cutover_at=SUBLEDGER_CUTOVER_AT,
        )
    )
    db.commit()


@pytest.fixture()
def subledger_authority(db_session):  # noqa: ANN001
    _activate_subledger_authority(db_session)


def _account(
    db,  # noqa: ANN001
    *,
    created_at: datetime = LEGACY_CREATED_AT,
    splynx_customer_id: int | None = None,
) -> Subscriber:
    account = Subscriber(
        first_name="Legacy",
        last_name="Account",
        email=f"legacy-{uuid4().hex}@example.com",
        billing_mode=BillingMode.prepaid,
        billing_enabled=True,
        splynx_customer_id=splynx_customer_id,
        created_at=created_at,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _staff(db) -> SystemUser:  # noqa: ANN001
    user = SystemUser(
        first_name="Billing",
        last_name="Lead",
        email=f"billing-lead-{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    return user


def _create_prepaid(db, account: Subscriber, offer, **extra):  # noqa: ANN001
    return catalog_service.subscriptions.create(
        db,
        SubscriptionCreate(
            account_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.pending,
            billing_mode=BillingMode.prepaid,
            **extra,
        ),
    )


def _grant(db, account: Subscriber, staff: SystemUser, **overrides):  # noqa: ANN001
    values = {
        "permission_granted": True,
        "reason": "Customer moved in today; Finance opening review INC-25448 pending.",
    }
    values.update(overrides)
    account_id, staff_id = account.id, staff.id
    context = override_command_context(
        actor_system_user_id=staff_id,
        account_id=account_id,
        action="grant",
        reason="pytest override",
        idempotency_key=f"pytest-grant:{account_id}:{uuid4()}",
    )
    db.rollback()
    return grant_prepaid_activation_funding_override(
        db,
        GrantPrepaidActivationFundingOverrideCommand(
            context=context,
            account_id=account_id,
            actor_system_user_id=staff_id,
            **values,
        ),
    )


# --- assessment -----------------------------------------------------------


def test_guard_is_inert_before_subledger_authority_activation(
    db_session, catalog_offer
):
    account = _account(db_session)

    assessment = assess_prepaid_funding_quarantine(db_session, account.id)

    assert assessment.funding_incomplete is True
    assert assessment.guard_active is False
    assert assessment.quarantined is False
    assert _create_prepaid(db_session, account, catalog_offer).id is not None


@pytest.mark.parametrize(
    ("created_at", "splynx_id", "reason", "runbook"),
    [
        (
            LEGACY_CREATED_AT,
            25448,
            PrepaidFundingQuarantineReason.migrated_opening_missing,
            PrepaidFundingRemediationRunbook.reviewed_migrated_opening_repair,
        ),
        (
            LEGACY_CREATED_AT,
            None,
            PrepaidFundingQuarantineReason.carried_source_identity_unresolved,
            PrepaidFundingRemediationRunbook.prepaid_funding_audit_restore,
        ),
        (
            datetime(2026, 7, 1, tzinfo=UTC),
            None,
            PrepaidFundingQuarantineReason.native_after_handoff_opening_missing,
            PrepaidFundingRemediationRunbook.native_prepaid_opening_repair,
        ),
    ],
)
def test_assessment_names_reason_and_runbook(
    db_session,
    subledger_authority,
    created_at,
    splynx_id,
    reason,
    runbook,
):
    account = _account(db_session, created_at=created_at, splynx_customer_id=splynx_id)

    assessment = assess_prepaid_funding_quarantine(db_session, account.id)

    assert assessment.quarantined is True
    assert assessment.admitted is False
    assert assessment.reason is reason
    assert assessment.runbook is runbook
    assert assessment.splynx_linked is (splynx_id is not None)
    assert runbook.value in assessment.refusal_message()
    assert OVERRIDE_PERMISSION in assessment.refusal_message()


def test_native_account_created_after_subledger_authority_is_admitted(
    db_session, subledger_authority, catalog_offer
):
    account = _account(db_session, created_at=datetime(2026, 10, 1, tzinfo=UTC))

    assert assess_prepaid_funding_quarantine(db_session, account.id).quarantined is (
        False
    )
    subscription = _create_prepaid(db_session, account, catalog_offer)
    assert subscription.billing_mode == BillingMode.prepaid


def test_account_with_active_reviewed_baseline_is_admitted(
    db_session, subledger_authority, catalog_offer
):
    account = _account(db_session, splynx_customer_id=25448)
    batch = db_session.scalar(select(PrepaidFundingReconstructionBatch))
    db_session.add(
        PrepaidFundingBaseline(
            batch_id=batch.id,
            account_id=account.id,
            currency="NGN",
            amount=Decimal("0.00"),
            position_at=batch.position_at,
            is_active=True,
        )
    )
    db_session.commit()

    assert assess_prepaid_funding_quarantine(db_session, account.id).quarantined is (
        False
    )
    assert _create_prepaid(db_session, account, catalog_offer).id is not None


# --- entry points ---------------------------------------------------------


def test_subscription_create_is_refused_with_runbook(
    db_session, subledger_authority, catalog_offer
):
    account = _account(db_session, splynx_customer_id=25448)

    with pytest.raises(PrepaidActivationFundingQuarantinedError) as raised:
        _create_prepaid(db_session, account, catalog_offer)

    error = raised.value
    assert isinstance(error, ValueError)
    assert error.code.endswith(".funding_quarantined")
    assert error.details["entry_point"] == "subscription_create"
    assert "REVIEWED_MIGRATED_PREPAID_OPENING_REPAIR.md" in error.message
    db_session.rollback()
    assert (
        db_session.scalar(
            select(Subscription).where(Subscription.subscriber_id == account.id)
        )
        is None
    )


def test_active_create_is_refused(db_session, subledger_authority, catalog_offer):
    account = _account(db_session)

    with pytest.raises(PrepaidActivationFundingQuarantinedError):
        catalog_service.subscriptions.create(
            db_session,
            SubscriptionCreate(
                account_id=account.id,
                offer_id=catalog_offer.id,
                status=SubscriptionStatus.active,
                billing_mode=BillingMode.prepaid,
            ),
        )


def test_postpaid_create_is_not_guarded(db_session, subledger_authority, catalog_offer):
    account = _account(db_session)
    account.billing_mode = BillingMode.postpaid
    catalog_offer.billing_mode = BillingMode.postpaid
    db_session.commit()

    subscription = catalog_service.subscriptions.create(
        db_session,
        SubscriptionCreate(
            account_id=account.id,
            offer_id=catalog_offer.id,
            status=SubscriptionStatus.pending,
            billing_mode=BillingMode.postpaid,
        ),
    )

    assert subscription.billing_mode == BillingMode.postpaid


def test_pending_to_active_activation_is_refused(db_session, catalog_offer):
    account = _account(db_session)
    # Created before subledger authority existed, so creation was admitted.
    subscription = _create_prepaid(db_session, account, catalog_offer)
    db_session.commit()
    _activate_subledger_authority(db_session)

    with pytest.raises(PrepaidActivationFundingQuarantinedError) as raised:
        activate_subscription(db_session, str(subscription.id), emit=False)

    assert raised.value.details["entry_point"] == "subscription_activation"
    assert raised.value.details["subscription_id"] == str(subscription.id)
    db_session.rollback()
    db_session.refresh(subscription)
    assert subscription.status == SubscriptionStatus.pending


def test_subscription_update_to_prepaid_is_refused(db_session, catalog_offer):
    account = _account(db_session)
    # A legacy postpaid row created before subledger authority existed.
    subscription = _create_prepaid(db_session, account, catalog_offer)
    subscription.billing_mode = BillingMode.postpaid
    db_session.commit()
    _activate_subledger_authority(db_session)

    with pytest.raises(PrepaidActivationFundingQuarantinedError) as raised:
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(billing_mode=BillingMode.prepaid),
        )

    assert raised.value.details["entry_point"] == "subscription_billing_mode_change"


def test_moving_prepaid_subscription_to_quarantined_account_is_refused(
    db_session, subledger_authority, catalog_offer
):
    native = _account(db_session, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    legacy = _account(db_session)
    subscription = _create_prepaid(db_session, native, catalog_offer)
    db_session.commit()

    with pytest.raises(PrepaidActivationFundingQuarantinedError):
        catalog_service.subscriptions.update(
            db_session,
            str(subscription.id),
            SubscriptionUpdate(account_id=legacy.id),
        )


def test_bulk_provisioning_refuses_quarantined_account(
    db_session, subledger_authority, caplog
):
    from app.models.catalog import (
        AccessType,
        BillingCycle,
        CatalogOffer,
        OfferStatus,
        PlanCategory,
        PriceBasis,
        ServiceType,
    )
    from app.models.subscriber import Reseller, SubscriberStatus
    from app.services import web_provisioning_bulk_activate as bulk_service

    reseller = Reseller(name="Guard Partner", is_active=True)
    db_session.add(reseller)
    db_session.commit()
    account = _account(db_session)
    account.status = SubscriberStatus.suspended
    account.reseller_id = reseller.id
    offer = CatalogOffer(
        name="Guarded Recurring Plan",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_cycle=BillingCycle.monthly,
        plan_category=PlanCategory.recurring,
        status=OfferStatus.active,
        is_active=True,
    )
    db_session.add(offer)
    db_session.commit()

    job = bulk_service.create_job(
        db_session,
        filters=bulk_service.BulkFilters(
            tab="recurring",
            reseller_id=str(reseller.id),
            subscriber_status="suspended",
            pop_site_id=None,
            date_from=None,
            date_to=None,
            custom_attr_key=None,
            custom_attr_value=None,
        ),
        mapping=bulk_service.BulkMapping(
            offer_id=str(offer.id),
            activation_date=None,
            nas_device_id=None,
            ipv4_assignment="dynamic",
            static_ipv4=None,
            mac_address=None,
            login_prefix="grd-",
            login_suffix=None,
            service_password_mode="auto",
            service_password_manual=None,
            skip_active_service_check=False,
            set_subscribers_active=True,
        ),
        actor_id=str(account.id),
    )
    with caplog.at_level("WARNING"):
        result = bulk_service.execute_job(db_session, job_id=str(job["job_id"]))

    assert result["counts"]["failed"] == 1
    refusals = [
        record
        for record in caplog.records
        if record.getMessage() == "prepaid_activation_refused_funding_quarantine"
    ]
    assert [record.entry_point for record in refusals] == [
        "bulk_provisioning_activation"
    ]
    assert (
        db_session.scalar(
            select(Subscription).where(Subscription.subscriber_id == account.id)
        )
        is None
    )


def test_billing_mode_change_to_prepaid_is_blocked_at_preview(
    db_session, subledger_authority
):
    account = _account(db_session)
    account.billing_mode = BillingMode.postpaid
    db_session.commit()

    preview = preview_billing_mode_transition(
        db_session,
        PreviewBillingModeTransitionRequest(
            account_id=account.id, target_mode=BillingMode.prepaid
        ),
    )

    codes = {item.code for item in preview.readiness.blocking_blockers}
    assert BillingModeTransitionIssue.prepaid_funding_quarantined.value in codes
    assert preview.allowed is False


def test_participant_check_names_each_entry_point(db_session, subledger_authority):
    account = _account(db_session)
    for entry_point in PrepaidActivationEntryPoint:
        with pytest.raises(PrepaidActivationFundingQuarantinedError) as raised:
            require_prepaid_activation_funding_admitted(
                db_session, account_id=account.id, entry_point=entry_point
            )
        assert raised.value.details["entry_point"] == entry_point.value


# --- override -------------------------------------------------------------


def test_override_requires_permission_and_reason(db_session, subledger_authority):
    account = _account(db_session)
    staff = _staff(db_session)

    with pytest.raises(PrepaidActivationFundingError) as denied:
        _grant(db_session, account, staff, permission_granted=False)
    assert denied.value.code.endswith(".permission_denied")

    with pytest.raises(PrepaidActivationFundingError) as short:
        _grant(db_session, account, staff, reason="urgent")
    assert short.value.code.endswith(".invalid_reason")

    db_session.rollback()
    assert db_session.scalar(select(PrepaidActivationFundingOverride)) is None


def test_override_requires_active_staff_actor(db_session, subledger_authority):
    account = _account(db_session)
    staff = _staff(db_session)
    staff.is_active = False
    db_session.commit()

    with pytest.raises(PrepaidActivationFundingError) as raised:
        _grant(db_session, account, staff)

    assert raised.value.code.endswith(".actor_unavailable")


def test_override_is_refused_when_account_is_not_quarantined(
    db_session, subledger_authority
):
    account = _account(db_session, created_at=datetime(2026, 10, 1, tzinfo=UTC))
    staff = _staff(db_session)

    with pytest.raises(PrepaidActivationFundingError) as raised:
        _grant(db_session, account, staff)

    assert raised.value.code.endswith(".not_quarantined")


def test_override_is_audited_admits_activation_and_keeps_quarantine(
    db_session, subledger_authority, catalog_offer
):
    account = _account(db_session, splynx_customer_id=25448)
    staff = _staff(db_session)
    quarantine_before = prepaid_funding_incomplete_source_account_ids(
        db_session, [account.id]
    )

    outcome = _grant(db_session, account, staff)

    row = db_session.get(PrepaidActivationFundingOverride, outcome.override_id)
    assert row is not None
    assert row.granted_by_system_user_id == staff.id
    assert row.granted_by == f"user:{staff.id}"
    assert row.quarantine_reason == "migrated_opening_missing"
    grant_audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "prepaid_activation_funding_override_granted",
            AuditEvent.entity_id == str(account.id),
        )
    )
    assert grant_audit is not None
    assert grant_audit.actor_id == str(staff.id)
    assert (
        db_session.scalar(
            select(EventStore).where(
                EventStore.event_type
                == "billing.prepaid_activation_funding_override.granted"
            )
        )
        is not None
    )

    subscription = _create_prepaid(db_session, account, catalog_offer)
    db_session.commit()
    assert subscription.billing_mode == BillingMode.prepaid
    admitted_audit = db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "prepaid_activation_admitted_by_funding_override",
            AuditEvent.entity_id == str(account.id),
        )
    )
    assert admitted_audit is not None

    # The override never changes the quarantine itself: the account stays
    # excluded from money actions and keeps counting in the quarantine signal.
    assert (
        prepaid_funding_incomplete_source_account_ids(db_session, [account.id])
        == quarantine_before
        == {account.id}
    )
    assessment = assess_prepaid_funding_quarantine(db_session, account.id)
    assert assessment.quarantined is True
    assert assessment.admitted is True


def test_second_override_and_revocation(db_session, subledger_authority, catalog_offer):
    account = _account(db_session)
    staff = _staff(db_session)
    _grant(db_session, account, staff)

    with pytest.raises(PrepaidActivationFundingError) as duplicate:
        _grant(db_session, account, staff)
    assert duplicate.value.code.endswith(".override_already_active")

    account_id, staff_id = account.id, staff.id
    command = RevokePrepaidActivationFundingOverrideCommand(
        context=override_command_context(
            actor_system_user_id=staff_id,
            account_id=account_id,
            action="revoke",
            reason="pytest revoke",
        ),
        account_id=account_id,
        actor_system_user_id=staff_id,
        permission_granted=True,
        reason="Opening review rejected the urgent activation.",
    )
    db_session.rollback()
    revoke_prepaid_activation_funding_override(db_session, command)

    assert assess_prepaid_funding_quarantine(db_session, account.id).override is None
    with pytest.raises(PrepaidActivationFundingQuarantinedError):
        _create_prepaid(db_session, account, catalog_offer)


def test_grant_replay_returns_the_stored_override(db_session, subledger_authority):
    account = _account(db_session)
    staff = _staff(db_session)
    context = override_command_context(
        actor_system_user_id=staff.id,
        account_id=account.id,
        action="grant",
        reason="pytest override",
        idempotency_key="pytest-grant-replay",
    )
    command = GrantPrepaidActivationFundingOverrideCommand(
        context=context,
        account_id=account.id,
        actor_system_user_id=staff.id,
        permission_granted=True,
        reason="Urgent install for a hospital customer; opening review pending.",
    )
    db_session.rollback()

    first = grant_prepaid_activation_funding_override(db_session, command)
    second = grant_prepaid_activation_funding_override(db_session, command)

    assert first.replayed is False
    assert second.replayed is True
    assert second.override_id == first.override_id


# --- banner ---------------------------------------------------------------


def _render_banner(context: dict) -> str:
    templates = Jinja2Templates(directory="templates")
    request = SimpleNamespace(
        query_params={}, state=SimpleNamespace(csrf_token="csrf-test")
    )
    template = templates.env.get_template(
        "admin/partials/_prepaid_funding_quarantine_banner.html"
    )
    return template.render(request=request, **context)


def test_banner_renders_reason_runbook_and_override_form(
    db_session, subledger_authority
):
    account = _account(db_session, splynx_customer_id=25448)

    banner = prepaid_funding_quarantine_banner(
        db_session, account.id, can_override=True
    )
    html = _render_banner(
        {
            "prepaid_funding_quarantine": banner,
            "prepaid_funding_return_to": f"/admin/customers/person/{account.id}",
        }
    )

    assert banner is not None
    assert banner.prepaid_exposure is True
    assert 'data-testid="prepaid-funding-quarantine-banner"' in html
    assert 'data-quarantine-reason="migrated_opening_missing"' in html
    assert "docs/runbooks/REVIEWED_MIGRATED_PREPAID_OPENING_REPAIR.md" in html
    assert 'data-testid="prepaid-funding-override-form"' in html
    assert f"/admin/customers/accounts/{account.id}/prepaid-activation-override" in (
        html
    )


def test_banner_hides_override_form_without_permission(db_session, subledger_authority):
    account = _account(db_session)

    banner = prepaid_funding_quarantine_banner(
        db_session, account.id, can_override=False
    )
    html = _render_banner(
        {"prepaid_funding_quarantine": banner, "prepaid_funding_return_to": "/admin/"}
    )

    assert "PREPAID_FUNDING_AUDIT_RESTORE.md" in html
    assert "prepaid-funding-override-form" not in html


def test_banner_shows_active_override(db_session, subledger_authority):
    account = _account(db_session)
    staff = _staff(db_session)
    _grant(db_session, account, staff)

    banner = prepaid_funding_quarantine_banner(
        db_session, account.id, can_override=True
    )
    html = _render_banner(
        {"prepaid_funding_quarantine": banner, "prepaid_funding_return_to": "/admin/"}
    )

    assert banner is not None and banner.override_active is True
    assert 'data-testid="prepaid-funding-override-active"' in html
    assert "Revoke override" in html


def test_no_banner_for_unquarantined_account(db_session, subledger_authority):
    account = _account(db_session, created_at=datetime(2026, 10, 1, tzinfo=UTC))

    assert (
        prepaid_funding_quarantine_banner(db_session, account.id, can_override=True)
        is None
    )
    assert "prepaid-funding-quarantine-banner" not in _render_banner(
        {"prepaid_funding_quarantine": None, "prepaid_funding_return_to": "/admin/"}
    )


def test_return_path_rejects_offsite_redirects():
    fallback = "/admin/customers/person/x"
    assert safe_return_path("/admin/catalog/subscriptions/1", fallback=fallback) == (
        "/admin/catalog/subscriptions/1"
    )
    assert safe_return_path("https://evil.example/", fallback=fallback) == fallback
    assert safe_return_path("//evil.example/admin/", fallback=fallback) == fallback
    assert safe_return_path(None, fallback=fallback) == fallback


# --- admin route adapter --------------------------------------------------


def _route_request(auth: dict) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


def test_override_route_requires_staff_principal(db_session, subledger_authority):
    from fastapi import HTTPException

    from app.web.admin import prepaid_activation_funding as routes

    account = _account(db_session)
    with pytest.raises(HTTPException) as raised:
        routes.grant_prepaid_activation_override(
            request=_route_request(
                {"principal_type": "api_key", "principal_id": str(uuid4())}
            ),
            account_id=account.id,
            reason="Reason long enough for the owner.",
            confirmed="yes",
            return_to=None,
            db=db_session,
        )
    assert raised.value.status_code == 403


def test_override_route_records_decision_and_redirects(
    db_session, subledger_authority, monkeypatch
):
    from app.web.admin import prepaid_activation_funding as routes

    account = _account(db_session)
    staff = _staff(db_session)
    account_id, staff_id = account.id, staff.id
    checked: list[str] = []

    def _has_permission(auth, db, key):  # noqa: ANN001
        checked.append(key)
        return True

    monkeypatch.setattr(routes, "has_permission", _has_permission)
    auth = {"principal_type": "system_user", "principal_id": str(staff_id)}

    unconfirmed = routes.grant_prepaid_activation_override(
        request=_route_request(auth),
        account_id=account_id,
        reason="Urgent install; Finance opening review INC-25448 pending.",
        confirmed=None,
        return_to=f"/admin/catalog/subscriptions/{uuid4()}",
        db=db_session,
    )
    assert "prepaid_funding_error=" in unconfirmed.headers["location"]

    db_session.rollback()
    response = routes.grant_prepaid_activation_override(
        request=_route_request(auth),
        account_id=account_id,
        reason="Urgent install; Finance opening review INC-25448 pending.",
        confirmed="yes",
        return_to="https://evil.example/",
        db=db_session,
    )

    location = response.headers["location"]
    assert response.status_code == 303
    assert location.startswith(f"/admin/customers/person/{account_id}?")
    assert "prepaid_funding_notice=" in location
    assert checked == [OVERRIDE_PERMISSION]
    row = db_session.scalar(
        select(PrepaidActivationFundingOverride).where(
            PrepaidActivationFundingOverride.account_id == account_id
        )
    )
    assert row is not None
    assert row.granted_by_system_user_id == staff_id
