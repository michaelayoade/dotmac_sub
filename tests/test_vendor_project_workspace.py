"""Behavior tests for typed vendor project workspace commands."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.event_store import EventStore
from app.models.project import Project
from app.models.system_user import SystemUser
from app.models.vendor_routes import (
    InstallationProject,
    InstallationProjectStatus,
    ProjectQuote,
    ProjectQuoteStatus,
    Vendor,
)
from app.schemas.vendor_portal import VendorQuoteCreate, VendorQuoteLineCreate
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.vendor_portal_operations import (
    AddVendorQuoteLineCommand,
    CreateVendorQuoteCommand,
    ReviewVendorQuoteCommand,
    VendorProjectWorkspaceError,
    vendor_portal_operations,
)


def _context(*, actor: str, scope: str, reason: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=actor,
        scope=scope,
        reason=reason,
    )


def _chain(db_session):
    project = Project(name="Typed vendor workspace")
    vendor = Vendor(name="Workspace Vendor", code=f"WV-{uuid4().hex[:8]}")
    user = SystemUser(
        first_name="Workspace",
        last_name="Operator",
        display_name="Workspace Operator",
        email=f"vendor-workspace-{uuid4().hex[:8]}@example.com",
    )
    db_session.add_all([project, vendor, user])
    db_session.flush()
    installation = InstallationProject(
        project_id=project.id,
        assigned_vendor_id=vendor.id,
    )
    db_session.add(installation)
    db_session.commit()
    return installation, vendor, user


def test_typed_quote_commands_commit_rows_and_event_evidence(db_session):
    installation, vendor, user = _chain(db_session)
    vendor_id = str(vendor.id)
    user_id = str(user.id)
    command = CreateVendorQuoteCommand(
        context=_context(
            actor=user_id,
            scope=vendor_id,
            reason="test quote creation",
        ),
        payload=VendorQuoteCreate(
            project_id=installation.id,
            currency="NGN",
            vat_rate_percent=Decimal("7.5"),
        ),
        vendor_id=vendor_id,
        user_id=user_id,
    )
    db_session_adapter.release_read_transaction(db_session)
    quote = vendor_portal_operations.create_quote(db_session, command)
    assert db_session.in_transaction() is False

    quote = vendor_portal_operations.add_quote_line(
        db_session,
            AddVendorQuoteLineCommand(
                context=_context(
                    actor=user_id,
                scope=vendor_id,
                reason="test quote line creation",
            ),
            quote_id=str(quote["id"]),
            payload=VendorQuoteLineCreate(
                description="Installation labor",
                quantity=Decimal("2"),
                unit_price=Decimal("10000"),
            ),
            vendor_id=vendor_id,
        ),
    )

    assert db_session.in_transaction() is False
    assert quote["subtotal"] == Decimal("20000.00")
    assert quote["tax_total"] == Decimal("1500.00")
    assert quote["total"] == Decimal("21500.00")
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "vendor_quote.changed")
        .count()
        == 2
    )


def test_rejected_quote_edit_rolls_back_the_owner_transaction(db_session):
    installation, vendor, user = _chain(db_session)
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db_session.add(quote)
    db_session.commit()
    command = AddVendorQuoteLineCommand(
        context=_context(
            actor=str(user.id),
            scope=str(vendor.id),
            reason="test rejected quote edit",
        ),
        quote_id=str(quote.id),
        payload=VendorQuoteLineCreate(
            description="Late line",
            quantity=Decimal("1"),
            unit_price=Decimal("1"),
        ),
        vendor_id=str(vendor.id),
    )
    db_session_adapter.release_read_transaction(db_session)

    with pytest.raises(VendorProjectWorkspaceError) as exc:
        vendor_portal_operations.add_quote_line(db_session, command)

    assert exc.value.code.endswith(".quote_not_editable")
    assert db_session.in_transaction() is False
    assert db_session.query(EventStore).count() == 0


def test_quote_review_updates_project_in_the_same_transaction(db_session):
    installation, vendor, user = _chain(db_session)
    quote = ProjectQuote(
        project_id=installation.id,
        vendor_id=vendor.id,
        status=ProjectQuoteStatus.submitted.value,
    )
    db_session.add(quote)
    db_session.commit()
    command = ReviewVendorQuoteCommand(
        context=_context(
            actor=str(user.id),
            scope=str(quote.id),
            reason="test quote approval",
        ),
        quote_id=str(quote.id),
        reviewer_id=str(user.id),
        approve=True,
        notes="Reviewed",
    )
    db_session_adapter.release_read_transaction(db_session)

    result = vendor_portal_operations.review_quote(
        db_session,
        command,
    )

    assert db_session.in_transaction() is False
    db_session.refresh(installation)
    assert result["status"] == ProjectQuoteStatus.approved.value
    assert installation.status == InstallationProjectStatus.approved.value
    assert installation.approved_quote_id == quote.id
    event = db_session.query(EventStore).one()
    assert event.event_type == "vendor_quote.changed"
    assert event.payload["action"] == "approved"
