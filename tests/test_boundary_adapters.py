from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4


def test_queue_adapter_delegates_to_enqueue_function() -> None:
    from app.services.queue_adapter import CeleryQueueAdapter, QueueMessage

    calls = []

    def fake_enqueue(task_name, **kwargs):
        calls.append((task_name, kwargs))
        return SimpleNamespace(id="task-123")

    adapter = CeleryQueueAdapter(enqueue_func=fake_enqueue)
    result = adapter.enqueue(
        QueueMessage(
            task_name="app.tasks.billing.run",
            args=("invoice-1",),
            kwargs={"force": True},
            queue="billing",
            correlation_id="corr-1",
            source="unit-test",
        )
    )

    assert result.queued is True
    assert result.task_id == "task-123"
    assert result.queue == "billing"
    assert calls[0][0] == "app.tasks.billing.run"
    assert calls[0][1]["queue"] == "billing"
    assert calls[0][1]["correlation_id"] == "corr-1"


def test_queue_adapter_returns_failure_when_backend_unavailable() -> None:
    from app.services.queue_adapter import CeleryQueueAdapter, QueueMessage

    def unavailable(_task_name, **_kwargs):
        raise RuntimeError("broker unavailable")

    adapter = CeleryQueueAdapter(enqueue_func=unavailable)
    result = adapter.enqueue(
        QueueMessage(
            task_name="app.tasks.billing.run",
            queue="billing",
        )
    )

    assert result.queued is False
    assert result.task_name == "app.tasks.billing.run"
    assert result.queue == "billing"
    assert result.error == "broker unavailable"


def test_adapter_result_base_supports_domain_results() -> None:
    from app.services.adapters.base import AdapterResult, AdapterStatus
    from app.services.network.olt_protocol_adapters import OltOperationResult

    result = OltOperationResult(
        success=True,
        message="created",
        data={"service_port": 401},
        ont_id=7,
    )
    failure = AdapterResult.from_exception(
        RuntimeError("boom"),
        operation="unit test adapter",
    )

    assert result.success is True
    assert result.data["service_port"] == 401
    assert result.ont_id == 7
    assert failure.success is False
    assert failure.status == AdapterStatus.error
    assert failure.error_code == "RuntimeError"


def test_adapter_registry_tracks_named_adapters() -> None:
    from app.services.adapters import AdapterRegistry

    class FakeAdapter:
        name = "fake"

    registry = AdapterRegistry()
    adapter = FakeAdapter()

    assert registry.register(adapter) is adapter
    assert registry.get("fake") is adapter
    assert registry.require("fake") is adapter
    assert registry.names() == ("fake",)


def test_operation_result_converts_to_shared_adapter_result() -> None:
    from app.services.adapters.base import AdapterStatus
    from app.services.network.result_adapter import OperationResult, ResultStatus

    operation = OperationResult.queued(
        "queued",
        data={"operation_id": "op-1"},
    )

    shared = operation.to_adapter_result()
    round_trip = OperationResult.from_adapter_result(shared)

    assert shared.success is True
    assert shared.status == AdapterStatus.queued
    assert shared.data["operation_id"] == "op-1"
    assert round_trip.status == ResultStatus.queued
    assert round_trip.message == "queued"


def test_rate_limiter_adapter_blocks_after_limit_until_window_resets() -> None:
    from app.services.rate_limiter_adapter import (
        InMemoryRateLimiterAdapter,
        RateLimitRule,
    )

    adapter = InMemoryRateLimiterAdapter()
    rule = RateLimitRule(key="olt:1", limit=2, window_seconds=60)
    now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)

    first = adapter.check(rule, now=now)
    second = adapter.check(rule, now=now + timedelta(seconds=1))
    blocked = adapter.check(rule, now=now + timedelta(seconds=2))
    reset = adapter.check(rule, now=now + timedelta(seconds=61))

    assert first.allowed is True
    assert second.allowed is True
    assert blocked.allowed is False
    assert blocked.retry_after_seconds is not None
    assert reset.allowed is True


def test_audit_adapter_builds_audit_payload() -> None:
    from app.models.audit import AuditActorType
    from app.services.audit_adapter import AuditAdapter, AuditRecord

    payload = AuditAdapter().build_payload(
        AuditRecord(
            action="provision",
            entity_type="ont",
            entity_id="ont-1",
            actor_type=AuditActorType.service,
            actor_id="provisioner",
            metadata={"result": "ok"},
            status_code=200,
            request_id="req-1",
        )
    )

    assert payload.action == "provision"
    assert payload.entity_type == "ont"
    assert payload.actor_type == AuditActorType.service
    assert payload.metadata_ == {"result": "ok"}
    assert payload.request_id == "req-1"


def test_billing_adapter_builds_invoice_and_payment_payloads(monkeypatch) -> None:
    from app.models.billing import InvoiceStatus, PaymentStatus
    from app.services.billing_adapter import (
        BillingAdapter,
        InvoiceIntent,
        PaymentIntent,
    )

    account_id = uuid4()
    captured = {}

    class FakeInvoices:
        @staticmethod
        def create(db, payload):
            captured["invoice"] = payload
            return payload

    class FakePayments:
        @staticmethod
        def create(db, payload):
            captured["payment"] = payload
            return payload

    fake_billing = SimpleNamespace(invoices=FakeInvoices(), payments=FakePayments())
    adapter = BillingAdapter(billing_service=fake_billing)
    invoice = adapter.create_invoice(
        None,
        InvoiceIntent(
            account_id=account_id,
            invoice_number="INV-1",
            total=Decimal("150.00"),
            status=InvoiceStatus.issued,
        ),
    )
    payment = adapter.record_payment(
        None,
        PaymentIntent(
            account_id=account_id,
            amount=Decimal("150.00"),
            external_id="GW-1",
            status=PaymentStatus.succeeded,
        ),
    )

    assert invoice.invoice_number == "INV-1"
    assert invoice.balance_due == Decimal("150.00")
    assert payment.external_id == "GW-1"
    assert payment.status == PaymentStatus.succeeded


def test_external_bss_adapter_builds_reference_payload() -> None:
    from app.models.external import ExternalEntityType
    from app.services.external_bss_adapter import (
        ExternalBssAdapter,
        ExternalBssReference,
    )

    connector_id = uuid4()
    entity_id = uuid4()
    payload = ExternalBssAdapter().build_reference_payload(
        ExternalBssReference(
            connector_config_id=connector_id,
            entity_type=ExternalEntityType.subscriber,
            entity_id=entity_id,
            external_id="splynx-123",
            external_url="https://bss.example/customers/123",
            metadata={"source": "splynx"},
        )
    )

    assert payload.connector_config_id == connector_id
    assert payload.entity_type == ExternalEntityType.subscriber
    assert payload.entity_id == entity_id
    assert payload.external_id == "splynx-123"
    assert payload.metadata_ == {"source": "splynx"}
