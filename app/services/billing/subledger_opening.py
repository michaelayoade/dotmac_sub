"""Finance-approved opening positions for ADR 0007 customer-subledger cutover.

The verifier records an immutable cohort proposal first. This migration owner
may capture only that exact, separately operator- and finance-approved result.
Each account/currency residual and its posting group share one owner command;
the complete cohort must be source-valid before capture. Existing immutable
openings are preserved while a later completion run adds only missing rows.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing_contract import BillingRecordAuthority
from app.models.billing_shadow_verification import BillingCutoverVerificationRun
from app.models.customer_subledger import (
    CustomerPostingGroup,
    CustomerSubledgerAuthorityCutover,
    CustomerSubledgerOpeningCorrection,
    CustomerSubledgerOpeningPosition,
    NativePrepaidOpeningRepair,
    PositionEffectKind,
    PostingCommandKind,
    PostingProducer,
    PostingSourceKind,
)
from app.models.prepaid_funding import (
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.splynx_transaction import SplynxBillingTransaction
from app.models.subscriber import Subscriber
from app.models.system_user import SystemUser
from app.schemas.audit import AuditEventCreate
from app.services.audit import AuditEvents
from app.services.auth_dependencies import has_permission
from app.services.billing.customer_subledger import (
    EffectInput,
    StagePostingGroupCommand,
    resolve_position_evidence_at,
    stage_posting_group,
)
from app.services.billing.opening_balance_history import (
    OpeningBalanceSourceIdentityDisposition,
    OpeningBalanceSourceIdentityQuery,
    classify_opening_balance_source_identities,
)
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.system_user_assignments import system_user_role_names


def _object_dict(value: object) -> dict[str, object]:
    """Narrow persisted JSON before using it as command evidence."""

    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _object_dict_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [_object_dict(item) for item in value if isinstance(item, dict)]


OWNER = "financial.customer_subledger_opening_positions"
CONCERN = "reviewed customer-subledger opening-position capture"
CORRECTION_SCOPE = "billing:customer_subledger_opening:correct"
NATIVE_REPAIR_SCOPE = "billing:prepaid_funding:native_opening_repair"
_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern=CONCERN,
    name="capture_customer_subledger_opening_positions",
)
_CUTOVER_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="customer-subledger authority cutover activation",
    name="activate_customer_subledger_authority",
)
_CORRECTION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="reviewed customer-subledger opening-position correction",
    name="correct_customer_subledger_opening_position",
)
_NATIVE_REPAIR_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="account-scoped native prepaid opening repair",
    name="repair_native_prepaid_opening",
)


class CustomerSubledgerOpeningError(DomainError):
    """Fail-closed opening-position migration error."""


def _error(
    suffix: str, message: str, **details: object
) -> CustomerSubledgerOpeningError:
    return CustomerSubledgerOpeningError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CaptureCustomerSubledgerOpeningsCommand:
    """Exact approved verifier result to materialize as opening groups."""

    context: CommandContext
    verification_run_id: UUID
    expected_result_fingerprint: str
    review_reference: str


@dataclass(frozen=True, slots=True)
class CustomerSubledgerOpeningCaptureResult:
    verification_run_id: UUID
    captured_count: int
    zero_count: int
    positive_total: Decimal
    negative_total: Decimal
    replayed: bool


@dataclass(frozen=True, slots=True)
class ActivateCustomerSubledgerAuthorityCommand:
    """Exact approved zero-blocker parity run authorising read/write cutover."""

    context: CommandContext
    verification_run_id: UUID
    expected_result_fingerprint: str
    review_reference: str


@dataclass(frozen=True, slots=True)
class CustomerSubledgerAuthorityResult:
    cutover_id: UUID
    verification_run_id: UUID
    cutover_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class PreviewCustomerSubledgerOpeningCorrectionQuery:
    account_id: UUID
    currency: str
    corrected_opening_amount: Decimal
    reason: str
    review_reference: str


@dataclass(frozen=True, slots=True)
class CustomerSubledgerOpeningCorrectionPreview:
    opening_position_id: UUID
    account_id: UUID
    currency: str
    previous_opening_amount: Decimal
    corrected_opening_amount: Decimal
    delta: Decimal
    preview_fingerprint: str


@dataclass(frozen=True, slots=True)
class CorrectCustomerSubledgerOpeningCommand:
    context: CommandContext
    query: PreviewCustomerSubledgerOpeningCorrectionQuery
    expected_preview_fingerprint: str
    permission_granted: bool
    authorized_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class CustomerSubledgerOpeningCorrectionResult:
    correction_id: UUID
    posting_group_id: UUID
    previous_opening_amount: Decimal
    corrected_opening_amount: Decimal
    delta: Decimal
    replayed: bool


@dataclass(frozen=True, slots=True)
class NativePrepaidOpeningApproval:
    finance_approver_system_user_id: UUID
    finance_approver_name: str
    approved_at: datetime
    ticket_reference: str
    evidence_ref: str
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class PreviewNativePrepaidOpeningRepairQuery:
    account_id: UUID
    approval: NativePrepaidOpeningApproval
    currency: str = "NGN"


@dataclass(frozen=True, slots=True)
class NativePrepaidOpeningRepairPreview:
    account_id: UUID
    currency: str
    account_created_at: datetime
    legacy_handoff_at: datetime
    original_cutover_batch_id: UUID
    original_cutover_at: datetime
    cutover_evidence_fingerprint: str
    source_classification: str
    splynx_transaction_count: int
    calculated_amount: Decimal
    native_event_count: int
    shadow_position_before: Decimal
    opening_delta: Decimal
    source_identity_fingerprint: str
    native_evidence_fingerprint: str
    shadow_evidence_fingerprint: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RepairNativePrepaidOpeningCommand:
    context: CommandContext
    query: PreviewNativePrepaidOpeningRepairQuery
    expected_preview_fingerprint: str
    operator_system_user_id: UUID


@dataclass(frozen=True, slots=True)
class NativePrepaidOpeningRepairResult:
    repair_id: UUID
    opening_position_id: UUID
    posting_group_id: UUID
    account_id: UUID
    currency: str
    calculated_amount: Decimal
    original_cutover_batch_id: UUID
    original_cutover_at: datetime
    preview_fingerprint: str
    replayed: bool


def _canonical_system_user_name(user: SystemUser) -> str:
    return (user.display_name or f"{user.first_name} {user.last_name}").strip()


def _validate_native_repair_approval(
    db: Session, approval: NativePrepaidOpeningApproval
) -> SystemUser:
    digest = approval.evidence_sha256.strip()
    reference = approval.evidence_ref.strip()
    ticket = approval.ticket_reference.strip()
    if approval.approved_at.tzinfo is None:
        raise _error(
            "invalid_finance_approval",
            "Finance approval time must include a timezone.",
        )
    if not ticket or len(ticket) > 120:
        raise _error(
            "invalid_finance_approval",
            "Finance approval requires a bounded ticket reference.",
        )
    if (
        not reference
        or len(reference) > 500
        or any(
            marker in reference.casefold()
            for marker in (
                "password=",
                "token=",
                "access_token=",
                "secret=",
                "api_key=",
                "apikey=",
                "authorization=",
            )
        )
        or reference.casefold().startswith(("bao://", "env://"))
    ):
        raise _error(
            "invalid_finance_approval",
            "Finance approval requires a bounded non-secret evidence reference.",
        )
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise _error(
            "invalid_finance_approval",
            "Finance approval evidence digest must be lowercase SHA-256 hex.",
        )
    approver = db.get(
        SystemUser,
        approval.finance_approver_system_user_id,
        populate_existing=True,
    )
    if (
        approver is None
        or not approver.is_active
        or _canonical_system_user_name(approver).casefold()
        != approval.finance_approver_name.strip().casefold()
    ):
        raise _error(
            "invalid_finance_approval",
            "Finance approver identity is inactive or does not match.",
        )
    return approver


def _verify_native_repair_operator(db: Session, system_user_id: UUID) -> SystemUser:
    user = lock_for_update(db, SystemUser, system_user_id)
    if user is None or not user.is_active:
        raise _error(
            "permission_denied",
            "Native opening repair requires an active staff principal.",
        )
    granted = has_permission(
        {
            "principal_id": str(system_user_id),
            "principal_type": "system_user",
            "roles": set(system_user_role_names(db, system_user_id)),
        },
        db,
        NATIVE_REPAIR_SCOPE,
    )
    if not granted:
        raise _error(
            "permission_denied",
            f"Native opening repair requires {NATIVE_REPAIR_SCOPE}.",
        )
    return user


def preview_native_prepaid_opening_repair(
    db: Session,
    query: PreviewNativePrepaidOpeningRepairQuery,
) -> NativePrepaidOpeningRepairPreview:
    """Calculate one omitted native opening from canonical Sub facts only."""

    from app.services.customer_financial_ledger import (
        native_customer_financial_position_evidence,
    )
    from app.services.prepaid_enforcement_planner import (
        candidate_prepaid_funding_account_ids,
    )
    from app.services.prepaid_funding_reconstruction import (
        LEGACY_FINANCIAL_HANDOFF_AT,
        authority_cutover_batch,
    )

    unit = query.currency.strip().upper()
    if len(unit) != 3 or not unit.isalpha():
        raise _error("invalid_currency", "Native opening currency is invalid.")
    _validate_native_repair_approval(db, query.approval)
    account = db.get(Subscriber, query.account_id, populate_existing=True)
    if account is None:
        raise _error(
            "account_not_found",
            "The selected prepaid account does not exist.",
            account_id=str(query.account_id),
        )
    created_at = _utc(account.created_at)
    cutover = authority_cutover_batch(db)
    if cutover is None:
        raise _error(
            "authority_not_active",
            "Prepaid funding authority cutover evidence is missing.",
        )
    cutover_at = _utc(cutover.position_at)
    cutover_evidence_fingerprint = _digest(
        {
            "id": str(cutover.id),
            "manifest_sha256": cutover.manifest_sha256,
            "manifest_payload_sha256": cutover.manifest_payload_sha256,
            "attestation_sha256": cutover.attestation_sha256,
            "attestation_key_fingerprint_sha256": (
                cutover.attestation_key_fingerprint_sha256
            ),
            "attestation_signed_at": _utc(cutover.attestation_signed_at).isoformat(),
            "blocker_manifest_sha256": cutover.blocker_manifest_sha256,
            "candidate_cohort_sha256": cutover.candidate_cohort_sha256,
            "source": cutover.source,
            "evidence_ref": cutover.evidence_ref,
            "position_at": cutover_at.isoformat(),
            "currency": cutover.currency,
            "account_count": cutover.account_count,
            "total_amount": str(cutover.total_amount),
            "approved_by": cutover.approved_by,
            "approved_at": _utc(cutover.approved_at).isoformat(),
        }
    )
    if created_at <= LEGACY_FINANCIAL_HANDOFF_AT:
        raise _error(
            "account_not_native_after_handoff",
            "Account creation does not prove native-after-handoff provenance.",
        )
    if created_at > cutover_at:
        raise _error(
            "account_not_in_original_cutover",
            "Account did not exist at the prepaid funding authority cutover.",
        )
    if query.account_id not in candidate_prepaid_funding_account_ids(db):
        raise _error(
            "account_not_in_funding_cohort",
            "Account is not in the applicable prepaid funding cohort.",
        )
    identity = classify_opening_balance_source_identities(
        db,
        OpeningBalanceSourceIdentityQuery(
            account_ids=(query.account_id,),
            native_after=LEGACY_FINANCIAL_HANDOFF_AT,
            position_at=cutover_at,
        ),
    )
    identity_row = identity.rows[0]
    if (
        identity_row.disposition
        is not OpeningBalanceSourceIdentityDisposition.native_after_handoff
        or identity_row.splynx_customer_id is not None
    ):
        raise _error(
            "splynx_identity_present",
            "Account has carried-source identity evidence and is outside this repair.",
        )
    splynx_count = int(
        db.scalar(
            select(func.count(SplynxBillingTransaction.id)).where(
                SplynxBillingTransaction.subscriber_id == query.account_id
            )
        )
        or 0
    )
    if splynx_count:
        raise _error(
            "splynx_transactions_present",
            "Account has carried-source transaction evidence and is outside this repair.",
        )
    baseline = db.scalar(
        select(PrepaidFundingBaseline.id).where(
            PrepaidFundingBaseline.account_id == query.account_id,
            PrepaidFundingBaseline.currency == unit,
            PrepaidFundingBaseline.is_active.is_(True),
        )
    )
    if baseline is not None:
        raise _error(
            "funding_baseline_already_exists",
            "Account already has an active prepaid funding baseline.",
        )
    opening = db.scalar(
        select(CustomerSubledgerOpeningPosition.id).where(
            CustomerSubledgerOpeningPosition.account_id == query.account_id,
            CustomerSubledgerOpeningPosition.currency == unit,
        )
    )
    if opening is not None:
        raise _error(
            "opening_position_already_captured",
            "Account already has an immutable opening position.",
        )
    authority = db.scalar(select(CustomerSubledgerAuthorityCutover).limit(1))
    if authority is None:
        raise _error(
            "authority_not_active",
            "Customer-subledger authority must be active before repair.",
        )
    try:
        native = native_customer_financial_position_evidence(
            db,
            query.account_id,
            currency=unit,
            after=LEGACY_FINANCIAL_HANDOFF_AT,
            before=cutover_at,
        )
    except RuntimeError as exc:
        raise _error(
            "native_evidence_incomplete",
            "Canonical Sub-native financial evidence is not exactly reconstructable.",
        ) from exc
    shadow = resolve_position_evidence_at(
        db,
        account_id=query.account_id,
        currency=unit,
        authority=BillingRecordAuthority.shadow,
        as_of=cutover_at,
    )
    shadow_position = round_money(
        shadow.position.unapplied_customer_credit
        + shadow.position.prepaid_funding_reserved
    )
    opening_delta = round_money(native.amount - shadow_position)
    payload = {
        "account_id": str(query.account_id),
        "account_created_at": created_at.isoformat(),
        "legacy_handoff_at": LEGACY_FINANCIAL_HANDOFF_AT.isoformat(),
        "original_cutover_batch_id": str(cutover.id),
        "original_cutover_at": cutover_at.isoformat(),
        "cutover_evidence_fingerprint": cutover_evidence_fingerprint,
        "currency": unit,
        "source_classification": identity_row.disposition.value,
        "source_identity_fingerprint": identity_row.evidence_fingerprint,
        "splynx_customer_id": None,
        "splynx_transaction_count": splynx_count,
        "active_baseline_id": None,
        "opening_position_id": None,
        "calculated_amount": str(native.amount),
        "native_event_count": native.event_count,
        "native_evidence_fingerprint": native.fingerprint,
        "shadow_position_before": str(shadow_position),
        "shadow_evidence_fingerprint": shadow.fingerprint,
        "opening_delta": str(opening_delta),
        "funding_cohort_member": True,
        "finance_approver_system_user_id": str(
            query.approval.finance_approver_system_user_id
        ),
        "finance_approver_name": query.approval.finance_approver_name.strip(),
        "approved_at": _utc(query.approval.approved_at).isoformat(),
        "ticket_reference": query.approval.ticket_reference.strip(),
        "evidence_ref": query.approval.evidence_ref.strip(),
        "evidence_sha256": query.approval.evidence_sha256.strip(),
    }
    return NativePrepaidOpeningRepairPreview(
        account_id=query.account_id,
        currency=unit,
        account_created_at=created_at,
        legacy_handoff_at=LEGACY_FINANCIAL_HANDOFF_AT,
        original_cutover_batch_id=cutover.id,
        original_cutover_at=cutover_at,
        cutover_evidence_fingerprint=cutover_evidence_fingerprint,
        source_classification=identity_row.disposition.value,
        splynx_transaction_count=splynx_count,
        calculated_amount=native.amount,
        native_event_count=native.event_count,
        shadow_position_before=shadow_position,
        opening_delta=opening_delta,
        source_identity_fingerprint=identity_row.evidence_fingerprint,
        native_evidence_fingerprint=native.fingerprint,
        shadow_evidence_fingerprint=shadow.fingerprint,
        fingerprint=_digest(payload),
    )


def _native_repair_result(
    db: Session,
    repair: NativePrepaidOpeningRepair,
    *,
    replayed: bool,
) -> NativePrepaidOpeningRepairResult:
    opening = db.scalar(
        select(CustomerSubledgerOpeningPosition).where(
            CustomerSubledgerOpeningPosition.native_repair_id == repair.id
        )
    )
    if opening is None:
        raise _error(
            "native_repair_incomplete",
            "Native opening repair has no immutable opening position.",
        )
    posting_id = db.scalar(
        select(CustomerPostingGroup.id).where(
            CustomerPostingGroup.producer_owner
            == PostingProducer.customer_subledger_opening_positions.value,
            CustomerPostingGroup.source_kind
            == PostingSourceKind.customer_subledger_opening_position.value,
            CustomerPostingGroup.source_id == opening.id,
        )
    )
    if posting_id is None:
        raise _error(
            "native_repair_incomplete",
            "Native opening repair has no matching customer posting.",
        )
    return NativePrepaidOpeningRepairResult(
        repair_id=repair.id,
        opening_position_id=opening.id,
        posting_group_id=posting_id,
        account_id=repair.account_id,
        currency=repair.currency,
        calculated_amount=round_money(Decimal(repair.calculated_amount)),
        original_cutover_batch_id=repair.original_cutover_batch_id,
        original_cutover_at=_utc(repair.original_cutover_at),
        preview_fingerprint=repair.preview_fingerprint,
        replayed=replayed,
    )


def repair_native_prepaid_opening(
    db: Session,
    command: RepairNativePrepaidOpeningCommand,
) -> NativePrepaidOpeningRepairResult:
    """Append one permissioned native omission repair and opening atomically."""

    return execute_owner_command(
        db,
        definition=_NATIVE_REPAIR_COMMAND,
        context=command.context,
        operation=lambda: _repair_native_prepaid_opening(db, command),
    )


def _repair_native_prepaid_opening(
    db: Session,
    command: RepairNativePrepaidOpeningCommand,
) -> NativePrepaidOpeningRepairResult:
    if command.context.scope != NATIVE_REPAIR_SCOPE:
        raise _error("permission_denied", "Native opening repair scope is invalid.")
    operator = _verify_native_repair_operator(db, command.operator_system_user_id)
    if command.context.actor != f"system_user:{command.operator_system_user_id}":
        raise _error(
            "permission_denied",
            "Repair actor must match the authenticated operator identity.",
        )
    key = (command.context.idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise _error(
            "invalid_idempotency_key",
            "Native opening repair requires a bounded idempotency key.",
        )
    expected = command.expected_preview_fingerprint.strip()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise _error(
            "invalid_result_fingerprint",
            "Native opening repair requires the exact lowercase preview SHA-256.",
        )
    account = lock_for_update(db, Subscriber, command.query.account_id)
    if account is None:
        raise _error("account_not_found", "The selected prepaid account is missing.")
    approver = lock_for_update(
        db,
        SystemUser,
        command.query.approval.finance_approver_system_user_id,
    )
    if approver is None:
        raise _error(
            "invalid_finance_approval",
            "Finance approver identity is inactive or does not match.",
        )
    existing = db.scalar(
        select(NativePrepaidOpeningRepair)
        .where(NativePrepaidOpeningRepair.idempotency_key == key)
        .with_for_update()
    )
    if existing is not None:
        if (
            existing.account_id != command.query.account_id
            or existing.preview_fingerprint != expected
            or existing.operator_system_user_id != command.operator_system_user_id
            or existing.reason != command.context.reason.strip()
        ):
            raise _error(
                "idempotency_conflict",
                "Idempotency key belongs to different native repair evidence.",
            )
        return _native_repair_result(db, existing, replayed=True)

    # Lock authority and competing source records before recomputing evidence.
    db.scalar(
        select(PrepaidFundingReconstructionBatch)
        .where(PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True))
        .with_for_update()
    )
    db.scalar(select(CustomerSubledgerAuthorityCutover).with_for_update())
    db.scalars(
        select(PrepaidFundingBaseline)
        .where(PrepaidFundingBaseline.account_id == command.query.account_id)
        .with_for_update()
    ).all()
    db.scalars(
        select(CustomerSubledgerOpeningPosition)
        .where(CustomerSubledgerOpeningPosition.account_id == command.query.account_id)
        .with_for_update()
    ).all()
    db.scalars(
        select(SplynxBillingTransaction)
        .where(SplynxBillingTransaction.subscriber_id == command.query.account_id)
        .with_for_update()
    ).all()
    preview = preview_native_prepaid_opening_repair(db, command.query)
    if preview.fingerprint != expected:
        raise _error(
            "stale_reviewed_preview",
            "Native opening evidence changed after preview; preview again.",
        )
    approval = command.query.approval
    occurred_at = datetime.now(UTC)
    repair = NativePrepaidOpeningRepair(
        account_id=preview.account_id,
        original_cutover_batch_id=preview.original_cutover_batch_id,
        currency=preview.currency,
        source_classification=preview.source_classification,
        account_created_at=preview.account_created_at,
        legacy_handoff_at=preview.legacy_handoff_at,
        original_cutover_at=preview.original_cutover_at,
        calculated_amount=preview.calculated_amount,
        splynx_transaction_count=preview.splynx_transaction_count,
        native_event_count=preview.native_event_count,
        cutover_evidence_fingerprint=preview.cutover_evidence_fingerprint,
        source_identity_fingerprint=preview.source_identity_fingerprint,
        native_evidence_fingerprint=preview.native_evidence_fingerprint,
        shadow_evidence_fingerprint=preview.shadow_evidence_fingerprint,
        preview_fingerprint=preview.fingerprint,
        finance_approver_system_user_id=approval.finance_approver_system_user_id,
        finance_approver_name=approval.finance_approver_name.strip(),
        approved_at=_utc(approval.approved_at),
        ticket_reference=approval.ticket_reference.strip(),
        evidence_ref=approval.evidence_ref.strip(),
        evidence_sha256=approval.evidence_sha256.strip(),
        operator_system_user_id=command.operator_system_user_id,
        reason=command.context.reason.strip(),
        idempotency_key=key,
        applied_by=command.context.actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        occurred_at=occurred_at,
    )
    db.add(repair)
    db.flush()
    opening = CustomerSubledgerOpeningPosition(
        verification_run_id=None,
        native_repair_id=repair.id,
        baseline_id=None,
        account_id=preview.account_id,
        currency=preview.currency,
        legacy_position=preview.calculated_amount,
        shadow_position_before=preview.shadow_position_before,
        opening_delta=preview.opening_delta,
        evidence_fingerprint=preview.fingerprint,
        review_reference=approval.evidence_ref.strip(),
        captured_by=command.context.actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        occurred_at=preview.original_cutover_at,
    )
    db.add(opening)
    db.flush()
    effects: tuple[EffectInput, ...] = ()
    if preview.opening_delta > 0:
        effects = (
            EffectInput(
                effect=PositionEffectKind.customer_credit_created,
                amount=preview.opening_delta,
            ),
        )
    elif preview.opening_delta < 0:
        effects = (
            EffectInput(
                effect=PositionEffectKind.customer_credit_consumed,
                amount=abs(preview.opening_delta),
            ),
        )
    group = stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=preview.account_id,
            currency=preview.currency,
            command_kind=PostingCommandKind.opening_position,
            producer_owner=PostingProducer.customer_subledger_opening_positions,
            source_kind=PostingSourceKind.customer_subledger_opening_position,
            source_id=opening.id,
            occurred_at=preview.original_cutover_at,
            effects=effects,
            idempotency_key=(
                f"posting:customer_subledger_opening:{preview.account_id}:"
                f"{preview.currency}"
            ),
        ),
        context=command.context,
    )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=str(command.operator_system_user_id),
            actor_label=_canonical_system_user_name(operator),
            action="repair_native_prepaid_opening",
            entity_type="native_prepaid_opening_repair",
            entity_id=str(repair.id),
            metadata_={
                "account_id": str(preview.account_id),
                "opening_position_id": str(opening.id),
                "posting_group_id": str(group.id),
                "original_cutover_batch_id": str(preview.original_cutover_batch_id),
                "original_cutover_at": preview.original_cutover_at.isoformat(),
                "currency": preview.currency,
                "calculated_amount": str(preview.calculated_amount),
                "source_classification": preview.source_classification,
                "cutover_evidence_fingerprint": (preview.cutover_evidence_fingerprint),
                "source_identity_fingerprint": preview.source_identity_fingerprint,
                "native_evidence_fingerprint": preview.native_evidence_fingerprint,
                "shadow_evidence_fingerprint": preview.shadow_evidence_fingerprint,
                "preview_fingerprint": preview.fingerprint,
                "finance_approver_system_user_id": str(
                    approval.finance_approver_system_user_id
                ),
                "approved_at": _utc(approval.approved_at).isoformat(),
                "ticket_reference": approval.ticket_reference.strip(),
                "evidence_ref": approval.evidence_ref.strip(),
                "evidence_sha256": approval.evidence_sha256.strip(),
                "command_id": str(command.context.command_id),
                "correlation_id": str(command.context.correlation_id),
            },
        ),
    )
    emit_event(
        db,
        EventType.native_prepaid_opening_repaired,
        {
            "schema_version": 1,
            "repair_id": str(repair.id),
            "opening_position_id": str(opening.id),
            "posting_group_id": str(group.id),
            "account_id": str(preview.account_id),
            "currency": preview.currency,
            "calculated_amount": str(preview.calculated_amount),
            "original_cutover_batch_id": str(preview.original_cutover_batch_id),
            "original_cutover_at": preview.original_cutover_at.isoformat(),
            "source_classification": preview.source_classification,
            "cutover_evidence_fingerprint": preview.cutover_evidence_fingerprint,
            "source_identity_fingerprint": preview.source_identity_fingerprint,
            "native_evidence_fingerprint": preview.native_evidence_fingerprint,
            "shadow_evidence_fingerprint": preview.shadow_evidence_fingerprint,
            "preview_fingerprint": preview.fingerprint,
            "ticket_reference": approval.ticket_reference.strip(),
        },
        actor=command.context.actor,
    )
    db.flush()
    return _native_repair_result(db, repair, replayed=False)


def capture_customer_subledger_opening_positions(
    db: Session,
    command: CaptureCustomerSubledgerOpeningsCommand,
) -> CustomerSubledgerOpeningCaptureResult:
    """Capture exactly one approved opening residual per eligible account."""

    return execute_owner_command(
        db,
        definition=_CAPTURE_COMMAND,
        context=command.context,
        operation=lambda: _capture(db, command),
    )


def _capture(
    db: Session,
    command: CaptureCustomerSubledgerOpeningsCommand,
) -> CustomerSubledgerOpeningCaptureResult:
    if not command.context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Opening-position capture requires an idempotency key.",
        )
    expected = command.expected_result_fingerprint.strip().lower()
    if len(expected) != 64:
        raise _error(
            "invalid_result_fingerprint",
            "Opening-position result fingerprint must be a SHA-256 digest.",
        )
    reference = command.review_reference.strip()
    if not reference:
        raise _error(
            "missing_review_reference",
            "Opening-position capture requires a durable review reference.",
        )
    run = lock_for_update(
        db, BillingCutoverVerificationRun, command.verification_run_id
    )
    if run is None or run.phase not in {
        "phase_3_opening_preview",
        "phase_3_post_cutover_opening_preview",
        "phase_3_migrated_opening_preview",
    }:
        raise _error(
            "verification_run_not_found",
            "The approved Phase 3 opening preview does not exist.",
            run_id=str(command.verification_run_id),
        )
    if run.result_fingerprint != expected:
        raise _error(
            "stale_reviewed_preview",
            "The supplied result fingerprint is not the reviewed preview.",
            run_id=str(run.id),
        )
    if not run.approved:
        raise _error(
            "approval_required",
            "Opening capture requires operator and finance approval on a clean run.",
            run_id=str(run.id),
        )
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    rows = _object_dict_rows(details.get("opening_rows"))
    result_contract = _object_dict(details.get("opening_result_contract"))
    fingerprint_payload: object = result_contract or rows
    contract_rows = _object_dict_rows(result_contract.get("opening_rows"))
    if _digest(fingerprint_payload) != run.result_fingerprint or (
        result_contract and contract_rows != rows
    ):
        raise _error(
            "corrupt_reviewed_preview",
            "Stored opening evidence no longer matches its immutable fingerprint.",
            run_id=str(run.id),
        )
    currency = str((run.currency_totals or {}).get("currency") or "").upper()
    if len(currency) != 3:
        raise _error(
            "corrupt_reviewed_preview",
            "Stored opening preview has no valid currency.",
            run_id=str(run.id),
        )

    existing = list(
        db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.verification_run_id == run.id
            )
        ).all()
    )
    if existing:
        expected_rows = {
            (UUID(str(row["account_id"])), str(row["evidence_fingerprint"]))
            for row in rows
        }
        recorded_rows = {(row.account_id, row.evidence_fingerprint) for row in existing}
        if recorded_rows != expected_rows:
            raise _error(
                "idempotency_conflict",
                "Recorded opening positions differ from the reviewed result.",
                run_id=str(run.id),
            )
        return _result(run.id, existing, replayed=True)

    if run.phase == "phase_3_post_cutover_opening_preview":
        if len(rows) != 1:
            raise _error(
                "corrupt_reviewed_preview",
                "A post-cutover account preview must contain exactly one opening.",
                run_id=str(run.id),
            )
        payload = rows[0]
        account_id = UUID(str(payload["account_id"]))
        if lock_for_update(db, Subscriber, account_id) is None:
            raise _error(
                "stale_reviewed_preview",
                "The reviewed account no longer exists.",
                account_id=str(account_id),
            )
        authority_cutover_id = details.get("authority_cutover_id")
        if authority_cutover_id is None:
            raise _error(
                "corrupt_reviewed_preview",
                "Post-cutover opening evidence has no authority identity.",
                run_id=str(run.id),
            )
        from app.services.billing.shadow_verification import (
            BillingShadowVerificationError,
            ResolvePostCutoverOpeningEvidenceQuery,
            resolve_post_cutover_opening_evidence,
        )

        try:
            current = resolve_post_cutover_opening_evidence(
                db,
                ResolvePostCutoverOpeningEvidenceQuery(
                    account_id=account_id,
                    currency=currency,
                    expected_authority_cutover_id=UUID(str(authority_cutover_id)),
                ),
            )
        except BillingShadowVerificationError as exc:
            raise _error(
                "stale_reviewed_preview",
                "The selected account no longer matches its reviewed opening evidence.",
                account_id=str(account_id),
                run_id=str(run.id),
                cause_code=exc.code,
            ) from exc
        if (
            str(payload.get("evidence_fingerprint")) != current.evidence_fingerprint
            or round_money(Decimal(str(payload.get("legacy_position"))))
            != current.legacy_position
            or round_money(Decimal(str(payload.get("shadow_position_before"))))
            != current.shadow_position_before
            or round_money(Decimal(str(payload.get("opening_delta"))))
            != current.opening_delta
            or _utc(run.cutoff_at) != current.opening_cutoff_at
        ):
            raise _error(
                "stale_reviewed_preview",
                "The selected account's opening evidence changed after approval.",
                account_id=str(account_id),
                run_id=str(run.id),
            )
    elif run.phase == "phase_3_migrated_opening_preview":
        if len(rows) != 1:
            raise _error(
                "corrupt_reviewed_preview",
                "A migrated-account preview must contain exactly one opening.",
                run_id=str(run.id),
            )
        payload = rows[0]
        account_id = UUID(str(payload["account_id"]))
        if lock_for_update(db, Subscriber, account_id) is None:
            raise _error(
                "stale_reviewed_preview",
                "The reviewed account no longer exists.",
                account_id=str(account_id),
            )
        authority_cutover_id = details.get("authority_cutover_id")
        if authority_cutover_id is None:
            raise _error(
                "corrupt_reviewed_preview",
                "Migrated opening evidence has no authority identity.",
                run_id=str(run.id),
            )
        try:
            source_position_at = datetime.fromisoformat(
                str(payload["opening_target_source_position_at"])
            )
            source_evidence_ref = str(payload["source_evidence_ref"])
            source_evidence_sha256 = str(payload["source_evidence_sha256"])
            legacy_position = round_money(Decimal(str(payload["legacy_position"])))
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise _error(
                "corrupt_reviewed_preview",
                "Migrated opening evidence has invalid reviewed source fields.",
                run_id=str(run.id),
            ) from exc
        from app.services.billing.shadow_verification import (
            BillingShadowVerificationError,
            ResolvePostCutoverMigratedOpeningEvidenceQuery,
            ReviewedMigratedOpeningSource,
            resolve_post_cutover_migrated_opening_evidence,
        )

        try:
            current_migrated = resolve_post_cutover_migrated_opening_evidence(
                db,
                ResolvePostCutoverMigratedOpeningEvidenceQuery(
                    account_id=account_id,
                    currency=currency,
                    expected_authority_cutover_id=UUID(str(authority_cutover_id)),
                    source=ReviewedMigratedOpeningSource(
                        position_at=source_position_at,
                        legacy_position=legacy_position,
                        evidence_ref=source_evidence_ref,
                        evidence_sha256=source_evidence_sha256,
                    ),
                ),
            )
        except BillingShadowVerificationError as exc:
            raise _error(
                "stale_reviewed_preview",
                "The migrated account no longer matches its reviewed evidence.",
                account_id=str(account_id),
                run_id=str(run.id),
                cause_code=exc.code,
            ) from exc
        if (
            str(payload.get("evidence_fingerprint"))
            != current_migrated.evidence_fingerprint
            or legacy_position != current_migrated.legacy_position
            or round_money(Decimal(str(payload.get("shadow_position_before"))))
            != current_migrated.shadow_position_before
            or round_money(Decimal(str(payload.get("opening_delta"))))
            != current_migrated.opening_delta
            or _utc(run.cutoff_at) != current_migrated.opening_cutoff_at
        ):
            raise _error(
                "stale_reviewed_preview",
                "The migrated account's opening evidence changed after approval.",
                account_id=str(account_id),
                run_id=str(run.id),
            )

    account_ids = tuple(UUID(str(row["account_id"])) for row in rows)
    conflicting = list(
        db.scalars(
            select(CustomerSubledgerOpeningPosition).where(
                CustomerSubledgerOpeningPosition.account_id.in_(account_ids),
                CustomerSubledgerOpeningPosition.currency == currency,
            )
        ).all()
    )
    if conflicting:
        raise _error(
            "opening_position_already_captured",
            "An account already has an immutable opening position.",
            account_count=len(conflicting),
        )

    captured: list[CustomerSubledgerOpeningPosition] = []
    for payload in sorted(rows, key=lambda item: str(item["account_id"])):
        account_id = UUID(str(payload["account_id"]))
        legacy = round_money(Decimal(str(payload["legacy_position"])))
        shadow = round_money(Decimal(str(payload["shadow_position_before"])))
        delta = round_money(Decimal(str(payload["opening_delta"])))
        if delta != round_money(legacy - shadow):
            raise _error(
                "corrupt_reviewed_preview",
                "Opening residual no longer equals legacy minus shadow.",
                account_id=str(account_id),
            )
        evidence_fingerprint = str(payload["evidence_fingerprint"])
        if len(evidence_fingerprint) != 64:
            raise _error(
                "corrupt_reviewed_preview",
                "Opening row has an invalid evidence fingerprint.",
                account_id=str(account_id),
            )
        baseline_id = payload.get("baseline_id")
        opening = CustomerSubledgerOpeningPosition(
            verification_run_id=run.id,
            baseline_id=UUID(str(baseline_id)) if baseline_id else None,
            account_id=account_id,
            currency=currency,
            legacy_position=legacy,
            shadow_position_before=shadow,
            opening_delta=delta,
            evidence_fingerprint=evidence_fingerprint,
            review_reference=reference,
            captured_by=command.context.actor,
            command_id=command.context.command_id,
            correlation_id=command.context.correlation_id,
            occurred_at=_utc(run.cutoff_at),
        )
        db.add(opening)
        db.flush()
        effects: tuple[EffectInput, ...]
        if delta > 0:
            effects = (
                EffectInput(
                    effect=PositionEffectKind.customer_credit_created,
                    amount=delta,
                ),
            )
        elif delta < 0:
            effects = (
                EffectInput(
                    effect=PositionEffectKind.customer_credit_consumed,
                    amount=abs(delta),
                ),
            )
        else:
            effects = ()
        stage_posting_group(
            db,
            StagePostingGroupCommand(
                account_id=account_id,
                currency=currency,
                command_kind=PostingCommandKind.opening_position,
                producer_owner=PostingProducer.customer_subledger_opening_positions,
                source_kind=PostingSourceKind.customer_subledger_opening_position,
                source_id=opening.id,
                occurred_at=_utc(run.cutoff_at),
                effects=effects,
                idempotency_key=(
                    f"posting:customer_subledger_opening:{account_id}:{currency}"
                ),
            ),
            context=command.context,
        )
        captured.append(opening)

    captured_quarantine = details.get("quarantined_accounts")
    emit_event(
        db,
        EventType.customer_subledger_opening_positions_captured,
        {
            "verification_run_id": str(run.id),
            "result_fingerprint": run.result_fingerprint,
            "currency": currency,
            "captured_count": len(captured),
            "quarantined_count": (
                len(captured_quarantine) if isinstance(captured_quarantine, list) else 0
            ),
            "authority_moved": False,
        },
        actor=command.context.actor,
    )
    return _result(run.id, captured, replayed=False)


def _result(
    run_id: UUID,
    rows: list[CustomerSubledgerOpeningPosition],
    *,
    replayed: bool,
) -> CustomerSubledgerOpeningCaptureResult:
    positive = sum(
        (Decimal(row.opening_delta) for row in rows if row.opening_delta > 0),
        Decimal("0"),
    )
    negative = sum(
        (abs(Decimal(row.opening_delta)) for row in rows if row.opening_delta < 0),
        Decimal("0"),
    )
    return CustomerSubledgerOpeningCaptureResult(
        verification_run_id=run_id,
        captured_count=len(rows),
        zero_count=sum(Decimal(row.opening_delta) == 0 for row in rows),
        positive_total=round_money(positive),
        negative_total=round_money(negative),
        replayed=replayed,
    )


def preview_customer_subledger_opening_correction(
    db: Session,
    query: PreviewCustomerSubledgerOpeningCorrectionQuery,
) -> CustomerSubledgerOpeningCorrectionPreview:
    """Preview one explicit replacement value without changing any records."""

    currency = query.currency.strip().upper()
    reason = query.reason.strip()
    review_reference = query.review_reference.strip()
    if len(currency) != 3:
        raise _error("invalid_currency", "Opening correction requires a currency.")
    if not reason:
        raise _error("missing_reason", "Opening correction requires a reason.")
    if not review_reference:
        raise _error(
            "missing_review_reference",
            "Opening correction requires a durable review reference.",
        )
    corrected = round_money(Decimal(query.corrected_opening_amount))
    if not corrected.is_finite():
        raise _error(
            "invalid_corrected_amount", "Corrected opening amount must be finite."
        )
    if db.scalar(select(CustomerSubledgerAuthorityCutover.id).limit(1)) is None:
        raise _error(
            "authority_not_active",
            "Opening corrections require active customer-subledger authority.",
        )
    opening = db.scalar(
        select(CustomerSubledgerOpeningPosition).where(
            CustomerSubledgerOpeningPosition.account_id == query.account_id,
            CustomerSubledgerOpeningPosition.currency == currency,
        )
    )
    if opening is None:
        raise _error(
            "opening_position_not_found",
            "The account has no immutable opening position to correct.",
            account_id=str(query.account_id),
            currency=currency,
        )
    prior_delta = db.scalar(
        select(
            func.coalesce(
                func.sum(CustomerSubledgerOpeningCorrection.delta),
                0,
            )
        ).where(CustomerSubledgerOpeningCorrection.opening_position_id == opening.id)
    )
    previous = round_money(Decimal(opening.legacy_position) + Decimal(prior_delta or 0))
    delta = round_money(corrected - previous)
    if delta == 0:
        raise _error(
            "no_change", "Corrected opening amount already matches the current value."
        )
    fingerprint = _digest(
        {
            "opening_position_id": str(opening.id),
            "account_id": str(query.account_id),
            "currency": currency,
            "previous_opening_amount": str(previous),
            "corrected_opening_amount": str(corrected),
            "delta": str(delta),
            "reason": reason,
            "review_reference": review_reference,
        }
    )
    return CustomerSubledgerOpeningCorrectionPreview(
        opening_position_id=opening.id,
        account_id=query.account_id,
        currency=currency,
        previous_opening_amount=previous,
        corrected_opening_amount=corrected,
        delta=delta,
        preview_fingerprint=fingerprint,
    )


def correct_customer_subledger_opening_position(
    db: Session,
    command: CorrectCustomerSubledgerOpeningCommand,
) -> CustomerSubledgerOpeningCorrectionResult:
    """Append an audited correction and matching customer-position effect."""

    return execute_owner_command(
        db,
        definition=_CORRECTION_COMMAND,
        context=command.context,
        operation=lambda: _correct_opening(db, command),
    )


def _correction_result(
    db: Session,
    correction: CustomerSubledgerOpeningCorrection,
    *,
    replayed: bool,
) -> CustomerSubledgerOpeningCorrectionResult:
    group_id = db.scalar(
        select(CustomerPostingGroup.id).where(
            CustomerPostingGroup.producer_owner
            == PostingProducer.customer_subledger_opening_positions.value,
            CustomerPostingGroup.source_kind
            == PostingSourceKind.customer_subledger_opening_correction.value,
            CustomerPostingGroup.source_id == correction.id,
        )
    )
    if group_id is None:
        raise _error(
            "correction_posting_missing",
            "The opening correction has no matching customer posting.",
            correction_id=str(correction.id),
        )
    return CustomerSubledgerOpeningCorrectionResult(
        correction_id=correction.id,
        posting_group_id=group_id,
        previous_opening_amount=round_money(
            Decimal(correction.previous_opening_amount)
        ),
        corrected_opening_amount=round_money(
            Decimal(correction.corrected_opening_amount)
        ),
        delta=round_money(Decimal(correction.delta)),
        replayed=replayed,
    )


def _correct_opening(
    db: Session,
    command: CorrectCustomerSubledgerOpeningCommand,
) -> CustomerSubledgerOpeningCorrectionResult:
    if command.context.scope != CORRECTION_SCOPE or not command.permission_granted:
        raise _error(
            "permission_denied",
            "Opening correction requires the dedicated staff permission.",
        )
    key = (command.context.idempotency_key or "").strip()
    if not key or len(key) > 120:
        raise _error(
            "invalid_idempotency_key",
            "Opening correction requires an idempotency key of at most 120 characters.",
        )
    existing = db.scalar(
        select(CustomerSubledgerOpeningCorrection).where(
            CustomerSubledgerOpeningCorrection.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.preview_fingerprint != command.expected_preview_fingerprint:
            raise _error(
                "idempotency_conflict",
                "This idempotency key was already used for a different correction.",
            )
        return _correction_result(db, existing, replayed=True)

    if lock_for_update(db, Subscriber, command.query.account_id) is None:
        raise _error(
            "account_not_found",
            "The customer account does not exist.",
            account_id=str(command.query.account_id),
        )
    preview = preview_customer_subledger_opening_correction(db, command.query)
    if preview.preview_fingerprint != command.expected_preview_fingerprint:
        raise _error(
            "stale_reviewed_preview",
            "The opening position changed after review; preview it again.",
        )
    occurred_at = datetime.now(UTC)
    correction = CustomerSubledgerOpeningCorrection(
        opening_position_id=preview.opening_position_id,
        account_id=preview.account_id,
        currency=preview.currency,
        previous_opening_amount=preview.previous_opening_amount,
        corrected_opening_amount=preview.corrected_opening_amount,
        delta=preview.delta,
        reason=command.query.reason.strip(),
        review_reference=command.query.review_reference.strip(),
        preview_fingerprint=preview.preview_fingerprint,
        idempotency_key=key,
        applied_by=command.context.actor,
        authorized_system_user_id=command.authorized_system_user_id,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        occurred_at=occurred_at,
    )
    db.add(correction)
    db.flush()
    effect = (
        PositionEffectKind.customer_credit_created
        if preview.delta > 0
        else PositionEffectKind.customer_credit_consumed
    )
    stage_posting_group(
        db,
        StagePostingGroupCommand(
            account_id=preview.account_id,
            currency=preview.currency,
            command_kind=PostingCommandKind.opening_position_correction,
            producer_owner=PostingProducer.customer_subledger_opening_positions,
            source_kind=PostingSourceKind.customer_subledger_opening_correction,
            source_id=correction.id,
            occurred_at=occurred_at,
            effects=(EffectInput(effect=effect, amount=abs(preview.delta)),),
            idempotency_key=f"posting:opening-correction:{correction.id}",
        ),
        context=command.context,
    )
    emit_event(
        db,
        EventType.customer_subledger_opening_position_corrected,
        {
            "correction_id": str(correction.id),
            "opening_position_id": str(preview.opening_position_id),
            "account_id": str(preview.account_id),
            "currency": preview.currency,
            "previous_opening_amount": str(preview.previous_opening_amount),
            "corrected_opening_amount": str(preview.corrected_opening_amount),
            "delta": str(preview.delta),
            "review_reference": command.query.review_reference.strip(),
            "authorized_system_user_id": str(command.authorized_system_user_id),
        },
        actor=command.context.actor,
    )
    return _correction_result(db, correction, replayed=False)


def activate_customer_subledger_authority(
    db: Session,
    command: ActivateCustomerSubledgerAuthorityCommand,
) -> CustomerSubledgerAuthorityResult:
    """Irreversibly activate subledger writes and default position reads."""

    return execute_owner_command(
        db,
        definition=_CUTOVER_COMMAND,
        context=command.context,
        operation=lambda: _activate_authority(db, command),
    )


def _activate_authority(
    db: Session,
    command: ActivateCustomerSubledgerAuthorityCommand,
) -> CustomerSubledgerAuthorityResult:
    if not command.context.idempotency_key:
        raise _error(
            "missing_idempotency_key",
            "Customer-subledger cutover requires an idempotency key.",
        )
    expected = command.expected_result_fingerprint.strip().lower()
    if len(expected) != 64:
        raise _error(
            "invalid_result_fingerprint",
            "Cutover result fingerprint must be a SHA-256 digest.",
        )
    reference = command.review_reference.strip()
    if not reference:
        raise _error(
            "missing_review_reference",
            "Customer-subledger cutover requires a durable review reference.",
        )
    existing = db.scalar(select(CustomerSubledgerAuthorityCutover).limit(1))
    if existing is not None:
        if (
            existing.verification_run_id == command.verification_run_id
            and existing.result_fingerprint == expected
        ):
            return CustomerSubledgerAuthorityResult(
                cutover_id=existing.id,
                verification_run_id=existing.verification_run_id,
                cutover_at=_utc(existing.cutover_at),
                replayed=True,
            )
        raise _error(
            "authority_already_activated",
            "Customer-subledger authority has one irreversible activation.",
            cutover_id=str(existing.id),
        )
    run = lock_for_update(
        db, BillingCutoverVerificationRun, command.verification_run_id
    )
    if run is None or run.phase != "phase_3_subledger_parity":
        raise _error(
            "verification_run_not_found",
            "The approved Phase 3 subledger parity run does not exist.",
            run_id=str(command.verification_run_id),
        )
    if run.result_fingerprint != expected:
        raise _error(
            "stale_reviewed_preview",
            "Cutover fingerprint is not the approved parity result.",
            run_id=str(run.id),
        )
    if not run.approved:
        raise _error(
            "approval_required",
            "Cutover requires operator and finance approval on a zero-blocker run.",
            run_id=str(run.id),
        )
    details = _object_dict((run.cohort_classification or {}).get("_details"))
    raw_quarantined = details.get("quarantined_accounts")
    quarantined = (
        {str(value) for value in raw_quarantined}
        if isinstance(raw_quarantined, list)
        else set()
    )
    if quarantined:
        raise _error(
            "source_cohort_incomplete",
            "Customer-subledger authority cannot activate with excluded accounts.",
            excluded_count=len(quarantined),
        )
    cutover = CustomerSubledgerAuthorityCutover(
        verification_run_id=run.id,
        result_fingerprint=run.result_fingerprint,
        review_reference=reference,
        activated_by=command.context.actor,
        command_id=command.context.command_id,
        correlation_id=command.context.correlation_id,
        cutover_at=datetime.now(UTC),
    )
    db.add(cutover)
    db.flush()
    emit_event(
        db,
        EventType.customer_subledger_authority_activated,
        {
            "cutover_id": str(cutover.id),
            "verification_run_id": str(run.id),
            "result_fingerprint": run.result_fingerprint,
            "cutover_at": cutover.cutover_at.isoformat(),
            "quarantined_count": len(quarantined),
            "authority_moved": True,
        },
        actor=command.context.actor,
    )
    return CustomerSubledgerAuthorityResult(
        cutover_id=cutover.id,
        verification_run_id=run.id,
        cutover_at=_utc(cutover.cutover_at),
        replayed=False,
    )


__all__ = [
    "ActivateCustomerSubledgerAuthorityCommand",
    "CaptureCustomerSubledgerOpeningsCommand",
    "CorrectCustomerSubledgerOpeningCommand",
    "CORRECTION_SCOPE",
    "CustomerSubledgerAuthorityResult",
    "CustomerSubledgerOpeningCaptureResult",
    "CustomerSubledgerOpeningCorrectionPreview",
    "CustomerSubledgerOpeningCorrectionResult",
    "CustomerSubledgerOpeningError",
    "NATIVE_REPAIR_SCOPE",
    "NativePrepaidOpeningApproval",
    "NativePrepaidOpeningRepairPreview",
    "NativePrepaidOpeningRepairResult",
    "PreviewNativePrepaidOpeningRepairQuery",
    "PreviewCustomerSubledgerOpeningCorrectionQuery",
    "RepairNativePrepaidOpeningCommand",
    "activate_customer_subledger_authority",
    "capture_customer_subledger_opening_positions",
    "correct_customer_subledger_opening_position",
    "preview_customer_subledger_opening_correction",
    "preview_native_prepaid_opening_repair",
    "repair_native_prepaid_opening",
]
