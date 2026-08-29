"""PostgreSQL contract for immutable Paystack recovery evidence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

TABLE = "paystack_outside_window_recovery_runs"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


def test_paystack_recovery_evidence_schema_is_migration_owned(db_session) -> None:
    inspector = inspect(db_session.get_bind())
    assert inspector.has_table(TABLE)

    indexes = {
        item["name"]: (tuple(item["column_names"]), item["unique"])
        for item in inspector.get_indexes(TABLE)
    }
    assert indexes["uq_paystack_outside_window_recovery_idempotency"] == (
        ("idempotency_key",),
        True,
    )
    assert indexes["ix_paystack_outside_window_recovery_intent_created"] == (
        ("intent_id", "created_at"),
        False,
    )

    foreign_keys = {
        tuple(item["constrained_columns"]): (
            item["referred_table"],
            tuple(item["referred_columns"]),
            item["options"].get("ondelete"),
        )
        for item in inspector.get_foreign_keys(TABLE)
    }
    assert foreign_keys == {
        ("intent_id",): ("topup_intents", ("id",), "RESTRICT"),
        ("payment_id",): ("payments", ("id",), "RESTRICT"),
        ("provider_event_id",): (
            "payment_provider_events",
            ("id",),
            "RESTRICT",
        ),
        ("provider_id",): ("payment_providers", ("id",), "RESTRICT"),
        ("checkout_binding_id",): (
            "integration_capability_bindings",
            ("id",),
            "RESTRICT",
        ),
    }

    checks = {item["name"] for item in inspector.get_check_constraints(TABLE)}
    assert {
        "ck_paystack_outside_window_recovery_provider",
        "ck_paystack_outside_window_recovery_disposition",
        "ck_paystack_outside_window_recovery_money",
    } <= checks

    triggers = db_session.scalars(
        text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'paystack_outside_window_recovery_runs'::regclass "
            "AND NOT tgisinternal"
        )
    ).all()
    assert triggers == ["paystack_outside_window_recovery_runs_append_only"]


def test_paystack_recovery_evidence_accepts_insert_but_refuses_rewrite(
    db_session,
) -> None:
    provider_id = uuid.uuid4()
    intent_id = uuid.uuid4()
    payment_id = uuid.uuid4()
    provider_event_id = uuid.uuid4()
    recovery_run_id = uuid.uuid4()
    reference = f"paystack-recovery-{uuid.uuid4().hex}"

    db_session.execute(
        text(
            "INSERT INTO payment_providers "
            "(id, name, provider_type, is_active, created_at, updated_at) "
            "VALUES (:id, :name, 'paystack', true, :now, :now)"
        ),
        {
            "id": provider_id,
            "name": f"Paystack recovery canary {provider_id}",
            "now": NOW,
        },
    )
    db_session.execute(
        text(
            "INSERT INTO topup_intents "
            "(id, provider_id, reference, provider_type, currency, "
            "requested_amount, status, gateway_reconcile_attempt_count, "
            "gateway_observation_count, created_at, updated_at) "
            "VALUES (:id, :provider_id, :reference, 'paystack', 'NGN', "
            ":amount, 'failed', 1, 1, :now, :now)"
        ),
        {
            "id": intent_id,
            "provider_id": provider_id,
            "reference": reference,
            "amount": Decimal("1000.00"),
            "now": NOW,
        },
    )
    db_session.execute(
        text(
            "INSERT INTO payments "
            "(id, provider_id, amount, refunded_amount, provider_fee, currency, "
            "status, paid_at, auto_allocate_on_settlement, external_id, "
            "is_active, created_at, updated_at) "
            "VALUES (:id, :provider_id, :amount, 0, :fee, 'NGN', 'succeeded', "
            ":now, false, :external_id, true, :now, :now)"
        ),
        {
            "id": payment_id,
            "provider_id": provider_id,
            "amount": Decimal("1000.00"),
            "fee": Decimal("7.50"),
            "external_id": f"paystack:{reference}",
            "now": NOW,
        },
    )
    db_session.execute(
        text(
            "INSERT INTO payment_provider_events "
            "(id, provider_id, payment_id, event_type, external_id, "
            "idempotency_key, source, observation_digest, "
            "observed_payment_status, amount, provider_fee, net_amount, "
            "provider_reference, currency, financial_effect, status, "
            "received_at, processed_at) "
            "VALUES (:id, :provider_id, :payment_id, :event_type, "
            ":external_id, :idempotency_key, 'gateway_reconciliation', "
            ":digest, 'succeeded', :amount, :fee, :net_amount, :reference, "
            "'NGN', 'none', 'processed', :now, :now)"
        ),
        {
            "id": provider_event_id,
            "provider_id": provider_id,
            "payment_id": payment_id,
            "event_type": "topup.outside_window_recovered",
            "external_id": f"event:{reference}",
            "idempotency_key": f"event:{reference}",
            "digest": "a" * 64,
            "amount": Decimal("1000.00"),
            "fee": Decimal("7.50"),
            "net_amount": Decimal("992.50"),
            "reference": reference,
            "now": NOW,
        },
    )

    inserted_id = db_session.scalar(
        text(
            "INSERT INTO paystack_outside_window_recovery_runs "
            "(id, intent_id, payment_id, provider_event_id, provider_id, "
            "checkout_binding_id, idempotency_key, command_fingerprint, "
            "preview_fingerprint, review_reference, provider_type, "
            "provider_reference, external_id, gross_amount, provider_fee, "
            "authorized_net_amount, currency, disposition, command_id, "
            "correlation_id, actor, reason, created_at) "
            "VALUES (:id, :intent_id, :payment_id, :provider_event_id, "
            ":provider_id, NULL, :idempotency_key, :command_fingerprint, "
            ":preview_fingerprint, :review_reference, 'paystack', :reference, "
            ":external_id, :gross_amount, :provider_fee, :net_amount, 'NGN', "
            "'recovered', :command_id, :correlation_id, :actor, :reason, :now) "
            "RETURNING id"
        ),
        {
            "id": recovery_run_id,
            "intent_id": intent_id,
            "payment_id": payment_id,
            "provider_event_id": provider_event_id,
            "provider_id": provider_id,
            "idempotency_key": f"recover:{reference}",
            "command_fingerprint": "b" * 64,
            "preview_fingerprint": "c" * 64,
            "review_reference": "review:pytest",
            "reference": reference,
            "external_id": f"paystack:{reference}",
            "gross_amount": Decimal("1000.00"),
            "provider_fee": Decimal("7.50"),
            "net_amount": Decimal("992.50"),
            "command_id": uuid.uuid4(),
            "correlation_id": uuid.uuid4(),
            "actor": "pytest",
            "reason": "PostgreSQL append-only migration canary",
            "now": NOW,
        },
    )
    assert inserted_id == recovery_run_id

    with pytest.raises(DBAPIError) as update_error:
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "UPDATE paystack_outside_window_recovery_runs "
                    "SET review_reference = 'review:rewritten' WHERE id = :id"
                ),
                {"id": recovery_run_id},
            )
    assert "Paystack recovery evidence is append-only" in str(update_error.value)

    with pytest.raises(DBAPIError) as delete_error:
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "DELETE FROM paystack_outside_window_recovery_runs WHERE id = :id"
                ),
                {"id": recovery_run_id},
            )
    assert "Paystack recovery evidence is append-only" in str(delete_error.value)

    retained = db_session.execute(
        text(
            "SELECT review_reference, disposition "
            "FROM paystack_outside_window_recovery_runs WHERE id = :id"
        ),
        {"id": recovery_run_id},
    ).one()
    assert retained == ("review:pytest", "recovered")
