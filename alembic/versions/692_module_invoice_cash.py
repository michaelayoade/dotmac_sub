"""Prospective immutable module account binding and invoice cash correlation.

Revision ID: 692_module_invoice_cash
Revises: 691_payments_intent_correlation

Expand-only source candidate. No backfill, confirmation, posting or authority flip.
The referenced account/settlement composite keys exist in released Billing a1;
this revision deliberately does not claim published Billing a8 acceptance.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text

from alembic import op

revision: str = "692_module_invoice_cash"
down_revision: str = "691_payments_intent_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str = "bi_0001_billing"


@dataclass(frozen=True, slots=True)
class ColumnShape:
    name: str
    sql_type: str
    not_null: bool = True


def _require_known_empty_table(
    *,
    table: str,
    columns: tuple[ColumnShape, ...],
    constraints: tuple[str, ...],
    create_sql: str,
) -> None:
    """Adopt only the exact empty local shape emitted by the squashed base.

    This is catalog validation, never metadata-based acceptance. Partial earlier
    installs, extra columns/defaults/constraints/indexes/policies/triggers/rules,
    partition/inheritance shapes and populated prospective tables refuse.
    """
    connection = op.get_bind()
    qualified = f"public.{table}"
    relation = connection.execute(
        text("""
        SELECT c.oid, c.relkind, c.relpersistence, c.relispartition,
               c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class c WHERE c.oid = pg_catalog.to_regclass(:relation)
    """),
        {"relation": qualified},
    ).one_or_none()
    if relation is None:
        op.execute(create_sql)
        return
    op.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
    # Re-observe under the lock rather than trusting the pre-lock name/OID.
    locked_relation = connection.execute(
        text("""
        SELECT c.oid, c.relkind, c.relpersistence, c.relispartition,
               c.relrowsecurity, c.relforcerowsecurity
        FROM pg_catalog.pg_class c WHERE c.oid = pg_catalog.to_regclass(:relation)
    """),
        {"relation": qualified},
    ).one()
    if tuple(locked_relation) != tuple(relation) or tuple(locked_relation[1:]) != (
        "r",
        "p",
        False,
        False,
        False,
    ):
        raise RuntimeError(f"Unexpected prospective relation shape: {qualified}")
    actual_columns = connection.execute(
        text("""
        SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
               a.attidentity, a.attgenerated, a.attislocal, a.attinhcount,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid)
        FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = :oid AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
    """),
        {"oid": relation.oid},
    ).all()
    expected_columns = [
        (c.name, c.sql_type, c.not_null, "", "", True, 0, None) for c in columns
    ]
    if [tuple(row) for row in actual_columns] != expected_columns:
        raise RuntimeError(f"Unexpected prospective columns: {qualified}")
    actual_constraints = connection.execute(
        text("""
        SELECT pg_catalog.pg_get_constraintdef(c.oid, false), c.condeferrable,
               c.condeferred, c.convalidated, c.conislocal, c.coninhcount
        FROM pg_catalog.pg_constraint c WHERE c.conrelid = :oid
        ORDER BY pg_catalog.pg_get_constraintdef(c.oid, false)
    """),
        {"oid": relation.oid},
    ).all()
    expected_constraints = [
        (definition, False, False, True, True, 0) for definition in sorted(constraints)
    ]
    if [tuple(row) for row in actual_constraints] != expected_constraints:
        raise RuntimeError(f"Unexpected prospective constraints: {qualified}")
    # Reuse Email 638's reviewed ordinary internal RI-trigger qualification,
    # including referenced-side actions identified by the local FK OID.
    unsupported = connection.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits WHERE inhrelid = :oid OR inhparent = :oid)
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_trigger WHERE tgrelid = :oid AND NOT tgisinternal)
            OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_trigger tr
                WHERE tr.tgrelid = :oid AND tr.tgisinternal AND NOT EXISTS (
                    SELECT 1 FROM pg_catalog.pg_constraint c
                    WHERE c.oid = tr.tgconstraint AND c.conrelid = :oid AND c.contype = 'f'
                )
            )
            OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_constraint c
                WHERE c.conrelid = :oid AND c.contype = 'f' AND NOT (
                    (SELECT count(*) FROM pg_catalog.pg_trigger tr WHERE tr.tgconstraint = c.oid) = 4
                    AND (SELECT count(*) FROM pg_catalog.pg_trigger tr
                        JOIN pg_catalog.pg_proc f ON f.oid = tr.tgfoid
                        JOIN pg_catalog.pg_namespace n ON n.oid = f.pronamespace
                        WHERE tr.tgconstraint = c.oid AND tr.tgisinternal AND tr.tgenabled = 'O'
                        AND tr.tgnargs = 0 AND octet_length(tr.tgargs) = 0 AND tr.tgqual IS NULL
                        AND tr.tgattr = ''::int2vector AND NOT tr.tgdeferrable AND NOT tr.tginitdeferred
                        AND n.nspname = 'pg_catalog' AND (
                            (tr.tgrelid = c.conrelid AND tr.tgconstrrelid = c.confrelid AND (
                                (tr.tgtype = 5 AND f.proname = 'RI_FKey_check_ins') OR
                                (tr.tgtype = 17 AND f.proname = 'RI_FKey_check_upd')
                            )) OR
                            (tr.tgrelid = c.confrelid AND tr.tgconstrrelid = c.conrelid AND (
                                (tr.tgtype = 9 AND f.proname = 'RI_FKey_restrict_del') OR
                                (tr.tgtype = 17 AND f.proname = 'RI_FKey_noaction_upd')
                            ))
                        )
                    ) = 4
                )
            )
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_policy WHERE polrelid = :oid)
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite WHERE ev_class = :oid)
            OR EXISTS (SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid = :oid AND attisdropped)
            OR EXISTS (
                SELECT 1 FROM pg_catalog.pg_index i
                JOIN pg_catalog.pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_catalog.pg_am am ON am.oid = ic.relam
                WHERE i.indrelid = :oid AND (
                    NOT (i.indisunique AND i.indisvalid AND i.indisready AND i.indislive AND i.indimmediate)
                    OR i.indpred IS NOT NULL OR i.indexprs IS NOT NULL OR am.amname <> 'btree'
                    OR i.indnkeyatts <> i.indnatts OR i.indnullsnotdistinct
                    OR i.indisclustered OR i.indisreplident
                    OR EXISTS (
                        SELECT 1 FROM unnest(i.indkey::smallint[], i.indcollation::oid[], i.indclass::oid[], i.indoption::smallint[])
                        AS key(attnum, collation_oid, opclass_oid, options)
                        JOIN pg_catalog.pg_attribute a ON a.attrelid = :oid AND a.attnum = key.attnum
                        JOIN pg_catalog.pg_opclass oc ON oc.oid = key.opclass_oid
                        WHERE key.options <> 0 OR key.collation_oid <> a.attcollation
                            OR NOT oc.opcdefault OR oc.opcnamespace <> 'pg_catalog'::pg_catalog.regnamespace
                    )
                    OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid = :oid AND k.conindid = i.indexrelid AND k.contype IN ('p','u'))
                )
            )
    """),
        {"oid": relation.oid},
    )
    populated = connection.scalar(
        text(f"SELECT EXISTS (SELECT 1 FROM public.{table} LIMIT 1)")
    )
    if unsupported or populated:
        raise RuntimeError(f"Unexpected prospective catalog or population: {qualified}")


