from __future__ import annotations

from app.models.party import PartyRoleStatus, PartyRoleType, PartyType
from app.models.subscriber import Subscriber
from app.services import party
from scripts.migration.audit_party_foundation import build_party_foundation_audit


def test_party_foundation_audit_reports_population_and_contracts(db_session):
    organization = party.create_party(
        db_session,
        party_type=PartyType.organization,
        display_name="ABC Networks Ltd",
    )
    party.assign_role(
        db_session,
        party_id=organization.id,
        role_type=PartyRoleType.reseller,
        status=PartyRoleStatus.active,
    )
    account = Subscriber(
        first_name="ABC",
        last_name="Account",
        email="billing@abcnetworks.ng",
    )
    db_session.add(account)
    db_session.flush()
    party.bind_subscriber_account(
        db_session,
        subscriber_id=account.id,
        party_id=organization.id,
        source="test_review",
        reason="Reviewed business account identity",
    )

    audit = build_party_foundation_audit(db_session)

    assert audit["status"] == "installed"
    assert audit["parties"]["by_type"] == {"organization": 1}
    assert audit["roles"]["by_type"] == {"reseller": 1}
    assert audit["subscriber_binding"] == {
        "status": "installed",
        "total_accounts": 1,
        "bound_accounts": 1,
        "unbound_accounts": 0,
        "parties_with_accounts": 1,
    }
    assert audit["backfill_receipts"] == {
        "status": "installed",
        "total": 0,
        "planned_parties": 0,
        "bindings": 0,
    }
    assert audit["role_contracts"]["reseller"]["implicit_permissions"] == []
    assert set(audit["role_contracts"]["partners"]) == {
        "referral",
        "technology",
        "infrastructure",
        "strategic",
    }
