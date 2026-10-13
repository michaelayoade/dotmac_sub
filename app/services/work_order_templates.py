"""Explicit work classification and read-only field-scope recommendations.

These requirements are planning guidance, never completion/activation evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.services.field.work_order_kind import WorkOrderKind


class WorkOrderPurpose(StrEnum):
    REPAIR = "repair"
    MAINTENANCE = "maintenance"
    CUSTOMER_DEPLOYMENT = "customer_deployment"
    INFRASTRUCTURE_ROLLOUT = "infrastructure_rollout"
    UPGRADE = "upgrade"
    RELOCATION = "relocation"


class WorkOrderAccessTechnology(StrEnum):
    FIBER = "fiber"
    RADIO = "radio"
    UNKNOWN = "unknown"


class ClassificationSource(StrEnum):
    EXPLICIT_OPERATOR_SELECTION = "explicit_operator_selection"


class TemplateSource(StrEnum):
    CHECKED_IN_APPROVED_CATALOG = "checked_in_approved_catalog"


class TemplateRequirementUse(StrEnum):
    RECOMMENDATION_ONLY = "recommendation_only"


class RequirementGate(StrEnum):
    BEFORE_DISPATCH = "before_dispatch"
    BEFORE_FIELD_WORK = "before_field_work"
    BEFORE_HANDOVER = "before_handover"


class RequirementCode(StrEnum):
    SCOPE_AND_TARGET = "scope_and_target"
    SUPERVISOR_DISPATCH = "supervisor_dispatch"
    SAFETY_AND_PERMITS = "safety_and_permits"
    MATERIALS = "materials"
    DIAGNOSIS = "diagnosis"
    MAINTENANCE_PLAN = "maintenance_plan"
    CUSTOMER_SITE = "customer_site"
    ROLLOUT_DESIGN = "rollout_design"
    CHANGE_PLAN = "change_plan"
    RELOCATION_PLAN = "relocation_plan"
    TECHNOLOGY_SURVEY = "technology_survey"
    FIBER_ROUTE_AND_SPLICE = "fiber_route_and_splice"
    FIBER_TESTS = "fiber_tests"
    RADIO_SURVEY_AND_MOUNT = "radio_survey_and_mount"
    RADIO_TESTS = "radio_tests"
    AS_BUILT = "as_built"
    HANDOVER = "handover"


class TemplateErrorCode(StrEnum):
    INVALID_CLASSIFICATION = "operations.work_order_templates.invalid_classification"
    INCOMPATIBLE_KIND = "operations.work_order_templates.incompatible_kind"
    TECHNOLOGY_REQUIRED = "operations.work_order_templates.technology_required"


class WorkOrderTemplateError(Exception):
    """Safe, transport-neutral rejection of explicit classification."""

    def __init__(self, code: TemplateErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, kw_only=True)
class ExplicitWorkOrderClassification:
    purpose: WorkOrderPurpose
    access_technology: WorkOrderAccessTechnology
    source: ClassificationSource = ClassificationSource.EXPLICIT_OPERATOR_SELECTION

    def __post_init__(self) -> None:
        if not (
            isinstance(self.purpose, WorkOrderPurpose)
            and isinstance(self.access_technology, WorkOrderAccessTechnology)
            and isinstance(self.source, ClassificationSource)
        ):
            raise WorkOrderTemplateError(
                TemplateErrorCode.INVALID_CLASSIFICATION,
                "Select a governed purpose and access technology explicitly.",
            )


@dataclass(frozen=True, kw_only=True)
class WorkOrderTemplateQuery:
    kind: WorkOrderKind
    classification: ExplicitWorkOrderClassification

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WorkOrderKind) or not isinstance(
            self.classification, ExplicitWorkOrderClassification
        ):
            raise WorkOrderTemplateError(
                TemplateErrorCode.INVALID_CLASSIFICATION,
                "A native work-order kind and explicit classification are required.",
            )


@dataclass(frozen=True, kw_only=True)
class TemplateIdentity:
    kind: WorkOrderKind
    purpose: WorkOrderPurpose
    access_technology: WorkOrderAccessTechnology
    version: int


@dataclass(frozen=True, kw_only=True)
class RecommendedRequirement:
    code: RequirementCode
    gate: RequirementGate
    evidence: str


@dataclass(frozen=True, kw_only=True)
class WorkOrderTemplateRecommendation:
    identity: TemplateIdentity
    classification: ExplicitWorkOrderClassification
    requirements: tuple[RecommendedRequirement, ...]
    source: TemplateSource = TemplateSource.CHECKED_IN_APPROVED_CATALOG
    use: TemplateRequirementUse = TemplateRequirementUse.RECOMMENDATION_ONLY


def _requirement(
    code: RequirementCode, gate: RequirementGate, evidence: str
) -> RecommendedRequirement:
    return RecommendedRequirement(code=code, gate=gate, evidence=evidence)


_COMMON_REQUIREMENTS = (
    _requirement(
        RequirementCode.SCOPE_AND_TARGET,
        RequirementGate.BEFORE_DISPATCH,
        "Record the customer/service or shared infrastructure target, location, scope and accountable team.",
    ),
    _requirement(
        RequirementCode.SUPERVISOR_DISPATCH,
        RequirementGate.BEFORE_DISPATCH,
        "Supervisor confirms the scope and assigns an eligible technician or vendor.",
    ),
    _requirement(
        RequirementCode.SAFETY_AND_PERMITS,
        RequirementGate.BEFORE_FIELD_WORK,
        "Record site risk assessment, safe access, applicable permits/wayleaves and isolation approvals; explain any not-applicable item.",
    ),
    _requirement(
        RequirementCode.MATERIALS,
        RequirementGate.BEFORE_FIELD_WORK,
        "Confirm approved materials and tools; record what was used or returned and the equipment installed.",
    ),
)
_PURPOSE_REQUIREMENTS = {
    WorkOrderPurpose.REPAIR: _requirement(
        RequirementCode.DIAGNOSIS,
        RequirementGate.BEFORE_FIELD_WORK,
        "Capture fault diagnosis, affected service/infrastructure and proposed repair; retain before/after evidence.",
    ),
    WorkOrderPurpose.MAINTENANCE: _requirement(
        RequirementCode.MAINTENANCE_PLAN,
        RequirementGate.BEFORE_FIELD_WORK,
        "Confirm maintenance scope, service impact, approved window and recovery plan.",
    ),
    WorkOrderPurpose.CUSTOMER_DEPLOYMENT: _requirement(
        RequirementCode.CUSTOMER_SITE,
        RequirementGate.BEFORE_DISPATCH,
        "Confirm customer service location, consent/access, approved installation design and equipment scope.",
    ),
    WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT: _requirement(
        RequirementCode.ROLLOUT_DESIGN,
        RequirementGate.BEFORE_DISPATCH,
        "Link approved project/route/site design, rollout scope, shared assets and accountable network acceptance owner.",
    ),
    WorkOrderPurpose.UPGRADE: _requirement(
        RequirementCode.CHANGE_PLAN,
        RequirementGate.BEFORE_FIELD_WORK,
        "Record approved upgrade design, impact, maintenance window and rollback/recovery plan.",
    ),
    WorkOrderPurpose.RELOCATION: _requirement(
        RequirementCode.RELOCATION_PLAN,
        RequirementGate.BEFORE_FIELD_WORK,
        "Record existing and destination locations, approved move design, service continuity and restoration plan.",
    ),
}
_TECHNOLOGY_REQUIREMENTS = {
    WorkOrderAccessTechnology.UNKNOWN: (
        _requirement(
            RequirementCode.TECHNOLOGY_SURVEY,
            RequirementGate.BEFORE_FIELD_WORK,
            "Identify access technology and obtain the applicable approved engineering/test plan before technology-specific work.",
        ),
    ),
    WorkOrderAccessTechnology.FIBER: (
        _requirement(
            RequirementCode.FIBER_ROUTE_AND_SPLICE,
            RequirementGate.BEFORE_FIELD_WORK,
            "Confirm the approved fiber route, cable/core/port identities and applicable splice plan with the network team.",
        ),
        _requirement(
            RequirementCode.FIBER_TESTS,
            RequirementGate.BEFORE_HANDOVER,
            "Attach applicable optical power, continuity and OTDR results, equipment/calibration and approved engineering acceptance criteria; record defects and reviewer decision.",
        ),
    ),
    WorkOrderAccessTechnology.RADIO: (
        _requirement(
            RequirementCode.RADIO_SURVEY_AND_MOUNT,
            RequirementGate.BEFORE_FIELD_WORK,
            "Confirm line of sight, approved link/spectrum plan, mounting, grounding, power and applicable working-at-height controls.",
        ),
        _requirement(
            RequirementCode.RADIO_TESTS,
            RequirementGate.BEFORE_HANDOVER,
            "Attach applicable alignment, signal/link quality, interference and service test results against approved engineering acceptance criteria; record defects and reviewer decision.",
        ),
    ),
}
_HANDOVER_REQUIREMENTS = (
    _requirement(
        RequirementCode.AS_BUILT,
        RequirementGate.BEFORE_HANDOVER,
        "Record the actual route or site, equipment installed or removed, photos and deviations; submit network and inventory changes for review.",
    ),
    _requirement(
        RequirementCode.HANDOVER,
        RequirementGate.BEFORE_HANDOVER,
        "Submit field evidence, outstanding defects and customer or network handover for supervisor review. Acceptance, service activation, billing and ticket closure require their separate approvals.",
    ),
)


def recommend_template(
    *, query: WorkOrderTemplateQuery
) -> WorkOrderTemplateRecommendation:
    """Recommend an approved scope without writing or enforcing completion gates.

    Unknown technology supports repair/maintenance diagnosis only. Legacy work
    without explicit classification never calls this query and is not inferred.
    """
    classification = query.classification
    purpose = classification.purpose
    technology = classification.access_technology
    required_kind = {
        WorkOrderPurpose.CUSTOMER_DEPLOYMENT: WorkOrderKind.CUSTOMER,
        WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT: WorkOrderKind.INFRASTRUCTURE,
    }.get(purpose)
    if required_kind is not None and query.kind != required_kind:
        raise WorkOrderTemplateError(
            TemplateErrorCode.INCOMPATIBLE_KIND,
            "Customer deployment requires customer work; rollout requires infrastructure work.",
        )
    if technology == WorkOrderAccessTechnology.UNKNOWN and purpose not in (
        WorkOrderPurpose.REPAIR,
        WorkOrderPurpose.MAINTENANCE,
    ):
        raise WorkOrderTemplateError(
            TemplateErrorCode.TECHNOLOGY_REQUIRED,
            "Select fiber or radio for deployment, rollout, upgrade or relocation.",
        )
    return WorkOrderTemplateRecommendation(
        identity=TemplateIdentity(
            kind=query.kind,
            purpose=purpose,
            access_technology=technology,
            version=1,
        ),
        classification=classification,
        requirements=(
            *_COMMON_REQUIREMENTS,
            _PURPOSE_REQUIREMENTS[purpose],
            *_TECHNOLOGY_REQUIREMENTS[technology],
            *_HANDOVER_REQUIREMENTS,
        ),
    )
