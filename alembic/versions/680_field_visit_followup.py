"""Accountable failed visits; additive immutable evidence, no inferred appointments.

Five-second lock and 120-second statement budgets. Retry whole migration on
contention. Exact empty fresh-baseline adoption only; no failure backfill.
"""

from alembic import op
from scripts.migrations.followup_schema_contract import verify_followup_adoption

revision = "680_field_visit_followup"
down_revision = "679_reviewed_failed_scope_replacement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")
    verify_followup_adoption(op.get_bind())
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS field_visit_followups (\n\tid UUID NOT NULL, \n\twork_order_id UUID NOT NULL, \n\tfield_event_id UUID, \n\trejected_decision_id UUID, \n\tcurrent_sequence INTEGER NOT NULL, \n\tPRIMARY KEY (id), \n\tCHECK ((field_event_id IS NOT NULL) <> (rejected_decision_id IS NOT NULL)), \n\tCHECK (current_sequence > 0), \n\tFOREIGN KEY(work_order_id) REFERENCES work_order (id) ON DELETE RESTRICT, \n\tUNIQUE (field_event_id), \n\tFOREIGN KEY(field_event_id) REFERENCES field_job_events (id) ON DELETE RESTRICT, \n\tUNIQUE (rejected_decision_id), \n\tFOREIGN KEY(rejected_decision_id) REFERENCES completion_review_decisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS field_visit_followup_revisions (\n\tid UUID NOT NULL, \n\tobligation_id UUID NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tdisposition VARCHAR(24) NOT NULL, \n\tresponsible_team_id UUID NOT NULL, \n\tresponsible_system_user_id UUID, \n\tresponsible_person_id UUID, \n\tdue_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\treason TEXT NOT NULL, \n\treplacement_decision_id UUID, \n\tactor_user_id UUID NOT NULL, \n\tactor_person_id UUID NOT NULL, \n\tdecided_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (obligation_id, sequence), \n\tCHECK (sequence > 0), \n\tCHECK (disposition IN ('triage','revisit_required','no_revisit')), \n\tCHECK (length(trim(reason)) > 0), \n\tCHECK ((responsible_system_user_id IS NULL) = (responsible_person_id IS NULL)), \n\tCHECK (replacement_decision_id IS NULL OR disposition = 'revisit_required'), \n\tFOREIGN KEY(obligation_id) REFERENCES field_visit_followups (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(responsible_team_id) REFERENCES service_teams (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(responsible_system_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(responsible_person_id) REFERENCES parties (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(replacement_decision_id) REFERENCES completion_replacement_decisions (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_user_id) REFERENCES system_users (id) ON DELETE RESTRICT, \n\tFOREIGN KEY(actor_person_id) REFERENCES parties (id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE IF NOT EXISTS field_visit_followup_receipts (\n\tintent_key UUID NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\trevision_id UUID NOT NULL, \n\tPRIMARY KEY (intent_key), \n\tCHECK (length(fingerprint) = 64), \n\tFOREIGN KEY(revision_id) REFERENCES field_visit_followup_revisions (id) ON DELETE RESTRICT\n)\n\n"
    )
    for table in ("field_visit_followup_revisions", "field_visit_followup_receipts"):
        op.execute(
            f"CREATE TRIGGER field_visit_followup_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION completion_review_immutable()"
        )
    op.execute("""CREATE FUNCTION field_visit_followup_source() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'followup evidence cannot be deleted'; END IF;
        IF TG_OP='UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id OR NEW.work_order_id IS DISTINCT FROM OLD.work_order_id
            OR NEW.field_event_id IS DISTINCT FROM OLD.field_event_id
            OR NEW.rejected_decision_id IS DISTINCT FROM OLD.rejected_decision_id
            OR NEW.current_sequence <> OLD.current_sequence+1
            THEN RAISE EXCEPTION 'followup source immutable and head must advance once'; END IF;
            RETURN NEW;
        END IF;
        IF NEW.current_sequence <> 1 THEN RAISE EXCEPTION 'followup starts at revision one'; END IF;
        IF NEW.field_event_id IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM field_job_events WHERE id=NEW.field_event_id
                AND event='unable_to_complete' AND work_order_mirror_id=NEW.work_order_id)
            THEN RAISE EXCEPTION 'followup exact unable source mismatch'; END IF;
        ELSE
            IF NOT EXISTS (SELECT 1 FROM completion_review_decisions d JOIN completion_enrollments e ON e.id=d.enrollment_id
                WHERE d.id=NEW.rejected_decision_id AND d.state='rejected' AND e.work_order_id=NEW.work_order_id)
            THEN RAISE EXCEPTION 'followup exact rejected source mismatch'; END IF;
        END IF;
        RETURN NEW;
    END; $$""")
    op.execute(
        "CREATE TRIGGER field_visit_followup_source BEFORE INSERT OR UPDATE OR DELETE ON field_visit_followups FOR EACH ROW EXECUTE FUNCTION field_visit_followup_source()"
    )
    op.execute("""CREATE FUNCTION field_visit_followup_complete() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE target uuid; head integer;
    BEGIN
        IF TG_TABLE_NAME='field_visit_followups' THEN target=NEW.id; ELSE target=NEW.obligation_id; END IF;
        SELECT current_sequence INTO STRICT head FROM field_visit_followups WHERE id=target;
        IF (SELECT count(*) FROM field_visit_followup_revisions WHERE obligation_id=target) <> head
        OR (SELECT max(sequence) FROM field_visit_followup_revisions WHERE obligation_id=target) <> head
        OR NOT EXISTS (SELECT 1 FROM field_visit_followup_revisions WHERE obligation_id=target AND sequence=head)
        THEN RAISE EXCEPTION 'followup revision head incomplete'; END IF;
        RETURN NEW;
    END; $$""")
    for table in ("field_visit_followups", "field_visit_followup_revisions"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER field_visit_followup_complete AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION field_visit_followup_complete()"
        )


def downgrade() -> None:
    raise RuntimeError(
        "Migration680 retains immutable accountability evidence; forward-fix only"
    )
