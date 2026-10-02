"""SQLite behavior for explicit, dormant payment email content adoption."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from dotmac_template_studio import service as studio
from dotmac_template_studio.models import Template, TemplateVersion
from sqlalchemy import func, select

from app.models.event_store import EventStore
from app.models.integration_platform import IntegrationDelivery
from app.models.notification import (
    NotificationChannel,
    NotificationTemplate,
    NotificationTemplatePurpose,
)
from app.services import payment_template_adoption as adoption
from app.services.domain_errors import DomainError
from app.services.operator_tenant import operator_tenant_id
from app.services.owner_commands import CommandContext
from app.services.payment_template_adoption import (
    ParityStatus,
    ReviewedPaymentEmailTemplates,
    payment_email_parity_report,
)
from app.services.payment_template_adoption import (
    adopt_payment_email_templates as _adopt_owner,
)


@pytest.fixture
def adoption_db(db_session):
    connection = db_session.connection()
    attached = {row[1] for row in connection.exec_driver_sql("PRAGMA database_list")}
    if "mod_tstudio" not in attached:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS mod_tstudio")
    Template.__table__.create(connection, checkfirst=True)
    TemplateVersion.__table__.create(connection, checkfirst=True)
    db_session.commit()
    return db_session


def _legacy(db, *, code: str, active: bool = True, body: str | None = None):
    row = NotificationTemplate(
        name=f"Operator {code}",
        code=code,
        channel=NotificationChannel.email,
        subject=(
            "Receipt {receipt_number}"
            if code.startswith("payment_received")
            else "Paid {invoice_number}"
        ),
        body=body
        or (
            "Hello {subscriber_name}; receipt {receipt_number}: {receipt_url}"
            if code.startswith("payment_received")
            else "Hello {subscriber_name}; invoice {invoice_number} paid."
        ),
        conditions={"field": "account_status", "operator": "=", "value": "active"},
        is_active=active,
    )
    db.add(row)
    db.commit()
    return row


def _context():
    return CommandContext.system(
        actor="test:operator",
        scope=str(operator_tenant_id()),
        reason="explicit content adoption",
        idempotency_key="payment-email-templates",
    )


def _outbound_counts(db):
    return (
        db.scalar(select(func.count()).select_from(EventStore)),
        db.scalar(select(func.count()).select_from(IntegrationDelivery)),
    )


def _reviewed(db):
    rows = dict(
        db.execute(select(NotificationTemplate.code, NotificationTemplate.id))
        .tuples()
        .all()
    )
    db.commit()
    return ReviewedPaymentEmailTemplates(
        payment_received_legacy_id=rows.get(
            "payment_received", rows.get("payment_received_email")
        ),
        invoice_paid_legacy_id=rows.get("invoice_paid", rows.get("invoice_paid_email")),
    )


def adopt_payment_email_templates(db, *, context):
    """Test helper: make the reviewed input explicit before the owner command."""
    return _adopt_owner(db, context=context, reviewed=_reviewed(db))


def _samples():
    return {
        "payment_received": (
            {
                "subscriber_name": "Ada",
                "receipt_number": "R-42",
                "receipt_url": "https://example.test/receipt/42",
            },
            {
                "subscriber_name": "Chinyere & Co",
                "receipt_number": "R-43",
                "receipt_url": "https://example.test/receipt/43",
            },
        ),
        "invoice_paid": (
            {"subscriber_name": "Ada", "invoice_number": "INV-42"},
            {"subscriber_name": "Chinyere & Co", "invoice_number": "INV-43"},
        ),
    }


def test_atomic_adoption_replay_and_parity(adoption_db):
    receipt = _legacy(adoption_db, code="payment_received_email")
    paid = _legacy(adoption_db, code="invoice_paid", active=False)
    before = payment_email_parity_report(adoption_db, contexts=_samples())
    assert {item.status for item in before.items} == {ParityStatus.studio_missing}
    adoption_db.commit()

    first = adopt_payment_email_templates(adoption_db, context=_context())
    assert len(first.items) == 2
    assert all(item.created for item in first.items)
    assert {item.legacy_template_id for item in first.items} == {receipt.id, paid.id}
    assert _outbound_counts(adoption_db) == (0, 0)
    for item in first.items:
        slug = "payment-received" if item.code == "payment_received" else "invoice-paid"
        template = studio.get_by_slug(adoption_db, operator_tenant_id(), slug, "email")
        assert str(item.legacy_template_id) in (template.description or "")
        assert "sha256:" in (template.description or "")
        assert template.published_version == item.published_version == 1
    assert all(
        item.status == ParityStatus.match
        for item in payment_email_parity_report(adoption_db, contexts=_samples()).items
    )
    adoption_db.commit()

    replay = adopt_payment_email_templates(adoption_db, context=_context())
    assert not any(item.created for item in replay.items)
    assert {item.studio_template_id for item in replay.items} == {
        item.studio_template_id for item in first.items
    }
    assert _outbound_counts(adoption_db) == (0, 0)
    tenant_id = operator_tenant_id()
    inactive = studio.get_by_slug(adoption_db, tenant_id, "invoice-paid", "email")
    assert inactive.is_active is False
    assert (
        studio.get_by_slug(adoption_db, tenant_id, "payment-received", "email").slug
        == "payment-received"
    )
    assert receipt.conditions == {
        "field": "account_status",
        "operator": "=",
        "value": "active",
    }
    assert paid.is_active is False


def test_existing_studio_operator_edit_refused_without_clobber(adoption_db):
    _legacy(adoption_db, code="payment_received")
    _legacy(adoption_db, code="invoice_paid")
    adopt_payment_email_templates(adoption_db, context=_context())
    tenant_id = operator_tenant_id()
    template = studio.get_by_slug(adoption_db, tenant_id, "payment-received", "email")
    draft = studio.create_version(
        adoption_db,
        tenant_id,
        template.id,
        subject="Operator edit",
        body="Operator edit",
    )
    adoption_db.commit()

    with pytest.raises(DomainError, match="parity changed"):
        adopt_payment_email_templates(adoption_db, context=_context())
    assert (
        studio.get_version(adoption_db, tenant_id, template.id, draft.version).body
        == "Operator edit"
    )


def test_ambiguous_legacy_identity_refused_before_studio_write(adoption_db):
    _legacy(adoption_db, code="payment_received")
    _legacy(adoption_db, code="payment_received_email")
    _legacy(adoption_db, code="invoice_paid")
    with pytest.raises(DomainError, match="Exactly one legacy"):
        adopt_payment_email_templates(adoption_db, context=_context())
    assert studio.list_templates(adoption_db, operator_tenant_id()) == []


def test_replaced_legacy_identity_after_preview_refused_atomically(adoption_db):
    _legacy(adoption_db, code="payment_received")
    paid = _legacy(adoption_db, code="invoice_paid")
    preview = payment_email_parity_report(adoption_db)
    assert all(item.status is ParityStatus.studio_missing for item in preview.items)
    receipt_id = preview.items[0].legacy_template_id
    paid_id = preview.items[1].legacy_template_id
    assert receipt_id is not None and paid_id is not None
    reviewed = ReviewedPaymentEmailTemplates(receipt_id, paid_id)
    adoption_db.commit()

    adoption_db.delete(paid)
    adoption_db.commit()
    replacement = _legacy(adoption_db, code="invoice_paid")
    assert replacement.id != paid_id
    adoption_db.commit()

    with pytest.raises(DomainError) as failure:
        _adopt_owner(adoption_db, context=_context(), reviewed=reviewed)
    assert failure.value.code == "payment_template_adoption.ambiguous_legacy"
    assert "identities changed" in failure.value.message
    assert studio.list_templates(adoption_db, operator_tenant_id()) == []
    assert _outbound_counts(adoption_db) == (0, 0)


def test_invalid_receipt_refused_before_studio_write(adoption_db):
    _legacy(adoption_db, code="payment_received", body="Thank you {subscriber_name}")
    _legacy(adoption_db, code="invoice_paid")
    with pytest.raises(DomainError, match="Invalid legacy"):
        adopt_payment_email_templates(adoption_db, context=_context())
    assert studio.list_templates(adoption_db, operator_tenant_id()) == []


@pytest.mark.parametrize(
    ("code", "extra"),
    (
        ("payment_received", " {invoice_number}"),
        ("invoice_paid", " {receipt_number}"),
    ),
)
def test_code_specific_context_rejects_other_events_variables(adoption_db, code, extra):
    receipt_body = (
        "Receipt {receipt_number}: {receipt_url}" + extra
        if code == "payment_received"
        else None
    )
    paid_body = (
        "Invoice {invoice_number} paid" + extra if code == "invoice_paid" else None
    )
    _legacy(adoption_db, code="payment_received", body=receipt_body)
    _legacy(adoption_db, code="invoice_paid", body=paid_body)
    with pytest.raises(DomainError, match="Invalid legacy"):
        adopt_payment_email_templates(adoption_db, context=_context())
    assert studio.list_templates(adoption_db, operator_tenant_id()) == []


def test_changed_legacy_conditions_refused_and_reported_as_mismatch(adoption_db):
    receipt = _legacy(adoption_db, code="payment_received")
    _legacy(adoption_db, code="invoice_paid")
    adopt_payment_email_templates(adoption_db, context=_context())
    receipt.conditions = {
        "field": "account_status",
        "operator": "=",
        "value": "inactive",
    }
    adoption_db.commit()
    with pytest.raises(DomainError, match="parity changed"):
        adopt_payment_email_templates(adoption_db, context=_context())
    report = payment_email_parity_report(adoption_db, contexts=_samples())
    assert report.items[0].status is ParityStatus.mismatch


def test_changed_legacy_purpose_is_visible_and_refuses_replay(adoption_db):
    receipt = _legacy(adoption_db, code="payment_received")
    _legacy(adoption_db, code="invoice_paid")
    adopt_payment_email_templates(adoption_db, context=_context())
    receipt.purpose = NotificationTemplatePurpose.billing
    adoption_db.commit()

    report = payment_email_parity_report(adoption_db, contexts=_samples())
    assert report.items[0].purpose == "billing"
    assert report.items[0].status is ParityStatus.mismatch
    with pytest.raises(DomainError, match="parity changed"):
        adopt_payment_email_templates(adoption_db, context=_context())


def test_legacy_rows_keep_ids_and_no_outbound_event_or_delivery(adoption_db):
    receipt = _legacy(adoption_db, code="payment_received")
    paid = _legacy(adoption_db, code="invoice_paid")
    ids = {receipt.id, paid.id}
    adoption_db.commit()
    adopt_payment_email_templates(adoption_db, context=_context())
    assert {
        row.id for row in adoption_db.scalars(select(NotificationTemplate)).all()
    } == ids
    assert _outbound_counts(adoption_db) == (0, 0)


def test_wrong_tenant_context_refused_before_transaction(adoption_db):
    _legacy(adoption_db, code="payment_received")
    _legacy(adoption_db, code="invoice_paid")
    wrong = CommandContext.system(
        actor="test:operator", scope=str(uuid4()), reason="wrong tenant"
    )
    with pytest.raises(DomainError, match="operator tenant"):
        adopt_payment_email_templates(adoption_db, context=wrong)
    assert studio.list_templates(adoption_db, operator_tenant_id()) == []


@pytest.mark.parametrize(
    "posture",
    (
        pytest.param((True, False), id="superuser"),
        pytest.param((False, True), id="bypass-rls"),
        pytest.param(None, id="missing-current-role"),
    ),
)
def test_runtime_role_guard_refuses_elevated_or_unknown_posture(posture):
    db = Mock()
    db.get_bind.return_value.dialect.name = "postgresql"
    db.execute.return_value.one_or_none.return_value = (
        None if posture is None else Mock(rolsuper=posture[0], rolbypassrls=posture[1])
    )

    with pytest.raises(DomainError) as failure:
        adoption.require_rls_runtime_role(db)

    assert failure.value.code == "payment_template_adoption.unsafe_runtime_role"
    assert failure.value.message == (
        "Payment email adoption requires an RLS-enforced database role."
    )
    assert "current_user" in str(db.execute.call_args.args[0])


@pytest.mark.parametrize("entrypoint", ("parity", "adoption"))
def test_unsafe_role_refused_before_legacy_or_studio_access(
    db_session, monkeypatch, entrypoint
):
    def refuse(_db):
        raise adoption._error(
            "unsafe_runtime_role",
            "Payment email adoption requires an RLS-enforced database role.",
        )

    legacy = Mock(side_effect=AssertionError("legacy read after role refusal"))
    studio_read = Mock(side_effect=AssertionError("Studio read after role refusal"))
    studio_write = Mock(side_effect=AssertionError("Studio write after role refusal"))
    monkeypatch.setattr(adoption, "require_rls_runtime_role", refuse)
    monkeypatch.setattr(adoption, "_legacy_snapshot", legacy)
    monkeypatch.setattr(studio, "get_by_slug", studio_read)
    monkeypatch.setattr(studio, "create_template", studio_write)

    with pytest.raises(DomainError) as failure:
        if entrypoint == "parity":
            payment_email_parity_report(db_session)
        else:
            _adopt_owner(
                db_session,
                context=_context(),
                reviewed=ReviewedPaymentEmailTemplates(uuid4(), uuid4()),
            )

    assert failure.value.code == "payment_template_adoption.unsafe_runtime_role"
    legacy.assert_not_called()
    studio_read.assert_not_called()
    studio_write.assert_not_called()
