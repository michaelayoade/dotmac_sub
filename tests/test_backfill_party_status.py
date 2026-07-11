"""Phase 3 PR 1: organizations/party schema + party-backfill decision logic."""

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from scripts.migration.backfill_party_status import (
    INSERT_SUBSCRIBER_SQL,
    BackfillPlan,
    CrmPersonRow,
    IdentityIndexRow,
    SubPartyRow,
    build_plan,
    choose_subscriber,
    normalize_phone,
    resolve_default_reseller_id,
)

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
T3 = datetime(2026, 3, 1, tzinfo=UTC)


def _person(
    person_id: str,
    *,
    email: str | None = "person@example.com",
    phone: str | None = None,
    party_status: str | None = "lead",
    is_active: bool = True,
    selfcare_id: str | None = None,
    sources: tuple[str, ...] = ("lead",),
) -> CrmPersonRow:
    return CrmPersonRow(
        id=person_id,
        first_name="Ada",
        last_name="Obi",
        display_name=None,
        email=email,
        phone=phone,
        party_status=party_status,
        is_active=is_active,
        selfcare_id=selfcare_id,
        sources=sources,
    )


def _sub(
    sub_id: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    party_status: str | None = None,
    metadata: dict | None = None,
    created_at: datetime = T1,
    is_active: bool = True,
) -> SubPartyRow:
    return SubPartyRow(
        id=sub_id,
        email=email,
        phone=phone,
        party_status=party_status,
        metadata_text=json.dumps(metadata) if metadata is not None else None,
        created_at=created_at,
        is_active=is_active,
    )


def _apply_plan(rows: list[SubPartyRow], plan: BackfillPlan) -> list[SubPartyRow]:
    """Simulate applying the plan, for idempotency checks."""
    updates = {update.subscriber_id: update for update in plan.updates}
    result = [
        SubPartyRow(
            id=row.id,
            email=row.email,
            phone=row.phone,
            party_status=(
                updates[row.id].party_status if row.id in updates else row.party_status
            ),
            metadata_text=(
                updates[row.id].metadata_json
                if row.id in updates
                else row.metadata_text
            ),
            created_at=row.created_at,
            is_active=row.is_active,
        )
        for row in rows
    ]
    result.extend(
        SubPartyRow(
            id=insert.subscriber_id,
            email=insert.email,
            phone=insert.phone,
            party_status=insert.party_status,
            metadata_text=insert.metadata_json,
            created_at=T3,
            is_active=insert.is_active,
        )
        for insert in plan.inserts
    )
    return result


# ---------------------------------------------------------------------------
# Resolution cascade
# ---------------------------------------------------------------------------


def test_resolves_via_crm_person_id_metadata_first() -> None:
    person = _person("p1", email="shared@example.com")
    rows = [
        _sub("s-linked", metadata={"crm_person_id": "p1"}),
        _sub("s-email", email="shared@example.com"),
    ]

    plan = build_plan([person], rows)

    assert plan.inserts == []
    assert plan.stats.resolved_crm_person_id == 1
    assert plan.person_map == [
        {
            "crm_person_id": "p1",
            "subscriber_id": "s-linked",
            "resolution": "crm_person_id",
            "sources": "lead",
        }
    ]


def test_resolves_via_selfcare_id_before_email() -> None:
    person = _person("p1", email="shared@example.com", selfcare_id="s-mine")
    rows = [
        _sub("s-mine"),
        _sub("s-email", email="shared@example.com"),
    ]

    plan = build_plan([person], rows)

    assert plan.stats.resolved_selfcare_id == 1
    assert plan.person_map[0]["subscriber_id"] == "s-mine"
    assert plan.person_map[0]["resolution"] == "selfcare_id"


def test_resolves_via_email_cascade_including_identity_index() -> None:
    person = _person("p1", email="Family@Example.com")
    rows = [_sub("s1")]
    identity = [IdentityIndexRow("email", "family@example.com", "s1")]

    plan = build_plan([person], rows, identity)

    assert plan.stats.resolved_email == 1
    assert plan.person_map[0]["subscriber_id"] == "s1"


def test_resolves_via_normalized_phone() -> None:
    person = _person("p1", email="nomatch@example.com", phone="0803 555 1234")
    rows = [_sub("s1", phone="+2348035551234")]

    plan = build_plan([person], rows)

    assert plan.stats.resolved_phone == 1
    assert plan.person_map[0]["resolution"] == "phone"


def test_phone_normalization_variants() -> None:
    assert normalize_phone("0803 555 1234") == "+2348035551234"
    assert normalize_phone("+234 803 555 1234") == "+2348035551234"
    assert normalize_phone("002348035551234") == "+2348035551234"
    assert normalize_phone("whatsapp:+2348035551234") == "+2348035551234"
    assert normalize_phone(None) is None


