"""Frozen migration680 adoption shapes; independent of current model metadata."""

import re
from dataclasses import dataclass

from sqlalchemy import Connection, inspect, text


@dataclass(frozen=True, slots=True)
class FollowupTableShape:
    name: str
    columns: tuple[tuple[str, str, bool], ...]
    primary_key: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    checks: tuple[str, ...]


FOLLOWUP_TABLES = tuple(
    FollowupTableShape(*value)
    for value in (
        (
            "field_visit_followups",
            (
                ("id", "uuid", True),
                ("work_order_id", "uuid", True),
                ("field_event_id", "uuid", False),
                ("rejected_decision_id", "uuid", False),
                ("current_sequence", "integer", True),
            ),
            ("id",),
            (("field_event_id",), ("rejected_decision_id",)),
            (
                (("field_event_id",), ("field_job_events.id",), "RESTRICT"),
                (
                    ("rejected_decision_id",),
                    ("completion_review_decisions.id",),
                    "RESTRICT",
                ),
                (("work_order_id",), ("work_order.id",), "RESTRICT"),
            ),
            (
                "current_sequence > 0",
                "(field_event_id IS NOT NULL) <> (rejected_decision_id IS NOT NULL)",
            ),
        ),
        (
            "field_visit_followup_revisions",
            (
                ("id", "uuid", True),
                ("obligation_id", "uuid", True),
                ("sequence", "integer", True),
                ("disposition", "character varying(24)", True),
                ("responsible_team_id", "uuid", True),
                ("responsible_system_user_id", "uuid", False),
                ("responsible_person_id", "uuid", False),
                ("due_at", "timestamp with time zone", True),
                ("reason", "text", True),
                ("replacement_decision_id", "uuid", False),
                ("actor_user_id", "uuid", True),
                ("actor_person_id", "uuid", True),
                ("decided_at", "timestamp with time zone", True),
            ),
            ("id",),
            (("obligation_id", "sequence"),),
            (
                (("actor_person_id",), ("parties.id",), "RESTRICT"),
                (("actor_user_id",), ("system_users.id",), "RESTRICT"),
                (("obligation_id",), ("field_visit_followups.id",), "RESTRICT"),
                (
                    ("replacement_decision_id",),
                    ("completion_replacement_decisions.id",),
                    "RESTRICT",
                ),
                (("responsible_person_id",), ("parties.id",), "RESTRICT"),
                (("responsible_system_user_id",), ("system_users.id",), "RESTRICT"),
                (("responsible_team_id",), ("service_teams.id",), "RESTRICT"),
            ),
            (
                "replacement_decision_id IS NULL OR disposition = 'revisit_required'",
                "sequence > 0",
                "length(trim(reason)) > 0",
                "(responsible_system_user_id IS NULL) = (responsible_person_id IS NULL)",
                "disposition IN ('triage','revisit_required','no_revisit')",
            ),
        ),
        (
            "field_visit_followup_receipts",
            (
                ("intent_key", "uuid", True),
                ("fingerprint", "character varying(64)", True),
                ("revision_id", "uuid", True),
            ),
            ("intent_key",),
            (),
            ((("revision_id",), ("field_visit_followup_revisions.id",), "RESTRICT"),),
            ("length(fingerprint) = 64",),
        ),
    )
)


def _check(value: str) -> str:
    value = value.lower().replace("both from", "")
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value)
    value = re.sub(r"([a-z_]+) in \(([^)]+)\)", r"\1=anyarray[\2]", value)
    return re.sub(r'[\s()"]', "", value)


def verify_followup_adoption(bind: Connection) -> None:
    inspector = inspect(bind)
    for expected in FOLLOWUP_TABLES:
        if not inspector.has_table(expected.name):
            continue
        if bind.scalar(
            text(f'SELECT EXISTS (SELECT 1 FROM "{expected.name}" LIMIT 1)')
        ):
            raise RuntimeError(
                f"Nonempty pre-migration followup table: {expected.name}"
            )
        columns = tuple(
            (str(row[0]), str(row[1]), bool(row[2]))
            for row in bind.execute(
                text(
                    "SELECT attname,format_type(atttypid,atttypmod),attnotnull FROM pg_attribute "
                    "WHERE attrelid=to_regclass(:name) AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                ),
                {"name": expected.name},
            )
        )
        primary = tuple(
            inspector.get_pk_constraint(expected.name)["constrained_columns"]
        )
        unique = tuple(
            sorted(
                tuple(item["column_names"])
                for item in inspector.get_unique_constraints(expected.name)
            )
        )
        foreign = tuple(
            sorted(
                (
                    tuple(item["constrained_columns"]),
                    tuple(
                        f"{item['referred_table']}.{column}"
                        for column in item["referred_columns"]
                    ),
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys(expected.name)
            )
        )
        checks = tuple(
            sorted(
                _check(str(item["sqltext"]))
                for item in inspector.get_check_constraints(expected.name)
            )
        )
        indexes = tuple(
            sorted(
                (tuple(item["column_names"]), bool(item["unique"]))
                for item in inspector.get_indexes(expected.name)
            )
        )
        expected_indexes = tuple(sorted((item, True) for item in expected.unique))
        if (
            columns != expected.columns
            or primary != expected.primary_key
            or unique != expected.unique
            or foreign != expected.foreign_keys
            or checks != tuple(sorted(_check(item) for item in expected.checks))
            or indexes != expected_indexes
        ):
            raise RuntimeError(f"Followup schema mismatch: {expected.name}")
