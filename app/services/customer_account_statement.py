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

from app.models.subscriber import Subscriber
from app.services import display_format
from app.services.brand_profiles import (
    resolve_brand,
    resolve_brand_logo_source,
)
from app.services.brand_theme import generate_scale
from app.services.customer_financial_ledger import list_customer_financial_events
from app.services.customer_financial_position import (
    get_customer_receivable_summaries,
    prepaid_available_balance,
)
from app.services.domain_errors import DomainError


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
class CustomerStatementBrand:
    legal_name: str
    product_name: str
    tagline: str
    primary_color: str
    primary_dark_color: str
    primary_tint_color: str
    secondary_color: str
    logo_source: str | None
    support_email: str
    support_phone: str
    address_lines: tuple[str, ...]
    source_scope: str
    source_scope_id: str | None


@dataclass(frozen=True, slots=True)
class CustomerStatementDocument:
    statement: CustomerStatement
    brand: CustomerStatementBrand


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


def read_customer_statement_document(
    db: Session, *, query: CustomerStatementQuery
) -> CustomerStatementDocument:
    """Compose canonical financial facts with the customer-facing brand."""
    statement = read_customer_statement(db, query=query)
    resolved = resolve_brand(db, subscriber_id=query.account_id)
    primary_scale = generate_scale(resolved.primary_color)
    secondary_scale = generate_scale(resolved.secondary_color)
    address = resolved.legal_address or {}
    city_line = " ".join(
        part.strip()
        for part in (
            str(address.get("city") or ""),
            str(address.get("postal_code") or ""),
        )
        if part.strip()
    )
    address_lines = tuple(
        line
        for line in (
            str(address.get("street1") or "").strip(),
            str(address.get("street2") or "").strip(),
            city_line,
            str(address.get("country") or "").strip(),
        )
        if line
    )
    return CustomerStatementDocument(
        statement=statement,
        brand=CustomerStatementBrand(
            legal_name=(resolved.legal_name or resolved.product_name or resolved.name),
            product_name=(resolved.product_name or resolved.name),
            tagline=resolved.tagline,
            primary_color=primary_scale[600],
            primary_dark_color=primary_scale[900],
            primary_tint_color=primary_scale[50],
            secondary_color=secondary_scale[600],
            logo_source=resolve_brand_logo_source(
                db, configured_logo=resolved.logo_url
            ),
            support_email=resolved.support_email,
            support_phone=resolved.support_phone,
            address_lines=address_lines,
            source_scope=resolved.source_scope,
            source_scope_id=resolved.source_scope_id,
        ),
    )


def statement_filename(statement: CustomerStatement) -> str:
    return (
        "account-statement-"
        f"{statement.period.start_date.isoformat()}-"
        f"{statement.period.end_date.isoformat()}.pdf"
    )


