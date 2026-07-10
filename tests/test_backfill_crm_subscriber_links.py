import json
from datetime import UTC, datetime

from scripts.migration.backfill_crm_subscriber_links import (
    BackfillPlan,
    CrmLinkRow,
    SubLinkRow,
    build_plan,
    choose_crm_link_row,
)

T1 = datetime(2026, 1, 1, tzinfo=UTC)
T2 = datetime(2026, 2, 1, tzinfo=UTC)
T3 = datetime(2026, 3, 1, tzinfo=UTC)


def _crm(
    crm_id: str,
    external_id: str,
    person_id: str | None = None,
    updated_at: datetime = T1,
    is_active: bool = True,
) -> CrmLinkRow:
    return CrmLinkRow(
        id=crm_id,
        external_id=external_id,
        person_id=person_id,
        is_active=is_active,
        updated_at=updated_at,
    )


def _sub(
    sub_id: str,
    crm_subscriber_id: str | None = None,
    metadata: dict | None = None,
    created_at: datetime = T1,
) -> SubLinkRow:
    return SubLinkRow(
        id=sub_id,
        crm_subscriber_id=crm_subscriber_id,
        metadata_text=json.dumps(metadata) if metadata is not None else None,
        created_at=created_at,
    )


def _apply_plan(rows: list[SubLinkRow], plan: BackfillPlan) -> list[SubLinkRow]:
    updates = {update.subscriber_id: update for update in plan.updates}
    return [
        SubLinkRow(
            id=row.id,
            crm_subscriber_id=(
                updates[row.id].crm_subscriber_id
                if row.id in updates
                else row.crm_subscriber_id
            ),
            metadata_text=(
                updates[row.id].metadata_json
                if row.id in updates
                else row.metadata_text
            ),
            created_at=row.created_at,
        )
        for row in rows
    ]


def test_choose_prefers_row_already_referenced_by_sub() -> None:
    rows = [
        _crm("crm-old", "sub-a", person_id="person-1", updated_at=T3),
        _crm("crm-referenced", "sub-a", updated_at=T1),
    ]

    winner, losers = choose_crm_link_row(rows, referenced_crm_ids={"crm-referenced"})

    assert winner.id == "crm-referenced"
    assert [row.id for row in losers] == ["crm-old"]


def test_choose_prefers_person_then_newest_updated_at() -> None:
    rows = [
        _crm("crm-newest", "sub-a", updated_at=T3),
        _crm("crm-person", "sub-a", person_id="person-1", updated_at=T1),
    ]

    winner, _ = choose_crm_link_row(rows, referenced_crm_ids=set())
    assert winner.id == "crm-person"

    rows = [
        _crm("crm-older", "sub-a", updated_at=T1),
        _crm("crm-newest", "sub-a", updated_at=T3),
    ]
    winner, _ = choose_crm_link_row(rows, referenced_crm_ids=set())
    assert winner.id == "crm-newest"


def test_plan_links_unlinked_subscriber_and_sets_person() -> None:
    plan = build_plan(
        [_sub("sub-a", metadata={"existing": "kept"})],
        [_crm("crm-1", "sub-a", person_id="person-1")],
    )

    assert plan.stats.linked == 1
    assert plan.stats.person_linked == 1
    assert len(plan.updates) == 1
    update = plan.updates[0]
    assert update.crm_subscriber_id == "crm-1"
    merged = json.loads(update.metadata_json or "{}")
    assert merged["existing"] == "kept"
    assert merged["crm_person_id"] == "person-1"


def test_plan_repoints_asymmetric_pair_and_keeps_old_as_alias() -> None:
    plan = build_plan(
        [_sub("sub-a", crm_subscriber_id="crm-stale")],
        [_crm("crm-1", "sub-a")],
    )

    assert plan.stats.repointed == 1
    update = plan.updates[0]
    assert update.crm_subscriber_id == "crm-1"
    merged = json.loads(update.metadata_json or "{}")
    assert merged["crm_alias_ids"] == ["crm-stale"]


