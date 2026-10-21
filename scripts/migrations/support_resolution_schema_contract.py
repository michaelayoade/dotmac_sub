"""Frozen migration678 adoption shapes from equal fresh/predecessor PostgreSQL."""

from scripts.migrations.engineering_schema_contract import (
    ColumnShape,
    ConstraintKind,
    ConstraintShape,
    IndexShape,
    NativeTableShape,
)

SUPPORT_RESOLUTION_TABLES: tuple[NativeTableShape, ...] = (
    NativeTableShape(
        "support_resolution_attempts",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("request_id", "uuid", False),
            ColumnShape("ticket_id", "uuid", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("outcome", "character varying(40)", True),
            ColumnShape("source", "character varying(20)", True),
            ColumnShape("reason", "character varying(255)", True),
            ColumnShape("attempted_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (outcome::text = ANY (ARRAY['confirmed'::character varying, 'blocked_completion_basis'::character varying]::text[]))",
            ),
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (source::text = ANY (ARRAY['customer'::character varying, 'grace'::character varying]::text[]))",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (request_id) REFERENCES support_resolution_requests(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
        ),
        (IndexShape(True, True, ("id",), None),),
    ),
    NativeTableShape(
        "support_resolution_bases",
        (
            ColumnShape("request_id", "uuid", True),
            ColumnShape("work_order_id", "uuid", True),
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("criteria_revision_id", "uuid", True),
            ColumnShape("policy_revision_id", "uuid", True),
            ColumnShape("assignment_generation", "uuid", True),
            ColumnShape("completed_event_id", "uuid", True),
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("accepted_decision_id", "uuid", True),
            ColumnShape("review_generation", "integer", True),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (review_generation > 0)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (accepted_decision_id) REFERENCES completion_review_decisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (completed_event_id) REFERENCES field_job_events(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (criteria_revision_id) REFERENCES completion_criteria_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id) REFERENCES completion_enrollments(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_revision_id) REFERENCES completion_review_policies(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (request_id) REFERENCES support_resolution_requests(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (submission_id) REFERENCES completion_submissions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (work_order_id) REFERENCES work_order(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("p"), "PRIMARY KEY (request_id, work_order_id)"
            ),
        ),
        (IndexShape(True, True, ("request_id", "work_order_id"), None),),
    ),
    NativeTableShape(
        "support_resolution_requests",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("ticket_id", "uuid", True),
            ColumnShape("token_id", "uuid", True),
            ColumnShape("command_id", "uuid", True),
            ColumnShape("intent_key", "character varying(255)", True),
            ColumnShape("fingerprint", "character varying(64)", True),
            ColumnShape("requested_at", "timestamp with time zone", True),
            ColumnShape("grace_hours", "integer", True),
            ColumnShape("requested_by", "character varying(255)", True),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (grace_hours >= 0)"),
            ConstraintShape(
                ConstraintKind("c"), "CHECK (length(fingerprint::text) = 64)"
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (token_id) REFERENCES ticket_access_tokens(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (ticket_id, intent_key)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (token_id)"),
        ),
        (
            IndexShape(True, False, ("ticket_id", "intent_key"), None),
            IndexShape(True, False, ("token_id",), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
)