def test_ambiguous_email_match_is_deterministic_and_reported() -> None:
    person = _person("p1", email="shared@example.com")
    rows = [
        _sub("s-newer", email="shared@example.com", created_at=T2),
        _sub("s-older", email="shared@example.com", created_at=T1),
        _sub("s-inactive", email="shared@example.com", created_at=T1, is_active=False),
    ]

    plan = build_plan([person], rows)

    # Active first, then earliest created_at.
    assert plan.person_map[0]["subscriber_id"] == "s-older"
    assert plan.stats.ambiguous == 1
    report = plan.reports["ambiguous"][0]
    assert report["chosen_subscriber_id"] == "s-older"
    assert "s-newer" in report["other_subscriber_ids"]


def test_choose_subscriber_tie_breaks_on_id() -> None:
    rows = [
        _sub("s-b", created_at=T1),
        _sub("s-a", created_at=T1),
    ]
    winner, losers = choose_subscriber(rows)
    assert winner.id == "s-a"
    assert [row.id for row in losers] == ["s-b"]


# ---------------------------------------------------------------------------
# Creation of prospect subscribers
# ---------------------------------------------------------------------------


def test_unresolved_person_becomes_new_subscriber_row() -> None:
    person = _person(
        "p1",
        email="prospect@example.com",
        phone="0803 000 0000",
        party_status="lead",
        sources=("lead", "quote"),
    )

    plan = build_plan([person], [])

    assert plan.stats.created == 1
    insert = plan.inserts[0]
    assert insert.email == "prospect@example.com"
    assert insert.party_status == "lead"
    assert insert.is_active is True
    assert json.loads(insert.metadata_json) == {"crm_person_id": "p1"}
    uuid.UUID(insert.subscriber_id)  # valid uuid
    created = plan.reports["created"][0]
    assert created["sources"] == "lead;quote"
    assert plan.person_map[0]["resolution"] == "created"


# ---------------------------------------------------------------------------
# reseller_id resolution (subscribers.reseller_id is NOT NULL, migration 116)
# ---------------------------------------------------------------------------


def test_resolve_default_reseller_prefers_house_row() -> None:
    # DEFAULT_RESELLER_SQL orders house-first then created_at, so the first row
    # is the House reseller — mirroring subscriber.py::_default_reseller_id.
    rows = [
        {"id": "house-id", "is_house": True},
        {"id": "other-id", "is_house": False},
    ]
    assert resolve_default_reseller_id(rows) == "house-id"


def test_resolve_default_reseller_falls_back_to_first_row() -> None:
    # No house row: the query already ordered by created_at, so the first row
    # is the earliest-created reseller (the fallback in the app helper).
    rows = [{"id": "earliest-id", "is_house": False}]
    assert resolve_default_reseller_id(rows) == "earliest-id"


def test_resolve_default_reseller_override_wins_and_normalizes() -> None:
    rows = [{"id": "house-id", "is_house": True}]
    assert (
        resolve_default_reseller_id(rows, "F02AAE97-0000-0000-0000-000000000000")
        == "f02aae97-0000-0000-0000-000000000000"
    )


def test_resolve_default_reseller_none_when_no_resellers() -> None:
    assert resolve_default_reseller_id([], None) is None


def test_insert_sql_sets_reseller_id() -> None:
    assert "reseller_id" in INSERT_SUBSCRIBER_SQL
    assert "CAST(:reseller_id AS uuid)" in INSERT_SUBSCRIBER_SQL


def test_prospect_insert_carries_resolved_reseller_id() -> None:
    person = _person("p1", email="prospect@example.com")

    plan = build_plan([person], [], default_reseller_id="house-id")

    assert plan.inserts[0].reseller_id == "house-id"
    assert plan.reports["created"][0]["reseller_id"] == "house-id"


def test_created_row_party_status_defaults_to_lead() -> None:
    person = _person("p1", party_status=None)

    plan = build_plan([person], [])

    assert plan.inserts[0].party_status == "lead"


def test_inactive_person_creates_inactive_subscriber() -> None:
    person = _person("p1", is_active=False, party_status="contact")

    plan = build_plan([person], [])

    assert plan.inserts[0].is_active is False
    assert plan.inserts[0].party_status == "contact"


def test_shared_email_prospects_each_get_their_own_row() -> None:
    # Email is non-unique in sub (doc 02 §3.2): two unresolved prospects
    # sharing an email must both import.
    people = [
        _person("p1", email="family@example.com"),
        _person("p2", email="family@example.com"),
    ]

    plan = build_plan(people, [])

    assert plan.stats.created == 2
    assert len({insert.subscriber_id for insert in plan.inserts}) == 2


# ---------------------------------------------------------------------------
# Stamping existing subscribers
# ---------------------------------------------------------------------------


def test_stamps_party_status_where_null_and_records_link() -> None:
    person = _person("p1", party_status="customer")
    rows = [_sub("s1", email="person@example.com", metadata={})]

    plan = build_plan([person], rows)

    assert plan.stats.party_status_stamped == 1
    assert plan.stats.person_link_recorded == 1
    update = plan.updates[0]
    assert update.subscriber_id == "s1"
    assert update.party_status == "customer"
    assert json.loads(update.metadata_json) == {"crm_person_id": "p1"}


