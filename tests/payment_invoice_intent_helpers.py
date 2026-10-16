"""Unit-only DTO seam; authoritative module/transaction behavior is tested on PG."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from dotmac_kernel.cache import TenantScope
from dotmac_payments import (
    GetPaymentIntentV1,
    OpenPaymentIntent,
    PaymentIntentObservationV1,
    PaymentIntentStatus,
)
from sqlalchemy.orm import Session

from app.services import gateway_topup_intents as svc
from app.services.operator_tenant import OPERATOR_TENANT_ID


@pytest.fixture(name="invoice_module_stub")
def _invoice_module_stub_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, PaymentIntentObservationV1]:
    """Expose package DTOs to logic tests without claiming package persistence."""
    records: dict[str, PaymentIntentObservationV1] = {}

    def read(
        _db: Session, *, scope: TenantScope, query: GetPaymentIntentV1
    ) -> PaymentIntentObservationV1 | None:
        assert scope.tenant_id == OPERATOR_TENANT_ID
        return records.get(query.reference)

    def open_binding(
        _db: Session, *, scope: TenantScope, command: OpenPaymentIntent
    ) -> None:
        assert scope.tenant_id == OPERATOR_TENANT_ID
        if command.reference not in records:
            records[command.reference] = PaymentIntentObservationV1(
                scope=scope,
                intent_id=uuid4(),
                reference=command.reference,
                payer_reference=command.payer_reference,
                target_reference=command.target_reference,
                purpose=command.purpose,
                requested=command.requested,
                confirmed=None,
                provider_type=command.provider_type.upper(),
                channel=command.channel.upper(),
                status=PaymentIntentStatus.PENDING,
                opened_at=command.opened_at or datetime.now(UTC),
                expires_at=command.expires_at,
                settled_at=None,
            )

    monkeypatch.setattr(svc, "get_payment_intent_v1", read)
    monkeypatch.setattr(svc, "open_payment_intent", open_binding)
    return records
