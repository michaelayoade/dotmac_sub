"""Unified VPN management helpers for WireGuard + OpenVPN admin workflows."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy.orm import Session

from app.models.subscription_engine import SettingValueType
from app.models.wireguard import WireGuardPeerStatus
from app.schemas.wireguard import WireGuardPeerCreate
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import wireguard as wg_service
from app.services.audit_helpers import log_audit_event
from app.services.wireguard_system import WireGuardSystemService

OPENVPN_CLIENTS_KEY = "openvpn_clients"
OPENVPN_SERVER_CONFIG_KEY = "openvpn_server_config"
VPN_CONTROL_JOBS_KEY = "vpn_control_jobs_log"
VPN_HEALTH_ALERTS_KEY = "vpn_tunnel_alerts_log"
VPN_HEALTH_SCAN_KEY = "vpn_last_health_scan_at"


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _network_setting_json(db: Session, key: str, default: Any) -> Any:
    try:
        setting = domain_settings_service.network_settings.get_by_key(db, key)
    except Exception:
        return default
    if isinstance(setting.value_json, (dict, list)):
        return setting.value_json
    if isinstance(setting.value_text, str) and setting.value_text.strip():
        try:
            return json.loads(setting.value_text)
        except json.JSONDecodeError:
            return default
    return default


def _upsert_network_json(db: Session, key: str, value: dict[str, Any] | list[dict[str, Any]]) -> None:
    domain_settings_service.network_settings.upsert_by_key(
        db,
        key,
        DomainSettingUpdate(
            value_type=SettingValueType.json,
            value_json=value,
            value_text=None,
            is_secret=False,
            is_active=True,
        ),
    )


def list_openvpn_clients(db: Session) -> list[dict[str, Any]]:
    clients = _network_setting_json(db, OPENVPN_CLIENTS_KEY, [])
    if not isinstance(clients, list):
        return []
    return [client for client in clients if isinstance(client, dict)]


def get_openvpn_server_config(db: Session) -> dict[str, Any]:
    config = _network_setting_json(db, OPENVPN_SERVER_CONFIG_KEY, {})
    if not isinstance(config, dict):
        config = {}
    return {
        "remote_host": config.get("remote_host") or "vpn.example.com",
        "remote_port": int(config.get("remote_port") or 1194),
        "proto": config.get("proto") or "udp",
        "server_subnet": config.get("server_subnet") or "10.8.0.0/24",
        "server_config": config.get("server_config")
        or "port 1194\nproto udp\ndev tun\nserver 10.8.0.0 255.255.255.0\nkeepalive 10 120\npersist-key\npersist-tun",
        "updated_at": config.get("updated_at"),
    }


def _openvpn_next_client_ip(clients: list[dict[str, Any]]) -> str:
    used = set()
    for item in clients:
        value = str(item.get("client_ip") or "").strip()
        if value.startswith("10.8.0."):
            used.add(value)
    for host in range(10, 250):
        candidate = f"10.8.0.{host}"
        if candidate not in used:
            return candidate
    return "10.8.0.250"


def _generate_openvpn_key_material(common_name: str) -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "NG"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DotMac VPN"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - timedelta(minutes=1))
        .not_valid_after(_now() + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    return key_pem, cert_pem


def _build_openvpn_client_config(
    *,
    client_name: str,
    client_key: str,
    client_cert: str,
    remote_host: str,
    remote_port: int,
    proto: str,
) -> str:
    fake_ca = (
        "-----BEGIN CERTIFICATE-----\n"
        + base64.b64encode(f"dotmac-openvpn-ca:{client_name}".encode("utf-8")).decode("utf-8")
        + "\n-----END CERTIFICATE-----"
    )
    return (
        "client\n"
        f"dev tun\nproto {proto}\n"
        f"remote {remote_host} {remote_port}\n"
        "nobind\npersist-key\npersist-tun\nremote-cert-tls server\n"
        "verb 3\n"
        "<ca>\n"
        f"{fake_ca}\n"
        "</ca>\n"
        "<cert>\n"
        f"{client_cert.strip()}\n"
        "</cert>\n"
        "<key>\n"
        f"{client_key.strip()}\n"
        "</key>\n"
    )


def build_unified_dashboard_data(db: Session, server_id: str | None = None) -> dict[str, Any]:
    from app.services import web_vpn_servers as web_vpn_servers_service

    wg_data = web_vpn_servers_service.build_dashboard_data(db, server_id=server_id)
    openvpn_clients = list_openvpn_clients(db)

    combined_connections: list[dict[str, Any]] = []

    for peer in wg_data.get("peers_read", []) or []:
        status = "up" if peer.status == WireGuardPeerStatus.active and peer.last_handshake_at else "down"
        combined_connections.append(
            {
                "protocol": "wireguard",
                "id": str(peer.id),
                "name": peer.name,
                "server_name": peer.server_name,
                "status": status,
                "last_handshake_at": peer.last_handshake_at,
                "uptime_seconds": None,
                "rx_bytes": int(peer.rx_bytes or 0),
                "tx_bytes": int(peer.tx_bytes or 0),
                "latency_ms": None,
                "address": peer.peer_address,
            }
        )

    for client in openvpn_clients:
        connected_since = None
        connected_since_text = str(client.get("connected_since") or "").strip()
        if connected_since_text:
            try:
                connected_since = datetime.fromisoformat(connected_since_text)
            except ValueError:
                connected_since = None
        uptime_seconds = int((_now() - connected_since).total_seconds()) if connected_since else None
        combined_connections.append(
            {
                "protocol": "openvpn",
                "id": str(client.get("id") or ""),
                "name": str(client.get("name") or "OpenVPN Client"),
                "server_name": "OpenVPN",
                "status": "up" if bool(client.get("is_connected")) else "down",
                "last_handshake_at": client.get("last_seen_at"),
                "uptime_seconds": uptime_seconds,
                "rx_bytes": int(client.get("rx_bytes") or 0),
                "tx_bytes": int(client.get("tx_bytes") or 0),
                "latency_ms": client.get("latency_ms"),
                "address": client.get("client_ip"),
            }
        )

    active_count = sum(1 for item in combined_connections if item["status"] == "up")
    alerts = list_vpn_alerts(db, limit=20)

    return {
        "wireguard": wg_data,
        "openvpn_clients": openvpn_clients,
        "openvpn_config": get_openvpn_server_config(db),
        "connections": combined_connections,
        "summary": {
            "total": len(combined_connections),
            "active": active_count,
            "down": max(0, len(combined_connections) - active_count),
            "wireguard": len(wg_data.get("peers_read", []) or []),
            "openvpn": len(openvpn_clients),
        },
        "alerts": alerts,
    }


def _job_entries(db: Session) -> list[dict[str, Any]]:
    entries = _network_setting_json(db, VPN_CONTROL_JOBS_KEY, [])
    if not isinstance(entries, list):
        return []
    return [item for item in entries if isinstance(item, dict)]


def _save_jobs(db: Session, entries: list[dict[str, Any]]) -> None:
    _upsert_network_json(db, VPN_CONTROL_JOBS_KEY, entries[:100])


def upsert_control_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("job_id is required")
    entries = _job_entries(db)
    for idx, item in enumerate(entries):
        if str(item.get("job_id") or "") == job_id:
            entries[idx] = {**item, **payload}
            _save_jobs(db, entries)
            return entries[idx]
    entries.insert(0, payload)
    _save_jobs(db, entries)
    return payload


def get_control_job(db: Session, job_id: str) -> dict[str, Any] | None:
    for item in _job_entries(db):
        if str(item.get("job_id") or "") == job_id:
            return item
    return None


def queue_control_job(
    db: Session,
    *,
    protocol: str,
    action: str,
    server_id: str | None,
    actor_id: str | None,
) -> dict[str, Any]:
    protocol_name = (protocol or "").strip().lower()
    action_name = (action or "").strip().lower()
    if protocol_name not in {"wireguard", "openvpn"}:
        raise ValueError("Unsupported VPN protocol")
    if action_name not in {"restart", "status", "config"}:
        raise ValueError("Unsupported VPN action")

    job_id = str(uuid.uuid4())
    return upsert_control_job(
        db,
        {
            "job_id": job_id,
            "protocol": protocol_name,
            "action": action_name,
            "server_id": server_id,
            "status": "queued",
            "queued_at": _now_iso(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "actor_id": actor_id,
        },
    )


def execute_control_job(db: Session, *, job_id: str) -> dict[str, Any]:
    job = get_control_job(db, job_id)
    if not job:
        raise ValueError("VPN control job not found")

    job = upsert_control_job(db, {**job, "status": "running", "started_at": _now_iso(), "error": None})
    protocol = str(job.get("protocol") or "")
    action = str(job.get("action") or "")

    try:
        result: dict[str, Any]
        if protocol == "wireguard":
            server_id = str(job.get("server_id") or "").strip()
            if not server_id:
                servers = wg_service.wg_servers.list(db, limit=1)
                if not servers:
                    raise ValueError("No WireGuard server configured")
                server_id = str(servers[0].id)
            server = wg_service.wg_servers.get(db, server_id)

            if action == "restart":
                down_ok, down_msg = WireGuardSystemService.undeploy_server(db, server.id)
                up_ok, up_msg = WireGuardSystemService.deploy_server(db, server.id)
                result = {
                    "server": server.name,
                    "down_ok": down_ok,
                    "down_message": down_msg,
                    "up_ok": up_ok,
                    "up_message": up_msg,
                }
            elif action == "status":
                result = WireGuardSystemService.get_interface_status(server.interface_name)
            elif action == "config":
                result = {
                    "server": server.name,
                    "interface": server.interface_name,
                    "config": WireGuardSystemService.generate_config(db, server.id),
                }
            else:
                raise ValueError("Unsupported WireGuard action")
        else:
            openvpn_config = get_openvpn_server_config(db)
            if action == "restart":
                openvpn_config["updated_at"] = _now_iso()
                _upsert_network_json(db, OPENVPN_SERVER_CONFIG_KEY, openvpn_config)
                result = {
                    "message": "OpenVPN service restart requested",
                    "updated_at": openvpn_config["updated_at"],
                }
            elif action == "status":
                clients = list_openvpn_clients(db)
                connected = sum(1 for c in clients if bool(c.get("is_connected")))
                result = {
                    "clients_total": len(clients),
                    "clients_connected": connected,
                    "remote_host": openvpn_config["remote_host"],
                    "remote_port": openvpn_config["remote_port"],
                }
            elif action == "config":
                result = {
                    "remote_host": openvpn_config["remote_host"],
                    "remote_port": openvpn_config["remote_port"],
                    "proto": openvpn_config["proto"],
                    "config": openvpn_config["server_config"],
                }
            else:
                raise ValueError("Unsupported OpenVPN action")

        return upsert_control_job(
            db,
            {
                **job,
                "status": "completed",
                "completed_at": _now_iso(),
                "result": result,
                "error": None,
            },
        )
    except Exception as exc:
        return upsert_control_job(
            db,
            {
                **job,
                "status": "failed",
                "completed_at": _now_iso(),
                "result": None,
                "error": str(exc),
            },
        )


def create_vpn_client(
    db: Session,
    *,
    protocol: str,
    name: str,
    server_id: str | None,
    peer_address: str | None,
    remote_host: str | None,
    remote_port: int | None,
    actor_id: str | None,
    request,
) -> dict[str, Any]:
    protocol_name = (protocol or "wireguard").strip().lower()
    if protocol_name == "wireguard":
        if not server_id:
            raise ValueError("WireGuard server is required")
        created = wg_service.wg_peers.create(
            db,
            payload=WireGuardPeerCreate(
                server_id=uuid.UUID(str(server_id)),
                name=name,
                description="Created from VPN client wizard",
                peer_address=peer_address or None,
                use_preshared_key=True,
                metadata_={"source": "vpn_client_wizard"},
            ),
        )
        config = wg_service.wg_peers.generate_peer_config(db, created.id)
        result = {
            "protocol": "wireguard",
            "client_id": str(created.id),
            "name": created.name,
            "filename": config.filename,
            "config_content": config.config_content,
        }
    elif protocol_name == "openvpn":
        clients = list_openvpn_clients(db)
        openvpn_config = get_openvpn_server_config(db)
        key_pem, cert_pem = _generate_openvpn_key_material(name)

        chosen_host = (remote_host or "").strip() or str(openvpn_config["remote_host"])
        chosen_port = int(remote_port or openvpn_config["remote_port"])
        config_content = _build_openvpn_client_config(
            client_name=name,
            client_key=key_pem,
            client_cert=cert_pem,
            remote_host=chosen_host,
            remote_port=chosen_port,
            proto=str(openvpn_config["proto"]),
        )
        client_id = str(uuid.uuid4())
        clients.insert(
            0,
            {
                "id": client_id,
                "name": name,
                "client_ip": _openvpn_next_client_ip(clients),
                "is_connected": False,
                "connected_since": None,
                "last_seen_at": None,
                "rx_bytes": 0,
                "tx_bytes": 0,
                "latency_ms": None,
                "created_at": _now_iso(),
                "config_content": config_content,
            },
        )
        _upsert_network_json(db, OPENVPN_CLIENTS_KEY, clients)
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in name.strip())
        result = {
            "protocol": "openvpn",
            "client_id": client_id,
            "name": name,
            "filename": f"{safe_name or 'openvpn-client'}.ovpn",
            "config_content": config_content,
        }
    else:
        raise ValueError("Unsupported VPN protocol")

    log_audit_event(
        db=db,
        request=request,
        action="create",
        entity_type="vpn_client",
        entity_id=result["client_id"],
        actor_id=actor_id,
        metadata={"protocol": result["protocol"], "name": result["name"]},
    )
    return result


def list_vpn_alerts(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    alerts = _network_setting_json(db, VPN_HEALTH_ALERTS_KEY, [])
    if not isinstance(alerts, list):
        return []
    return [item for item in alerts if isinstance(item, dict)][: max(1, limit)]


def run_health_scan(db: Session) -> dict[str, Any]:
    now = _now()
    threshold = now - timedelta(minutes=15)
    alerts = list_vpn_alerts(db, limit=200)

    active_wg_peers = wg_service.wg_peers.list(db, status=WireGuardPeerStatus.active, limit=2000)
    new_alerts: list[dict[str, Any]] = []

    for peer in active_wg_peers:
        if not peer.last_handshake_at or peer.last_handshake_at < threshold:
            new_alerts.append(
                {
                    "id": str(uuid.uuid4()),
                    "protocol": "wireguard",
                    "tunnel": peer.name,
                    "severity": "warning",
                    "message": f"WireGuard tunnel '{peer.name}' has no recent handshake",
                    "created_at": _now_iso(),
                }
            )

    for client in list_openvpn_clients(db):
        last_seen = None
        last_seen_text = str(client.get("last_seen_at") or "").strip()
        if last_seen_text:
            try:
                last_seen = datetime.fromisoformat(last_seen_text)
            except ValueError:
                last_seen = None
        if bool(client.get("is_connected")):
            continue
        if not last_seen or last_seen < threshold:
            new_alerts.append(
                {
                    "id": str(uuid.uuid4()),
                    "protocol": "openvpn",
                    "tunnel": str(client.get("name") or "OpenVPN Client"),
                    "severity": "warning",
                    "message": f"OpenVPN tunnel '{client.get('name') or 'Client'}' is down",
                    "created_at": _now_iso(),
                }
            )

    if new_alerts:
        merged = new_alerts + alerts
        _upsert_network_json(db, VPN_HEALTH_ALERTS_KEY, merged[:200])

    domain_settings_service.network_settings.upsert_by_key(
        db,
        VPN_HEALTH_SCAN_KEY,
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=_now_iso(),
            value_json=None,
            is_secret=False,
            is_active=True,
        ),
    )

    return {"scanned": len(active_wg_peers) + len(list_openvpn_clients(db)), "alerts_added": len(new_alerts)}


def should_schedule_health_scan(db: Session, *, every_minutes: int = 5) -> bool:
    try:
        setting = domain_settings_service.network_settings.get_by_key(db, VPN_HEALTH_SCAN_KEY)
    except Exception:
        return True
    if not setting.value_text:
        return True
    try:
        last = datetime.fromisoformat(setting.value_text)
    except ValueError:
        return True
    return (_now() - last) >= timedelta(minutes=max(1, every_minutes))
