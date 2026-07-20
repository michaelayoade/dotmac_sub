"""Vendor submits require a signed impact preview and idempotent confirmation."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    AsBuiltRoute,
    InstallationProject,
    InstallationProjectLifecycleEvent,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteLineItem,
    ProjectQuoteStatus,
    Vendor,
    VendorPurchaseInvoice,
    VendorPurchaseInvoiceLineItem,
)
from app.schemas.vendor_portal import VendorAsBuiltCreate, VendorAsBuiltLineCreate
from app.services import vendor_submission_proposals
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.vendor_submission_proposals import (
    ConfirmVendorSubmissionCommand,
    VendorSubmissionError,
)


def _confirm(
    db_session,
    *,
    token: str,
    vendor_id: str,
    user_id: str,
    project_id: str,
):
    db_session_adapter.release_read_transaction(db_session)
    command_id = uuid4()
    return vendor_submission_proposals.confirm_submission(
        db_session,
        ConfirmVendorSubmissionCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=user_id,
                scope=vendor_id,
                reason="test_vendor_submission_confirmation",
            ),
            confirmation_token=token,
            vendor_id=vendor_id,
            user_id=user_id,
            project_id=project_id,
        ),
    )


def _chain(db_session):
    project = Project(name="Proposal-bound vendor install")
    vendor = Vendor(name="Proposal Vendor", code=f"PV-{uuid4().hex[:8]}")
    user = SystemUser(
        first_name="Vendor",
        last_name="Operator",
        display_name="Vendor Operator",
        email=f"vendor-proposal-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, user])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(installation)
    db_session.flush()
    return installation, vendor, user


def _quote(db_session, installation, vendor) -> ProjectQuote:
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.draft.value,
        currency="NGN",
        vat_rate_percent=Decimal("7.50"),
    )
    db_session.add(quote)
    db_session.flush()
    db_session.add(
        ProjectQuoteLineItem(
            quote_id=quote.id,
            description="Drop cable and installation",
            quantity=Decimal("2"),
            unit_price=Decimal("10000"),
            amount=Decimal("20000"),
            is_active=True,
        )
    )
    db_session.commit()
    return quote


def test_quote_confirmation_is_preview_bound_and_replay_safe(db_session):
    installation, vendor, user = _chain(db_session)
    quote = _quote(db_session, installation, vendor)
    proposal = vendor_submission_proposals.issue_quote_submission(
        db_session,
        quote_id=str(quote.id),
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )

    first = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )
    replay = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )

    db_session.refresh(quote)
    assert proposal.details[-1][1] == (
        "Quote becomes read-only and enters staff review"
    )
    assert quote.status == ProjectQuoteStatus.submitted.value
    assert first.result_id == str(quote.id)
    assert first.replayed is False
    assert replay.result_id == str(quote.id)
    assert replay.replayed is True
    confirmation_event = (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_submission.confirmed")
        .one()
    )
    assert confirmation_event.payload["schema_version"] == 1
    assert confirmation_event.payload["submission_type"] == "quote"
    assert confirmation_event.payload["result_id"] == str(quote.id)
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == "vendor_quote_submit")
        .count()
        == 1
    )


def test_quote_confirmation_rejects_state_changed_after_preview(db_session):
    installation, vendor, user = _chain(db_session)
    quote = _quote(db_session, installation, vendor)
    proposal = vendor_submission_proposals.issue_quote_submission(
        db_session,
        quote_id=str(quote.id),
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )
    quote.line_items[0].amount = Decimal("25000.00")
    db_session.commit()

    with pytest.raises(VendorSubmissionError, match="changed after preview") as exc:
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            vendor_id=str(vendor.id),
            user_id=str(user.id),
            project_id=str(installation.id),
        )

    assert exc.value.code.endswith(".stale_proposal")
    assert db_session.query(IdempotencyKey).count() == 0


def test_purchase_invoice_confirmation_uses_financial_preview(db_session):
    installation, vendor, user = _chain(db_session)
    quote = _quote(db_session, installation, vendor)
    quote.status = ProjectQuoteStatus.submitted.value
    invoice = VendorPurchaseInvoice(
        project_id=installation.id,
        vendor_id=vendor.id,
        invoice_number="PV-INV-001",
        currency="NGN",
        tax_rate_percent=Decimal("7.50"),
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        VendorPurchaseInvoiceLineItem(
            invoice_id=invoice.id,
            description="Completed installation",
            quantity=Decimal("1"),
            unit_price=Decimal("50000"),
            amount=Decimal("50000"),
            is_active=True,
        )
    )
    db_session.commit()

    proposal = vendor_submission_proposals.issue_purchase_invoice_submission(
        db_session,
        invoice_id=str(invoice.id),
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )
    result = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )

    db_session.refresh(invoice)
    assert ("Total", "NGN 53,750.00") in proposal.details
    assert invoice.status == "submitted"
    assert result.result_id == str(invoice.id)


def test_as_built_confirmation_uses_signed_payload_and_is_idempotent(db_session):
    installation, vendor, user = _chain(db_session)
    db_session.commit()
    payload = VendorAsBuiltCreate(
        project_id=installation.id,
        actual_length_meters=150.5,
        variation_reason="Avoided an obstruction",
        line_items=[
            VendorAsBuiltLineCreate(
                description="Installed drop cable",
                quantity=Decimal("150.5"),
                unit_price=Decimal("0"),
            )
        ],
    )
    proposal = vendor_submission_proposals.issue_as_built_submission(
        db_session,
        payload=payload,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )

    first = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )
    replay = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )

    assert first.result_id == replay.result_id
    assert replay.replayed is True
    assert db_session.query(AsBuiltRoute).count() == 1


def test_confirmation_cannot_cross_vendor_principal_context(db_session):
    installation, vendor, user = _chain(db_session)
    quote = _quote(db_session, installation, vendor)
    proposal = vendor_submission_proposals.issue_quote_submission(
        db_session,
        quote_id=str(quote.id),
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )

    with pytest.raises(VendorSubmissionError) as exc:
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            vendor_id=str(uuid4()),
            user_id=str(user.id),
            project_id=str(installation.id),
        )

    assert exc.value.code.endswith(".proposal_context_mismatch")


def test_confirmation_failure_rolls_back_reservation_and_domain_mutation(
    db_session, monkeypatch
):
    installation, vendor, user = _chain(db_session)
    quote = _quote(db_session, installation, vendor)
    proposal = vendor_submission_proposals.issue_quote_submission(
        db_session,
        quote_id=str(quote.id),
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )

    def fail_after_mutation(db, quote_id, vendor_id, *, commit):
        target = db.query(ProjectQuote).filter(ProjectQuote.id == quote.id).one()
        target.status = ProjectQuoteStatus.submitted.value
        db.flush()
        raise RuntimeError("simulated collaborator failure")

    monkeypatch.setattr(
        vendor_submission_proposals.vendor_portal_operations,
        "submit_quote",
        fail_after_mutation,
    )

    with pytest.raises(RuntimeError, match="simulated collaborator failure"):
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            vendor_id=str(vendor.id),
            user_id=str(user.id),
            project_id=str(installation.id),
        )

    assert db_session.in_transaction() is False
    assert db_session.query(IdempotencyKey).count() == 0
    db_session.refresh(quote)
    assert quote.status == ProjectQuoteStatus.draft.value


def test_project_start_confirmation_is_preview_bound_and_replay_safe(db_session):
    installation, vendor, user = _chain(db_session)
    installation.status = InstallationProjectStatus.approved.value
    db_session.commit()
    proposal = vendor_submission_proposals.issue_project_lifecycle(
        db_session,
        project_id=str(installation.id),
        action="start",
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )

    first = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )
    replay = _confirm(
        db_session,
        token=proposal.confirmation_token,
        vendor_id=str(vendor.id),
        user_id=str(user.id),
        project_id=str(installation.id),
    )

    db_session.refresh(installation)
    evidence = db_session.query(InstallationProjectLifecycleEvent).one()
    assert installation.status == InstallationProjectStatus.in_progress.value
    assert evidence.actor_id == str(user.id)
    assert first.result_id == str(evidence.id)
    assert first.replayed is False
    assert replay.result_id == str(evidence.id)
    assert replay.replayed is True
    assert db_session.query(InstallationProjectLifecycleEvent).count() == 1
    assert db_session.query(EventStore).count() == 2
    assert proposal.confirmation_label == "Confirm start"
    assert (
        db_session.query(IdempotencyKey)
        .filter(IdempotencyKey.scope == "vendor_project_start")
        .count()
        == 1
    )


def test_project_lifecycle_confirmation_rejects_changed_state(db_session):
    installation, vendor, user = _chain(db_session)
    installation.status = InstallationProjectStatus.approved.value
    db_session.commit()
    proposal = vendor_submission_proposals.issue_project_lifecycle(
        db_session,
        project_id=str(installation.id),
        action="start",
        vendor_id=str(vendor.id),
        user_id=str(user.id),
    )
    installation.status = InstallationProjectStatus.in_progress.value
    db_session.commit()

    with pytest.raises(VendorSubmissionError) as exc:
        _confirm(
            db_session,
            token=proposal.confirmation_token,
            vendor_id=str(vendor.id),
            user_id=str(user.id),
            project_id=str(installation.id),
        )

    assert exc.value.code.endswith(".lifecycle_invalid_transition")
    assert db_session.query(InstallationProjectLifecycleEvent).count() == 0
