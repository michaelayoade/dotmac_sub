"""Durable evidence for the Sale → Money shadow phase.

A WARNING log is an alert, not evidence. The cutover gate requires a
*consecutive clean observation window*, which cannot be established from logs
that rotate. Each scan therefore persists an append-only row carrying the
contract version, a cohort fingerprint and the full bucket counts, so a reviewer
can prove the cohort was stable and clean across N consecutive runs rather than
taking one green run on trust.

Append-only: a shadow observation is evidence of what was true at a moment. It
is never amended.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class SalesBillingShadowBucket(enum.StrEnum):
    """Every in-scope sales order lands in exactly one of these.

    Exhaustive and mutually exclusive by construction — the scan asserts the
    bucket counts sum to the scanned total, so a silently unclassified order
    fails the run rather than quietly shrinking the denominator.
    """

    #: Settled by decision, not by money. Excluded from comparison, but its
    #: canonical waiver evidence is validated.
    WAIVED_EXCLUDED = "waived_excluded"
    #: Invalid waiver: marked waived without the evidence the owner writes.
    WAIVED_EVIDENCE_MISSING = "waived_evidence_missing"
    #: No billing artifacts, and none is due yet (draft, or nothing to bill).
    UNLINKED_EXPECTED = "unlinked_expected"
    #: No billing artifacts although the order should have them. Must be zero
    #: before cutover.
    UNLINKED_UNEXPECTED = "unlinked_unexpected"
    #: A metadata identifier that is not a well-formed id.
    UNRESOLVED_INVALID = "unresolved_invalid"
    #: A well-formed identifier pointing at no live invoice.
    UNRESOLVED_MISSING = "unresolved_missing"
    #: An artifact reachable from more than one sales order.
    UNRESOLVED_AMBIGUOUS = "unresolved_ambiguous"
    #: Linked, resolvable, and the stored columns agree with the ledger.
    AGREEING = "agreeing"
    #: Linked and resolvable, but the stored columns disagree with the ledger.
    DRIFTING = "drifting"


#: Bumped whenever bucket semantics change. A clean window may only be counted
#: across runs sharing one contract version.
SALES_BILLING_SHADOW_CONTRACT_VERSION = 1


class SalesBillingShadowRun(Base):
    """One append-only observation of the Sale → Money shadow comparison."""

    __tablename__ = "sales_billing_shadow_runs"
    __table_args__ = (
        CheckConstraint("scanned >= 0", name="ck_sales_billing_shadow_scanned"),
        CheckConstraint(
            "contract_version > 0", name="ck_sales_billing_shadow_contract_version"
        ),
        Index("ix_sales_billing_shadow_runs_observed_at", "observed_at"),
        Index(
            "ix_sales_billing_shadow_runs_version_observed",
            "contract_version",
            "observed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Stable hash over the in-scope order ids and their bucket assignments.
    #: Two consecutive runs with the same fingerprint observed the same cohort
    #: in the same state — which is what "consecutive clean window" means.
    cohort_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: bucket value -> count. Sums to ``scanned``.
    bucket_counts: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON()), nullable=False
    )
    #: Whether this observation satisfies the cutover conditions on its own.
    clean: Mapped[bool] = mapped_column(
        default=False, nullable=False, server_default="false"
    )
    actor_id: Mapped[str | None] = mapped_column(String(120))


class SalesBillingShadowImmutableError(RuntimeError):
    """Raised when code attempts to rewrite a shadow observation."""


@event.listens_for(SalesBillingShadowRun, "before_update")
def _reject_shadow_run_update(*_args: object) -> None:
    raise SalesBillingShadowImmutableError(
        "Sale → Money shadow observations are append-only"
    )


@event.listens_for(SalesBillingShadowRun, "before_delete")
def _reject_shadow_run_delete(*_args: object) -> None:
    raise SalesBillingShadowImmutableError(
        "Sale → Money shadow observations are append-only"
    )
