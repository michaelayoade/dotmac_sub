"""Static ownership guard for the Inbox Lead intake slice."""

from pathlib import Path

from app.services.sot_relationships import all_services


def test_lead_intake_owner_has_complete_manifest_contract():
    service = next(item for item in all_services() if item.name == "sales.lead_intake")
    assert service.module == "app.services.sales.lead_intake"
    assert service.contract is not None
    assert service.contract.transaction.mode.value == "coordinator_managed"
    assert {item.name for item in service.contract.concerns} == set(service.owns)


def test_lead_intake_adapters_do_not_construct_owned_records():
    for path in (
        Path("app/web/public/lead_intake.py"),
        Path("app/web/admin/lead_intake.py"),
        Path("app/services/lead_intake_ai.py"),
        Path("app/services/events/handlers/lead_intake.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "LeadIntakeInvitation(" not in source
        assert "LeadIntakeTemplate(" not in source
        assert "Lead(" not in source
        assert "Party(" not in source


def test_ai_candidate_event_has_one_sales_owned_handler():
    source = Path("app/services/events/handlers/lead_intake.py").read_text(
        encoding="utf-8"
    )
    assert "EventType.ai_intake_lead_candidate_classified" in source
    assert "lead_intake_ai.apply_shared_classification(" in source
    assert "execute_owner_command(" not in source


def test_sales_capture_is_not_gated_on_final_routing_status():
    source = Path("app/services/ai_conversation_intake.py").read_text(encoding="utf-8")
    capture = source.split("def _stage_lead_candidate_classified(", 1)[1].split(
        "@dataclass", 1
    )[0]
    assert "classification.requires_follow_up" not in capture
    assert "AiIntakeStatus.awaiting_follow_up" in capture
    assert "AiIntakeStatus.fallback" in capture
    assert "classification.party_type_confidence < threshold" in capture
    assert "conversation.subscriber_id is not None" in capture
    for path in (
        "app/services/team_inbox_customer_completion.py",
        "app/services/sales/lead_intake.py",
        "app/services/lead_intake_ai.py",
    ):
        assert "ai_lead_candidate_event_id" in Path(path).read_text(encoding="utf-8")


def test_uncertain_sales_candidate_review_has_no_parallel_lead_writer():
    for path in (
        "app/services/ai_conversation_intake.py",
        "app/services/team_inbox_customer_completion.py",
        "app/services/sales/lead_intake.py",
    ):
        assert "ai_sales_candidate_review_reason" in Path(path).read_text(
            encoding="utf-8"
        )
    adapter = Path("scripts/support/reconcile_inbox_classified_leads.py").read_text(
        encoding="utf-8"
    )
    assert "staff_review_required" in adapter
    assert "ClassifiedCandidateDriftQuery(" in adapter
    assert "Lead(" not in adapter
