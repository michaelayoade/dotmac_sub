from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType, AuditEvent
from app.models.party import Party, PartyContactPoint, PartyContactPointType, PartyType
from app.models.sales import Lead
from app.models.subscriber import Address, AddressType, Subscriber
from app.models.team_inbox import (
    InboxConversation,
    InboxConversationLeadLink,
    InboxConversationStatus,
    InboxCustomerCompletionPolicyVersion,
    InboxMessage,
    InboxReplyMacro,
    InboxStatusTransitionEvent,
)
from app.services import (
    team_inbox_channel_receive,
    team_inbox_customer_completion,
    team_inbox_customer_completion_policy,
    team_inbox_operations,
    team_inbox_status,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext


def _policy(db_session, fields=("name", "phone", "address")):
    existing = db_session.query(InboxCustomerCompletionPolicyVersion).first()
    if existing is not None:
        return existing
    policy = InboxCustomerCompletionPolicyVersion(
        version=1,
        required_fields=list(fields),
        decision_source="pytest",
    )
    db_session.add(policy)
    db_session.flush()
    return policy


def _customer_conversation(
    db_session,
    *,
    name="Ada Lovelace",
    phone="+2348012345678",
    address="1 Example Road",
):
    policy = _policy(db_session)
    parts = name.split(" ", 1) if name else [""]
    customer = Subscriber(
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        display_name=name,
        email=f"{uuid4().hex}@example.test",
        phone=phone,
        address_line1=address,
    )
    db_session.add(customer)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=customer.id,
        customer_completion_policy_version_id=policy.id,
        channel_type="whatsapp",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation, customer


def _lead_conversation(db_session, profile_state="missing_all"):
    policy = _policy(db_session)
    missing_name = profile_state in {"missing_name", "missing_all"}
    missing_phone = profile_state in {"missing_phone", "missing_all"}
    missing_address = profile_state in {"missing_address", "missing_all"}
    party = Party(
        party_type=PartyType.person.value,
        display_name="Unknown Lead" if missing_name else "Ada Lead",
        metadata_=None if missing_address else {"address_line1": "2 Lead Street"},
    )
    db_session.add(party)
    db_session.flush()
    if not missing_phone:
        db_session.add(
            PartyContactPoint(
                party_id=party.id,
                channel_type=PartyContactPointType.phone.value,
                normalized_value="+2348099999999",
                display_value="+2348099999999",
                is_primary=True,
            )
        )
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Explicit Lead fixture",
        title="Inbox Lead",
    )
    db_session.add(lead)
    db_session.flush()
    conversation = InboxConversation(
        customer_completion_policy_version_id=policy.id,
        channel_type="whatsapp",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxConversationLeadLink(
            conversation_id=conversation.id,
            lead_id=lead.id,
            party_id=party.id,
            link_source="reviewed_selection",
            link_reason="Explicit Lead fixture",
            command_id=uuid4(),
        )
    )
    db_session.flush()
    return conversation


def test_disabled_identity_guard_allows_unresolved_resolution(db_session):
    _policy(db_session)
    db_session.commit()
    policy = team_inbox_customer_completion_policy.create_policy_version(
        db_session,
        team_inbox_customer_completion_policy.CreateCustomerCompletionPolicyCommand(
            context=CommandContext.system(
                actor="person:pytest",
                scope="team-inbox:customer-completion-policy",
                reason="pytest disable identity guard",
            ),
            required_fields=(),
            actor_person_id=None,
            actor_type=AuditActorType.service,
            decision_source="pytest_settings",
            identity_guard_enabled=False,
        ),
    )
    conversation = InboxConversation(
        customer_completion_policy_version_id=policy.policy_id,
        channel_type="whatsapp",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )

    assert (
        verdict.classification
        is team_inbox_customer_completion.InboxIdentityClassification.unresolved
    )
    assert verdict.can_agent_resolve is True