def _install_occurrence_column() -> None:
    connection = op.get_bind()
    op.execute("LOCK TABLE public.payment_provider_events IN ACCESS EXCLUSIVE MODE")
    column = connection.execute(
        text("""
        SELECT pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull,
               a.attidentity, a.attgenerated, a.attislocal, a.attinhcount,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attnum
        FROM pg_catalog.pg_attribute a
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = 'public.payment_provider_events'::pg_catalog.regclass
          AND a.attname = 'occurred_at' AND NOT a.attisdropped
    """)
    ).one_or_none()
    if column is None:
        op.execute(
            "ALTER TABLE public.payment_provider_events ADD COLUMN occurred_at timestamptz"
        )
        return
    if tuple(column[:-1]) != ("timestamp with time zone", False, "", "", True, 0, None):
        raise RuntimeError("Unexpected prospective provider occurrence column")
    unexpected = connection.scalar(
        text("""
        SELECT EXISTS (SELECT 1 FROM public.payment_provider_events WHERE occurred_at IS NOT NULL)
        OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_constraint
            WHERE conrelid = 'public.payment_provider_events'::pg_catalog.regclass
              AND :number = ANY(conkey)
        ) OR EXISTS (
            SELECT 1 FROM pg_catalog.pg_depend d
            WHERE d.refobjid = 'public.payment_provider_events'::pg_catalog.regclass
              AND d.refobjsubid = :number
              AND d.classid = 'pg_catalog.pg_class'::pg_catalog.regclass
        )
    """),
        {"number": column.attnum},
    )
    if unexpected:
        raise RuntimeError(
            "Unexpected prospective provider occurrence facts or constraints"
        )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    original_search_path = op.get_bind().scalar(
        text("SELECT current_setting('search_path')")
    )
    # pg_get_constraintdef must qualify public references deterministically.
    op.execute("SET LOCAL search_path = pg_catalog")
    _install_occurrence_column()
    _require_known_empty_table(
        table="module_billing_account_bindings",
        columns=(
            ColumnShape("tenant_id", "uuid"),
            ColumnShape("account_id", "uuid"),
            ColumnShape("currency", "character varying(3)"),
            ColumnShape("billing_account_id", "uuid"),
        ),
        constraints=(
            "PRIMARY KEY (tenant_id, account_id, currency)",
            "UNIQUE (tenant_id, billing_account_id)",
            "FOREIGN KEY (account_id) REFERENCES public.subscribers(id) ON DELETE RESTRICT",
        ),
        create_sql="""
        CREATE TABLE public.module_billing_account_bindings (
            tenant_id uuid NOT NULL, account_id uuid NOT NULL REFERENCES public.subscribers(id) ON DELETE RESTRICT,
            currency varchar(3) NOT NULL, billing_account_id uuid NOT NULL,
            PRIMARY KEY (tenant_id, account_id, currency),
            CONSTRAINT uq_module_billing_binding_target UNIQUE (tenant_id, billing_account_id)
        )
        """,
    )
    _require_known_empty_table(
        table="module_invoice_cash_correlations",
        columns=tuple(
            ColumnShape(name, sql_type)
            for name, sql_type in (
                ("tenant_id", "uuid"),
                ("observation_id", "uuid"),
                ("topup_intent_id", "uuid"),
                ("module_intent_id", "uuid"),
                ("confirmation_id", "uuid"),
                ("account_id", "uuid"),
                ("billing_account_id", "uuid"),
                ("settlement_id", "uuid"),
                ("source_system", "character varying(120)"),
                ("source_settlement_key", "character varying(255)"),
                ("currency", "character varying(3)"),
                ("observation_digest", "character varying(64)"),
                ("occurred_at", "timestamp with time zone"),
                ("observed_at", "timestamp with time zone"),
            )
        ),
        constraints=(
            "PRIMARY KEY (tenant_id, observation_id)",
            "UNIQUE (tenant_id, confirmation_id)",
            "UNIQUE (tenant_id, settlement_id)",
            "UNIQUE (tenant_id, source_system, source_settlement_key)",
            "FOREIGN KEY (observation_id) REFERENCES public.payment_provider_events(id) ON DELETE RESTRICT",
            "FOREIGN KEY (topup_intent_id) REFERENCES public.topup_intents(id) ON DELETE RESTRICT",
        ),
        create_sql="""
        CREATE TABLE public.module_invoice_cash_correlations (
            tenant_id uuid NOT NULL, observation_id uuid NOT NULL REFERENCES public.payment_provider_events(id) ON DELETE RESTRICT,
            topup_intent_id uuid NOT NULL REFERENCES public.topup_intents(id) ON DELETE RESTRICT,
            module_intent_id uuid NOT NULL, confirmation_id uuid NOT NULL,
            account_id uuid NOT NULL, billing_account_id uuid NOT NULL, settlement_id uuid NOT NULL,
            source_system varchar(120) NOT NULL, source_settlement_key varchar(255) NOT NULL,
            currency varchar(3) NOT NULL, observation_digest varchar(64) NOT NULL,
            occurred_at timestamptz NOT NULL, observed_at timestamptz NOT NULL,
            PRIMARY KEY (tenant_id, observation_id),
            CONSTRAINT uq_module_invoice_cash_confirmation UNIQUE (tenant_id, confirmation_id),
            CONSTRAINT uq_module_invoice_cash_settlement UNIQUE (tenant_id, settlement_id),
            CONSTRAINT uq_module_invoice_cash_source UNIQUE (tenant_id, source_system, source_settlement_key)
        )
        """,
    )
    op.get_bind().execute(
        text("SELECT set_config('search_path', :original, true)"),
        {"original": original_search_path},
    )
    for table, name, columns, target, target_columns in (
        (
            "module_billing_account_bindings",
            "fk_module_billing_binding_account",
            "tenant_id, billing_account_id",
            "mod_billing.billing_accounts",
            "tenant_id, id",
        ),
        (
            "module_invoice_cash_correlations",
            "fk_module_invoice_cash_account",
            "tenant_id, account_id, currency",
            "public.module_billing_account_bindings",
            "tenant_id, account_id, currency",
        ),
        (
            "module_invoice_cash_correlations",
            "fk_module_invoice_cash_settlement",
            "tenant_id, settlement_id, billing_account_id",
            "mod_billing.confirmed_settlements",
            "tenant_id, id, billing_account_id",
        ),
        (
            "module_invoice_cash_correlations",
            "fk_module_invoice_cash_intent",
            "tenant_id, module_intent_id",
            "mod_payments.payment_intents",
            "tenant_id, id",
        ),
        (
            "module_invoice_cash_correlations",
            "fk_module_invoice_cash_confirmation",
            "tenant_id, confirmation_id",
            "mod_payments.payment_confirmations",
            "tenant_id, id",
        ),
    ):
        op.execute(
            f"ALTER TABLE public.{table} ADD CONSTRAINT {name} FOREIGN KEY ({columns}) "
            f"REFERENCES {target} ({target_columns}) ON DELETE RESTRICT"
        )
    op.execute("""
        CREATE FUNCTION public.guard_module_invoice_cash_append_only() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'module invoice cash identity is append-only' USING ERRCODE = 'restrict_violation';
        END $$
    """)
    for table in (
        "module_billing_account_bindings",
        "module_invoice_cash_correlations",
    ):
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON public.{table} "
            "USING (tenant_id = public.app_current_tenant_id()) "
            "WITH CHECK (tenant_id = public.app_current_tenant_id())"
        )
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_module_invoice_cash_append_only()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON public.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION public.guard_module_invoice_cash_append_only()"
        )
        # app_user receives no writer grant. Actual login/owner separation and
        # binding/cash admission belong to the pending installation handoff.
        op.execute(f"REVOKE ALL ON public.{table} FROM PUBLIC, app_user")
        op.execute(f"GRANT SELECT ON public.{table} TO app_user")
    op.execute("""
        CREATE FUNCTION public.guard_module_invoice_cash_binding() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            PERFORM 1 FROM public.payment_provider_events WHERE id = NEW.observation_id FOR UPDATE;
            IF NOT EXISTS (
                SELECT 1 FROM public.module_billing_account_bindings b
                JOIN mod_billing.billing_accounts a ON a.tenant_id = b.tenant_id AND a.id = b.billing_account_id
                WHERE b.tenant_id = NEW.tenant_id AND b.account_id = NEW.account_id
                AND b.currency = NEW.currency AND b.billing_account_id = NEW.billing_account_id
                AND a.currency = NEW.currency
            ) OR NOT EXISTS (
                SELECT 1 FROM public.payment_provider_events e
                JOIN public.topup_intents t ON t.id = NEW.topup_intent_id
                JOIN mod_payments.payment_confirmations c ON c.tenant_id = NEW.tenant_id AND c.id = NEW.confirmation_id
                WHERE e.id = NEW.observation_id AND e.provider_id = t.provider_id
                AND e.provider_reference = t.reference AND e.external_id = c.external_reference
                AND e.source IN ('verified_webhook', 'gateway_reconciliation')
                AND e.observed_payment_status = 'succeeded'
                AND e.financial_effect = 'none'
                AND e.observation_digest = NEW.observation_digest
                AND e.occurred_at = NEW.occurred_at AND e.received_at = NEW.observed_at
                AND t.module_intent_tenant_id = NEW.tenant_id AND t.module_intent_id = NEW.module_intent_id
                AND t.account_id = NEW.account_id AND t.currency = NEW.currency
                AND c.intent_id = NEW.module_intent_id
                AND c.provider_type = upper(t.provider_type)
                AND c.currency_code = e.currency AND c.confirmed_amount = e.amount
                AND NEW.source_settlement_key = e.external_id
            ) OR NOT EXISTS (
                SELECT 1 FROM mod_billing.confirmed_settlements s
                WHERE s.tenant_id = NEW.tenant_id AND s.id = NEW.settlement_id
                AND s.billing_account_id = NEW.billing_account_id
                AND s.source_system = NEW.source_system AND s.source_settlement_key = NEW.source_settlement_key
                AND s.currency = NEW.currency
            ) THEN
                RAISE EXCEPTION 'module invoice cash correlation differs' USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER module_invoice_cash_binding BEFORE INSERT ON public.module_invoice_cash_correlations "
        "FOR EACH ROW EXECUTE FUNCTION public.guard_module_invoice_cash_binding()"
    )
    # Provider observations remain mutable under their existing legacy owner.
    # A positive correlation writer requires a separately reviewed RLS-independent
    # source-freeze contract; an invoker trigger cannot safely infer hidden joins.


def downgrade() -> None:
    raise RuntimeError("Module cash correlations require reviewed forward migration")
