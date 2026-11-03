"""Customer-owned account statement projection and PDF presentation.

The archived pre-opening ledger is deliberately unavailable. Money in this
projection comes only from the canonical customer financial-event reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.notification import CommunicationIntentRecord, NotificationChannel
from app.models.subscriber import Subscriber
from app.services import display_format
from app.services.audit_adapter import stage_audit_event
from app.services.communication_intents import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.customer_financial_ledger import list_customer_financial_events
from app.services.customer_financial_position import (
    get_customer_receivable_summaries,
    prepaid_available_balance,
)
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)


class CustomerStatementError(DomainError):
    """A statement request could not be fulfilled safely."""


@dataclass(frozen=True, slots=True)
class CustomerStatementRange:
    start_date: date
    end_date: date
    timezone: ZoneInfo = ZoneInfo("Africa/Lagos")

    @property
    def start(self) -> datetime:
        return datetime.combine(
            self.start_date, time.min, tzinfo=self.timezone
        ).astimezone(UTC)

    @property
    def end_exclusive(self) -> datetime:
        return datetime.combine(
            self.end_date + timedelta(days=1), time.min, tzinfo=self.timezone
        ).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CustomerStatementQuery:
    account_id: UUID
    period: CustomerStatementRange


@dataclass(frozen=True, slots=True)
class CustomerStatementAccount:
    account_id: UUID
    label: str


@dataclass(frozen=True, slots=True)
class CustomerStatementRow:
    occurred_at: datetime
    entry: str
    entry_type: str
    source: str
    debit: Decimal
    credit: Decimal
    running_balance: Decimal
    currency: str
    description: str


@dataclass(frozen=True, slots=True)
class CustomerStatementBalance:
    currency: str
    period_opening: Decimal
    period_activity: Decimal
    period_closing: Decimal
    amount_owed: Decimal
    prepaid_credit: Decimal | None


@dataclass(frozen=True, slots=True)
class CustomerStatement:
    account_id: UUID
    account_label: str
    period: CustomerStatementRange
    balances: tuple[CustomerStatementBalance, ...]
    rows: tuple[CustomerStatementRow, ...]
    generated_at: datetime
    before_opening_available: bool = False


@dataclass(frozen=True, slots=True)
class CustomerStatementEmailCommand:
    query: CustomerStatementQuery
    principal_id: UUID
    idempotency_key: UUID
    context: CommandContext


@dataclass(frozen=True, slots=True)
class CustomerStatementEmailOutcome:
    intent_id: UUID
    notification_id: UUID
    replayed: bool


_EMAIL_COMMAND = OwnerCommandDefinition(
    owner="customer.account_statement_email",
    concern="customer statement PDF email request",
    name="request_customer_statement_email",
)


def statement_range(
    *,
    start_date: str | None,
    end_date: str | None,
    today: date | None = None,
    timezone: ZoneInfo = ZoneInfo("Africa/Lagos"),
) -> CustomerStatementRange:
    """Normalize a customer date window with no maximum historical age."""
    current = today or datetime.now(timezone).date()
    try:
        end = date.fromisoformat(end_date) if end_date else current
        start = (
            date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
        )
    except ValueError as exc:
        raise CustomerStatementError(
            code="customer.account_statement.invalid_dates",
            message="Enter valid start and end dates.",
        ) from exc
    if start > end or end >= date.max:
        raise CustomerStatementError(
            code="customer.account_statement.invalid_dates",
            message="The start date must be on or before the end date.",
        )
    return CustomerStatementRange(start_date=start, end_date=end, timezone=timezone)


def list_statement_accounts(
    db: Session, *, allowed_account_ids: tuple[UUID, ...]
) -> tuple[CustomerStatementAccount, ...]:
    """Label only accounts already admitted by the customer context owner."""
    if not allowed_account_ids:
        return ()
    accounts = db.scalars(
        select(Subscriber)
        .where(Subscriber.id.in_(allowed_account_ids))
        .order_by(Subscriber.account_number, Subscriber.id)
    ).all()
    return tuple(
        CustomerStatementAccount(
            account_id=account.id,
            label=account.account_number or f"Account {str(account.id)[:8]}",
        )
        for account in accounts
    )


def read_customer_statement(
    db: Session, *, query: CustomerStatementQuery
) -> CustomerStatement:
    """Read one account's canonical activity without replaying archived history."""
    account = db.get(Subscriber, query.account_id)
    if account is None:
        raise CustomerStatementError(
            code="customer.account_statement.account_not_found",
            message="Account not found.",
        )
    events = list_customer_financial_events(
        db, query.account_id, end=query.period.end_exclusive, currency=None
    )
    opening: dict[str, Decimal] = {}
    activity: dict[str, Decimal] = {}
    running: dict[str, Decimal] = {}
    rows: list[CustomerStatementRow] = []
    for event in events:
        currency = display_format.currency_code(event.currency)
        signed = event.signed_amount
        if event.occurred_at < query.period.start:
            opening[currency] = opening.get(currency, Decimal("0")) + signed
            continue
        running[currency] = (
            running.get(currency, opening.get(currency, Decimal("0"))) + signed
        )
        activity[currency] = activity.get(currency, Decimal("0")) + signed
        rows.append(
            CustomerStatementRow(
                occurred_at=event.occurred_at,
                entry=event.id,
                entry_type=event.entry_type.value.replace("_", " ").title(),
                source=event.source.value.replace("_", " ").title(),
                debit=-signed if signed < 0 else Decimal("0"),
                credit=signed if signed > 0 else Decimal("0"),
                running_balance=running[currency],
                currency=currency,
                description=event.memo,
            )
        )
    receivables = {
        summary.currency: summary.outstanding
        for summary in get_customer_receivable_summaries(db, query.account_id)
    }
    currencies = sorted(
        set(opening) | set(activity) | set(receivables)
        or {display_format.default_currency(db)}
    )
    balances: list[CustomerStatementBalance] = []
    for currency in currencies:
        try:
            prepaid: Decimal | None = prepaid_available_balance(
                db, query.account_id, currency=currency
            )
        except Exception:
            # Missing funding authority is unavailable, never a fabricated zero.
            prepaid = None
        period_opening = opening.get(currency, Decimal("0"))
        period_activity = activity.get(currency, Decimal("0"))
        balances.append(
            CustomerStatementBalance(
                currency=currency,
                period_opening=period_opening,
                period_activity=period_activity,
                period_closing=period_opening + period_activity,
                amount_owed=receivables.get(currency, Decimal("0")),
                prepaid_credit=prepaid,
            )
        )
    return CustomerStatement(
        account_id=account.id,
        account_label=account.account_number or f"Account {str(account.id)[:8]}",
        period=query.period,
        balances=tuple(balances),
        rows=tuple(rows),
        generated_at=datetime.now(UTC),
    )