def test_classified_sales_candidate_cannot_resolve_until_lead_is_linked(
    db_session,
):
    _policy(db_session)
    db_session.commit()
    policy = team_inbox_customer_completion_policy.create_policy_version(
        db_session,
        team_inbox_customer_completion_policy.CreateCustomerCompletionPolicyCommand(
            context=CommandContext.system(
                actor="person:pytest",
                scope="team-inbox:customer-completion-policy",
                reason="pytest disable identity guard for lead classification",
            ),
            required_fields=(),
            actor_person_id=None,
            actor_type=AuditActorType.service,
            decision_source="pytest_settings",
            identity_guard_enabled=False,
        ),
    )
    conversation = InboxConversation(
        customer_completion_policy_version_id=policy.policy_id,
        channel_type="instagram_dm",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type="instagram_dm",
            direction="inbound",
            body="I need a new business connection",
            from_address="17841400000000001",
            external_message_id=f"m_ig_{uuid4().hex}",
            metadata_={
                "ai_intake_status": "classified",
                "ai_intake_requires_follow_up": False,
                "ai_intent": "new_connection",
                "ai_party_type": "organization",
            },
        )
    )
    db_session.flush()

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )

    assert verdict.can_agent_resolve is False
    assert verdict.readiness.blockers[0].code == "inbox_lead_materialization_required"
    assert verdict.readiness.blockers[0].owner == "sales.lead_intake"
    assert not team_inbox_status.resolution_readiness(
        db_session, conversation
    ).can_resolve_as_spam
    with pytest.raises(team_inbox_status.InboxResolutionError):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=None,
            reason=team_inbox_status.InboxStatusReason.operator_change,
            resolution_reason=team_inbox_status.InboxResolutionReason.spam_irrelevant,
        )
    assert conversation.status == "open"


@pytest.mark.parametrize("channel", ["email", "whatsapp", "instagram_dm"])
def test_unlinked_chat_can_resolve_as_spam_with_audit_and_no_csat(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    from app.services import support_csat

    csat = Mock(spec=support_csat.ensure_inbox_request)
    monkeypatch.setattr(support_csat, "ensure_inbox_request", csat)
    policy = _policy(db_session)
    conversation = InboxConversation(
        customer_completion_policy_version_id=policy.id,
        channel_type=channel,
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    readiness = team_inbox_status.resolution_readiness(db_session, conversation)
    assert readiness.can_resolve_as_spam
    assert not readiness.customer_readiness.can_agent_resolve
    command = team_inbox_status.InboxStatusTransitionCommand(
        conversation_id=conversation.id,
        status=InboxConversationStatus.resolved,
        actor_person_id=None,
        reason=team_inbox_status.InboxStatusReason.operator_change,
        source_id=f"pytest-spam:{uuid4()}",
        occurred_at=datetime.now(UTC),
        compatibility_source="pytest",
        resolution_reason=team_inbox_status.InboxResolutionReason.spam_irrelevant,
    )
    # Exercise the same typed transition as direct/bulk/macro adapters.
    outcome = team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=command.status,
        actor_person_id=command.actor_person_id,
        reason=command.reason,
        source_id=command.source_id,
        occurred_at=command.occurred_at,
        resolution_reason=command.resolution_reason,
    )
    event = db_session.get(InboxStatusTransitionEvent, outcome.event_id)
    assert event is not None
    assert event.resolution_reason == "spam_irrelevant"
    assert conversation.status == "resolved"
    assert conversation.subscriber_id is None
    assert (
        conversation.metadata_["status_history"][-1]["resolution_reason"]
        == "spam_irrelevant"
    )
    repeated = team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=command.status,
        actor_person_id=None,
        reason=command.reason,
        resolution_reason=command.resolution_reason,
    )
    assert repeated.already_set
    assert db_session.query(InboxStatusTransitionEvent).count() == 1
    csat.assert_not_called()


