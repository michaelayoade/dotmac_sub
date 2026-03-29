"""Add contract_signatures table for click-to-sign workflow."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "i7d4e5f6g8h9"
down_revision = "h6c3d4e5f7g8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contract_signatures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subscriber_accounts.id"),
            nullable=False,
        ),
        sa.Column(
            "service_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("service_orders.id"),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("legal_documents.id"),
        ),
        sa.Column("signer_name", sa.String(200), nullable=False),
        sa.Column("signer_email", sa.String(255), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", sa.String(45), nullable=False),
        sa.Column("user_agent", sa.String(500)),
        sa.Column("agreement_text", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_contract_signatures_account_id", "contract_signatures", ["account_id"])
    op.create_index("ix_contract_signatures_service_order_id", "contract_signatures", ["service_order_id"])


def downgrade() -> None:
    op.drop_index("ix_contract_signatures_service_order_id", table_name="contract_signatures")
    op.drop_index("ix_contract_signatures_account_id", table_name="contract_signatures")
    op.drop_table("contract_signatures")
