"""Add immutable approved project issuance receipt/provenance; no backfill.

Small empty additive table; 5s lock/120s statement budgets. Retry whole upgrade
on contention. Retain evidence and forward-fix; downgrade refuses destruction.
"""

from alembic import op
from scripts.migrations.engineering_schema_contract import (
    ISSUANCE_TABLES,
    verify_precreated_tables,
)

revision: str = "676_project_work_order_issuance"
down_revision: str | None = "675_project_engineering_plans"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_precreated_tables(op.get_bind(), expected=ISSUANCE_TABLES)
    op.execute("""CREATE TABLE IF NOT EXISTS project_work_order_issuances (
        id UUID PRIMARY KEY, idempotency_key VARCHAR(120) NOT NULL UNIQUE,
        fingerprint VARCHAR(64) NOT NULL, work_order_id UUID NOT NULL UNIQUE REFERENCES work_order(id) ON DELETE RESTRICT,
        public_id VARCHAR(64) NOT NULL, project_id UUID NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
        task_id UUID REFERENCES project_tasks(id) ON DELETE RESTRICT,
        plan_id UUID NOT NULL, revision_id UUID NOT NULL, approval_id UUID NOT NULL,
        digest VARCHAR(64) NOT NULL, infrastructure_type VARCHAR(40) NOT NULL,
        infrastructure_id UUID NOT NULL, purpose VARCHAR(40) NOT NULL, technology VARCHAR(10) NOT NULL,
        actor_user_id UUID NOT NULL REFERENCES system_users(id) ON DELETE RESTRICT,
        actor_person_id UUID NOT NULL REFERENCES parties(id) ON DELETE RESTRICT,
        reason TEXT NOT NULL, command_id UUID NOT NULL, issued_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT fk_project_issuance_task FOREIGN KEY(project_id, task_id) REFERENCES project_tasks(project_id,id) ON DELETE RESTRICT,
        CONSTRAINT fk_project_issuance_plan FOREIGN KEY(plan_id,project_id) REFERENCES project_engineering_plans(id,project_id) ON DELETE RESTRICT,
        CONSTRAINT fk_project_issuance_approval FOREIGN KEY(plan_id,approval_id,revision_id) REFERENCES engineering_plan_decisions(plan_id,id,revision_id) ON DELETE RESTRICT,
        CONSTRAINT ck_project_issuance CHECK (length(trim(idempotency_key)) > 0 AND length(fingerprint)=64 AND length(digest)=64 AND length(trim(reason)) > 0 AND technology IN ('fiber','radio'))
    )""")
    op.execute("""CREATE FUNCTION project_issuance_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'project issuance evidence is immutable'; END; $$""")
    op.execute("""CREATE TRIGGER project_issuance_immutable BEFORE UPDATE OR DELETE ON project_work_order_issuances
        FOR EACH ROW EXECUTE FUNCTION project_issuance_immutable()""")
    op.execute("""CREATE FUNCTION project_issuance_check() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM project_engineering_plans p
            JOIN engineering_plan_decisions d ON d.id=NEW.approval_id AND d.plan_id=p.id
            JOIN engineering_plan_revisions r ON r.id=NEW.revision_id AND r.plan_id=p.id
            JOIN work_order w ON w.id=NEW.work_order_id
            WHERE p.id=NEW.plan_id AND p.project_id=NEW.project_id AND p.task_id IS NOT DISTINCT FROM NEW.task_id
            AND p.current_approval_id=NEW.approval_id AND d.kind='approve' AND d.revision_id=NEW.revision_id
            AND d.digest=NEW.digest AND r.digest=NEW.digest
            AND w.project_id=NEW.project_id AND w.project_task_id IS NOT DISTINCT FROM NEW.task_id
            AND w.public_id=NEW.public_id AND w.subscriber_id IS NULL AND w.origin_ticket_id IS NULL
            AND w.work_order_kind='infrastructure' AND w.purpose=NEW.purpose AND w.access_technology=NEW.technology
            AND r.content->>'technology'=NEW.technology
            AND r.content->'infrastructure'->>'type'=NEW.infrastructure_type
            AND r.content->'infrastructure'->>'id'=NEW.infrastructure_id::text) THEN
            RAISE EXCEPTION 'project issuance provenance mismatch';
        END IF;
        RETURN NEW;
    END; $$""")
    op.execute("""CREATE TRIGGER project_issuance_check BEFORE INSERT ON project_work_order_issuances
        FOR EACH ROW EXECUTE FUNCTION project_issuance_check()""")


def downgrade() -> None:
    raise RuntimeError("Project issuance evidence must be retained; forward-fix only")
