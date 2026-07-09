"""Field service is work-order execution, not sales/project entry."""

from __future__ import annotations

from pathlib import Path

FIELD_ROOTS = (Path("app/api/field"), Path("app/services/field"))
FIELD_AUTHORITY_PATHS = FIELD_ROOTS + (
    Path("app/models/work_order_mirror.py"),
    Path("app/schemas/field.py"),
    Path("app/services/work_order_views.py"),
)
PORTAL_FIELD_SERVICE_PATHS = (
    Path("app/api/me.py"),
    Path("app/api/reseller.py"),
    Path("app/web/customer/work_orders.py"),
    Path("app/services/web_reseller_routes.py"),
)
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
    "pending-sync",
    "crm_sync",
    "writeback",
    "write-back",
)
FORBIDDEN_CRM_AUTHORITY_PATTERNS = (
    "source of truth",
    "crm owns",
    "keeps crm",
    "crm remains",
    "crm-synced",
    "crm-sourced",
)
FORBIDDEN_PORTAL_CRM_METHODS = (
    "get_portal_technician_location",
    "submit_portal_technician_rating",
)


def _python_files(paths: tuple[Path, ...]):
    for path in paths:
        if path.is_dir():
            yield from path.rglob("*.py")
        elif path.exists():
            yield path


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
    for path in _python_files(FIELD_ROOTS):
        text = path.read_text().casefold()
        for pattern in FORBIDDEN_WRITEBACK_PATTERNS:
            if pattern in text:
                offenders.append(f"{path}: contains {pattern!r}")
    assert not offenders, (
        "Native field writes are authoritative in sub. Do not add CRM write-back "
        "or pending-sync machinery under field service:\n  " + "\n  ".join(offenders)
    )


def test_field_work_order_docs_do_not_present_crm_as_authoritative():
    offenders: list[str] = []
    for path in _python_files(FIELD_AUTHORITY_PATHS):
        text = path.read_text().casefold()
        for pattern in FORBIDDEN_CRM_AUTHORITY_PATTERNS:
            if pattern in text:
                offenders.append(f"{path}: contains {pattern!r}")
    assert not offenders, (
        "Phase 2 work-order execution is sub-authoritative. CRM may import or "
        "backfill legacy headers, but field modules must not describe CRM as "
        "the owner for native field activity:\n  " + "\n  ".join(offenders)
    )


def test_portal_field_service_actions_do_not_proxy_crm():
    offenders: list[str] = []
    for path in _python_files(PORTAL_FIELD_SERVICE_PATHS):
        text = path.read_text()
        for pattern in FORBIDDEN_PORTAL_CRM_METHODS:
            if pattern in text:
                offenders.append(f"{path}: contains {pattern!r}")
    assert not offenders, (
        "Customer/reseller field-service location and rating actions are local "
        "sub behavior. Do not reintroduce CRM portal proxy calls here:\n  "
        + "\n  ".join(offenders)
    )
