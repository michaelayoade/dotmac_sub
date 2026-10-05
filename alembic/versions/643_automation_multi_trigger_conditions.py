"""Add multi-trigger and nested condition storage to automation rules."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "643_automation_multi_trigger_conditions"
down_revision = "642_network_map_import_feature_classification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "automation_rules",
        sa.Column(
            "trigger_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE automation_rules "
        "SET trigger_keys = jsonb_build_array(trigger_key) "
        "WHERE jsonb_array_length(trigger_keys) = 0"
    )
    op.create_index(
        "ix_automation_rules_trigger_keys",
        "automation_rules",
        ["trigger_keys"],
        postgresql_using="gin",
    )
    op.add_column(
        "automation_rule_versions",
        sa.Column(
            "trigger_schema_versions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE automation_rule_versions AS versions "
        "SET trigger_schema_versions = jsonb_build_object("
        "rules.trigger_key, versions.trigger_schema_version) "
        "FROM automation_rules AS rules "
        "WHERE versions.rule_id = rules.id "
        "AND versions.trigger_schema_versions = '{}'::jsonb"
    )


def downgrade() -> None:
    op.drop_column("automation_rule_versions", "trigger_schema_versions")
    op.drop_index("ix_automation_rules_trigger_keys", table_name="automation_rules")
    op.drop_column("automation_rules", "trigger_keys")
