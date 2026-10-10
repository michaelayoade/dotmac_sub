"""API regression for typed AI intake configuration response outcomes."""

from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import ai_operations as api
from app.db import get_db
from app.schemas.ai_intake import AiIntakeIntent
from app.schemas.ai_operations import (
    AiIntakeConfigMetadata,
    AiIntakeConfigRead,
    AiIntakeConfigUpsert,
    AiIntakeDepartmentMapping,
)
from app.schemas.common import ListResponse
from app.services import ai_intake
from app.services.auth_dependencies import require_user_auth


@pytest.fixture
def config_outcome() -> ai_intake.AiIntakeConfigOutcome:
    now = datetime.now(UTC)
    return ai_intake.AiIntakeConfigOutcome(
        id=uuid4(),
        scope_key="test:whatsapp",
        channel_type="whatsapp",
        is_enabled=True,
        confidence_threshold=0.8,
        allow_followup_questions=True,
        max_clarification_turns=2,
        escalate_after_minutes=5,
        customer_response_timeout_minutes=7,
        exclude_campaign_attribution=True,
        fallback_team_id=uuid4(),
        instructions="Use the approved support questions.",
        department_mappings=tuple(
            ai_intake.AiIntakeDepartmentMappingOutcome(
                intent=intent, department=department, service_team_id=uuid4()
            )
            for intent, department in (
                (AiIntakeIntent.technical_support, "support"),
                (AiIntakeIntent.billing_issue, "billing"),
                (AiIntakeIntent.complaint, "customer_care"),
            )
        ),
        metadata=ai_intake.AiIntakeConfigMetadataOutcome(
            notes="Response contract regression",
            display_name="Support",
            welcome_message="How can we help?",
            clarification_questions=(
                "What do you need help with?",
                "Is this for an organization?",
            ),
            queue_templates={"support": "We have queued your request."},
            data_cleanup_enabled=False,
        ),
        created_at=now,
        updated_at=now,
        changed=True,
        command_id=uuid4(),
        correlation_id=uuid4(),
    )


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_intake_config_routes_serialize_typed_nested_outcomes(
    method: Literal["GET", "POST"],
    config_outcome: ai_intake.AiIntakeConfigOutcome,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1")

    def database() -> Session:
        return db_session

    def authorized_user() -> dict[str, str]:
        return {"principal_type": "system_user", "principal_id": str(uuid4())}

    def permission_granted() -> None:
        return None

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_user_auth] = authorized_user
    for route in api.router.routes:
        if isinstance(route, APIRoute) and route.path.endswith("/intake-configs"):
            for dependency in route.dependant.dependencies:
                if dependency.call is not None and dependency.call not in {
                    get_db,
                    require_user_auth,
                }:
                    app.dependency_overrides[dependency.call] = permission_granted

    def list_configs(
        db: Session,
        *,
        channel_type: str | None = None,
        enabled: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ai_intake.AiIntakeConfigOutcome, ...]:
        assert db is db_session
        return (config_outcome,)

    def upsert_config(
        db: Session, command: ai_intake.UpsertAiIntakeConfigCommand
    ) -> ai_intake.AiIntakeConfigOutcome:
        assert db is db_session
        assert command.context.scope == ai_intake.CONFIG_SCOPE
        assert command.policy.scope_key == config_outcome.scope_key
        return config_outcome

    monkeypatch.setattr(ai_intake, "list_configs", list_configs)
    monkeypatch.setattr(ai_intake, "upsert_config", upsert_config)
    client = TestClient(app)
    path = "/api/v1/ai-operations/intake-configs"
    if method == "GET":
        response = client.get(path)
        assert response.status_code == 200
        page = ListResponse[AiIntakeConfigRead].model_validate(response.json())
        assert page.count == 1
        assert page.limit == 50
        assert page.offset == 0
        result = page.items[0]
        wire = response.json()["items"][0]
    else:
        payload = AiIntakeConfigUpsert(
            scope_key=config_outcome.scope_key,
            channel_type="whatsapp",
            department_mappings=tuple(
                AiIntakeDepartmentMapping(
                    intent=item.intent,
                    department=item.department,
                    service_team_id=item.service_team_id,
                )
                for item in config_outcome.department_mappings
            ),
            metadata=AiIntakeConfigMetadata(display_name="Support"),
        )
        response = client.post(path, json=payload.model_dump(mode="json"))
        assert response.status_code == 200
        result = AiIntakeConfigRead.model_validate(response.json())
        wire = response.json()

    assert result.id == config_outcome.id
    assert result.confidence_threshold == config_outcome.confidence_threshold
    assert result.customer_response_timeout_minutes == 7
    assert len(result.department_mappings) == 3
    assert result.department_mappings[0].service_team_id == (
        config_outcome.department_mappings[0].service_team_id
    )
    assert result.metadata is not None
    assert result.metadata.display_name == "Support"
    assert result.metadata.queue_templates == {
        "support": "We have queued your request.",
    }
    assert result.metadata.clarification_questions == [
        "What do you need help with?",
        "Is this for an organization?",
    ]
    assert "changed" not in wire
    assert "command_id" not in wire
    assert "correlation_id" not in wire
