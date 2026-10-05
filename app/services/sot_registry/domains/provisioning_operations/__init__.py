"""Assemble the canonical provisioning_operations SOT domain from capability shards."""

from __future__ import annotations

from app.services.automation_contracts import (
    AutomationActionCapability,
    AutomationActionInput,
    AutomationCatalogItem,
    AutomationCatalogState,
    AutomationConditionField,
    AutomationDomainCapabilities,
    AutomationOperator,
    AutomationScriptTargetCapability,
    AutomationTriggerCapability,
    AutomationValueType,
)
from app.services.sot_registry.domains.provisioning_operations.core import (
    SERVICES as CORE_SERVICES,
)
from app.services.sot_registry.domains.provisioning_operations.vendor_delivery import (
    SERVICES as VENDOR_DELIVERY_SERVICES,
)
from app.services.sot_registry.domains.provisioning_operations.vendor_identity import (
    SERVICES as VENDOR_IDENTITY_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

_PROJECT_STATUS_VALUES = (
    "open",
    "planned",
    "active",
    "on_hold",
    "completed",
    "canceled",
)
_WORK_ORDER_STATUS_VALUES = (
    "draft",
    "scheduled",
    "dispatched",
    "in_progress",
    "paused",
    "completed",
    "canceled",
)
_VENDOR_STATUS_VALUES = ("in_progress", "completed")


def _enum_field(
    key: str,
    label: str,
    values: tuple[str, ...],
) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.enum,
        operators=(
            AutomationOperator.equals,
            AutomationOperator.not_equals,
            AutomationOperator.in_values,
            AutomationOperator.not_in_values,
        ),
        enum_values=values,
    )


_PROJECT_STATUS_FIELD = _enum_field("status", "Project status", _PROJECT_STATUS_VALUES)
_PROJECT_FROM_STATUS_FIELD = _enum_field(
    "from_status", "Previous project status", _PROJECT_STATUS_VALUES
)
_PROJECT_TO_STATUS_FIELD = _enum_field(
    "to_status", "New project status", _PROJECT_STATUS_VALUES
)
_WORK_ORDER_STATUS_FIELD = _enum_field(
    "status", "Work-order status", _WORK_ORDER_STATUS_VALUES
)
_VENDOR_FROM_STATUS_FIELD = _enum_field(
    "from_status", "Previous vendor-project status", _VENDOR_STATUS_VALUES
)
_VENDOR_TO_STATUS_FIELD = _enum_field(
    "to_status", "New vendor-project status", _VENDOR_STATUS_VALUES
)
_PROJECT_TYPE_FIELD = AutomationConditionField(
    key="project_type",
    label="Project type",
    value_type=AutomationValueType.string,
    operators=(
        AutomationOperator.equals,
        AutomationOperator.not_equals,
        AutomationOperator.contains,
    ),
)


def _text_field(key: str, label: str) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.string,
        operators=(
            AutomationOperator.equals,
            AutomationOperator.not_equals,
            AutomationOperator.contains,
            AutomationOperator.is_empty,
            AutomationOperator.is_not_empty,
        ),
    )


def _uuid_field(key: str, label: str) -> AutomationConditionField:
    return AutomationConditionField(
        key=key,
        label=label,
        value_type=AutomationValueType.uuid,
        operators=(AutomationOperator.equals, AutomationOperator.not_equals),
    )


