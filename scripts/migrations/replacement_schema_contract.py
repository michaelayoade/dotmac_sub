"""Frozen migration679 adoption shapes; independent of current model metadata."""

import re
from dataclasses import dataclass

from sqlalchemy import Connection, inspect, text

from scripts.migrations.outer_transaction_provenance import (
    CreationParentTable,
    verify_optional_outer_transaction_column,
)


@dataclass(frozen=True, slots=True)
class ReplacementTableShape:
    name: str
    columns: tuple[tuple[str, str, bool], ...]
    primary_key: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    checks: tuple[str, ...]


REPLACEMENT_TABLES: tuple[ReplacementTableShape, ...] = (
    ReplacementTableShape(
        *(
            "completion_replacement_decisions",
            (
                ("id", "uuid", True),
                ("source_enrollment_id", "uuid", True),
                ("replacement_enrollment_id", "uuid", True),
                ("generation", "integer", True),
                ("state", "character varying(12)", True),
                ("revokes_id", "uuid", False),
                ("source_criteria_revision_id", "uuid", True),
                ("replacement_criteria_revision_id", "uuid", True),
                ("source_policy_revision_id", "uuid", True),
                ("replacement_policy_revision_id", "uuid", True),
                ("source_assignment_generation", "uuid", True),
                ("replacement_assignment_generation", "uuid", True),
                ("failure_event_id", "uuid", False),
                ("failure_submission_id", "uuid", False),
                ("failure_decision_id", "uuid", False),
                ("failure_review_generation", "integer", False),
                ("actor_user_id", "uuid", True),
                ("actor_person_id", "uuid", True),
                ("reason", "text", True),
                ("decided_at", "timestamp with time zone", True),
            ),
            ("id",),
            (("source_enrollment_id", "generation"),),
            (
                (("actor_person_id",), ("parties.id",), "RESTRICT"),
                (("actor_user_id",), ("system_users.id",), "RESTRICT"),
                (
                    ("failure_decision_id",),
                    ("completion_review_decisions.id",),
                    "RESTRICT",
                ),
                (("failure_event_id",), ("field_job_events.id",), "RESTRICT"),
                (
                    ("failure_submission_id",),
                    ("completion_submissions.id",),
                    "RESTRICT",
                ),
                (
                    ("replacement_criteria_revision_id",),
                    ("completion_criteria_revisions.id",),
                    "RESTRICT",
                ),
                (
                    ("replacement_enrollment_id",),
                    ("completion_enrollments.id",),
                    "RESTRICT",
                ),
                (
                    ("replacement_policy_revision_id",),
                    ("completion_review_policies.id",),
                    "RESTRICT",
                ),
                (("revokes_id",), ("completion_replacement_decisions.id",), "RESTRICT"),
                (
                    ("source_criteria_revision_id",),
                    ("completion_criteria_revisions.id",),
                    "RESTRICT",
                ),
                (("source_enrollment_id",), ("completion_enrollments.id",), "RESTRICT"),
                (
                    ("source_policy_revision_id",),
                    ("completion_review_policies.id",),
                    "RESTRICT",
                ),
            ),
            (
                "(failure_event_id IS NOT NULL AND failure_submission_id IS NULL AND failure_decision_id IS NULL AND failure_review_generation IS NULL) OR (failure_event_id IS NULL AND failure_submission_id IS NOT NULL AND failure_decision_id IS NOT NULL AND failure_review_generation > 0)",
                "source_enrollment_id <> replacement_enrollment_id AND generation > 0 AND length(trim(reason)) > 0",
                "state IN ('approved','revoked') AND ((state='revoked') = (revokes_id IS NOT NULL))",
            ),
        )
    ),
    ReplacementTableShape(
        *(
            "completion_replacement_coverage",
            (
                ("decision_id", "uuid", True),
                ("source_criterion_id", "uuid", True),
                ("replacement_criterion_id", "uuid", True),
            ),
            ("decision_id", "source_criterion_id"),
            (("decision_id", "replacement_criterion_id"),),
            ((("decision_id",), ("completion_replacement_decisions.id",), "RESTRICT"),),
            (),
        )
    ),
    ReplacementTableShape(
        *(
            "completion_replacement_heads",
            (
                ("source_enrollment_id", "uuid", True),
                ("replacement_enrollment_id", "uuid", False),
                ("decision_id", "uuid", True),
                ("generation", "integer", True),
            ),
            ("source_enrollment_id",),
            (("replacement_enrollment_id",),),
            (
                (
                    ("decision_id",),
                    ("completion_replacement_decisions.id",),
                    "RESTRICT",
                ),
                (
                    ("replacement_enrollment_id",),
                    ("completion_enrollments.id",),
                    "RESTRICT",
                ),
                (("source_enrollment_id",), ("completion_enrollments.id",), "RESTRICT"),
            ),
            ("generation > 0 AND source_enrollment_id <> replacement_enrollment_id",),
        )
    ),
    ReplacementTableShape(
        *(
            "completion_replacement_receipts",
            (
                ("idempotency_key", "character varying(120)", True),
                ("fingerprint", "character varying(64)", True),
                ("decision_id", "uuid", True),
            ),
            ("idempotency_key",),
            (),
            ((("decision_id",), ("completion_replacement_decisions.id",), "RESTRICT"),),
            ("length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64",),
        )
    ),
    ReplacementTableShape(
        *(
            "support_resolution_effective_bases",
            (
                ("request_id", "uuid", True),
                ("work_order_id", "uuid", True),
                ("effective_work_order_id", "uuid", True),
                ("effective_enrollment_id", "uuid", True),
            ),
            ("request_id", "work_order_id"),
            (),
            (
                (
                    ("effective_enrollment_id",),
                    ("completion_enrollments.id",),
                    "RESTRICT",
                ),
                (("effective_work_order_id",), ("work_order.id",), "RESTRICT"),
                (
                    ("request_id", "work_order_id"),
                    (
                        "support_resolution_bases.request_id",
                        "support_resolution_bases.work_order_id",
                    ),
                    "RESTRICT",
                ),
            ),
            (),
        )
    ),
    ReplacementTableShape(
        *(
            "support_resolution_replacement_lineage",
            (
                ("request_id", "uuid", True),
                ("work_order_id", "uuid", True),
                ("position", "integer", True),
                ("decision_id", "uuid", True),
            ),
            ("request_id", "work_order_id", "position"),
            (("request_id", "work_order_id", "decision_id"),),
            (
                (
                    ("decision_id",),
                    ("completion_replacement_decisions.id",),
                    "RESTRICT",
                ),
                (
                    ("request_id", "work_order_id"),
                    (
                        "support_resolution_effective_bases.request_id",
                        "support_resolution_effective_bases.work_order_id",
                    ),
                    "RESTRICT",
                ),
            ),
            ("position >= 0",),
        )
    ),
)


def _check(value: str) -> str:
    value = value.lower().replace("both from", "")
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value)
    value = re.sub(r"([a-z_]+) in \(([^)]+)\)", r"\1=anyarray[\2]", value)
    return re.sub(r'[\s()"]', "", value)


def verify_replacement_adoption(bind: Connection) -> None:
    inspector = inspect(bind)
    for expected in REPLACEMENT_TABLES:
        if not inspector.has_table(expected.name):
            continue
        if bind.scalar(
            text(f'SELECT EXISTS (SELECT 1 FROM "{expected.name}" LIMIT 1)')
        ):
            raise RuntimeError(
                f"Nonempty pre-migration replacement table: {expected.name}"
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
        if (
            expected.name == CreationParentTable.replacement_decision
            and verify_optional_outer_transaction_column(
                bind, table_name=CreationParentTable.replacement_decision
            )
        ):
            columns = tuple(x for x in columns if x[0] != "creation_outer_xid")
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
            raise RuntimeError(f"Replacement schema mismatch: {expected.name}")
