"""Immutable shared-outage support basis and infrastructure replacement provenance."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SupportResolutionSharedIncident(Base):
    __tablename__ = "support_resolution_shared_incidents"
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("support_resolution_requests.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    complaint_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incident_ticket_links.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incidents.id", ondelete="RESTRICT"), nullable=False
    )
    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False
    )
    original_receipt_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_complaint_correlation_receipts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_receipt_ref: Mapped[str] = mapped_column(String(120), nullable=False)
    original_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_scope_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    original_revision_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    infrastructure_ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_scope_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_revision_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_token: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    empty_scope_review: Mapped[str | None] = mapped_column(String(48))
    __table_args__ = (
        CheckConstraint(
            "original_revision_sequence>0 AND reviewed_revision_sequence>0 AND length(membership_token)=64"
        ),
        CheckConstraint(
            "empty_scope_review IS NULL OR empty_scope_review='restored_without_issued_field_scope'"
        ),
    )


class SupportResolutionSharedWorkScope(Base):
    __tablename__ = "support_resolution_shared_work_scopes"
    request_id: Mapped[UUID] = mapped_column(primary_key=True)
    complaint_link_id: Mapped[UUID] = mapped_column(primary_key=True)
    issuance_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incident_work_order_links.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    work_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("work_order.id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["request_id", "complaint_link_id"],
            [
                "support_resolution_shared_incidents.request_id",
                "support_resolution_shared_incidents.complaint_link_id",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["request_id", "work_order_id"],
            [
                "support_resolution_bases.request_id",
                "support_resolution_bases.work_order_id",
            ],
            ondelete="RESTRICT",
        ),
    )


class CompletionReplacementOutageProof(Base):
    __tablename__ = "completion_replacement_outage_proofs"
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("completion_replacement_decisions.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    source_issuance_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incident_work_order_links.id", ondelete="RESTRICT"),
        nullable=False,
    )
    replacement_issuance_link_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incident_work_order_links.id", ondelete="RESTRICT"),
        nullable=False,
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_incidents.id", ondelete="RESTRICT"), nullable=False
    )
    infrastructure_ticket_id: Mapped[UUID] = mapped_column(
        ForeignKey("support_tickets.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_scope_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    revision_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    membership_token: Mapped[str] = mapped_column(String(64), nullable=False)
    replacement_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("outage_scope_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    replacement_revision_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    replacement_membership_token: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[UUID | None] = mapped_column()
    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT")
    )
    task_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("project_tasks.id", ondelete="RESTRICT")
    )
    purpose: Mapped[str | None] = mapped_column(String(40))
    access_technology: Mapped[str | None] = mapped_column(String(40))
    __table_args__ = (
        CheckConstraint(
            "source_issuance_link_id<>replacement_issuance_link_id AND revision_sequence>0 AND length(membership_token)=64"
        ),
    )
