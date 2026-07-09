"""Field service is work-order execution, not sales/project entry."""

from __future__ import annotations

from pathlib import Path

FIELD_ROOTS = (Path("app/api/field"), Path("app/services/field"))
FORBIDDEN_FILES = {
    Path("app/api/field/sales_orders.py"),
    Path("app/services/field/sales_orders.py"),
    Path("app/api/field/vendor_projects.py"),
    Path("app/services/field/vendor_projects.py"),
    Path("app/api/field/vendor_quotes.py"),
    Path("app/services/field/vendor_quotes.py"),
}
FORBIDDEN_PATTERNS = (
    "sales-orders",
    "sales_orders",
    "SalesOrder",
    "sales_order",
    "vendor/projects",
    "vendor/quotes",
    "ProjectQuote",
    "project_quotes",
    "InstallationProject",
)
FORBIDDEN_WRITEBACK_PATTERNS = (
    "pending_sync",
    "crm_sync",
    "writeback",
    "write-back",
)


def test_field_service_does_not_expose_sales_or_project_entry_modules():
    existing = sorted(str(path) for path in FORBIDDEN_FILES if path.exists())
    assert not existing, (
        "Field service must not expose sales/project entry modules. Re-home "
        "sales orders, vendor project bidding, and quote workflows under their "
        "CRM/vendor/project domains instead:\n  " + "\n  ".join(existing)
    )


def test_field_modules_do_not_depend_on_sales_or_project_entry_domains():
    offenders: list[str] = []
    for root in FIELD_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text()
            for pattern in FORBIDDEN_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path}: contains {pattern!r}")
    assert not offenders, (
        "Field modules should process work orders only. Create a sales/CRM "
        "workflow, vendor-project workflow, or work-order follow-up instead of "
        "adding sales/project entry behavior here:\n  " + "\n  ".join(offenders)
    )


def test_field_modules_do_not_reintroduce_crm_writeback():
    offenders: list[str] = []
    for root in FIELD_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text().casefold()
            for pattern in FORBIDDEN_WRITEBACK_PATTERNS:
                if pattern in text:
                    offenders.append(f"{path}: contains {pattern!r}")
    assert not offenders, (
        "Native field writes are authoritative in sub. Do not add CRM write-back "
        "or pending-sync machinery under field service:\n  " + "\n  ".join(offenders)
    )