DOMAIN = DomainSOT(
    domain="provisioning_operations",
    setting_domains=(
        "provisioning",
        "projects",
        "inventory",
        "field",
    ),
    services=(
        *CORE_SERVICES,
        *VENDOR_IDENTITY_SERVICES,
        *VENDOR_DELIVERY_SERVICES,
    ),
    entrypoints=(
        "app.services.events.handlers.provisioning",
        "app.tasks.ont_provisioning",
        "app.web.admin.provisioning",
        "app.web.admin.projects",
        "app.web.vendor_portal",
        "app.api.vendor_portal",
        "app.api.projects",
        "app.api.field.*",
        "app.services.web_projects",
        "app.services.web_dispatch_work_orders",
        "app.services.work_order_commands",
        "field_mobile",
    ),
    rule="Provisioning callers resolve customer/network context through the "
    "shared context layer before executing workflow steps. Native project "
    "mutation adapters delegate to Projects.update for lifecycle consequences. "
    "Field clients consume completion_requirements from authenticated job "
    "detail and leave completion eligibility to the field transition service. "
    "Dispatch adapters delegate native work-order and assignment writes to "
    "operations.work_order_commands.",
    automation=AutomationDomainCapabilities(
        target_types=(
            "operations.project",
            "operations.work_order",
            "operations.material_request",
            "operations.vendor",
        ),
        triggers=(
            AutomationTriggerCapability(
                key="operations.project.created",
                label="Project created",
                event_type="project.created",
                event_schema_version=1,
                entity_type="operations.project",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(
                    _PROJECT_TYPE_FIELD,
                    _uuid_field("sales_order_id", "Sales order"),
                    _uuid_field("quote_id", "Quote"),
                    _uuid_field("subscriber_id", "Customer account"),
                ),
                author_permission="project:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.project.updated",
                label="Project updated",
                event_type="project.updated",
                event_schema_version=1,
                entity_type="operations.project",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(
                    _PROJECT_STATUS_FIELD,
                    _text_field("project_name", "Project name"),
                ),
                author_permission="project:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.project.completed",
                label="Project completed",
                event_type="project.completed",
                event_schema_version=1,
                entity_type="operations.project",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(_PROJECT_FROM_STATUS_FIELD, _PROJECT_TO_STATUS_FIELD),
                author_permission="project:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.project.canceled",
                label="Project canceled",
                event_type="project.canceled",
                event_schema_version=1,
                entity_type="operations.project",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(_PROJECT_FROM_STATUS_FIELD, _PROJECT_TO_STATUS_FIELD),
                author_permission="project:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.work_order.created",
                label="Work order created",
                event_type="work_order.created",
                event_schema_version=1,
                entity_type="operations.work_order",
                tenant_id_field="tenant_id",
                entity_id_field="work_order_id",
                fields=(
                    _WORK_ORDER_STATUS_FIELD,
                    _uuid_field("project_id", "Project"),
                    _uuid_field("project_task_id", "Project task"),
                ),
                author_permission="operations:dispatch:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.work_order.updated",
                label="Work order updated",
                event_type="work_order.updated",
                event_schema_version=1,
                entity_type="operations.work_order",
                tenant_id_field="tenant_id",
                entity_id_field="work_order_id",
                fields=(
                    _WORK_ORDER_STATUS_FIELD,
                    _uuid_field("project_id", "Project"),
                    _uuid_field("project_task_id", "Project task"),
                ),
                author_permission="operations:dispatch:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.material_request.cancellation_requested",
                label="Material request cancellation requested",
                event_type="field_material_request.cancellation_requested",
                event_schema_version=1,
                entity_type="operations.material_request",
                tenant_id_field="tenant_id",
                entity_id_field="material_request_id",
                fields=(
                    _text_field("reason", "Cancellation reason"),
                    _uuid_field("work_order_mirror_id", "Work order"),
                ),
                author_permission="operations:material_request:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.material_request.approved",
                label="Material request approved",
                event_type="field_material_request.approved",
                event_schema_version=1,
                entity_type="operations.material_request",
                tenant_id_field="tenant_id",
                entity_id_field="material_request_id",
                fields=(
                    _text_field("source_warehouse_code", "Source warehouse"),
                    _uuid_field("work_order_mirror_id", "Work order"),
                ),
                author_permission="operations:material_request:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.material_request.fulfilled",
                label="Material request fulfilled",
                event_type="field_material_request.fulfilled",
                event_schema_version=1,
                entity_type="operations.material_request",
                tenant_id_field="tenant_id",
                entity_id_field="material_request_id",
                fields=(
                    _text_field("support_system", "Support system"),
                    _text_field("support_status", "Support status"),
                    _uuid_field("work_order_mirror_id", "Work order"),
                ),
                author_permission="operations:material_request:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.vendor.project_completed",
                label="Vendor project completed",
                event_type="vendor_project.completed",
                event_schema_version=1,
                entity_type="operations.vendor",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(_VENDOR_FROM_STATUS_FIELD, _VENDOR_TO_STATUS_FIELD),
                author_permission="vendor:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.vendor.project_started",
                label="Vendor project started",
                event_type="vendor_project.started",
                event_schema_version=1,
                entity_type="operations.vendor",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(_VENDOR_FROM_STATUS_FIELD, _VENDOR_TO_STATUS_FIELD),
                author_permission="vendor:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.vendor.project_published",
                label="Vendor project published",
                event_type="vendor_project.published",
                event_schema_version=1,
                entity_type="operations.vendor",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(),
                author_permission="vendor:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.vendor.project_verified",
                label="Vendor project verified",
                event_type="vendor_project.verified",
                event_schema_version=1,
                entity_type="operations.vendor",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(),
                author_permission="vendor:read",
                runtime_enabled=True,
            ),
            AutomationTriggerCapability(
                key="operations.project.scheduled",
                label="Project scheduled evaluation",
                event_type="operations.project.scheduled",
                event_schema_version=1,
                entity_type="operations.project",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
                fields=(_PROJECT_STATUS_FIELD, _PROJECT_TYPE_FIELD),
                author_permission="project:read",
                runtime_enabled=True,
                scheduled=True,
                schedule_adapter_key="operations.project",
            ),
            AutomationTriggerCapability(
                key="operations.work_order.scheduled",
                label="Work order scheduled evaluation",
                event_type="operations.work_order.scheduled",
                event_schema_version=1,
                entity_type="operations.work_order",
                tenant_id_field="tenant_id",
                entity_id_field="work_order_id",
                fields=(_WORK_ORDER_STATUS_FIELD,),
                author_permission="operations:dispatch:read",
                runtime_enabled=True,
                scheduled=True,
                schedule_adapter_key="operations.work_order",
            ),
        ),
        actions=(
            AutomationActionCapability(
                key="operations.project.set_status",
                label="Set project status",
                entity_type="operations.project",
                command_owner="operations.project_lifecycle",
                command_name="update_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=_PROJECT_STATUS_VALUES,
                    ),
                ),
                author_permission="project:update",
                runtime_scope="one project",
                idempotency="tenant/project/status/version",
                runtime_enabled=True,
            ),
            AutomationActionCapability(
                key="operations.work_order.set_status",
                label="Set work-order status",
                entity_type="operations.work_order",
                command_owner="operations.work_order_commands",
                command_name="update_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=_WORK_ORDER_STATUS_VALUES,
                    ),
                ),
                author_permission="operations:dispatch:write",
                runtime_scope="one work order",
                idempotency="tenant/work-order/status/version",
                runtime_enabled=True,
            ),
            AutomationActionCapability(
                key="operations.material_request.enqueue_cancellation",
                label="Queue material-request cancellation",
                entity_type="operations.material_request",
                command_owner="operations.material_dependencies",
                command_name="consume_material_request_cancellation_requested",
                input_schema_version=1,
                inputs=(),
                author_permission="operations:material_request:write",
                runtime_scope="one material request",
                idempotency="tenant/material-request/cancellation-event",
                runtime_enabled=True,
            ),
            AutomationActionCapability(
                key="operations.vendor.set_status",
                label="Set vendor-project status",
                entity_type="operations.vendor",
                command_owner="operations.vendor_project_automation",
                command_name="transition_status",
                input_schema_version=1,
                inputs=(
                    AutomationActionInput(
                        key="status",
                        label="Status",
                        value_type=AutomationValueType.enum,
                        enum_values=("in_progress", "completed"),
                    ),
                ),
                author_permission="vendor:write",
                runtime_scope="one vendor project",
                idempotency="tenant/vendor-project/status/version",
                runtime_enabled=True,
            ),
        ),
        script_targets=(
            AutomationScriptTargetCapability(
                key="operations.project",
                label="Project",
                entity_type="operations.project",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "project.created",
                    "project.updated",
                    "project.completed",
                    "project.canceled",
                ),
                read_permission="project:read",
                write_permission="project:update",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
            ),
            AutomationScriptTargetCapability(
                key="operations.work_order",
                label="Work order",
                entity_type="operations.work_order",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=("work_order.created", "work_order.updated"),
                read_permission="operations:dispatch:read",
                write_permission="operations:dispatch:write",
                tenant_id_field="tenant_id",
                entity_id_field="work_order_id",
            ),
            AutomationScriptTargetCapability(
                key="operations.material_request",
                label="Material request",
                entity_type="operations.material_request",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "field_material_request.approved",
                    "field_material_request.cancellation_requested",
                    "field_material_request.fulfilled",
                ),
                read_permission="operations:material_request:read",
                write_permission="operations:material_request:write",
                tenant_id_field="tenant_id",
                entity_id_field="material_request_id",
            ),
            AutomationScriptTargetCapability(
                key="operations.vendor",
                label="Vendor project",
                entity_type="operations.vendor",
                client_events=("form.load", "field.change", "form.validate"),
                server_events=(
                    "vendor_project.published",
                    "vendor_project.completed",
                    "vendor_project.verified",
                ),
                read_permission="vendor:read",
                write_permission="vendor:write",
                tenant_id_field="tenant_id",
                entity_id_field="project_id",
            ),
        ),
        catalog_items=(
            AutomationCatalogItem(
                key="provisioning.subscription_activation",
                label="Subscription activation provisioning",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Activation provisions network access through linked lifecycle owners; the ordered steps are not yet registered as Center actions.",
            ),
            AutomationCatalogItem(
                key="provisioning.subscription_resume",
                label="Subscription-resume provisioning",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Resume and access restoration remain in the existing provisioning flow; no Center trigger or safe action is registered.",
            ),
            AutomationCatalogItem(
                key="provisioning.service_order_workflow",
                label="Service-order provisioning workflow",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="The service-order workflow includes dependent provisioning steps owned by current services and is not yet available as Center actions.",
            ),
            AutomationCatalogItem(
                key="provisioning.readiness_decision",
                label="Provisioning readiness decision",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Readiness checks stay in the provisioning owner; a rule cannot bypass their required checks.",
            ),
            AutomationCatalogItem(
                key="provisioning.stale_run_cleanup",
                label="Stale provisioning-run cleanup",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Cleanup is a protected recovery job and is not an editable customer-business action in the Center.",
            ),
            AutomationCatalogItem(
                key="provisioning.compensation_retry",
                label="Provisioning compensation retry",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Retrying compensation can affect service access and remains under its existing recovery safeguards.",
            ),
            AutomationCatalogItem(
                key="provisioning.bulk_activation_migration",
                label="Background bulk activation and migration",
                group="Provisioning",
                state=AutomationCatalogState.unavailable,
                explanation="Bulk activation is an operator-started process with bounded migration safeguards, not a reusable per-customer rule action.",
            ),
            AutomationCatalogItem(
                key="field.approved_material_request_export",
                label="Approved material request export",
                group="Field operations and ERP",
                state=AutomationCatalogState.unavailable,
                explanation="ERP delivery follows the approved field-request owner and integration outbox; a Center action is not registered.",
            ),
            AutomationCatalogItem(
                key="field.material_request_cancellation_export",
                label="Material-request cancellation export",
                group="Field operations and ERP",
                state=AutomationCatalogState.unavailable,
                explanation="Cancellation delivery is governed by the field request and ERP integration owners; no Center action is registered.",
            ),
        ),
    ),
)
