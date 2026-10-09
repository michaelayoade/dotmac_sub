"""Immutable contracts exposed by modules to the Automation Center.

These declarations contain no executable callbacks. A module opts into
automation by publishing one closed set of triggers, condition fields and
actions from its source-of-truth domain declaration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutomationValueType(StrEnum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    date = "date"
    datetime = "datetime"
    uuid = "uuid"
    enum = "enum"


class AutomationLookupKey(StrEnum):
    """Canonical system-backed sources available to condition pickers."""

    customer = "customer"
    service_team = "service_team"
    project = "project"
    project_task = "project_task"
    work_order = "work_order"
    material_request = "material_request"
    pipeline = "pipeline"
    lead = "lead"
    quote = "quote"
    sales_order = "sales_order"
    system_user = "system_user"
    cx_handoff = "cx_handoff"
    ticket_type = "ticket_type"
    project_type = "project_type"
    project_name = "project_name"
    region = "region"
    lead_source = "lead_source"
    currency = "currency"
    warehouse = "warehouse"
    support_system = "support_system"
    support_status = "support_status"


class AutomationCatalogState(StrEnum):
    available = "available"
    unavailable = "unavailable"
    managed_elsewhere = "managed_elsewhere"
    retired = "retired"


class AutomationMechanism(StrEnum):
    """Authoring mechanisms exposed by the Automation Center."""

    rule = "rule"
    client_script = "client_script"
    server_script = "server_script"


class AutomationScriptLanguage(StrEnum):
    """Languages admitted by the native script control plane."""

    javascript = "javascript"


@dataclass(frozen=True, slots=True)
class AutomationScriptTargetCapability:
    """Closed target contract for client/server scripts.

    This is deliberately separate from rule triggers/actions. A target may be
    scriptable without being writable, and script execution must still go
    through the target owner's typed API.
    """

    key: str
    label: str
    entity_type: str
    client_events: tuple[str, ...] = ()
    server_events: tuple[str, ...] = ()
    read_permission: str = ""
    write_permission: str | None = None
    tenant_id_field: str = "tenant_id"
    entity_id_field: str = "id"


class AutomationOperator(StrEnum):
    equals = "equals"
    not_equals = "not_equals"
    in_values = "in"
    not_in_values = "not_in"
    greater_than = "greater_than"
    greater_than_or_equal = "greater_than_or_equal"
    less_than = "less_than"
    less_than_or_equal = "less_than_or_equal"
    contains = "contains"
    is_empty = "is_empty"
    is_not_empty = "is_not_empty"


@dataclass(frozen=True, slots=True)
class AutomationConditionField:
    key: str
    label: str
    value_type: AutomationValueType
    operators: tuple[AutomationOperator, ...]
    enum_values: tuple[str, ...] = ()
    sensitive: bool = False
    lookup_key: AutomationLookupKey | None = None

    def __post_init__(self) -> None:
        """Infer lookup metadata for canonical system references.

        Domain declarations remain concise while the contract still exposes a
        typed, explicit lookup capability to the authoring surface.
        """

        if self.lookup_key is not None:
            return
        lookup = {
            "customer_id": AutomationLookupKey.customer,
            "service_team_id": AutomationLookupKey.service_team,
            "project_id": AutomationLookupKey.project,
            "project_task_id": AutomationLookupKey.project_task,
            "work_order_mirror_id": AutomationLookupKey.work_order,
            "material_request_id": AutomationLookupKey.material_request,
            "pipeline_id": AutomationLookupKey.pipeline,
            "lead_id": AutomationLookupKey.lead,
            "quote_id": AutomationLookupKey.quote,
            "sales_order_id": AutomationLookupKey.sales_order,
            "reviewer_system_user_id": AutomationLookupKey.system_user,
            "cx_handoff_id": AutomationLookupKey.cx_handoff,
            "ticket_type": AutomationLookupKey.ticket_type,
            "project_type": AutomationLookupKey.project_type,
            "project_name": AutomationLookupKey.project_name,
            "region": AutomationLookupKey.region,
            "lead_source": AutomationLookupKey.lead_source,
            "currency": AutomationLookupKey.currency,
            "source_warehouse_code": AutomationLookupKey.warehouse,
            "support_system": AutomationLookupKey.support_system,
            "support_status": AutomationLookupKey.support_status,
        }.get(self.key)
        if lookup is not None:
            object.__setattr__(self, "lookup_key", lookup)


@dataclass(frozen=True, slots=True)
class AutomationTriggerCapability:
    key: str
    label: str
    event_type: str
    event_schema_version: int
    entity_type: str
    tenant_id_field: str
    entity_id_field: str
    fields: tuple[AutomationConditionField, ...]
    author_permission: str
    #: A trigger may be admitted for draft authoring before its durable event
    #: producer and identity contract are ready for runtime delivery.
    runtime_enabled: bool = False
    #: Older condition contracts that remain safe against this event payload.
    compatible_event_schema_versions: tuple[int, ...] = ()
    #: Whether the trigger can be evaluated by the shared scheduled-rule runner.
    scheduled: bool = False
    #: Stable key for the module-owned record provider used by scheduled runs.
    schedule_adapter_key: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationActionInput:
    key: str
    label: str
    value_type: AutomationValueType
    required: bool = True
    enum_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationActionCapability:
    key: str
    label: str
    entity_type: str
    command_owner: str
    command_name: str
    input_schema_version: int
    inputs: tuple[AutomationActionInput, ...]
    author_permission: str
    runtime_scope: str
    idempotency: str
    #: Declarative mutually-exclusive consequence family. Publication rejects
    #: overlapping rules with the same trigger and conflict scope.
    conflict_scope: str | None = None
    #: Retired actions may remain executable only so immutable published rule
    #: versions can complete against their historical action key. They are not
    #: offered to rule builders or accepted in new/updated definitions.
    authoring_enabled: bool = True
    #: An action may be admitted for draft authoring before its runtime
    #: event-to-command adapter is ready. Publication must reject it until this
    #: flag is enabled in a later reviewed slice.
    runtime_enabled: bool = False
    #: Optional target types for shared actions. An empty tuple preserves the
    #: original same-entity contract; ``("*",)`` admits the action for any
    #: trigger target after the action validates its own recipient/target.
    target_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyAutomationSurface:
    key: str
    label: str
    owner_service: str
    management_path: str | None
    conflict_scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AutomationCatalogItem:
    """One owner-declared business automation shown in the central catalogue."""

    key: str
    label: str
    group: str
    state: AutomationCatalogState
    explanation: str
    management_path: str | None = None
    trigger_keys: tuple[str, ...] = ()
    action_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationDomainCapabilities:
    """Automation contract declared by one canonical SOT domain."""

    target_types: tuple[str, ...] = ()
    triggers: tuple[AutomationTriggerCapability, ...] = ()
    actions: tuple[AutomationActionCapability, ...] = ()
    legacy_surfaces: tuple[LegacyAutomationSurface, ...] = ()
    catalog_items: tuple[AutomationCatalogItem, ...] = ()
    script_targets: tuple[AutomationScriptTargetCapability, ...] = ()
    manifest_version: int = 1


@dataclass(frozen=True, slots=True)
class AutomationModuleManifest:
    """Resolved module record presented to authoring and runtime adapters."""

    module_key: str
    label: str
    owner_domain: str
    registered: bool
    target_types: tuple[str, ...]
    triggers: tuple[AutomationTriggerCapability, ...]
    actions: tuple[AutomationActionCapability, ...]
    legacy_surfaces: tuple[LegacyAutomationSurface, ...]
    catalog_items: tuple[AutomationCatalogItem, ...]
    script_targets: tuple[AutomationScriptTargetCapability, ...]
    manifest_version: int | None
