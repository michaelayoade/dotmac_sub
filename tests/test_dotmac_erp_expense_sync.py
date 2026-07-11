"""ERP re-home PR 2 — expense-claim flow (map + enqueue + write-back + reconcile).

Everything runs against a MOCKED ERP: the outbox uses a fake client, and the
status reconcile is fed a canned response. The flow is proven end-to-end WITHOUT
a live ERP and, crucially, WITHOUT flipping ownership in prod — tests set
``sync_flow_ownership.expense_claim = sub`` in-test only. The default (crm) path
is asserted to send nothing (the inert guarantee).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.dispatch import TechnicianProfile
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_expense import FieldExpenseRequest
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order_mirror import WorkOrderMirror
from app.services.dotmac_erp import expense_sync, outbox
from app.services.field import expense_requests as expense_requests_module
from app.services.field.expense_requests import field_expense_requests

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


def _work_order(db, subscriber: Subscriber, **overrides) -> WorkOrderMirror:
    row = WorkOrderMirror(
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
        "notes": "Urgent part pickup",
    }
    item.update(overrides)
    return [item]


def _make_submitted_request(db, *, crm_work_order_id="wo-exp") -> FieldExpenseRequest:
    """Create + submit a request via the real service, ERP sync disabled (default)."""
    crm_person_id = f"crm-tech-{uuid4().hex[:8]}"
    user = _user(db)
    _profile(db, user, crm_person_id=crm_person_id)
    subscriber = _subscriber(db)
    _work_order(
        db,
        subscriber,
        crm_work_order_id=crm_work_order_id,
        assigned_to_crm_person_id=crm_person_id,
    )
    db.commit()
    created = field_expense_requests.create(
        db,
        _auth(user),
        crm_work_order_id=crm_work_order_id,
        purpose="Transport for extra drop cable",
        expense_date=date.today(),
        currency="NGN",
        notes="Customer site was missing materials",
        client_ref=uuid4(),
        items=_items(),
    )
    field_expense_requests.submit(db, _auth(user), str(created["id"]))
    return db.get(FieldExpenseRequest, created["id"])


class _FakeERPClient:
    """Mocked ERP client for outbox + reconcile: canned responses in order."""

    def __init__(self, post_outcomes=None, status_outcomes=None):
        self._post = list(post_outcomes or [])
        self._status = list(status_outcomes or [])
        self.posts: list[dict] = []
        self.status_calls: list[str] = []
        self.closed = False

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def get_expense_claim_status(self, omni_id):
        self.status_calls.append(omni_id)
        outcome = self._status.pop(0) if self._status else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Payload mapping fidelity (vs CRM's _map_expense_request shape)
# ---------------------------------------------------------------------------


def test_payload_mapping_matches_crm_shape(db_session):
    request = _make_submitted_request(db_session)
    payload = expense_sync.build_expense_claim_payload(request)

    assert payload["omni_id"] == str(request.id)
    assert payload["purpose"] == "Transport for extra drop cable"
    assert payload["claim_date"] == date.today().isoformat()
    assert payload["requested_by_email"] == request.requested_by_system_user.email
    # ticket/project ids come off the work-order mirror (sub has no direct FKs).
    assert payload["ticket_crm_id"] == "crm-ticket-77"
    assert payload["project_crm_id"] == "crm-project-88"
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


def test_idempotency_key_is_stable_across_resubmit(db_session):
    request = _make_submitted_request(db_session)
    key1 = expense_sync.expense_claim_idempotency_key(request)
    key2 = expense_sync.expense_claim_idempotency_key(request)
    assert key1 == key2 == f"exp-{request.id}-submit-v1"


def test_eligibility_requires_submitted_items_and_email(db_session):
    request = _make_submitted_request(db_session)
    assert expense_sync.expense_claim_eligibility_error(request) is None

    request.status = "draft"
    assert "cannot be synced" in expense_sync.expense_claim_eligibility_error(request)


# ---------------------------------------------------------------------------
# Enqueue on submit — gated by dotmac_erp_sync_enabled
# ---------------------------------------------------------------------------


def _outbox_rows(db, request) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == request.id)
        .all()
    )


def test_submit_does_not_enqueue_when_flag_off(db_session):
    # Default: dotmac_erp_sync_enabled resolves False → flow inert, no outbox row.
    request = _make_submitted_request(db_session)
    assert _outbox_rows(db_session, request) == []


def test_submit_enqueues_when_flag_on(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    request = _make_submitted_request(db_session)

    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    row = rows[0]
    assert row.flow == FieldErpSyncFlow.expense_claim.value
    assert row.idempotency_key == f"exp-{request.id}-submit-v1"
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.payload["omni_id"] == str(request.id)


def test_resubmit_reuses_the_same_outbox_row(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    request = _make_submitted_request(db_session)
    # Re-enqueue directly with the same (stable) key → idempotent, no duplicate.
    first = expense_sync.enqueue_expense_claim(db_session, request)
    second = expense_sync.enqueue_expense_claim(db_session, request)
    assert first.id == second.id
    assert len(_outbox_rows(db_session, request)) == 1


# ---------------------------------------------------------------------------
# Outbox delivery → write-back onto the source row
# ---------------------------------------------------------------------------


def test_delivery_accepted_writes_erp_fields_back(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "claim_id": "ERP-CLAIM-1",
                "claim_number": "EXP-0001",
                "status": "submitted",
                "omni_id": str(request.id),
            }
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.accepted == 1
    assert request.erp_expense_claim_id == "ERP-CLAIM-1"
    assert request.erp_claim_number == "EXP-0001"
    assert request.erp_claim_status == "submitted"
    # Non-terminal ERP status leaves the sub row in submitted.
    assert request.status == "submitted"
    assert client.posts[0]["path"] == "/sync/crm/expense-claims"


def test_delivery_approved_maps_terminal_status(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    client = _FakeERPClient(
        post_outcomes=[
            {"claim_id": "ERP-2", "claim_number": "EXP-2", "status": "approved"}
        ]
    )

    outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert request.erp_claim_status == "approved"
    assert request.status == "approved"
    assert request.approved_at is not None


def test_delivery_rejected_records_reason(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    client = _FakeERPClient(
        post_outcomes=[{"status": "rejected", "rejection_reason": "over budget"}]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert result.rejected == 1
    assert row.status == FieldErpSyncStatus.rejected.value
    assert request.erp_claim_status == "rejected"
    assert request.status == "rejected"
    assert request.rejection_reason == "over budget"


# ---------------------------------------------------------------------------
# Ownership guard — the inert guarantee
# ---------------------------------------------------------------------------


def test_delivery_refused_when_flow_owned_by_crm(db_session, monkeypatch):
    monkeypatch.setattr(expense_requests_module, "_erp_sync_enabled", lambda db: True)
    # expense_claim left at the seeded default (crm) — must NOT be sent.
    _seed_ownership(db_session)
    request = _make_submitted_request(db_session)
    client = _FakeERPClient(post_outcomes=[{"claim_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.skipped_not_owned == 1
    assert client.posts == []
    assert request.erp_expense_claim_id is None
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.attempts == 0


# ---------------------------------------------------------------------------
# Status reconcile
# ---------------------------------------------------------------------------


def test_refresh_updates_status_for_in_flight_claim(db_session):
    request = _make_submitted_request(db_session)
    request.erp_expense_claim_id = "ERP-CLAIM-9"
    request.erp_claim_status = "submitted"
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
    assert request.erp_claim_status == "approved"
    assert request.status == "approved"


def test_refresh_skips_unsynced_and_terminal_requests(db_session):
    # Not synced yet (no erp id) → excluded.
    unsynced = _make_submitted_request(db_session, crm_work_order_id="wo-a")
    # Synced but already paid (terminal) → excluded from the in-flight poll.
    paid = _make_submitted_request(db_session, crm_work_order_id="wo-b")
    paid.erp_expense_claim_id = "ERP-PAID"
    paid.status = "paid"
    db_session.commit()

    client = _FakeERPClient(status_outcomes=[])
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert result["processed"] == 0
    assert client.status_calls == []
    assert unsynced.erp_expense_claim_id is None
