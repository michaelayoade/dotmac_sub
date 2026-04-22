"""ACS client protocol and construction boundary."""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from sqlalchemy.orm import Session


class AcsUnavailable(Exception):
    """Raised when an ACS backend is temporarily unreachable."""


class AcsClient(Protocol):
    """Structural interface for ACS client implementations."""

    def list_devices(
        self, query: dict | None = None, projection: dict | None = None
    ) -> list[dict[str, Any]]: ...

    def get_device(self, device_id: str) -> dict[str, Any]: ...

    def delete_device(self, device_id: str) -> None: ...

    def count_devices(self, query: dict | None = None) -> int: ...

    def create_task(
        self,
        device_id: str,
        task: dict,
        connection_request: bool = True,
        dedupe_pending: bool = True,
        enforce_safety: bool = True,
        allow_broad_refresh: bool = False,
        max_pending_tasks: int | None = None,
        allow_when_pending: bool = False,
    ) -> dict: ...

    def get_parameter_values(
        self,
        device_id: str,
        parameters: list[str],
        connection_request: bool = True,
        allow_when_pending: bool = False,
    ) -> dict: ...

    def set_parameter_values(
        self,
        device_id: str,
        parameters: dict[str, Any],
        connection_request: bool = True,
    ) -> dict: ...

    def refresh_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
        allow_broad_refresh: bool = False,
        allow_when_pending: bool = False,
    ) -> dict: ...

    def reboot_device(
        self, device_id: str, connection_request: bool = True
    ) -> dict: ...

    def factory_reset(
        self, device_id: str, connection_request: bool = True
    ) -> dict: ...

    def download(
        self,
        device_id: str,
        file_type: str,
        file_url: str,
        filename: str | None = None,
        connection_request: bool = True,
    ) -> dict: ...

    def add_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
    ) -> dict: ...

    def delete_object(
        self,
        device_id: str,
        object_path: str,
        connection_request: bool = True,
    ) -> dict: ...

    def get_pending_tasks(self, device_id: str) -> list[dict[str, Any]]: ...

    def list_tasks(self) -> list[dict[str, Any]]: ...

    def delete_task(self, task_id: str) -> None: ...

    def delete_stale_tasks(
        self,
        *,
        older_than: timedelta,
        dry_run: bool = False,
        device_id: str | None = None,
    ) -> dict[str, Any]: ...

    def list_presets(self) -> list[dict[str, Any]]: ...

    def get_preset(self, preset_id: str) -> dict[str, Any]: ...

    def create_preset(self, preset: dict[str, Any]) -> dict[str, Any]: ...

    def delete_preset(self, preset_id: str) -> None: ...

    def list_provisions(self) -> list[dict[str, Any]]: ...

    def get_provision(self, provision_id: str) -> dict[str, Any]: ...

    def create_provision(self, provision_id: str, script: str) -> None: ...

    def delete_provision(self, provision_id: str) -> None: ...

    def list_faults(self, device_id: str | None = None) -> list[dict[str, Any]]: ...

    def delete_fault(self, fault_id: str) -> None: ...

    def retry_fault(self, fault_id: str) -> None: ...

    def add_tag(self, device_id: str, tag: str) -> None: ...

    def remove_tag(self, device_id: str, tag: str) -> None: ...

    def clear_device_faults(self, device_id: str) -> int: ...

    def wait_for_task_completion(
        self,
        device_id: str,
        task_id: str,
        *,
        timeout_sec: int = 30,
    ) -> tuple[bool, str]: ...

    def set_parameter_values_and_wait(
        self,
        device_id: str,
        parameters: dict[str, Any],
        *,
        connection_request: bool = True,
        timeout_sec: int = 30,
    ) -> tuple[bool, str, dict]: ...

    def build_device_id(
        self, oui: str, product_class: str, serial_number: str
    ) -> str: ...

    def parse_device_id(self, device_id: str) -> tuple[str, str, str]: ...

    def extract_parameter_value(
        self, device: dict[str, Any], parameter_path: str
    ) -> Any: ...