def statement_filename(statement: CustomerStatement) -> str:
    return (
        "account-statement-"
        f"{statement.period.start_date.isoformat()}-"
        f"{statement.period.end_date.isoformat()}.pdf"
    )


def render_statement_html(statement: CustomerStatement) -> str:
    """Build a printable, escaped view of the typed statement."""

    def money(value: Decimal | None, currency: str) -> str:
        return (
            "Unavailable"
            if value is None
            else escape(display_format.format_currency_amount(value, currency))
        )

    def movement(value: Decimal, currency: str) -> str:
        return "&mdash;" if value == 0 else money(value, currency)

    summaries = "".join(
        "<tr>"
        f"<td>{escape(balance.currency)}</td>"
        f"<td>{money(balance.period_opening, balance.currency)}</td>"
        f"<td>{money(balance.period_activity, balance.currency)}</td>"
        f"<td>{money(balance.period_closing, balance.currency)}</td>"
        f"<td>{money(balance.amount_owed, balance.currency)}</td>"
        f"<td>{money(balance.prepaid_credit, balance.currency)}</td>"
        "</tr>"
        for balance in statement.balances
    )
    rows = "".join(
        "<tr>"
        f"<td>{escape(row.entry.partition(':')[2][:12] or row.entry[:12])}</td>"
        f"<td>{escape(row.entry_type)}</td>"
        f"<td>{escape(row.source)}</td>"
        f"<td>{movement(row.debit, row.currency)}</td>"
        f"<td>{movement(row.credit, row.currency)}</td>"
        f"<td>{money(row.running_balance, row.currency)}</td>"
        f"<td>{escape(row.description)}</td>"
        f"<td>{escape(row.occurred_at.astimezone(statement.period.timezone).date().isoformat())}</td>"
        "</tr>"
        for row in statement.rows
    )
    activity_rows = rows or '<tr><td colspan="8">No activity in this period.</td></tr>'
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        "@page{size:A4 landscape;margin:16mm}body{font:11px Arial,sans-serif;color:#172323}"
        "h1{font-size:22px}h2{font-size:15px;margin-top:22px}"
        "table{width:100%;border-collapse:collapse;table-layout:fixed}"
        "th,td{border-bottom:1px solid #dbe5e3;padding:6px;text-align:left;"
        "vertical-align:top;overflow-wrap:anywhere}th{background:#eaf5ef}"
        "thead{display:table-header-group}tr{break-inside:avoid}"
        ".notice{padding:12px;background:#f4f6f5}"
        "</style></head><body>"
        "<h1>Account statement</h1>"
        f"<p>Account: {escape(statement.account_label)}<br>"
        f"Period: {statement.period.start_date.isoformat()} to "
        f"{statement.period.end_date.isoformat()}</p>"
        f"<p>Generated: {statement.generated_at.astimezone(statement.period.timezone).date().isoformat()} ({escape(statement.period.timezone.key)})</p>"
        "<h2>Account summary</h2><table><thead><tr>"
        "<th>Currency</th><th>Period opening</th><th>Activity</th>"
        "<th>Period closing</th><th>Current amount owed</th>"
        "<th>Current prepaid credit</th>"
        f"</tr></thead><tbody>{summaries}</tbody></table>"
        "<h2>History before the reviewed opening</h2>"
        "<p class='notice'>Unavailable. Older transaction history is not included "
        "in this statement.</p>"
        "<h2>History after the reviewed opening</h2>"
        "<table><thead><tr><th>Entry</th><th>Type</th>"
        "<th>Source</th><th>Debit</th><th>Credit</th>"
        "<th>Running balance</th><th>Description</th><th>Date</th></tr></thead>"
        f"<tbody>{activity_rows}</tbody>"
        "</table></body></html>"
    )


