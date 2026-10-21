"""Frozen migration677 adoption shapes from equal fresh/predecessor PostgreSQL."""

from scripts.migrations.engineering_schema_contract import (
    ColumnShape,
    ConstraintKind,
    ConstraintShape,
    IndexShape,
    NativeTableShape,
)

COMPLETION_TABLES: tuple[NativeTableShape, ...] = (
    NativeTableShape(
        "completion_criteria",
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
                'CHECK (length(TRIM(BOTH FROM statement)) > 0 AND length(TRIM(BOTH FROM verification_method)) > 0 AND "order" >= 0)',
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (revision_id) REFERENCES completion_criteria_revisions(id) ON DELETE RESTRICT",
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
        "completion_criteria_heads",
        (
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("generation", "integer", True),
            ColumnShape("revision_id", "uuid", True),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (generation > 0)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id) REFERENCES completion_enrollments(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id, revision_id) REFERENCES completion_criteria_revisions(enrollment_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (enrollment_id)"),
        ),
        (IndexShape(True, True, ("enrollment_id",), None),),
    ),
    NativeTableShape(
        "completion_criteria_revisions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("revision", "integer", True),
            ColumnShape("engineering_revision_id", "uuid", False),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
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
                "FOREIGN KEY (engineering_revision_id) REFERENCES engineering_plan_revisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id) REFERENCES completion_enrollments(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (enrollment_id, id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (enrollment_id, revision)"),
        ),
        (
            IndexShape(True, False, ("enrollment_id", "id"), None),
            IndexShape(True, False, ("enrollment_id", "revision"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "completion_criterion_results",
        (
            ColumnShape("decision_id", "uuid", True),
            ColumnShape("criterion_id", "uuid", True),
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("verdict", "character varying(8)", True),
            ColumnShape("reason", "text", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK ((verdict::text = ANY (ARRAY['passed'::character varying, 'failed'::character varying]::text[])) AND length(TRIM(BOTH FROM reason)) > 0)",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (decision_id) REFERENCES completion_review_decisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (submission_id, criterion_id) REFERENCES completion_observations(submission_id, criterion_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("p"), "PRIMARY KEY (decision_id, criterion_id)"
            ),
        ),
        (IndexShape(True, True, ("decision_id", "criterion_id"), None),),
    ),
    NativeTableShape(
        "completion_enrollments",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("work_order_id", "uuid", True),
            ColumnShape("public_id", "character varying(64)", True),
            ColumnShape("provenance", "character varying(24)", True),
            ColumnShape("engineering_issuance_id", "uuid", False),
            ColumnShape("project_id", "uuid", False),
            ColumnShape("task_id", "uuid", False),
            ColumnShape("subscriber_id", "uuid", False),
            ColumnShape("origin_ticket_id", "uuid", False),
            ColumnShape("policy_scope_type", "character varying(12)", True),
            ColumnShape("policy_scope_id", "uuid", True),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("enrolled_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"), "CHECK (length(TRIM(BOTH FROM reason)) > 0)"
            ),
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (policy_scope_type::text = 'project'::text AND policy_scope_id = project_id AND project_id IS NOT NULL OR policy_scope_type::text = 'work_order'::text AND policy_scope_id = work_order_id)",
            ),
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (provenance::text = 'engineering_issuance'::text AND engineering_issuance_id IS NOT NULL AND project_id IS NOT NULL OR provenance::text = 'native_work_order'::text AND engineering_issuance_id IS NULL)",
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
                "FOREIGN KEY (engineering_issuance_id) REFERENCES project_work_order_issuances(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (origin_ticket_id) REFERENCES support_tickets(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_scope_type, policy_scope_id) REFERENCES completion_review_policy_heads(scope_type, scope_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (subscriber_id) REFERENCES subscribers(id) ON DELETE RESTRICT",
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
            ConstraintShape(ConstraintKind("u"), "UNIQUE (engineering_issuance_id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (work_order_id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (work_order_id, id)"),
        ),
        (
            IndexShape(True, False, ("engineering_issuance_id",), None),
            IndexShape(True, False, ("work_order_id",), None),
            IndexShape(True, False, ("work_order_id", "id"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "completion_evidence_files",
        (
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("criterion_id", "uuid", True),
            ColumnShape("attachment_id", "uuid", True),
            ColumnShape("stored_file_id", "uuid", True),
            ColumnShape("checksum", "character varying(64)", True),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (length(checksum::text) = 64)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (attachment_id) REFERENCES field_attachments(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (stored_file_id) REFERENCES stored_files(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (submission_id, criterion_id) REFERENCES completion_observations(submission_id, criterion_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("p"),
                "PRIMARY KEY (submission_id, criterion_id, attachment_id)",
            ),
        ),
        (
            IndexShape(False, False, ("stored_file_id",), None),
            IndexShape(
                True, True, ("submission_id", "criterion_id", "attachment_id"), None
            ),
        ),
    ),
    NativeTableShape(
        "completion_observations",
        (
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("criterion_id", "uuid", True),
            ColumnShape("criteria_revision_id", "uuid", True),
            ColumnShape("observation", "text", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"), "CHECK (length(TRIM(BOTH FROM observation)) > 0)"
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (criteria_revision_id, criterion_id) REFERENCES completion_criteria(revision_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (submission_id, criteria_revision_id) REFERENCES completion_submissions(id, criteria_revision_id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("p"), "PRIMARY KEY (submission_id, criterion_id)"
            ),
        ),
        (IndexShape(True, True, ("submission_id", "criterion_id"), None),),
    ),
    NativeTableShape(
        "completion_review_decisions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("policy_revision_id", "uuid", True),
            ColumnShape("state", "character varying(12)", True),
            ColumnShape("revokes_id", "uuid", False),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("decided_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK ((state::text = ANY (ARRAY['accepted'::character varying, 'rejected'::character varying, 'revoked'::character varying]::text[])) AND length(TRIM(BOTH FROM reason)) > 0 AND (state::text = 'revoked'::text) = (revokes_id IS NOT NULL))",
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
                "FOREIGN KEY (enrollment_id, submission_id) REFERENCES completion_submissions(enrollment_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_revision_id) REFERENCES completion_review_policies(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (revokes_id) REFERENCES completion_review_decisions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (enrollment_id, id)"),
        ),
        (
            IndexShape(True, False, ("enrollment_id", "id"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "completion_review_heads",
        (
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("generation", "integer", True),
            ColumnShape("submission_id", "uuid", True),
            ColumnShape("decision_id", "uuid", False),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (generation > 0)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id) REFERENCES completion_enrollments(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id, decision_id) REFERENCES completion_review_decisions(enrollment_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (enrollment_id, submission_id) REFERENCES completion_submissions(enrollment_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (enrollment_id)"),
        ),
        (IndexShape(True, True, ("enrollment_id",), None),),
    ),
    NativeTableShape(
        "completion_review_policies",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("scope_type", "character varying(12)", True),
            ColumnShape("scope_id", "uuid", True),
            ColumnShape("project_id", "uuid", False),
            ColumnShape("work_order_id", "uuid", False),
            ColumnShape("revision", "integer", True),
            ColumnShape("independent_reviewer_required", "boolean", True),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", True),
            ColumnShape("reason", "text", True),
            ColumnShape("created_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (revision > 0 AND length(TRIM(BOTH FROM reason)) > 0)",
            ),
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (scope_type::text = 'project'::text AND project_id IS NOT NULL AND scope_id = project_id AND work_order_id IS NULL OR scope_type::text = 'work_order'::text AND work_order_id IS NOT NULL AND scope_id = work_order_id AND project_id IS NULL)",
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
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (work_order_id) REFERENCES work_order(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (scope_type, scope_id, id)"),
            ConstraintShape(
                ConstraintKind("u"), "UNIQUE (scope_type, scope_id, revision)"
            ),
        ),
        (
            IndexShape(True, False, ("scope_type", "scope_id", "id"), None),
            IndexShape(True, False, ("scope_type", "scope_id", "revision"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "completion_review_policy_heads",
        (
            ColumnShape("scope_type", "character varying(12)", True),
            ColumnShape("scope_id", "uuid", True),
            ColumnShape("generation", "integer", True),
            ColumnShape("revision_id", "uuid", True),
        ),
        (
            ConstraintShape(ConstraintKind("c"), "CHECK (generation > 0)"),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (scope_type, scope_id, revision_id) REFERENCES completion_review_policies(scope_type, scope_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (scope_type, scope_id)"),
        ),
        (IndexShape(True, True, ("scope_type", "scope_id"), None),),
    ),
    NativeTableShape(
        "completion_review_receipts",
        (
            ColumnShape("idempotency_key", "character varying(120)", True),
            ColumnShape("fingerprint", "character varying(64)", True),
            ColumnShape("enrollment_id", "uuid", False),
            ColumnShape("criteria_revision_id", "uuid", False),
            ColumnShape("completed_event_id", "uuid", False),
            ColumnShape("criteria_generation", "integer", True),
            ColumnShape("policy_revision_id", "uuid", False),
            ColumnShape("submission_id", "uuid", False),
            ColumnShape("decision_id", "uuid", False),
            ColumnShape("generation", "integer", True),
            ColumnShape("state", "character varying(24)", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"),
                "CHECK (length(TRIM(BOTH FROM idempotency_key)) > 0 AND length(fingerprint::text) = 64 AND generation >= 0)",
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
                "FOREIGN KEY (decision_id) REFERENCES completion_review_decisions(id) ON DELETE RESTRICT",
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
                "FOREIGN KEY (submission_id) REFERENCES completion_submissions(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (idempotency_key)"),
        ),
        (IndexShape(True, True, ("idempotency_key",), None),),
    ),
    NativeTableShape(
        "completion_submissions",
        (
            ColumnShape("id", "uuid", True),
            ColumnShape("enrollment_id", "uuid", True),
            ColumnShape("policy_revision_id", "uuid", True),
            ColumnShape("criteria_revision_id", "uuid", True),
            ColumnShape("completed_event_id", "uuid", True),
            ColumnShape("assignment_generation", "uuid", True),
            ColumnShape("actor_user_id", "uuid", True),
            ColumnShape("actor_person_id", "uuid", False),
            ColumnShape("actor_vendor_user_id", "uuid", False),
            ColumnShape("reason", "text", True),
            ColumnShape("submitted_at", "timestamp with time zone", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("c"), "CHECK (length(TRIM(BOTH FROM reason)) > 0)"
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
                "FOREIGN KEY (actor_vendor_user_id) REFERENCES field_vendor_users(id) ON DELETE RESTRICT",
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
                "FOREIGN KEY (enrollment_id, criteria_revision_id) REFERENCES completion_criteria_revisions(enrollment_id, id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (policy_revision_id) REFERENCES completion_review_policies(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(ConstraintKind("p"), "PRIMARY KEY (id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (enrollment_id, id)"),
            ConstraintShape(ConstraintKind("u"), "UNIQUE (id, criteria_revision_id)"),
        ),
        (
            IndexShape(True, False, ("enrollment_id", "id"), None),
            IndexShape(True, False, ("id", "criteria_revision_id"), None),
            IndexShape(True, True, ("id",), None),
        ),
    ),
    NativeTableShape(
        "stored_file_retentions",
        (
            ColumnShape("stored_file_id", "uuid", True),
            ColumnShape("reference_id", "uuid", True),
            ColumnShape("source_owner", "character varying(80)", True),
        ),
        (
            ConstraintShape(
                ConstraintKind("f"),
                "FOREIGN KEY (stored_file_id) REFERENCES stored_files(id) ON DELETE RESTRICT",
            ),
            ConstraintShape(
                ConstraintKind("p"), "PRIMARY KEY (stored_file_id, reference_id)"
            ),
        ),
        (IndexShape(True, True, ("stored_file_id", "reference_id"), None),),
    ),
)
