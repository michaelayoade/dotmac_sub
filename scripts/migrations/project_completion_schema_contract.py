"""Frozen migration682 adoption shapes, independent of current model metadata."""

import re
from dataclasses import dataclass

from sqlalchemy import Connection, inspect, text


@dataclass(frozen=True, slots=True)
class ProjectCompletionTableShape:
    name: str
    columns: tuple[tuple[str, str, bool], ...]
    primary_key: tuple[str, ...]
    unique: tuple[tuple[str, ...], ...]
    foreign_keys: tuple[tuple[tuple[str, ...], tuple[str, ...], str | None], ...]
    checks: tuple[str, ...]
    indexes: tuple[tuple[tuple[str, ...], bool], ...]


PROJECT_COMPLETION_TABLES: tuple[ProjectCompletionTableShape, ...] = (
    ProjectCompletionTableShape(
        *(
            "project_field_completion_requirements",
            (
                ("enrollment_id", "uuid", True),
                ("work_order_id", "uuid", True),
                ("project_id", "uuid", True),
                ("task_id", "uuid", False),
                ("project_subscriber_id", "uuid", False),
                ("registered_at", "timestamp with time zone", True),
            ),
            ("enrollment_id",),
            (("work_order_id",),),
            (
                (("enrollment_id",), ("completion_enrollments.id",), "RESTRICT"),
                (("project_id",), ("projects.id",), "RESTRICT"),
                (
                    ("project_id", "task_id"),
                    ("project_tasks.project_id", "project_tasks.id"),
                    "RESTRICT",
                ),
                (("project_subscriber_id",), ("subscribers.id",), "RESTRICT"),
                (("task_id",), ("project_tasks.id",), "RESTRICT"),
                (("work_order_id",), ("work_order.id",), "RESTRICT"),
            ),
            (),
            (
                (("project_id",), False),
                (("task_id",), False),
                (("work_order_id",), True),
            ),
        )
    ),
    ProjectCompletionTableShape(
        *(
            "project_field_completion_applications",
            (
                ("creation_outer_xid", "xid8", False),
                ("id", "uuid", True),
                ("project_id", "uuid", True),
                ("task_id", "uuid", False),
                ("expected_project_revision", "timestamp with time zone", True),
                ("expected_task_revision", "timestamp with time zone", False),
                ("requirement_token", "character varying(64)", True),
                ("actor_user_id", "uuid", True),
                ("actor_person_id", "uuid", True),
                ("intent_id", "uuid", True),
                ("command_id", "uuid", True),
                ("reason", "text", True),
                ("applied_at", "timestamp with time zone", True),
            ),
            ("id",),
            (("id", "intent_id"),),
            (
                (("actor_person_id",), ("parties.id",), "RESTRICT"),
                (("actor_user_id",), ("system_users.id",), "RESTRICT"),
                (("project_id",), ("projects.id",), "RESTRICT"),
                (
                    ("project_id", "task_id"),
                    ("project_tasks.project_id", "project_tasks.id"),
                    "RESTRICT",
                ),
                (("task_id",), ("project_tasks.id",), "RESTRICT"),
            ),
            (
                "length(requirement_token)=64 AND length(trim(reason))>0 AND ((task_id IS NULL) = (expected_task_revision IS NULL))",
            ),
            ((("id", "intent_id"), True),),
        )
    ),
    ProjectCompletionTableShape(
        *(
            "project_field_completion_bases",
            (
                ("application_id", "uuid", True),
                ("enrollment_id", "uuid", True),
                ("work_order_id", "uuid", True),
                ("effective_enrollment_id", "uuid", True),
                ("effective_work_order_id", "uuid", True),
                ("criteria_revision_id", "uuid", True),
                ("policy_revision_id", "uuid", True),
                ("submission_id", "uuid", True),
                ("accepted_decision_id", "uuid", True),
                ("review_generation", "integer", True),
                ("completed_event_id", "uuid", True),
                ("assignment_generation", "uuid", True),
            ),
            ("application_id", "enrollment_id"),
            (),
            (
                (
                    ("accepted_decision_id",),
                    ("completion_review_decisions.id",),
                    "RESTRICT",
                ),
                (
                    ("application_id",),
                    ("project_field_completion_applications.id",),
                    "RESTRICT",
                ),
                (("completed_event_id",), ("field_job_events.id",), "RESTRICT"),
                (
                    ("criteria_revision_id",),
                    ("completion_criteria_revisions.id",),
                    "RESTRICT",
                ),
                (
                    ("effective_enrollment_id",),
                    ("completion_enrollments.id",),
                    "RESTRICT",
                ),
                (("effective_work_order_id",), ("work_order.id",), "RESTRICT"),
                (
                    ("enrollment_id",),
                    ("project_field_completion_requirements.enrollment_id",),
                    "RESTRICT",
                ),
                (
                    ("policy_revision_id",),
                    ("completion_review_policies.id",),
                    "RESTRICT",
                ),
                (("submission_id",), ("completion_submissions.id",), "RESTRICT"),
                (("work_order_id",), ("work_order.id",), "RESTRICT"),
            ),
            (),
            (),
        )
    ),
    ProjectCompletionTableShape(
        *(
            "project_field_completion_lineage",
            (
                ("application_id", "uuid", True),
                ("enrollment_id", "uuid", True),
                ("position", "integer", True),
                ("decision_id", "uuid", True),
            ),
            ("application_id", "enrollment_id", "position"),
            (("application_id", "enrollment_id", "decision_id"),),
            (
                (
                    ("application_id", "enrollment_id"),
                    (
                        "project_field_completion_bases.application_id",
                        "project_field_completion_bases.enrollment_id",
                    ),
                    "RESTRICT",
                ),
                (
                    ("decision_id",),
                    ("completion_replacement_decisions.id",),
                    "RESTRICT",
                ),
            ),
            ("position>=0",),
            ((("application_id", "enrollment_id", "decision_id"), True),),
        )
    ),
    ProjectCompletionTableShape(
        *(
            "project_field_completion_receipts",
            (
                ("intent_id", "uuid", True),
                ("fingerprint", "character varying(64)", True),
                ("application_id", "uuid", True),
            ),
            ("intent_id",),
            (("application_id",),),
            (
                (
                    ("application_id",),
                    ("project_field_completion_applications.id",),
                    "RESTRICT",
                ),
                (
                    ("application_id", "intent_id"),
                    (
                        "project_field_completion_applications.id",
                        "project_field_completion_applications.intent_id",
                    ),
                    "RESTRICT",
                ),
            ),
            ("length(fingerprint)=64",),
            ((("application_id",), True),),
        )
    ),
)


