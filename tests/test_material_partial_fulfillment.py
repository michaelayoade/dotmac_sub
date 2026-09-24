"""Cumulative ERP quantities are a projection, never local stock issuance."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.field_material import FieldMaterialRequest, FieldMaterialRequestItem
from app.schemas.erp_material_webhook import ErpMaterialStatusWebhook
from app.services.dotmac_erp import material_sync
from app.services.field import material_requests as owner

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _request():
    def line(sku):
        return cast(
            FieldMaterialRequestItem,
            SimpleNamespace(
                id=uuid4(),
                item_id=uuid4(),
                quantity=5,
                sku_snapshot=sku,
                item=SimpleNamespace(sku=sku),
                metadata_=None,
                serial_numbers=[],
            ),
        )

    # Deliberately different from the ERP sequence order.
    return cast(
        FieldMaterialRequest,
        SimpleNamespace(
            items=[line("B"), line("A")],
            metadata_=None,
            status="pending_stock",
            support_status="pending_stock",
            support_system="dotmac_erp",
            support_reference="ERP-ID",
            work_order_mirror=None,
        ),
    )


def _progress():
    return (
        owner.MaterialLineFulfillment(1, "A", Decimal("5"), Decimal("3")),
        owner.MaterialLineFulfillment(2, "B", Decimal("5"), Decimal("0"), True),
    )


def test_progress_matches_sku_not_relationship_order():
    request = _request()
    plan = owner._fulfillment_plan(request, "partially_issued", _progress(), NOW)
    assert plan is not None
    assert [line.sku_snapshot for line, _ in plan] == ["A", "B"]
    assert [line.quantity for line in request.items] == [5, 5]


def test_unknown_historical_quantity_is_not_zero():
    line = _request().items[0]
    assert owner._issued_quantity(line) is None
    assert owner._outstanding_quantity(line) is None


@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "duplicate",
        "unknown",
        "negative",
        "over",
        "nan",
        "changed_request",
        "false_completed",
        "no_issue",
    ],
)
def test_inconsistent_snapshot_is_rejected_before_any_projection_write(defect):
    request = _request()
    rows = list(_progress())
    status = "partially_issued"
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows[1] = rows[0]
    elif defect == "unknown":
        rows[0] = replace(rows[0], item_code="UNKNOWN")
    elif defect == "negative":
        rows[0] = replace(rows[0], issued_qty=Decimal("-1"))
    elif defect == "over":
        rows[0] = replace(rows[0], issued_qty=Decimal("6"))
    elif defect == "nan":
        rows[0] = replace(rows[0], issued_qty=Decimal("NaN"))
    elif defect == "changed_request":
        rows[0] = replace(rows[0], requested_qty=Decimal("6"))
    elif defect == "false_completed":
        status = "issued"
    elif defect == "no_issue":
        rows[0] = replace(rows[0], issued_qty=Decimal("0"))
    with pytest.raises(owner.MaterialRequestError):
        owner._fulfillment_plan(request, status, tuple(rows), NOW)
    assert all(line.metadata_ is None for line in request.items)


def test_stale_quantity_observation_is_a_noop():
    request = _request()
    request.metadata_ = {"erp_fulfillment_source_at": NOW.isoformat()}
    assert (
        owner._fulfillment_plan(
            request, "partially_issued", _progress(), NOW - timedelta(seconds=1)
        )
        is None
    )


def test_newer_regressing_quantity_requires_reconciliation():
    request = _request()
    request.items[1].metadata_ = {"erp_fulfillment": {"issued_qty": "4"}}
    with pytest.raises(owner.MaterialRequestError, match="regressed"):
        owner._fulfillment_plan(request, "partially_issued", _progress(), NOW)


def test_status_only_acknowledgement_cannot_close_or_cancel_partial_issue():
    request = _request()
    request.items[1].metadata_ = {"erp_fulfillment": {"issued_qty": "3"}}
    request.support_status = "partially_issued"
    assert not owner._can_cancel_request(request)
    for status in ("issued", "cancelled", "submitted", "pending_stock"):
        assert owner._fulfillment_plan(request, status, None, NOW) is None


def test_serial_selection_cannot_be_replaced_by_partial_prefix():
    request = _request()
    request.items[1].serial_numbers = ["1", "2", "3", "4", "5"]
    rows = (replace(_progress()[0], serial_numbers=("1", "2", "3")), _progress()[1])
    with pytest.raises(owner.MaterialRequestError, match="serial selections changed"):
        owner._fulfillment_plan(request, "partially_issued", rows, NOW)


def _wire():
    return {
        "source_request_id": str(uuid4()),
        "request_id": "ERP-ID",
        "request_number": "MR-0001",
        "new_status": "PARTIALLY_ISSUED",
        "fulfillment_version": 1,
        "updated_at": NOW.isoformat(),
        "items": [
            {"sequence": 1, "item_code": "A", "requested_qty": "5", "issued_qty": "3"},
            {
                "sequence": 2,
                "item_code": "B",
                "requested_qty": "5",
                "issued_qty": "0",
                "out_of_stock": True,
            },
        ],
    }


def test_signed_and_polled_snapshots_produce_the_same_typed_quantities():
    payload = ErpMaterialStatusWebhook.model_validate(_wire())
    webhook = material_sync.material_line_progress(
        payload.model_dump(mode="json", exclude_none=True)
    )
    polled = _wire()
    for row in polled["items"]:
        row["ordered_qty"] = row.pop("issued_qty")
    assert material_sync.material_line_progress(polled) == webhook
    assert webhook == _progress()


@pytest.mark.parametrize(
    "defect",
    ["missing_time", "naive_time", "missing_issued", "unknown_version", "over"],
)
def test_wire_snapshot_fails_closed(defect):
    payload = _wire()
    if defect == "missing_time":
        payload.pop("updated_at")
    elif defect == "naive_time":
        payload["updated_at"] = "2026-09-23T12:00:00"
    elif defect == "missing_issued":
        payload["items"][0].pop("issued_qty")
    elif defect == "unknown_version":
        payload["fulfillment_version"] = 2
    elif defect == "over":
        payload["items"][0]["issued_qty"] = "6"
    with pytest.raises(ValidationError):
        ErpMaterialStatusWebhook.model_validate(payload)


def test_legacy_webhook_remains_accepted():
    payload = _wire()
    payload.pop("fulfillment_version")
    payload.pop("updated_at")
    payload["items"] = [{"sequence": 1, "serial_numbers": ["S-1"]}]
    accepted = ErpMaterialStatusWebhook.model_validate(payload)
    assert (
        material_sync.material_line_progress(
            accepted.model_dump(mode="json", exclude_none=True)
        )
        is None
    )


def test_owner_keeps_partial_pending_and_emits_fulfillment_only_once(db_session):
    from app.models.event_store import EventStore
    from app.models.field_material import FieldWorkOrderMaterial
    from app.services.owner_commands import CommandContext
    from tests.test_dotmac_erp_material_sync import _make_approved_request

    request = _make_approved_request(db_session)
    request_id = request.id
    code = request.items[0].item.sku
    db_session.commit()

    def command(quantity, status, offset):
        command_id = uuid4()
        return owner.ObserveErpMaterialStatus(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="integration:test-dotmac-erp",
                scope="erp.material_status.webhook.v1",
                reason="Verify cumulative fulfillment",
                idempotency_key=str(command_id),
            ),
            request_id=request_id,
            provider_request_id="ERP-ID",
            provider_request_number="MR-0001",
            provider_status=status,
            observed_at=NOW + timedelta(seconds=offset),
            line_progress=(
                owner.MaterialLineFulfillment(1, code, Decimal("5"), Decimal(quantity)),
            ),
        )

    partial = owner.observe_erp_material_status(
        db_session, command("3", "PARTIALLY_ISSUED", 0)
    )
    assert partial.status == owner.MaterialRequestStatus.PENDING_STOCK
    assert partial.fulfillment_status == "partially_issued"
    assert partial.items[0].quantity == 5
    assert partial.items[0].issued_quantity == Decimal("3")
    assert partial.items[0].outstanding_quantity == Decimal("2")
    assert not partial.can_cancel
    assert db_session.query(FieldWorkOrderMaterial).count() == 0
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "field_material_request.fulfilled")
        .count()
        == 0
    )
    db_session.commit()
    final = owner.observe_erp_material_status(db_session, command("5", "ISSUED", 1))
    assert final.items[0].outstanding_quantity == Decimal("0")
    db_session.commit()
    owner.observe_erp_material_status(db_session, command("5", "ISSUED", 1))
    assert db_session.query(FieldWorkOrderMaterial).count() == 1
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "field_material_request.fulfilled")
        .count()
        == 1
    )
