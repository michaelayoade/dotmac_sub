from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.party import PartyIdentityStatus, PartyRole, PartyType
from app.models.subscriber import Subscriber
from app.services import party as party_service


def _subscriber(email: str) -> Subscriber:
    return Subscriber(
        first_name="Ada",
        last_name="Okafor",
        email=email,
    )


def test_one_party_can_own_multiple_subscriber_accounts_without_implicit_role(
    db_session,
):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Ada Okafor",
    )
    first = _subscriber("ada-one@realbusiness.ng")
    second = _subscriber("ada-two@realbusiness.ng")
    db_session.add_all((first, second))
    db_session.flush()

    for account in (first, second):
        party_service.bind_subscriber_account(
            db_session,
            subscriber_id=account.id,
            party_id=identity.id,
            source="reviewed_identity_worklist",
            reason="Operator confirmed both accounts belong to this person",
        )

    assert first.party_id == identity.id
    assert second.party_id == identity.id
    assert first.party_bound_at is not None
    assert first.party_binding_source == "reviewed_identity_worklist"
    assert {account.id for account in identity.subscriber_accounts} == {
        first.id,
        second.id,
    }
    assert db_session.query(PartyRole).count() == 0


def test_exact_binding_retry_is_idempotent_and_rebind_is_refused(db_session):
    first_identity = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Ada Okafor",
    )
    second_identity = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Another Ada",
    )
    account = _subscriber("ada@realbusiness.ng")
    db_session.add(account)
    db_session.flush()

    bound = party_service.bind_subscriber_account(
        db_session,
        subscriber_id=account.id,
        party_id=first_identity.id,
        source="reviewed_identity_worklist",
        reason="Initial reviewed binding",
    )
    initial_bound_at = bound.party_bound_at
    retried = party_service.bind_subscriber_account(
        db_session,
        subscriber_id=account.id,
        party_id=first_identity.id,
        source="retry",
        reason="Idempotent retry",
    )

    assert retried is bound
    assert retried.party_bound_at == initial_bound_at
    assert retried.party_binding_source == "reviewed_identity_worklist"
    assert retried.party_binding_reason == "Initial reviewed binding"

    with pytest.raises(
        party_service.PartyInvariantError,
        match="reviewed merge/repoint workflow",
    ):
        party_service.bind_subscriber_account(
            db_session,
            subscriber_id=account.id,
            party_id=second_identity.id,
            source="manual_override",
            reason="Attempted unreviewed target change",
        )


def test_quarantined_party_can_preserve_binding_but_archived_party_cannot(db_session):
    quarantined = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Unresolved Candidate",
    )
    party_service.quarantine_party(
        db_session,
        party_id=quarantined.id,
        reason="Identity evidence requires review",
    )
    unresolved_account = _subscriber("unresolved@realbusiness.ng")
    db_session.add(unresolved_account)
    db_session.flush()

    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=unresolved_account.id,
        party_id=quarantined.id,
        source="identity_quarantine_review",
        reason="Preserve unresolved account provenance without activation",
    )

    assert unresolved_account.party_id == quarantined.id
    assert quarantined.status == PartyIdentityStatus.quarantined.value

    archived = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Archived Identity",
    )
    archived.status = PartyIdentityStatus.archived.value
    other_account = _subscriber("archived@realbusiness.ng")
    db_session.add(other_account)
    db_session.flush()

    with pytest.raises(party_service.PartyInvariantError, match="cannot own"):
        party_service.bind_subscriber_account(
            db_session,
            subscriber_id=other_account.id,
            party_id=archived.id,
            source="reviewed_identity_worklist",
            reason="Invalid archived target",
        )


def test_database_rejects_party_binding_without_provenance(db_session):
    identity = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Ada Okafor",
    )
    account = _subscriber("missing-evidence@realbusiness.ng")
    db_session.add(account)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            account.party_id = identity.id
            account.party_bound_at = datetime.now(UTC)
            db_session.flush()
