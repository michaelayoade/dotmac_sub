"""Add WireGuard VPN tables.

Revision ID: f4a1b2c3d5e6
Revises: e2c7a9d3f0b1
Create Date: 2025-01-18

WireGuard replaces OpenVPN as the sole VPN protocol.
- wireguard_servers: Server/interface configuration
- wireguard_peers: Peer configuration with device links
- wireguard_connection_logs: Connection tracking (optional auditing)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f4a1b2c3d5e6"
down_revision = "e2c7a9d3f0b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE wireguardpeerstatus AS ENUM ('active', 'disabled');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create wireguard_servers table
    if "wireguard_servers" not in existing_tables:
        op.create_table(
            "wireguard_servers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(160), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            # WireGuard interface settings
            sa.Column(
                "listen_port", sa.Integer(), nullable=False, server_default="51820"
            ),
            # Server keypair
            sa.Column("private_key", sa.Text(), nullable=True),  # Encrypted
            sa.Column("public_key", sa.String(64), nullable=True),  # Base64, 44 chars
            # Public endpoint
            sa.Column("public_host", sa.String(255), nullable=True),
            sa.Column("public_port", sa.Integer(), nullable=True),
            # VPN network settings
            sa.Column(
                "vpn_address",
                sa.String(64),
                nullable=False,
                server_default="10.10.0.1/24",
            ),
            sa.Column("mtu", sa.Integer(), nullable=False, server_default="1420"),
            # DNS servers
            sa.Column("dns_servers", postgresql.JSON(), nullable=True),
            # Status
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            # Metadata
            sa.Column("metadata", postgresql.JSON(), nullable=True),
            # Timestamps
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # Create wireguard_peers table
    if "wireguard_peers" not in existing_tables:
        op.create_table(
            "wireguard_peers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "server_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("wireguard_servers.id"),
                nullable=False,
            ),
            # Peer identity
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            # Peer keypair
            sa.Column("public_key", sa.String(64), nullable=False),
            sa.Column("private_key", sa.Text(), nullable=True),  # Encrypted
            sa.Column("preshared_key", sa.Text(), nullable=True),  # Encrypted
            # Device links
            sa.Column(
                "nas_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("nas_devices.id"),
                nullable=True,
            ),
            sa.Column(
                "network_device_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("network_devices.id"),
                nullable=True,
            ),
            sa.Column(
                "pop_site_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("pop_sites.id"),
                nullable=True,
            ),
            # IP configuration
            sa.Column("allowed_ips", postgresql.JSON(), nullable=True),
            sa.Column("peer_address", sa.String(64), nullable=True),
            # WireGuard settings
            sa.Column(
                "persistent_keepalive",
                sa.Integer(),
                nullable=False,
                server_default="25",
            ),
            # Status
            sa.Column(
                "status",
                postgresql.ENUM(
                    "active", "disabled", name="wireguardpeerstatus", create_type=False
                ),
                nullable=False,
                server_default="active",
            ),
            # Provisioning token
            sa.Column("provision_token_hash", sa.String(128), nullable=True),
            sa.Column(
                "provision_token_expires_at", sa.DateTime(timezone=True), nullable=True
            ),
            # Connection tracking
            sa.Column("last_handshake_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("endpoint_ip", sa.String(64), nullable=True),
            sa.Column("rx_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("tx_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            # Metadata
            sa.Column("metadata", postgresql.JSON(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            # Timestamps
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )

    # Refresh inspector to see new tables
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create unique constraint for public_key per server
    if "wireguard_peers" in existing_tables:
        existing_constraints = {
            c["name"] for c in inspector.get_unique_constraints("wireguard_peers")
        }
        if "uq_wireguard_peers_server_public_key" not in existing_constraints:
            op.create_unique_constraint(
                "uq_wireguard_peers_server_public_key",
                "wireguard_peers",
                ["server_id", "public_key"],
            )

    # Create wireguard_connection_logs table
    if "wireguard_connection_logs" not in existing_tables:
        op.create_table(
            "wireguard_connection_logs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "peer_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("wireguard_peers.id"),
                nullable=False,
            ),
            # Connection details
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("endpoint_ip", sa.String(64), nullable=True),
            sa.Column("peer_address", sa.String(64), nullable=True),
            # Traffic stats
            sa.Column("rx_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("tx_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            # Disconnection info
            sa.Column("disconnect_reason", sa.String(255), nullable=True),
        )

    # Create indexes
    if "wireguard_servers" in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("wireguard_servers")}
        if "ix_wireguard_servers_is_active" not in existing_indexes:
            op.create_index(
                "ix_wireguard_servers_is_active", "wireguard_servers", ["is_active"]
            )

    if "wireguard_peers" in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("wireguard_peers")}
        if "ix_wireguard_peers_server_id" not in existing_indexes:
            op.create_index(
                "ix_wireguard_peers_server_id", "wireguard_peers", ["server_id"]
            )
        if "ix_wireguard_peers_status" not in existing_indexes:
            op.create_index("ix_wireguard_peers_status", "wireguard_peers", ["status"])
        if "ix_wireguard_peers_server_status" not in existing_indexes:
            op.create_index(
                "ix_wireguard_peers_server_status",
                "wireguard_peers",
                ["server_id", "status"],
            )
        if "ix_wireguard_peers_nas_device_id" not in existing_indexes:
            op.create_index(
                "ix_wireguard_peers_nas_device_id", "wireguard_peers", ["nas_device_id"]
            )

    if "wireguard_connection_logs" in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("wireguard_connection_logs")}
        if "ix_wireguard_connection_logs_peer_id" not in existing_indexes:
            op.create_index(
                "ix_wireguard_connection_logs_peer_id",
                "wireguard_connection_logs",
                ["peer_id"],
            )
        if "ix_wireguard_connection_logs_connected_at" not in existing_indexes:
            op.create_index(
                "ix_wireguard_connection_logs_connected_at",
                "wireguard_connection_logs",
                ["connected_at"],
            )


def downgrade() -> None:
    # Drop indexes
    op.drop_index(
        "ix_wireguard_connection_logs_connected_at", "wireguard_connection_logs"
    )
    op.drop_index("ix_wireguard_connection_logs_peer_id", "wireguard_connection_logs")
    op.drop_index("ix_wireguard_peers_nas_device_id", "wireguard_peers")
    op.drop_index("ix_wireguard_peers_server_status", "wireguard_peers")
    op.drop_index("ix_wireguard_peers_status", "wireguard_peers")
    op.drop_index("ix_wireguard_peers_server_id", "wireguard_peers")
    op.drop_index("ix_wireguard_servers_is_active", "wireguard_servers")

    # Drop unique constraint
    op.drop_constraint(
        "uq_wireguard_peers_server_public_key", "wireguard_peers", type_="unique"
    )

    # Drop tables
    op.drop_table("wireguard_connection_logs")
    op.drop_table("wireguard_peers")
    op.drop_table("wireguard_servers")

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS wireguardpeerstatus")
