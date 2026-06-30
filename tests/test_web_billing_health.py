from datetime import UTC, datetime

from app.models.autopay import AutopayMandate
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.scheduler import ScheduledTask
from app.models.subscriber import Subscriber
from app.services import web_billing_health


def test_billing_health_data_includes_autopay_summary(db_session):
    account = Subscriber(first_name="Auto", last_name="Pay", email="auto@example.com")
    db_session.add(account)
    db_session.flush()
    db_session.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="autopay_max_consecutive_failures",
            value_text="2",
        )
    )
    db_session.add(
        ScheduledTask(
            name="autopay_runner",
            task_name=web_billing_health.AUTOPAY_TASK_NAME,
            enabled=True,
            interval_seconds=600,
        )
    )
    db_session.add(
        AutopayMandate(
            account_id=account.id,
            is_active=True,
            failure_count=2,
            last_failure_at=datetime(2026, 6, 30, 10, 0, tzinfo=UTC),
            last_failure_reason="card_declined",
        )
    )
    db_session.commit()

    state = web_billing_health.build_billing_health_data(
        db_session, now=datetime(2026, 6, 30, 11, 0, tzinfo=UTC)
    )

    assert state["autopay"]["total"] == 1
    assert state["autopay"]["active"] == 1
    assert state["autopay"]["with_failures"] == 1
    assert state["autopay"]["suspended"] == 1
    assert state["autopay"]["failure_cap"] == 2
    assert state["autopay"]["recent_failures"][0]["account_name"] == "Auto Pay"
    assert state["autopay"]["task"]["enabled"] is True