@pytest.mark.parametrize("identity", ["customer", "lead", "ambiguous"])
def test_spam_resolution_rechecks_identity_before_writing(
    db_session: Session, identity: str
) -> None:
    policy = _policy(db_session)
    conversation = InboxConversation(
        customer_completion_policy_version_id=policy.id,
        channel_type="email",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    assert team_inbox_status.resolution_readiness(
        db_session, conversation
    ).can_resolve_as_spam
    _linked, customer = _customer_conversation(db_session)
    if identity == "customer":
        conversation.subscriber_id = customer.id
    else:
        lead_conversation = _lead_conversation(db_session)
        link = (
            db_session.query(InboxConversationLeadLink)
            .filter_by(conversation_id=lead_conversation.id)
            .one()
        )
        link.conversation_id = conversation.id
        if identity == "ambiguous":
            customer_party = Party(
                party_type=PartyType.person.value,
                display_name="Different Customer",
            )
            db_session.add(customer_party)
            db_session.flush()
            customer.party_id = customer_party.id
            customer.party_bound_at = datetime.now(UTC)
            customer.party_binding_source = "pytest"
            customer.party_binding_reason = "Explicit conflicting identity fixture"
            conversation.subscriber_id = customer.id
    db_session.flush()
    assert not team_inbox_status.resolution_readiness(
        db_session, conversation
    ).can_resolve_as_spam
    with pytest.raises(team_inbox_status.InboxResolutionError):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=None,
            reason=team_inbox_status.InboxStatusReason.operator_change,
            resolution_reason=team_inbox_status.InboxResolutionReason.spam_irrelevant,
        )
    assert conversation.status == "open"
    assert db_session.query(InboxStatusTransitionEvent).count() == 0


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("name", "name"),
        ("phone", "phone"),
        ("address", "address"),
    ],
)
def test_customer_missing_configured_field_blocks_resolution(
    db_session, missing, expected
):
    values = {
        "name": "Ada Lovelace",
        "phone": "+2348012345678",
        "address": "1 Example Road",
    }
    values[missing] = ""
    conversation, _customer = _customer_conversation(db_session, **values)

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )

    assert verdict.can_agent_resolve is False
    assert [field.value for field in verdict.missing_fields] == [expected]
    with pytest.raises(DomainError, match="Cannot resolve conversation"):
        team_inbox_status.apply_status_transition(
            db_session,
            conversation=conversation,
            status=InboxConversationStatus.resolved,
            actor_person_id=uuid4(),
            reason=team_inbox_status.InboxStatusReason.operator_change,
        )


def test_complete_customer_can_resolve(db_session):
    conversation, _customer = _customer_conversation(db_session)

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.resolved,
        actor_person_id=uuid4(),
        reason=team_inbox_status.InboxStatusReason.operator_change,
    )

    assert verdict.can_agent_resolve is True
    assert conversation.status == "resolved"


def test_explicit_lead_context_outranks_customer_role_for_same_party(db_session):
    conversation, customer = _customer_conversation(
        db_session,
        name="",
        phone="",
        address="",
    )
    party = Party(
        party_type=PartyType.person.value,
        display_name="Existing Customer Prospect",
    )
    db_session.add(party)
    db_session.flush()
    customer.party_id = party.id
    customer.party_bound_at = datetime.now(UTC)
    customer.party_binding_source = "pytest"
    customer.party_binding_reason = "Same Party Customer and Lead context"
    lead = Lead(
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="New service opportunity for existing Customer",
        title="Second location",
    )
    db_session.add(lead)
    db_session.flush()
    db_session.add(
        InboxConversationLeadLink(
            conversation_id=conversation.id,
            lead_id=lead.id,
            party_id=party.id,
            link_source="reviewed_selection",
            link_reason="Conversation explicitly handled as sales",
            command_id=uuid4(),
        )
    )
    db_session.flush()

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )

    assert verdict.classification.value == "lead"
    assert verdict.can_agent_resolve is True
    assert verdict.fields == ()


def test_unreviewed_phone_match_is_narrowed_by_exact_observed_name(db_session):
    customer = Subscriber(
        first_name="Ada",
        last_name="Lovelace",
        display_name="Ada Lovelace",
        email=f"{uuid4().hex}@example.test",
        phone="08012345678",
    )
    db_session.add(customer)
    db_session.flush()

    matched = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type="whatsapp",
        contact_address="+2348012345678",
        contact_name="Ada Lovelace",
    )
    mismatch = team_inbox_channel_receive.resolve_contact_context(
        db_session,
        channel_type="whatsapp",
        contact_address="+2348012345678",
        contact_name="Different Person",
    )

    assert matched.subscriber_id == customer.id
    assert matched.status == "linked_subscriber"
    assert mismatch.subscriber_id is None
    assert mismatch.status == "ambiguous"


