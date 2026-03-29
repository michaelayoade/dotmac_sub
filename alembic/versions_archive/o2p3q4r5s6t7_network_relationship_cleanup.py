"""Network entity relationship cleanup and improvements.

- Remove incorrect WireGuardPeer device FKs (nas_device_id, network_device_id, pop_site_id)
- Remove NetworkDevice.wireguard_server_id (VPN is not a monitoring concern)
- Add Subscription provisioning FKs (provisioning_nas_device_id, radius_profile_id)
- Add ProvisioningLog.subscription_id for billing traceability
- Add capacity/health tracking fields to NetworkDevice and NasDevice

Revision ID: o2p3q4r5s6t7
Revises: n1o2p3q4r5s6
Create Date: 2026-01-21
"""

from alembic import op
import sqlalchemy as sa


revision = "o2p3q4r5s6t7"
down_revision = "n1o2p3q4r5s6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # =========================================================================
    # 1. Remove incorrect FKs from WireGuardPeer
    #    VPN peers are clients, not owned by devices/sites
    # =========================================================================

    # Drop foreign key constraints first
    op.drop_constraint(
        "wireguard_peers_nas_device_id_fkey",
        "wireguard_peers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "wireguard_peers_network_device_id_fkey",
        "wireguard_peers",
        type_="foreignkey",
    )
    op.drop_constraint(
        "wireguard_peers_pop_site_id_fkey",
        "wireguard_peers",
        type_="foreignkey",
    )

    # Drop the columns
    op.drop_column("wireguard_peers", "nas_device_id")
    op.drop_column("wireguard_peers", "network_device_id")
    op.drop_column("wireguard_peers", "pop_site_id")

    # =========================================================================
    # 2. Remove wireguard_server_id from NetworkDevice
    #    NetworkDevice is for monitoring, not VPN membership
    # =========================================================================

    op.drop_constraint(
        "fk_network_devices_wireguard_server_id",
        "network_devices",
        type_="foreignkey",
    )
    op.drop_column("network_devices", "wireguard_server_id")

    # =========================================================================
    # 3. Add provisioning FKs to Subscription
    # =========================================================================

    op.add_column(
        "subscriptions",
        sa.Column("provisioning_nas_device_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("radius_profile_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_provisioning_nas_device_id",
        "subscriptions",
        "nas_devices",
        ["provisioning_nas_device_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_subscriptions_radius_profile_id",
        "subscriptions",
        "radius_profiles",
        ["radius_profile_id"],
        ["id"],
    )
    op.create_index(
        "ix_subscriptions_provisioning_nas_device_id",
        "subscriptions",
        ["provisioning_nas_device_id"],
    )

    # =========================================================================
    # 4. Add subscription_id to ProvisioningLog
    # =========================================================================

    op.add_column(
        "provisioning_logs",
        sa.Column("subscription_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_provisioning_logs_subscription_id",
        "provisioning_logs",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )

    # =========================================================================
    # 5. Add capacity/health fields to NetworkDevice
    # =========================================================================

    op.add_column(
        "network_devices",
        sa.Column("max_concurrent_subscribers", sa.Integer(), nullable=True),
    )
    op.add_column(
        "network_devices",
        sa.Column("current_subscriber_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "network_devices",
        sa.Column("health_status", sa.String(20), server_default="unknown", nullable=False),
    )
    op.add_column(
        "network_devices",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )

    # =========================================================================
    # 6. Add capacity/health fields to NasDevice
    # =========================================================================

    op.add_column(
        "nas_devices",
        sa.Column("max_concurrent_subscribers", sa.Integer(), nullable=True),
    )
    op.add_column(
        "nas_devices",
        sa.Column("current_subscriber_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "nas_devices",
        sa.Column("health_status", sa.String(20), server_default="unknown", nullable=False),
    )
    op.add_column(
        "nas_devices",
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Remove NasDevice capacity/health fields
    op.drop_column("nas_devices", "last_health_check_at")
    op.drop_column("nas_devices", "health_status")
    op.drop_column("nas_devices", "current_subscriber_count")
    op.drop_column("nas_devices", "max_concurrent_subscribers")

    # Remove NetworkDevice capacity/health fields
    op.drop_column("network_devices", "last_health_check_at")
    op.drop_column("network_devices", "health_status")
    op.drop_column("network_devices", "current_subscriber_count")
    op.drop_column("network_devices", "max_concurrent_subscribers")

    # Remove ProvisioningLog.subscription_id
    op.drop_constraint(
        "fk_provisioning_logs_subscription_id",
        "provisioning_logs",
        type_="foreignkey",
    )
    op.drop_column("provisioning_logs", "subscription_id")

    # Remove Subscription provisioning FKs
    op.drop_index("ix_subscriptions_provisioning_nas_device_id", "subscriptions")
    op.drop_constraint(
        "fk_subscriptions_radius_profile_id",
        "subscriptions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_subscriptions_provisioning_nas_device_id",
        "subscriptions",
        type_="foreignkey",
    )
    op.drop_column("subscriptions", "radius_profile_id")
    op.drop_column("subscriptions", "provisioning_nas_device_id")

    # Restore NetworkDevice.wireguard_server_id
    op.add_column(
        "network_devices",
        sa.Column("wireguard_server_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_network_devices_wireguard_server_id",
        "network_devices",
        "wireguard_servers",
        ["wireguard_server_id"],
        ["id"],
    )

    # Restore WireGuardPeer device FKs
    op.add_column(
        "wireguard_peers",
        sa.Column("nas_device_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "wireguard_peers",
        sa.Column("network_device_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "wireguard_peers",
        sa.Column("pop_site_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "wireguard_peers_nas_device_id_fkey",
        "wireguard_peers",
        "nas_devices",
        ["nas_device_id"],
        ["id"],
    )
    op.create_foreign_key(
        "wireguard_peers_network_device_id_fkey",
        "wireguard_peers",
        "network_devices",
        ["network_device_id"],
        ["id"],
    )
    op.create_foreign_key(
        "wireguard_peers_pop_site_id_fkey",
        "wireguard_peers",
        "pop_sites",
        ["pop_site_id"],
        ["id"],
    )
