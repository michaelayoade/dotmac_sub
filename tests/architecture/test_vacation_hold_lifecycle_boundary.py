"""Vacation-hold adapters must delegate policy and writes to lifecycle owners."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_vacation_policy_and_commands_are_lifecycle_owned() -> None:
    policy = _source("app/services/subscription_lifecycle.py")
    execution = _source("app/services/subscription_lifecycle_commands.py")
    assert "class VacationHoldPolicyDecision" in policy
    assert "def resolve_vacation_hold_policy(" in policy
    assert 'vacation_hold = "vacation_hold"' in policy
    assert 'vacation_resume = "vacation_resume"' in policy
    assert "SubscriptionPauseReason.customer_vacation_hold" in execution
    assert "EnforcementReason.customer_hold" not in execution
    assert "resolve_vacation_hold_policy(" not in execution


def test_vacation_adapters_do_not_call_account_lifecycle_writers() -> None:
    for relative in (
        "app/services/customer_portal_flow_services.py",
        "app/services/web_catalog_subscription_workflows.py",
        "app/tasks/vacation_holds.py",
    ):
        source = _source(relative)
        assert "suspend_subscription(" not in source
        assert "restore_subscription(" not in source
        assert "execute_subscription_command(" in source


def test_vacation_task_does_not_complete_transactions() -> None:
    source = _source("app/tasks/vacation_holds.py")
    assert "session.commit(" not in source
    assert "session.rollback(" not in source


def test_admin_ui_distinguishes_suspend_from_pause_billing() -> None:
    policy = _source("app/services/subscription_lifecycle.py")
    execution = _source("app/services/subscription_lifecycle_commands.py")
    template = _source("templates/admin/catalog/subscription_detail.html")

    assert 'pause = "pause"' in policy
    assert 'resume_pause = "resume_pause"' in policy
    assert "SubscriptionPauseReason.administrative" in execution
    assert "Pause billing and service" in template
    assert "Suspend access and billing" in template
    assert (
        "Suspension blocks network access and stops future recurring billing."
        in template
    )
    assert "Suspension does not preserve unused time." in template
