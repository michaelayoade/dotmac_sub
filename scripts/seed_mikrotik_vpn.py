#!/usr/bin/env python3
"""Seed script to create the MikroTik VPN Service.

Creates a dedicated OpenVPN server instance configured for MikroTik device management.
This server uses TCP protocol and AES-256-CBC cipher for maximum MikroTik compatibility.

Usage:
    python scripts/seed_mikrotik_vpn.py [--generate-certs] [--public-host YOUR_SERVER_IP]

The generated server config can be downloaded from the admin UI and run as a separate
OpenVPN instance (via systemd or Docker) on port 1195.
"""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.vpn import VpnAuthDigest, VpnCipher, VpnProtocol, VpnServer
from app.schemas.vpn import GenerateCertificatesRequest
from app.services.vpn import VpnServerService


MIKROTIK_VPN_CONFIG = {
    "name": "MikroTik VPN Service",
    "description": (
        "Dedicated OpenVPN server for MikroTik device management tunnels. "
        "Uses TCP protocol and AES-256-CBC for maximum RouterOS compatibility. "
        "Run this as a separate OpenVPN instance on port 1195."
    ),
    "listen_address": "0.0.0.0",
    "port": 1195,
    "protocol": VpnProtocol.tcp,
    "vpn_network": "10.9.0.0",
    "vpn_netmask": "255.255.255.0",
    "cipher": VpnCipher.aes_256_cbc,  # CBC for MikroTik compatibility
    "auth_digest": VpnAuthDigest.sha256,
    "tls_version_min": "1.2",
    "keepalive_interval": 10,
    "keepalive_timeout": 120,
    "max_clients": 100,
    "client_to_client": True,  # Allow MikroTik devices to communicate
    "is_active": True,
    "metadata_": {
        "purpose": "mikrotik_management",
        "notes": "Do not use tls-auth with MikroTik - RouterOS OpenVPN client does not support it",
    },
    "extra_config": (
        "# MikroTik-specific settings\n"
        "# Disable NCP (cipher negotiation) for older RouterOS versions\n"
        "ncp-disable\n"
        "\n"
        "# Topology - use net30 for better MikroTik compatibility\n"
        "topology net30\n"
        "\n"
        "# Client-specific config directory (optional)\n"
        "# client-config-dir /etc/openvpn/mikrotik/ccd"
    ),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create the MikroTik VPN Service configuration."
    )
    parser.add_argument(
        "--generate-certs",
        action="store_true",
        help="Generate CA and server certificates (takes ~30 seconds for DH params)",
    )
    parser.add_argument(
        "--public-host",
        type=str,
        help="Public hostname or IP for MikroTik clients to connect to",
    )
    parser.add_argument(
        "--public-port",
        type=int,
        default=None,
        help="Public port if different from listen port (e.g., behind NAT)",
    )
    parser.add_argument(
        "--organization",
        type=str,
        default="ISP Network",
        help="Organization name for certificates",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Update existing server configuration if it already exists",
    )
    return parser.parse_args()


def create_mikrotik_vpn_server(db, args):
    """Create or update the MikroTik VPN server configuration."""

    # Check if server already exists
    existing = db.query(VpnServer).filter(
        VpnServer.name == MIKROTIK_VPN_CONFIG["name"]
    ).first()

    if existing and not args.force:
        print(f"MikroTik VPN Service already exists (ID: {existing.id})")
        print("Use --force to update the configuration")
        return existing

    if existing:
        # Update existing server
        print(f"Updating existing MikroTik VPN Service (ID: {existing.id})...")
        for key, value in MIKROTIK_VPN_CONFIG.items():
            if key not in ("ca_cert", "ca_key", "server_cert", "server_key", "dh_params", "tls_auth_key"):
                setattr(existing, key, value)

        if args.public_host:
            existing.public_host = args.public_host
        if args.public_port:
            existing.public_port = args.public_port

        db.commit()
        db.refresh(existing)
        print("Server configuration updated.")
        return existing

    # Create new server
    print("Creating MikroTik VPN Service...")

    config = MIKROTIK_VPN_CONFIG.copy()
    if args.public_host:
        config["public_host"] = args.public_host
    if args.public_port:
        config["public_port"] = args.public_port

    server = VpnServer(**config)
    db.add(server)
    db.commit()
    db.refresh(server)

    print(f"Created MikroTik VPN Service (ID: {server.id})")
    return server


