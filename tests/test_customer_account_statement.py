"""Customer statement boundaries: history, currency lanes, and disclosure scope."""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models.billing import LedgerEntryType, LedgerSource
from app.services import customer_account_statement as statements
from app.services.communication_attachments import (
    CommunicationAttachmentError,
    _resolve_customer_statement_pdf,
)
from app.services.customer_financial_ledger import CustomerFinancialEvent
from app.services.owner_commands import CommandContext
from app.web.customer import routes as customer_routes


def test_statement_range_accepts_older_history_without_twelve_month_limit() -> None:
    period = statements.statement_range(start_date="2018-01-01", end_date="2026-11-03")
    assert period.start_date == date(2018, 1, 1)
    assert period.end_date == date(2026, 11, 3)
    assert period.start == datetime(2017, 12, 31, 23, 0, tzinfo=UTC)
    assert statements.statement_range(
        start_date=None, end_date=None, today=date(2026, 11, 3)
    ).start_date == date(2026, 10, 5)
    with pytest.raises(statements.CustomerStatementError):
        statements.statement_range(start_date="2026-11-04", end_date="2026-11-03")


def test_canonical_rows_start_from_reviewed_opening_and_keep_debt_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    events = [
        CustomerFinancialEvent(
            id="prepaid-opening:one",
            account_id=account_id,
            entry_type=LedgerEntryType.credit,
            source=LedgerSource.adjustment,
            amount=Decimal("100.00"),
            currency="NGN",
            memo="Reviewed prepaid opening position",
            occurred_at=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        CustomerFinancialEvent(
            id="invoice:one",
            account_id=account_id,
            entry_type=LedgerEntryType.debit,
            source=LedgerSource.invoice,
            amount=Decimal("30.00"),
            currency="NGN",
            memo="Service charge",
            occurred_at=datetime(2026, 10, 2, tzinfo=UTC),
        ),
    ]
    monkeypatch.setattr(
        statements, "list_customer_financial_events", lambda *_a, **_k: events
    )
    monkeypatch.setattr(
        statements,
        "get_customer_receivable_summaries",
        lambda *_a, **_k: (
            SimpleNamespace(currency="NGN", outstanding=Decimal("50.00")),
        ),
    )
    monkeypatch.setattr(
        statements, "prepaid_available_balance", lambda *_a, **_k: Decimal("80.00")
    )

    class AccountSession:
        def get(self, _model: object, key: object) -> SimpleNamespace:
            assert key == account_id
            return SimpleNamespace(id=account_id, account_number="ACC-1")

    statement = statements.read_customer_statement(
        AccountSession(),  # type: ignore[arg-type]
        query=statements.CustomerStatementQuery(
            account_id=account_id,
            period=statements.statement_range(
                start_date="2026-10-01", end_date="2026-10-31"
            ),
        ),
    )
    assert len(statement.rows) == 1
    assert statement.rows[0].running_balance == Decimal("70.00")
    assert statement.balances[0].period_opening == Decimal("100.00")
    assert statement.balances[0].period_closing == Decimal("70.00")
    assert statement.balances[0].amount_owed == Decimal("50.00")
    assert statement.balances[0].prepaid_credit == Decimal("80.00")
    assert not statement.before_opening_available


def test_pdf_text_marks_older_history_unavailable_and_escapes_customer_text() -> None:
    statement = statements.CustomerStatement(
        account_id=uuid4(),
        account_label="A&B <account>",
        period=statements.CustomerStatementRange(
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
        ),
        balances=(
            statements.CustomerStatementBalance(
                currency="NGN",
                period_opening=Decimal("100.00"),
                period_activity=Decimal("0.00"),
                period_closing=Decimal("100.00"),
                amount_owed=Decimal("30.00"),
                prepaid_credit=Decimal("100.00"),
            ),
        ),
        rows=(),
        generated_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    html = statements.render_statement_html(statement)
    assert "History before the reviewed opening" in html
    assert "Older transaction history is not included" in html
    assert "Current amount owed</th><th>Current prepaid credit" in html
    assert "A&amp;B &lt;account&gt;" in html
    assert statements.render_statement_pdf(statement).startswith(b"%PDF-")


@pytest.mark.parametrize("refusal", ("wrong_account", "unverified", "changed_email"))
def test_email_attachment_refuses_stale_or_wrong_recipient(refusal: str) -> None:
    account_id = uuid4()
    principal_id = uuid4()
    account = SimpleNamespace(
        id=principal_id,
        email="new@example.com"
        if refusal == "changed_email"
        else "customer@example.com",
        email_verified=refusal != "unverified",
    )

    class AccountSession:
        def get(self, _model: object, key: object) -> SimpleNamespace:
            if key == principal_id:
                return account
            assert key == account_id
            return SimpleNamespace(id=account_id)

    notification = SimpleNamespace(
        subscriber_id=uuid4() if refusal == "wrong_account" else principal_id,
        recipient="customer@example.com",
        metadata_={
            "statement_start_date": "2026-01-01",
            "statement_end_date": "2026-01-31",
            "statement_account_id": str(account_id),
            "statement_principal_id": str(principal_id),
            "statement_timezone": "Africa/Lagos",
        },
    )
    with pytest.raises(CommunicationAttachmentError, match="statement_scope_mismatch"):
        _resolve_customer_statement_pdf(
            AccountSession(),  # type: ignore[arg-type]
            notification,  # type: ignore[arg-type]
            {"entity_id": str(account_id)},
        )


def test_email_attachment_uses_the_selected_account_and_date_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    principal_id = uuid4()

    class AccountSession:
        def get(self, _model: object, key: object) -> SimpleNamespace:
            assert key in {account_id, principal_id}
            return SimpleNamespace(
                id=key, email="verified@example.com", email_verified=True
            )

    seen: list[statements.CustomerStatementQuery] = []

    def read_statement(
        _db: object, *, query: statements.CustomerStatementQuery
    ) -> object:
        seen.append(query)
        return object()

    monkeypatch.setattr(statements, "read_customer_statement", read_statement)
    monkeypatch.setattr(
        statements, "render_statement_pdf", lambda _statement: b"%PDF-1.4 example"
    )
    notification = SimpleNamespace(
        subscriber_id=principal_id,
        recipient="verified@example.com",
        metadata_={
            "statement_account_id": str(account_id),
            "statement_principal_id": str(principal_id),
            "statement_start_date": "2020-01-01",
            "statement_end_date": "2026-01-31",
            "statement_timezone": "Africa/Lagos",
        },
    )
    attachment = _resolve_customer_statement_pdf(
        AccountSession(),  # type: ignore[arg-type]
        notification,  # type: ignore[arg-type]
        {"entity_id": str(account_id), "filename": "statement.pdf"},
    )
    assert attachment.content.startswith(b"%PDF-")
    assert attachment.filename == "statement.pdf"
    assert seen[0].account_id == account_id
    assert seen[0].period.start_date == date(2020, 1, 1)


def test_email_request_targets_verified_account_and_replay_writes_no_new_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    principal_id = uuid4()
    intent_id = uuid4()
    notification_id = uuid4()
    account = SimpleNamespace(
        id=account_id, email="billing@example.com", email_verified=False
    )
    principal = SimpleNamespace(
        id=principal_id, email="verified@example.com", email_verified=True
    )

    class AccountSession:
        def get(self, _model: object, key: object) -> SimpleNamespace:
            assert key in {account_id, principal_id}
            return account if key == account_id else principal

        def scalar(self, _query: object) -> None:
            return None

        def flush(self) -> None:
            pass

    command = statements.CustomerStatementEmailCommand(
        query=statements.CustomerStatementQuery(
            account_id=account_id,
            period=statements.CustomerStatementRange(
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
            ),
        ),
        principal_id=principal_id,
        idempotency_key=uuid4(),
        context=CommandContext.system(
            actor=f"customer:{principal_id}",
            scope=str(account_id),
            reason="Customer requested account statement by email",
        ),
    )
    monkeypatch.setattr(
        statements, "execute_owner_command", lambda _db, **kwargs: kwargs["operation"]()
    )
    monkeypatch.setattr(
        statements, "read_customer_statement", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(
        statements, "render_statement_pdf", lambda _statement: b"%PDF-1.4"
    )
    written: list[str] = []
    monkeypatch.setattr(
        statements, "stage_audit_event", lambda *_a, **_k: written.append("audit")
    )
    monkeypatch.setattr(
        statements, "emit_event", lambda *_a, **_k: written.append("event")
    )
    sent: list[object] = []

    def submit_intent(_db: object, intent: object) -> SimpleNamespace:
        sent.append(intent)
        return SimpleNamespace(
            intent_id=intent_id,
            queued=(SimpleNamespace(id=notification_id),),
            deliveries=(SimpleNamespace(id=notification_id),),
            replayed=len(sent) > 1,
        )

    monkeypatch.setattr(statements, "submit", submit_intent)
    first = statements.request_customer_statement_email(
        AccountSession(),
        command=command,  # type: ignore[arg-type]
    )
    second = statements.request_customer_statement_email(
        AccountSession(),
        command=command,  # type: ignore[arg-type]
    )
    assert first.notification_id == second.notification_id == notification_id
    assert not first.replayed and second.replayed
    assert written == ["audit", "event"]
    assert sent[0].recipients == {
        statements.NotificationChannel.email: "verified@example.com"
    }
    assert sent[0].attachments[0].entity_id == account_id
    assert sent[0].metadata["statement_start_date"] == "2026-01-01"
    assert sent[0].metadata["statement_account_id"] == str(account_id)
    assert sent[0].metadata["statement_principal_id"] == str(principal_id)
    assert sent[0].metadata["statement_timezone"] == "Africa/Lagos"


def test_email_replay_does_not_reread_changed_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account_id = uuid4()
    principal_id = uuid4()
    intent_id = uuid4()
    notification_id = uuid4()
    period = statements.CustomerStatementRange(
        start_date=date(2026, 1, 1), end_date=date(2026, 1, 31)
    )
    command = statements.CustomerStatementEmailCommand(
        query=statements.CustomerStatementQuery(account_id=account_id, period=period),
        principal_id=principal_id,
        idempotency_key=uuid4(),
        context=CommandContext.system(
            actor=f"customer:{principal_id}",
            scope=str(account_id),
            reason="Customer requested account statement by email",
        ),
    )
    existing = SimpleNamespace(
        id=intent_id,
        subscriber_id=principal_id,
        metadata_={
            "statement_account_id": str(account_id),
            "statement_principal_id": str(principal_id),
            "statement_start_date": "2026-01-01",
            "statement_end_date": "2026-01-31",
            "statement_timezone": "Africa/Lagos",
        },
        notifications=[SimpleNamespace(id=notification_id)],
    )

    class ReplaySession:
        def scalar(self, _query: object) -> SimpleNamespace:
            return existing

        def get(self, _model: object, _key: object) -> None:
            raise AssertionError("replay must not inspect a changed email")

    monkeypatch.setattr(
        statements, "execute_owner_command", lambda _db, **kwargs: kwargs["operation"]()
    )
    outcome = statements.request_customer_statement_email(
        ReplaySession(),
        command=command,  # type: ignore[arg-type]
    )
    assert outcome.replayed
    assert outcome.notification_id == notification_id


def test_statement_route_refuses_another_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned_id = uuid4()
    other_id = uuid4()
    context = SimpleNamespace(
        is_impersonation=False,
        allowed_account_ids=(str(owned_id),),
        principal_id=str(owned_id),
        owns_account=lambda value: value == owned_id,
        require_account_id=lambda: str(owned_id),
    )
    monkeypatch.setattr(
        customer_routes, "resolve_customer_context", lambda *_a: context
    )
    with pytest.raises(HTTPException) as denied:
        customer_routes._customer_statement_account(
            None,
            {},
            other_id,  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 404
