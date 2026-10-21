"""Frozen migration675/676 baseline adoption contract, independent of ORM models.

The squashed baseline emits current metadata before historical migrations.
Only empty tables with these exact migration-owned definitions may be adopted.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, literal, select, table, text

from scripts.migrations.outer_transaction_provenance import (
    CreationParentTable,
    verify_optional_outer_transaction_column,
)


class ConstraintKind(StrEnum):
    primary = "p"
    unique = "u"
    foreign = "f"
    check = "c"


@dataclass(frozen=True, slots=True)
class ColumnShape:
    name: str
    sql_type: str
    not_null: bool


@dataclass(frozen=True, slots=True)
class ConstraintShape:
    kind: ConstraintKind
    definition: str


@dataclass(frozen=True, slots=True)
class IndexShape:
    unique: bool
    primary: bool
    columns: tuple[str, ...]
    predicate: str | None


@dataclass(frozen=True, slots=True)
class NativeTableShape:
    name: str
    columns: tuple[ColumnShape, ...]
    constraints: tuple[ConstraintShape, ...]
    indexes: tuple[IndexShape, ...]


def _index_key(shape: IndexShape) -> tuple[bool, bool, tuple[str, ...], str]:
    return shape.unique, shape.primary, shape.columns, shape.predicate or ""


def verify_precreated_tables(
    bind: Connection,
    *,
    expected: tuple[NativeTableShape, ...],
    outer_transaction_extensions: tuple[CreationParentTable, ...] = (),
) -> None:
    for target in expected:
        parameters = {"table": target.name}
        if bind.scalar(text("SELECT to_regclass(:table)"), parameters) is None:
            continue
        if bind.scalar(select(literal(True)).select_from(table(target.name)).limit(1)):
            raise RuntimeError(
                f"Nonempty pre-migration engineering table: {target.name}"
            )
        columns = tuple(
            ColumnShape(str(row[0]), str(row[1]), bool(row[2]))
            for row in bind.execute(
                text(
                    "SELECT attname, format_type(atttypid,atttypmod), attnotnull "
                    "FROM pg_attribute WHERE attrelid=to_regclass(:table) "
                    "AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                ),
                parameters,
            )
        )
        if target.name in outer_transaction_extensions:
            parent = CreationParentTable(target.name)
            if verify_optional_outer_transaction_column(bind, table_name=parent):
                columns = tuple(x for x in columns if x.name != "creation_outer_xid")
        constraints = tuple(
            sorted(
                (
                    ConstraintShape(ConstraintKind(str(row[0])), str(row[1]))
                    for row in bind.execute(
                        text(
                            "SELECT contype, pg_get_constraintdef(oid,true) FROM pg_constraint "
                            "WHERE conrelid=to_regclass(:table) AND contype IN ('p','u','f','c')"
                        ),
                        parameters,
                    )
                ),
                key=lambda item: (item.kind.value, item.definition),
            )
        )
        indexes = tuple(
            sorted(
                (
                    IndexShape(
                        bool(row[0]),
                        bool(row[1]),
                        tuple(str(value) for value in row[2]),
                        row[3],
                    )
                    for row in bind.execute(
                        text(
                            "SELECT i.indisunique,i.indisprimary, "
                            "ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord) "
                            "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
                            "ORDER BY k.ord), pg_get_expr(i.indpred,i.indrelid,true) "
                            "FROM pg_index i WHERE i.indrelid=to_regclass(:table)"
                        ),
                        parameters,
                    )
                ),
                key=_index_key,
            )
        )
        if NativeTableShape(target.name, columns, constraints, indexes) != target:
            raise RuntimeError(f"Pre-migration engineering schema drift: {target.name}")


ENGINEERING_TABLES: tuple[NativeTableShape, ...] = (
    NativeTableShape(
        "engineering_plan_criteria",
        (
            ColumnShape("revision_id", "uuid", True),
            ColumnShape("id", "uuid", True),
            ColumnShape("order", "integer", True),
            ColumnShape("statement", "text", True),
            ColumnShape("verification_method", "text", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                'CHECK ("order" >= 0 AND length(TRIM(BOTH FROM statement)) > 0 AND length(TRIM(BOTH FROM verification_method)) > 0)',
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (revision_id) REFERENCES engineering_plan_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (revision_id, id)"),
            ConstraintShape(ConstraintKind("u"), 'UNIQUE (revision_id, "order")'),
        ),
        (
            IndexShape(True, False, ("revision_id", "order"), None),
            IndexShape(True, True, ("revision_id", "id"), None),
        ),
    ),
    NativeTableShape(
        "engineering_plan_decisions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("plan_id", "uuid", True),
            ColumnShape("project_id", "uuid", True),
            ColumnShape("revision_id", "uuid", True),
            ColumnShape("policy_revision_id", "uuid", True),
            ColumnShape("kind", "character varying(12)", True),
            ColumnShape("digest", "character varying(64)", True),
            ColumnShape("prior_approval_id", "uuid", False),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("author_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("created_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK ((kind::text = ANY (ARRAY['approve'::character varying, 'reject'::character varying, 'revoke'::character varying, 'supersede'::character varying]::text[])) AND length(TRIM(BOTH FROM reason)) > 0 AND length(digest::text) = 64)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_person_id) REFERENCES parties(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_user_id) REFERENCES system_users(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (author_person_id) REFERENCES parties(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id) REFERENCES project_engineering_plans(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, prior_approval_id) REFERENCES engineering_plan_decisions(plan_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, project_id) REFERENCES project_engineering_plans(id, project_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, revision_id) REFERENCES engineering_plan_revisions(plan_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_revision_id) REFERENCES engineering_policy_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (prior_approval_id) REFERENCES engineering_plan_decisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id, policy_revision_id) REFERENCES engineering_policy_revisions(project_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (plan_id, id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (plan_id, id, revision_id)"),
        ),
        (
            IndexShape(True, False, ("plan_id", "id"), None),
            IndexShape(True, False, ("plan_id", "id", "revision_id"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "engineering_plan_receipts",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("idempotency_key", "character varying(120)", True),
            ColumnShape("fingerprint", "character varying(64)", True),
            ColumnShape("plan_id", "uuid", False),
            ColumnShape("revision_id", "uuid", False),
            ColumnShape("decision_id", "uuid", False),
            ColumnShape("policy_revision_id", "uuid", False),
            ColumnShape("generation", "integer", True),
            ColumnShape("action", "character varying(20)", True),
            ColumnShape("digest", "character varying(64)", False),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (generation > 0 AND length(TRIM(BOTH FROM idempotency_key)) > 0 AND length(fingerprint::text) = 64)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (decision_id) REFERENCES engineering_plan_decisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id) REFERENCES project_engineering_plans(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, decision_id) REFERENCES engineering_plan_decisions(plan_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, decision_id, revision_id) REFERENCES engineering_plan_decisions(plan_id, id, revision_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, revision_id) REFERENCES engineering_plan_revisions(plan_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_revision_id) REFERENCES engineering_policy_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (revision_id) REFERENCES engineering_plan_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (idempotency_key)"),
        ),
        (
            IndexShape(True, False, ("idempotency_key",), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "engineering_plan_revisions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("plan_id", "uuid", True),
            ColumnShape("revision", "integer", True),
            ColumnShape("content", "json", True),
            ColumnShape("origin_binding", "json", True),
            ColumnShape("digest", "character varying(64)", True),
            ColumnShape("author_user_id", "uuid", True),
            ColumnShape("author_person_id", "uuid", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("created_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (revision > 0 AND length(digest::text) = 64)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (author_person_id) REFERENCES parties(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (author_user_id) REFERENCES system_users(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id) REFERENCES project_engineering_plans(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (command_id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (plan_id, id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (plan_id, revision)"),
        ),
        (
            IndexShape(True, False, ("command_id",), None),
            IndexShape(True, False, ("plan_id", "id"), None),
            IndexShape(True, False, ("plan_id", "revision"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "engineering_policy_head",
        (
            ColumnShape("project_id", "uuid", True),
            ColumnShape("generation", "integer", True),
            ColumnShape("current_revision_id", "uuid", False),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (generation >= 0)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (current_revision_id) REFERENCES engineering_policy_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id, current_revision_id) REFERENCES engineering_policy_revisions(project_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (project_id)"),
        ),
        (IndexShape(True, True, ("project_id",), None),),
    ),
    NativeTableShape(
        "engineering_policy_revisions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("project_id", "uuid", True),
            ColumnShape("revision", "integer", True),
            ColumnShape("independent_reviewer_required", "boolean", True),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("created_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (revision > 0 AND length(TRIM(BOTH FROM reason)) > 0)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_person_id) REFERENCES parties(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_user_id) REFERENCES system_users(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (command_id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (project_id, id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (project_id, revision)"),
        ),
        (
            IndexShape(True, False, ("command_id",), None),
            IndexShape(True, False, ("project_id", "id"), None),
            IndexShape(True, False, ("project_id", "revision"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "project_engineering_plans",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("project_id", "uuid", True),
            ColumnShape("task_id", "uuid", False),
            ColumnShape("technology", "character varying(10)", True),
            ColumnShape("generation", "integer", True),
            ColumnShape("latest_revision_id", "uuid", False),
            ColumnShape("current_approval_id", "uuid", False),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (generation >= 0 AND (technology::text = ANY (ARRAY['fiber'::character varying, 'radio'::character varying]::text[])))",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (id, current_approval_id) REFERENCES engineering_plan_decisions(plan_id, id)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (id, latest_revision_id) REFERENCES engineering_plan_revisions(plan_id, id)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id, task_id) REFERENCES project_tasks(project_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (task_id) REFERENCES project_tasks(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (id, project_id)"),
        ),
        (
            IndexShape(True, False, ("id", "project_id"), None),
            IndexShape(
                True,
                False,
                ("project_id", "task_id", "technology"),
                "task_id IS NOT NULL",
            ),
            IndexShape(True, False, ("project_id", "technology"), "task_id IS NULL"),
            IndexShape(True, True, ("id",), None),
        ),
    ),
)


ISSUANCE_TABLES: tuple[NativeTableShape, ...] = (
    NativeTableShape(
        "project_work_order_issuances",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("idempotency_key", "character varying(120)", True),
            ColumnShape("fingerprint", "character varying(64)", True),
            ColumnShape("work_order_id", "uuid", True),
            ColumnShape("public_id", "character varying(64)", True),
            ColumnShape("project_id", "uuid", True),
            ColumnShape("task_id", "uuid", False),
            ColumnShape("plan_id", "uuid", True),
            ColumnShape("revision_id", "uuid", True),
            ColumnShape("approval_id", "uuid", True),
            ColumnShape("digest", "character varying(64)", True),
            ColumnShape("infrastructure_type", "character varying(40)", True),
            ColumnShape("infrastructure_id", "uuid", True),
            ColumnShape("purpose", "character varying(40)", True),
            ColumnShape("technology", "character varying(10)", True),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("issued_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (length(TRIM(BOTH FROM idempotency_key)) > 0 AND length(fingerprint::text) = 64 AND length(digest::text) = 64 AND length(TRIM(BOTH FROM reason)) > 0 AND (technology::text = ANY (ARRAY['fiber'::character varying, 'radio'::character varying]::text[])))",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_person_id) REFERENCES parties(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (actor_user_id) REFERENCES system_users(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, approval_id, revision_id) REFERENCES engineering_plan_decisions(plan_id, id, revision_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (plan_id, project_id) REFERENCES project_engineering_plans(id, project_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id, task_id) REFERENCES project_tasks(project_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (task_id) REFERENCES project_tasks(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (work_order_id) REFERENCES work_order(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (idempotency_key)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (work_order_id)"),
        ),
        (
            IndexShape(True, False, ("idempotency_key",), None),
            IndexShape(True, False, ("work_order_id",), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
)
