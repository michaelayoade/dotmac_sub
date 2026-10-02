"""Migrated PostgreSQL proof for payment email correlation, claims, and cutover.

These tests use a clone at all composed heads, including host revision 638 and
Template Studio's independent lineage. SQLite metadata and an elevated role
cannot prove the row-lock or tenant-isolation contracts exercised here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy import event as sa_event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def payment_engine(cloned_database, monkeypatch):
    from app.tasks import notifications as notification_tasks

    # An after-commit ETA request is part of the queue contract, but a test
    # must not hand a task to an external broker.
    monkeypatch.setattr(
        notification_tasks.deliver_notification, "apply_async", lambda **_kwargs: None
    )
    engine = create_engine(cloned_database("heads"))
    try:
        assert engine.dialect.name == "postgresql"
        yield engine
    finally:
        engine.dispose()


def _scope(db: Session) -> None:
    from app.services.operator_tenant import operator_tenant_id

    db.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"),
        {"tenant": str(operator_tenant_id())},
    )


def _app_user_engine(payment_engine):
    from app.services.operator_tenant import operator_tenant_id

    engine = create_engine(payment_engine.url)

    @sa_event.listens_for(engine, "begin")
    def install_runtime_role(connection):
        # The public owner commands must enter with a transaction-free Session.
        connection.exec_driver_sql("SET LOCAL ROLE app_user")
        connection.execute(
            text("SELECT set_config('app.current_tenant', :tenant, true)"),
            {"tenant": str(operator_tenant_id())},
        )

    return engine


def _assert_runtime_role(db: Session) -> None:
    posture = db.execute(
        text(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = current_user"
        )
    ).one()
    assert posture[0] == "app_user"
    assert not posture.rolsuper and not posture.rolbypassrls


def _assert_migrated_app_user_grants(payment_engine) -> None:
    with payment_engine.connect() as connection:
        for table, privileges in (
            ("notification_templates", ("SELECT", "UPDATE")),
            ("event_store", ("SELECT", "INSERT")),
        ):
            for privilege in privileges:
                assert connection.scalar(
                    text("SELECT has_table_privilege('app_user', :table, :privilege)"),
                    {"table": f"public.{table}", "privilege": privilege},
                ), f"revision 638 must grant app_user {privilege} on {table}"
        for privilege in ("INSERT", "DELETE"):
            assert not connection.scalar(
                text(
                    "SELECT has_table_privilege('app_user', "
                    "'public.notification_templates', :privilege)"
                ),
                {"privilege": privilege},
            ), f"app_user must not have {privilege} on notification_templates"


def _assert_trigger_rejected(connection, statement, parameters, message) -> None:
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(DBAPIError) as failure:
            connection.execute(text(statement), parameters)
        assert failure.value.orig.sqlstate == "P0001"
        assert message in str(failure.value.orig)
    finally:
        savepoint.rollback()


def _seed_and_activate_payment_templates(payment_engine, app_user_engine):
    from app.models.notification import NotificationChannel, NotificationTemplate
    from app.services.operator_tenant import (
        operator_tenant_id,
        provision_operator_tenant,
    )
    from app.services.owner_commands import CommandContext
    from app.services.payment_email_cutover import activate_payment_email_cutover
    from app.services.payment_template_adoption import (
        ReviewedPaymentEmailTemplates,
        adopt_payment_email_templates,
    )
    from app.services.settings_seed import seed_notification_templates

    with Session(payment_engine) as db:
        _scope(db)
        provision_operator_tenant(db)
        # Startup before activation materializes every default. The same
        # seeder must later read these identities without attempting INSERT.
        seed_notification_templates(db)
        receipt = db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.code == "payment_received",
                NotificationTemplate.channel == NotificationChannel.email,
            )
        ).one()
        paid = db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.code == "invoice_paid",
                NotificationTemplate.channel == NotificationChannel.email,
            )
        ).one()
        sms = db.scalars(
            select(NotificationTemplate).where(
                NotificationTemplate.code == "payment_received",
                NotificationTemplate.channel == NotificationChannel.sms,
            )
        ).one()
        receipt.name = "PG receipt"
        receipt.subject = "Receipt {receipt_number}"
        receipt.body = (
            "Hello {subscriber_name}; receipt {receipt_number}: {receipt_url}"
        )
        paid.name = "PG invoice paid"
        paid.subject = "Invoice {invoice_number} paid"
        paid.body = "Hello {subscriber_name}; invoice {invoice_number} paid."
        db.commit()
        reviewed = ReviewedPaymentEmailTemplates(receipt.id, paid.id)
        ids = receipt.id, paid.id, sms.id

    def context():
        return CommandContext.system(
            actor="test:operator",
            scope=str(operator_tenant_id()),
            reason="reviewed PostgreSQL activation proof",
        )

    with Session(app_user_engine) as db:
        assert not db.in_transaction()
        adopted = adopt_payment_email_templates(
            db, context=context(), reviewed=reviewed
        )
        assert {item.code for item in adopted.items} == {
            "payment_received",
            "invoice_paid",
        }
        assert not db.in_transaction()
        assert (
            activate_payment_email_cutover(db, context=context(), reviewed=reviewed)
            == operator_tenant_id()
        )
        assert not db.in_transaction()
        _assert_runtime_role(db)
        before = _payment_template_snapshot(db, ids)
        _seed_without_template_insert(db)
        assert _payment_template_snapshot(db, ids) == before
    return ids


def _payment_template_snapshot(db: Session, ids):
    from app.models.notification import NotificationTemplate

    return tuple(
        db.execute(
            select(
                NotificationTemplate.id,
                NotificationTemplate.code,
                NotificationTemplate.channel,
                NotificationTemplate.subject,
                NotificationTemplate.body,
                NotificationTemplate.is_active,
                NotificationTemplate.studio_content_sealed,
            )
            .where(NotificationTemplate.id.in_(ids))
            .order_by(NotificationTemplate.id)
        ).all()
    )


def _seed_without_template_insert(db: Session) -> None:
    from app.services.settings_seed import seed_notification_templates

    attempts = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().lower().startswith("insert into") and (
            "notification_templates" in statement.lower()
        ):
            attempts.append("notification_templates")

    bind = db.get_bind()
    sa_event.listen(bind, "before_cursor_execute", observe)
    try:
        seed_notification_templates(db)
    finally:
        sa_event.remove(bind, "before_cursor_execute", observe)
    assert attempts == []


def _prepared_sources(engine):
    from app.models.billing import (
        Invoice,
        InvoiceStatus,
        LedgerEntry,
        LedgerEntryType,
        LedgerSource,
        Payment,
        PaymentAllocation,
        PaymentSettlement,
        PaymentSettlementOrigin,
        PaymentStatus,
    )
    from app.models.notification import NotificationChannel
    from app.models.subscriber import Subscriber
    from app.schemas.notification import NotificationDeliveryLatency
    from app.services.communication_intents import CommunicationIntent, plan_intent
    from app.services.operator_tenant import provision_operator_tenant
    from app.services.payment_email_content import (
        PaymentEmailKind,
        PublishedPaymentEmail,
    )
    from app.services.payment_email_episodes import (
        PaymentEmailSource,
        prove_payment_pair,
    )
    from app.services.subscriber import _default_reseller_id

    with Session(engine, expire_on_commit=False) as db:
        _scope(db)
        provision_operator_tenant(db)
        subscriber = Subscriber(
            first_name="Postgres",
            last_name="Payment",
            email=f"payment-{uuid4().hex}@example.test",
            reseller_id=_default_reseller_id(db),
        )
        db.add(subscriber)
        db.flush()
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number=f"PG-{uuid4().hex}",
            status=InvoiceStatus.paid,
            total=Decimal("100.00"),
            balance_due=Decimal("0.00"),
        )
        payment = Payment(
            account_id=subscriber.id,
            amount=Decimal("100.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
        )
        db.add_all((invoice, payment))
        db.flush()
        ledger = LedgerEntry(
            account_id=subscriber.id,
            invoice_id=invoice.id,
            payment_id=payment.id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.payment,
            amount=Decimal("100.00"),
            currency="NGN",
        )
        settlement = PaymentSettlement(
            payment_id=payment.id,
            amount=Decimal("100.00"),
            unallocated_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
        )
        db.add_all((ledger, settlement))
        db.flush()
        allocation = PaymentAllocation(
            payment_id=payment.id,
            invoice_id=invoice.id,
            ledger_entry_id=ledger.id,
            amount=Decimal("100.00"),
        )
        db.add(allocation)
        db.flush()
        pair = prove_payment_pair(
            db,
            payment_id=payment.id,
            invoice_id=invoice.id,
            subscriber_id=subscriber.id,
            causing_allocation_id=allocation.id,
            causing_ledger_entry_id=ledger.id,
        )
        assert pair is not None

        def planned(kind):
            receipt = kind is PaymentEmailKind.receipt
            subject = "Receipt PG-1" if receipt else "Invoice PG-1 paid"
            body = "Receipt PG-1 body" if receipt else "Invoice PG-1 paid body"
            plan = plan_intent(
                db,
                CommunicationIntent(
                    subscriber_id=subscriber.id,
                    event_type="payment_received" if receipt else "invoice_paid",
                    category="billing",
                    subject=subject,
                    body=body,
                    channels=(NotificationChannel.email,),
                    recipients={NotificationChannel.email: subscriber.email},
                    include_reseller=False,
                    delivery_latency=NotificationDeliveryLatency.normal,
                    dedupe_key=f"pg-payment-{kind.value}-{uuid4().hex}",
                ),
            )
            assert len(plan.recipients) == 1 and plan.recipients[0].accepted
            recipient = plan.recipients[0]
            source = PaymentEmailSource(
                pair,
                uuid4(),
                kind,
                PublishedPaymentEmail(uuid4(), 1, subject, body),
            )
            return plan.intent_id, source, recipient

        receipt = planned(PaymentEmailKind.receipt)
        paid = planned(PaymentEmailKind.invoice_paid)
        db.commit()
        return receipt, paid


def test_concurrent_first_sources_share_one_queued_delivery(payment_engine):
    from app.models.notification import Notification, NotificationIntentCoverage
    from app.models.payment_email import PaymentEmailEpisode, PaymentEmailPart
    from app.services.payment_email_episodes import stage_planned_payment_email, utc

    receipt, paid = _prepared_sources(payment_engine)
    factory = sessionmaker(bind=payment_engine, expire_on_commit=False)
    barrier = Barrier(2)

    def stage(source_and_recipient):
        _intent_id, source, recipient = source_and_recipient
        with factory() as db:
            _scope(db)
            barrier.wait(timeout=10)
            notification_id = stage_planned_payment_email(
                db, source=source, recipient=recipient, now=recipient.planned_at
            )
            db.commit()
            return notification_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        notification_ids = tuple(pool.map(stage, (receipt, paid)))
    assert notification_ids[0] == notification_ids[1]
    with factory() as db:
        _scope(db)
        notifications = db.scalars(select(Notification)).all()
        episode = db.scalars(select(PaymentEmailEpisode)).one()
        parts = db.scalars(select(PaymentEmailPart)).all()
        coverages = db.scalars(select(NotificationIntentCoverage)).all()
        assert len(notifications) == 1
        assert len(parts) == len(coverages) == 2
        assert {part.kind for part in parts} == {"receipt", "invoice_paid"}
        assert {coverage.notification_id for coverage in coverages} == {
            notifications[0].id
        }
        first = (
            receipt if notifications[0].communication_intent_id == receipt[0] else paid
        )
        assert utc(episode.created_at) == utc(first[2].planned_at)
        assert utc(episode.deadline_at) == utc(first[2].planned_at) + timedelta(
            seconds=60
        )
        assert notifications[0].send_at is not None
        assert utc(notifications[0].send_at) >= utc(episode.deadline_at)


def test_claimed_notification_blocks_join_and_preserves_first_body(payment_engine):
    from app.models.notification import (
        Notification,
        NotificationIntentCoverage,
        NotificationStatus,
    )
    from app.models.payment_email import PaymentEmailEpisode, PaymentEmailPart
    from app.services.payment_email_episodes import stage_planned_payment_email

    receipt, paid = _prepared_sources(payment_engine)
    factory = sessionmaker(bind=payment_engine, expire_on_commit=False)
    with factory() as db:
        _scope(db)
        first_id = stage_planned_payment_email(
            db, source=receipt[1], recipient=receipt[2], now=receipt[2].planned_at
        )
        assert first_id is not None
        db.commit()
    locked = Event()
    release = Event()
    joining = Event()

    def claim():
        with factory() as db:
            _scope(db)
            first = db.scalar(
                select(Notification)
                .where(Notification.id == first_id)
                .with_for_update()
            )
            assert first is not None
            first.status = NotificationStatus.sending
            db.flush()
            locked.set()
            assert release.wait(timeout=10)
            db.commit()

    def join():
        with factory() as db:
            _scope(db)
            joining.set()
            second_id = stage_planned_payment_email(
                db, source=paid[1], recipient=paid[2], now=paid[2].planned_at
            )
            db.commit()
            return second_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimant = pool.submit(claim)
        assert locked.wait(timeout=10)
        joiner = pool.submit(join)
        try:
            assert joining.wait(timeout=10)
            assert not joiner.done()
        finally:
            release.set()
        claimant.result(timeout=10)
        second_id = joiner.result(timeout=10)
    assert second_id is not None and second_id != first_id
    with factory() as db:
        _scope(db)
        first = db.get(Notification, first_id)
        second = db.get(Notification, second_id)
        assert first.body == receipt[2].body
        assert first.status is NotificationStatus.sending
        assert second.body == paid[2].body
        assert second.status is NotificationStatus.queued
        assert db.scalar(select(func.count()).select_from(Notification)) == 2
        assert db.scalar(select(func.count()).select_from(PaymentEmailEpisode)) == 1
        assert db.scalar(select(func.count()).select_from(PaymentEmailPart)) == 1
        assert (
            db.scalar(select(func.count()).select_from(NotificationIntentCoverage)) == 2
        )


def test_app_user_rls_and_activated_legacy_content_trigger(payment_engine):
    from app.services.operator_tenant import operator_tenant_id
    from app.services.payment_email_episodes import stage_planned_payment_email

    receipt, _paid = _prepared_sources(payment_engine)
    with Session(payment_engine) as db:
        _scope(db)
        notification_id = stage_planned_payment_email(
            db, source=receipt[1], recipient=receipt[2], now=receipt[2].planned_at
        )
        assert notification_id is not None
        db.commit()

    _assert_migrated_app_user_grants(payment_engine)
    app_user_engine = _app_user_engine(payment_engine)
    try:
        receipt_template_id, paid_template_id, sms_id = (
            _seed_and_activate_payment_templates(payment_engine, app_user_engine)
        )
    finally:
        app_user_engine.dispose()

    with payment_engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text("SET LOCAL ROLE app_user"))
            assert connection.scalar(text("SELECT current_user")) == "app_user"
            posture = connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            assert not posture.rolsuper and not posture.rolbypassrls

            def set_tenant(tenant_id):
                connection.execute(
                    text("SELECT set_config('app.current_tenant', :tenant, true)"),
                    {"tenant": str(tenant_id)},
                )

            counts = (
                text("SELECT count(*) FROM payment_email_episodes"),
                text("SELECT count(*) FROM payment_email_parts"),
                text("SELECT count(*) FROM payment_email_cutovers"),
            )
            set_tenant(operator_tenant_id())
            for query in counts:
                assert connection.scalar(query) == 1
            cutover = connection.execute(
                text(
                    "SELECT receipt_legacy_id, invoice_legacy_id FROM payment_email_cutovers"
                )
            ).one()
            assert tuple(cutover) == (receipt_template_id, paid_template_id)
            sealed = (
                connection.execute(
                    text(
                        "SELECT id FROM notification_templates "
                        "WHERE id IN (:receipt, :paid) AND studio_content_sealed"
                    ),
                    {"receipt": receipt_template_id, "paid": paid_template_id},
                )
                .scalars()
                .all()
            )
            assert set(sealed) == {receipt_template_id, paid_template_id}
            set_tenant(uuid4())
            for query in counts:
                assert connection.scalar(query) == 0
            assert (
                connection.execute(
                    text("UPDATE payment_email_episodes SET recipient = 'blocked'")
                ).rowcount
                == 0
            )
            assert (
                connection.execute(
                    text("UPDATE payment_email_parts SET body = 'blocked'")
                ).rowcount
                == 0
            )
            assert (
                connection.execute(
                    text("UPDATE payment_email_cutovers SET activated_by = 'blocked'")
                ).rowcount
                == 0
            )

            _assert_trigger_rejected(
                connection,
                "UPDATE notification_templates SET body = 'changed' WHERE id = :id",
                {"id": receipt_template_id},
                "Payment email content is authored in Template Studio",
            )
            _assert_trigger_rejected(
                connection,
                "UPDATE notification_templates SET studio_content_sealed = false WHERE id = :id",
                {"id": receipt_template_id},
                "Payment email content is authored in Template Studio",
            )
            _assert_trigger_rejected(
                connection,
                "UPDATE notification_templates SET code = 'invoice_paid_email', "
                "channel = 'email' WHERE id = :id",
                {"id": sms_id},
                "Activated payment email routing identity cannot be added or rebound",
            )
            assert (
                connection.execute(
                    text(
                        "UPDATE notification_templates SET is_active = false WHERE id = :id"
                    ),
                    {"id": receipt_template_id},
                ).rowcount
                == 1
            )
            assert (
                connection.execute(
                    text(
                        "UPDATE notification_templates SET body = 'new SMS' WHERE id = :id"
                    ),
                    {"id": sms_id},
                ).rowcount
                == 1
            )
        finally:
            transaction.rollback()

    # These are wider legacy-administration trigger canaries. The clone's
    # fixture role can INSERT/DELETE; app_user intentionally has neither grant.
    # Retain the wrong tenant setting for consistency. Only the app_user phase
    # above proves the seal works while its cutover row is invisible; this
    # broader administration phase does not prove RLS isolation.
    with payment_engine.connect() as fixture_connection:
        transaction = fixture_connection.begin()
        try:
            assert fixture_connection.scalar(text("SELECT current_user")) != "app_user"
            for privilege in ("INSERT", "DELETE"):
                assert fixture_connection.scalar(
                    text(
                        "SELECT has_table_privilege(current_user, "
                        "'public.notification_templates', :privilege)"
                    ),
                    {"privilege": privilege},
                )
            wrong_tenant = str(uuid4())
            fixture_connection.execute(
                text("SELECT set_config('app.current_tenant', :tenant, true)"),
                {"tenant": wrong_tenant},
            )
            assert (
                fixture_connection.scalar(
                    text("SELECT current_setting('app.current_tenant')")
                )
                == wrong_tenant
            )
            _assert_trigger_rejected(
                fixture_connection,
                "DELETE FROM notification_templates WHERE id = :id",
                {"id": receipt_template_id},
                "Activated payment email routing identity cannot be deleted",
            )
            _assert_trigger_rejected(
                fixture_connection,
                "INSERT INTO notification_templates (id, name, code, channel, body, studio_content_sealed) "
                "VALUES (:id, 'alias', 'payment_received_email', 'email', 'alias body', false)",
                {"id": uuid4()},
                "Activated payment email routing identity cannot be added or rebound",
            )
        finally:
            transaction.rollback()


def test_pause_waits_for_inflight_gate_and_fences_the_next_gate(payment_engine):
    from app.services.operator_tenant import operator_tenant_id
    from app.services.owner_commands import CommandContext
    from app.services.payment_email_cutover import (
        composition_enabled,
        pause_payment_email_composition,
    )

    _assert_migrated_app_user_grants(payment_engine)
    app_user_engine = _app_user_engine(payment_engine)
    factory = sessionmaker(bind=app_user_engine, expire_on_commit=False)
    try:
        template_ids = _seed_and_activate_payment_templates(
            payment_engine, app_user_engine
        )
        with factory() as db:
            _assert_runtime_role(db)

        gate_held = Event()
        release_gate = Event()
        pause_started = Event()
        pause_finished = Event()

        def source_work():
            with factory() as db:
                assert composition_enabled(db) is True
                gate_held.set()
                assert release_gate.wait(timeout=15)
                db.commit()

        def public_pause():
            with factory() as db:
                assert not db.in_transaction()
                pause_started.set()
                pause_payment_email_composition(
                    db,
                    context=CommandContext.system(
                        actor="test:operator",
                        scope=str(operator_tenant_id()),
                        reason="fence new payment email collection",
                    ),
                )
                assert not db.in_transaction()
                pause_finished.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            source = pool.submit(source_work)
            assert gate_held.wait(timeout=10)
            pause = pool.submit(public_pause)
            try:
                assert pause_started.wait(timeout=10)
                assert not pause_finished.wait(timeout=0.25)
                assert not pause.done()
            finally:
                release_gate.set()
            source.result(timeout=15)
            pause.result(timeout=15)
        assert pause_finished.is_set()
        with factory() as db:
            assert composition_enabled(db) is False
            before = _payment_template_snapshot(db, template_ids)
            _seed_without_template_insert(db)
            assert _payment_template_snapshot(db, template_ids) == before
        with payment_engine.connect() as connection:
            transaction = connection.begin()
            try:
                _assert_trigger_rejected(
                    connection,
                    "INSERT INTO notification_templates "
                    "(id, name, code, channel, body, studio_content_sealed) "
                    "VALUES (:id, 'alias', 'payment_received_email', "
                    "'email', 'alias body', false)",
                    {"id": uuid4()},
                    "Activated payment email routing identity cannot be added or rebound",
                )
            finally:
                transaction.rollback()
    finally:
        app_user_engine.dispose()
