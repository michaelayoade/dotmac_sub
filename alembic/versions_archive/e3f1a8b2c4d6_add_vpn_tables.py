"""Add VPN tables for OpenVPN server and client management.

Revision ID: e3f1a8b2c4d6
Revises: dc9b3d0b6b2a
Create Date: 2025-01-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'e3f1a8b2c4d6'
down_revision = 'dc9b3d0b6b2a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum types
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE vpnprotocol AS ENUM ('udp', 'tcp');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE vpncipher AS ENUM ('AES-256-GCM', 'AES-128-GCM', 'AES-256-CBC', 'AES-128-CBC', 'CHACHA20-POLY1305');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE vpnauthdigest AS ENUM ('SHA256', 'SHA384', 'SHA512');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE vpnclientstatus AS ENUM ('active', 'disabled', 'revoked');
        EXCEPTION
            WHEN duplicate_object THEN null;
        END $$;
    """)

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create vpn_servers table
    if 'vpn_servers' not in existing_tables:
        op.create_table(
            'vpn_servers',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('name', sa.String(160), nullable=False, unique=True),
            sa.Column('description', sa.Text(), nullable=True),

        # Server network configuration
        sa.Column('listen_address', sa.String(64), nullable=False, server_default='0.0.0.0'),
        sa.Column('port', sa.Integer(), nullable=False, server_default='1194'),
        sa.Column('protocol', postgresql.ENUM('udp', 'tcp', name='vpnprotocol', create_type=False), nullable=False, server_default='udp'),

        # Public endpoint for clients
        sa.Column('public_host', sa.String(255), nullable=True),
        sa.Column('public_port', sa.Integer(), nullable=True),

        # VPN network settings
        sa.Column('vpn_network', sa.String(64), nullable=False, server_default='10.8.0.0'),
        sa.Column('vpn_netmask', sa.String(64), nullable=False, server_default='255.255.255.0'),

        # Encryption settings
        sa.Column('cipher', postgresql.ENUM('AES-256-GCM', 'AES-128-GCM', 'AES-256-CBC', 'AES-128-CBC', 'CHACHA20-POLY1305', name='vpncipher', create_type=False), nullable=False, server_default='AES-256-GCM'),
        sa.Column('auth_digest', postgresql.ENUM('SHA256', 'SHA384', 'SHA512', name='vpnauthdigest', create_type=False), nullable=False, server_default='SHA256'),
        sa.Column('tls_version_min', sa.String(16), nullable=False, server_default='1.2'),

        # TLS authentication
        sa.Column('tls_auth_key', sa.Text(), nullable=True),
        sa.Column('tls_auth_direction', sa.Integer(), nullable=False, server_default='0'),

        # CA and server certificates (PEM format)
        sa.Column('ca_cert', sa.Text(), nullable=True),
        sa.Column('ca_key', sa.Text(), nullable=True),
        sa.Column('server_cert', sa.Text(), nullable=True),
        sa.Column('server_key', sa.Text(), nullable=True),
        sa.Column('dh_params', sa.Text(), nullable=True),

        # Connection settings
        sa.Column('keepalive_interval', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('keepalive_timeout', sa.Integer(), nullable=False, server_default='120'),
        sa.Column('max_clients', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('client_to_client', sa.Boolean(), nullable=False, server_default='false'),

        # Routes and DNS
        sa.Column('push_routes', postgresql.JSON(), nullable=True),
        sa.Column('push_dns', postgresql.JSON(), nullable=True),

        # Additional config
        sa.Column('extra_config', sa.Text(), nullable=True),

        # Status
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),

        # Metadata
        sa.Column('metadata', postgresql.JSON(), nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # Create vpn_clients table
    if 'vpn_clients' not in existing_tables:
        op.create_table(
            'vpn_clients',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('server_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('vpn_servers.id'), nullable=False),

        # Client identity
        sa.Column('common_name', sa.String(160), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),

        # Linked devices (optional)
        sa.Column('nas_device_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('nas_devices.id'), nullable=True),
        sa.Column('network_device_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('network_devices.id'), nullable=True),
        sa.Column('pop_site_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('pop_sites.id'), nullable=True),

        # Client certificates
        sa.Column('client_cert', sa.Text(), nullable=True),
        sa.Column('client_key', sa.Text(), nullable=True),

        # Static IP assignment
        sa.Column('static_ip', sa.String(64), nullable=True),
        sa.Column('static_netmask', sa.String(64), nullable=True),

        # Client-specific routes and options
        sa.Column('client_routes', postgresql.JSON(), nullable=True),
        sa.Column('push_options', postgresql.JSON(), nullable=True),

        # Status
        sa.Column('status', postgresql.ENUM('active', 'disabled', 'revoked', name='vpnclientstatus', create_type=False), nullable=False, server_default='active'),

        # Connection tracking
        sa.Column('last_connected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_connected_ip', sa.String(64), nullable=True),
        sa.Column('bytes_received', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('bytes_sent', sa.Integer(), nullable=False, server_default='0'),

        # Certificate validity
        sa.Column('cert_not_before', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cert_not_after', sa.DateTime(timezone=True), nullable=True),

        # Metadata
        sa.Column('metadata', postgresql.JSON(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),

        # Timestamps
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )

    # Refresh inspector state to see tables created above.
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # Create unique constraint for common_name per server
    if 'vpn_clients' in existing_tables:
        existing_constraints = {c["name"] for c in inspector.get_unique_constraints("vpn_clients")}
        if 'uq_vpn_clients_server_common_name' not in existing_constraints:
            op.create_unique_constraint(
                'uq_vpn_clients_server_common_name',
                'vpn_clients',
                ['server_id', 'common_name']
            )

    # Create vpn_connection_logs table
    if 'vpn_connection_logs' not in existing_tables:
        op.create_table(
            'vpn_connection_logs',
            sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column('client_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('vpn_clients.id'), nullable=False),

        # Connection details
        sa.Column('connected_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('disconnected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('real_ip', sa.String(64), nullable=True),
        sa.Column('vpn_ip', sa.String(64), nullable=True),

        # Traffic stats
        sa.Column('bytes_received', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('bytes_sent', sa.Integer(), nullable=False, server_default='0'),

        # Disconnection reason
            sa.Column('disconnect_reason', sa.String(255), nullable=True),
        )

    # Create indexes
    if 'vpn_servers' in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("vpn_servers")}
        if 'ix_vpn_servers_is_active' not in existing_indexes:
            op.create_index('ix_vpn_servers_is_active', 'vpn_servers', ['is_active'])

    if 'vpn_clients' in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("vpn_clients")}
        if 'ix_vpn_clients_server_id' not in existing_indexes:
            op.create_index('ix_vpn_clients_server_id', 'vpn_clients', ['server_id'])
        if 'ix_vpn_clients_status' not in existing_indexes:
            op.create_index('ix_vpn_clients_status', 'vpn_clients', ['status'])
        if 'ix_vpn_clients_nas_device_id' not in existing_indexes:
            op.create_index('ix_vpn_clients_nas_device_id', 'vpn_clients', ['nas_device_id'])

    if 'vpn_connection_logs' in existing_tables:
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("vpn_connection_logs")}
        if 'ix_vpn_connection_logs_client_id' not in existing_indexes:
            op.create_index('ix_vpn_connection_logs_client_id', 'vpn_connection_logs', ['client_id'])
        if 'ix_vpn_connection_logs_connected_at' not in existing_indexes:
            op.create_index('ix_vpn_connection_logs_connected_at', 'vpn_connection_logs', ['connected_at'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_vpn_connection_logs_connected_at', 'vpn_connection_logs')
    op.drop_index('ix_vpn_connection_logs_client_id', 'vpn_connection_logs')
    op.drop_index('ix_vpn_clients_nas_device_id', 'vpn_clients')
    op.drop_index('ix_vpn_clients_status', 'vpn_clients')
    op.drop_index('ix_vpn_clients_server_id', 'vpn_clients')
    op.drop_index('ix_vpn_servers_is_active', 'vpn_servers')

    # Drop tables
    op.drop_table('vpn_connection_logs')
    op.drop_table('vpn_clients')
    op.drop_table('vpn_servers')

    # Drop enum types
    op.execute("DROP TYPE vpnclientstatus")
    op.execute("DROP TYPE vpnauthdigest")
    op.execute("DROP TYPE vpncipher")
    op.execute("DROP TYPE vpnprotocol")
