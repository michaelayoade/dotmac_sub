"""Rendered record identities and exact financial evidence survive list compaction."""

from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader, select_autoescape


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.destinations: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.destinations.extend(
                value for key, value in attrs if key == "href" and value
            )


def _table(name: str, context: dict[str, object]) -> str:
    source = Path(f"templates/admin/billing/{name}").read_text(encoding="utf-8")
    source = source[source.index("<table") : source.index("</table>") + 8]
    environment = Environment(
        loader=FileSystemLoader("templates"), autoescape=select_autoescape(default=True)
    )
    environment.filters["app_datetime"] = lambda value, *_args, **_kwargs: value
    return environment.from_string(
        '{% from "components/ui/macros.html" import table_row, empty_state %}' + source
    ).render(**context)


def test_ledger_entry_opens_its_record_without_losing_currency_or_precision() -> None:
    entry = SimpleNamespace(
        id="entry-123",
        account_id="account-456",
        account=SimpleNamespace(name="A&B"),
        entry_type=SimpleNamespace(value="debit"),
        source=SimpleNamespace(value="manual"),
        memo="Correction <script>alert(1)</script>",
        currency="USD",
        amount=Decimal("123456789.99"),
        effective_date=None,
        created_at="2026-11-09",
    )
    html = _table(
        "ledger.html",
        {
            "entries": [entry],
            "ledger_totals": SimpleNamespace(
                credit_count=0,
                credit_display="USD 0.00",
                debit_count=1,
                debit_display="USD 123,456,789.99",
                net_display="USD 123,456,789.99",
            ),
        },
    )
    links = Links()
    links.feed(html)
    assert "/admin/billing/ledger/entry-123" in links.destinations
    assert "/admin/billing/accounts/account-456" in links.destinations
    assert "USD 123,456,789.99" in html
    assert ">Debit<" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_batch_investigation_keeps_evidence_and_retry_confirmation() -> None:
    run = SimpleNamespace(
        id="run-123",
        run_at="2026-11-09",
        billing_cycle="monthly",
        launch_kind="manual",
        source_run_id="original-run",
        subscriptions_scanned=12,
        subscriptions_billed=10,
        invoices_created=10,
        lines_created=20,
        skipped=2,
        duration_seconds=5,
        status_badge="warning",
        status="Partial",
        status_message="Provider timed out",
        retry_allowed=True,
    )
    context: dict[str, object] = {
        "recent_runs": [run],
        "can_write": True,
        "request": SimpleNamespace(state=SimpleNamespace(csrf_token="csrf-fixture")),
    }
    html = _table("_invoice_batch_history_table.html", context)
    assert "Provider timed out" in html
    assert "original-run" in html
    assert 'action="/admin/billing/invoices/batch/run-123/retry/preview"' in html
    assert 'value="csrf-fixture"' in html
    context["can_write"] = False
    assert "/retry/preview" not in _table("_invoice_batch_history_table.html", context)


def test_ledger_no_results_preserves_the_six_column_grid() -> None:
    html = _table("ledger.html", {"entries": [], "customer_ref": "customer-123"})
    assert html.count('<th scope="col"') == 6
    assert 'colspan="6"' in html
    assert 'colspan="8"' not in html
    assert "No ledger entries found" in html
    assert "try clearing the partner" in html
