import logging
import threading
import time

import httpx
from sshtunnel import SSHTunnelForwarder

from app.models.router_management import JumpHost, Router
from app.schemas.router_management import ConnectionTestResult
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)

DANGEROUS_COMMANDS = [
    "/system/reset-configuration",
    "/system/shutdown",
    "/system/reboot",
    "/disk/format-drive",
    "/file/remove",
    "/user/remove",
]

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0


def check_dangerous_commands(commands: list[str]) -> None:
    for cmd in commands:
        cmd_lower = cmd.lower().strip()
        for dangerous in DANGEROUS_COMMANDS:
            if cmd_lower.startswith(dangerous):
                raise ValueError(
                    f"Dangerous command blocked: {cmd}. "
                    f"Commands matching {dangerous} are not allowed."
                )


class RouterConnectionService:
    _tunnels: dict[str, SSHTunnelForwarder] = {}
    _lock = threading.Lock()

    @staticmethod
    def _build_base_url(management_ip: str, port: int, use_ssl: bool) -> str:
        scheme = "https" if use_ssl else "http"
        return f"{scheme}://{management_ip}:{port}"

    @classmethod
    def _get_or_create_tunnel(
        cls, router: Router, jump_host: JumpHost
    ) -> SSHTunnelForwarder:
        with cls._lock:
            tunnel_key = f"{jump_host.id}:{router.management_ip}:{router.rest_api_port}"

            if tunnel_key in cls._tunnels:
                tunnel = cls._tunnels[tunnel_key]
                if tunnel.is_active:
                    return tunnel
                del cls._tunnels[tunnel_key]

            ssh_key = decrypt_credential(jump_host.ssh_key)
            ssh_password = decrypt_credential(jump_host.ssh_password)

            kwargs: dict = {
                "ssh_username": jump_host.username,
                "remote_bind_address": (
                    router.management_ip,
                    router.rest_api_port,
                ),
            }
            if ssh_key:
                kwargs["ssh_pkey"] = ssh_key
            elif ssh_password:
                kwargs["ssh_password"] = ssh_password

            tunnel = SSHTunnelForwarder(
                (jump_host.hostname, jump_host.port),
                **kwargs,
            )
            tunnel.start()
            cls._tunnels[tunnel_key] = tunnel
            logger.info(
                "SSH tunnel opened: %s:%d -> localhost:%d via %s",
                router.management_ip,
                router.rest_api_port,
                tunnel.local_bind_port,
                jump_host.hostname,
            )
            return tunnel

    @classmethod
    def cleanup_idle_tunnels(cls) -> int:
        with cls._lock:
            closed = 0
            dead_keys = []
            for key, tunnel in cls._tunnels.items():
                if not tunnel.is_active:
                    dead_keys.append(key)
                    closed += 1
                    continue
            for key in dead_keys:
                try:
                    cls._tunnels[key].stop()
                except Exception:
                    logger.debug("Failed to stop dead tunnel during cleanup", exc_info=True)
                del cls._tunnels[key]
            return closed

    @classmethod
    def close_all_tunnels(cls) -> None:
        with cls._lock:
            for tunnel in cls._tunnels.values():
                try:
                    tunnel.stop()
                except Exception:
                    logger.debug("Failed to stop tunnel during shutdown", exc_info=True)
            cls._tunnels.clear()

    @classmethod
    def get_client(cls, router: Router) -> httpx.Client:
        username = (
            decrypt_credential(router.rest_api_username) or router.rest_api_username
        )
        password = (
            decrypt_credential(router.rest_api_password) or router.rest_api_password
        )

        if router.access_method.value == "jump_host" and router.jump_host:
            tunnel = cls._get_or_create_tunnel(router, router.jump_host)
            base_url = cls._build_base_url(
                "127.0.0.1", tunnel.local_bind_port, router.use_ssl
            )
        else:
            base_url = cls._build_base_url(
                router.management_ip, router.rest_api_port, router.use_ssl
            )

        return httpx.Client(
            base_url=base_url,
            auth=(username, password),
            verify=router.verify_tls,
            timeout=httpx.Timeout(CONNECT_TIMEOUT, read=READ_TIMEOUT),
        )

    @classmethod
    def execute(
        cls,
        router: Router,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                with cls.get_client(router) as client:
                    response = client.request(
                        method=method,
                        url=f"/rest{path}",
                        json=payload,
                    )
                    response.raise_for_status()
                    return response.json() if response.text else {}
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE**attempt
                    logger.warning(
                        "Router %s attempt %d failed: %s. Retrying in %.1fs",
                        router.name,
                        attempt + 1,
                        str(exc),
                        wait,
                    )
                    time.sleep(wait)
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"Router {router.name} returned {exc.response.status_code}: "
                    f"{exc.response.text[:200]}"
                ) from exc

        raise RuntimeError(
            f"Router {router.name} unreachable after {MAX_RETRIES} attempts: {last_error}"
        )

    @classmethod
    def execute_batch(cls, router: Router, commands: list[dict]) -> list[dict]:
        results = []
        for cmd in commands:
            result = cls.execute(
                router,
                method=cmd.get("method", "POST"),
                path=cmd["path"],
                payload=cmd.get("payload"),
            )
            results.append(result)
        return results

    @classmethod
    def test_connection(cls, router: Router) -> ConnectionTestResult:
        start = time.time()
        try:
            data = cls.execute(router, "GET", "/system/resource")
            elapsed_ms = int((time.time() - start) * 1000)
            version = data.get("version", "unknown")
            return ConnectionTestResult(
                success=True,
                message=f"Connected. RouterOS {version}",
                response_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.time() - start) * 1000)
            return ConnectionTestResult(
                success=False,
                message=str(exc),
                response_time_ms=elapsed_ms,
            )
