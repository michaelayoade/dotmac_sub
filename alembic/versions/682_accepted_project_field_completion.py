"""Explicit enrolled physical requirements gate project/task completion.

Additive expand/backfill/verify. Historical terminal scope remains terminal.
No acceptance, provisioning or commissioning backfill. Forward-fix only.
"""

from alembic import op
from scripts.migrations.outer_transaction_provenance import (
    CreationParentTable,
    install_outer_transaction_provenance,
)
from scripts.migrations.project_completion_schema_contract import (
    verify_project_completion_adoption,
)

revision: str = "682_accepted_project_field_completion"
down_revision: str | None = "681_shared_outage_completion"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_project_completion_adoption(op.get_bind())
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS project_field_completion_requirements (\n\tenrollment_id UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\tproject_id UUID NOT NULL, \n\ttask_id UUID, \n\tproject_subscriber_id UUID, \n\tregistered_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (enrollment_id), \n\tFOREIGN KEY(project_id, task_id) REFERENCES project_tasks (project_id, id) ON DELETE RESTRICT, \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tUNIQUE (work_order_id), \n\tFOREIGN KEY(work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(task_id) REFERENCES project_tasks (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(project_subscriber_id) REFERENCES subscribers (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_field_completion_requirements_project_id ON project_field_completion_requirements (project_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_field_completion_requirements_task_id ON project_field_completion_requirements (task_id)"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS project_field_completion_applications (\n\tcreation_outer_xid xid8, \n\tid UUID NOT NULL, \n\tproject_id UUID NOT NULL, \n\ttask_id UUID, \n\texpected_project_revision TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpected_task_revision TIMESTAMP WITH TIME ZONE, \n\trequirement_token VARCHAR(64) NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\tintent_id UUID NOT NULL, \n\tcommand_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tapplied_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK (length(requirement_token)=64 AND length(trim(reason))>0 AND ((task_id IS NULL) = (expected_task_revision IS NULL))), \n\tUNIQUE (id, intent_id), \n\tFOREIGN KEY(project_id, task_id) REFERENCES project_tasks (project_id, id) ON DELETE RESTRICT, \n\tFOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(task_id) REFERENCES project_tasks (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS project_field_completion_bases (\n\tapplication_id UUID NOT NULL, \n\tenrollment_id UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\teffective_enrollment_id UUID NOT NULL, \n\teffective_work_order_id UUID NOT NULL, \n\tcriteria_revision_id UUID NOT NULL, \n\tpolicy_revision_id UUID NOT NULL, \n\tsubmission_id UUID NOT NULL, \n\taccepted_decision_id UUID NOT NULL, \n\treview_generation INTEGER NOT NULL, \n\tcompleted_event_id UUID NOT NULL, \n\tassignment_generation UUID NOT NULL, \n\tPRIMARY KEY (application_id, enrollment_id), \n\tFOREIGN KEY(application_id) REFERENCES project_field_completion_applications (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(enrollment_id) REFERENCES project_field_completion_requirements (enrollment_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(effective_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(effective_work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(criteria_revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(submission_id) REFERENCES completion_submissions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(accepted_decision_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(completed_event_id) REFERENCES field_job_events (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS project_field_completion_lineage (\n\tapplication_id UUID NOT NULL, \n\tenrollment_id UUID NOT NULL, \n\tposition INTEGER NOT NULL, \n\tdecision_id UUID NOT NULL, \n\tPRIMARY KEY (application_id, enrollment_id, position), \n\tFOREIGN KEY(application_id, enrollment_id) REFERENCES project_field_completion_bases (application_id, enrollment_id) ON DELETE RESTRICT, \n\tCHECK (position>=0), \n\tUNIQUE (application_id, enrollment_id, decision_id), \n\tFOREIGN KEY(decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS project_field_completion_receipts (\n\tintent_id UUID NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tapplication_id UUID NOT NULL, \n\tPRIMARY KEY (intent_id), \n\tCHECK (length(fingerprint)=64), \n\tFOREIGN KEY(application_id, intent_id) REFERENCES project_field_completion_applications (id, intent_id) ON DELETE RESTRICT, \n\tUNIQUE (application_id), \n\tFOREIGN KEY(application_id) REFERENCES project_field_completion_applications (id) ON DELETE RESTRICT\n)\n\n"
    )
    install_outer_transaction_provenance(
        op.get_bind(),
        table_names=(CreationParentTable.project_application,),
    )
    # Backfill explicit enrollments only. Contradictory history refuses adoption.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM completion_enrollments e JOIN work_order w ON w.id=e.work_order_id JOIN projects p ON p.id=e.project_id
            WHERE e.project_id IS NOT NULL AND (w.project_id IS DISTINCT FROM e.project_id OR w.project_task_id IS DISTINCT FROM e.task_id OR (p.subscriber_id IS NOT NULL AND w.subscriber_id IS NOT NULL AND p.subscriber_id<>w.subscriber_id)))
        THEN RAISE EXCEPTION 'Project field enrollment binding drift requires owner repair before migration'; END IF;
    END; $$""")
    op.execute("""INSERT INTO project_field_completion_requirements(enrollment_id,work_order_id,project_id,task_id,project_subscriber_id,registered_at)
        SELECT e.id,e.work_order_id,e.project_id,e.task_id,p.subscriber_id,e.enrolled_at
        FROM completion_enrollments e JOIN projects p ON p.id=e.project_id
        WHERE e.project_id IS NOT NULL""")
    op.execute("""CREATE FUNCTION project_field_completion_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'Project field completion evidence is immutable'; END; $$""")
    for table in (
        "project_field_completion_requirements",
        "project_field_completion_applications",
        "project_field_completion_bases",
        "project_field_completion_lineage",
        "project_field_completion_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER project_field_completion_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION project_field_completion_immutable()"
        )
    op.execute("""CREATE FUNCTION project_field_requirement_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE root projects; target project_tasks;
    BEGIN
        SELECT * INTO STRICT root FROM projects WHERE id=NEW.project_id FOR UPDATE;
        IF NEW.task_id IS NOT NULL THEN SELECT * INTO STRICT target FROM project_tasks WHERE id=NEW.task_id AND project_id=NEW.project_id FOR UPDATE; END IF;
        IF NOT root.is_active OR (NEW.task_id IS NOT NULL AND (NOT target.is_active OR target.template_plan_state='superseded')) OR root.status IN ('completed','canceled') OR (NEW.task_id IS NOT NULL AND target.status IN ('done','canceled'))
        OR NEW.project_subscriber_id IS DISTINCT FROM root.subscriber_id
        OR EXISTS (SELECT 1 FROM work_order w WHERE w.id=NEW.work_order_id AND w.subscriber_id IS NOT NULL AND root.subscriber_id IS NOT NULL AND w.subscriber_id<>root.subscriber_id)
        OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN work_order w ON w.id=e.work_order_id
            WHERE e.id=NEW.enrollment_id AND e.work_order_id=NEW.work_order_id AND e.project_id=NEW.project_id
            AND e.task_id IS NOT DISTINCT FROM NEW.task_id AND w.project_id=NEW.project_id AND w.project_task_id IS NOT DISTINCT FROM NEW.task_id)
        THEN RAISE EXCEPTION 'Exact nonterminal enrolled project field requirement required'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER project_field_requirement_lineage BEFORE INSERT ON project_field_completion_requirements FOR EACH ROW EXECUTE FUNCTION project_field_requirement_lineage()"
    )
    op.execute("""CREATE FUNCTION project_field_basis_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE applied project_field_completion_applications; cursor UUID; entry RECORD; position INTEGER:=0;
    BEGIN
        SELECT * INTO STRICT applied FROM project_field_completion_applications WHERE id=NEW.application_id;
        IF NOT EXISTS (SELECT 1 FROM project_field_completion_applications WHERE id=NEW.application_id AND creation_outer_xid=pg_current_xact_id())
        OR NOT EXISTS (SELECT 1 FROM project_field_completion_requirements r JOIN completion_enrollments e ON e.id=r.enrollment_id JOIN work_order original ON original.id=e.work_order_id
            WHERE original.project_id=r.project_id AND original.project_task_id IS NOT DISTINCT FROM r.task_id AND original.subscriber_id IS NOT DISTINCT FROM e.subscriber_id AND original.origin_ticket_id IS NOT DISTINCT FROM e.origin_ticket_id
            AND r.enrollment_id=NEW.enrollment_id AND r.work_order_id=NEW.work_order_id AND r.project_id=applied.project_id
            AND e.work_order_id=r.work_order_id AND e.project_id=r.project_id AND e.task_id IS NOT DISTINCT FROM r.task_id)
        THEN RAISE EXCEPTION 'Exact original project field requirement basis required'; END IF;
        IF applied.task_id IS NOT NULL AND NOT EXISTS (
            WITH RECURSIVE subtree(id) AS (SELECT applied.task_id UNION SELECT t.id FROM project_tasks t JOIN subtree s ON t.parent_task_id=s.id WHERE t.project_id=applied.project_id)
            SELECT 1 FROM project_field_completion_requirements r JOIN subtree s ON s.id=r.task_id WHERE r.enrollment_id=NEW.enrollment_id)
        THEN RAISE EXCEPTION 'Project field requirement leaves current task subtree'; END IF;
        cursor:=NEW.enrollment_id;
        FOR entry IN SELECT l.position,d.*,h.decision_id AS current_decision,h.replacement_enrollment_id AS current_replacement
            FROM project_field_completion_lineage l JOIN completion_replacement_decisions d ON d.id=l.decision_id
            JOIN completion_replacement_heads h ON h.source_enrollment_id=d.source_enrollment_id
            WHERE l.application_id=NEW.application_id AND l.enrollment_id=NEW.enrollment_id ORDER BY l.position
        LOOP
            IF entry.position<>position OR entry.source_enrollment_id<>cursor OR entry.state<>'approved'
            OR entry.current_decision<>entry.id OR entry.current_replacement IS DISTINCT FROM entry.replacement_enrollment_id
            OR (entry.failure_event_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM completion_enrollments e JOIN work_order w ON w.id=e.work_order_id
                JOIN field_job_events f ON f.work_order_mirror_id=w.id
                WHERE e.id=entry.source_enrollment_id AND w.status='canceled'
                AND f.id=entry.failure_event_id AND f.event='unable_to_complete' AND f.new_status='canceled'
                AND f.assignment_generation=entry.source_assignment_generation
                AND f.id=(SELECT recent.id FROM field_job_events recent WHERE recent.work_order_mirror_id=w.id ORDER BY recent.received_at DESC,recent.id DESC LIMIT 1)))
            OR (entry.failure_event_id IS NULL AND NOT EXISTS (
                SELECT 1 FROM completion_review_heads h JOIN completion_review_decisions d ON d.id=h.decision_id
                JOIN completion_submissions s ON s.id=h.submission_id
                JOIN completion_enrollments e ON e.id=h.enrollment_id JOIN work_order w ON w.id=e.work_order_id
                JOIN field_job_events f ON f.id=s.completed_event_id AND f.work_order_mirror_id=w.id
                WHERE h.enrollment_id=entry.source_enrollment_id AND h.decision_id=entry.failure_decision_id
                AND h.submission_id=entry.failure_submission_id AND h.generation=entry.failure_review_generation AND d.state='rejected'
                AND w.status='completed' AND f.event='complete' AND f.new_status='completed'
                AND s.criteria_revision_id=entry.source_criteria_revision_id AND s.policy_revision_id=entry.source_policy_revision_id
                AND s.assignment_generation=entry.source_assignment_generation AND f.assignment_generation=entry.source_assignment_generation
                AND f.system_user_id=s.actor_user_id AND f.author_vendor_user_id IS NOT DISTINCT FROM s.actor_vendor_user_id
                AND f.id=(SELECT recent.id FROM field_job_events recent WHERE recent.work_order_mirror_id=w.id ORDER BY recent.received_at DESC,recent.id DESC LIMIT 1)))
            OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads WHERE enrollment_id=entry.source_enrollment_id AND revision_id=entry.source_criteria_revision_id)
            OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads WHERE enrollment_id=entry.replacement_enrollment_id AND revision_id=entry.replacement_criteria_revision_id)
            OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN completion_review_policy_heads h ON h.scope_type=e.policy_scope_type AND h.scope_id=e.policy_scope_id JOIN work_order_assignment_queue q ON q.work_order_mirror_id=e.work_order_id AND q.status='assigned'
                WHERE e.id=entry.source_enrollment_id AND h.revision_id=entry.source_policy_revision_id AND q.assignment_generation=entry.source_assignment_generation)
            OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN completion_review_policy_heads h ON h.scope_type=e.policy_scope_type AND h.scope_id=e.policy_scope_id JOIN work_order_assignment_queue q ON q.work_order_mirror_id=e.work_order_id AND q.status='assigned'
                WHERE e.id=entry.replacement_enrollment_id AND h.revision_id=entry.replacement_policy_revision_id AND q.assignment_generation=entry.replacement_assignment_generation)
            THEN RAISE EXCEPTION 'Project field replacement ordered lineage mismatch'; END IF;
            cursor:=entry.replacement_enrollment_id; position:=position+1;
        END LOOP;
        IF cursor<>NEW.effective_enrollment_id
        OR NOT EXISTS (SELECT 1 FROM completion_enrollments e WHERE e.id=NEW.effective_enrollment_id AND e.work_order_id=NEW.effective_work_order_id)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_revisions c WHERE c.id=NEW.criteria_revision_id AND c.enrollment_id=NEW.effective_enrollment_id)
        OR NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.enrollment_id=NEW.effective_enrollment_id
            AND s.criteria_revision_id=NEW.criteria_revision_id AND s.policy_revision_id=NEW.policy_revision_id
            AND s.assignment_generation=NEW.assignment_generation AND s.completed_event_id=NEW.completed_event_id)
        OR NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.accepted_decision_id AND d.enrollment_id=NEW.effective_enrollment_id
            AND d.submission_id=NEW.submission_id AND d.policy_revision_id=NEW.policy_revision_id AND d.state='accepted')
        OR NOT EXISTS (SELECT 1 FROM completion_review_heads h WHERE h.enrollment_id=NEW.effective_enrollment_id AND h.submission_id=NEW.submission_id
            AND h.decision_id=NEW.accepted_decision_id AND h.generation=NEW.review_generation)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads WHERE enrollment_id=NEW.effective_enrollment_id AND revision_id=NEW.criteria_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN completion_review_policy_heads h ON h.scope_type=e.policy_scope_type AND h.scope_id=e.policy_scope_id
            WHERE e.id=NEW.effective_enrollment_id AND h.revision_id=NEW.policy_revision_id)
        OR NOT EXISTS (SELECT 1 FROM work_order_assignment_queue q WHERE q.work_order_mirror_id=NEW.effective_work_order_id AND q.status='assigned' AND q.assignment_generation=NEW.assignment_generation)
        OR NOT EXISTS (SELECT 1 FROM field_job_events f JOIN work_order w ON w.id=f.work_order_mirror_id
            WHERE f.id=NEW.completed_event_id AND f.work_order_mirror_id=NEW.effective_work_order_id AND f.event='complete' AND f.new_status='completed'
            AND f.assignment_generation=NEW.assignment_generation AND w.status='completed'
            AND f.id=(SELECT recent.id FROM field_job_events recent WHERE recent.work_order_mirror_id=w.id ORDER BY recent.received_at DESC,recent.id DESC LIMIT 1))
        THEN RAISE EXCEPTION 'Project field effective accepted completion evidence mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER project_field_basis_lineage AFTER INSERT ON project_field_completion_bases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION project_field_basis_lineage()"
    )
    op.execute("""CREATE FUNCTION project_field_evidence_creation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM project_field_completion_applications WHERE id=NEW.application_id AND creation_outer_xid=pg_current_xact_id())
        THEN RAISE EXCEPTION 'Project field evidence must accompany this transaction application'; END IF;
        RETURN NEW;
    END; $$""")
    for table in (
        "project_field_completion_bases",
        "project_field_completion_lineage",
        "project_field_completion_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER project_field_evidence_creation BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION project_field_evidence_creation()"
        )
    op.execute("""CREATE FUNCTION project_field_completion_covered(application UUID) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
        WITH RECURSIVE applied AS (SELECT * FROM project_field_completion_applications WHERE id=application),
        subtree(id) AS (SELECT task_id FROM applied WHERE task_id IS NOT NULL UNION SELECT t.id FROM project_tasks t JOIN subtree s ON t.parent_task_id=s.id JOIN applied a ON a.project_id=t.project_id),
        required AS (SELECT r.* FROM project_field_completion_requirements r JOIN applied a ON a.project_id=r.project_id WHERE a.task_id IS NULL OR r.task_id IN (SELECT id FROM subtree))
        SELECT EXISTS (SELECT 1 FROM required)
        AND NOT EXISTS (SELECT 1 FROM required r WHERE NOT EXISTS (SELECT 1 FROM project_field_completion_bases b WHERE b.application_id=application AND b.enrollment_id=r.enrollment_id AND b.work_order_id=r.work_order_id))
        AND NOT EXISTS (SELECT 1 FROM project_field_completion_bases b WHERE b.application_id=application AND NOT EXISTS (SELECT 1 FROM required r WHERE r.enrollment_id=b.enrollment_id AND r.work_order_id=b.work_order_id))
        AND EXISTS (SELECT 1 FROM project_field_completion_receipts receipt JOIN applied a ON a.id=receipt.application_id AND a.intent_id=receipt.intent_id)
        AND NOT EXISTS (SELECT 1 FROM required r JOIN projects p ON p.id=r.project_id WHERE r.project_subscriber_id IS DISTINCT FROM p.subscriber_id);
    $$""")
    op.execute("""CREATE FUNCTION project_field_completion_status_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.is_active IS DISTINCT FROM OLD.is_active AND EXISTS (SELECT 1 FROM project_field_completion_requirements WHERE project_id=OLD.id)
        THEN RAISE EXCEPTION 'Explicitly enrolled project scope cannot be archived'; END IF;
        IF NEW.status='completed' AND OLD.status IS DISTINCT FROM 'completed'
        AND EXISTS (SELECT 1 FROM project_field_completion_requirements WHERE project_id=NEW.id)
        AND NOT EXISTS (SELECT 1 FROM project_field_completion_applications a WHERE a.project_id=NEW.id AND a.task_id IS NULL
            AND a.expected_project_revision=OLD.updated_at AND a.creation_outer_xid=pg_current_xact_id() AND project_field_completion_covered(a.id))
        THEN RAISE EXCEPTION 'Use accepted field completion for enrolled project scope'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER project_field_completion_status_guard BEFORE UPDATE OF status, is_active ON projects FOR EACH ROW EXECUTE FUNCTION project_field_completion_status_guard()"
    )
    op.execute("""CREATE FUNCTION project_task_field_completion_status_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE root projects; marked BOOLEAN;
    BEGIN
        SELECT * INTO STRICT root FROM projects WHERE id=OLD.project_id FOR UPDATE NOWAIT;
        WITH RECURSIVE subtree(id) AS (SELECT OLD.id UNION SELECT t.id FROM project_tasks t JOIN subtree s ON t.parent_task_id=s.id WHERE t.project_id=OLD.project_id)
        SELECT EXISTS (SELECT 1 FROM project_field_completion_requirements r JOIN subtree s ON s.id=r.task_id WHERE r.project_id=OLD.project_id) INTO marked;
        IF marked AND (NEW.parent_task_id IS DISTINCT FROM OLD.parent_task_id OR NEW.project_id IS DISTINCT FROM OLD.project_id OR NEW.is_active IS DISTINCT FROM OLD.is_active)
        THEN RAISE EXCEPTION 'Explicitly enrolled task scope cannot be archived or reparented'; END IF;
        IF marked AND NEW.status='done' AND OLD.status IS DISTINCT FROM 'done'
        AND NOT EXISTS (SELECT 1 FROM project_field_completion_applications a WHERE a.project_id=NEW.project_id AND a.task_id=NEW.id
            AND a.expected_project_revision=root.updated_at AND a.expected_task_revision=OLD.updated_at
            AND a.creation_outer_xid=pg_current_xact_id() AND project_field_completion_covered(a.id))
        THEN RAISE EXCEPTION 'Use accepted field completion for enrolled task scope'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER project_task_field_completion_status_guard BEFORE UPDATE ON project_tasks FOR EACH ROW EXECUTE FUNCTION project_task_field_completion_status_guard()"
    )


def downgrade() -> None:
    raise RuntimeError(
        "Project requirements and applied completion evidence must be retained; forward-fix only"
    )
