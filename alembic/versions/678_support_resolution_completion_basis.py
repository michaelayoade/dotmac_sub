"""Pin support resolution confirmation to immutable accepted completion evidence.

Additive only; no legacy closure backfill or reopening. Empty adoption from the
fresh baseline is verified before trigger installation. Five-second lock and
120-second statement budgets; retry the migration on contention.
"""

import re

from sqlalchemy import inspect, text

from alembic import op
from scripts.migrations.engineering_schema_contract import verify_precreated_tables
from scripts.migrations.outer_transaction_provenance import (
    CreationParentTable,
    verify_optional_outer_transaction_column,
)
from scripts.migrations.support_resolution_schema_contract import (
    SUPPORT_RESOLUTION_TABLES,
)

revision: str = "678_support_resolution_completion_basis"
down_revision: str | None = "677_field_completion_review"
branch_labels: str | None = None
depends_on: str | None = None

_TABLES: tuple[str, ...] = (
    "support_resolution_requests",
    "support_resolution_bases",
    "support_resolution_attempts",
)


def _normalized_check(value: str) -> str:
    value = re.sub(r"::(?:character varying|text)(?:\[\])?", "", value.lower())
    return re.sub(r"[\s()]", "", value)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_precreated_tables(
        op.get_bind(),
        expected=SUPPORT_RESOLUTION_TABLES,
        outer_transaction_extensions=(CreationParentTable.support_request,),
    )
    bind = op.get_bind()
    op.execute("""CREATE TABLE IF NOT EXISTS support_resolution_requests (
        id UUID PRIMARY KEY, ticket_id UUID NOT NULL REFERENCES support_tickets(id) ON DELETE RESTRICT,
        token_id UUID NOT NULL UNIQUE REFERENCES ticket_access_tokens(id) ON DELETE RESTRICT,
        command_id UUID NOT NULL, intent_key VARCHAR(255) NOT NULL, fingerprint VARCHAR(64) NOT NULL, requested_at TIMESTAMPTZ NOT NULL,
        grace_hours INTEGER NOT NULL CHECK(grace_hours>=0), requested_by VARCHAR(255) NOT NULL, CHECK(length(fingerprint)=64), UNIQUE(ticket_id,intent_key)
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS support_resolution_bases (
        request_id UUID NOT NULL REFERENCES support_resolution_requests(id) ON DELETE RESTRICT,
        work_order_id UUID NOT NULL REFERENCES work_order(id) ON DELETE RESTRICT,
        enrollment_id UUID NOT NULL REFERENCES completion_enrollments(id) ON DELETE RESTRICT,
        criteria_revision_id UUID NOT NULL REFERENCES completion_criteria_revisions(id) ON DELETE RESTRICT,
        policy_revision_id UUID NOT NULL REFERENCES completion_review_policies(id) ON DELETE RESTRICT,
        assignment_generation UUID NOT NULL,
        completed_event_id UUID NOT NULL REFERENCES field_job_events(id) ON DELETE RESTRICT,
        submission_id UUID NOT NULL REFERENCES completion_submissions(id) ON DELETE RESTRICT,
        accepted_decision_id UUID NOT NULL REFERENCES completion_review_decisions(id) ON DELETE RESTRICT,
        review_generation INTEGER NOT NULL CHECK(review_generation>0),
        PRIMARY KEY(request_id,work_order_id)
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS support_resolution_attempts (
        id UUID PRIMARY KEY,
        request_id UUID REFERENCES support_resolution_requests(id) ON DELETE RESTRICT,
        ticket_id UUID NOT NULL REFERENCES support_tickets(id) ON DELETE RESTRICT,
        command_id UUID NOT NULL, outcome VARCHAR(40) NOT NULL,
        source VARCHAR(20) NOT NULL, reason VARCHAR(255) NOT NULL,
        attempted_at TIMESTAMPTZ NOT NULL,
        CHECK(outcome IN ('confirmed','blocked_completion_basis')),
        CHECK(source IN ('customer','grace'))
    )""")
    expected_columns: dict[str, tuple[tuple[str, str, bool], ...]] = {
        "support_resolution_requests": (
            ("id", "uuid", False),
            ("ticket_id", "uuid", False),
            ("token_id", "uuid", False),
            ("command_id", "uuid", False),
            ("intent_key", "character varying(255)", False),
            ("fingerprint", "character varying(64)", False),
            ("requested_at", "timestamp with time zone", False),
            ("grace_hours", "integer", False),
            ("requested_by", "character varying(255)", False),
        ),
        "support_resolution_bases": tuple(
            (name, "integer" if name == "review_generation" else "uuid", False)
            for name in (
                "request_id",
                "work_order_id",
                "enrollment_id",
                "criteria_revision_id",
                "policy_revision_id",
                "assignment_generation",
                "completed_event_id",
                "submission_id",
                "accepted_decision_id",
                "review_generation",
            )
        ),
        "support_resolution_attempts": (
            ("id", "uuid", False),
            ("request_id", "uuid", True),
            ("ticket_id", "uuid", False),
            ("command_id", "uuid", False),
            ("outcome", "character varying(40)", False),
            ("source", "character varying(20)", False),
            ("reason", "character varying(255)", False),
            ("attempted_at", "timestamp with time zone", False),
        ),
    }
    expected_foreign_keys: dict[str, set[tuple[str, str, str]]] = {
        "support_resolution_requests": {
            ("ticket_id", "support_tickets", "id"),
            ("token_id", "ticket_access_tokens", "id"),
        },
        "support_resolution_bases": {
            ("request_id", "support_resolution_requests", "id"),
            ("work_order_id", "work_order", "id"),
            ("enrollment_id", "completion_enrollments", "id"),
            ("criteria_revision_id", "completion_criteria_revisions", "id"),
            ("policy_revision_id", "completion_review_policies", "id"),
            ("completed_event_id", "field_job_events", "id"),
            ("submission_id", "completion_submissions", "id"),
            ("accepted_decision_id", "completion_review_decisions", "id"),
        },
        "support_resolution_attempts": {
            ("request_id", "support_resolution_requests", "id"),
            ("ticket_id", "support_tickets", "id"),
        },
    }
    expected_checks = {
        "support_resolution_requests": {
            "checkgrace_hours>=0",
            "checklengthfingerprint=64",
        },
        "support_resolution_bases": {"checkreview_generation>0"},
        "support_resolution_attempts": {
            "checkoutcome=anyarray['confirmed','blocked_completion_basis']",
            "checksource=anyarray['customer','grace']",
        },
    }
    inspector = inspect(bind)
    for table_name, expected in expected_columns.items():
        actual = tuple(
            (str(row[0]), str(row[1]), not bool(row[2]))
            for row in bind.execute(
                text(
                    "SELECT attname,format_type(atttypid,atttypmod),attnotnull FROM pg_attribute WHERE attrelid=to_regclass(:name) AND attnum>0 AND NOT attisdropped ORDER BY attnum"
                ),
                {"name": table_name},
            )
        )
        if (
            table_name == CreationParentTable.support_request
            and verify_optional_outer_transaction_column(
                bind, table_name=CreationParentTable.support_request
            )
        ):
            actual = tuple(x for x in actual if x[0] != "creation_outer_xid")
        foreign_keys = inspector.get_foreign_keys(table_name)
        native_fks = {
            (
                str(fk["constrained_columns"][0]),
                str(fk["referred_table"]),
                str(fk["referred_columns"][0]),
            )
            for fk in foreign_keys
            if len(fk["constrained_columns"]) == 1
        }
        expected_pk = (
            ["request_id", "work_order_id"]
            if table_name == "support_resolution_bases"
            else ["id"]
        )
        expected_unique = (
            {("token_id",), ("ticket_id", "intent_key")}
            if table_name == "support_resolution_requests"
            else set()
        )
        actual_unique = {
            tuple(str(column) for column in unique["column_names"])
            for unique in inspector.get_unique_constraints(table_name)
        }
        checks = {
            _normalized_check("CHECK (" + str(check["sqltext"]) + ")")
            for check in inspector.get_check_constraints(table_name)
        }
        if (
            actual != expected
            or native_fks != expected_foreign_keys[table_name]
            or len(foreign_keys) != len(native_fks)
            or any(fk["options"].get("ondelete") != "RESTRICT" for fk in foreign_keys)
            or inspector.get_pk_constraint(table_name)["constrained_columns"]
            != expected_pk
            or actual_unique != expected_unique
            or checks != expected_checks[table_name]
        ):
            raise RuntimeError(f"Support resolution schema mismatch: {table_name}")
    op.execute("""CREATE FUNCTION support_resolution_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'support resolution evidence is immutable'; END; $$""")
    for table_name in _TABLES:
        op.execute(
            f"CREATE TRIGGER support_resolution_immutable BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW EXECUTE FUNCTION support_resolution_immutable()"
        )
    op.execute("""CREATE FUNCTION support_resolution_request_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM ticket_access_tokens t WHERE t.id=NEW.token_id AND t.ticket_id=NEW.ticket_id AND t.purpose='resolution_confirm')
        THEN RAISE EXCEPTION 'support resolution token lineage mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER support_resolution_request_lineage BEFORE INSERT ON support_resolution_requests FOR EACH ROW EXECUTE FUNCTION support_resolution_request_lineage()"
    )
    op.execute("""CREATE FUNCTION support_resolution_basis_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM support_resolution_requests r JOIN work_order w ON w.origin_ticket_id=r.ticket_id JOIN completion_enrollments e ON e.work_order_id=w.id WHERE r.id=NEW.request_id AND w.id=NEW.work_order_id AND e.id=NEW.enrollment_id AND r.xmin=(txid_current() % 4294967296)::text::xid)
        THEN RAISE EXCEPTION 'support resolution basis must accompany exact ticket request creation'; END IF;
        IF NOT EXISTS (SELECT 1 FROM completion_criteria_revisions c WHERE c.id=NEW.criteria_revision_id AND c.enrollment_id=NEW.enrollment_id)
        OR NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.enrollment_id=NEW.enrollment_id AND s.criteria_revision_id=NEW.criteria_revision_id AND s.policy_revision_id=NEW.policy_revision_id AND s.assignment_generation=NEW.assignment_generation AND s.completed_event_id=NEW.completed_event_id)
        OR NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.accepted_decision_id AND d.enrollment_id=NEW.enrollment_id AND d.submission_id=NEW.submission_id AND d.policy_revision_id=NEW.policy_revision_id AND d.state='accepted')
        OR NOT EXISTS (SELECT 1 FROM completion_review_heads h WHERE h.enrollment_id=NEW.enrollment_id AND h.submission_id=NEW.submission_id AND h.decision_id=NEW.accepted_decision_id AND h.generation=NEW.review_generation)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads h WHERE h.enrollment_id=NEW.enrollment_id AND h.revision_id=NEW.criteria_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN completion_review_policy_heads h ON h.scope_type=e.policy_scope_type AND h.scope_id=e.policy_scope_id WHERE e.id=NEW.enrollment_id AND h.revision_id=NEW.policy_revision_id)
        OR NOT EXISTS (SELECT 1 FROM field_job_events e WHERE e.id=NEW.completed_event_id AND e.work_order_mirror_id=NEW.work_order_id AND e.event='complete' AND e.new_status='completed' AND e.assignment_generation=NEW.assignment_generation)
        THEN RAISE EXCEPTION 'support resolution completion evidence lineage mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER support_resolution_basis_lineage BEFORE INSERT ON support_resolution_bases FOR EACH ROW EXECUTE FUNCTION support_resolution_basis_lineage()"
    )
    op.execute("""CREATE FUNCTION support_resolution_attempt_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.request_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM support_resolution_requests r WHERE r.id=NEW.request_id AND r.ticket_id=NEW.ticket_id)
        THEN RAISE EXCEPTION 'support resolution attempt ticket mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER support_resolution_attempt_lineage BEFORE INSERT ON support_resolution_attempts FOR EACH ROW EXECUTE FUNCTION support_resolution_attempt_lineage()"
    )

    op.execute("""CREATE FUNCTION support_resolution_basis_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (SELECT count(*) FROM support_resolution_bases b WHERE b.request_id=NEW.id) <> (SELECT count(*) FROM work_order w JOIN completion_enrollments e ON e.work_order_id=w.id WHERE w.origin_ticket_id=NEW.ticket_id)
        THEN RAISE EXCEPTION 'support resolution request requires all exact enrolled linked work orders'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER support_resolution_basis_complete AFTER INSERT ON support_resolution_requests DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION support_resolution_basis_complete()"
    )


def downgrade() -> None:
    raise RuntimeError("Support resolution evidence must be retained; forward-fix only")
