"""What each cutover stage actually does, and what the activation gate refuses.

The architecture suite proves the stage branch lives in one file. It cannot
prove the branch is CORRECT — that LOCAL leaves the modules empty, that SHADOW
keeps ids aligned, that MODULE refuses what it must. Those are behaviours, and
until they are exercised the control plane is a well-shaped guess.

Everything here runs on the unit-test session (SQLite, no RLS), because none of
it is a tenancy question. The identity, refusal and comparator claims are pure
logic over rows. The one thing SQLite cannot answer — that a failed module write
takes Sub's row down with it — is asserted through the session's own
transaction, and the PostgreSQL concurrency canaries stay the RLS suite's job.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from dotmac_inbox.models import Conversation as ModuleConversation
from dotmac_inbox.models import Message as ModuleMessage

from app.models.team_inbox import (
    InboxChannelType,
    InboxConversation,
    InboxConversationStatus,
    InboxMessage,
    TeamInboxChannelRoute,
)
from app.services import inbox_writes
from app.services.inbox_authority import (
    ActivateInboxAuthority,
    InboxAuthorityActivationRefused,
    InboxAuthorityStage,
    activate,
    cutover_record,
    drift_fingerprint,
    resolve_stage,
)
from app.services.inbox_projection_reconciler import compare, reconcile

ACCOUNT = "support@dotmac.ng"
CONTACT = "customer@example.com"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def team_id(db_session):
    """A service team with one active email route, so the ladder resolves.

    Rung 2 of the ADR-0013 § 5 ladder needs EXACTLY ONE active route for the
    team and channel. Creating two would make the scope unresolvable, which is
    its own test below rather than an accident here.
    """
    team_uuid = uuid.uuid4()
    db_session.add(
        TeamInboxChannelRoute(
            service_team_id=team_uuid,
            channel_type=InboxChannelType.email.value,
            provider="smtp",
            account_scope=ACCOUNT,
            is_active=True,
        )
    )
    db_session.flush()
    return team_uuid


def _open(db, *, contact=CONTACT, team=None, thread=None, when=None):
    return inbox_writes.open_conversation(
        db,
        channel=InboxChannelType.email.value,
        contact=contact,
        external_thread_id=thread,
        subject="Subject",
        occurred_at=when or datetime.now(UTC),
        service_team_id=team,
        provider_account_scope=ACCOUNT if team is None else None,
    )


def _module_conversations(db):
    return list(db.scalars(ModuleConversation.__table__.select()))


# --------------------------------------------------------------------------
# stage resolution
# --------------------------------------------------------------------------


def test_stage_is_local_with_no_row_and_no_setting(db_session):
    assert resolve_stage(db_session) is InboxAuthorityStage.LOCAL


def test_local_writes_sub_only(db_session):
    """The default must not touch the composed modules at all."""
    conversation = _open(db_session)
    assert isinstance(conversation, InboxConversation)
    assert db_session.query(ModuleConversation).count() == 0


def test_local_stage_properties_are_coherent():
    local = InboxAuthorityStage.LOCAL
    assert not local.writes_modules
    assert not local.modules_are_authoritative
    assert local.writes_sub_tables


def test_module_stage_stops_writing_sub_owned_columns():
    """The property that makes the reconciler the only projection writer."""
    module = InboxAuthorityStage.MODULE
    assert module.writes_modules
    assert module.modules_are_authoritative
    assert not module.writes_sub_tables


# --------------------------------------------------------------------------
# identity parity — the load-bearing claim
# --------------------------------------------------------------------------


def test_shadow_keeps_one_id_across_both_sides(db_session, monkeypatch, team_id):
    """Sub's row must adopt the id the module minted, not mint its own.

    This is the claim the whole comparator rests on. If it ever regresses, every
    comparison reports the entire inbox as simultaneously missing and orphaned,
    and the failure looks like a comparator bug rather than an identity bug.
    """
    monkeypatch.setattr(
        "app.services.inbox_authority.shadow_writes_enabled", lambda db: True
    )
    assert resolve_stage(db_session) is InboxAuthorityStage.SHADOW

    conversation = _open(db_session, team=team_id)
    module_rows = _module_conversations(db_session)
    assert len(module_rows) == 1
    assert module_rows[0].id == conversation.id


def test_shadow_message_ids_match_too(db_session, monkeypatch, team_id):
    monkeypatch.setattr(
        "app.services.inbox_authority.shadow_writes_enabled", lambda db: True
    )
    conversation = _open(db_session, team=team_id)
    message = inbox_writes.record_message(
        db_session,
        conversation=conversation,
        channel=InboxChannelType.email.value,
        direction="inbound",
        occurred_at=datetime.now(UTC),
        external_message_id="<m1@example.com>",
        body="hello",
    )
    module_messages = list(db_session.scalars(ModuleMessage.__table__.select()))
    assert len(module_messages) == 1
    assert module_messages[0].id == message.id


def test_shadow_swallows_a_module_failure_and_still_ingests(
    db_session, monkeypatch, team_id
):
    """Losing an inbound message to a shadow bug is the outcome to avoid."""
    monkeypatch.setattr(
        "app.services.inbox_authority.shadow_writes_enabled", lambda db: True
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("shadow path is broken")

    monkeypatch.setattr(
        "app.services.inbox_module.conversations.open_conversation", _explode
    )
    conversation = _open(db_session, team=team_id)
    assert conversation.id is not None
    assert db_session.query(ModuleConversation).count() == 0


# --------------------------------------------------------------------------
# missing contact — reachable, and refused at MODULE
# --------------------------------------------------------------------------


def test_shadow_skips_a_contactless_conversation_without_failing(
    db_session, monkeypatch, team_id
):
    monkeypatch.setattr(
        "app.services.inbox_authority.shadow_writes_enabled", lambda db: True
    )
    conversation = _open(db_session, contact=None, team=team_id)
    assert conversation.id is not None
    assert db_session.query(ModuleConversation).count() == 0


def test_module_refuses_a_contactless_conversation(db_session, monkeypatch, team_id):
    """A widget session with no visitor identity cannot be threaded.

    The refusal is the point. An empty contact would put every anonymous
    visitor in one thread; a synthesised one would make each unmergeable.
    """
    monkeypatch.setattr(
        "app.services.inbox_authority.resolve_stage",
        lambda db: InboxAuthorityStage.MODULE,
    )
    monkeypatch.setattr(
        "app.services.inbox_writes.resolve_stage",
        lambda db: InboxAuthorityStage.MODULE,
    )
    with pytest.raises(inbox_writes.MissingConversationContact):
        _open(db_session, contact=None, team=team_id)
    assert db_session.query(InboxConversation).count() == 0


def test_module_refuses_a_contactless_message(db_session, monkeypatch, team_id):
    conversation = _open(db_session, contact=None, team=team_id)
    monkeypatch.setattr(
        "app.services.inbox_writes.resolve_stage",
        lambda db: InboxAuthorityStage.MODULE,
    )
    with pytest.raises(inbox_writes.MissingConversationContact):
        inbox_writes.record_message(
            db_session,
            conversation=conversation,
            channel=InboxChannelType.email.value,
            direction="inbound",
            occurred_at=datetime.now(UTC),
            body="orphan",
        )


# --------------------------------------------------------------------------
# comparator symmetry — the bug this suite exists to pin
# --------------------------------------------------------------------------


def test_comparator_reports_a_sub_only_conversation(db_session, team_id):
    _open(db_session, team=team_id)
    report = compare(db_session)
    assert [entity for entity, _ in report.orphan_projection] == ["conversation"]
    assert not report.clean


def test_comparator_reports_a_sub_only_message(db_session, team_id):
    """The asymmetry that let a local message escape both guards.

    A message written straight into `public.inbox_messages` disagrees with
    nothing, because the module never heard of it. Column comparison alone
    reports a clean inbox; only the orphan direction sees it.
    """
    conversation = _open(db_session, team=team_id)
    db_session.add(
        InboxMessage(
            conversation_id=conversation.id,
            channel_type=InboxChannelType.email.value,
            direction="inbound",
            body="written straight into Sub",
        )
    )
    db_session.flush()

    report = compare(db_session)
    orphan_entities = sorted(entity for entity, _ in report.orphan_projection)
    assert "message" in orphan_entities
    assert not report.clean


def test_reconcile_is_idempotent_over_an_empty_module(db_session, team_id):
    _open(db_session, team=team_id)
    assert reconcile(db_session) == 0
    assert reconcile(db_session) == 0


# --------------------------------------------------------------------------
# the activation gate
# --------------------------------------------------------------------------


def _command(**overrides):
    base = {
        "activated_by": "michael",
        "review_reference": "CAB-2026-08-23 priority ordering accepted",
        "command_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
    }
    base.update(overrides)
    return ActivateInboxAuthority(**base)


def test_activation_refuses_with_no_review_reference(db_session):
    with pytest.raises(InboxAuthorityActivationRefused, match="review reference"):
        activate(db_session, _command(review_reference="   "))
    assert cutover_record(db_session) is None


def test_activation_refuses_over_an_empty_comparison(db_session):
    """A clean verdict over zero rows is what an unrun backfill looks like."""
    with pytest.raises(InboxAuthorityActivationRefused, match="nothing to compare"):
        activate(db_session, _command())
    assert cutover_record(db_session) is None


def test_activation_refuses_over_known_drift(db_session, team_id):
    _open(db_session, team=team_id)  # Sub-only: an orphan the comparator sees
    with pytest.raises(InboxAuthorityActivationRefused, match="not clean"):
        activate(db_session, _command())
    assert cutover_record(db_session) is None


def test_activation_refuses_twice(db_session, monkeypatch):
    """There is no second cutover, and no idempotent re-activation."""

    class _Clean:
        conversations_compared = 3
        messages_compared = 5
        missing_projection: list = []
        orphan_projection: list = []
        drift: list = []
        clean = True

        def summary(self):
            return "clean"

    monkeypatch.setattr(
        "app.services.inbox_projection_reconciler.compare", lambda db: _Clean()
    )
    first = activate(db_session, _command())
    assert first.conversations_verified == 3
    assert first.messages_verified == 5

    with pytest.raises(InboxAuthorityActivationRefused, match="already moved"):
        activate(db_session, _command())


def test_activation_makes_the_stage_module(db_session, monkeypatch):
    class _Clean:
        conversations_compared = 1
        messages_compared = 1
        missing_projection: list = []
        orphan_projection: list = []
        drift: list = []
        clean = True

        def summary(self):
            return "clean"

    monkeypatch.setattr(
        "app.services.inbox_projection_reconciler.compare", lambda db: _Clean()
    )
    assert resolve_stage(db_session) is InboxAuthorityStage.LOCAL
    activate(db_session, _command())
    assert resolve_stage(db_session) is InboxAuthorityStage.MODULE


def test_the_fingerprint_ignores_row_ids_but_not_counts():
    """It must survive an inbox that keeps living, and change when parity does."""

    class _Report:
        def __init__(self, conversations, messages, drift):
            self.conversations_compared = conversations
            self.messages_compared = messages
            self.missing_projection = []
            self.orphan_projection = []
            self.drift = drift

    a = drift_fingerprint(_Report(10, 20, []))
    assert a == drift_fingerprint(_Report(10, 20, []))
    assert a != drift_fingerprint(_Report(11, 20, []))
    assert a != drift_fingerprint(_Report(10, 20, ["something"]))


# --------------------------------------------------------------------------
# transaction semantics
# --------------------------------------------------------------------------


def test_module_failure_leaves_no_sub_row(db_session, monkeypatch, team_id):
    """At MODULE the module's refusal IS the answer; Sub must not keep the row.

    The seam does not catch at MODULE, so the exception reaches the caller with
    the flush not yet committed. Rolling back is the caller's job — this asserts
    the seam gives it something to roll back rather than having quietly
    committed a Sub-only row on its way out.
    """
    monkeypatch.setattr(
        "app.services.inbox_writes.resolve_stage",
        lambda db: InboxAuthorityStage.MODULE,
    )

    def _explode(*args, **kwargs):
        raise RuntimeError("module refused")

    monkeypatch.setattr(
        "app.services.inbox_module.conversations.open_conversation", _explode
    )
    with pytest.raises(RuntimeError, match="module refused"):
        _open(db_session, team=team_id)

    db_session.rollback()
    assert db_session.query(InboxConversation).count() == 0


def test_an_unresolvable_account_scope_is_refused_at_module(db_session, monkeypatch):
    """No route, no provider scope, external channel — the ladder's fourth rung."""
    from app.services.inbox_account_scope import AccountScopeUnresolved

    monkeypatch.setattr(
        "app.services.inbox_writes.resolve_stage",
        lambda db: InboxAuthorityStage.MODULE,
    )
    with pytest.raises(AccountScopeUnresolved):
        inbox_writes.open_conversation(
            db_session,
            channel=InboxChannelType.email.value,
            contact=CONTACT,
            external_thread_id=None,
            subject="No route anywhere",
            occurred_at=datetime.now(UTC),
            service_team_id=uuid.uuid4(),
        )


def test_two_active_routes_make_the_scope_unresolvable(db_session, monkeypatch):
    """ "Single active route" is load-bearing: guessing merges two teams' threads."""
    from app.services.inbox_account_scope import resolve_account_scope

    team = uuid.uuid4()
    for scope in ("one@dotmac.ng", "two@dotmac.ng"):
        db_session.add(
            TeamInboxChannelRoute(
                service_team_id=team,
                channel_type=InboxChannelType.email.value,
                provider="smtp",
                account_scope=scope,
                is_active=True,
            )
        )
    db_session.flush()

    assert (
        resolve_account_scope(
            channel=InboxChannelType.email.value,
            service_team_id=team,
            db=db_session,
        )
        is None
    )


def test_snoozed_transition_carries_its_wake_time(db_session, team_id):
    """Status and wake time are one fact; the seam must not split them."""
    conversation = _open(db_session, team=team_id)
    target = datetime.now(UTC) + timedelta(hours=2)
    inbox_writes.set_status(
        db_session,
        conversation=conversation,
        status=InboxConversationStatus.snoozed.value,
        occurred_at=datetime.now(UTC),
        snoozed_until=target,
    )
    assert conversation.status == InboxConversationStatus.snoozed.value
    assert conversation.snoozed_until == target