def generate_certificates(db, server, args):
    """Generate CA and server certificates for the VPN server."""

    if server.ca_cert and server.server_cert:
        print("Certificates already exist. Skipping generation.")
        print("(Delete existing certs in the database to regenerate)")
        return

    print("Generating certificates (this may take ~30 seconds for DH params)...")

    cert_request = GenerateCertificatesRequest(
        ca_common_name=f"{MIKROTIK_VPN_CONFIG['name']} CA",
        server_common_name=MIKROTIK_VPN_CONFIG["name"],
        ca_validity_days=3650,  # 10 years
        server_validity_days=3650,
        key_size=2048,
        country="US",
        state="California",
        organization=args.organization,
    )

    server = VpnServerService.generate_certificates(db, server.id, cert_request)

    # For MikroTik, we don't want tls-auth (RouterOS doesn't support it well)
    # Clear it if it was generated
    server.tls_auth_key = None
    db.commit()
    db.refresh(server)

    print("Certificates generated successfully!")
    print("  - CA Certificate: OK")
    print("  - Server Certificate: OK")
    print("  - DH Parameters: OK")
    print("  - TLS Auth: Disabled (not supported by MikroTik)")


def print_deployment_instructions(server):
    """Print instructions for deploying the OpenVPN server."""

    print("\n" + "=" * 70)
    print("DEPLOYMENT INSTRUCTIONS")
    print("=" * 70)
    print(f"""
MikroTik VPN Service created successfully!

Server Details:
  - Name: {server.name}
  - Port: {server.port}/TCP
  - VPN Network: {server.vpn_network}/{server.vpn_netmask}
  - Cipher: {server.cipher.value}
  - Auth: {server.auth_digest.value}
  - Public Host: {server.public_host or 'NOT SET (configure in admin UI)'}

Next Steps:

1. Download the server config from the admin UI:
   Admin > Network > VPN > MikroTik VPN Service > Download Config

2. Deploy as a separate OpenVPN instance:

   Option A - systemd:
   ─────────────────────────────────────────────────────────────────────
   sudo cp mikrotik-vpn.conf /etc/openvpn/server/mikrotik.conf
   sudo mkdir -p /var/log/openvpn
   sudo systemctl enable --now openvpn-server@mikrotik
   ─────────────────────────────────────────────────────────────────────

   Option B - Docker:
   ─────────────────────────────────────────────────────────────────────
   docker run -d --name openvpn-mikrotik \\
     --cap-add=NET_ADMIN \\
     -p {server.port}:{server.port}/tcp \\
     -v /path/to/mikrotik.conf:/etc/openvpn/server.conf:ro \\
     kylemanna/openvpn \\
     ovpn_run --config /etc/openvpn/server.conf
   ─────────────────────────────────────────────────────────────────────

3. Configure firewall to allow port {server.port}/TCP

4. Create VPN clients for your MikroTik devices in the admin UI:
   Admin > Network > VPN > MikroTik VPN Service > Add Client
""")

    if not server.public_host:
        print("""
WARNING: Public host not configured!
─────────────────────────────────────────────────────────────────────
Set the public host in the admin UI or re-run this script with:
  python scripts/seed_mikrotik_vpn.py --public-host YOUR_SERVER_IP
─────────────────────────────────────────────────────────────────────
""")


def main():
    load_dotenv()
    args = parse_args()

    db = SessionLocal()
    try:
        server = create_mikrotik_vpn_server(db, args)

        if args.generate_certs:
            generate_certificates(db, server, args)

        print_deployment_instructions(server)

    finally:
        db.close()


if __name__ == "__main__":
    main()