def render_statement_pdf(statement: CustomerStatement) -> bytes:
    from app.services.billing_invoice_pdf import _ensure_weasyprint_pydyf_compat

    _ensure_weasyprint_pydyf_compat()
    from weasyprint import HTML

    return HTML(string=render_statement_html(statement)).write_pdf()


def request_customer_statement_email(
    db: Session, *, command: CustomerStatementEmailCommand
) -> CustomerStatementEmailOutcome:
    """Queue one attachment to the signed-in principal's verified address."""

    def operation() -> CustomerStatementEmailOutcome:
        period = command.query.period
        dedupe_key = (
            f"customer-statement:{command.query.account_id}:"
            f"{period.start_date}:{period.end_date}:{command.idempotency_key}"
        )
        existing = db.scalar(
            select(CommunicationIntentRecord).where(
                CommunicationIntentRecord.dedupe_key == dedupe_key
            )
        )
        if existing is not None:
            metadata = existing.metadata_ or {}
            if (
                existing.subscriber_id != command.principal_id
                or metadata.get("statement_account_id") != str(command.query.account_id)
                or metadata.get("statement_principal_id") != str(command.principal_id)
                or metadata.get("statement_start_date") != period.start_date.isoformat()
                or metadata.get("statement_end_date") != period.end_date.isoformat()
                or metadata.get("statement_timezone") != period.timezone.key
                or len(existing.notifications) != 1
            ):
                raise CustomerStatementError(
                    code="customer.account_statement.email_idempotency_conflict",
                    message="This statement email request cannot be replayed.",
                )
            return CustomerStatementEmailOutcome(
                intent_id=existing.id,
                notification_id=existing.notifications[0].id,
                replayed=True,
            )
        account = db.get(Subscriber, command.query.account_id)
        if account is None:
            raise CustomerStatementError(
                code="customer.account_statement.account_not_found",
                message="Account not found.",
            )
        principal = db.get(Subscriber, command.principal_id)
        if (
            principal is None
            or not principal.email_verified
            or not principal.email.strip()
        ):
            raise CustomerStatementError(
                code="customer.account_statement.email_unverified",
                message="Verify your email before emailing a statement.",
            )
        pdf = render_statement_pdf(read_customer_statement(db, query=command.query))
        if len(pdf) > MAX_EMAIL_ATTACHMENT_BYTES:
            raise CustomerStatementError(
                code="customer.account_statement.email_attachment_too_large",
                message="This statement is too large to email. Download the PDF instead.",
            )
        filename = (
            f"account-statement-{period.start_date.isoformat()}-"
            f"{period.end_date.isoformat()}.pdf"
        )
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=principal.id,
                event_type="customer.account_statement.email_requested",
                category="billing",
                subject="Your account statement",
                body=(
                    "Your account statement is attached. History before the "
                    "reviewed opening is unavailable and is not included."
                ),
                channels=(NotificationChannel.email,),
                include_reseller=False,
                resolve_subscriber_identity=False,
                recipients={NotificationChannel.email: principal.email.strip()},
                attachments=(
                    CommunicationAttachment(
                        kind=CommunicationAttachmentKind.customer_statement_pdf,
                        entity_id=account.id,
                        filename=filename,
                    ),
                ),
                metadata={
                    "statement_start_date": period.start_date.isoformat(),
                    "statement_end_date": period.end_date.isoformat(),
                    "statement_timezone": period.timezone.key,
                    "statement_account_id": str(account.id),
                    "statement_principal_id": str(principal.id),
                },
                dedupe_key=dedupe_key,
            ),
        )
        notifications = result.queued or result.deliveries
        if (not result.replayed and len(result.queued) != 1) or len(notifications) != 1:
            raise CustomerStatementError(
                code="customer.account_statement.email_unavailable",
                message="The statement email could not be queued.",
            )
        if result.replayed:
            return CustomerStatementEmailOutcome(
                intent_id=result.intent_id,
                notification_id=notifications[0].id,
                replayed=True,
            )
        stage_audit_event(
            db,
            action="customer_statement.email_queued",
            entity_type="customer_account_statement",
            entity_id=str(account.id),
            actor_type=AuditActorType.user,
            actor_id=str(command.principal_id),
            metadata={
                "start_date": period.start_date.isoformat(),
                "end_date": period.end_date.isoformat(),
                "communication_intent_id": str(result.intent_id),
            },
        )
        emit_event(
            db,
            EventType.customer_account_statement_email_queued,
            {
                "account_id": str(account.id),
                "communication_intent_id": str(result.intent_id),
            },
            actor=str(command.principal_id),
        )
        db.flush()
        return CustomerStatementEmailOutcome(
            intent_id=result.intent_id,
            notification_id=notifications[0].id,
            replayed=result.replayed,
        )

    return execute_owner_command(
        db,
        definition=_EMAIL_COMMAND,
        context=command.context,
        operation=operation,
    )
