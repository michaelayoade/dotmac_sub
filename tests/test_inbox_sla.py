from datetime import UTC, datetime, time
from types import SimpleNamespace
from uuid import uuid4

from app.models.inbox_sla import InboxSlaPolicy, InboxSlaRule
from app.services.inbox_sla import (
    SlaPolicyInput,
    SlaRuleInput,
    add_business_minutes,
    is_eligible_human_response,
    select_policy_rule,
    validate_policy,
)


def _policy() -> InboxSlaPolicy:
    policy = InboxSlaPolicy(
        name="Nigeria Inbox",
        timezone="Africa/Lagos",
        working_days=[0, 1, 2, 3, 4],
        workday_start=time(9),
        workday_end=time(17),
        holidays=["2026-09-07"],
        is_default=True,
    )
    policy.rules = [
        InboxSlaRule(
            first_response_minutes=60, resolution_minutes=240, warning_minutes=15
        )
    ]
    return policy


def test_business_calendar_skips_weekends_holidays_and_uses_lagos() -> None:
    policy = _policy()
    start = datetime(2026, 9, 4, 16, 30, tzinfo=UTC)  # Friday, 17:30 Lagos
    due = add_business_minutes(start, 60, policy)
    assert due == datetime(2026, 9, 8, 9, 0, tzinfo=UTC)


def test_policy_validation_rejects_overlap_and_bad_warning() -> None:
    command = SlaPolicyInput(
        name="x",
        description=None,
        rules=(
            SlaRuleInput(
                first_response_minutes=10, resolution_minutes=20, warning_minutes=10
            ),
        ),
    )
    try:
        validate_policy(command)
    except Exception as exc:
        assert "warning" in str(exc).lower()
    else:
        raise AssertionError("invalid warning was accepted")


def test_ai_and_system_messages_do_not_satisfy_first_response() -> None:
    assert not is_eligible_human_response(
        SimpleNamespace(direction="outbound", metadata_={"sender_type": "ai"})
    )
    assert not is_eligible_human_response(
        SimpleNamespace(direction="outbound", metadata_={"sender_type": "system"})
    )
    assert is_eligible_human_response(
        SimpleNamespace(
            direction="outbound", metadata_={"sent_by_person_id": str(uuid4())}
        )
    )


def test_rule_specificity_prefers_team_channel_and_priority(db_session) -> None:
    team_id = uuid4()
    policy = _policy()
    policy.rules = [
        InboxSlaRule(
            first_response_minutes=60, resolution_minutes=240, warning_minutes=10
        ),
        InboxSlaRule(
            service_team_id=team_id,
            channel_type="whatsapp",
            priority=1,
            first_response_minutes=5,
            resolution_minutes=20,
            warning_minutes=1,
        ),
    ]
    db_session.add(policy)
    db_session.flush()
    conversation = SimpleNamespace(
        primary_service_team_id=team_id, channel_type="whatsapp", priority=1
    )
    selected = select_policy_rule(db_session, conversation)
    assert selected is not None
    assert selected[1].first_response_minutes == 5
