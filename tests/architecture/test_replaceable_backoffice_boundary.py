from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

DOMAIN_PATHS = (
    "app/services/field/material_requests.py",
    "app/services/field/expense_requests.py",
    "app/services/vendor_purchase_invoices.py",
    "app/services/vendor_portal_operations.py",
    "app/api/field/inventory.py",
    "app/services/ncc_regulatory_pack.py",
)

MODEL_PATHS = (
    "app/models/dispatch.py",
    "app/models/field_expense.py",
    "app/models/field_material.py",
    "app/models/organization.py",
    "app/models/project.py",
    "app/models/service_team.py",
    "app/models/support.py",
    "app/models/vendor_routes.py",
)


def _imports(path: Path) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_sub_domain_owners_do_not_import_provider_adapter() -> None:
    for relative_path in DOMAIN_PATHS:
        imports = _imports(ROOT / relative_path)
        assert not any(
            name == "app.services.dotmac_erp"
            or name.startswith("app.services.dotmac_erp.")
            for name in imports
        ), relative_path
        assert "app.models.field_erp_sync" not in imports, relative_path


def test_core_models_expose_provider_neutral_references() -> None:
    for relative_path in MODEL_PATHS:
        tree = ast.parse((ROOT / relative_path).read_text())
        assigned_names = {
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert not {name for name in assigned_names if name.startswith("erp")}, (
            relative_path
        )


def test_external_reference_uniqueness_is_scoped_by_source_system() -> None:
    expected_constraints = {
        "app/models/vendor_routes.py": "uq_vendors_supplier_system_reference",
        "app/models/organization.py": ("uq_organizations_backoffice_system_reference"),
        "app/models/dispatch.py": ("uq_technician_profiles_workforce_system_reference"),
        "app/models/service_team.py": ("uq_service_teams_workforce_system_reference"),
        "app/models/project.py": "uq_projects_external_system_reference",
        "app/models/support.py": ("ix_support_tickets_external_system_reference"),
    }
    for relative_path, constraint in expected_constraints.items():
        assert constraint in (ROOT / relative_path).read_text(), relative_path


def test_migration_backfills_provenance_and_scopes_legacy_references() -> None:
    migration = (
        ROOT / "alembic/versions/373_replaceable_backoffice_boundary.py"
    ).read_text()

    assert 'revision = "373_replaceable_backoffice"' in migration
    assert 'down_revision = "372_vendor_payment_projection"' in migration
    assert "SET supplier_system = 'dotmac_erp'" in migration
    assert "uq_vendors_supplier_system_reference" in migration
    assert "uq_organizations_backoffice_system_reference" in migration
    assert "SET external_system = 'erpnext'" in migration


def test_checked_in_boundary_declares_replaceable_independent_products() -> None:
    boundary = " ".join(
        (ROOT / "docs/BACKOFFICE_INTEGRATION_BOUNDARY.md").read_text().split()
    )

    assert "not an enterprise control plane" in boundary
    assert "may be replaced by Zoho" in boundary
    assert "There is no enterprise tax-ID registry" in boundary
    assert "There are no cross-system database queries" in boundary


def test_provider_specific_imports_are_confined_to_local_boundary() -> None:
    boundary_imports = _imports(ROOT / "app/services/backoffice.py")

    assert any(name.startswith("app.services.dotmac_erp") for name in boundary_imports)
