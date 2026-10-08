"""Canonical SOT declarations for the geospatial domain."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationDomainCapabilities,
)
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="geospatial",
    setting_domains=(
        "geocoding",
        "gis",
    ),
    services=(
        SOTService(
            name="gis.geocoding",
            module="app.services.geocoding",
            owns=(
                "address and coordinate resolution",
                "geocode lookup and result caching",
            ),
        ),
        SOTService(
            name="gis.spatial_sync",
            module="app.services.gis_sync",
            owns=(
                "GIS/spatial data synchronization",
                "spatial feature import and projection",
            ),
        ),
        SOTService(
            name="gis.customer_regions",
            module="app.services.customer_regions",
            owns=(
                "customer region configuration",
                "radius-based customer region assignment",
                "overlapping customer region tie-break resolution",
            ),
            depends_on=(
                "auth.permission_gate",
                "customer.accounts",
                "network.identity",
                "gis.spatial_sync",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer region configuration",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=("authorized region command",),
                        canonical_writer="gis.customer_regions",
                    ),
                    ConcernContract(
                        name="radius-based customer region assignment",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "configured customer regions",
                            "customer service location",
                            "network session identity",
                        ),
                    ),
                    ConcernContract(
                        name="overlapping customer region tie-break resolution",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "configured customer regions",
                            "customer service location",
                            "network session identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized region command",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed SaveCustomerRegionCommand or "
                            "DisableCustomerRegionCommand authorized for gis:area:write"
                        ),
                    ),
                    AuthorityInput(
                        name="configured customer regions",
                        owner="gis.customer_regions",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="customer_regions rows written only by this owner",
                    ),
                    AuthorityInput(
                        name="customer service location",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the stable primary geocoded subscriber address and POP "
                            "assignment"
                        ),
                    ),
                    AuthorityInput(
                        name="network session identity",
                        owner="network.identity",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "subscription provisioning NAS and canonical active-session "
                            "NAS observations"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "one public customer-region command owns one atomic transaction"
                    ),
                    locking=(
                        "updates and disables lock the selected customer_regions row; "
                        "database uniqueness arbitrates concurrent names"
                    ),
                    idempotency=(
                        "disable replays preserve inactive state; command identity and "
                        "change evidence are retained in the staged event"
                    ),
                    retries=(
                        "retry the complete command only after transient database "
                        "failure with the same business intent"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes("gis.customer_regions"),
                        "gis.customer_regions.invalid_region",
                        "gis.customer_regions.region_not_found",
                        "gis.customer_regions.duplicate_name",
                    ),
                    mapping_owner="admin customer-regions web adapter",
                    fail_closed_on=(
                        "invalid geometry or matching policy",
                        "missing region",
                        "duplicate region name",
                    ),
                ),
                events=EventContract(
                    event_types=("customer_region.changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "additive payload changes only within schema version 1"
                    ),
                    replay=("durable event-store delivery replays by event identity"),
                ),
                projections=(
                    ProjectionContract(
                        name="customer region assignment",
                        input_names=(
                            "configured customer regions",
                            "customer service location",
                            "network session identity",
                        ),
                        writer="gis.customer_regions",
                        freshness="resolved from authoritative inputs on each query",
                        stale_behavior="omit assignment when required location is absent",
                        drift_signal=(
                            "SQL cohort and deterministic resolver select different "
                            "winning region identities"
                        ),
                        rebuild_operation=(
                            "rerun customer_region_assignment_cte or resolve_region"
                        ),
                        repair_owner="gis.customer_regions",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="gis.customer_regions",
                    old_owner="app.web.admin.customer_regions",
                    verification=(
                        "customer-region behavior and architecture tests exercise the "
                        "typed owner and canonical resolver"
                    ),
                    cutover_gate=(
                        "admin writes call only the typed owner command boundary"
                    ),
                    fallback_retirement=(
                        "web adapters contain no region writes or transaction completion"
                    ),
                ),
                steward="geospatial operations",
                design_refs=(
                    "docs/designs/CUSTOMER_REGION_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_regions.py",
                    "tests/architecture/test_sot_manifest_contracts.py",
                    "tests/architecture/test_owner_command_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.api.geocoding",
        "app.api.gis",
        "app.tasks.gis",
        "app.services.web_system_geocode_tool",
        "app.services.web_gis",
    ),
    rule="Address/coordinate resolution and spatial data synchronization "
    "resolve through these owners. API, web, and task callers request a "
    "geocode or a sync outcome; they do not embed their own geocode "
    "lookups or spatial write logic.",
    automation=AutomationDomainCapabilities(
        catalog_items=(
            AutomationCatalogItem(
                key="reports.gis_synchronization",
                label="GIS synchronization",
                group="Reports and exports",
                state=AutomationCatalogState.unavailable,
                explanation="GIS synchronization remains with the geospatial owner and has no Automation Center trigger or action.",
            ),
            AutomationCatalogItem(
                key="reports.batch_geocoding",
                label="Batch geocoding",
                group="Reports and exports",
                state=AutomationCatalogState.unavailable,
                explanation="Batch geocoding is an authorized user-started job; Center rules cannot start it yet.",
            ),
        ),
    ),
)
