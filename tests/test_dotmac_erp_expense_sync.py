"""ERP re-home PR 2 — expense-claim flow (map + enqueue + write-back + reconcile).

Everything runs against a MOCKED ERP: the outbox uses a fake client, and the
status reconcile is fed a canned response. The flow is proven end-to-end WITHOUT
a live ERP and, crucially, WITHOUT flipping ownership in prod — tests set
``sync_flow_ownership.expense_claim = sub`` in-test only. The default (crm) path
is asserted to send nothing (the inert guarantee).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_expense import FieldExpenseRequest
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import backoffice
from app.services.backoffice import ExpenseCategoryView
from app.services.db_session_adapter import db_session_adapter
from app.services.dotmac_erp import expense_sync, outbox
from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.field import attachments as attachments_module
from app.services.field import expense_recovery as expense_recovery_module
from app.services.field.attachments import ResolvedExpenseReceiptAttachment
from app.services.field.expense_recovery import (
    PreviewExpenseDeliveryRecovery,
    preview_expense_delivery_recovery,
)
from app.services.field.expense_requests import (
    ApproveFieldExpenseRequest,
    ExpenseErpSyncStatus,
    ExpenseRequestLineInput,
    ExpenseWorkOrderIdentity,
    FieldExpenseRequestError,
    InitiateFieldExpensePayment,
    RecoverExpenseDelivery,
    RejectFieldExpenseRequest,
    SelectedExpenseApprover,
    SubmitFieldExpenseRequest,
    VerifiedExpenseDestinationInput,
    approve_field_expense_request_command,
    initiate_field_expense_payment_command,
    recover_expense_delivery,
    reject_field_expense_request_command,
    submit_field_expense_request_command,
)
from app.services.integrations.backoffice_contracts import ERP_OUTBOX_CAPABILITY
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_erp_capability

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = (
            SyncFlowOwner.sub.value
            if flow.value in sub_flows
            else SyncFlowOwner.crm.value
        )
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _user(db, name: str = "Expense") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db.add(user)
    db.flush()
    return user


def _profile(
    db, user: SystemUser, crm_person_id: str = "crm-expense-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db.add(profile)
    db.flush()
    return profile


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Expense",
        last_name="Customer",
        email=f"expense-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _work_order(db, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-expense"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field expense"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-expense-tech"
        ),
        crm_ticket_id=overrides.pop("crm_ticket_id", "crm-ticket-77"),
        crm_project_id=overrides.pop("crm_project_id", "crm-project-88"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db.add(row)
    db.flush()
    return row


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _items(**overrides):
    item = {
        "category_code": "transport",
        "category_name": "Transport",
        "description": "Bike delivery",
        "amount": "2500.00",
        "expense_date": date.today(),
        "vendor_name": "Rider",
        "receipt_url": None,
        "receipt_attachment_id": None,
        "notes": "Urgent part pickup",
    }
    item.update(overrides)
    return [item]


def _receipt_attachment(
    db, request: FieldExpenseRequest, attachment_id, *, file_name: str
) -> FieldAttachment:
    content = f"receipt:{attachment_id}".encode()
    stored = StoredFile(
        entity_type="field_attachment",
        entity_id=request.work_order_mirror.public_id,
        original_filename=file_name,
        storage_key_or_relative_path=f"attachments/{attachment_id}",
        file_size=len(content),
        content_type="application/pdf",
        checksum=hashlib.sha256(content).hexdigest(),
        storage_provider="s3",
    )
    db.add(stored)
    db.flush()
    attachment = FieldAttachment(
        id=attachment_id,
        work_order_mirror_id=request.work_order_mirror_id,
        stored_file_id=stored.id,
        kind="document",
        file_name=file_name,
        mime_type="application/pdf",
        size_bytes=len(content),
        uploaded_by_person_id=request.requested_by_person_id,
        uploaded_by_system_user_id=request.requested_by_system_user_id,
    )
    db.add(attachment)
    db.flush()
    return attachment


def _make_submitted_request(
    db,
    *,
    crm_work_order_id="wo-exp",
    items: list[dict] | None = None,
) -> FieldExpenseRequest:
    """Create and submit a request through the real domain service."""
    crm_person_id = f"crm-tech-{uuid4().hex[:8]}"
    user = _user(db)
    _profile(db, user, crm_person_id=crm_person_id)
    subscriber = _subscriber(db)
    work_order = _work_order(
        db,
        subscriber,
        crm_work_order_id=crm_work_order_id,
        assigned_to_crm_person_id=crm_person_id,
    )
    request_id = uuid4()
    user_id = user.id
    command = SubmitFieldExpenseRequest(
        context=CommandContext(
            command_id=request_id,
            correlation_id=request_id,
            actor=f"user:{user_id}",
            scope="field:expense_requests:write",
            reason="test expense submission",
            idempotency_key=str(request_id),
        ),
        requester_person_id=user_id,
        work_order=ExpenseWorkOrderIdentity(public_id=work_order.public_id),
        request_id=request_id,
        purpose="Transport for extra drop cable",
        expense_date=date.today(),
        currency="NGN",
        notes="Customer site was missing materials",
        items=tuple(ExpenseRequestLineInput(**item) for item in (items or _items())),
        selected_approver=SelectedExpenseApprover(
            erp_employee_id=uuid4(),
            system_user_id=user.id,
            display_name=user.display_name,
            email=user.email,
        ),
        payment_destination=VerifiedExpenseDestinationInput(
            mode="erp_profile",
            destination_token="verified-expense-destination-token",
            bank_code="058",
            bank_name="Example Bank",
            masked_account_number="******6789",
            verified_beneficiary_name=user.display_name,
            verified_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        ),
    )
    db.commit()
    outcome = submit_field_expense_request_command(db, command)
    return db.get(FieldExpenseRequest, outcome.id)


@pytest.fixture(autouse=True)
def _expense_categories(monkeypatch):
    monkeypatch.setattr(
        "app.services.field.expense_categories.list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=None,
            ),
        ),
    )


def _approve(db, request: FieldExpenseRequest):
    request_id = request.id
    reviewer_id = request.requested_by_system_user_id
    assert reviewer_id is not None
    command_id = uuid4()
    db.commit()
    return approve_field_expense_request_command(
        db=db,
        command=ApproveFieldExpenseRequest(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{reviewer_id}",
                scope="operations:expense_request:write",
                reason=f"approve_expense_request:{request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=request_id,
            reviewer_system_user_id=reviewer_id,
        ),
    )


class _FakeERPClient:
    """Mocked ERP client for outbox + reconcile: canned responses in order."""

    def __init__(
        self,
        post_outcomes=None,
        status_outcomes=None,
        upload_outcomes=None,
    ):
        self._post = list(post_outcomes or [])
        self._status = list(status_outcomes or [])
        self._uploads = list(upload_outcomes or [])
        self.posts: list[dict] = []
        self.status_calls: list[str] = []
        self.closed = False
        self._draft_outcome = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create_expense_claim_draft(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": "/api/v1/sync/sub/expense-claims/drafts",
                "payload": command.model_dump(mode="json"),
                "idempotency_key": idempotency_key,
            }
        )
        if self._draft_outcome is None:
            self._draft_outcome = type(
                "DraftOutcome",
                (),
                {
                    "claim_id": uuid4(),
                    "claim_number": "EXP-DRAFT",
                    "status": "draft",
                    "items": tuple(
                        type(
                            "DraftLine",
                            (),
                            {
                                "source_line_id": item.source_line_id,
                                "item_id": uuid4(),
                            },
                        )()
                        for item in command.items
                    ),
                },
            )()
        return self._draft_outcome

    def upload_expense_receipt(self, command):
        self.posts.append(
            {
                "path": "receipt",
                "payload": {"source_attachment_id": str(command.source_attachment_id)},
                "idempotency_key": command.idempotency_key,
            }
        )
        outcome = self._uploads.pop(0) if self._uploads else None
        if isinstance(outcome, Exception):
            raise outcome
        return type("ReceiptOutcome", (), {"created": True})()

    def approve_expense_claim(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": f"/api/v1/sync/sub/expense-claims/{command.source_claim_id}/approve",
                "payload": command.model_dump(mode="json", exclude_none=True),
                "idempotency_key": idempotency_key,
            }
        )
        outcome = self._post.pop(0) if self._post else {"status": "approved"}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_expense_claim_status(self, source_claim_id):
        self.status_calls.append(source_claim_id)
        outcome = self._status.pop(0) if self._status else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Payload mapping fidelity (vs CRM's _map_expense_request shape)
# ---------------------------------------------------------------------------


def test_payload_mapping_matches_neutral_erp_contract(db_session):
    request = _make_submitted_request(db_session)
    request.selected_approver_erp_id = uuid4()
    request.payment_destination_token = "enc:opaque-destination-token"
    payload = expense_sync.build_expense_claim_payload(request)

    assert payload["source_claim_id"] == str(request.id)
    assert payload["purpose"] == "Transport for extra drop cable"
    assert payload["claim_date"] == date.today().isoformat()
    assert payload["requested_by_email"] == request.requested_by_system_user.email
    assert payload["requested_approver_id"] == str(request.selected_approver_erp_id)
    assert payload["payment_destination_token"] == "enc:opaque-destination-token"
    assert "recipient_account_number" not in payload
    # Neutral source references come from retained work-order provenance.
    assert payload["ticket_source_reference"] == "crm-ticket-77"
    assert payload["project_source_reference"] == "crm-project-88"
    assert payload["currency_code"] == "NGN"
    assert payload["remarks"] == "Customer site was missing materials"

    assert len(payload["items"]) == 1
    line = payload["items"][0]
    assert line["category_code"] == "transport"
    assert line["description"] == "Bike delivery"
    # amount stringified into claimed_amount (CRM parity).
    assert line["claimed_amount"] == "2500.00"
    assert line["expense_date"] == date.today().isoformat()
    assert line["vendor_name"] == "Rider"
    assert line["notes"] == "Urgent part pickup"


def test_approval_release_idempotency_key_is_stable(db_session):
    request = _make_submitted_request(db_session)
    key1 = expense_sync.expense_release_idempotency_key(request)
    key2 = expense_sync.expense_release_idempotency_key(request)
    assert key1 == key2 == f"exp-{request.id}-approved-release-v2"


def test_eligibility_requires_manager_approval(db_session):
    request = _make_submitted_request(db_session)
    assert "cannot be synced" in expense_sync.expense_claim_eligibility_error(request)

    request.status = "approved"
    request.approved_at = datetime.now(UTC)
    assert expense_sync.expense_claim_eligibility_error(request) is None

    request.status = "draft"
    assert "cannot be synced" in expense_sync.expense_claim_eligibility_error(request)


def test_only_selected_approver_can_approve(db_session):
    request = _make_submitted_request(db_session)
    selected_approver = _user(db_session, "Selected Approver")
    request.selected_approver_system_user_id = selected_approver.id
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        _approve(db_session, request)

    assert exc.value.code == "operations.expense_requests.approver_mismatch"


# ---------------------------------------------------------------------------
# Enqueue on submission and approval — gated by ownership
# ---------------------------------------------------------------------------


def _outbox_rows(db, request) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == request.id)
        .all()
    )


def test_submit_does_not_enqueue_before_ownership_cutover(db_session):
    request = _make_submitted_request(db_session)
    assert _outbox_rows(db_session, request) == []
    assert not (request.metadata_ or {}).get("backoffice_events")


def test_approval_fails_closed_before_ownership_cutover(db_session):
    request = _make_submitted_request(db_session)

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    request = db_session.get(FieldExpenseRequest, request.id)
    assert raised.value.code.endswith("erp_delivery_not_configured")
    assert request.status == "submitted"
    assert request.approved_at is None
    assert _outbox_rows(db_session, request) == []


def test_adapter_failure_rolls_back_approval_for_safe_retry(db_session, monkeypatch):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    request = _make_submitted_request(db_session)
    monkeypatch.setattr(backoffice, "enqueue_expense_decision", fail_enqueue)

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    request = db_session.get(FieldExpenseRequest, request.id)
    assert raised.value.code.endswith("erp_staging_failed")
    assert request.status == "submitted"
    assert request.approved_at is None
    assert _outbox_rows(db_session, request) == []


def test_approval_enqueues_with_owner_and_enabled_capability(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    assert _outbox_rows(db_session, request) == []

    outcome = _approve(db_session, request)

    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    row = rows[0]
    assert row.flow == FieldErpSyncFlow.expense_claim.value
    assert row.idempotency_key == f"exp-{request.id}-approved-release-v2"
    assert row.payload["_expense_action"] == "release_approved_v2"
    assert "_depends_on_idempotency_key" not in row.payload
    assert row.status == FieldErpSyncStatus.pending.value
    assert outcome.status == "approved"
    assert outcome.erp_sync_status is ExpenseErpSyncStatus.PENDING
    assert outcome.erp_sync_event_id == row.id


def test_approval_stages_before_delivery_capability_is_enabled(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)

    outcome = _approve(db_session, request)

    assert outcome.erp_sync_status is ExpenseErpSyncStatus.PENDING
    assert len(_outbox_rows(db_session, request)) == 1


def test_reapprove_reuses_the_same_outbox_row(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    first = _approve(db_session, request)
    request = db_session.get(FieldExpenseRequest, request.id)
    # Re-enqueue directly with the same (stable) key → idempotent, no duplicate.
    second = _approve(db_session, request)
    assert first.erp_sync_event_id == second.erp_sync_event_id
    assert len(_outbox_rows(db_session, request)) == 1


def test_payment_stages_after_approval_with_a_distinct_permission(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    manager = _user(db_session, "PayingManager")
    command_id = uuid4()
    db_session.commit()

    command = InitiateFieldExpensePayment(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"user:{manager.id}",
            scope="operations:expense_request:pay",
            reason=f"pay_expense_request:{request.id}",
            idempotency_key=str(command_id),
        ),
        expense_request_id=request.id,
        manager_system_user_id=manager.id,
    )
    db_session_adapter.release_read_transaction(db_session)
    outcome = initiate_field_expense_payment_command(
        db_session,
        command=command,
    )

    rows = _outbox_rows(db_session, request)
    payment = next(
        row for row in rows if row.payload["_expense_action"] == "initiate_payment"
    )
    assert outcome.payment_status == "queued"
    assert outcome.erp_sync_event_id == payment.id
    assert payment.payload["_depends_on_idempotency_key"] == (
        f"exp-{request.id}-approved-release-v2"
    )
    assert payment.payload["initiated_by_email"] == manager.email


# ---------------------------------------------------------------------------
# Outbox delivery → write-back onto the source row
# ---------------------------------------------------------------------------


def test_delivery_accepted_writes_erp_fields_back(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "claim_id": "ERP-CLAIM-1",
                "claim_number": "EXP-0001",
                "status": "approved",
                "source_claim_id": str(request.id),
            },
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.accepted == 1
    assert request.expense_claim_reference == "ERP-CLAIM-1"
    assert request.expense_claim_number == "EXP-0001"
    assert request.expense_claim_status == "approved"
    # ERP transport status cannot rewind the local approval decision.
    assert request.status == "approved"
    assert client.posts[0]["path"] == "/api/v1/sync/sub/expense-claims/drafts"
    assert client.posts[1]["path"] == (
        f"/api/v1/sync/sub/expense-claims/{request.id}/approve"
    )


def test_delivery_approved_maps_terminal_status(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[
            {"claim_id": "ERP-2", "claim_number": "EXP-2", "status": "approved"},
        ]
    )

    outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert request.expense_claim_status == "approved"
    assert request.status == "approved"
    assert request.approved_at is not None


def test_delivery_rejected_keeps_failure_evidence_on_outbox(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"status": "rejected", "rejection_reason": "over budget"}]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert result.rejected == 1
    assert row.status == FieldErpSyncStatus.rejected.value
    assert request.expense_claim_status is None
    assert request.status == "approved"
    assert request.rejection_reason is None


def test_partial_receipt_failure_reuses_claim_and_uploads_only_missing_receipts(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(
        db_session,
        items=[
            _items(description="Taxi")[0],
            _items(description="Hotel", amount="5000.00")[0],
        ],
    )
    attachment_ids = (uuid4(), uuid4())
    for item, attachment_id in zip(request.items, attachment_ids, strict=True):
        _receipt_attachment(
            db_session,
            request,
            attachment_id,
            file_name=f"{attachment_id}.pdf",
        )
        item.receipt_attachment_id = attachment_id

    def resolve_receipt(_db, *, work_order_id, attachment_id, allowed_owner_ids):
        assert work_order_id == request.work_order_mirror_id
        assert request.requested_by_system_user_id in allowed_owner_ids
        content = f"receipt:{attachment_id}".encode()
        return ResolvedExpenseReceiptAttachment(
            attachment_id=attachment_id,
            work_order_id=work_order_id,
            file_name=f"{attachment_id}.pdf",
            mime_type="application/pdf",
            size_bytes=len(content),
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )

    monkeypatch.setattr(
        attachments_module, "resolve_expense_receipt_attachment", resolve_receipt
    )
    db_session.commit()
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"status": "approved"}],
        upload_outcomes=[
            None,
            DotMacERPTransientError("private upstream detail"),
            None,
        ],
    )

    first = outbox.deliver_pending(db_session, client=client)
    row = _outbox_rows(db_session, request)[0]
    assert first.retried == 1
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.last_error == "ERP expense release is temporarily unavailable"

    second = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(row)
    assert second.accepted == 1
    assert row.status == FieldErpSyncStatus.accepted.value
    draft_posts = [entry for entry in client.posts if entry["path"].endswith("/drafts")]
    receipt_posts = [entry for entry in client.posts if entry["path"] == "receipt"]
    approval_posts = [
        entry for entry in client.posts if entry["path"].endswith("/approve")
    ]
    assert len(draft_posts) == 2
    assert [entry["payload"]["source_attachment_id"] for entry in receipt_posts] == [
        str(attachment_ids[0]),
        str(attachment_ids[1]),
        str(attachment_ids[1]),
    ]
    assert len(approval_posts) == 1
    assert row.erp_response["uploaded_source_attachment_ids"] == sorted(
        str(value) for value in attachment_ids
    )


def test_permanent_receipt_failure_is_dead_with_safe_diagnostics(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    attachment_id = uuid4()
    _receipt_attachment(
        db_session,
        request,
        attachment_id,
        file_name="private-person-name.pdf",
    )
    request.items[0].receipt_attachment_id = attachment_id
    content = b"private receipt bytes"
    monkeypatch.setattr(
        attachments_module,
        "resolve_expense_receipt_attachment",
        lambda _db, *, work_order_id, attachment_id, allowed_owner_ids: (
            ResolvedExpenseReceiptAttachment(
                attachment_id=attachment_id,
                work_order_id=work_order_id,
                file_name="private-person-name.pdf",
                mime_type="application/pdf",
                size_bytes=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        ),
    )
    db_session.commit()
    _approve(db_session, request)
    client = _FakeERPClient(
        upload_outcomes=[DotMacERPError("credential and private file detail")]
    )

    result = outbox.deliver_pending(db_session, client=client)

    row = _outbox_rows(db_session, request)[0]
    assert result.dead == 1
    assert row.status == FieldErpSyncStatus.dead.value
    assert row.last_error == "ERP expense release was rejected"
    diagnostic = " ".join(result.errors)
    assert "credential" not in diagnostic
    assert "private-person-name" not in diagnostic
    assert "private receipt bytes" not in diagnostic


def test_dead_event_recovery_is_previewed_linked_and_non_destructive(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    original = _outbox_rows(db_session, request)[0]
    original.status = FieldErpSyncStatus.dead.value
    original.last_error = "ERP expense release was rejected"
    original.erp_response = {
        "claim_status": "draft",
        "claim_id": "63d11a42-c707-4ddb-ad01-3a0523b520dd",
        "uploaded_source_attachment_ids": [],
    }
    original_id = original.id
    db_session.commit()
    erp = _FakeERPClient(status_outcomes=[{"status": "draft"}, {"status": "draft"}])
    monkeypatch.setattr(
        expense_recovery_module,
        "capability_client",
        lambda _db: erp,
    )

    preview = preview_expense_delivery_recovery(
        db_session,
        PreviewExpenseDeliveryRecovery(dead_event_id=original_id),
    )
    db_session.commit()
    command_id = uuid4()
    outcome = recover_expense_delivery(
        db_session,
        command=RecoverExpenseDelivery(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:recovery-operator",
                scope="operations:expense_request:write",
                reason="explicit expense delivery recovery",
                idempotency_key=str(command_id),
            ),
            dead_event_id=original_id,
            preview_fingerprint=preview.fingerprint,
        ),
    )

    original = db_session.get(FieldErpSyncEvent, original_id)
    replacement = db_session.get(FieldErpSyncEvent, outcome.replacement_event_id)
    assert original.status == FieldErpSyncStatus.dead.value
    assert original.last_error == "ERP expense release was rejected"
    assert replacement.status == FieldErpSyncStatus.pending.value
    assert replacement.payload["_replaces_event_id"] == str(original_id)
    assert replacement.idempotency_key.endswith("expense-delivery-recovery.v1")
    assert replacement.erp_response["claim_status"] == "draft"


def test_local_rejection_does_not_enqueue_erp_delivery(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    reviewer_id = request.requested_by_system_user_id
    assert reviewer_id is not None
    expense_request_id = request.id
    command_id = uuid4()
    db_session.commit()

    outcome = reject_field_expense_request_command(
        db_session,
        command=RejectFieldExpenseRequest(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{reviewer_id}",
                scope="operations:expense_request:write",
                reason=f"reject_expense_request:{expense_request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=expense_request_id,
            reviewer_system_user_id=reviewer_id,
            reason="Missing receipt",
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.erp_sync_event_id is None
    assert _outbox_rows(db_session, request) == []


# ---------------------------------------------------------------------------
# Ownership guard — the inert guarantee
# ---------------------------------------------------------------------------


def test_historical_preapproval_event_is_never_delivered(db_session):
    # expense_claim left at the seeded default (crm) — must NOT be sent.
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    outbox.enqueue(
        db_session,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type="field_expense_request",
        entity_id=request.id,
        idempotency_key=f"exp-{request.id}-submit-v1",
        payload={"_expense_action": "submit", "source_claim_id": str(request.id)},
        isolate=False,
    )
    db_session.commit()
    client = _FakeERPClient(post_outcomes=[{"claim_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.skipped_preapproval == 1
    assert client.posts == []
    assert request.expense_claim_reference is None
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.attempts == 0


# ---------------------------------------------------------------------------
# Status reconcile
# ---------------------------------------------------------------------------


def test_refresh_updates_status_for_in_flight_claim(db_session):
    request = _make_submitted_request(db_session)
    request.expense_system = "dotmac_erp"
    request.expense_claim_reference = "ERP-CLAIM-9"
    request.expense_claim_status = "submitted"
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[
            {"claim_id": "ERP-CLAIM-9", "claim_number": "EXP-9", "status": "approved"}
        ]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    db_session.refresh(request)
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert client.status_calls == [str(request.id)]
    assert request.expense_claim_status == "approved"
    # ERP polling is transport evidence only; manager approval remains local authority.
    assert request.status == "submitted"


def test_refresh_drains_a_sent_row_that_the_linked_query_could_never_select(
    db_session,
):
    """A ``sent`` row has no reference BY DEFINITION — the old query excluded it
    forever (the dead end). The widened poller must find it via the outbox and
    drain it to ``accepted`` once ERP returns a real id.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    # Simulate a historical sent delivery whose projection write-back was lost.
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    row.status = FieldErpSyncStatus.sent.value
    row.erp_response = {}
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert request.expense_claim_reference is None
    assert row.status == FieldErpSyncStatus.sent.value

    client = _FakeERPClient(
        status_outcomes=[
            {
                "claim_id": "ERP-CLAIM-LATE",
                "claim_number": "EXP-LATE",
                "status": "approved",
            }
        ]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.accepted.value
    assert request.expense_claim_reference == "ERP-CLAIM-LATE"
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert client.status_calls == [str(request.id)]


# ---------------------------------------------------------------------------
# Write-back repair — a delivered claim whose write-back was lost, no re-emit
# ---------------------------------------------------------------------------


def test_repair_restores_a_dropped_expense_claim_writeback(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"claim_id": "ERP-CLAIM-REPAIR", "status": "approved"}]
    )
    outbox.deliver_pending(db_session, client=client)
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"

    # Simulate a DROPPED write-back: the outbox row is terminal but the
    # request's own reference never landed.
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    row_ids_before = {row.id for row in _outbox_rows(db_session, request)}

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 1
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"
    # No re-emit: approval retains the same sole release-delivery row.
    rows = _outbox_rows(db_session, request)
    assert {row.id for row in rows} == row_ids_before
    assert len(rows) == 1
    assert sum(row.status == FieldErpSyncStatus.accepted.value for row in rows) == 1


