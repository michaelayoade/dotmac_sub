"""Governed sole-approver exception for the two-person finance flows.

Three flows refuse self-approval by default: the prepaid renewal-terms record,
the carried-source identity review and the paid-invoice period repair. Michael
is the company's sole finance and admin decision-maker, and decided that his
own approval is enough ("I only approve is enough"). This module is the ONE
typed policy that lets those flows accept ``requester == approver`` (or
``reviewer == approver``) and nothing else. It is a time-boxed exception in the
style of Governance decision 53, not a standing rule.

The exception is allowed only when ALL of these hold:

* ``billing.sole_approver_exception_enabled`` is true (default false);
* today (UTC) is strictly before ``billing.sole_approver_exception_review_due``;
* the approver is the system user named by
  ``billing.sole_approver_exception_principal``, an active human staff
  ``SystemUser`` acting as ``user:<that id>`` (never an API key, a service or
  any automated actor);
* a non-empty justification is supplied, and a decision reference is set.

Every other guard in the calling flow (permission, fingerprint, staleness,
idempotency) is unchanged. This module decides nothing but the self-approval
question; the calling owner records the evidence in its own approval record and
stages the distinct ``approval.sole_approver_exception_used`` audit action.

The settings change only through the settings owner (``control:settings:write``)
which audits every write. Setting ``enabled`` to false, clearing the principal,
or letting the review date pass each switch the exception off.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.domain_settings import SettingDomain
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)

OWNER = "governance.sole_approver_exception"
AUDIT_ACTION = "approval.sole_approver_exception_used"
SETTING_DOMAIN = SettingDomain.billing
ENABLED_KEY = "sole_approver_exception_enabled"
PRINCIPAL_KEY = "sole_approver_exception_principal"
REVIEW_DUE_KEY = "sole_approver_exception_review_due"
DECISION_REF_KEY = "sole_approver_exception_decision_ref"
RUNBOOK = "docs/runbooks/SOLE_APPROVER_EXCEPTION.md"
MAX_JUSTIFICATION_LENGTH = 1000


class SoleApproverRefusal(StrEnum):
    """Closed reasons the exception does not apply."""

    disabled = "disabled"
    review_date_unset = "review_date_unset"
    expired = "expired"
    principal_unset = "principal_unset"
    wrong_principal = "wrong_principal"
    not_human_staff = "not_human_staff"
    justification_missing = "justification_missing"
    decision_ref_missing = "decision_ref_missing"


@dataclass(frozen=True, slots=True)
class SoleApproverExceptionPolicy:
    """The four governance settings, parsed. Malformed values parse to None."""

    enabled: bool
    principal: UUID | None
    review_due: date | None
    decision_ref: str


@dataclass(frozen=True, slots=True)
class SoleApproverExceptionGrant:
    """Proof that one self-approval was allowed, to be recorded as evidence."""

    flow: str
    approver_id: UUID
    decision_ref: str
    review_due: date
    justification: str

    def evidence(self) -> dict[str, object]:
        return {
            "sole_approver_exception": True,
            "sole_approver_exception_decision_ref": self.decision_ref,
            "sole_approver_exception_review_due": self.review_due.isoformat(),
            "sole_approver_exception_justification": self.justification,
        }


@dataclass(frozen=True, slots=True)
class SoleApproverExceptionDecision:
    """Typed outcome: a grant when allowed, otherwise the closed refusal."""

    grant: SoleApproverExceptionGrant | None
    refusal: SoleApproverRefusal | None

    @property
    def allowed(self) -> bool:
        return self.grant is not None


def _parse_uuid(value: object) -> UUID | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value.strip())
    except ValueError:
        return None


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def load_sole_approver_exception_policy(db: Session) -> SoleApproverExceptionPolicy:
    """Read the governance settings; anything unreadable fails closed."""

    decision_ref = resolve_value(db, SETTING_DOMAIN, DECISION_REF_KEY)
    return SoleApproverExceptionPolicy(
        enabled=resolve_value(db, SETTING_DOMAIN, ENABLED_KEY) is True,
        principal=_parse_uuid(resolve_value(db, SETTING_DOMAIN, PRINCIPAL_KEY)),
        review_due=_parse_date(resolve_value(db, SETTING_DOMAIN, REVIEW_DUE_KEY)),
        decision_ref=decision_ref.strip() if isinstance(decision_ref, str) else "",
    )


def evaluate_sole_approver_exception(
    policy: SoleApproverExceptionPolicy,
    *,
    flow: str,
    approver_id: UUID,
    actor: str,
    approver_is_human_staff: bool,
    justification: str | None,
    today: date,
) -> SoleApproverExceptionDecision:
    """Pure policy decision; see the module docstring for the rule."""

    def refuse(reason: SoleApproverRefusal) -> SoleApproverExceptionDecision:
        return SoleApproverExceptionDecision(grant=None, refusal=reason)

    if not policy.enabled:
        return refuse(SoleApproverRefusal.disabled)
    if policy.review_due is None:
        return refuse(SoleApproverRefusal.review_date_unset)
    if today >= policy.review_due:
        return refuse(SoleApproverRefusal.expired)
    if policy.principal is None:
        return refuse(SoleApproverRefusal.principal_unset)
    if approver_id != policy.principal:
        return refuse(SoleApproverRefusal.wrong_principal)
    if not approver_is_human_staff or actor != f"user:{approver_id}":
        return refuse(SoleApproverRefusal.not_human_staff)
    reason = (justification or "").strip()
    if not reason or len(reason) > MAX_JUSTIFICATION_LENGTH:
        return refuse(SoleApproverRefusal.justification_missing)
    if not policy.decision_ref:
        return refuse(SoleApproverRefusal.decision_ref_missing)
    return SoleApproverExceptionDecision(
        grant=SoleApproverExceptionGrant(
            flow=flow,
            approver_id=approver_id,
            decision_ref=policy.decision_ref,
            review_due=policy.review_due,
            justification=reason,
        ),
        refusal=None,
    )


def authorize_sole_approver(
    db: Session,
    *,
    flow: str,
    approver_id: UUID,
    actor: str,
    justification: str | None,
    today: date | None = None,
) -> SoleApproverExceptionDecision:
    """Decide whether ``approver_id`` may approve their own request now."""

    user = db.get(SystemUser, approver_id)
    human_staff = (
        user is not None and user.is_active and user.user_type == UserType.system_user
    )
    decision = evaluate_sole_approver_exception(
        load_sole_approver_exception_policy(db),
        flow=flow,
        approver_id=approver_id,
        actor=actor,
        approver_is_human_staff=human_staff,
        justification=justification,
        today=today or datetime.now(UTC).date(),
    )
    if not decision.allowed:
        logger.info(
            "sole_approver_exception_refused: flow=%s refusal=%s",
            flow,
            decision.refusal.value if decision.refusal else None,
        )
    return decision


def stage_sole_approver_exception_audit(
    db: Session,
    grant: SoleApproverExceptionGrant,
    *,
    entity_type: str,
    entity_id: str,
    evidence_ref: str,
) -> None:
    """Stage the distinct audit action in the caller's owner transaction."""

    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(grant.approver_id),
            action=AUDIT_ACTION,
            entity_type=entity_type,
            entity_id=entity_id,
            metadata_={
                "flow": grant.flow,
                "evidence_ref": evidence_ref,
                **grant.evidence(),
            },
        ),
    )


__all__ = [
    "AUDIT_ACTION",
    "OWNER",
    "SoleApproverExceptionDecision",
    "SoleApproverExceptionGrant",
    "SoleApproverExceptionPolicy",
    "SoleApproverRefusal",
    "authorize_sole_approver",
    "evaluate_sole_approver_exception",
    "load_sole_approver_exception_policy",
    "stage_sole_approver_exception_audit",
]
