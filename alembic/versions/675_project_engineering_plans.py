"""Native project engineering plans and explicit per-project approval policy.

Additive criteria-content-only storage. No approval backfill or role grants.
"""

import sqlalchemy as sa

from alembic import op
from scripts.migrations.engineering_schema_contract import (
    ENGINEERING_TABLES,
    verify_precreated_tables,
)

revision: str = "675_project_engineering_plans"
down_revision: str | None = "674_selected_vendor_attending_person"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_precreated_tables(op.get_bind(), expected=ENGINEERING_TABLES)
    # Adopt an exact current-metadata baseline constraint if present; actual
    # predecessor deployments receive it here.
    unique_constraints = sa.inspect(op.get_bind()).get_unique_constraints(
        "project_tasks"
    )
    task_identity = next(
        (
            item
            for item in unique_constraints
            if item["name"] == "uq_project_tasks_project_id_id"
        ),
        None,
    )
    if task_identity is None:
        op.create_unique_constraint(
            "uq_project_tasks_project_id_id", "project_tasks", ["project_id", "id"]
        )
    elif task_identity["column_names"] != ["project_id", "id"]:
        raise RuntimeError("Engineering baseline project-task identity drift")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_policy_revisions (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	revision INTEGER NOT NULL,
	independent_reviewer_required BOOLEAN NOT NULL,
	actor_user_id UUID NOT NULL,
	actor_person_id UUID NOT NULL,
	reason TEXT NOT NULL,
	command_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_engineering_policy_revision CHECK (revision > 0 AND length(trim(reason)) > 0),
	CONSTRAINT uq_engineering_policy_project_revision UNIQUE (project_id, revision),
	CONSTRAINT uq_engineering_policy_revision_pair UNIQUE (project_id, id),
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	FOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT,
	FOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT,
	UNIQUE (command_id)
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_policy_head (
	project_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	current_revision_id UUID,
	PRIMARY KEY (project_id),
	CONSTRAINT ck_engineering_policy_head CHECK (generation >= 0),
	CONSTRAINT fk_engineering_policy_head_revision FOREIGN KEY(project_id, current_revision_id) REFERENCES engineering_policy_revisions (project_id, id) ON DELETE RESTRICT,
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	FOREIGN KEY(current_revision_id) REFERENCES engineering_policy_revisions (id) ON DELETE RESTRICT
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS project_engineering_plans (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	task_id UUID,
	technology VARCHAR(10) NOT NULL,
	generation INTEGER NOT NULL,
	latest_revision_id UUID,
	current_approval_id UUID,
	PRIMARY KEY (id),
	CONSTRAINT uq_engineering_plan_project UNIQUE (id, project_id),
	CONSTRAINT fk_engineering_plan_task_project FOREIGN KEY(project_id, task_id) REFERENCES project_tasks (project_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_engineering_plan_head CHECK (generation >= 0 AND technology IN ('fiber','radio')),
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	FOREIGN KEY(task_id) REFERENCES project_tasks (id) ON DELETE RESTRICT
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_plan_revisions (
	id UUID NOT NULL,
	plan_id UUID NOT NULL,
	revision INTEGER NOT NULL,
	content JSON NOT NULL,
	origin_binding JSON NOT NULL,
	digest VARCHAR(64) NOT NULL,
	author_user_id UUID NOT NULL,
	author_person_id UUID NOT NULL,
	command_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_engineering_plan_revision UNIQUE (plan_id, revision),
	CONSTRAINT uq_engineering_revision_pair UNIQUE (plan_id, id),
	CONSTRAINT ck_engineering_revision CHECK (revision > 0 AND length(digest) = 64),
	FOREIGN KEY(plan_id) REFERENCES project_engineering_plans (id) ON DELETE RESTRICT,
	FOREIGN KEY(author_user_id) REFERENCES system_users (id) ON DELETE RESTRICT,
	FOREIGN KEY(author_person_id) REFERENCES parties (id) ON DELETE RESTRICT,
	UNIQUE (command_id)
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_plan_criteria (
	revision_id UUID NOT NULL,
	id UUID NOT NULL,
	"order" INTEGER NOT NULL,
	statement TEXT NOT NULL,
	verification_method TEXT NOT NULL,
	PRIMARY KEY (revision_id, id),
	CONSTRAINT uq_engineering_criterion_order UNIQUE (revision_id, "order"),
	CONSTRAINT ck_engineering_criterion CHECK ("order" >= 0 AND length(trim(statement)) > 0 AND length(trim(verification_method)) > 0),
	FOREIGN KEY(revision_id) REFERENCES engineering_plan_revisions (id) ON DELETE RESTRICT
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_plan_decisions (
	id UUID NOT NULL,
	plan_id UUID NOT NULL,
	project_id UUID NOT NULL,
	revision_id UUID NOT NULL,
	policy_revision_id UUID NOT NULL,
	kind VARCHAR(12) NOT NULL,
	digest VARCHAR(64) NOT NULL,
	prior_approval_id UUID,
	actor_user_id UUID NOT NULL,
	actor_person_id UUID NOT NULL,
	author_person_id UUID NOT NULL,
	reason TEXT NOT NULL,
	command_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_engineering_decision_revision_pair UNIQUE (plan_id, id, revision_id),
	CONSTRAINT fk_engineering_decision_project FOREIGN KEY(plan_id, project_id) REFERENCES project_engineering_plans (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_engineering_decision_project_policy FOREIGN KEY(project_id, policy_revision_id) REFERENCES engineering_policy_revisions (project_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_engineering_prior_approval_plan FOREIGN KEY(plan_id, prior_approval_id) REFERENCES engineering_plan_decisions (plan_id, id) ON DELETE RESTRICT,
	CONSTRAINT uq_engineering_decision_pair UNIQUE (plan_id, id),
	CONSTRAINT fk_engineering_decision_revision FOREIGN KEY(plan_id, revision_id) REFERENCES engineering_plan_revisions (plan_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_engineering_decision CHECK (kind IN ('approve','reject','revoke','supersede') AND length(trim(reason)) > 0 AND length(digest) = 64),
	FOREIGN KEY(plan_id) REFERENCES project_engineering_plans (id) ON DELETE RESTRICT,
	FOREIGN KEY(policy_revision_id) REFERENCES engineering_policy_revisions (id) ON DELETE RESTRICT,
	FOREIGN KEY(prior_approval_id) REFERENCES engineering_plan_decisions (id) ON DELETE RESTRICT,
	FOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT,
	FOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT,
	FOREIGN KEY(author_person_id) REFERENCES parties (id) ON DELETE RESTRICT
)""")
    op.execute("""CREATE TABLE IF NOT EXISTS engineering_plan_receipts (
	id UUID NOT NULL,
	idempotency_key VARCHAR(120) NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	plan_id UUID,
	revision_id UUID,
	decision_id UUID,
	policy_revision_id UUID,
	generation INTEGER NOT NULL,
	action VARCHAR(20) NOT NULL,
	digest VARCHAR(64),
	PRIMARY KEY (id),
	CONSTRAINT fk_engineering_receipt_decision_revision FOREIGN KEY(plan_id, decision_id, revision_id) REFERENCES engineering_plan_decisions (plan_id, id, revision_id) ON DELETE RESTRICT,
	CONSTRAINT fk_engineering_receipt_revision FOREIGN KEY(plan_id, revision_id) REFERENCES engineering_plan_revisions (plan_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_engineering_receipt_decision FOREIGN KEY(plan_id, decision_id) REFERENCES engineering_plan_decisions (plan_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_engineering_receipt CHECK (generation > 0 AND length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64),
	UNIQUE (idempotency_key),
	FOREIGN KEY(plan_id) REFERENCES project_engineering_plans (id) ON DELETE RESTRICT,
	FOREIGN KEY(revision_id) REFERENCES engineering_plan_revisions (id) ON DELETE RESTRICT,
	FOREIGN KEY(decision_id) REFERENCES engineering_plan_decisions (id) ON DELETE RESTRICT,
	FOREIGN KEY(policy_revision_id) REFERENCES engineering_policy_revisions (id) ON DELETE RESTRICT
)""")
    # The squashed baseline emits current model tables, including deferred FKs.
    # A real predecessor has neither; adopt only the exact declared binding.
    inspector = sa.inspect(op.get_bind())
    foreign_keys = {
        key["name"]: key
        for key in inspector.get_foreign_keys("project_engineering_plans")
    }
    for name, local, target in (
        (
            "fk_engineering_latest_revision",
            "latest_revision_id",
            "engineering_plan_revisions",
        ),
        (
            "fk_engineering_current_approval",
            "current_approval_id",
            "engineering_plan_decisions",
        ),
    ):
        existing = foreign_keys.get(name)
        if existing is not None:
            if (
                existing["constrained_columns"] != ["id", local]
                or existing["referred_table"] != target
                or existing["referred_columns"] != ["plan_id", "id"]
            ):
                raise RuntimeError("Engineering baseline foreign-key binding drift")
        else:
            op.create_foreign_key(
                name,
                "project_engineering_plans",
                target,
                ["id", local],
                ["plan_id", "id"],
            )
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_engineering_project_plan ON project_engineering_plans (project_id, technology) WHERE task_id IS NULL"""
    )
    op.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_engineering_task_plan ON project_engineering_plans (project_id, task_id, technology) WHERE task_id IS NOT NULL"""
    )
    op.execute("""
    CREATE FUNCTION engineering_evidence_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Engineering plan evidence is immutable'; END; $$
    """)
    op.execute(
        "CREATE TRIGGER engineering_policy_revisions_immutable BEFORE UPDATE OR DELETE ON engineering_policy_revisions FOR EACH ROW EXECUTE FUNCTION engineering_evidence_immutable()"
    )
    op.execute(
        "CREATE TRIGGER engineering_plan_revisions_immutable BEFORE UPDATE OR DELETE ON engineering_plan_revisions FOR EACH ROW EXECUTE FUNCTION engineering_evidence_immutable()"
    )
    op.execute(
        "CREATE TRIGGER engineering_plan_criteria_immutable BEFORE UPDATE OR DELETE ON engineering_plan_criteria FOR EACH ROW EXECUTE FUNCTION engineering_evidence_immutable()"
    )
    op.execute(
        "CREATE TRIGGER engineering_plan_decisions_immutable BEFORE UPDATE OR DELETE ON engineering_plan_decisions FOR EACH ROW EXECUTE FUNCTION engineering_evidence_immutable()"
    )
    op.execute(
        "CREATE TRIGGER engineering_plan_receipts_immutable BEFORE UPDATE OR DELETE ON engineering_plan_receipts FOR EACH ROW EXECUTE FUNCTION engineering_evidence_immutable()"
    )
    op.execute("""
    CREATE FUNCTION engineering_plan_binding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' OR NEW.id IS DISTINCT FROM OLD.id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.technology IS DISTINCT FROM OLD.technology THEN
        RAISE EXCEPTION 'Engineering plan binding is immutable';
      END IF;
      RETURN NEW;
    END; $$
    """)
    op.execute(
        "CREATE TRIGGER engineering_plan_binding_immutable BEFORE UPDATE OR DELETE ON project_engineering_plans FOR EACH ROW EXECUTE FUNCTION engineering_plan_binding_immutable()"
    )

    op.execute("""
    CREATE FUNCTION engineering_criteria_creation_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM engineering_plan_revisions
        WHERE id = NEW.revision_id AND xmin = (txid_current() % 4294967296)::text::xid
      ) THEN RAISE EXCEPTION 'Criteria must be created atomically with their immutable revision'; END IF;
      RETURN NEW;
    END; $$
    """)
    op.execute(
        "CREATE TRIGGER engineering_criteria_creation_only BEFORE INSERT ON engineering_plan_criteria FOR EACH ROW EXECUTE FUNCTION engineering_criteria_creation_only()"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('39f12d13-e142-5c04-95bf-495e19be7345', 'project:engineering_plan:read', 'Native project engineering plan read', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('d94ed0c3-c0f1-5769-b30a-59c8237654f6', 'project:engineering_plan:write', 'Native project engineering plan write', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('5f0dc14b-92cc-5e00-b33d-7fb646b157c6', 'project:engineering_plan:approve', 'Native project engineering plan approve', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('029b6c41-49b8-5e54-8b3d-5f6c7bf6ed72', 'project:engineering_plan:revoke', 'Native project engineering plan revoke', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('abae2c8f-3696-50ef-9e55-e455c825636c', 'project:engineering_plan:policy', 'Native project engineering plan policy', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Engineering approval history requires a reviewed forward fix; preserve immutable evidence"
    )