def test_repair_makes_no_erp_call_and_no_writeback_for_a_crm_owned_flow(db_session):
    """Michael's finding, applied to expense claims: ownership can move back
    to CRM after a row was delivered while sub owned the flow — the repair
    must re-check ownership on every run, not assume the past delivery still
    means current ownership. No write-back and no ERP call may happen when
    the flow is currently CRM-owned; the row must be counted under
    ``skipped_not_owned``, not ``repaired``.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"claim_id": "ERP-CLAIM-REPAIR", "status": "approved"}]
    )
    outbox.deliver_pending(db_session, client=client)
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"

    # Simulate a DROPPED write-back, same as the repaired-case test above.
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()

    # Ownership moves back to CRM before the scheduled repair runs again.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 0
    assert result["processed"] == 0
    # The sole approval-release response is deliberately skipped.
    assert result["skipped_not_owned"] == 1
    # No re-apply happened: the request's own reference is still missing.
    assert request.expense_claim_reference is None
    assert request.expense_claim_status is None


def test_unlinked_status_poll_makes_no_erp_call_for_a_crm_owned_expense_flow(
    db_session,
):
    """The poll-drain path (``_poll_unlinked_expense_claims``, reached via
    ``refresh_expense_claim_statuses``) makes a real ERP call
    (``get_expense_claim_status``). It must be skipped for a currently
    CRM-owned flow, even though the row was delivered while sub owned it.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    row.status = FieldErpSyncStatus.sent.value
    row.erp_response = {}
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert request.expense_claim_reference is None
    assert row.status == FieldErpSyncStatus.sent.value

    # Ownership moves back to CRM before the poll runs.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[{"claim_id": "SHOULD-NOT-HAPPEN", "status": "approved"}]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert client.status_calls == []
    assert result["skipped_not_owned"] == 1
    db_session.refresh(request)
    assert request.expense_claim_reference is None
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.sent.value