def test_inbox_save_updates_canonical_customer_and_refreshes_readiness(db_session):
    conversation, customer = _customer_conversation(db_session, address="")
    party = Party(
        party_type=PartyType.person.value,
        display_name="Canonical Party Name",
    )
    db_session.add(party)
    db_session.flush()
    customer.party_id = party.id
    customer.party_bound_at = datetime.now(UTC)
    customer.party_binding_source = "pytest"
    customer.party_binding_reason = "Explicit canonical Party fixture"
    conversation_id = conversation.id
    customer_id = customer.id
    party_id = party.id
    db_session.commit()

    outcome = team_inbox_customer_completion.complete_customer_profile(
        db_session,
        team_inbox_customer_completion.CompleteInboxCustomerProfileCommand(
            context=CommandContext.system(
                actor="person:pytest",
                scope="team-inbox:customer-profile-completion",
                reason="pytest canonical save",
            ),
            conversation_id=conversation_id,
            customer_id=customer_id,
            values=team_inbox_customer_completion.CustomerProfileValues(
                address="10 Canonical Avenue"
            ),
            submitted_fields=frozenset(
                {team_inbox_customer_completion.CustomerProfileField.address}
            ),
            confirmed_replacements=frozenset(),
            actor_person_id=None,
            actor_type=AuditActorType.service,
            decision_source="pytest_inbox",
        ),
    )

    db_session.expire_all()
    saved_customer = db_session.get(Subscriber, customer_id)
    saved_party = db_session.get(Party, party_id)
    saved_address = (
        db_session.query(Address)
        .filter_by(subscriber_id=customer_id, address_type=AddressType.service)
        .one()
    )
    audit = (
        db_session.query(AuditEvent)
        .filter_by(action="inbox_customer_profile.changed")
        .one()
    )
    assert saved_customer.address_line1 == "10 Canonical Avenue"
    assert saved_address.address_line1 == "10 Canonical Avenue"
    assert saved_party.metadata_["address_line1"] == "10 Canonical Avenue"
    assert saved_party.display_name == "Canonical Party Name"
    assert outcome.readiness.can_agent_resolve is True
    assert audit.metadata_["previous_value"] is None
    assert audit.metadata_["new_value"] == "10 Canonical Avenue"
    assert audit.metadata_["decision_source"] == "pytest_inbox"


def test_customer_value_replacement_requires_explicit_confirmation(db_session):
    conversation, customer = _customer_conversation(db_session)
    conversation_id = conversation.id
    customer_id = customer.id
    original_phone = customer.phone
    db_session.commit()
    base = dict(
        context=CommandContext.system(
            actor="person:pytest",
            scope="team-inbox:customer-profile-completion",
            reason="pytest conflict",
        ),
        conversation_id=conversation_id,
        customer_id=customer_id,
        values=team_inbox_customer_completion.CustomerProfileValues(
            phone="08099999999"
        ),
        submitted_fields=frozenset(
            {team_inbox_customer_completion.CustomerProfileField.phone}
        ),
        actor_person_id=None,
        actor_type=AuditActorType.service,
        decision_source="pytest_inbox",
    )

    with pytest.raises(DomainError) as captured:
        team_inbox_customer_completion.complete_customer_profile(
            db_session,
            team_inbox_customer_completion.CompleteInboxCustomerProfileCommand(
                **base,
                confirmed_replacements=frozenset(),
            ),
        )

    assert captured.value.code.endswith("replacement_confirmation_required")
    assert captured.value.details["existing_value"] == original_phone
    assert captured.value.details["proposed_value"] == "08099999999"
    assert db_session.get(Subscriber, customer_id).phone == original_phone


