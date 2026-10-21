"""Universal explicit completion enrollment and immutable review; no acceptance backfill.

Unpublished677 is refactored before adoption. Frozen adoption shapes match the
real fresh and revision-674 predecessor rehearsals; precreated tables must be
empty and match their columns, constraints and indexes exactly.
"""

from alembic import op
from scripts.migrations.completion_schema_contract import COMPLETION_TABLES
from scripts.migrations.engineering_schema_contract import verify_precreated_tables
from scripts.migrations.outer_transaction_provenance import CreationParentTable

revision: str = "677_field_completion_review"
down_revision: str | None = "676_project_work_order_issuance"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_precreated_tables(
        op.get_bind(),
        expected=COMPLETION_TABLES,
        outer_transaction_extensions=(
            CreationParentTable.criteria_revision,
            CreationParentTable.submission,
            CreationParentTable.review_decision,
        ),
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS stored_file_retentions (\n\tstored_file_id UUID NOT NULL, \n\treference_id UUID NOT NULL, \n\tsource_owner VARCHAR(80) NOT NULL, \n\tPRIMARY KEY (stored_file_id, reference_id), \n\tFOREIGN KEY(stored_file_id) REFERENCES stored_files (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_review_policies (\n\tid UUID NOT NULL, \n\tscope_type VARCHAR(12) NOT NULL, \n\tscope_id UUID NOT NULL, \n\tproject_id UUID, \n\twork_order_id UUID, \n\trevision INTEGER NOT NULL, \n\tindependent_reviewer_required BOOLEAN NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (scope_type, scope_id, revision), \n\tUNIQUE (scope_type, scope_id, id), \n\tCHECK ((scope_type='project' AND project_id IS NOT NULL AND scope_id=project_id AND work_order_id IS NULL) OR (scope_type='work_order' AND work_order_id IS NOT NULL AND scope_id=work_order_id AND project_id IS NULL)), \n\tCHECK (revision>0 AND length(trim(reason))>0), \n\tFOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_review_policy_heads (\n\tscope_type VARCHAR(12) NOT NULL, \n\tscope_id UUID NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\trevision_id UUID NOT NULL, \n\tPRIMARY KEY (scope_type, scope_id), \n\tFOREIGN KEY(scope_type, scope_id, revision_id) REFERENCES completion_review_policies (scope_type, scope_id, id) ON DELETE RESTRICT, \n\tCHECK (generation>0)\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_enrollments (\n\tid UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\tpublic_id VARCHAR(64) NOT NULL, \n\tprovenance VARCHAR(24) NOT NULL, \n\tengineering_issuance_id UUID, \n\tproject_id UUID, \n\ttask_id UUID, \n\tsubscriber_id UUID, \n\torigin_ticket_id UUID, \n\tpolicy_scope_type VARCHAR(12) NOT NULL, \n\tpolicy_scope_id UUID NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tenrolled_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (work_order_id, id), \n\tFOREIGN KEY(policy_scope_type, policy_scope_id) REFERENCES completion_review_policy_heads (scope_type, scope_id) ON DELETE RESTRICT, \n\tCHECK ((provenance='engineering_issuance' AND engineering_issuance_id IS NOT NULL AND project_id IS NOT NULL) OR (provenance='native_work_order' AND engineering_issuance_id IS NULL)), \n\tCHECK ((policy_scope_type='project' AND policy_scope_id=project_id AND project_id IS NOT NULL) OR (policy_scope_type='work_order' AND policy_scope_id=work_order_id)), \n\tCHECK (length(trim(reason)) > 0), \n\tUNIQUE (work_order_id), \n\tFOREIGN KEY(work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tUNIQUE (engineering_issuance_id), \n\tFOREIGN KEY(engineering_issuance_id) REFERENCES project_work_order_issuances (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(task_id) REFERENCES project_tasks (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(subscriber_id) REFERENCES subscribers (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(origin_ticket_id) REFERENCES support_tickets (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_criteria_revisions (\n\tid UUID NOT NULL, \n\tenrollment_id UUID NOT NULL, \n\trevision INTEGER NOT NULL, \n\tengineering_revision_id UUID, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (enrollment_id, id), \n\tUNIQUE (enrollment_id, revision), \n\tCHECK (revision>0 AND length(trim(reason))>0), \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(engineering_revision_id) REFERENCES engineering_plan_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_criteria_heads (\n\tenrollment_id UUID NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\trevision_id UUID NOT NULL, \n\tPRIMARY KEY (enrollment_id), \n\tFOREIGN KEY(enrollment_id, revision_id) REFERENCES completion_criteria_revisions (enrollment_id, id) ON DELETE RESTRICT, \n\tCHECK (generation>0), \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        'CREATE TABLE IF NOT EXISTS completion_criteria (\n\trevision_id UUID NOT NULL, \n\tid UUID NOT NULL, \n\t"order" INTEGER NOT NULL, \n\tstatement TEXT NOT NULL, \n\tverification_method TEXT NOT NULL, \n\tPRIMARY KEY (revision_id, id), \n\tUNIQUE (revision_id, "order"), \n\tCHECK (length(trim(statement))>0 AND length(trim(verification_method))>0 AND "order">=0), \n\tFOREIGN KEY(revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT\n)'
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_submissions (\n\tid UUID NOT NULL, \n\tenrollment_id UUID NOT NULL, \n\tpolicy_revision_id UUID NOT NULL, \n\tcriteria_revision_id UUID NOT NULL, \n\tcompleted_event_id UUID NOT NULL, \n\tassignment_generation UUID NOT NULL, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID, \n\tactor_vendor_user_id UUID, \n\treason TEXT NOT NULL, \n\tsubmitted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (id, criteria_revision_id), \n\tFOREIGN KEY(enrollment_id, criteria_revision_id) REFERENCES completion_criteria_revisions (enrollment_id, id) ON DELETE RESTRICT, \n\tUNIQUE (enrollment_id, id), \n\tCHECK (length(trim(reason)) > 0), \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(criteria_revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(completed_event_id) REFERENCES field_job_events (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_vendor_user_id) REFERENCES field_vendor_users (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_observations (\n\tsubmission_id UUID NOT NULL, \n\tcriterion_id UUID NOT NULL, \n\tcriteria_revision_id UUID NOT NULL, \n\tobservation TEXT NOT NULL, \n\tPRIMARY KEY (submission_id, criterion_id), \n\tFOREIGN KEY(submission_id, criteria_revision_id) REFERENCES completion_submissions (id, criteria_revision_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(criteria_revision_id, criterion_id) REFERENCES completion_criteria (revision_id, id) ON DELETE RESTRICT, \n\tCHECK (length(trim(observation)) > 0)\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_evidence_files (\n\tsubmission_id UUID NOT NULL, \n\tcriterion_id UUID NOT NULL, \n\tattachment_id UUID NOT NULL, \n\tstored_file_id UUID NOT NULL, \n\tchecksum VARCHAR(64) NOT NULL, \n\tPRIMARY KEY (submission_id, criterion_id, attachment_id), \n\tFOREIGN KEY(submission_id, criterion_id) REFERENCES completion_observations (submission_id, criterion_id) ON DELETE RESTRICT, \n\tCHECK (length(checksum) = 64), \n\tFOREIGN KEY(attachment_id) REFERENCES field_attachments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(stored_file_id) REFERENCES stored_files (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_completion_evidence_files_stored_file_id ON completion_evidence_files (stored_file_id)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_review_decisions (\n\tid UUID NOT NULL, \n\tenrollment_id UUID NOT NULL, \n\tsubmission_id UUID NOT NULL, \n\tpolicy_revision_id UUID NOT NULL, \n\tstate VARCHAR(12) NOT NULL, \n\trevokes_id UUID, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (enrollment_id, id), \n\tFOREIGN KEY(enrollment_id, submission_id) REFERENCES completion_submissions (enrollment_id, id) ON DELETE RESTRICT, \n\tCHECK (state IN ('accepted','rejected','revoked') AND length(trim(reason)) > 0 AND ((state='revoked') = (revokes_id IS NOT NULL))), \n\tFOREIGN KEY(policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(revokes_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_criterion_results (\n\tdecision_id UUID NOT NULL, \n\tcriterion_id UUID NOT NULL, \n\tsubmission_id UUID NOT NULL, \n\tverdict VARCHAR(8) NOT NULL, \n\treason TEXT NOT NULL, \n\tPRIMARY KEY (decision_id, criterion_id), \n\tFOREIGN KEY(submission_id, criterion_id) REFERENCES completion_observations (submission_id, criterion_id) ON DELETE RESTRICT, \n\tCHECK (verdict IN ('passed','failed') AND length(trim(reason)) > 0), \n\tFOREIGN KEY(decision_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_review_heads (\n\tenrollment_id UUID NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tsubmission_id UUID NOT NULL, \n\tdecision_id UUID, \n\tPRIMARY KEY (enrollment_id), \n\tFOREIGN KEY(enrollment_id, submission_id) REFERENCES completion_submissions (enrollment_id, id) ON DELETE RESTRICT, \n\tFOREIGN KEY(enrollment_id, decision_id) REFERENCES completion_review_decisions (enrollment_id, id) ON DELETE RESTRICT, \n\tCHECK (generation > 0), \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT\n)"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS completion_review_receipts (\n\tidempotency_key VARCHAR(120) NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tenrollment_id UUID, \n\tcriteria_revision_id UUID, \n\tcompleted_event_id UUID, \n\tcriteria_generation INTEGER NOT NULL, \n\tpolicy_revision_id UUID, \n\tsubmission_id UUID, \n\tdecision_id UUID, \n\tgeneration INTEGER NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tPRIMARY KEY (idempotency_key), \n\tCHECK (length(trim(idempotency_key)) > 0 AND length(fingerprint)=64 AND generation >= 0), \n\tFOREIGN KEY(enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(criteria_revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(completed_event_id) REFERENCES field_job_events (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(submission_id) REFERENCES completion_submissions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(decision_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT\n)"
    )
    op.execute("""CREATE FUNCTION completion_review_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'completion review history is immutable'; END; $$""")
    for table in (
        "completion_enrollments",
        "completion_criteria_revisions",
        "completion_criteria",
        "stored_file_retentions",
        "completion_review_policies",
        "completion_submissions",
        "completion_observations",
        "completion_evidence_files",
        "completion_review_decisions",
        "completion_criterion_results",
        "completion_review_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER completion_review_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION completion_review_immutable()"
        )
    op.execute("""CREATE FUNCTION completion_evidence_check() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE actual_file UUID;
    BEGIN
        SELECT stored_file_id INTO actual_file FROM field_attachments WHERE id=NEW.attachment_id;
        IF actual_file IS DISTINCT FROM NEW.stored_file_id THEN RAISE EXCEPTION 'completion evidence file mismatch'; END IF;
        PERFORM 1 FROM stored_files WHERE id=NEW.stored_file_id FOR UPDATE;
        IF NOT EXISTS (SELECT 1 FROM field_attachments a JOIN stored_files f ON f.id=a.stored_file_id
            JOIN completion_submissions s ON s.id=NEW.submission_id
            JOIN completion_enrollments i ON i.id=s.enrollment_id
            WHERE a.id=NEW.attachment_id AND a.is_active AND a.work_order_mirror_id=i.work_order_id
            AND NOT f.is_deleted AND f.checksum=NEW.checksum AND f.entity_type='field_attachment'
            AND f.entity_id=i.public_id) THEN RAISE EXCEPTION 'completion evidence unavailable'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        """CREATE TRIGGER completion_evidence_check BEFORE INSERT ON completion_evidence_files FOR EACH ROW EXECUTE FUNCTION completion_evidence_check()"""
    )
    op.execute("""CREATE FUNCTION completion_file_retained() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM stored_file_retentions WHERE stored_file_id=OLD.id) THEN
            RAISE EXCEPTION 'completion evidence must be retained';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        """CREATE TRIGGER completion_file_retained BEFORE UPDATE OR DELETE ON stored_files FOR EACH ROW EXECUTE FUNCTION completion_file_retained()"""
    )
    op.execute("""CREATE FUNCTION completion_attachment_retained() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM 1 FROM stored_files WHERE id=OLD.stored_file_id FOR UPDATE;
        IF EXISTS (SELECT 1 FROM stored_file_retentions WHERE stored_file_id=OLD.stored_file_id) THEN
            RAISE EXCEPTION 'completion evidence attachment must be retained';
        END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        """CREATE TRIGGER completion_attachment_retained BEFORE UPDATE OR DELETE ON field_attachments FOR EACH ROW EXECUTE FUNCTION completion_attachment_retained()"""
    )
    op.execute("""CREATE FUNCTION completion_submission_event_lock() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        PERFORM 1 FROM field_job_events WHERE id=NEW.completed_event_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'completion field event unavailable'; END IF;
        IF NOT EXISTS (SELECT 1 FROM field_job_events f JOIN completion_enrollments e ON e.work_order_id=f.work_order_mirror_id
            WHERE f.id=NEW.completed_event_id AND e.id=NEW.enrollment_id AND f.event='complete' AND f.new_status='completed'
                AND f.assignment_generation=NEW.assignment_generation AND f.system_user_id=NEW.actor_user_id
                AND f.author_vendor_user_id IS NOT DISTINCT FROM NEW.actor_vendor_user_id)
        THEN RAISE EXCEPTION 'completion field event lineage mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_submission_event_lock BEFORE INSERT ON completion_submissions FOR EACH ROW EXECUTE FUNCTION completion_submission_event_lock()"
    )
    op.execute("""CREATE FUNCTION completion_submission_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM completion_enrollments e
            JOIN completion_criteria_revisions c ON c.enrollment_id=e.id AND c.id=NEW.criteria_revision_id
            JOIN completion_review_policies p ON p.id=NEW.policy_revision_id AND p.scope_type=e.policy_scope_type AND p.scope_id=e.policy_scope_id
            JOIN field_job_events f ON f.id=NEW.completed_event_id AND f.work_order_mirror_id=e.work_order_id
            WHERE e.id=NEW.enrollment_id AND f.event='complete' AND f.new_status='completed'
                AND f.assignment_generation=NEW.assignment_generation AND f.system_user_id=NEW.actor_user_id
                AND f.author_vendor_user_id IS NOT DISTINCT FROM NEW.actor_vendor_user_id)
            OR (SELECT count(*) FROM completion_observations WHERE submission_id=NEW.id)=0
            OR (SELECT count(*) FROM completion_observations WHERE submission_id=NEW.id) <>
               (SELECT count(*) FROM completion_criteria WHERE revision_id=NEW.criteria_revision_id)
        THEN RAISE EXCEPTION 'completion submission criteria/event lineage mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        """CREATE CONSTRAINT TRIGGER completion_submission_complete AFTER INSERT ON completion_submissions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION completion_submission_complete()"""
    )
    op.execute("""CREATE FUNCTION completion_decision_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE required_count INTEGER; actual_count INTEGER; failed_count INTEGER;
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.enrollment_id=NEW.enrollment_id AND s.policy_revision_id=NEW.policy_revision_id)
        THEN RAISE EXCEPTION 'completion decision lineage mismatch'; END IF;
        SELECT count(*), count(*) FILTER (WHERE verdict='failed') INTO actual_count,failed_count FROM completion_criterion_results WHERE decision_id=NEW.id;
        SELECT count(*) INTO required_count FROM completion_observations WHERE submission_id=NEW.submission_id;
        IF NEW.state='revoked' THEN
            IF actual_count<>0 OR NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.revokes_id AND d.enrollment_id=NEW.enrollment_id AND d.submission_id=NEW.submission_id AND d.state IN ('accepted','rejected'))
            THEN RAISE EXCEPTION 'completion revocation mismatch'; END IF;
        ELSIF actual_count<>required_count OR required_count=0 OR EXISTS (SELECT 1 FROM completion_criterion_results r WHERE r.decision_id=NEW.id AND r.submission_id<>NEW.submission_id)
           OR ((NEW.state='accepted') <> (failed_count=0)) THEN RAISE EXCEPTION 'completion decision criteria mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        """CREATE CONSTRAINT TRIGGER completion_decision_complete AFTER INSERT ON completion_review_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION completion_decision_complete()"""
    )

    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('915e1be8-5ba1-5ad7-91bc-ae9491cc98d6', 'operations:completion-review:read', 'Read native issued completion review', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )
    op.execute(
        "INSERT INTO permissions (id, key, description, is_active, is_ui_assignable, created_at, updated_at) VALUES ('712260a1-d43d-57c5-b847-b99b7e0a7284', 'operations:completion-review:write', 'Publish policy and review native issued completion', true, true, now(), now()) ON CONFLICT (key) DO NOTHING"
    )

    op.execute("""CREATE FUNCTION completion_child_creation_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_TABLE_NAME='completion_criterion_results' THEN
            IF NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.decision_id AND d.submission_id=NEW.submission_id AND d.xmin=(txid_current() % 4294967296)::text::xid)
            THEN RAISE EXCEPTION 'criterion results require atomic decision creation'; END IF;
        ELSE
            IF NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.xmin=(txid_current() % 4294967296)::text::xid)
            THEN RAISE EXCEPTION 'completion evidence requires atomic submission creation'; END IF;
        END IF;
        RETURN NEW;
    END; $$""")
    for table in (
        "completion_observations",
        "completion_evidence_files",
        "completion_criterion_results",
    ):
        op.execute(
            f"CREATE TRIGGER completion_child_creation_only BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION completion_child_creation_only()"
        )
    op.execute("""CREATE FUNCTION completion_head_check() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'completion heads must be retained'; END IF;
        IF TG_OP='UPDATE' AND (NEW.enrollment_id IS DISTINCT FROM OLD.enrollment_id OR NEW.generation<>OLD.generation+1)
        THEN RAISE EXCEPTION 'completion head generation mismatch'; END IF;
        IF TG_OP='INSERT' AND NEW.generation<>1 THEN RAISE EXCEPTION 'completion head initial generation mismatch'; END IF;
        IF NEW.decision_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.decision_id AND d.enrollment_id=NEW.enrollment_id AND d.submission_id=NEW.submission_id)
        THEN RAISE EXCEPTION 'completion head decision mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_head_check BEFORE INSERT OR UPDATE OR DELETE ON completion_review_heads FOR EACH ROW EXECUTE FUNCTION completion_head_check()"
    )

    op.execute("""CREATE FUNCTION completion_receipt_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.enrollment_id IS NULL THEN
            IF NEW.policy_revision_id IS NULL OR NEW.submission_id IS NOT NULL OR NEW.decision_id IS NOT NULL OR NEW.state<>'legacy_not_reviewed'
            THEN RAISE EXCEPTION 'completion policy receipt lineage mismatch'; END IF;
        ELSE
            IF NOT EXISTS (SELECT 1 FROM completion_criteria_revisions c WHERE c.id=NEW.criteria_revision_id AND c.enrollment_id=NEW.enrollment_id AND c.revision=NEW.criteria_generation)
            THEN RAISE EXCEPTION 'completion criteria receipt lineage mismatch'; END IF;
            IF NEW.submission_id IS NULL THEN
                IF NEW.decision_id IS NOT NULL OR NEW.state<>'legacy_not_reviewed' THEN RAISE EXCEPTION 'completion enrollment receipt mismatch'; END IF;
            ELSE
                IF NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.enrollment_id=NEW.enrollment_id AND s.policy_revision_id=NEW.policy_revision_id AND s.criteria_revision_id=NEW.criteria_revision_id AND s.completed_event_id=NEW.completed_event_id)
                THEN RAISE EXCEPTION 'completion submission receipt lineage mismatch'; END IF;
                IF NEW.decision_id IS NULL THEN
                    IF NEW.state<>'submitted' THEN RAISE EXCEPTION 'completion submission receipt state mismatch'; END IF;
                ELSIF NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.decision_id AND d.enrollment_id=NEW.enrollment_id AND d.submission_id=NEW.submission_id AND d.policy_revision_id=NEW.policy_revision_id AND d.state=NEW.state)
                THEN RAISE EXCEPTION 'completion decision receipt lineage mismatch'; END IF;
            END IF;
        END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_receipt_lineage BEFORE INSERT ON completion_review_receipts FOR EACH ROW EXECUTE FUNCTION completion_receipt_lineage()"
    )
    op.execute("""CREATE FUNCTION completion_enrollment_check() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM work_order w WHERE w.id=NEW.work_order_id AND w.public_id=NEW.public_id
            AND w.project_id IS NOT DISTINCT FROM NEW.project_id AND w.project_task_id IS NOT DISTINCT FROM NEW.task_id
            AND w.subscriber_id IS NOT DISTINCT FROM NEW.subscriber_id AND w.origin_ticket_id IS NOT DISTINCT FROM NEW.origin_ticket_id)
        THEN RAISE EXCEPTION 'completion enrollment native scope mismatch'; END IF;
        IF NEW.provenance='engineering_issuance' AND NOT EXISTS (SELECT 1 FROM project_work_order_issuances i WHERE i.id=NEW.engineering_issuance_id AND i.work_order_id=NEW.work_order_id AND i.project_id=NEW.project_id AND i.task_id IS NOT DISTINCT FROM NEW.task_id)
        THEN RAISE EXCEPTION 'completion enrollment engineering provenance mismatch'; END IF;
        IF NEW.provenance='native_work_order' AND EXISTS (SELECT 1 FROM project_work_order_issuances i WHERE i.work_order_id=NEW.work_order_id)
        THEN RAISE EXCEPTION 'completion enrollment cannot bypass issued engineering'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_enrollment_check BEFORE INSERT ON completion_enrollments FOR EACH ROW EXECUTE FUNCTION completion_enrollment_check()"
    )
    op.execute("""CREATE FUNCTION completion_criteria_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE e completion_enrollments; required_count INTEGER;
    BEGIN
        SELECT * INTO e FROM completion_enrollments WHERE id=NEW.enrollment_id;
        SELECT count(*) INTO required_count FROM completion_criteria WHERE revision_id=NEW.id;
        IF required_count=0 OR EXISTS (SELECT 1 FROM completion_criteria c WHERE c.revision_id=NEW.id AND (c."order"<0 OR c."order">=required_count))
        THEN RAISE EXCEPTION 'completion criteria revision incomplete'; END IF;
        IF e.provenance='native_work_order' AND NEW.engineering_revision_id IS NOT NULL THEN RAISE EXCEPTION 'native criteria engineering provenance forbidden'; END IF;
        IF e.provenance='engineering_issuance' AND (NEW.revision<>1 OR NOT EXISTS (SELECT 1 FROM project_work_order_issuances i WHERE i.id=e.engineering_issuance_id AND i.revision_id=NEW.engineering_revision_id)
            OR required_count<>(SELECT count(*) FROM engineering_plan_criteria WHERE revision_id=NEW.engineering_revision_id)
            OR EXISTS (SELECT 1 FROM completion_criteria c WHERE c.revision_id=NEW.id AND NOT EXISTS (SELECT 1 FROM engineering_plan_criteria a WHERE a.revision_id=NEW.engineering_revision_id AND a.id=c.id AND a."order"=c."order" AND a.statement=c.statement AND a.verification_method=c.verification_method)))
        THEN RAISE EXCEPTION 'completion criteria must retain exact issued engineering'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER completion_criteria_complete AFTER INSERT ON completion_criteria_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION completion_criteria_complete()"
    )
    op.execute("""CREATE FUNCTION completion_criteria_child_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM completion_criteria_revisions c WHERE c.id=NEW.revision_id AND c.xmin::text::bigint=(txid_current() % 4294967296))
        THEN RAISE EXCEPTION 'completion criteria rows may only be created with revision'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_criteria_child_only BEFORE INSERT ON completion_criteria FOR EACH ROW EXECUTE FUNCTION completion_criteria_child_only()"
    )
    op.execute("""CREATE FUNCTION completion_event_retained() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF EXISTS (SELECT 1 FROM completion_submissions WHERE completed_event_id=OLD.id) THEN RAISE EXCEPTION 'submitted completion event is immutable'; END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_event_retained BEFORE UPDATE OR DELETE ON field_job_events FOR EACH ROW EXECUTE FUNCTION completion_event_retained()"
    )

    op.execute("""CREATE FUNCTION completion_revision_head_check() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'completion revision heads must be retained'; END IF;
        IF TG_OP='INSERT' AND NEW.generation<>1 THEN RAISE EXCEPTION 'completion revision head initial generation mismatch'; END IF;
        IF TG_OP='UPDATE' AND NEW.generation<>OLD.generation+1 THEN RAISE EXCEPTION 'completion revision head generation mismatch'; END IF;
        IF TG_TABLE_NAME='completion_criteria_heads' THEN
            IF TG_OP='UPDATE' AND NEW.enrollment_id<>OLD.enrollment_id THEN RAISE EXCEPTION 'completion criteria enrollment immutable'; END IF;
            IF NOT EXISTS (SELECT 1 FROM completion_criteria_revisions r WHERE r.id=NEW.revision_id AND r.enrollment_id=NEW.enrollment_id AND r.revision=NEW.generation)
            THEN RAISE EXCEPTION 'completion criteria head revision mismatch'; END IF;
        ELSE
            IF TG_OP='UPDATE' AND (NEW.scope_type<>OLD.scope_type OR NEW.scope_id<>OLD.scope_id) THEN RAISE EXCEPTION 'completion policy scope immutable'; END IF;
            IF NOT EXISTS (SELECT 1 FROM completion_review_policies p WHERE p.id=NEW.revision_id AND p.scope_type=NEW.scope_type AND p.scope_id=NEW.scope_id AND p.revision=NEW.generation)
            THEN RAISE EXCEPTION 'completion policy head revision mismatch'; END IF;
        END IF;
        RETURN NEW;
    END; $$""")
    for table in ("completion_criteria_heads", "completion_review_policy_heads"):
        op.execute(
            f"CREATE TRIGGER completion_revision_head_check BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION completion_revision_head_check()"
        )


def downgrade() -> None:
    raise RuntimeError("Completion evidence must be retained; forward-fix only")