def _check(value: str) -> str:
    value = value.lower().replace("both from", "")
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value)
    value = re.sub(r"([a-z_]+) in \(([^)]+)\)", r"\1=anyarray[\2]", value)
    return re.sub(r'[\s()"]', "", value)


def verify_project_completion_adoption(bind: Connection) -> None:
    inspector = inspect(bind)
    for expected in PROJECT_COMPLETION_TABLES:
        if not inspector.has_table(expected.name):
            continue
        if bind.scalar(
            text(f'SELECT EXISTS (SELECT 1 FROM "{expected.name}" LIMIT 1)')
        ):
            raise RuntimeError(
                f"Nonempty pre-migration project completion table: {expected.name}"
            )
        raw_columns = bind.execute(
            text(
                "SELECT attname,format_type(atttypid,atttypmod),attnotnull, "
                "atthasdef,attidentity,attgenerated,atttypid='pg_catalog.xid8'::regtype FROM pg_attribute "
                "WHERE attrelid=to_regclass(:name) AND attnum>0 AND NOT attisdropped ORDER BY attnum"
            ),
            {"name": expected.name},
        ).all()
        columns = tuple((str(row[0]), str(row[1]), bool(row[2])) for row in raw_columns)
        invalid_marker = any(
            row[0] == "creation_outer_xid" and tuple(row[3:]) != (False, "", "", True)
            for row in raw_columns
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
        expected_indexes = expected.indexes
        if (
            invalid_marker
            or columns != expected.columns
            or primary != expected.primary_key
            or unique != expected.unique
            or foreign != expected.foreign_keys
            or checks != tuple(sorted(_check(item) for item in expected.checks))
            or indexes != expected_indexes
        ):
            raise RuntimeError(f"Project completion schema mismatch: {expected.name}")