class AcsConfigWriter(Protocol):
    """Application-level ACS configuration port."""

    @property
    def queueable_actions(self) -> Collection[str]: ...

    def supports_config_action(self, action: str) -> bool: ...

    def execute_config_action(
        self,
        db: Session,
        action: str,
        ont_id: str,
        *,
        args: list[object] | tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
    ) -> Any: ...

    def set_wifi_ssid(self, db: Session, ont_id: str, ssid: str) -> Any: ...

    def set_wifi_password(self, db: Session, ont_id: str, password: str) -> Any: ...

    def set_wifi_config(
        self,
        db: Session,
        ont_id: str,
        *,
        enabled: bool | None = None,
        ssid: str | None = None,
        password: str | None = None,
        channel: int | None = None,
        security_mode: str | None = None,
    ) -> Any: ...

    def toggle_lan_port(
        self, db: Session, ont_id: str, port: int, enabled: bool
    ) -> Any: ...

    def set_lan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        lan_ip: str | None = None,
        lan_subnet: str | None = None,
        dhcp_enabled: bool | None = None,
        dhcp_start: str | None = None,
        dhcp_end: str | None = None,
    ) -> Any: ...

    def configure_wan_config(
        self,
        db: Session,
        ont_id: str,
        *,
        wan_mode: str,
        wan_vlan: int | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
        dns_servers: str | None = None,
        instance_index: int = 1,
    ) -> Any: ...

    def set_pppoe_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        instance_index: int | None = None,
        wan_vlan: int | None = None,
    ) -> Any: ...

    def set_connection_request_credentials(
        self,
        db: Session,
        ont_id: str,
        username: str,
        password: str,
        *,
        periodic_inform_interval: int = 3600,
    ) -> Any: ...

    def send_connection_request(self, db: Session, ont_id: str) -> Any: ...

    def push_config_urgent(
        self,
        db: Session,
        ont_id: str,
        parameters: dict[str, Any],
        *,
        expected: dict[str, Any] | None = None,
        connection_request_attempts: int = 3,
        connection_request_backoff_sec: float = 1.0,
    ) -> Any: ...

    def download(
        self,
        db: Session,
        ont_id: str,
        *,
        file_type: str,
        file_url: str,
        filename: str | None = None,
    ) -> Any: ...

    def firmware_upgrade(
        self, db: Session, ont_id: str, firmware_image_id: str
    ) -> Any: ...

    def enable_ipv6_on_wan(
        self, db: Session, ont_id: str, *, wan_instance: int | None = None
    ) -> Any: ...


class AcsStateReader(Protocol):
    """Application-level ACS observed-state read port."""

    def get_device_summary(
        self,
        db: Session,
        ont_id: str,
        *,
        persist_observed_runtime: bool = False,
    ) -> Any: ...

    def get_lan_hosts(self, db: Session, ont_id: str) -> list[dict[str, Any]]: ...

    def get_ethernet_ports(self, db: Session, ont_id: str) -> list[dict[str, Any]]: ...

    def persist_observed_runtime(
        self,
        db: Session,
        ont: object,
        summary: object,
        *,
        commit: bool = True,
    ) -> None: ...


class AcsEventIngestor(Protocol):
    """Application-level ACS webhook/event ingestion port."""

    def receive_inform(
        self,
        db: Session,
        *,
        serial_number: str | None,
        device_id_raw: str | None,
        event: Any,
        raw_payload: dict[str, Any] | None = None,
        request_id: str | None = None,
        remote_addr: str | None = None,
        headers: dict[str, Any] | None = None,
        oui: str | None = None,
        product_class: str | None = None,
        acs_server_id: str | None = None,
    ) -> dict[str, Any]: ...


def _is_unavailable_exception(exc: Exception) -> bool:
    cause = exc.__cause__
    if cause is not None:
        cause_module = type(cause).__module__
        cause_name = type(cause).__name__
        if (
            cause_module.startswith("httpx")
            and cause_name.endswith("Error")
            and cause_name != "HTTPStatusError"
        ):
            return True
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "request error",
            "connect error",
            "connection refused",
            "connection reset",
            "timed out",
            "timeout",
            "temporarily unavailable",
            "name or service not known",
        )
    )