def test_repair_never_writes_back_a_rejected_rows_response(db_session):
    """A rejected/dead row's stored response must never be applied as if ERP
    had accepted it — repair is restricted to accepted/sent rows only."""
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "status": "rejected",
                "rejection_reason": "over budget",
                # A rejected response could still technically carry an id;
                # repair must not treat that as an acceptance.
                "claim_id": "ERP-SHOULD-NOT-LINK",
            }
        ]
    )
    outbox.deliver_pending(db_session, client=client)
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.rejected.value

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 0
    assert request.expense_claim_reference is None


def test_refresh_skips_unsynced_and_terminal_requests(db_session):
    # Not synced yet (no erp id) → excluded.
    unsynced = _make_submitted_request(db_session, crm_work_order_id="wo-a")
    # Synced but already paid (terminal) → excluded from the in-flight poll.
    paid = _make_submitted_request(db_session, crm_work_order_id="wo-b")
    paid.expense_system = "dotmac_erp"
    paid.expense_claim_reference = "ERP-PAID"
    paid.status = "paid"
    db_session.commit()

    client = _FakeERPClient(status_outcomes=[])
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert result["processed"] == 0
    assert client.status_calls == []
    assert unsynced.expense_claim_reference is None


# ---------------------------------------------------------------------------
# Operator-visible diagnostic surface — delivered-but-unlinked rows per flow
# ---------------------------------------------------------------------------


def test_diagnostics_reports_raw_count_and_oldest_age_with_no_threshold_flag(
    db_session,
):
    """The surface is informational only: count + oldest age, no "stale"/alert
    framing, regardless of how old the row is (Michael's design decision:
    "Do not treat it as an alert/SLA. Show count and oldest age as
    informational data until each flow has an explicitly owned threshold.").
    """
    from datetime import timedelta

    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert row.status == FieldErpSyncStatus.accepted.value
    # Backdate well past any plausible threshold — must still be reported
    # plainly, not filtered or flagged.
    row.created_at = datetime.now(UTC) - timedelta(hours=48)
    db_session.commit()

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 1
    assert flow_report["oldest_age_hours"] >= 24
    assert "stale" not in flow_report
    assert "stale_after_hours" not in flow_report


def test_diagnostics_reports_a_fresh_unlinked_row_without_filtering_it(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert row.status == FieldErpSyncStatus.accepted.value

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 1
    assert flow_report["oldest_age_hours"] < 24
    assert "stale" not in flow_report


def test_diagnostics_excludes_a_row_whose_writeback_actually_landed(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(post_outcomes=[{"claim_id": "ERP-LINKED-OK"}]),
    )
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-LINKED-OK"

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 0
