"""Make back-office projections provider-neutral.

Revision ID: 383_replaceable_backoffice
Revises: 380_integration_platform_cutover
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "383_replaceable_backoffice"
down_revision = "382_ticket_work_order_handoff"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _unique_constraints(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table)
        if constraint.get("name")
    }


def _rename_column(table: str, old: str, new: str) -> None:
    columns = _columns(table)
    if old not in columns:
        return
    if new not in columns:
        op.alter_column(table, old, new_column_name=new)
        return

    # Revision 001 creates fresh databases from current ORM metadata. A later
    # historical migration can therefore add the legacy name beside the
    # provider-neutral column before this revision runs. Preserve any legacy
    # value that is not yet projected, then remove the duplicate old column.
    projection = sa.table(table, sa.column(old), sa.column(new))
    op.execute(
        projection.update()
        .where(projection.c[new].is_(None))
        .values({new: projection.c[old]})
    )
    op.drop_column(table, old)


def _add_string(table: str, column: str, length: int) -> None:
    if column not in _columns(table):
        op.add_column(table, sa.Column(column, sa.String(length), nullable=True))


def _add_datetime(table: str, column: str) -> None:
    if column not in _columns(table):
        op.add_column(
            table, sa.Column(column, sa.DateTime(timezone=True), nullable=True)
        )


def _rename_index(
    table: str,
    old: str,
    new: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    indexes = _indexes(table)
    if old in indexes:
        op.drop_index(old, table_name=table)
    if new not in _indexes(table):
        op.create_index(new, table, columns, unique=unique)


def _replace_unique_constraint(
    table: str,
    old: str,
    new: str,
    columns: list[str],
) -> None:
    constraints = _unique_constraints(table)
    if old in constraints:
        op.drop_constraint(old, table, type_="unique")
    if new not in _unique_constraints(table):
        op.create_unique_constraint(new, table, columns)


def _drop_unique_constraint(table: str, name: str) -> None:
    if name in _unique_constraints(table):
        op.drop_constraint(name, table, type_="unique")


def _create_unique_constraint(table: str, name: str, columns: list[str]) -> None:
    if name not in _unique_constraints(table):
        op.create_unique_constraint(name, table, columns)


def upgrade() -> None:
    # Service-support and reimbursement projections.
    _rename_column(
        "field_material_requests", "erp_material_request_id", "support_reference"
    )
    _rename_index(
        "field_material_requests",
        "ix_field_material_requests_erp_material_request_id",
        "ix_field_material_requests_support_reference",
        ["support_reference"],
    )
    _rename_column("field_material_requests", "erp_material_status", "support_status")
    _add_string("field_material_requests", "support_system", 40)
    op.execute(
        sa.text(
            "UPDATE field_material_requests SET support_system = 'dotmac_erp' "
            "WHERE support_system IS NULL AND "
            "(support_reference IS NOT NULL OR support_status IS NOT NULL)"
        )
    )

    _rename_column(
        "field_expense_requests", "erp_expense_claim_id", "expense_claim_reference"
    )
    _rename_column("field_expense_requests", "erp_claim_number", "expense_claim_number")
    _rename_column("field_expense_requests", "erp_claim_status", "expense_claim_status")
    _add_string("field_expense_requests", "expense_system", 40)
    op.execute(
        sa.text(
            "UPDATE field_expense_requests SET expense_system = 'dotmac_erp' "
            "WHERE expense_system IS NULL AND (expense_claim_reference IS NOT NULL "
            "OR expense_claim_number IS NOT NULL OR expense_claim_status IS NOT NULL)"
        )
    )

    # Workforce projections used only for operational routing in Sub.
    _rename_column(
        "technician_profiles", "erp_employee_id", "workforce_employee_reference"
    )
    if "ix_technician_profiles_erp_employee_id" in _indexes("technician_profiles"):
        op.drop_index(
            "ix_technician_profiles_erp_employee_id",
            table_name="technician_profiles",
        )
    _add_string("technician_profiles", "workforce_system", 40)
    op.execute(
        sa.text(
            "UPDATE technician_profiles SET workforce_system = 'dotmac_erp' "
            "WHERE workforce_system IS NULL AND workforce_employee_reference IS NOT NULL"
        )
    )
    _replace_unique_constraint(
        "technician_profiles",
        "uq_technician_profiles_erp_employee_id",
        "uq_technician_profiles_workforce_system_reference",
        ["workforce_system", "workforce_employee_reference"],
    )

    for table in ("shifts", "availability_blocks"):
        _rename_column(table, "erp_id", "workforce_record_reference")
        if f"ix_{table}_erp_id" in _indexes(table):
            op.drop_index(f"ix_{table}_erp_id", table_name=table)
        _add_string(table, "workforce_system", 40)
        op.execute(
            sa.text(
                f"UPDATE {table} SET workforce_system = 'dotmac_erp' "
                "WHERE workforce_system IS NULL AND workforce_record_reference IS NOT NULL"
            )
        )
        _replace_unique_constraint(
            table,
            f"uq_{table}_erp_id",
            f"uq_{table}_workforce_system_reference",
            ["workforce_system", "workforce_record_reference"],
        )

    _rename_column("service_teams", "erp_department", "workforce_department_reference")
    _add_string("service_teams", "workforce_system", 40)
    op.execute(
        sa.text(
            "UPDATE service_teams SET workforce_system = 'dotmac_erp' "
            "WHERE workforce_system IS NULL AND workforce_department_reference IS NOT NULL"
        )
    )
    _replace_unique_constraint(
        "service_teams",
        "uq_service_teams_erp_department",
        "uq_service_teams_workforce_system_reference",
        ["workforce_system", "workforce_department_reference"],
    )

    # External account and procurement/payables projections.
    _rename_column("organizations", "erp_id", "backoffice_account_reference")
    if "ix_organizations_erp" in _indexes("organizations"):
        op.drop_index("ix_organizations_erp", table_name="organizations")
    _add_string("organizations", "backoffice_system", 40)
    op.execute(
        sa.text(
            "UPDATE organizations SET backoffice_system = 'dotmac_erp' "
            "WHERE backoffice_system IS NULL AND backoffice_account_reference IS NOT NULL"
        )
    )
    _replace_unique_constraint(
        "organizations",
        "uq_organizations_erp_id",
        "uq_organizations_backoffice_system_reference",
        ["backoffice_system", "backoffice_account_reference"],
    )

    _rename_column("organizations", "erpnext_id", "legacy_account_reference")
    if "ix_organizations_erpnext_id" in _indexes("organizations"):
        op.drop_index("ix_organizations_erpnext_id", table_name="organizations")
    _add_string("organizations", "legacy_account_system", 40)
    op.execute(
        sa.text(
            "UPDATE organizations SET legacy_account_system = 'erpnext' "
            "WHERE legacy_account_system IS NULL "
            "AND legacy_account_reference IS NOT NULL"
        )
    )
    _create_unique_constraint(
        "organizations",
        "uq_organizations_legacy_account_system_reference",
        ["legacy_account_system", "legacy_account_reference"],
    )

    for table, constraint in (
        ("projects", "uq_projects_external_system_reference"),
        ("project_tasks", "uq_project_tasks_external_system_reference"),
    ):
        _rename_column(table, "erpnext_id", "external_reference")
        old_index = f"ix_{table}_erpnext_id"
        if old_index in _indexes(table):
            op.drop_index(old_index, table_name=table)
        _add_string(table, "external_system", 40)
        op.execute(
            sa.text(
                f"UPDATE {table} SET external_system = 'erpnext' "
                "WHERE external_system IS NULL AND external_reference IS NOT NULL"
            )
        )
        _create_unique_constraint(
            table,
            constraint,
            ["external_system", "external_reference"],
        )

    _rename_column("support_tickets", "erpnext_id", "external_reference")
    if "ix_support_tickets_erpnext_id" in _indexes("support_tickets"):
        op.drop_index("ix_support_tickets_erpnext_id", table_name="support_tickets")
    _add_string("support_tickets", "external_system", 40)
    op.execute(
        sa.text(
            "UPDATE support_tickets SET external_system = 'erpnext' "
            "WHERE external_system IS NULL AND external_reference IS NOT NULL"
        )
    )
    if "ix_support_tickets_external_system_reference" not in _indexes(
        "support_tickets"
    ):
        op.create_index(
            "ix_support_tickets_external_system_reference",
            "support_tickets",
            ["external_system", "external_reference"],
        )

    _rename_column("vendors", "erp_id", "supplier_reference")
    if "ix_vendors_erp_id" in _indexes("vendors"):
        op.drop_index("ix_vendors_erp_id", table_name="vendors")
    _add_string("vendors", "supplier_system", 40)
    op.execute(
        sa.text(
            "UPDATE vendors SET supplier_system = 'dotmac_erp' "
            "WHERE supplier_system IS NULL AND supplier_reference IS NOT NULL"
        )
    )
    _replace_unique_constraint(
        "vendors",
        "uq_vendors_erp_id",
        "uq_vendors_supplier_system_reference",
        ["supplier_system", "supplier_reference"],
    )

    _rename_column(
        "installation_projects",
        "erp_purchase_order_id",
        "procurement_order_reference",
    )
    _rename_index(
        "installation_projects",
        "ix_installation_projects_erp_purchase_order_id",
        "ix_installation_projects_procurement_order_reference",
        ["procurement_order_reference"],
    )
    _add_string("installation_projects", "procurement_system", 40)
    _add_string("installation_projects", "procurement_delivery_status", 40)
    _add_string("installation_projects", "procurement_delivery_error", 500)
    _add_datetime("installation_projects", "procurement_delivered_at")
    op.execute(
        sa.text(
            "UPDATE installation_projects SET procurement_system = 'dotmac_erp' "
            "WHERE procurement_system IS NULL AND procurement_order_reference IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE installation_projects SET procurement_delivery_status = 'accepted' "
            "WHERE procurement_delivery_status IS NULL "
            "AND procurement_order_reference IS NOT NULL"
        )
    )

    invoice_renames = {
        "erp_purchase_order_id": "procurement_order_reference",
        "erp_purchase_invoice_id": "payables_document_reference",
        "erp_purchase_invoice_creation_status": "payables_document_status",
        "erp_purchase_invoice_status": "payment_status",
        "erp_purchase_invoice_total_amount": "payment_total_amount",
        "erp_purchase_invoice_amount_paid": "payment_amount_paid",
        "erp_purchase_invoice_balance_due": "payment_balance_due",
        "erp_purchase_invoice_status_observed_at": "payment_observed_at",
        "erp_purchase_invoice_status_source_updated_at": "payment_source_updated_at",
        "erp_purchase_invoice_status_error": "payment_observation_error",
        "erp_sync_error": "payables_submission_error",
        "erp_synced_at": "payables_submitted_at",
        "erp_attachment_synced_at": "payables_attachment_submitted_at",
    }
    for old, new in invoice_renames.items():
        _rename_column("vendor_purchase_invoices", old, new)
    _rename_index(
        "vendor_purchase_invoices",
        "ix_vendor_purchase_invoices_erp_purchase_order_id",
        "ix_vendor_purchase_invoices_procurement_order_reference",
        ["procurement_order_reference"],
    )
    _rename_index(
        "vendor_purchase_invoices",
        "ix_vendor_purchase_invoices_erp_purchase_invoice_id",
        "ix_vendor_purchase_invoices_payables_document_reference",
        ["payables_document_reference"],
    )
    _add_string("vendor_purchase_invoices", "payables_system", 40)
    op.execute(
        sa.text(
            "UPDATE vendor_purchase_invoices SET payables_system = 'dotmac_erp' "
            "WHERE payables_system IS NULL AND (payables_document_reference IS NOT NULL "
            "OR payables_document_status IS NOT NULL OR payment_status IS NOT NULL)"
        )
    )

    for old, new in {
        "erp_sync_status": "backoffice_sync_status",
        "erp_reference": "backoffice_reference",
        "erp_sync_at": "backoffice_synced_at",
    }.items():
        _rename_column("as_built_routes", old, new)


def downgrade() -> None:
    for old, new in {
        "backoffice_sync_status": "erp_sync_status",
        "backoffice_reference": "erp_reference",
        "backoffice_synced_at": "erp_sync_at",
    }.items():
        _rename_column("as_built_routes", old, new)

    invoice_renames = {
        "procurement_order_reference": "erp_purchase_order_id",
        "payables_document_reference": "erp_purchase_invoice_id",
        "payables_document_status": "erp_purchase_invoice_creation_status",
        "payment_status": "erp_purchase_invoice_status",
        "payment_total_amount": "erp_purchase_invoice_total_amount",
        "payment_amount_paid": "erp_purchase_invoice_amount_paid",
        "payment_balance_due": "erp_purchase_invoice_balance_due",
        "payment_observed_at": "erp_purchase_invoice_status_observed_at",
        "payment_source_updated_at": "erp_purchase_invoice_status_source_updated_at",
        "payment_observation_error": "erp_purchase_invoice_status_error",
        "payables_submission_error": "erp_sync_error",
        "payables_submitted_at": "erp_synced_at",
        "payables_attachment_submitted_at": "erp_attachment_synced_at",
    }
    for old, new in invoice_renames.items():
        _rename_column("vendor_purchase_invoices", old, new)
    if "payables_system" in _columns("vendor_purchase_invoices"):
        op.drop_column("vendor_purchase_invoices", "payables_system")
    _rename_index(
        "vendor_purchase_invoices",
        "ix_vendor_purchase_invoices_procurement_order_reference",
        "ix_vendor_purchase_invoices_erp_purchase_order_id",
        ["erp_purchase_order_id"],
    )
    _rename_index(
        "vendor_purchase_invoices",
        "ix_vendor_purchase_invoices_payables_document_reference",
        "ix_vendor_purchase_invoices_erp_purchase_invoice_id",
        ["erp_purchase_invoice_id"],
    )

    for column in (
        "procurement_delivered_at",
        "procurement_delivery_error",
        "procurement_delivery_status",
        "procurement_system",
    ):
        if column in _columns("installation_projects"):
            op.drop_column("installation_projects", column)
    _rename_column(
        "installation_projects",
        "procurement_order_reference",
        "erp_purchase_order_id",
    )
    _rename_index(
        "installation_projects",
        "ix_installation_projects_procurement_order_reference",
        "ix_installation_projects_erp_purchase_order_id",
        ["erp_purchase_order_id"],
    )

    _replace_unique_constraint(
        "vendors",
        "uq_vendors_supplier_system_reference",
        "uq_vendors_erp_id",
        ["supplier_reference"],
    )
    if "supplier_system" in _columns("vendors"):
        op.drop_column("vendors", "supplier_system")
    _rename_column("vendors", "supplier_reference", "erp_id")
    if "ix_vendors_erp_id" not in _indexes("vendors"):
        op.create_index("ix_vendors_erp_id", "vendors", ["erp_id"])

    _replace_unique_constraint(
        "organizations",
        "uq_organizations_backoffice_system_reference",
        "uq_organizations_erp_id",
        ["backoffice_account_reference"],
    )
    if "backoffice_system" in _columns("organizations"):
        op.drop_column("organizations", "backoffice_system")
    _rename_column("organizations", "backoffice_account_reference", "erp_id")
    if "ix_organizations_erp" not in _indexes("organizations"):
        op.create_index("ix_organizations_erp", "organizations", ["erp_id"])

    _drop_unique_constraint(
        "organizations", "uq_organizations_legacy_account_system_reference"
    )
    if "legacy_account_system" in _columns("organizations"):
        op.drop_column("organizations", "legacy_account_system")
    _rename_column("organizations", "legacy_account_reference", "erpnext_id")
    if "ix_organizations_erpnext_id" not in _indexes("organizations"):
        op.create_index(
            "ix_organizations_erpnext_id",
            "organizations",
            ["erpnext_id"],
            unique=True,
        )

    if "ix_support_tickets_external_system_reference" in _indexes("support_tickets"):
        op.drop_index(
            "ix_support_tickets_external_system_reference",
            table_name="support_tickets",
        )
    if "external_system" in _columns("support_tickets"):
        op.drop_column("support_tickets", "external_system")
    _rename_column("support_tickets", "external_reference", "erpnext_id")
    if "ix_support_tickets_erpnext_id" not in _indexes("support_tickets"):
        op.create_index(
            "ix_support_tickets_erpnext_id",
            "support_tickets",
            ["erpnext_id"],
        )

    for table, constraint in (
        ("project_tasks", "uq_project_tasks_external_system_reference"),
        ("projects", "uq_projects_external_system_reference"),
    ):
        _drop_unique_constraint(table, constraint)
        if "external_system" in _columns(table):
            op.drop_column(table, "external_system")
        _rename_column(table, "external_reference", "erpnext_id")
        old_index = f"ix_{table}_erpnext_id"
        if old_index not in _indexes(table):
            op.create_index(old_index, table, ["erpnext_id"], unique=True)

    _replace_unique_constraint(
        "service_teams",
        "uq_service_teams_workforce_system_reference",
        "uq_service_teams_erp_department",
        ["workforce_department_reference"],
    )
    if "workforce_system" in _columns("service_teams"):
        op.drop_column("service_teams", "workforce_system")
    _rename_column("service_teams", "workforce_department_reference", "erp_department")
    for table in ("availability_blocks", "shifts"):
        _replace_unique_constraint(
            table,
            f"uq_{table}_workforce_system_reference",
            f"uq_{table}_erp_id",
            ["workforce_record_reference"],
        )
        if "workforce_system" in _columns(table):
            op.drop_column(table, "workforce_system")
        _rename_column(table, "workforce_record_reference", "erp_id")
        if f"ix_{table}_erp_id" not in _indexes(table):
            op.create_index(f"ix_{table}_erp_id", table, ["erp_id"])
    _replace_unique_constraint(
        "technician_profiles",
        "uq_technician_profiles_workforce_system_reference",
        "uq_technician_profiles_erp_employee_id",
        ["workforce_employee_reference"],
    )
    if "workforce_system" in _columns("technician_profiles"):
        op.drop_column("technician_profiles", "workforce_system")
    _rename_column(
        "technician_profiles", "workforce_employee_reference", "erp_employee_id"
    )
    if "ix_technician_profiles_erp_employee_id" not in _indexes("technician_profiles"):
        op.create_index(
            "ix_technician_profiles_erp_employee_id",
            "technician_profiles",
            ["erp_employee_id"],
        )

    if "expense_system" in _columns("field_expense_requests"):
        op.drop_column("field_expense_requests", "expense_system")
    _rename_column("field_expense_requests", "expense_claim_status", "erp_claim_status")
    _rename_column("field_expense_requests", "expense_claim_number", "erp_claim_number")
    _rename_column(
        "field_expense_requests", "expense_claim_reference", "erp_expense_claim_id"
    )

    if "support_system" in _columns("field_material_requests"):
        op.drop_column("field_material_requests", "support_system")
    _rename_column("field_material_requests", "support_status", "erp_material_status")
    _rename_column(
        "field_material_requests", "support_reference", "erp_material_request_id"
    )
    _rename_index(
        "field_material_requests",
        "ix_field_material_requests_support_reference",
        "ix_field_material_requests_erp_material_request_id",
        ["erp_material_request_id"],
    )
