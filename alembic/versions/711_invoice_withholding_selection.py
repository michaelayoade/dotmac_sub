"""Persist opt-in WHT selection and freeze it at invoice issue.

Revision ID: 711_invoice_withholding_selection
Revises: 709_inbox_latest_visible_message_index
"""

import sqlalchemy as sa

from alembic import op

revision = "711_invoice_withholding_selection"
down_revision = "709_inbox_latest_visible_message_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "withholding_tax_selected",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column(
            "withholding_tax_selection_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "invoices",
        sa.Column("starter_withholding_discharge_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "invoices",
        sa.Column("starter_withholding_tax_set_id", sa.UUID(), nullable=True),
    )
    op.create_check_constraint(
        "ck_invoice_native_withholding_source_pair",
        "invoices",
        "(starter_withholding_discharge_id IS NULL) = (starter_withholding_tax_set_id IS NULL)",
    )
    op.execute("""UPDATE invoices SET withholding_tax_selected=true WHERE EXISTS
        (SELECT 1 FROM withholding_tax_records w WHERE w.source_invoice_id=invoices.id)""")
    op.execute("""CREATE FUNCTION guard_invoice_withholding_selection() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'INSERT' THEN
        IF NEW.starter_withholding_discharge_id IS NOT NULL OR NEW.starter_withholding_tax_set_id IS NOT NULL THEN
          RAISE EXCEPTION 'Native withholding projection must follow accepted source evidence';
        END IF;
        RETURN NEW;
      END IF;
      IF NEW.withholding_tax_selected IS DISTINCT FROM OLD.withholding_tax_selected OR NEW.withholding_tax_selection_version IS DISTINCT FROM OLD.withholding_tax_selection_version THEN
        IF OLD.status <> 'draft' OR NEW.status <> 'draft' THEN RAISE EXCEPTION 'Issued invoice withholding selection is immutable'; END IF;
        IF NEW.withholding_tax_selection_version <> OLD.withholding_tax_selection_version + 1 OR
           current_setting('app.invoice_withholding_selection_invoice',true) IS DISTINCT FROM OLD.id::text THEN
           RAISE EXCEPTION 'Withholding selection requires its draft owner and current version';
        END IF;
      END IF;
      IF NEW.starter_withholding_discharge_id IS DISTINCT FROM OLD.starter_withholding_discharge_id OR
         NEW.starter_withholding_tax_set_id IS DISTINCT FROM OLD.starter_withholding_tax_set_id THEN
        IF OLD.starter_withholding_discharge_id IS NOT NULL OR OLD.starter_withholding_tax_set_id IS NOT NULL OR
           NEW.starter_withholding_discharge_id IS NULL OR NEW.starter_withholding_tax_set_id IS NULL OR
           NOT NEW.withholding_tax_selected OR NEW.status = 'draft' OR
           current_setting('app.invoice_native_withholding_projection_invoice',true) IS DISTINCT FROM OLD.id::text THEN
          RAISE EXCEPTION 'Native withholding source projection requires its owner and immutable evidence';
        END IF;
      END IF;
      RETURN NEW;
    END; $$""")
    op.execute("""CREATE TRIGGER invoice_withholding_selection_guard BEFORE INSERT OR UPDATE ON invoices
        FOR EACH ROW EXECUTE FUNCTION guard_invoice_withholding_selection()""")


def downgrade() -> None:
    selected = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM invoices WHERE withholding_tax_selected OR withholding_tax_selection_version > 0 OR starter_withholding_discharge_id IS NOT NULL OR starter_withholding_tax_set_id IS NOT NULL"
            )
        )
        .scalar_one()
    )
    if selected:
        raise RuntimeError(
            "Cannot remove invoice withholding selection while persisted choices exist"
        )
    op.execute("DROP TRIGGER invoice_withholding_selection_guard ON invoices")
    op.execute("DROP FUNCTION guard_invoice_withholding_selection()")
    op.drop_constraint(
        "ck_invoice_native_withholding_source_pair", "invoices", type_="check"
    )
    op.drop_column("invoices", "starter_withholding_tax_set_id")
    op.drop_column("invoices", "starter_withholding_discharge_id")
    op.drop_column("invoices", "withholding_tax_selection_version")
    op.drop_column("invoices", "withholding_tax_selected")
