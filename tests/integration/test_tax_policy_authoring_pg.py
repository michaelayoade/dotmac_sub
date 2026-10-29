"""Real migration-backed Tax policy authoring canaries; hosted PostgreSQL only."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from dotmac_tax import (
    ListTaxRuleHistoryV1,
    TaxNotFound,
    get_tax_authority,
    list_tax_rule_history,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.system_user import SystemUser
from app.services import tax_policy_authoring as owner
from app.services.operator_tenant import OPERATOR_TENANT_ID, provision_operator_tenant
from app.services.owner_commands import CommandContext
from app.services.permission_admission import PermissionAdmission
from app.services.tax_policy_authoring_contracts import (
    AuthoringDraft,
    AuthorityDraft,
    CodeDraft,
    JurisdictionDraft,
    PreviewDraft,
    RuleDraft,
    SupersessionDraft,
)

pytestmark = pytest.mark.integration


def _publish(db: Session, actor_id: UUID, draft: AuthoringDraft) -> UUID:
    db.commit()  # end fixture/read Session transaction before the public owner
    return owner.author_tax_policy(
        db,
        command=owner.AuthorTaxPolicy(
            context=CommandContext.system(
                actor=f"system_user:{actor_id}",
                scope=owner.WRITE_PERMISSION,
                reason=draft.reason,
                idempotency_key=f"{draft.source_ref}:{draft.source_version}",
            ),
            admission=PermissionAdmission(
                actor_id, "system_user", frozenset({owner.WRITE_PERMISSION})
            ),
            draft=draft,
            confirmed=True,
        ),
    ).entity_id


@pytest.fixture
def policy(db_session: Session) -> tuple[Session, UUID, UUID, UUID, str, str]:
    provision_operator_tenant(db_session)
    actor = SystemUser(
        first_name="Tax",
        last_name="Reviewer",
        display_name="Policy Reviewer",
        email=f"tax-policy-{uuid4()}@example.test",
        is_active=True,
    )
    db_session.add(actor)
    db_session.flush()
    actor_id = actor.id
    suffix = uuid4().hex
    provenance = {
        "reason": "Synthetic approved test policy",
        "evidence_ref": "test:decision",
        "source_ref": f"test:policy:{suffix}",
        "source_version": "1",
    }
    authority_id = _publish(
        db_session,
        actor_id,
        AuthorityDraft(code=f"AUTH-{suffix}", name="Test authority", **provenance),
    )
    jurisdiction_id = _publish(
        db_session,
        actor_id,
        JurisdictionDraft(
            authority_id=authority_id,
            code=f"JUR-{suffix}",
            name="Test jurisdiction",
            country_code="NG",
            currency_code="NGN",
            minor_units=2,
            **provenance,
        ),
    )
    code_id = _publish(
        db_session,
        actor_id,
        CodeDraft(
            jurisdiction_id=jurisdiction_id,
            jurisdiction_code=f"JUR-{suffix}",
            code=f"CODE-{suffix}",
            name="Test policy",
            tax_kind_code="test_tax",
            **provenance,
        ),
    )
    return (
        db_session,
        actor_id,
        code_id,
        jurisdiction_id,
        f"CODE-{suffix}",
        f"JUR-{suffix}",
    )


def _rule(
    code_id: UUID,
    *,
    jurisdiction_id: UUID,
    tax_code: str,
    jurisdiction_code: str,
    version: int = 1,
    effective_from: date = date(2026, 1, 1),
    rate: str = "0.10",
) -> RuleDraft:
    return RuleDraft(
        tax_code_id=code_id,
        jurisdiction_id=jurisdiction_id,
        tax_code=tax_code,
        jurisdiction_code=jurisdiction_code,
        version=version,
        effective_from=effective_from,
        priority=100,
        fact_kind="test_invoice",
        recognition_basis_code="invoice_expectation",
        transaction_side="output",
        calculation_method="percentage",
        rate=Decimal(rate),
        currency_code="NGN",
        minor_units=2,
        inclusive=False,
        recoverable_rate=Decimal("0"),
        treatment_code="standard_rated",
        calculation_sequence=100,
        calculation_base_code="source_amount",
        reason="Synthetic approved test rule",
        evidence_ref="test:rule-evidence",
        source_ref=f"test:rule:{code_id}",
        source_version=str(version),
    )


def test_exact_rule_replay_stale_supersession_and_effective_selection(
    policy: tuple[Session, UUID, UUID, UUID, str, str],
) -> None:
    db, actor, code_id, jurisdiction_id, tax_code, jurisdiction_code = policy
    initial = _rule(
        code_id,
        jurisdiction_id=jurisdiction_id,
        tax_code=tax_code,
        jurisdiction_code=jurisdiction_code,
    )
    predecessor_id = _publish(db, actor, initial)
    assert _publish(db, actor, initial) == predecessor_id
    replacement = SupersessionDraft(
        **_rule(
            code_id,
            jurisdiction_id=jurisdiction_id,
            tax_code=tax_code,
            jurisdiction_code=jurisdiction_code,
            version=2,
            effective_from=date(2026, 7, 1),
            rate="0.20",
        ).model_dump(),
        predecessor_rule_id=predecessor_id,
        predecessor_version=1,
    )
    replacement_id = _publish(db, actor, replacement)
    assert _publish(db, actor, replacement) == replacement_id
    stale = replacement.model_copy(
        update={"source_version": "stale-attempt", "predecessor_version": 99}
    )
    with pytest.raises(owner.TaxPolicyAuthoringError):
        _publish(db, actor, stale)
    history = list_tax_rule_history(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        request=ListTaxRuleHistoryV1(tax_code_id=code_id, page_size=1),
    )
    assert history.next_cursor is not None
    assert len(history.items) == 1
    for occurred_on, expected in (
        (date(2026, 6, 30), "10.00"),
        (date(2026, 7, 1), "20.00"),
    ):
        preview = owner.preview_persisted_policy(
            db,
            admission=PermissionAdmission(
                actor, "system_user", frozenset({owner.READ_PERMISSION})
            ),
            draft=PreviewDraft(
                jurisdiction_id=jurisdiction_id,
                occurred_on=occurred_on,
                fact_kind="test_invoice",
                recognition_basis_code="invoice_expectation",
                transaction_side="output",
                base_amount=Decimal("100.00"),
                currency_code="NGN",
                minor_units=2,
                source_ref="test:preview",
                source_version="1",
                evidence_ref="test:evidence",
            ),
        )
        assert preview.tax_amount.amount == Decimal(expected)
    audits = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == str(replacement_id))
    ).all()
    assert len(audits) == 2
    assert all(
        event.actor_id == str(actor) and event.actor_label == "Policy Reviewer"
        for event in audits
    )


def test_audit_failure_rolls_back_tax_publication(
    policy: tuple[Session, UUID, UUID, UUID, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, actor, _code_id, _jurisdiction_id, _tax_code, _jurisdiction_code = policy
    code = f"ROLLBACK-{uuid4().hex}"
    draft = AuthorityDraft(
        code=code,
        name="Must roll back",
        reason="Atomicity test",
        evidence_ref="test:evidence",
        source_ref=f"test:{code}",
        source_version="1",
    )

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Synthetic audit refusal")

    monkeypatch.setattr(owner, "stage_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit refusal"):
        _publish(db, actor, draft)
    with pytest.raises(TaxNotFound):
        get_tax_authority(db, tenant_id=OPERATOR_TENANT_ID, code=code)


@pytest.mark.parametrize(
    ("method", "expected"), (("fixed", "3.00"), ("progressive", "15.00"))
)
def test_fixed_and_progressive_publication_preview_use_tax_math(
    policy: tuple[Session, UUID, UUID, UUID, str, str], method: str, expected: str
) -> None:
    from app.services.tax_policy_authoring_contracts import BandDraft

    db, actor, code_id, jurisdiction_id, tax_code, jurisdiction_code = policy
    draft = _rule(
        code_id,
        jurisdiction_id=jurisdiction_id,
        tax_code=tax_code,
        jurisdiction_code=jurisdiction_code,
    ).model_copy(
        update={
            "calculation_method": method,
            "rate": None,
            "fixed_amount": Decimal("3.00") if method == "fixed" else None,
            "bands": (
                BandDraft(sequence=1, lower_bound=Decimal("0"), rate=Decimal("0.15")),
            )
            if method == "progressive"
            else (),
        }
    )
    _publish(db, actor, draft)
    preview = owner.preview_persisted_policy(
        db,
        admission=PermissionAdmission(
            actor, "system_user", frozenset({owner.READ_PERMISSION})
        ),
        draft=PreviewDraft(
            jurisdiction_id=jurisdiction_id,
            occurred_on=date(2026, 2, 1),
            fact_kind="test_invoice",
            recognition_basis_code="invoice_expectation",
            transaction_side="output",
            base_amount=Decimal("100.00"),
            currency_code="NGN",
            minor_units=2,
            source_ref="test:preview",
            source_version="1",
            evidence_ref="test:evidence",
        ),
    )
    assert preview.tax_amount.amount == Decimal(expected)
    assert len(preview.components) == 1


@pytest.mark.parametrize("supersession", (False, True))
@pytest.mark.parametrize(
    ("method", "currency", "minor_units"),
    (("percentage", "USD", 2), ("progressive", "NGN", 3)),
)
def test_nonfixed_currency_mismatch_refuses_before_policy_or_audit(
    policy: tuple[Session, UUID, UUID, UUID, str, str],
    supersession: bool,
    method: str,
    currency: str,
    minor_units: int,
) -> None:
    from app.services.tax_policy_authoring_contracts import BandDraft

    db, actor, code_id, jurisdiction_id, tax_code, jurisdiction_code = policy
    draft = _rule(
        code_id,
        jurisdiction_id=jurisdiction_id,
        tax_code=tax_code,
        jurisdiction_code=jurisdiction_code,
    )
    if method == "progressive":
        draft = draft.model_copy(
            update={
                "calculation_method": method,
                "rate": None,
                "bands": (
                    BandDraft(
                        sequence=1, lower_bound=Decimal("0"), rate=Decimal("0.1")
                    ),
                ),
            }
        )
    if supersession:
        predecessor = _publish(db, actor, draft)
        draft = SupersessionDraft(
            **draft.model_copy(
                update={
                    "version": 2,
                    "effective_from": date(2026, 7, 1),
                    "source_version": "2",
                }
            ).model_dump(),
            predecessor_rule_id=predecessor,
            predecessor_version=1,
        )
    mismatch = draft.model_copy(
        update={"currency_code": currency, "minor_units": minor_units}
    )
    with pytest.raises(owner.TaxPolicyAuthoringError, match="currency"):
        _publish(db, actor, mismatch)
    history = list_tax_rule_history(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        request=ListTaxRuleHistoryV1(tax_code_id=code_id),
    )
    assert len(history.items) == int(supersession)
    audited = db.scalars(
        select(AuditEvent).where(
            AuditEvent.actor_id == str(actor),
            AuditEvent.action.in_(("tax_policy.initial-rule", "tax_policy.supersede")),
        )
    ).all()
    assert len(audited) == int(supersession)
    _publish(db, actor, draft)  # Same method and exact matching catalog units pass.


@pytest.mark.parametrize("selection", ("all_prior", "tax_code_ids"))
def test_dependency_publication_history_supersession_and_verified_preview(
    policy: tuple[Session, UUID, UUID, UUID, str, str],
    selection: str,
) -> None:
    from datetime import UTC, datetime

    from dotmac_tax import (
        ListTaxRuleHistoryV2,
        TaxCommercialSourceV1,
        TaxFactV2,
        determine_tax_set_v2,
        list_tax_rule_history_v2,
    )

    from app.services.tax_policy_authoring_contracts import DependencySelection

    db, actor, prior_code_id, jurisdiction_id, prior_code, jurisdiction_code = policy
    _publish(
        db,
        actor,
        _rule(
            prior_code_id,
            jurisdiction_id=jurisdiction_id,
            tax_code=prior_code,
            jurisdiction_code=jurisdiction_code,
        ),
    )
    prior_draft = PreviewDraft(
        jurisdiction_id=jurisdiction_id,
        occurred_on=date(2026, 2, 1),
        fact_kind="test_invoice",
        recognition_basis_code="invoice_expectation",
        transaction_side="output",
        base_amount=Decimal("100.00"),
        currency_code="NGN",
        minor_units=2,
        source_ref=f"test:sealed-prior:{uuid4()}",
        source_version="1",
        evidence_ref="test:prior-evidence",
    )
    prior = determine_tax_set_v2(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        fact=TaxFactV2(
            fact=prior_draft.tax_fact(),
            commercial_source=TaxCommercialSourceV1(f"test:commercial:{uuid4()}", "1"),
        ),
        determined_at=datetime.now(UTC),
    )
    target_code = f"WHT-{uuid4().hex}"
    target_id = _publish(
        db,
        actor,
        CodeDraft(
            jurisdiction_id=jurisdiction_id,
            jurisdiction_code=jurisdiction_code,
            code=target_code,
            name="Dependent expectation",
            tax_kind_code="test_withholding",
            reason="Approved dependent policy",
            evidence_ref="test:decision",
            source_ref=f"test:code:{target_code}",
            source_version="1",
        ),
    )
    target = _rule(
        target_id,
        jurisdiction_id=jurisdiction_id,
        tax_code=target_code,
        jurisdiction_code=jurisdiction_code,
    ).model_copy(
        update={
            "fact_kind": "test_withholding",
            "transaction_side": "withholding",
            "rate": Decimal("0.05"),
            "calculation_base_code": "source_plus_dependency_tax",
            "dependency_selection": DependencySelection(selection),
            "dependency_prior_jurisdiction_id": jurisdiction_id,
            "dependency_prior_fact_kind": "test_invoice",
            "dependency_prior_recognition_basis_code": "invoice_expectation",
            "dependency_prior_transaction_side": "output",
            "dependency_tax_code_ids": (prior_code_id,)
            if selection == "tax_code_ids"
            else (),
        }
    )
    predecessor = _publish(db, actor, target)
    assert _publish(db, actor, target) == predecessor
    history = list_tax_rule_history_v2(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        request=ListTaxRuleHistoryV2(tax_code_id=target_id),
    )
    assert (
        history.items[0].rule.dependency_policy == target.tax_input().dependency_policy
    )
    dependent_draft = prior_draft.model_copy(
        update={
            "preview_mode": "dependency",
            "prior_determination_set_ids": (prior.determination_set_id,),
            "fact_kind": "test_withholding",
            "transaction_side": "withholding",
            "source_ref": f"test:preview-wht:{uuid4()}",
        }
    )
    admission = PermissionAdmission(
        actor, "system_user", frozenset({owner.READ_PERMISSION})
    )
    preview = owner.preview_policy_details(
        db, admission=admission, draft=dependent_draft
    )
    assert preview.commercial_source == prior.commercial_source
    assert preview.prior_references[0].source_ref == prior.source_ref
    assert preview.prior_references[0].result_fingerprint == prior.result_fingerprint
    assert preview.result.source_amount.amount == Decimal("100.00")
    assert preview.result.tax_amount.amount == Decimal("5.50")
    assert not db.new and not db.dirty
    with pytest.raises(owner.TaxPolicyAuthoringError, match="actual sealed prior V2"):
        owner.preview_policy_details(
            db,
            admission=admission,
            draft=dependent_draft.model_copy(
                update={"preview_mode": "current", "prior_determination_set_ids": ()}
            ),
        )
    replacement = SupersessionDraft(
        **target.model_copy(
            update={
                "version": 2,
                "effective_from": date(2026, 7, 1),
                "source_version": "2",
                "rate": Decimal("0.06"),
            }
        ).model_dump(),
        predecessor_rule_id=predecessor,
        predecessor_version=1,
    )
    replacement_id = _publish(db, actor, replacement)
    assert _publish(db, actor, replacement) == replacement_id
    history = list_tax_rule_history_v2(
        db,
        tenant_id=OPERATOR_TENANT_ID,
        request=ListTaxRuleHistoryV2(tax_code_id=target_id),
    )
    assert len(history.items) == 2
    assert all(entry.rule.dependency_policy is not None for entry in history.items)
    assert owner.preview_policy_details(
        db, admission=admission, draft=dependent_draft
    ).result.tax_amount.amount == Decimal("5.50")
