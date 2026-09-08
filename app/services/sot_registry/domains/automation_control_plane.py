"""Canonical SOT declarations for the automation control plane."""

from __future__ import annotations

from app.services.automation_contracts import AutomationDomainCapabilities
from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="automation_control_plane",
    services=(
        SOTService(
            name="automation.capability_registry",
            module="app.services.automation_capabilities",
            owns=("automation capability declarations and compatibility validation",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "automation capability declarations and compatibility "
                            "validation"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=("SOT domain automation declarations",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="SOT domain automation declarations",
                        owner="automation.capability_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "immutable AutomationDomainCapabilities declared by each "
                            "canonical DomainSOT"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Pure in-process registry resolution; no database writes.",
                    locking="Not applicable because declarations are immutable code.",
                    idempotency="The same checked-in declarations resolve identically.",
                    retries="Retry the pure query after correcting an invalid manifest.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation_capability_undeclared",
                        "automation_capability_ambiguous",
                        "automation_capability_manifest_invalid",
                    ),
                    mapping_owner="automation authoring adapters",
                    fail_closed_on=(
                        "unknown module, trigger, action, target, or contract version",
                        "duplicate capability ownership",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="automation.capability_registry",
                    verification="automation capability registry architecture tests",
                    cutover_gate=(
                        "only registered capabilities can be stored or executed by the "
                        "Automation Center"
                    ),
                    fallback_retirement="No runtime-editable capability registry exists.",
                ),
                steward="platform automation",
                design_refs=(
                    "docs/designs/AUTOMATION_CENTER_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/architecture/test_automation_capability_registry.py",
                ),
            ),
        ),
    ),
    entrypoints=("app.services.automation_capabilities",),
    rule=(
        "Every SOT domain is visible to the Automation Center, but only a closed, "
        "versioned domain declaration may expose executable triggers or actions."
    ),
    automation=AutomationDomainCapabilities(target_types=("automation.rule",)),
)
