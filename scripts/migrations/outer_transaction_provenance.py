"""Exact nullable xid8 extensions and server-stamped outer transaction provenance."""

from enum import StrEnum

from sqlalchemy import Connection, text


class CreationParentTable(StrEnum):
    outage_revision = "outage_scope_revisions"
    outage_ticket_link = "outage_incident_ticket_links"
    support_request = "support_resolution_requests"
    replacement_decision = "completion_replacement_decisions"
    project_application = "project_field_completion_applications"
    criteria_revision = "completion_criteria_revisions"
    submission = "completion_submissions"
    review_decision = "completion_review_decisions"


def verify_optional_outer_transaction_column(
    bind: Connection, *, table_name: CreationParentTable
) -> bool:
    """Accept absence or precisely nullable pg_catalog.xid8 without any default."""
    if not isinstance(table_name, CreationParentTable):
        raise TypeError("Creation parent must be a fixed migration-owned table")
    row = bind.execute(
        text(
            "SELECT a.atttypid='pg_catalog.xid8'::regtype, a.attnotnull, "
            "a.attidentity, a.attgenerated, EXISTS(SELECT 1 FROM pg_attrdef d "
            "WHERE d.adrelid=a.attrelid AND d.adnum=a.attnum) "
            "FROM pg_attribute a WHERE a.attrelid=to_regclass(:name) "
            "AND a.attname='creation_outer_xid' AND a.attnum>0 AND NOT a.attisdropped"
        ),
        {"name": table_name},
    ).first()
    if row is None:
        return False
    if tuple(row) != (True, False, "", "", False):
        raise RuntimeError(f"Invalid outer transaction provenance column: {table_name}")
    return True


def install_outer_transaction_provenance(
    bind: Connection, *, table_names: tuple[CreationParentTable, ...]
) -> None:
    """Stamp inserts unconditionally; never backfill historical parents or allow edits."""
    bind.execute(
        text("""
CREATE OR REPLACE FUNCTION stamp_creation_outer_xid() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  NEW.creation_outer_xid:=pg_current_xact_id();
 ELSIF NEW.creation_outer_xid IS DISTINCT FROM OLD.creation_outer_xid THEN
  RAISE EXCEPTION 'creation outer transaction provenance is immutable';
 END IF;
 RETURN NEW;
END; $$;
""")
    )
    for name in table_names:
        if not isinstance(name, CreationParentTable):
            raise TypeError("Creation parent must be a fixed migration-owned table")
        if not verify_optional_outer_transaction_column(bind, table_name=name):
            bind.execute(
                text(f'ALTER TABLE "{name}" ADD COLUMN creation_outer_xid xid8 NULL')
            )
        bind.execute(
            text(
                f'CREATE TRIGGER stamp_creation_outer_xid BEFORE INSERT OR UPDATE ON "{name}" '
                "FOR EACH ROW EXECUTE FUNCTION stamp_creation_outer_xid()"
            )
        )
