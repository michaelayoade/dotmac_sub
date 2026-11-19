from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_reply_window_is_the_only_expiry_definition() -> None:
    reply_window = _source("app/services/team_inbox_reply_window.py")
    projection_writer = _source("app/services/team_inbox_reply_window_projection.py")
    assignment = _source("app/services/team_inbox_assignment.py")
    assert "WINDOW_HOURS = 24" in reply_window
    assert "InboxMessage" not in reply_window
    assert "conversation.last_qualifying_inbound_at" in reply_window
    assert "def project_qualifying_inbound" in projection_writer
    assert "expired_whatsapp_conversation_ids_query" in assignment
    assert "timedelta(hours=24)" not in assignment


def test_expiry_release_is_a_scheduled_typed_owner_path() -> None:
    maintenance = _source("app/services/team_inbox_maintenance.py")
    assignment = _source("app/services/team_inbox_assignment.py")
    receive = _source("app/services/team_inbox_channel_receive.py")
    tasks = _source("app/tasks/team_inbox.py")
    scheduler = _source("app/services/scheduler_config.py")
    assert "class WhatsAppWindowExpirySweepCommand" in maintenance
    assert "class ReleaseExpiredWhatsAppConversationCommand" in assignment
    assert (
        "reason_code=InboxAssignmentReleaseReason.whatsapp_window_expired.value"
        in assignment
    )
    assert "expire_whatsapp_service_windows" in tasks
    assert "managed_after rollout watermark is required" in tasks
    assert 'name="team_inbox_whatsapp_window_expiry"' in scheduler
    assert "release_expired_whatsapp_conversation" in receive


def test_reply_window_projection_has_one_writer_and_a_bounded_reconciler() -> None:
    receive = _source("app/services/team_inbox_channel_receive.py")
    maintenance = _source("app/services/team_inbox_maintenance.py")
    tasks = _source("app/tasks/team_inbox.py")
    migration = _source("alembic/versions/710_inbox_reply_window_projection.py")
    assert receive.count("project_qualifying_inbound(") == 1
    assert "class ReconcileReplyWindowProjectionCommand" in maintenance
    assert "def reconcile_reply_window_projection" in maintenance
    assert "reconcile_reply_window_projection" in tasks
    assert "_verify_projection()" in migration
    assert "CREATE INDEX CONCURRENTLY" in migration


def test_expired_resolution_is_separate_from_customer_completion() -> None:
    status = _source("app/services/team_inbox_status.py")
    operations = _source("app/services/team_inbox_operations.py")
    projection = _source("app/services/team_inbox_projection.py")
    assert "class InboxResolutionReason" in status
    assert "requires_resolution_reason" in status
    assert "require_agent_resolution_ready" in status
    assert "release_expired_whatsapp_conversation" in status
    assert "channel_state_at_resolution" in status
    assert "expired_whatsapp_conversation_ids_query" in operations
    assert "not expired_whatsapp" in projection


def test_registry_declares_expiry_dependencies() -> None:
    routing = service_relationship("communications.team_inbox_routing")
    status = service_relationship("communications.team_inbox_status")
    maintenance = service_relationship("communications.team_inbox_maintenance")
    assert "communications.team_inbox_reply_window" in routing.depends_on
    assert "communications.team_inbox_reply_window" in status.depends_on
    assert "communications.team_inbox_reply_window" in maintenance.depends_on