def test_existing_party_status_never_overwritten() -> None:
    person = _person("p1", party_status="lead")
    rows = [
        _sub(
            "s1",
            party_status="subscriber",
            metadata={"crm_person_id": "p1"},
        )
    ]

    plan = build_plan([person], rows)

    assert plan.updates == []
    assert plan.stats.party_status_mismatch == 1
    report = plan.reports["party_status_mismatch"][0]
    assert report["existing_party_status"] == "subscriber"
    assert report["crm_party_status"] == "lead"


def test_differing_crm_person_id_reported_not_overwritten() -> None:
    person = _person("p1", selfcare_id="s1", party_status="customer")
    rows = [_sub("s1", party_status="customer", metadata={"crm_person_id": "p-other"})]

    plan = build_plan([person], rows)

    assert plan.stats.person_mismatch == 1
    assert plan.updates == []
    metadata = json.loads(rows[0].metadata_text)
    assert metadata["crm_person_id"] == "p-other"


def test_two_persons_sharing_one_subscriber_first_writer_wins() -> None:
    people = [
        _person("p1", email="family@example.com", party_status="customer"),
        _person("p2", email="family@example.com", party_status="lead"),
    ]
    rows = [_sub("s1", email="family@example.com", metadata={})]

    plan = build_plan(people, rows)

    # p1 (ordered first) stamps; p2 sees the in-plan state and only reports.
    assert plan.stats.party_status_stamped == 1
    assert plan.stats.party_status_mismatch == 1
    assert plan.stats.person_mismatch == 1
    assert len(plan.updates) == 1
    assert plan.updates[0].party_status == "customer"
    assert json.loads(plan.updates[0].metadata_json) == {"crm_person_id": "p1"}


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_rerun_after_apply_plans_no_work() -> None:
    people = [
        _person("p1", email="prospect@example.com", party_status="lead"),
        _person("p2", email="existing@example.com", party_status="customer"),
        _person("p3", selfcare_id="s-known", party_status="subscriber"),
    ]
    rows = [
        _sub("s-existing", email="existing@example.com", metadata={}),
        _sub("s-known", party_status="subscriber", metadata={"crm_person_id": "p3"}),
    ]

    first = build_plan(people, rows)
    assert first.stats.created == 1
    assert first.stats.updates_planned == 1

    second = build_plan(people, _apply_plan(rows, first))

    assert second.inserts == []
    assert second.updates == []
    assert second.stats.resolved_crm_person_id == 3
    assert second.stats.created == 0


def test_person_map_covers_every_person() -> None:
    people = [
        _person("p1", email="prospect@example.com"),
        _person("p2", selfcare_id="s1"),
    ]
    rows = [_sub("s1")]

    plan = build_plan(people, rows)

    assert {entry["crm_person_id"] for entry in plan.person_map} == {"p1", "p2"}


# ---------------------------------------------------------------------------
# Schema: model registration + migration
# ---------------------------------------------------------------------------


def test_organizations_models_registered() -> None:
    from app.db import Base
    from app.models import (
        Organization,
        OrganizationMembership,
        PartyStatus,
    )

    assert Organization.__tablename__ == "organizations"
    assert OrganizationMembership.__tablename__ == "organization_memberships"
    assert "organizations" in Base.metadata.tables
    assert "organization_memberships" in Base.metadata.tables
    assert [status.value for status in PartyStatus] == [
        "lead",
        "contact",
        "customer",
        "subscriber",
    ]

    organizations = Base.metadata.tables["organizations"]
    # Person FKs carried as plain UUIDs (no people table in sub).
    assert not list(organizations.columns["primary_contact_id"].foreign_keys)
    assert not list(organizations.columns["owner_id"].foreign_keys)
    # Hierarchy self-FK stays real.
    parent_fks = list(organizations.columns["parent_id"].foreign_keys)
    assert parent_fks and parent_fks[0].column.table.name == "organizations"

    memberships = Base.metadata.tables["organization_memberships"]
    assert not list(memberships.columns["person_id"].foreign_keys)
    uq_names = {
        constraint.name
        for constraint in memberships.constraints
        if constraint.name is not None
    }
    assert "uq_organization_memberships_org_person" in uq_names


def test_subscriber_party_columns_registered() -> None:
    from app.db import Base

    subscribers = Base.metadata.tables["subscribers"]
    assert subscribers.columns["party_status"].type.length == 20
    org_fks = list(subscribers.columns["organization_id"].foreign_keys)
    assert org_fks and org_fks[0].column.table.name == "organizations"
    # Real FK since the expand-B migration (244) landed sales_orders.
    so_fks = list(subscribers.columns["sales_order_id"].foreign_keys)
    assert so_fks and so_fks[0].column.table.name == "sales_orders"


def test_migration_243_imports_and_targets_expected_revision() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "243_phase3_organizations_party.py"
    )
    spec = importlib.util.spec_from_file_location("migration_243_phase3", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration.revision == "243_phase3_organizations_party"
    assert migration.down_revision == "242_field_note_metadata"
    assert callable(migration.upgrade)
    assert callable(migration.downgrade)
