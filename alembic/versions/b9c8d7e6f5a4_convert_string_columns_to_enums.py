"""convert string columns to enums

Revision ID: b9c8d7e6f5a4
Revises: 2d4f7d5b3b0a
Create Date: 2026-02-17 00:00:00.000000
"""

from alembic import op  # noqa: I001
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM


# revision identifiers, used by Alembic.
revision = "b9c8d7e6f5a4"
down_revision = "2d4f7d5b3b0a"
branch_labels = None
depends_on = None


# Enum definitions matching the Python enum classes
HEALTH_STATUS_VALUES = ("unknown", "healthy", "degraded", "unhealthy")
PROVISIONING_LOG_STATUS_VALUES = ("pending", "running", "success", "failed", "timeout")
EXECUTION_METHOD_VALUES = ("ssh", "api", "radius_coa")
DISCOUNT_TYPE_VALUES = ("percentage", "percent", "fixed")
SERVICE_ORDER_TYPE_VALUES = (
    "new_install",
    "upgrade",
    "downgrade",
    "disconnect",
    "reconnect",
    "change_service",
)
WIRELESS_MAST_STATUS_VALUES = ("active", "inactive", "maintenance", "decommissioned")
HARDWARE_UNIT_STATUS_VALUES = ("active", "inactive", "failed", "unknown")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Helper: create enum type if it doesn't already exist
    def _create_enum_if_missing(name: str, values: tuple[str, ...]) -> ENUM:
        enum_type = ENUM(*values, name=name, create_type=False)
        # Check if enum type exists in pg_type
        result = bind.execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": name},
        )
        if result.fetchone() is None:
            enum_type.create(bind)
        return enum_type

    # Helper: check if a column exists on a table
    def _has_column(table: str, column: str) -> bool:
        if table not in inspector.get_table_names():
            return False
        cols = {c["name"] for c in inspector.get_columns(table)}
        return column in cols

    # 1. health_status enum (shared by nas_devices and network_devices)
    _create_enum_if_missing("healthstatus", HEALTH_STATUS_VALUES)

    if _has_column("nas_devices", "health_status"):
        op.execute(sa.text("ALTER TABLE nas_devices ALTER COLUMN health_status DROP DEFAULT"))
        op.execute(
            sa.text(
                "ALTER TABLE nas_devices "
                "ALTER COLUMN health_status TYPE healthstatus "
                "USING health_status::healthstatus"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE nas_devices "
                "ALTER COLUMN health_status SET DEFAULT 'unknown'"
            )
        )

    if _has_column("network_devices", "health_status"):
        op.execute(sa.text("ALTER TABLE network_devices ALTER COLUMN health_status DROP DEFAULT"))
        op.execute(
            sa.text(
                "ALTER TABLE network_devices "
                "ALTER COLUMN health_status TYPE healthstatus "
                "USING health_status::healthstatus"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE network_devices "
                "ALTER COLUMN health_status SET DEFAULT 'unknown'"
            )
        )

    # 2. provisioninglogstatus enum
    _create_enum_if_missing("provisioninglogstatus", PROVISIONING_LOG_STATUS_VALUES)

    if _has_column("provisioning_logs", "status"):
        op.execute(sa.text("ALTER TABLE provisioning_logs ALTER COLUMN status DROP DEFAULT"))
        op.execute(
            sa.text(
                "ALTER TABLE provisioning_logs "
                "ALTER COLUMN status TYPE provisioninglogstatus "
                "USING status::provisioninglogstatus"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE provisioning_logs "
                "ALTER COLUMN status SET DEFAULT 'pending'"
            )
        )

    # 3. executionmethod enum
    _create_enum_if_missing("executionmethod", EXECUTION_METHOD_VALUES)

    if _has_column("provisioning_templates", "execution_method"):
        op.execute(
            sa.text(
                "ALTER TABLE provisioning_templates "
                "ALTER COLUMN execution_method TYPE executionmethod "
                "USING execution_method::executionmethod"
            )
        )

    # 4. discounttype enum
    _create_enum_if_missing("discounttype", DISCOUNT_TYPE_VALUES)

    if _has_column("subscriptions", "discount_type"):
        op.execute(
            sa.text(
                "ALTER TABLE subscriptions "
                "ALTER COLUMN discount_type TYPE discounttype "
                "USING discount_type::discounttype"
            )
        )

    # 5. serviceordertype enum
    _create_enum_if_missing("serviceordertype", SERVICE_ORDER_TYPE_VALUES)

    if _has_column("service_orders", "order_type"):
        op.execute(
            sa.text(
                "ALTER TABLE service_orders "
                "ALTER COLUMN order_type TYPE serviceordertype "
                "USING order_type::serviceordertype"
            )
        )

    # 6. wirelessmaststatus enum
    _create_enum_if_missing("wirelessmaststatus", WIRELESS_MAST_STATUS_VALUES)

    if _has_column("wireless_masts", "status"):
        op.execute(sa.text("ALTER TABLE wireless_masts ALTER COLUMN status DROP DEFAULT"))
        op.execute(
            sa.text(
                "ALTER TABLE wireless_masts "
                "ALTER COLUMN status TYPE wirelessmaststatus "
                "USING status::wirelessmaststatus"
            )
        )
        op.execute(
            sa.text(
                "ALTER TABLE wireless_masts "
                "ALTER COLUMN status SET DEFAULT 'active'"
            )
        )

    # 7. hardwareunitstatus enum
    _create_enum_if_missing("hardwareunitstatus", HARDWARE_UNIT_STATUS_VALUES)

    if _has_column("olt_power_units", "status"):
        op.execute(
            sa.text(
                "ALTER TABLE olt_power_units "
                "ALTER COLUMN status TYPE hardwareunitstatus "
                "USING status::hardwareunitstatus"
            )
        )


def downgrade() -> None:
    """Revert enum columns back to varchar."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def _has_column(table: str, column: str) -> bool:
        if table not in inspector.get_table_names():
            return False
        cols = {c["name"] for c in inspector.get_columns(table)}
        return column in cols

    # Revert each column back to VARCHAR
    revert_specs = [
        ("nas_devices", "health_status", "20", "'unknown'"),
        ("network_devices", "health_status", "20", "'unknown'"),
        ("provisioning_logs", "status", "40", "'pending'"),
        ("provisioning_templates", "execution_method", "40", None),
        ("subscriptions", "discount_type", "40", None),
        ("service_orders", "order_type", "60", None),
        ("wireless_masts", "status", "40", "'active'"),
        ("olt_power_units", "status", "40", None),
    ]

    for table, column, length, default in revert_specs:
        if _has_column(table, column):
            op.execute(
                sa.text(
                    f"ALTER TABLE {table} "
                    f"ALTER COLUMN {column} TYPE varchar({length}) "
                    f"USING {column}::text"
                )
            )
            if default:
                op.execute(
                    sa.text(
                        f"ALTER TABLE {table} "
                        f"ALTER COLUMN {column} SET DEFAULT {default}"
                    )
                )

    # Drop enum types
    for enum_name in (
        "hardwareunitstatus",
        "wirelessmaststatus",
        "serviceordertype",
        "discounttype",
        "executionmethod",
        "provisioninglogstatus",
        "healthstatus",
    ):
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
