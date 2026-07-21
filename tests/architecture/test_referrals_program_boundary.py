from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/referrals.py"


def _source(path: str | Path) -> str:
    resolved = path if isinstance(path, Path) else ROOT / path
    return resolved.read_text(encoding="utf-8")


def test_referrals_program_has_a_complete_coordinator_contract() -> None:
    service = service_relationship("referrals.program")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.COORDINATOR_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in all_services()},
    )
    concerns = {item.name: item for item in service.contract.concerns}
    assert concerns["canonical Referral program record"].role is (
        OwnerRole.AUTHORITATIVE_RECORD
    )
    assert concerns["Referral Subscriber attachment record"].canonical_writer == (
        "referrals.program"
    )
    assert concerns["referral qualification and reward policy"].role is (
        OwnerRole.POLICY
    )
    assert concerns["atomic referral program transition orchestration"].role is (
        OwnerRole.APPLICATION_COORDINATOR
    )


def test_referrals_program_owner_is_transport_and_transaction_neutral() -> None:
    source = _source(OWNER)
    assert "fastapi" not in source
    assert "HTTPException" not in source
    assert "os.getenv" not in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert "begin_nested" not in source
    assert "crm_api" not in source
    assert "push_service" not in source
    assert "execute_owner_command(" in source


def test_referral_writes_use_typed_commands_and_no_legacy_mutation_surface() -> None:
    source = _source(OWNER)
    for command in (
        "EnsureReferralCodeCommand",
        "CaptureReferralCommand",
        "ReferFriendCommand",
        "QualifyReferralForSubscriberCommand",
        "QualifyReferralOverrideCommand",
        "RejectReferralCommand",
        "IssueReferralRewardCommand",
    ):
        assert f"class {command}:" in source
    for legacy in (
        "def ensure_code(",
        "def capture(",
        "def qualify_for_subscriber(",
        "def qualify_override(",
        "def issue_reward(",
        "def reject(",
        "def refer_a_friend(",
        "def attach_subscriber(",
    ):
        assert legacy not in source


def test_production_adapters_delegate_program_mutations() -> None:
    api = _source("app/api/crm_referrals.py")
    customer_api = _source("app/api/me.py")
    admin = _source("app/web/admin/crm_referrals.py")
    portal = _source("app/web/customer/referrals.py")
    handler = _source("app/services/events/handlers/referral.py")
    combined = "\n".join((api, customer_api, admin, portal, handler))
    for command in (
        "EnsureReferralCodeCommand",
        "CaptureReferralCommand",
        "ReferFriendCommand",
        "QualifyReferralForSubscriberCommand",
        "QualifyReferralOverrideCommand",
        "RejectReferralCommand",
        "IssueReferralRewardCommand",
    ):
        assert command in combined
    assert ".commit(" not in combined
    assert ".rollback(" not in combined


def test_reward_and_runtime_policy_use_their_canonical_owners() -> None:
    owner = _source(OWNER)
    credit_notes = _source("app/services/billing/credit_notes.py")
    settings = _source("app/services/settings_spec.py")
    notification = _source("app/services/events/handlers/notification.py")
    assert "CreditNotes.issue_referral_reward(" in owner
    assert "def issue_referral_reward(" in credit_notes
    assert 'key="referral_share_base_url"' in settings
    assert 'env_var="PORTAL_REFERRAL_SHARE_BASE"' in settings
    assert "EventType.referral_reward_issued" in notification
    assert "dedupe_key=" in notification


def test_attachment_writer_is_nested_only_under_approved_coordinators() -> None:
    callers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        if path == OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        if "attach_subscriber_for_conversion(" in source:
            callers.add(str(path.relative_to(ROOT)))
    assert callers == {"app/services/referral_account_conversion.py"}
