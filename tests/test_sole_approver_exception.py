"""The shared sole-approver exception policy and its governance settings."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.models.domain_settings import SettingDomain
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.settings_spec import get_spec, resolve_value
from app.services.sole_approver_exception import (
    AUDIT_ACTION,
    SoleApproverExceptionPolicy,
    SoleApproverRefusal,
    authorize_sole_approver,
    evaluate_sole_approver_exception,
    load_sole_approver_exception_policy,
)
from tests.sole_approver_support import (
    DECISION_REF,
    JUSTIFICATION,
    configure_sole_approver_exception,
    future_review_due,
)

TODAY = date(2026, 10, 10)
PRINCIPAL = uuid4()


def _policy(**overrides) -> SoleApproverExceptionPolicy:
    values = {
        "enabled": True,
        "principal": PRINCIPAL,
        "review_due": TODAY + timedelta(days=1),
        "decision_ref": DECISION_REF,
    }
    values.update(overrides)
    return SoleApproverExceptionPolicy(**values)


def _evaluate(policy=None, **overrides):
    values = {
        "flow": "test.flow",
        "approver_id": PRINCIPAL,
        "actor": f"user:{PRINCIPAL}",
        "approver_is_human_staff": True,
        "justification": JUSTIFICATION,
        "today": TODAY,
    }
    values.update(overrides)
    return evaluate_sole_approver_exception(policy or _policy(), **values)


def test_allowed_only_when_every_condition_holds():
    decision = _evaluate()
    assert decision.allowed
    assert decision.refusal is None
    assert decision.grant is not None
    evidence = decision.grant.evidence()
    assert evidence["sole_approver_exception"] is True
    assert evidence["sole_approver_exception_decision_ref"] == DECISION_REF
    assert evidence["sole_approver_exception_justification"] == JUSTIFICATION


@pytest.mark.parametrize(
    ("policy", "overrides", "refusal"),
    [
        ({"enabled": False}, {}, SoleApproverRefusal.disabled),
        ({"review_due": None}, {}, SoleApproverRefusal.review_date_unset),
        ({"review_due": TODAY}, {}, SoleApproverRefusal.expired),
        ({"review_due": TODAY - timedelta(days=1)}, {}, SoleApproverRefusal.expired),
        ({"principal": None}, {}, SoleApproverRefusal.principal_unset),
        ({}, {"approver_id": uuid4()}, SoleApproverRefusal.wrong_principal),
        (
            {},
            {"approver_is_human_staff": False},
            SoleApproverRefusal.not_human_staff,
        ),
        ({}, {"actor": "api_key:abc"}, SoleApproverRefusal.not_human_staff),
        ({}, {"actor": "service:celery"}, SoleApproverRefusal.not_human_staff),
        ({}, {"justification": None}, SoleApproverRefusal.justification_missing),
        ({}, {"justification": "   "}, SoleApproverRefusal.justification_missing),
        ({"decision_ref": ""}, {}, SoleApproverRefusal.decision_ref_missing),
    ],
)
def test_every_other_case_is_refused(policy, overrides, refusal):
    decision = _evaluate(_policy(**policy), **overrides)
    assert not decision.allowed
    assert decision.refusal is refusal


def test_settings_are_registered_typed_and_off_by_default(db_session):
    for key in (
        "sole_approver_exception_enabled",
        "sole_approver_exception_principal",
        "sole_approver_exception_review_due",
        "sole_approver_exception_decision_ref",
    ):
        spec = get_spec(SettingDomain.billing, key)
        assert spec is not None
        assert spec.env_var is None  # never bootstrapped from the environment
    assert (
        resolve_value(
            db_session, SettingDomain.billing, "sole_approver_exception_enabled"
        )
        is False
    )
    policy = load_sole_approver_exception_policy(db_session)
    assert policy == SoleApproverExceptionPolicy(
        enabled=False, principal=None, review_due=None, decision_ref=""
    )


def test_malformed_settings_fail_closed(db_session):
    user = SystemUser(
        id=uuid4(), first_name="A", last_name="B", email=f"a-{uuid4().hex}@x.test"
    )
    db_session.add(user)
    db_session.commit()
    configure_sole_approver_exception(db_session, principal=user.id)
    # Not a date and not a UUID: parsed as unset, never as a permissive value.
    from app.models.subscription_engine import SettingValueType
    from app.schemas.settings import DomainSettingUpdate
    from app.services.domain_settings import billing_settings

    for key, value in (
        ("sole_approver_exception_review_due", "next month"),
        ("sole_approver_exception_principal", "not-a-uuid"),
    ):
        billing_settings.upsert_by_key(
            db_session,
            key,
            DomainSettingUpdate(value_type=SettingValueType.string, value_text=value),
        )
    policy = load_sole_approver_exception_policy(db_session)
    assert policy.review_due is None
    assert policy.principal is None
    decision = authorize_sole_approver(
        db_session,
        flow="test.flow",
        approver_id=user.id,
        actor=f"user:{user.id}",
        justification=JUSTIFICATION,
    )
    assert not decision.allowed


def test_authorize_requires_an_active_human_system_user(db_session):
    def staff(**overrides):
        user = SystemUser(
            id=uuid4(),
            first_name="S",
            last_name="U",
            email=f"s-{uuid4().hex}@x.test",
            **overrides,
        )
        db_session.add(user)
        db_session.commit()
        return user

    human = staff()
    configure_sole_approver_exception(
        db_session, principal=human.id, review_due=future_review_due()
    )
    kwargs = {"flow": "test.flow", "justification": JUSTIFICATION}
    ok = authorize_sole_approver(
        db_session, approver_id=human.id, actor=f"user:{human.id}", **kwargs
    )
    assert ok.allowed

    inactive = staff(is_active=False)
    configure_sole_approver_exception(
        db_session, principal=inactive.id, review_due=future_review_due()
    )
    assert not authorize_sole_approver(
        db_session, approver_id=inactive.id, actor=f"user:{inactive.id}", **kwargs
    ).allowed

    contractor = staff(user_type=UserType.vendor)
    configure_sole_approver_exception(
        db_session, principal=contractor.id, review_due=future_review_due()
    )
    assert not authorize_sole_approver(
        db_session,
        approver_id=contractor.id,
        actor=f"user:{contractor.id}",
        **kwargs,
    ).allowed

    missing = uuid4()
    configure_sole_approver_exception(
        db_session, principal=missing, review_due=future_review_due()
    )
    assert not authorize_sole_approver(
        db_session, approver_id=missing, actor=f"user:{missing}", **kwargs
    ).allowed


def test_audit_action_name_is_the_documented_contract():
    assert AUDIT_ACTION == "approval.sole_approver_exception_used"


# --- CLI adapters carry the justification to the owner command ---------------


def test_renewal_term_cli_passes_the_justification(monkeypatch):
    import sys

    from scripts.billing import billing_target_shadow as cli

    seen = {}

    class _Session:
        def close(self) -> None:
            return None

    def _fake(db, args) -> int:
        seen["justification"] = args.sole_approver_justification
        return 0

    monkeypatch.setattr(cli, "SessionLocal", _Session)
    monkeypatch.setattr(cli, "_cmd_approve_renewal_term_record", _fake)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "billing_target_shadow",
            "approve-renewal-term-record",
            "--request",
            str(uuid4()),
            "--amount",
            "17500.00",
            "--approver",
            str(uuid4()),
            "--idempotency-key",
            "k",
            "--sole-approver-justification",
            JUSTIFICATION,
        ],
    )
    assert cli.main() == 0
    assert seen["justification"] == JUSTIFICATION


def test_period_repair_and_carried_source_clis_accept_the_justification():
    from scripts.billing import repair_prepaid_paid_invoice_period as repair
    from scripts.one_off import review_carried_source_identity as carried

    approve = repair.build_parser().parse_args(
        [
            "approve",
            "--request",
            str(uuid4()),
            "--fingerprint",
            "a" * 64,
            "--approver",
            str(uuid4()),
            "--idempotency-key",
            "k",
            "--sole-approver-justification",
            JUSTIFICATION,
        ]
    )
    assert approve.sole_approver_justification == JUSTIFICATION
    review = carried._parser().parse_args(
        [
            "--account-id",
            str(uuid4()),
            "--sole-approver-justification",
            JUSTIFICATION,
        ]
    )
    assert review.sole_approver_justification == JUSTIFICATION
