"""Reviewed native and exactly equivalent engineering failed-scope replacement; immutable lineage, no failure rewrite.

Additive expand only, no acceptance or replacement backfill. Five-second lock
and 120-second statement budgets; retry the whole migration on contention.
Existing requests retain their original basis. Downgrade refuses evidence loss;
forward-fix only. Fresh-baseline adoption requires empty exact frozen shapes.
"""

from alembic import op
from scripts.migrations.replacement_schema_contract import verify_replacement_adoption

revision: str = "679_reviewed_failed_scope_replacement"
down_revision: str | None = "678_support_resolution_completion_basis"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_replacement_adoption(op.get_bind())
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS completion_replacement_decisions (\n\tid UUID NOT NULL, \n\tsource_enrollment_id UUID NOT NULL, \n\treplacement_enrollment_id UUID NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tstate VARCHAR(12) NOT NULL, \n\trevokes_id UUID, \n\tsource_criteria_revision_id UUID NOT NULL, \n\treplacement_criteria_revision_id UUID NOT NULL, \n\tsource_policy_revision_id UUID NOT NULL, \n\treplacement_policy_revision_id UUID NOT NULL, \n\tsource_assignment_generation UUID NOT NULL, \n\treplacement_assignment_generation UUID NOT NULL, \n\tfailure_event_id UUID, \n\tfailure_submission_id UUID, \n\tfailure_decision_id UUID, \n\tfailure_review_generation INTEGER, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\treason TEXT NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (source_enrollment_id, generation), \n\tCHECK (source_enrollment_id <> replacement_enrollment_id AND generation > 0 AND length(trim(reason)) > 0), \n\tCHECK (state IN ('approved','revoked') AND ((state='revoked') = (revokes_id IS NOT NULL))), \n\tCHECK ((failure_event_id IS NOT NULL AND failure_submission_id IS NULL AND failure_decision_id IS NULL AND failure_review_generation IS NULL) OR (failure_event_id IS NULL AND failure_submission_id IS NOT NULL AND failure_decision_id IS NOT NULL AND failure_review_generation > 0)), \n\tFOREIGN KEY(source_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(replacement_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(revokes_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(source_criteria_revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(replacement_criteria_revision_id) REFERENCES completion_criteria_revisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(source_policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(replacement_policy_revision_id) REFERENCES completion_review_policies (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(failure_event_id) REFERENCES field_job_events (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(failure_submission_id) REFERENCES completion_submissions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(failure_decision_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS completion_replacement_coverage (\n\tdecision_id UUID NOT NULL, \n\tsource_criterion_id UUID NOT NULL, \n\treplacement_criterion_id UUID NOT NULL, \n\tPRIMARY KEY (decision_id, source_criterion_id), \n\tUNIQUE (decision_id, replacement_criterion_id), \n\tFOREIGN KEY(decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS completion_replacement_heads (\n\tsource_enrollment_id UUID NOT NULL, \n\treplacement_enrollment_id UUID, \n\tdecision_id UUID NOT NULL, \n\tgeneration INTEGER NOT NULL, \n\tPRIMARY KEY (source_enrollment_id), \n\tCHECK (generation > 0 AND source_enrollment_id <> replacement_enrollment_id), \n\tFOREIGN KEY(source_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tUNIQUE (replacement_enrollment_id), \n\tFOREIGN KEY(replacement_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS completion_replacement_receipts (\n\tidempotency_key VARCHAR(120) NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tdecision_id UUID NOT NULL, \n\tPRIMARY KEY (idempotency_key), \n\tCHECK (length(trim(idempotency_key)) > 0 AND length(fingerprint) = 64), \n\tFOREIGN KEY(decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS support_resolution_effective_bases (\n\trequest_id UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\teffective_work_order_id UUID NOT NULL, \n\teffective_enrollment_id UUID NOT NULL, \n\tPRIMARY KEY (request_id, work_order_id), \n\tFOREIGN KEY(request_id, work_order_id) REFERENCES support_resolution_bases (request_id, work_order_id) ON DELETE RESTRICT, \n\tFOREIGN KEY(effective_work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(effective_enrollment_id) REFERENCES completion_enrollments (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS support_resolution_replacement_lineage (\n\trequest_id UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\tposition INTEGER NOT NULL, \n\tdecision_id UUID NOT NULL, \n\tPRIMARY KEY (request_id, work_order_id, position), \n\tFOREIGN KEY(request_id, work_order_id) REFERENCES support_resolution_effective_bases (request_id, work_order_id) ON DELETE RESTRICT, \n\tCHECK (position >= 0), \n\tUNIQUE (request_id, work_order_id, decision_id), \n\tFOREIGN KEY(decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    for table in (
        "completion_replacement_decisions",
        "completion_replacement_coverage",
        "completion_replacement_receipts",
        "support_resolution_effective_bases",
        "support_resolution_replacement_lineage",
    ):
        op.execute(
            f"CREATE TRIGGER completion_replacement_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION completion_review_immutable()"
        )
    op.execute("""CREATE FUNCTION completion_replacement_decision_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE source completion_enrollments; replacement completion_enrollments;
    BEGIN
        SELECT * INTO STRICT source FROM completion_enrollments WHERE id=NEW.source_enrollment_id;
        SELECT * INTO STRICT replacement FROM completion_enrollments WHERE id=NEW.replacement_enrollment_id;
        PERFORM 1 FROM support_tickets WHERE id=source.origin_ticket_id FOR UPDATE;
        IF source.origin_ticket_id IS DISTINCT FROM replacement.origin_ticket_id
        OR source.subscriber_id IS DISTINCT FROM replacement.subscriber_id
        OR source.project_id IS DISTINCT FROM replacement.project_id
        OR source.task_id IS DISTINCT FROM replacement.task_id
        OR source.provenance IS DISTINCT FROM replacement.provenance
        THEN RAISE EXCEPTION 'replacement exact scope mismatch'; END IF;
        IF source.provenance='native_work_order' THEN
            IF source.origin_ticket_id IS NULL OR source.subscriber_id IS NULL
            THEN RAISE EXCEPTION 'replacement exact native ticket scope mismatch'; END IF;
        ELSIF source.provenance='engineering_issuance' AND source.project_id IS NOT NULL THEN
            PERFORM 1 FROM projects WHERE id=source.project_id FOR UPDATE;
            IF source.task_id IS NOT NULL THEN
                PERFORM 1 FROM project_tasks WHERE id=source.task_id AND project_id=source.project_id FOR UPDATE;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM project_work_order_issuances a JOIN project_work_order_issuances b ON
                a.project_id=b.project_id AND a.task_id IS NOT DISTINCT FROM b.task_id AND a.plan_id=b.plan_id
                AND a.revision_id=b.revision_id AND a.approval_id=b.approval_id AND a.digest=b.digest
                AND a.infrastructure_type=b.infrastructure_type AND a.infrastructure_id=b.infrastructure_id
                AND a.purpose=b.purpose AND a.technology=b.technology
                WHERE a.id=source.engineering_issuance_id AND b.id=replacement.engineering_issuance_id
                AND a.work_order_id=source.work_order_id AND a.public_id=source.public_id
                AND b.work_order_id=replacement.work_order_id AND b.public_id=replacement.public_id
                AND a.project_id=source.project_id AND a.task_id IS NOT DISTINCT FROM source.task_id)
            THEN RAISE EXCEPTION 'replacement exact engineering issuance equivalence required'; END IF;
            IF NEW.state='approved' AND NOT EXISTS (
                SELECT 1 FROM project_work_order_issuances a JOIN project_engineering_plans p ON p.id=a.plan_id
                JOIN engineering_plan_decisions d ON d.id=a.approval_id AND d.plan_id=p.id
                JOIN engineering_policy_head policy ON policy.project_id=a.project_id
                JOIN engineering_policy_revisions approval_policy ON approval_policy.id=d.policy_revision_id AND approval_policy.project_id=a.project_id
                JOIN work_order wa ON wa.id=source.work_order_id JOIN work_order wb ON wb.id=replacement.work_order_id
                JOIN completion_criteria_revisions ca ON ca.id=NEW.source_criteria_revision_id AND ca.enrollment_id=source.id
                JOIN completion_criteria_revisions cb ON cb.id=NEW.replacement_criteria_revision_id AND cb.enrollment_id=replacement.id
                WHERE a.id=source.engineering_issuance_id AND p.current_approval_id=a.approval_id
                AND d.kind='approve' AND d.revision_id=a.revision_id AND d.digest=a.digest
                AND policy.current_revision_id IS NOT NULL
                AND ca.engineering_revision_id=a.revision_id AND cb.engineering_revision_id=a.revision_id
                AND wa.project_id=source.project_id AND wa.project_task_id IS NOT DISTINCT FROM source.task_id
                AND wb.project_id=replacement.project_id AND wb.project_task_id IS NOT DISTINCT FROM replacement.task_id
                AND wa.origin_ticket_id IS NOT DISTINCT FROM source.origin_ticket_id AND wb.origin_ticket_id IS NOT DISTINCT FROM replacement.origin_ticket_id
                AND wa.subscriber_id IS NOT DISTINCT FROM source.subscriber_id AND wb.subscriber_id IS NOT DISTINCT FROM replacement.subscriber_id
                AND wa.purpose=a.purpose AND wb.purpose=a.purpose AND wa.access_technology=a.technology AND wb.access_technology=a.technology
                AND wa.work_order_kind='infrastructure' AND wb.work_order_kind='infrastructure'
                AND (SELECT count(*) FROM engineering_plan_criteria WHERE revision_id=a.revision_id)>0
                AND (SELECT count(*) FROM completion_criteria WHERE revision_id=ca.id)=(SELECT count(*) FROM engineering_plan_criteria WHERE revision_id=a.revision_id)
                AND (SELECT count(*) FROM completion_criteria WHERE revision_id=cb.id)=(SELECT count(*) FROM engineering_plan_criteria WHERE revision_id=a.revision_id)
                AND NOT EXISTS (SELECT 1 FROM completion_criteria c LEFT JOIN engineering_plan_criteria canonical ON
                    canonical.revision_id=a.revision_id AND canonical.id=c.id
                    WHERE c.revision_id IN (ca.id,cb.id) AND (canonical.id IS NULL OR
                        (c."order",c.statement,c.verification_method) IS DISTINCT FROM
                        (canonical."order",canonical.statement,canonical.verification_method))))
            THEN RAISE EXCEPTION 'replacement current engineering approval and canonical criteria required'; END IF;
        ELSE RAISE EXCEPTION 'replacement unsupported provenance';
        END IF;
        IF NEW.state='revoked' THEN
            IF NOT EXISTS (SELECT 1 FROM completion_replacement_decisions d JOIN completion_replacement_heads h ON h.decision_id=d.id
                WHERE d.id=NEW.revokes_id AND d.state='approved' AND d.source_enrollment_id=NEW.source_enrollment_id
                AND d.replacement_enrollment_id=NEW.replacement_enrollment_id AND h.replacement_enrollment_id IS NOT NULL
                AND h.generation+1=NEW.generation)
            THEN RAISE EXCEPTION 'replacement revocation lineage mismatch'; END IF;
            RETURN NEW;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM completion_criteria_heads h WHERE h.enrollment_id=source.id AND h.revision_id=NEW.source_criteria_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads h WHERE h.enrollment_id=replacement.id AND h.revision_id=NEW.replacement_criteria_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_review_policy_heads h WHERE h.scope_type=source.policy_scope_type AND h.scope_id=source.policy_scope_id AND h.revision_id=NEW.source_policy_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_review_policy_heads h WHERE h.scope_type=replacement.policy_scope_type AND h.scope_id=replacement.policy_scope_id AND h.revision_id=NEW.replacement_policy_revision_id)
        THEN RAISE EXCEPTION 'replacement current criteria or policy mismatch'; END IF;
        IF NEW.failure_event_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM field_job_events f JOIN work_order w ON w.id=f.work_order_mirror_id
                WHERE f.id=NEW.failure_event_id AND f.work_order_mirror_id=source.work_order_id
                AND f.event='unable_to_complete' AND f.new_status='canceled' AND w.status='canceled'
                AND f.assignment_generation=NEW.source_assignment_generation
                AND f.id=(SELECT id FROM field_job_events WHERE work_order_mirror_id=source.work_order_id ORDER BY received_at DESC,id DESC LIMIT 1))
            THEN RAISE EXCEPTION 'replacement authoritative failure event mismatch'; END IF;
        ELSE
            IF NOT EXISTS (SELECT 1 FROM completion_review_heads h JOIN completion_review_decisions d ON d.id=h.decision_id
                JOIN completion_submissions s ON s.id=h.submission_id
                WHERE h.enrollment_id=source.id AND h.submission_id=NEW.failure_submission_id
                AND h.decision_id=NEW.failure_decision_id AND h.generation=NEW.failure_review_generation
                AND d.state='rejected' AND d.submission_id=s.id AND d.enrollment_id=source.id
                AND s.criteria_revision_id=NEW.source_criteria_revision_id AND s.policy_revision_id=NEW.source_policy_revision_id
                AND s.assignment_generation=NEW.source_assignment_generation)
            THEN RAISE EXCEPTION 'replacement exact rejected submission mismatch'; END IF;
        END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_replacement_decision_lineage BEFORE INSERT ON completion_replacement_decisions FOR EACH ROW EXECUTE FUNCTION completion_replacement_decision_lineage()"
    )
    op.execute("""CREATE FUNCTION completion_replacement_coverage_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NEW.state='approved' AND EXISTS (SELECT 1 FROM completion_enrollments e
            WHERE e.id=NEW.source_enrollment_id AND e.provenance='engineering_issuance')
            AND EXISTS (SELECT 1 FROM completion_replacement_coverage WHERE decision_id=NEW.id
                AND source_criterion_id<>replacement_criterion_id)
        THEN RAISE EXCEPTION 'engineering replacement requires identity criterion coverage'; END IF;
        IF NEW.state='approved' AND (
            NOT EXISTS (SELECT 1 FROM completion_criteria WHERE revision_id=NEW.source_criteria_revision_id)
            OR (SELECT count(*) FROM completion_replacement_coverage WHERE decision_id=NEW.id)
                <> (SELECT count(*) FROM completion_criteria WHERE revision_id=NEW.source_criteria_revision_id)
            OR EXISTS (SELECT 1 FROM completion_replacement_coverage c WHERE c.decision_id=NEW.id AND (
                NOT EXISTS (SELECT 1 FROM completion_criteria WHERE revision_id=NEW.source_criteria_revision_id AND id=c.source_criterion_id)
                OR NOT EXISTS (SELECT 1 FROM completion_criteria WHERE revision_id=NEW.replacement_criteria_revision_id AND id=c.replacement_criterion_id)))
        ) THEN RAISE EXCEPTION 'replacement requires complete exact criterion coverage'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER completion_replacement_coverage_complete AFTER INSERT ON completion_replacement_decisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION completion_replacement_coverage_complete()"
    )
    op.execute("""CREATE FUNCTION completion_replacement_coverage_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM completion_replacement_decisions d WHERE d.id=NEW.decision_id
            AND d.state='approved' AND d.xmin=(txid_current() % 4294967296)::text::xid
            AND EXISTS (SELECT 1 FROM completion_criteria WHERE revision_id=d.source_criteria_revision_id AND id=NEW.source_criterion_id)
            AND EXISTS (SELECT 1 FROM completion_criteria WHERE revision_id=d.replacement_criteria_revision_id AND id=NEW.replacement_criterion_id))
        THEN RAISE EXCEPTION 'replacement coverage must accompany exact decision creation'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_replacement_coverage_lineage BEFORE INSERT ON completion_replacement_coverage FOR EACH ROW EXECUTE FUNCTION completion_replacement_coverage_lineage()"
    )
    op.execute("""CREATE FUNCTION completion_replacement_head_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE decision completion_replacement_decisions;
    BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'replacement heads must retain revocation history'; END IF;
        SELECT * INTO STRICT decision FROM completion_replacement_decisions WHERE id=NEW.decision_id;
        PERFORM 1 FROM support_tickets t JOIN completion_enrollments e ON e.origin_ticket_id=t.id WHERE e.id=NEW.source_enrollment_id FOR UPDATE OF t;
        PERFORM 1 FROM projects p JOIN completion_enrollments e ON e.project_id=p.id
            WHERE e.id=NEW.source_enrollment_id AND e.provenance='engineering_issuance' FOR UPDATE OF p;
        IF decision.source_enrollment_id<>NEW.source_enrollment_id OR decision.generation<>NEW.generation
        OR (decision.state='approved' AND decision.replacement_enrollment_id IS DISTINCT FROM NEW.replacement_enrollment_id)
        OR (decision.state='revoked' AND NEW.replacement_enrollment_id IS NOT NULL)
        OR decision.xmin<>(txid_current() % 4294967296)::text::xid
        OR (TG_OP='INSERT' AND NEW.generation<>1)
        OR (TG_OP='UPDATE' AND (NEW.source_enrollment_id<>OLD.source_enrollment_id OR NEW.generation<>OLD.generation+1
            OR (OLD.replacement_enrollment_id IS NOT NULL AND decision.state='approved')))
        THEN RAISE EXCEPTION 'replacement head lineage mismatch'; END IF;
        IF NEW.replacement_enrollment_id IS NOT NULL AND EXISTS (
            WITH RECURSIVE chain(id) AS (
                SELECT NEW.replacement_enrollment_id UNION
                SELECT h.replacement_enrollment_id FROM completion_replacement_heads h JOIN chain c ON h.source_enrollment_id=c.id WHERE h.replacement_enrollment_id IS NOT NULL
            ) SELECT 1 FROM chain WHERE id=NEW.source_enrollment_id)
        THEN RAISE EXCEPTION 'replacement chain cycle'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER completion_replacement_head_lineage BEFORE INSERT OR UPDATE OR DELETE ON completion_replacement_heads FOR EACH ROW EXECUTE FUNCTION completion_replacement_head_lineage()"
    )
    op.execute("""CREATE FUNCTION completion_receipt_unique_intent() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF (TG_TABLE_NAME='completion_replacement_receipts' AND EXISTS (SELECT 1 FROM completion_review_receipts WHERE idempotency_key=NEW.idempotency_key))
        OR (TG_TABLE_NAME='completion_review_receipts' AND EXISTS (SELECT 1 FROM completion_replacement_receipts WHERE idempotency_key=NEW.idempotency_key))
        THEN RAISE EXCEPTION 'completion receipt intent already exists'; END IF;
        RETURN NEW;
    END; $$""")
    for table in ("completion_review_receipts", "completion_replacement_receipts"):
        op.execute(
            f"CREATE TRIGGER completion_receipt_unique_intent BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION completion_receipt_unique_intent()"
        )
    # Final graph/basis is validated at transaction end, after its immutable child rows.
    op.execute(
        "DROP TRIGGER support_resolution_basis_lineage ON support_resolution_bases"
    )
    op.execute("""CREATE OR REPLACE FUNCTION support_resolution_basis_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE effective_enrollment UUID; effective_order UUID; cursor UUID; entry RECORD; count_lineage INTEGER:=0;
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM support_resolution_requests r JOIN work_order w ON w.origin_ticket_id=r.ticket_id
            JOIN completion_enrollments e ON e.work_order_id=w.id WHERE r.id=NEW.request_id AND w.id=NEW.work_order_id
            AND e.id=NEW.enrollment_id AND r.xmin=(txid_current() % 4294967296)::text::xid)
        THEN RAISE EXCEPTION 'support resolution original required scope mismatch'; END IF;
        SELECT b.effective_enrollment_id,b.effective_work_order_id INTO effective_enrollment,effective_order
            FROM support_resolution_effective_bases b WHERE b.request_id=NEW.request_id AND b.work_order_id=NEW.work_order_id;
        cursor:=NEW.enrollment_id;
        FOR entry IN SELECT l.position,d.*,h.decision_id AS current_decision,h.replacement_enrollment_id AS current_replacement
            FROM support_resolution_replacement_lineage l JOIN completion_replacement_decisions d ON d.id=l.decision_id
            JOIN completion_replacement_heads h ON h.source_enrollment_id=d.source_enrollment_id
            WHERE l.request_id=NEW.request_id AND l.work_order_id=NEW.work_order_id ORDER BY l.position
        LOOP
            IF entry.position<>count_lineage OR entry.source_enrollment_id<>cursor OR entry.state<>'approved'
            OR entry.current_decision<>entry.id OR entry.current_replacement IS DISTINCT FROM entry.replacement_enrollment_id
            THEN RAISE EXCEPTION 'support resolution replacement lineage mismatch'; END IF;
            cursor:=entry.replacement_enrollment_id;
            count_lineage:=count_lineage+1;
        END LOOP;
        IF effective_enrollment IS NULL THEN
            effective_enrollment:=NEW.enrollment_id; effective_order:=NEW.work_order_id;
            IF count_lineage<>0 THEN RAISE EXCEPTION 'support replacement endpoint missing'; END IF;
        ELSIF count_lineage=0 OR cursor<>effective_enrollment THEN
            RAISE EXCEPTION 'support replacement ordered endpoint mismatch';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM completion_enrollments WHERE id=effective_enrollment AND work_order_id=effective_order)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_revisions WHERE id=NEW.criteria_revision_id AND enrollment_id=effective_enrollment)
        OR NOT EXISTS (SELECT 1 FROM completion_submissions s WHERE s.id=NEW.submission_id AND s.enrollment_id=effective_enrollment
            AND s.criteria_revision_id=NEW.criteria_revision_id AND s.policy_revision_id=NEW.policy_revision_id
            AND s.assignment_generation=NEW.assignment_generation AND s.completed_event_id=NEW.completed_event_id)
        OR NOT EXISTS (SELECT 1 FROM completion_review_decisions d WHERE d.id=NEW.accepted_decision_id AND d.enrollment_id=effective_enrollment
            AND d.submission_id=NEW.submission_id AND d.policy_revision_id=NEW.policy_revision_id AND d.state='accepted')
        OR NOT EXISTS (SELECT 1 FROM completion_review_heads h WHERE h.enrollment_id=effective_enrollment AND h.submission_id=NEW.submission_id
            AND h.decision_id=NEW.accepted_decision_id AND h.generation=NEW.review_generation)
        OR NOT EXISTS (SELECT 1 FROM completion_criteria_heads WHERE enrollment_id=effective_enrollment AND revision_id=NEW.criteria_revision_id)
        OR NOT EXISTS (SELECT 1 FROM completion_enrollments e JOIN completion_review_policy_heads h ON h.scope_type=e.policy_scope_type
            AND h.scope_id=e.policy_scope_id WHERE e.id=effective_enrollment AND h.revision_id=NEW.policy_revision_id)
        OR NOT EXISTS (SELECT 1 FROM field_job_events e WHERE e.id=NEW.completed_event_id AND e.work_order_mirror_id=effective_order
            AND e.event='complete' AND e.new_status='completed' AND e.assignment_generation=NEW.assignment_generation)
        THEN RAISE EXCEPTION 'support effective accepted completion evidence mismatch'; END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER support_resolution_basis_lineage AFTER INSERT ON support_resolution_bases DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION support_resolution_basis_lineage()"
    )
    op.execute("""CREATE FUNCTION support_resolution_replacement_child_lineage() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM support_resolution_requests WHERE id=NEW.request_id
            AND xmin=(txid_current() % 4294967296)::text::xid)
        THEN RAISE EXCEPTION 'support replacement lineage must accompany request creation'; END IF;
        RETURN NEW;
    END; $$""")
    for table in (
        "support_resolution_effective_bases",
        "support_resolution_replacement_lineage",
    ):
        op.execute(
            f"CREATE TRIGGER support_resolution_replacement_child_lineage BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION support_resolution_replacement_child_lineage()"
        )


def downgrade() -> None:
    raise RuntimeError(
        "Reviewed replacement and confirmation lineage must be retained; forward-fix only"
    )
