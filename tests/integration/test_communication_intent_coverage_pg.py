"""PostgreSQL migration and competing source-coverage claims."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models.notification import (
    CommunicationIntentRecipient,
    CommunicationIntentRecord,
    Notification,
    NotificationChannel,
    NotificationIntentCoverage,
    NotificationStatus,
)
from app.services.communication_intents import cover_planned_recipient
from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id


def _tenant_scope(db) -> None:
    db.execute(
        text("SELECT set_config('app.current_tenant', :tenant_id, true)"),
        {"tenant_id": str(operator_tenant_id())},
    )


def test_migrated_coverage_tables_enforce_rls_and_online_grants(engine):
    assert engine.dialect.name == "postgresql"
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       has_table_privilege('app_user', c.oid, 'SELECT')
                         AND has_table_privilege('app_user', c.oid, 'INSERT')
                         AND has_table_privilege('app_user', c.oid, 'UPDATE')
                         AND has_table_privilege('app_user', c.oid, 'DELETE'),
                       has_table_privilege('platform_api', c.oid, 'SELECT')
                         AND has_table_privilege('platform_api', c.oid, 'INSERT')
                         AND has_table_privilege('platform_api', c.oid, 'UPDATE')
                         AND has_table_privilege('platform_api', c.oid, 'DELETE')
                  FROM pg_class c
                 WHERE c.relname IN (
                   'communication_intent_recipients',
                   'notification_intent_coverage'
                 )
                """
            )
        ).all()
    assert len(rows) == 2
    assert all(
        enabled and forced and app_grant and platform_grant
        for _, enabled, forced, app_grant, platform_grant in rows
    )


def test_two_physical_rows_cannot_cover_one_source_recipient(engine):
    assert engine.dialect.name == "postgresql"
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    audience_id = uuid4()
    with factory() as setup:
        _tenant_scope(setup)
        intent = CommunicationIntentRecord(
            event_type="payment_received",
            category="billing",
            communication_class="transactional",
            subject="Receipt",
            body="Receipt body",
            channels=["email"],
            include_reseller=False,
            status="planned",
            suppression_reasons=[],
            metadata_={},
        )
        setup.add(intent)
        setup.flush()
        decision = CommunicationIntentRecipient(
            tenant_id=operator_tenant_id(),
            intent_id=intent.id,
            audience_type="operational",
            audience_id=audience_id,
            channel=NotificationChannel.email,
            recipient="coverage@example.com",
            normalized_recipient="coverage@example.com",
            decision="accepted",
            requested_status=NotificationStatus.queued,
            delivery_latency="normal",
            persist_suppression=False,
            metadata_={},
            created_at=datetime.now(UTC),
        )
        setup.add(decision)
        notifications = [
            Notification(
                audience_type="operational",
                audience_id=audience_id,
                channel=NotificationChannel.email,
                recipient="coverage@example.com",
                category="billing",
                subject="Receipt",
                body="Receipt body",
                status=NotificationStatus.queued,
                metadata_={"communication_class": "transactional"},
            )
            for _ in range(2)
        ]
        setup.add_all(notifications)
        setup.commit()
        decision_id = decision.id
        notification_ids = tuple(row.id for row in notifications)

    barrier = Barrier(2)

    def claim(notification_id):
        with factory() as db:
            _tenant_scope(db)
            barrier.wait(timeout=10)
            try:
                cover_planned_recipient(db, decision_id, notification_id)
                db.commit()
                return "covered"
            except DomainError:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, notification_ids))
    assert sorted(outcomes) == ["conflict", "covered"]
    with factory() as check:
        _tenant_scope(check)
        assert (
            check.query(NotificationIntentCoverage)
            .filter(NotificationIntentCoverage.intent_recipient_id == decision_id)
            .count()
            == 1
        )
