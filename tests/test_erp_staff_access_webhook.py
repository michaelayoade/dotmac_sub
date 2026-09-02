from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api import erp_staff_access_webhooks
from app.services.integrations.backoffice_contracts import (
    ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
)


class _Request:
    def __init__(self, body: bytes, *, secret: str, delivery_id: str) -> None:
        self._body = body
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.headers = {
            "content-type": "application/json",
            "X-Dotmac-Delivery": delivery_id,
            "X-Dotmac-Signature": f"sha256={signature}",
        }

    async def body(self) -> bytes:
        return self._body


def _leave_payload(user_id: UUID) -> bytes:
    return json.dumps(
        {
            "event_type": "staff.leave_restriction.v1",
            "leave_restriction": {
                "event_id": "leave-event-1",
                "restriction_id": "leave-1",
                "erp_employee_id": "employee-1",
                "system_user_id": str(user_id),
                "effective_from": "2026-01-10T00:00:00Z",
                "effective_until": "2026-01-15T00:00:00Z",
                "status": "active",
                "version": 1,
                "updated_at": "2026-01-10T09:00:00Z",
            },
        }
    ).encode()


def test_staff_access_webhook_rejects_invalid_signature_before_claim(
    db_session,
    monkeypatch,
) -> None:
    binding_id = uuid4()
    monkeypatch.setattr(
        erp_staff_access_webhooks,
        "build_execution_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            binding=SimpleNamespace(
                capability_id=ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
                installation_id=uuid4(),
            ),
            secret_material={"webhook_signing_secret": "expected-secret"},
        ),
    )
    monkeypatch.setattr(
        erp_staff_access_webhooks.integration_inbox,
        "receive_and_claim_verified",
        lambda *_args, **_kwargs: pytest.fail("invalid delivery was claimed"),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            erp_staff_access_webhooks.receive_erp_staff_access(
                binding_id,
                _Request(
                    _leave_payload(uuid4()),
                    secret="wrong-secret",
                    delivery_id="invalid-signature",
                ),
                db_session,
            )
        )

    assert exc_info.value.status_code == 401


def test_processed_staff_access_delivery_replays_without_second_owner_command(
    db_session,
    monkeypatch,
) -> None:
    secret = "test-webhook-secret"
    monkeypatch.setattr(
        erp_staff_access_webhooks,
        "build_execution_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            binding=SimpleNamespace(
                capability_id=ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY,
                installation_id=uuid4(),
            ),
            secret_material={"webhook_signing_secret": secret},
        ),
    )
    monkeypatch.setattr(
        erp_staff_access_webhooks.integration_inbox,
        "receive_and_claim_verified",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                consequence_json={
                    "event_id": "leave-event-1",
                    "event_type": "staff.leave_restriction.v1",
                    "applied": True,
                    "status": "active",
                }
            ),
            False,
        ),
    )
    monkeypatch.setattr(
        erp_staff_access_webhooks.erp_staff_access,
        "apply_staff_leave_restriction_event",
        lambda *_args, **_kwargs: pytest.fail("replay ran owner command"),
    )

    receipt = asyncio.run(
        erp_staff_access_webhooks.receive_erp_staff_access(
            uuid4(),
            _Request(
                _leave_payload(uuid4()),
                secret=secret,
                delivery_id="already-processed",
            ),
            db_session,
        )
    )

    assert receipt.event_id == "leave-event-1"
    assert receipt.event_type == "staff.leave_restriction.v1"
    assert receipt.applied is True
    assert receipt.replayed is True


def test_staff_access_webhook_enters_owner_command_after_inbox_commit(
    db_session,
    monkeypatch,
) -> None:
    secret = "test-webhook-secret"
    binding_id = uuid4()
    installation_id = uuid4()
    receipt_id = uuid4()
    claimed = False

    class _Binding:
        capability_id = ERP_STAFF_ACCESS_WEBHOOK_CAPABILITY

        @property
        def installation_id(self) -> UUID:
            if claimed:
                db_session.execute(text("SELECT 1"))
            return installation_id

    monkeypatch.setattr(
        erp_staff_access_webhooks,
        "build_execution_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            binding=_Binding(),
            secret_material={"webhook_signing_secret": secret},
        ),
    )

    def receive_and_claim(*_args, **_kwargs):
        nonlocal claimed
        db_session.commit()
        claimed = True
        return SimpleNamespace(id=receipt_id, consequence_json={}), True

    monkeypatch.setattr(
        erp_staff_access_webhooks.integration_inbox,
        "receive_and_claim_verified",
        receive_and_claim,
    )

    def apply_leave(db, command):
        assert not db.in_transaction()
        assert command.delivery_id == "stable-delivery"
        return SimpleNamespace(
            event_id=command.event.event_id,
            applied=True,
            status="active",
        )

    monkeypatch.setattr(
        erp_staff_access_webhooks.erp_staff_access,
        "apply_staff_leave_restriction_event",
        apply_leave,
    )
    monkeypatch.setattr(
        erp_staff_access_webhooks.integration_inbox,
        "get_receipt",
        lambda *_args, **_kwargs: SimpleNamespace(id=receipt_id),
    )
    completed: list[dict[str, object]] = []
    monkeypatch.setattr(
        erp_staff_access_webhooks.integration_inbox,
        "complete_consequence",
        lambda *_args, **kwargs: completed.append(kwargs["consequence"]),
    )

    receipt = asyncio.run(
        erp_staff_access_webhooks.receive_erp_staff_access(
            binding_id,
            _Request(
                _leave_payload(uuid4()),
                secret=secret,
                delivery_id="stable-delivery",
            ),
            db_session,
        )
    )

    assert receipt.replayed is False
    assert completed == [
        {
            "event_id": "leave-event-1",
            "event_type": "staff.leave_restriction.v1",
            "applied": True,
            "status": "active",
        }
    ]