def test_plan_records_duplicate_losers_as_aliases_with_dedup() -> None:
    plan = build_plan(
        [
            _sub(
                "sub-a",
                crm_subscriber_id="crm-winner",
                metadata={"crm_alias_ids": ["crm-loser-1"]},
            )
        ],
        [
            _crm("crm-winner", "sub-a", updated_at=T1),
            _crm("crm-loser-1", "sub-a", updated_at=T2),
            _crm("crm-loser-2", "sub-a", updated_at=T3),
        ],
    )

    # crm-winner wins because sub already references it.
    assert plan.stats.crm_duplicate_external_ids == 1
    assert plan.stats.repointed == 0
    assert plan.stats.alias_recorded == 1
    merged = json.loads(plan.updates[0].metadata_json or "{}")
    assert merged["crm_alias_ids"] == ["crm-loser-1", "crm-loser-2"]


def test_plan_reports_dangling_link_untouched() -> None:
    plan = build_plan(
        [_sub("sub-a", crm_subscriber_id="crm-gone")],
        [],
    )

    assert plan.stats.dangling == 1
    assert plan.updates == []
    assert plan.reports["dangling"] == [
        {"subscriber_id": "sub-a", "crm_subscriber_id": "crm-gone"}
    ]


def test_plan_never_overwrites_existing_person_id() -> None:
    plan = build_plan(
        [
            _sub(
                "sub-a",
                crm_subscriber_id="crm-1",
                metadata={"crm_person_id": "person-existing"},
            )
        ],
        [_crm("crm-1", "sub-a", person_id="person-other")],
    )

    assert plan.stats.person_mismatch == 1
    assert plan.updates == []


def test_plan_collision_current_holder_wins() -> None:
    # sub-b already holds crm-1 but no selfcare row points back at sub-b
    # (dangling); sub-a wants crm-1 but must not violate the unique index.
    plan = build_plan(
        [
            _sub("sub-a", created_at=T1),
            _sub("sub-b", crm_subscriber_id="crm-1", created_at=T2),
        ],
        [_crm("crm-1", "sub-a")],
    )

    assert plan.stats.collision == 1
    assert plan.stats.dangling == 1
    assert plan.updates == []
    assert plan.reports["collision"] == [
        {
            "subscriber_id": "sub-a",
            "current_crm_subscriber_id": None,
            "wanted_crm_subscriber_id": "crm-1",
            "holding_subscriber_id": "sub-b",
        }
    ]


def test_plan_repoint_frees_id_for_waiting_subscriber() -> None:
    # sub-b holds crm-1, but crm-1 belongs to sub-a and crm-2 belongs to
    # sub-b: the repoint of sub-b frees crm-1 so sub-a can take it.
    plan = build_plan(
        [
            _sub("sub-a", created_at=T1),
            _sub("sub-b", crm_subscriber_id="crm-1", created_at=T2),
        ],
        [_crm("crm-1", "sub-a"), _crm("crm-2", "sub-b")],
    )

    assert plan.stats.collision == 0
    assert plan.stats.linked == 1
    assert plan.stats.repointed == 1
    by_id = {update.subscriber_id: update for update in plan.updates}
    assert by_id["sub-a"].crm_subscriber_id == "crm-1"
    assert by_id["sub-b"].crm_subscriber_id == "crm-2"


def test_plan_is_idempotent_after_apply() -> None:
    sub_rows = [
        _sub("sub-a", metadata={"existing": "kept"}, created_at=T1),
        _sub("sub-b", crm_subscriber_id="crm-stale", created_at=T2),
        _sub("sub-c", crm_subscriber_id="crm-c", created_at=T3),
    ]
    crm_rows = [
        _crm("crm-a", "sub-a", person_id="person-a"),
        _crm("crm-b", "sub-b"),
        _crm("crm-c", "sub-c", updated_at=T1),
        _crm("crm-c-dup", "sub-c", updated_at=T3),
    ]

    first = build_plan(sub_rows, crm_rows)
    assert first.stats.updates_planned > 0

    second = build_plan(_apply_plan(sub_rows, first), crm_rows)

    assert second.stats.updates_planned == 0
    assert second.stats.linked == 0
    assert second.stats.repointed == 0
    assert second.stats.alias_recorded == 0
    assert second.stats.person_linked == 0
    assert second.stats.collision == 0