def test_customer_replacement_still_passes_duplicate_identity_validation(db_session):
    conversation, customer = _customer_conversation(db_session)
    other = Subscriber(
        first_name="Other",
        last_name="Customer",
        display_name="Other Customer",
        email=f"{uuid4().hex}@example.test",
        phone="+2348099999999",
    )
    db_session.add(other)
    db_session.flush()
    conversation_id = conversation.id
    customer_id = customer.id
    original_phone = customer.phone
    db_session.commit()

    with pytest.raises(DomainError) as captured:
        team_inbox_customer_completion.complete_customer_profile(
            db_session,
            team_inbox_customer_completion.CompleteInboxCustomerProfileCommand(
                context=CommandContext.system(
                    actor="person:pytest",
                    scope="team-inbox:customer-profile-completion",
                    reason="pytest duplicate conflict",
                ),
                conversation_id=conversation_id,
                customer_id=customer_id,
                values=team_inbox_customer_completion.CustomerProfileValues(
                    phone="+2348099999999"
                ),
                submitted_fields=frozenset(
                    {team_inbox_customer_completion.CustomerProfileField.phone}
                ),
                confirmed_replacements=frozenset(
                    {team_inbox_customer_completion.CustomerProfileField.phone}
                ),
                actor_person_id=None,
                actor_type=AuditActorType.service,
                decision_source="pytest_inbox",
            ),
        )

    assert captured.value.code.endswith("duplicate_identity_conflict")
    assert db_session.get(Subscriber, customer_id).phone == original_phone


def test_policy_change_is_versioned_and_existing_conversation_keeps_snapshot(
    db_session,
):
    conversation, _customer = _customer_conversation(db_session)
    conversation_id = conversation.id
    original_policy_id = conversation.customer_completion_policy_version_id
    db_session.commit()

    outcome = team_inbox_customer_completion_policy.create_policy_version(
        db_session,
        team_inbox_customer_completion_policy.CreateCustomerCompletionPolicyCommand(
            context=CommandContext.system(
                actor="person:pytest",
                scope="team-inbox:customer-completion-policy",
                reason="pytest policy version",
            ),
            required_fields=(
                team_inbox_customer_completion_policy.CustomerCompletionField.name,
                team_inbox_customer_completion_policy.CustomerCompletionField.email,
            ),
            actor_person_id=None,
            actor_type=AuditActorType.service,
            decision_source="pytest_settings",
        ),
    )

    existing = db_session.get(InboxConversation, conversation_id)
    assert outcome.version == 2
    assert existing.customer_completion_policy_version_id == original_policy_id
    assert (
        team_inbox_customer_completion_policy.snapshot_active_policy_id(db_session)
        == outcome.policy_id
    )


def test_customer_gate_uses_empty_snapshotted_policy_without_hard_coded_fields(
    db_session,
):
    _policy(db_session)
    db_session.commit()
    policy = team_inbox_customer_completion_policy.create_policy_version(
        db_session,
        team_inbox_customer_completion_policy.CreateCustomerCompletionPolicyCommand(
            context=CommandContext.system(
                actor="person:pytest",
                scope="team-inbox:customer-completion-policy",
                reason="pytest empty policy",
            ),
            required_fields=(),
            actor_person_id=None,
            actor_type=AuditActorType.service,
            decision_source="pytest_settings",
        ),
    )
    customer = Subscriber(
        first_name="",
        last_name="",
        display_name="",
        email=f"{uuid4().hex}@example.test",
        phone=None,
        address_line1=None,
    )
    db_session.add(customer)
    db_session.flush()
    conversation = InboxConversation(
        subscriber_id=customer.id,
        customer_completion_policy_version_id=policy.policy_id,
        channel_type="whatsapp",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )

    assert verdict.fields == ()
    assert verdict.can_agent_resolve is True


@pytest.mark.parametrize(
    "profile_state",
    ["complete", "missing_name", "missing_phone", "missing_address", "missing_all"],
)
def test_lead_profile_completeness_never_blocks_resolution(db_session, profile_state):
    conversation = _lead_conversation(db_session, profile_state)

    verdict = team_inbox_customer_completion.resolution_readiness(
        db_session, conversation
    )
    team_inbox_status.apply_status_transition(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.resolved,
        actor_person_id=uuid4(),
        reason=team_inbox_status.InboxStatusReason.operator_change,
    )

    assert verdict.classification.value == "lead"
    assert verdict.can_agent_resolve is True
    assert conversation.status == "resolved"


