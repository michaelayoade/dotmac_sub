"""Architecture guards for the approved integration-platform migration."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services import sot_relationships

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_ENV = PROJECT_ROOT / "alembic/env.py"
ACTIVE_MIGRATIONS = PROJECT_ROOT / "alembic/versions"
DESIGN = PROJECT_ROOT / "docs/designs/INTEGRATION_PLATFORM_SOT.md"
SOT_MAP = PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md"
CONNECTOR_ROOT = PROJECT_ROOT / "app/services/integrations/connectors"

RETIRED_PATHS = (
    "app/api/webhooks.py",
    "app/models/crm_webhook_delivery.py",
    "app/models/integration_hook.py",
    "app/models/webhook.py",
    "app/services/crm_native_sync.py",
    "app/services/crm_webhook_deliveries.py",
    "app/services/flutterwave.py",
    "app/services/integration_hooks.py",
    "app/services/integrations/connectors/whatsapp.py",
    "app/services/paystack.py",
    "app/services/webhook.py",
    "app/services/webhook_deliveries.py",
    "app/tasks/crm_native_sync.py",
    "app/tasks/webhooks.py",
    "templates/admin/integrations/register.html",
    "templates/admin/integrations/register_configure.html",
)

CURRENT_INTEGRATION_SERVICES = (
    "integration.registry",
    "integration.installations",
    "integration.runtime",
    "integration.delivery",
    "integration.inbox",
    "integration.jobs",
    "integration.sync",
    "integration.vendor_purchase_invoice_erp_projection",
    "integration.erp_material_support",
)

# This is shrink-only compatibility debt. New connector modules must consume
# typed ports and runtime context rather than Sub's database or ORM models.
LEGACY_DIRECT_PERSISTENCE_IMPORTS: dict[str, set[str]] = {}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _direct_persistence_imports(path: Path) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.db" or alias.name.startswith("app.models"):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.db" or module.startswith("app.models"):
                imports.add(module)
    return imports


def _alembic_model_module_imports() -> set[str]:
    tree = ast.parse(_read(ALEMBIC_ENV), filename=str(ALEMBIC_ENV))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.models"
        for alias in node.names
    }


def test_integration_platform_design_records_the_approved_invariants() -> None:
    design = _read(DESIGN)
    normalized_design = " ".join(design.split())

    required_contracts = (
        "Status: implemented source-of-truth architecture, 2026-07-20.",
        "A connector is a transport and translation implementation",
        "Connector code cannot write Sub domain tables.",
        "the admin UI does not install arbitrary executable code",
        "no duplicate external send and no duplicate Sub domain write",
        "Service names enter the executable SOT registry only when",
    )
    for contract in required_contracts:
        assert contract in normalized_design


def test_alembic_registry_does_not_import_retired_integration_models() -> None:
    retired_model_modules = {
        Path(path).stem for path in RETIRED_PATHS if path.startswith("app/models/")
    }

    assert _alembic_model_module_imports().isdisjoint(retired_model_modules)


def test_active_migrations_do_not_extend_retired_integration_types() -> None:
    offenders = [
        path.name
        for path in sorted(ACTIVE_MIGRATIONS.glob("*.py"))
        if "ALTER TYPE webhookeventtype" in _read(path)
    ]

    assert not offenders, (
        "Active migrations must not depend on the retired webhook enum; "
        f"revision 377 owns cleanup of deployed legacy objects: {offenders}"
    )


def test_integration_sot_names_the_live_cutover_owners() -> None:
    assert (
        sot_relationships.service_names_for_domain("integration_control_plane")
        == CURRENT_INTEGRATION_SERVICES
    )
    assert sot_relationships.dependencies_for("integration.jobs") == (
        "integration.registry",
    )
    assert sot_relationships.dependencies_for("integration.installations") == (
        "integration.registry",
        "secrets.reference_store",
    )
    assert sot_relationships.dependencies_for("integration.sync") == (
        "integration.jobs",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for("integration.runtime") == (
        "integration.registry",
        "integration.installations",
        "secrets.reference_store",
    )
    assert sot_relationships.dependencies_for("integration.delivery") == (
        "events.store",
        "integration.installations",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for("integration.inbox") == (
        "integration.installations",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for(
        "integration.vendor_purchase_invoice_erp_projection"
    ) == ("operations.vendor_purchase_invoices",)
    assert sot_relationships.dependencies_for("integration.erp_material_support") == (
        "operations.material_dependencies",
    )

    registry = sot_relationships.services_for_domain("integration_control_plane")[0]
    assert registry.notes is not None
    assert "INTEGRATION_PLATFORM_SOT.md" in registry.notes

    premature_target_owners = {
        "integration.links",
        "integration.health",
    }
    assert premature_target_owners.isdisjoint(
        sot_relationships.service_names_for_domain("integration_control_plane")
    )


def test_narrative_sot_names_each_integration_authority_migration() -> None:
    relationships = _read(SOT_MAP)

    assert "docs/designs/INTEGRATION_PLATFORM_SOT.md" in relationships
    for concern in (
        "Connector catalogue",
        "Installation configuration",
        "Sync dispatch",
        "CRM",
        "Outbound webhooks and hooks",
        "WhatsApp messaging",
        "ERP",
        "Payments",
    ):
        assert f"| {concern} |" in relationships

    assert "Authority cutover is complete" in relationships
    assert "Connectors translate\nbounded, typed contracts" in relationships


def test_retired_integration_paths_cannot_return() -> None:
    present = [path for path in RETIRED_PATHS if (PROJECT_ROOT / path).exists()]
    assert not present, f"Retired integration paths returned: {present}"

    application_text = "\n".join(
        _read(path)
        for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts")
        for path in sorted(root.rglob("*.py"))
    )
    for retired_import in (
        "app.services.paystack",
        "app.services.flutterwave",
        "app.services.integration_hooks",
        "app.services.webhook_deliveries",
    ):
        assert retired_import not in application_text
    for retired_environment_read in (
        'os.getenv("CRM_BASE_URL"',
        'os.getenv("CRM_USERNAME"',
        'os.getenv("CRM_PASSWORD"',
        'os.getenv("PAYSTACK_SECRET_KEY"',
        'os.getenv("FLUTTERWAVE_SECRET_KEY"',
        'env_var="WHATSAPP_PROVIDER"',
        'env_var="WHATSAPP_API_KEY"',
        'env_var="WHATSAPP_API_SECRET"',
        'env_var="WHATSAPP_API_TIMEOUT_SECONDS"',
        'env_var="META_WEBHOOK_VERIFY_TOKEN"',
    ):
        assert retired_environment_read not in application_text


def test_crm_transport_is_constructed_only_by_the_registered_connector() -> None:
    allowed_constructor = (
        PROJECT_ROOT / "app/services/integrations/connectors/dotmac_crm.py"
    )
    violations: list[str] = []
    retired_factory_violations: list[str] = []
    for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            source = _read(path)
            if "CRMClient(" in source and path != allowed_constructor:
                violations.append(str(path.relative_to(PROJECT_ROOT)))
            if "get_crm_client" in source:
                retired_factory_violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert not violations, (
        "CRMClient is a connector-private transport; callers must use a typed "
        f"dotmac.crm capability binding: {violations}"
    )
    assert not retired_factory_violations, (
        f"The pre-platform CRM client factory is retired: {retired_factory_violations}"
    )


def test_arbitrary_integration_registration_surface_stays_retired() -> None:
    routes = _read(PROJECT_ROOT / "app/web/admin/integrations.py")
    service = _read(PROJECT_ROOT / "app/services/web_integrations.py")

    assert '"/register"' not in routes
    for retired_symbol in (
        "create_registered_integration",
        "registered_integration_config_state",
        "update_registered_integration_config",
    ):
        assert retired_symbol not in service


def test_new_connector_modules_cannot_import_sub_persistence() -> None:
    actual_legacy: dict[str, set[str]] = {}
    violations: dict[str, list[str]] = {}

    for path in sorted(CONNECTOR_ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        imports = _direct_persistence_imports(path)
        if path.name in LEGACY_DIRECT_PERSISTENCE_IMPORTS:
            actual_legacy[path.name] = imports
        elif imports:
            violations[path.name] = sorted(imports)

    assert actual_legacy == LEGACY_DIRECT_PERSISTENCE_IMPORTS, (
        "The shrink-only connector persistence baseline changed. Remove a "
        "resolved import from LEGACY_DIRECT_PERSISTENCE_IMPORTS, or perform an "
        "explicit ownership review before adding debt."
    )
    assert not violations, (
        "Connector modules must consume typed integration/domain ports and may "
        f"not import Sub persistence directly: {violations}"
    )
