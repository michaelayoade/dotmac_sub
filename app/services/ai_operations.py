from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.ai_insight import AIInsight, AIInsightStatus
from app.models.ai_intake import AiIntakeConfig
from app.services.common import coerce_uuid


def _now() -> datetime:
    return datetime.now(UTC)


def _insight_or_404(db: Session, insight_id: str | UUID) -> AIInsight:
    insight = db.get(AIInsight, coerce_uuid(insight_id))
    if insight is None:
        raise HTTPException(status_code=404, detail="AI insight not found")
    return insight


def create_insight(
    db: Session,
    payload,
    *,
    triggered_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    insight = AIInsight(
        persona_key=payload.persona_key,
        domain=payload.domain,
        severity=payload.severity,
        status=AIInsightStatus.pending.value,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        title=payload.title,
        summary=payload.summary,
        structured_output=payload.structured_output,
        confidence_score=payload.confidence_score,
        recommendations=payload.recommendations,
        context_quality_score=payload.context_quality_score,
        llm_provider="native",
        trigger=payload.trigger,
        triggered_by_system_user_id=coerce_uuid(triggered_by_system_user_id),
        expires_at=payload.expires_at,
        metadata_=payload.metadata or {},
    )
    db.add(insight)
    db.flush()
    return insight


def list_insights(
    db: Session,
    *,
    domain: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AIInsight]:
    query = db.query(AIInsight)
    if domain:
        query = query.filter(AIInsight.domain == domain)
    if status:
        query = query.filter(AIInsight.status == status)
    if severity:
        query = query.filter(AIInsight.severity == severity)
    return (
        query.order_by(AIInsight.created_at.desc(), AIInsight.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def acknowledge_insight(
    db: Session,
    insight_id: str | UUID,
    *,
    acknowledged_by_system_user_id: str | UUID | None = None,
) -> AIInsight:
    insight = _insight_or_404(db, insight_id)
    insight.status = AIInsightStatus.acknowledged.value
    insight.acknowledged_at = _now()
    insight.acknowledged_by_system_user_id = coerce_uuid(acknowledged_by_system_user_id)
    db.flush()
    return insight


def expire_stale_insights(
    db: Session, *, now: datetime | None = None, limit: int = 500
) -> int:
    current_time = now or _now()
    expirable_statuses = [
        AIInsightStatus.pending.value,
        AIInsightStatus.completed.value,
        AIInsightStatus.failed.value,
        AIInsightStatus.skipped.value,
    ]
    rows = (
        db.query(AIInsight)
        .filter(AIInsight.expires_at.is_not(None))
        .filter(AIInsight.expires_at <= current_time)
        .filter(AIInsight.status.in_(expirable_statuses))
        .limit(limit)
        .all()
    )
    for insight in rows:
        insight.status = AIInsightStatus.expired.value
    db.flush()
    return len(rows)


def upsert_intake_config(db: Session, payload) -> AiIntakeConfig:
    config = (
        db.query(AiIntakeConfig)
        .filter(AiIntakeConfig.scope_key == payload.scope_key)
        .one_or_none()
    )
    if config is None:
        config = AiIntakeConfig(
            scope_key=payload.scope_key, channel_type=payload.channel_type
        )
        db.add(config)
    config.channel_type = payload.channel_type
    config.is_enabled = payload.is_enabled
    config.confidence_threshold = payload.confidence_threshold
    config.allow_followup_questions = payload.allow_followup_questions
    config.max_clarification_turns = payload.max_clarification_turns
    config.escalate_after_minutes = payload.escalate_after_minutes
    config.exclude_campaign_attribution = payload.exclude_campaign_attribution
    config.fallback_team_id = payload.fallback_team_id
    config.instructions = payload.instructions
    config.department_mappings = payload.department_mappings
    config.metadata_ = payload.metadata or {}
    db.flush()
    return config


def list_intake_configs(
    db: Session,
    *,
    channel_type: str | None = None,
    enabled: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AiIntakeConfig]:
    query = db.query(AiIntakeConfig)
    if channel_type:
        query = query.filter(AiIntakeConfig.channel_type == channel_type)
    if enabled is not None:
        query = query.filter(AiIntakeConfig.is_enabled == enabled)
    return (
        query.order_by(AiIntakeConfig.scope_key.asc()).limit(limit).offset(offset).all()
    )
