"""Pure unit evidence for explicit scope recommendations, with no database."""

from dataclasses import FrozenInstanceError

import pytest

from app.services.field.work_order_kind import WorkOrderKind
from app.services.work_order_templates import (
    ClassificationSource,
    ExplicitWorkOrderClassification,
    RequirementCode,
    RequirementGate,
    TemplateErrorCode,
    TemplateRequirementUse,
    TemplateSource,
    WorkOrderAccessTechnology,
    WorkOrderPurpose,
    WorkOrderTemplateError,
    WorkOrderTemplateQuery,
    recommend_template,
)


@pytest.mark.parametrize("kind", tuple(WorkOrderKind))
@pytest.mark.parametrize("purpose", tuple(WorkOrderPurpose))
@pytest.mark.parametrize("technology", tuple(WorkOrderAccessTechnology))
def test_explicit_classification_compatibility_matrix(
    kind: WorkOrderKind,
    purpose: WorkOrderPurpose,
    technology: WorkOrderAccessTechnology,
) -> None:
    query = WorkOrderTemplateQuery(
        kind=kind,
        classification=ExplicitWorkOrderClassification(
            purpose=purpose, access_technology=technology
        ),
    )
    incompatible = (
        purpose == WorkOrderPurpose.CUSTOMER_DEPLOYMENT
        and kind == WorkOrderKind.INFRASTRUCTURE
    ) or (
        purpose == WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT
        and kind == WorkOrderKind.CUSTOMER
    )
    technology_required = (
        technology == WorkOrderAccessTechnology.UNKNOWN
        and purpose
        in (
            WorkOrderPurpose.CUSTOMER_DEPLOYMENT,
            WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            WorkOrderPurpose.UPGRADE,
            WorkOrderPurpose.RELOCATION,
        )
    )
    if incompatible or technology_required:
        with pytest.raises(WorkOrderTemplateError) as rejected:
            recommend_template(query=query)
        assert rejected.value.code == (
            TemplateErrorCode.INCOMPATIBLE_KIND
            if incompatible
            else TemplateErrorCode.TECHNOLOGY_REQUIRED
        )
        return
    recommendation = recommend_template(query=query)
    assert recommendation.identity.kind == kind
    assert recommendation.identity.purpose == purpose
    assert recommendation.identity.access_technology == technology
    assert recommendation.classification.source == (
        ClassificationSource.EXPLICIT_OPERATOR_SELECTION
    )
    assert recommendation.use == TemplateRequirementUse.RECOMMENDATION_ONLY
    assert recommendation.source == TemplateSource.CHECKED_IN_APPROVED_CATALOG


@pytest.mark.parametrize("kind", tuple(WorkOrderKind))
@pytest.mark.parametrize(
    ("technology", "required", "excluded"),
    (
        (
            WorkOrderAccessTechnology.FIBER,
            RequirementCode.FIBER_TESTS,
            RequirementCode.RADIO_TESTS,
        ),
        (
            WorkOrderAccessTechnology.RADIO,
            RequirementCode.RADIO_TESTS,
            RequirementCode.FIBER_TESTS,
        ),
        (
            WorkOrderAccessTechnology.UNKNOWN,
            RequirementCode.TECHNOLOGY_SURVEY,
            RequirementCode.FIBER_TESTS,
        ),
    ),
)
def test_repair_recommendation_has_targeted_evidence_and_review_gates(
    kind: WorkOrderKind,
    technology: WorkOrderAccessTechnology,
    required: RequirementCode,
    excluded: RequirementCode,
) -> None:
    recommendation = recommend_template(
        query=WorkOrderTemplateQuery(
            kind=kind,
            classification=ExplicitWorkOrderClassification(
                purpose=WorkOrderPurpose.REPAIR,
                access_technology=technology,
            ),
        )
    )
    requirements = {item.code: item for item in recommendation.requirements}
    assert required in requirements
    assert excluded not in requirements
    assert requirements[RequirementCode.SUPERVISOR_DISPATCH].gate == (
        RequirementGate.BEFORE_DISPATCH
    )
    assert requirements[RequirementCode.SAFETY_AND_PERMITS].gate == (
        RequirementGate.BEFORE_FIELD_WORK
    )
    assert requirements[RequirementCode.MATERIALS].gate == (
        RequirementGate.BEFORE_FIELD_WORK
    )
    assert (
        requirements[RequirementCode.AS_BUILT].gate == RequirementGate.BEFORE_HANDOVER
    )
    assert (
        requirements[RequirementCode.HANDOVER].gate == RequirementGate.BEFORE_HANDOVER
    )
    assert len(requirements) == len(recommendation.requirements)
    if technology == WorkOrderAccessTechnology.UNKNOWN:
        assert RequirementCode.RADIO_TESTS not in requirements
    else:
        assert requirements[required].gate == RequirementGate.BEFORE_HANDOVER


def test_catalog_replay_is_deterministic_and_recommendations_are_immutable() -> None:
    query = WorkOrderTemplateQuery(
        kind=WorkOrderKind.INFRASTRUCTURE,
        classification=ExplicitWorkOrderClassification(
            purpose=WorkOrderPurpose.INFRASTRUCTURE_ROLLOUT,
            access_technology=WorkOrderAccessTechnology.FIBER,
        ),
    )
    recommendation = recommend_template(query=query)
    assert recommendation == recommend_template(query=query)
    assert recommendation.identity.version == 1
    with pytest.raises(FrozenInstanceError):
        del recommendation.requirements
    with pytest.raises(FrozenInstanceError):
        del recommendation.classification.purpose
