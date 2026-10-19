"""Sub-owned Party context after genuine unchanged Kernel105 entry.

Independent history; deliberately absent from ordinary Sub version_locations.
"""

from sqlalchemy import text

from alembic import op

revision = "ska0001_party_context"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from scripts.migration.kernel_lineage_entry import KernelEntryObserved
    from scripts.migration.kernel_party_import import require_canonical_entry

    connection = op.get_bind()
    observed = op.get_context().config.attributes.get("canonical_entry")
    if not isinstance(observed, KernelEntryObserved):
        raise RuntimeError("Exact typed canonical entry observation required")
    require_canonical_entry(connection, observed=observed)
    op.execute("""
        CREATE TABLE public.sub_party_context (
            tenant_id uuid NOT NULL,
            party_id uuid NOT NULL,
            status VARCHAR(24) NOT NULL,
            data_classification VARCHAR(32) NOT NULL,
            merged_into_party_id uuid NULL,
            merge_reason TEXT NULL,
            metadata JSON NULL,
            PRIMARY KEY (tenant_id,party_id),
            CONSTRAINT fk_sub_party_context_party FOREIGN KEY (tenant_id,party_id)
                REFERENCES public.parties(tenant_id,id),
            CONSTRAINT fk_sub_party_context_merge FOREIGN KEY (tenant_id,merged_into_party_id)
                REFERENCES public.parties(tenant_id,id),
            CONSTRAINT ck_sub_party_context_status CHECK (status IN ('active','quarantined','merged','archived')),
            CONSTRAINT ck_sub_party_context_classification CHECK (data_classification IN ('production','test','imported_unverified')),
            CONSTRAINT ck_sub_party_context_not_self CHECK (merged_into_party_id IS NULL OR merged_into_party_id <> party_id),
            CONSTRAINT ck_sub_party_context_merge_required CHECK (
                (status='merged' AND merged_into_party_id IS NOT NULL) OR
                (status<>'merged' AND merged_into_party_id IS NULL))
        )
    """)
    op.execute("ALTER TABLE public.sub_party_context ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.sub_party_context FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY sub_party_context_tenant ON public.sub_party_context
        USING (tenant_id = public.app_current_tenant_id())
        WITH CHECK (tenant_id = public.app_current_tenant_id())""")
    # Default privileges can grant newly created tables: explicitly revoke all
    # non-owner grantees, including PUBLIC, before any imported context exists.
    connection.exec_driver_sql("REVOKE ALL ON public.sub_party_context FROM PUBLIC")
    roles = tuple(
        connection.scalars(
            text("""
        SELECT DISTINCT r.rolname FROM pg_class c
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) a
        JOIN pg_roles r ON r.oid=a.grantee
        WHERE c.oid='public.sub_party_context'::regclass AND a.grantee<>c.relowner
    """)
        )
    )
    quote = connection.dialect.identifier_preparer.quote
    for role in roles:
        connection.exec_driver_sql(
            f"REVOKE ALL ON public.sub_party_context FROM {quote(role)}"
        )


def downgrade() -> None:
    connection = op.get_bind()
    # Never mistake an RLS-hidden populated table for an empty destination.
    # PostgreSQL refuses this read for roles unable to bypass forced RLS.
    connection.execute(text("SELECT set_config('row_security','off',true)"))
    if connection.scalar(text("SELECT count(*) FROM public.sub_party_context")):
        raise RuntimeError("Populated Party context downgrade refused")
    op.drop_table("sub_party_context", schema="public")
