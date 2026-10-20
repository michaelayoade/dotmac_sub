"""S12: required audit evidence for access-imposing actions and settings/secret
writes fails closed. A write whose audit record cannot be written must not
stand."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import (
    domain_settings,
    subscription_lifecycle_commands,
    web_system_secrets,
)
from app.services.subscription_lifecycle import (
    SubscriptionCommandKind,
    SubscriptionCommandOutcomeStatus,
    SubscriptionLifecycleCommand,
    resolve_subscription_lifecycle,
)


def _boom(*_args, **_kwargs):
    raise RuntimeError("audit store unavailable")


def test_secret_write_is_refused_when_the_audit_record_cannot_be_written(monkeypatch):
    written = []
    monkeypatch.setattr(
        "app.services.secrets.read_secret_fields", lambda _path: {"api_key": "old"}
    )
    monkeypatch.setattr(
        "app.services.secrets.write_secret",
        lambda path, fields: written.append((path, fields)) or True,
    )

    def failing_audit(*_args):
        raise web_system_secrets.SecretAuditUnavailable(
            web_system_secrets.AUDIT_UNAVAILABLE_MESSAGE
        )

    saved = web_system_secrets.save_secret(
        "integrations/example", {"field_api_key": "new"}, audit=failing_audit
    )
    created = web_system_secrets.create_secret(
        {"path": "integrations/example2", "key_0": "k", "val_0": "v"},
        audit=failing_audit,
    )
    deleted = web_system_secrets.delete_secret_path(
        "integrations/example", audit=failing_audit
    )

    assert saved["ok"] is False and "not changed" in saved["error"]
    assert created["ok"] is False and "not changed" in created["error"]
    assert "status=error" in deleted
    assert written == []


def test_secret_auditor_records_names_only_and_fails_closed(db_session, monkeypatch):
    from app.models.audit import AuditEvent

    audit = web_system_secrets.secret_write_auditor(db_session, None)
    audit("secret_updated", "integrations/example", ["b_key", "a_key"])

    event = db_session.query(AuditEvent).filter_by(entity_type="secret").one()
    assert event.entity_id == "integrations/example"
    assert event.metadata_["field_names"] == ["a_key", "b_key"]
    assert "value" not in str(event.metadata_)

    monkeypatch.setattr("app.services.audit_helpers.persist_audit_event", _boom)
    with pytest.raises(web_system_secrets.SecretAuditUnavailable):
        audit("secret_updated", "integrations/example", ["a_key"])


def test_setting_write_rolls_back_when_its_audit_row_cannot_be_staged(
    db_session, monkeypatch
):
    monkeypatch.setattr(domain_settings, "stage_audit_event", _boom)
    service = domain_settings.DomainSettings(SettingDomain.audit)

    with pytest.raises(RuntimeError):
        service.upsert_by_key(
            db_session,
            "methods",
            DomainSettingUpdate(value_type=SettingValueType.json, value_json=["POST"]),
        )
    db_session.rollback()

    assert (
        db_session.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.audit)
        .filter(DomainSetting.key == "methods")
        .count()
        == 0
    )


def test_setting_write_stages_audit_without_values(db_session):
    from app.models.audit import AuditEvent

    service = domain_settings.DomainSettings(SettingDomain.audit)
    service.upsert_by_key(
        db_session,
        "methods",
        DomainSettingUpdate(
            value_type=SettingValueType.json, value_json=["POST", "PUT"]
        ),
    )

    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "control.setting_written")
        .one()
    )
    assert event.entity_id == "audit.methods"
    assert event.metadata_["operation"] == "create"
    assert "POST" not in str(event.metadata_)


def _subscription(db_session, subscriber, offer) -> Subscription:
    subscriber.billing_mode = BillingMode.postpaid
    item = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.postpaid,
        start_at=datetime(2026, 7, 1, tzinfo=UTC),
        next_billing_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    db_session.add(item)
    db_session.flush()
    return item


def test_suspension_is_not_applied_when_its_audit_row_cannot_be_staged(
    db_session, subscriber, catalog_offer, monkeypatch
):
    subscription = _subscription(db_session, subscriber, catalog_offer)
    db_session.commit()
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))
    monkeypatch.setattr(subscription_lifecycle_commands, "stage_audit_event", _boom)

    outcome = subscription_lifecycle_commands.execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.suspend,
            source="admin:test",
            reason="operator hold",
            expected_head=reviewed.head,
            idempotency_key="s12-suspend-audit-fail",
        ),
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.failed
    after = resolve_subscription_lifecycle(db_session, str(subscription.id))
    assert after.head == reviewed.head


def test_applied_suspension_audit_is_staged_with_the_change(
    db_session, subscriber, catalog_offer
):
    from app.models.audit import AuditEvent

    subscription = _subscription(db_session, subscriber, catalog_offer)
    db_session.commit()
    reviewed = resolve_subscription_lifecycle(db_session, str(subscription.id))

    outcome = subscription_lifecycle_commands.execute_subscription_command(
        db_session,
        SubscriptionLifecycleCommand(
            subscription_id=str(subscription.id),
            kind=SubscriptionCommandKind.suspend,
            source="admin:test",
            reason="operator hold",
            expected_head=reviewed.head,
            idempotency_key="s12-suspend-audit-ok",
        ),
        actor_id="operator-1",
    )

    assert outcome.status == SubscriptionCommandOutcomeStatus.applied
    events = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.entity_type == "subscription_lifecycle_command")
        .all()
    )
    assert len(events) == 1
    assert events[0].metadata_["status"] == "applied"
    assert events[0].metadata_["current_head"] == outcome.current_head
