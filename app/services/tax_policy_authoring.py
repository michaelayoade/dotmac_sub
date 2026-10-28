"""Atomic staff coordinator for public Tax policy authoring, never tax arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from dotmac_kernel.money import Currency, MoneyError
from dotmac_tax import (
    TaxAuthorityInput,
    TaxCodeV1,
    TaxConflict,
    TaxFactV2,
    TaxJurisdictionInput,
    TaxJurisdictionV1,
    TaxNotFound,
    TaxPolicyPreviewV1,
    TaxPriorResultReferenceV1,
    TaxRuleSupersessionInputV2,
    TaxRuleViolation,
    ensure_tax_authority_in_transaction,
    ensure_tax_code_in_transaction,
    ensure_tax_jurisdiction_in_transaction,
    ensure_tax_rule_v2_in_transaction,
    get_tax_code,
    get_tax_determination_set_v2,
    get_tax_jurisdiction,
    preview_tax_policy,
    preview_tax_policy_v2,
    supersede_tax_rule_v2,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.operator_tenant import OPERATOR_TENANT_ID
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.permission_admission import PermissionAdmission
from app.services.staff_provisioning import (
    StaffDisplayIdentityQuery,
    resolve_staff_display_identities,
)
from app.services.tax_policy_authoring_contracts import (
    AuthoringDraft,
    AuthoringOutcome,
    AuthorityDraft,
    CodeDraft,
    JurisdictionDraft,
    Operation,
    PolicyPreviewOutcome,
    PreviewDraft,
    RuleDraft,
    SupersessionDraft,
)

READ_PERMISSION = "billing:tax:read"
WRITE_PERMISSION = "billing:tax:write"
OWNER = "financial.tax_policy_authoring"
CONCERN = "atomic staff Tax policy publication and audit"
_DEFINITION = OwnerCommandDefinition(
    owner=OWNER, concern=CONCERN, name="author_tax_policy"
)


class TaxPolicyAuthoringError(DomainError):
    """Safe stable rejection mapped by adapters only."""


def error(suffix: str, message: str) -> TaxPolicyAuthoringError:
    return TaxPolicyAuthoringError(
        code=f"{OWNER}.{suffix}", message=message, retryable=False
    )


def admit(*, admission: PermissionAdmission, permission: str) -> None:
    if (
        admission.principal_type != "system_user"
        or permission not in admission.admitted_permissions
    ):
        raise error("unauthorized", "An authorized staff principal is required.")


@dataclass(frozen=True, slots=True)
class AuthorTaxPolicy:
    context: CommandContext
    admission: PermissionAdmission
    draft: AuthoringDraft
    confirmed: bool


def author_tax_policy(db: Session, *, command: AuthorTaxPolicy) -> AuthoringOutcome:
    """One transaction-free entry; public Tax participants and audit are atomic."""
    return execute_owner_command(
        db,
        definition=_DEFINITION,
        context=command.context,
        operation=lambda: _author(db=db, command=command),
    )


def _author(*, db: Session, command: AuthorTaxPolicy) -> AuthoringOutcome:
    admit(admission=command.admission, permission=WRITE_PERMISSION)
    draft = command.draft
    actor_id = command.admission.principal_id
    if (
        not command.confirmed
        or command.context.actor != f"system_user:{actor_id}"
        or command.context.scope != WRITE_PERMISSION
        or command.context.reason != draft.reason
        or command.context.idempotency_key
        != f"{draft.source_ref}:{draft.source_version}"
    ):
        raise error("invalid_input", "Review and confirm this exact policy command.")
    # Lock canonical staff identity for the action; never accept a submitted actor label.
    actor = db.scalar(
        select(SystemUser)
        .where(SystemUser.id == actor_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor is None or not actor.is_active:
        raise error("unauthorized", "An active staff principal is required.")
    identities = resolve_staff_display_identities(
        db, query=StaffDisplayIdentityQuery(user_ids=frozenset({actor_id}))
    )
    label = identities.identities[0].display_name if identities.identities else ""
    if not label:
        raise error(
            "unauthorized", "The staff principal has no canonical display name."
        )
    try:
        rule_scope = (
            validate_rule_currency(db=db, draft=draft)
            if isinstance(draft, RuleDraft)
            else None
        )
        if isinstance(draft, AuthorityDraft):
            result = ensure_tax_authority_in_transaction(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                command=TaxAuthorityInput(
                    code=draft.code,
                    name=draft.name,
                    authority_level_code=draft.authority_level_code,
                ),
            )
            outcome = AuthoringOutcome(
                entity_id=result.authority_id, operation=Operation.authority
            )
        elif isinstance(draft, JurisdictionDraft):
            jurisdiction = ensure_tax_jurisdiction_in_transaction(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                command=TaxJurisdictionInput(
                    authority_id=draft.authority_id,
                    code=draft.code,
                    name=draft.name,
                    country_code=draft.country_code,
                    subdivision_code=draft.subdivision_code,
                    currency=Currency(draft.currency_code, draft.minor_units),
                ),
            )
            outcome = AuthoringOutcome(
                entity_id=jurisdiction.jurisdiction_id,
                operation=Operation.jurisdiction,
                jurisdiction_code=jurisdiction.code,
            )
        elif isinstance(draft, CodeDraft):
            code_jurisdiction = get_tax_jurisdiction(
                db, tenant_id=OPERATOR_TENANT_ID, code=draft.jurisdiction_code
            )
            if code_jurisdiction.jurisdiction_id != draft.jurisdiction_id:
                raise error(
                    "invalid_input",
                    "The selected jurisdiction identity does not match.",
                )
            code = ensure_tax_code_in_transaction(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                jurisdiction_id=draft.jurisdiction_id,
                code=draft.code,
                name=draft.name,
                tax_kind_code=draft.tax_kind_code,
                description=draft.description,
            )
            outcome = AuthoringOutcome(
                entity_id=code.tax_code_id,
                operation=Operation.code,
                jurisdiction_id=code.jurisdiction_id,
                tax_code_id=code.tax_code_id,
                jurisdiction_code=code_jurisdiction.code,
                tax_code=code.code,
            )
        elif isinstance(draft, SupersessionDraft):
            assert rule_scope is not None
            supersession = supersede_tax_rule_v2(
                db,
                tenant_id=OPERATOR_TENANT_ID,
                command=TaxRuleSupersessionInputV2(
                    predecessor_rule_id=draft.predecessor_rule_id,
                    predecessor_version=draft.predecessor_version,
                    replacement=draft.tax_input(),
                    effective_from=draft.effective_from,
                    actor_id=actor_id,
                    published_by_ref=f"system_user:{actor_id}",
                    reason=draft.reason,
                    evidence_ref=draft.evidence_ref,
                    source_ref=draft.source_ref,
                    source_version=draft.source_version,
                ),
            )
            outcome = AuthoringOutcome(
                entity_id=supersession.replacement.rule_id,
                operation=Operation.supersede,
                jurisdiction_id=draft.jurisdiction_id,
                tax_code_id=draft.tax_code_id,
                jurisdiction_code=rule_scope[1].code,
                tax_code=rule_scope[0].code,
            )
        elif isinstance(draft, RuleDraft):
            assert rule_scope is not None
            if draft.version != 1:
                raise error(
                    "invalid_input",
                    "Initial publication requires version 1. Use explicit supersession for later versions.",
                )
            rule = ensure_tax_rule_v2_in_transaction(
                db, tenant_id=OPERATOR_TENANT_ID, command=draft.tax_input()
            )
            outcome = AuthoringOutcome(
                entity_id=rule.rule_id,
                operation=Operation.initial_rule,
                jurisdiction_id=draft.jurisdiction_id,
                tax_code_id=draft.tax_code_id,
                jurisdiction_code=rule_scope[1].code,
                tax_code=rule_scope[0].code,
            )
        else:
            raise error("invalid_input", "Unknown authoring command.")
    except TaxNotFound as exc:
        raise error("not_found", str(exc)) from exc
    except TaxConflict as exc:
        raise error("conflict", str(exc)) from exc
    except (TaxRuleViolation, ValueError, MoneyError) as exc:
        raise error("invalid_input", str(exc)) from exc
    stage_audit_event(
        db,
        actor_type=AuditActorType.user,
        actor_id=str(actor_id),
        actor_label=label,
        actor_party_id=actor.person_party_id,
        action=f"tax_policy.{outcome.operation.value}",
        entity_type="tax_policy",
        entity_id=str(outcome.entity_id),
        metadata={
            "reason": draft.reason,
            "evidence_ref": draft.evidence_ref,
            "source_ref": draft.source_ref,
            "source_version": draft.source_version,
            "command_id": str(command.context.command_id),
            "predecessor_rule_id": str(draft.predecessor_rule_id)
            if isinstance(draft, SupersessionDraft)
            else None,
            "predecessor_version": draft.predecessor_version
            if isinstance(draft, SupersessionDraft)
            else None,
        },
    )
    db.flush()
    return outcome


def preview_persisted_policy(
    db: Session, *, admission: PermissionAdmission, draft: PreviewDraft
) -> TaxPolicyPreviewV1:
    """Compatibility query returning the public Tax result; details retain its provenance."""
    return preview_policy_details(db, admission=admission, draft=draft).result


def preview_policy_details(
    db: Session, *, admission: PermissionAdmission, draft: PreviewDraft
) -> PolicyPreviewOutcome:
    """Read only: no draft publication, durable determination, or replay identity."""
    admit(admission=admission, permission=READ_PERMISSION)
    if draft.preview_mode == "dependency":
        if not draft.prior_determination_set_ids:
            raise error(
                "invalid_input",
                "Dependent preview requires actual sealed prior V2 determination set UUIDs. Select the original commercial facts; do not add prior tax to the amount.",
            )
        return preview_dependency_details(db=db, draft=draft)
    if draft.prior_determination_set_ids:
        raise error(
            "invalid_input",
            "Choose dependency preview to use prior determination evidence.",
        )
    try:
        result = preview_tax_policy(
            db,
            tenant_id=OPERATOR_TENANT_ID,
            fact=draft.tax_fact(),
            determined_at=datetime.now(UTC),
        )
        return PolicyPreviewOutcome(result=result)
    except TaxNotFound as exc:
        raise error("not_found", str(exc)) from exc
    except TaxConflict as exc:
        raise error("conflict", str(exc)) from exc
    except TaxRuleViolation as exc:
        if "dependency rule requires" in str(exc):
            raise error(
                "invalid_input",
                "This persisted rule requires dependency preview with actual sealed prior V2 determination set UUIDs and their original commercial source; current-policy V1 preview cannot simulate it.",
            ) from exc
        raise error("invalid_input", str(exc)) from exc
    except (ValueError, MoneyError) as exc:
        raise error("invalid_input", str(exc)) from exc


def validate_rule_currency(
    *, db: Session, draft: RuleDraft
) -> tuple[TaxCodeV1, TaxJurisdictionV1]:
    """Bind every method's submitted units to public tenant-scoped catalog facts."""
    code = get_tax_code(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        jurisdiction_id=draft.jurisdiction_id,
        code=draft.tax_code,
    )
    jurisdiction = get_tax_jurisdiction(
        db, tenant_id=OPERATOR_TENANT_ID, code=draft.jurisdiction_code
    )
    if (
        code.tax_code_id != draft.tax_code_id
        or code.jurisdiction_id != jurisdiction.jurisdiction_id
        or jurisdiction.jurisdiction_id != draft.jurisdiction_id
    ):
        raise error(
            "invalid_input",
            "Tax code and jurisdiction identities do not match the selected policy.",
        )
    submitted = Currency(draft.currency_code, draft.minor_units)
    if submitted != jurisdiction.currency:
        raise error(
            "invalid_input",
            "Policy currency and minor-unit precision must match the selected jurisdiction.",
        )

    return code, jurisdiction