def test_bulk_resolution_skips_incomplete_customer_and_resolves_lead(db_session):
    customer_conversation, _customer = _customer_conversation(db_session, address="")
    lead_conversation = _lead_conversation(db_session)

    outcome = team_inbox_operations.bulk_update_status(
        db_session,
        conversation_ids=[customer_conversation.id, lead_conversation.id],
        status_value="resolved",
        actor_person_id=uuid4(),
    )

    assert outcome["updated"] == [str(lead_conversation.id)]
    assert outcome["skipped"] == [str(customer_conversation.id)]
    assert outcome["blocked"][0]["details"]["missing_fields"] == ["address"]
    assert customer_conversation.status == "open"
    assert lead_conversation.status == "resolved"


@pytest.mark.parametrize("kind", ["customer", "lead"])
def test_macro_uses_customer_only_completion_gate(db_session, kind):
    if kind == "customer":
        conversation, _customer = _customer_conversation(db_session, phone="")
    else:
        conversation = _lead_conversation(db_session)
    macro = InboxReplyMacro(
        name=f"Resolve {kind}",
        body_text="Done",
        actions=[{"action_type": "set_status", "params": {"status": "resolved"}}],
    )
    db_session.add(macro)
    db_session.flush()

    outcome = team_inbox_operations.execute_macro_actions(
        db_session,
        conversation=conversation,
        macro_id=macro.id,
        actor_person_id=uuid4(),
    )

    if kind == "customer":
        assert outcome["actions_failed"] == 1
        assert conversation.status == "open"
    else:
        assert outcome["actions_failed"] == 0
        assert conversation.status == "resolved"


def _unlinked_conversation(db_session):
    conversation = InboxConversation(
        customer_completion_policy_version_id=_policy(db_session).id,
        channel_type="email",
        status="open",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def test_bulk_spam_reason_keeps_identity_gate_and_skips_unlinked(db_session):
    unlinked = _unlinked_conversation(db_session)
    lead_conversation = _lead_conversation(db_session)

    outcome = team_inbox_operations.bulk_update_status(
        db_session,
        conversation_ids=[unlinked.id, lead_conversation.id],
        status_value="resolved",
        actor_person_id=uuid4(),
        resolution_reason=team_inbox_status.InboxResolutionReason.spam_irrelevant,
    )

    assert outcome["updated"] == [str(lead_conversation.id)]
    assert outcome["skipped"] == [str(unlinked.id)]
    assert unlinked.status == "open"
    assert lead_conversation.status == "resolved"


def test_bulk_skips_spam_resolution_not_allowed_without_aborting(
    db_session, monkeypatch
):
    blocked = _unlinked_conversation(db_session)
    lead_conversation = _lead_conversation(db_session)
    real = team_inbox_status.apply_status_transition

    def fake(db, *, conversation, **kwargs):
        if conversation.id == blocked.id:
            raise team_inbox_status.InboxResolutionError(
                code="communications.team_inbox_status.spam_resolution_not_allowed",
                message="not allowed",
                details={"conversation_id": str(conversation.id)},
            )
        return real(db, conversation=conversation, **kwargs)

    monkeypatch.setattr(team_inbox_status, "apply_status_transition", fake)
    outcome = team_inbox_operations.bulk_update_status(
        db_session,
        conversation_ids=[blocked.id, lead_conversation.id],
        status_value="resolved",
        actor_person_id=uuid4(),
    )

    assert outcome["updated"] == [str(lead_conversation.id)]
    assert outcome["skipped"] == [str(blocked.id)]
    assert lead_conversation.status == "resolved"


def test_macro_spam_reason_keeps_identity_gate(db_session):
    conversation = _unlinked_conversation(db_session)
    macro = InboxReplyMacro(
        name="Resolve spam",
        body_text="Done",
        actions=[{"action_type": "set_status", "params": {"status": "resolved"}}],
    )
    db_session.add(macro)
    db_session.flush()

    outcome = team_inbox_operations.execute_macro_actions(
        db_session,
        conversation=conversation,
        macro_id=macro.id,
        actor_person_id=uuid4(),
        resolution_reason=team_inbox_status.InboxResolutionReason.spam_irrelevant,
    )

    assert outcome["actions_failed"] == 1
    assert conversation.status == "open"