class AcsClientPool:
    """ACS client wrapper that retries unavailable operations on a secondary ACS."""

    def __init__(
        self,
        primary: AcsClient,
        secondary: AcsClient | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary

    def with_fallback(self, operation: Callable[[AcsClient], Any]) -> Any:
        try:
            return operation(self.primary)
        except Exception as exc:
            if self.secondary is None or not _is_unavailable_exception(exc):
                raise
            try:
                return operation(self.secondary)
            except Exception as secondary_exc:
                if _is_unavailable_exception(secondary_exc):
                    raise AcsUnavailable(
                        f"Primary and secondary ACS backends are unavailable: "
                        f"{exc}; {secondary_exc}"
                    ) from secondary_exc
                raise

    def __getattr__(self, name: str) -> Any:
        primary_attr = getattr(self.primary, name)
        if not callable(primary_attr):
            return primary_attr

        def call_with_fallback(*args: Any, **kwargs: Any) -> Any:
            return self.with_fallback(
                lambda client: getattr(client, name)(*args, **kwargs)
            )

        return call_with_fallback


@dataclass(frozen=True)
class AcsBackend:
    """Factories for a complete ACS backend implementation."""

    create_client: Callable[..., AcsClient]
    create_config_writer: Callable[[], AcsConfigWriter]
    create_state_reader: Callable[[], AcsStateReader]
    create_event_ingestor: Callable[[], AcsEventIngestor]


_ACS_BACKENDS: dict[str, AcsBackend] = {}
_ACS_BACKEND_ALIASES: dict[str, str] = {
    "genie": "genieacs",
    "genie_acs": "genieacs",
}


def _normalize_kind(kind: str | None) -> str:
    raw = str(kind or os.getenv("ACS_BACKEND", "genieacs")).strip().lower()
    normalized = raw.replace("-", "_").replace(" ", "_")
    return _ACS_BACKEND_ALIASES.get(normalized, normalized)


def register_acs_backend(
    kind: str,
    backend: AcsBackend,
    *,
    aliases: Collection[str] = (),
) -> None:
    """Register an ACS backend implementation.

    External ACS integrations such as Axiros or Larsen can register their
    backend bundle here without changing application call sites.
    """
    normalized_kind = _normalize_kind(kind)
    _ACS_BACKENDS[normalized_kind] = backend
    for alias in aliases:
        _ACS_BACKEND_ALIASES[_normalize_kind(alias)] = normalized_kind


def registered_acs_backends() -> tuple[str, ...]:
    """Return registered ACS backend identifiers."""
    return tuple(sorted(_ACS_BACKENDS))


def _backend_for(kind: str | None) -> AcsBackend:
    normalized_kind = _normalize_kind(kind)
    backend = _ACS_BACKENDS.get(normalized_kind)
    if backend is None:
        supported = ", ".join(registered_acs_backends()) or "none"
        raise ValueError(
            f"Unsupported ACS backend '{kind or normalized_kind}'. "
            f"Registered backends: {supported}"
        )
    return backend


def _create_genieacs_client(
    base_url: str,
    *,
    timeout: float = 30.0,
    headers: dict | None = None,
) -> AcsClient:
    from app.services.genieacs import GenieACSClient

    return GenieACSClient(base_url, timeout=timeout, headers=headers)


def _create_genieacs_config_writer() -> AcsConfigWriter:
    from app.services.acs_config_adapter import acs_config_adapter

    return acs_config_adapter


def _create_genieacs_state_reader() -> AcsStateReader:
    from app.services.acs_state_adapter import acs_state_adapter

    return acs_state_adapter


def _create_genieacs_event_ingestor() -> AcsEventIngestor:
    from app.services.acs_event_adapter import acs_event_adapter

    return acs_event_adapter


register_acs_backend(
    "genieacs",
    AcsBackend(
        create_client=_create_genieacs_client,
        create_config_writer=_create_genieacs_config_writer,
        create_state_reader=_create_genieacs_state_reader,
        create_event_ingestor=_create_genieacs_event_ingestor,
    ),
    aliases=("genie", "genie-acs"),
)


def create_acs_client(
    base_url: str,
    *,
    timeout: float = 30.0,
    headers: dict | None = None,
    kind: str | None = None,
    secondary_base_url: str | None = None,
) -> AcsClient:
    """Create an ACS client for the configured backend."""
    backend = _backend_for(kind)
    primary = backend.create_client(
        base_url,
        timeout=timeout,
        headers=headers,
    )
    fallback_url = (
        str(secondary_base_url or os.getenv("ACS_SECONDARY_BASE_URL") or "").strip()
    )
    if not fallback_url:
        return primary
    secondary = backend.create_client(
        fallback_url,
        timeout=timeout,
        headers=headers,
    )
    return AcsClientPool(primary=primary, secondary=secondary)


def create_acs_config_writer(kind: str | None = None) -> AcsConfigWriter:
    """Create the application-level ACS config writer."""
    return _backend_for(kind).create_config_writer()


def create_acs_state_reader(kind: str | None = None) -> AcsStateReader:
    """Create the application-level ACS state reader."""
    return _backend_for(kind).create_state_reader()


def create_acs_event_ingestor(kind: str | None = None) -> AcsEventIngestor:
    """Create the application-level ACS webhook/event ingestor."""
    return _backend_for(kind).create_event_ingestor()