def dependency_preview_fact(*, db: Session, draft: PreviewDraft) -> TaxFactV2:
    """Resolve exact scoped immutable owner evidence; never accept entered hashes."""
    if not draft.prior_determination_set_ids:
        raise error(
            "invalid_input",
            "Select actual sealed prior V2 determination set UUIDs before previewing a dependency.",
        )
    results = tuple(
        get_tax_determination_set_v2(
            db, tenant_id=OPERATOR_TENANT_ID, determination_set_id=identifier
        )
        for identifier in draft.prior_determination_set_ids
    )
    commercial_source = results[0].commercial_source
    if any(result.commercial_source != commercial_source for result in results):
        raise error(
            "invalid_input",
            "The selected prior results have different original commercial identities. Select results from one original source.",
        )
    return TaxFactV2(
        fact=draft.tax_fact(),
        commercial_source=commercial_source,
        prior_results=tuple(
            TaxPriorResultReferenceV1(
                determination_set_id=result.determination_set_id,
                source_ref=result.source_ref,
                source_version=result.source_version,
                source_fingerprint=result.source_fingerprint,
                result_fingerprint=result.result_fingerprint,
            )
            for result in results
        ),
    )


def preview_dependency_details(
    *, db: Session, draft: PreviewDraft
) -> PolicyPreviewOutcome:
    """Actual sealed references and original commercial identity feed public Tax V2."""
    try:
        fact = dependency_preview_fact(db=db, draft=draft)
        result = preview_tax_policy_v2(
            db, tenant_id=OPERATOR_TENANT_ID, fact=fact, determined_at=datetime.now(UTC)
        )
        return PolicyPreviewOutcome(
            result=result,
            commercial_source=fact.commercial_source,
            prior_references=fact.prior_results,
        )
    except TaxNotFound as exc:
        raise error(
            "not_found",
            "A selected prior result was not found as sealed V2 evidence in this tenant. Select its exact original V2 result UUID.",
        ) from exc
    except TaxConflict as exc:
        raise error("conflict", str(exc)) from exc
    except (TaxRuleViolation, ValueError, MoneyError) as exc:
        raise error("invalid_input", str(exc)) from exc