def render_statement_html(document: CustomerStatementDocument) -> str:
    """Build a printable, escaped view of the typed statement."""

    statement = document.statement
    brand = document.brand

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
    activity_rows = rows or (
        '<tr><td class="empty" colspan="8">No activity in this period.</td></tr>'
    )
    legal_name = escape(brand.legal_name)
    footer_name = (
        brand.legal_name.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )
    contact_parts = tuple(
        value
        for value in (
            escape(brand.support_email.strip()),
            escape(brand.support_phone.strip()),
        )
        if value
    )
    company_lines = "".join(
        f"<div>{escape(line)}</div>" for line in brand.address_lines
    )
    contact_line = " &nbsp;&middot;&nbsp; ".join(contact_parts)
    logo_markup = (
        f'<img class="logo" src="{escape(brand.logo_source, quote=True)}" '
        f'alt="{legal_name} logo">'
        if brand.logo_source
        else f'<div class="logo-fallback">{escape(brand.legal_name[:1].upper())}</div>'
    )
    generated = statement.generated_at.astimezone(statement.period.timezone)
    generated_label = generated.strftime("%d %b %Y, %H:%M")
    period_label = (
        f"{statement.period.start_date.strftime('%d %b %Y')} &ndash; "
        f"{statement.period.end_date.strftime('%d %b %Y')}"
    )
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Account statement - {escape(statement.account_label)}</title>
<style>
  @page {{
    size: A4 landscape;
    margin: 13mm 13mm 17mm;
    @bottom-left {{
      content: "{footer_name}  |  Account statement";
      color: #64748b;
      font: 8px "DejaVu Sans", Arial, sans-serif;
    }}
    @bottom-right {{
      content: "Page " counter(page) " of " counter(pages);
      color: #64748b;
      font: 8px "DejaVu Sans", Arial, sans-serif;
    }}
  }}
  :root {{
    --brand: {brand.primary_color};
    --brand-dark: {brand.primary_dark_color};
    --brand-tint: {brand.primary_tint_color};
    --accent: {brand.secondary_color};
    --ink: #0f172a;
    --muted: #64748b;
    --line: #dbe3ea;
    --soft: #f8fafc;
    --white: #ffffff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    color: var(--ink);
    background: var(--white);
    font: 9.5px/1.45 "DejaVu Sans", Arial, sans-serif;
  }}
  .masthead {{
    display: flex;
    justify-content: space-between;
    align-items: stretch;
    min-height: 124px;
    overflow: hidden;
    border-radius: 14px;
    background: var(--brand-dark);
    color: var(--white);
  }}
  .company {{ display: flex; gap: 14px; padding: 19px 20px; max-width: 58%; }}
  .logo {{
    display: block;
    max-width: 148px;
    max-height: 54px;
    padding: 6px 9px;
    border-radius: 9px;
    background: var(--white);
    object-fit: contain;
  }}
  .logo-fallback {{
    width: 50px;
    height: 50px;
    border: 1px solid rgba(255,255,255,.32);
    border-radius: 12px;
    background: rgba(255,255,255,.14);
    font-size: 23px;
    font-weight: 800;
    line-height: 50px;
    text-align: center;
  }}
  .company-copy {{ padding-top: 1px; }}
  .legal-name {{ margin: 0 0 5px; font-size: 16px; font-weight: 750; }}
  .tagline {{ margin: 0 0 9px; color: rgba(255,255,255,.82); }}
  .company-detail {{ color: rgba(255,255,255,.9); font-size: 8.5px; }}
  .statement-title {{
    min-width: 37%;
    padding: 19px 21px;
    background: rgba(255,255,255,.1);
    border-left: 1px solid rgba(255,255,255,.2);
  }}
  .eyebrow {{
    margin: 0 0 4px;
    color: rgba(255,255,255,.75);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: .18em;
    text-transform: uppercase;
  }}
  h1 {{ margin: 0 0 13px; font-size: 24px; line-height: 1.05; }}
  .meta {{ width: 100%; border-collapse: collapse; }}
  .meta td {{ padding: 2px 0; border: 0; color: var(--white); }}
  .meta td:first-child {{ width: 39%; color: rgba(255,255,255,.72); }}
  .summary {{ margin-top: 13px; }}
  .section-heading {{
    margin: 16px 0 7px;
    color: var(--brand-dark);
    font-size: 12px;
    font-weight: 800;
  }}
  table.data {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  table.data th, table.data td {{
    padding: 6px 7px;
    border-bottom: 1px solid var(--line);
    text-align: left;
    vertical-align: top;
    overflow-wrap: anywhere;
  }}
  table.data th {{
    background: var(--brand-tint);
    color: var(--brand-dark);
    font-size: 8px;
    font-weight: 800;
    letter-spacing: .02em;
  }}
  table.data tbody tr:nth-child(even) {{ background: var(--soft); }}
  table.data thead {{ display: table-header-group; }}
  table.data tr {{ break-inside: avoid; }}
  .notice {{
    margin: 0;
    padding: 9px 11px;
    border-left: 4px solid var(--accent);
    border-radius: 4px;
    background: var(--soft);
    color: #475569;
  }}
  .empty {{ padding: 17px !important; color: var(--muted); text-align: center !important; }}
  .activity th:nth-child(1) {{ width: 9%; }}
  .activity th:nth-child(2) {{ width: 10%; }}
  .activity th:nth-child(3) {{ width: 10%; }}
  .activity th:nth-child(4), .activity th:nth-child(5) {{ width: 10%; }}
  .activity th:nth-child(6) {{ width: 13%; }}
  .activity th:nth-child(7) {{ width: 25%; }}
  .activity th:nth-child(8) {{ width: 13%; }}
</style>
</head>
<body>
  <header class="masthead">
    <div class="company">
      <div>{logo_markup}</div>
      <div class="company-copy">
        <p class="legal-name">{legal_name}</p>
        <p class="tagline">{escape(brand.tagline)}</p>
        <div class="company-detail">{company_lines}</div>
        <div class="company-detail">{contact_line}</div>
      </div>
    </div>
    <div class="statement-title">
      <p class="eyebrow">Official customer document</p>
      <h1>Account statement</h1>
      <table class="meta" role="presentation">
        <tr><td>Account</td><td>{escape(statement.account_label)}</td></tr>
        <tr><td>Statement period</td><td>{period_label}</td></tr>
        <tr><td>Generated</td><td>{generated_label} ({escape(statement.period.timezone.key)})</td></tr>
      </table>
    </div>
  </header>
  <section class="summary">
    <h2 class="section-heading">Account summary</h2>
    <table class="data"><thead><tr>
      <th>Currency</th><th>Period opening</th><th>Activity</th>
      <th>Period closing</th><th>Current amount owed</th>
      <th>Current prepaid credit</th>
    </tr></thead><tbody>{summaries}</tbody></table>
  </section>
  <section>
    <h2 class="section-heading">History before the reviewed opening</h2>
    <p class="notice">Unavailable. Older transaction history is not included in this statement.</p>
  </section>
  <section>
    <h2 class="section-heading">History after the reviewed opening</h2>
    <table class="data activity"><thead><tr>
      <th>Entry</th><th>Type</th><th>Source</th><th>Debit</th>
      <th>Credit</th><th>Running balance</th><th>Description</th><th>Date</th>
    </tr></thead><tbody>{activity_rows}</tbody></table>
  </section>
</body>
</html>"""


def render_statement_pdf(document: CustomerStatementDocument) -> bytes:
    from app.services.billing_invoice_pdf import _ensure_weasyprint_pydyf_compat

    _ensure_weasyprint_pydyf_compat()
    from weasyprint import HTML

    return HTML(string=render_statement_html(document)).write_pdf()
