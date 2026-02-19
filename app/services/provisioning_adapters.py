from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlparse

import paramiko
import routeros_api
from ncclient import manager

from app.models.provisioning import ProvisioningVendor
from app.services.genieacs import GenieACSClient
from app.services.response import ListResponseMixin


@dataclass
class ProvisioningResult(ListResponseMixin):
    status: str
    detail: str | None = None
    payload: dict | None = None


class Provisioner(ABC, ListResponseMixin):
    vendor: ProvisioningVendor

    @abstractmethod
    def assign_ont(self, context: dict, config: dict | None) -> ProvisioningResult:
        raise NotImplementedError

    @abstractmethod
    def push_config(self, context: dict, config: dict | None) -> ProvisioningResult:
        raise NotImplementedError

    @abstractmethod
    def confirm_up(self, context: dict, config: dict | None) -> ProvisioningResult:
        raise NotImplementedError


class StubProvisioner(Provisioner, ListResponseMixin):
    def __init__(self, vendor: ProvisioningVendor) -> None:
        self.vendor = vendor

    def assign_ont(self, context: dict, config: dict | None) -> ProvisioningResult:
        return ProvisioningResult(status="ok", detail="assign_ont stub", payload=config)

    def push_config(self, context: dict, config: dict | None) -> ProvisioningResult:
        return ProvisioningResult(status="ok", detail="push_config stub", payload=config)

    def confirm_up(self, context: dict, config: dict | None) -> ProvisioningResult:
        return ProvisioningResult(status="ok", detail="confirm_up stub", payload=config)


def _resolve_connection(context: dict) -> dict:
    connector = context.get("connector") or {}
    auth_config = dict(connector.get("auth_config") or {})
    base_url = connector.get("base_url") or auth_config.get("host") or ""
    host = auth_config.get("host")
    port = auth_config.get("port")
    if base_url and not host:
        parsed = urlparse(base_url)
        host = parsed.hostname or base_url
        port = port or parsed.port
    return {
        "host": host,
        "port": port,
        "username": auth_config.get("username"),
        "password": auth_config.get("password"),
        "private_key": auth_config.get("private_key"),
        "private_key_path": auth_config.get("private_key_path"),
        "timeout_sec": auth_config.get("timeout_sec") or connector.get("timeout_sec"),
        "use_ssl": auth_config.get("use_ssl", False),
        "hostkey_verify": auth_config.get("hostkey_verify", True),
    }


def _ssh_client(conn: dict) -> paramiko.SSHClient:
    if not conn.get("host"):
        raise ValueError("SSH host is required")
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    pkey = None
    if conn.get("private_key"):
        pkey = paramiko.RSAKey.from_private_key(
            io.StringIO(conn["private_key"])
        )
    elif conn.get("private_key_path"):
        pkey = paramiko.RSAKey.from_private_key_file(conn["private_key_path"])
    client.connect(
        hostname=conn["host"],
        port=int(conn.get("port") or 22),
        username=conn.get("username"),
        password=conn.get("password"),
        pkey=pkey,
        timeout=conn.get("timeout_sec") or 30,
    )
    return client


def _run_ssh_commands(conn: dict, commands: list[str]) -> list[str]:
    outputs: list[str] = []
    client = _ssh_client(conn)
    try:
        for cmd in commands:
            stdin, stdout, stderr = client.exec_command(cmd)
            output = stdout.read().decode("utf-8").strip()
            err = stderr.read().decode("utf-8").strip()
            outputs.append(output if output else err)
    finally:
        client.close()
    return outputs


def _netconf_manager(conn: dict) -> manager.Manager:
    if not conn.get("host"):
        raise ValueError("NETCONF host is required")
    return manager.connect(
        host=conn["host"],
        port=int(conn.get("port") or 830),
        username=conn.get("username"),
        password=conn.get("password"),
        hostkey_verify=bool(conn.get("hostkey_verify")),
        allow_agent=False,
        look_for_keys=False,
        timeout=conn.get("timeout_sec") or 30,
    )


def _routeros_api(conn: dict) -> routeros_api.RouterOsApiPool:
    if not conn.get("host"):
        raise ValueError("RouterOS host is required")
    return routeros_api.RouterOsApiPool(
        host=conn["host"],
        username=conn.get("username"),
        password=conn.get("password"),
        port=int(conn.get("port") or 8728),
        use_ssl=bool(conn.get("use_ssl")),
        plaintext_login=True,
    )


class MikrotikProvisioner(Provisioner, ListResponseMixin):
    vendor = ProvisioningVendor.mikrotik

    def assign_ont(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "assign_ont")

    def push_config(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "push_config")

    def confirm_up(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "confirm_up")

    def _execute(self, context: dict, config: dict | None, step: str) -> ProvisioningResult:
        config = config or {}
        conn = _resolve_connection(context)
        commands = config.get("commands") or []
        api_calls = config.get("api_calls") or []
        outputs: list[str] = []
        if commands:
            outputs = _run_ssh_commands(conn, commands)
        if api_calls:
            pool = _routeros_api(conn)
            try:
                api = pool.get_api()
                for call in api_calls:
                    path = call.get("path")
                    if not path:
                        raise ValueError("RouterOS api_calls requires path")
                    resource = api.get_resource(path)
                    method = call.get("method", "add")
                    data = call.get("data") or {}
                    func = getattr(resource, method)
                    outputs.append(str(func(**data)))
            finally:
                pool.disconnect()
        if not commands and not api_calls:
            raise ValueError(f"{step} requires commands or api_calls")
        return ProvisioningResult(status="ok", detail="mikrotik step ok", payload={"outputs": outputs})


class HuaweiProvisioner(Provisioner, ListResponseMixin):
    vendor = ProvisioningVendor.huawei

    def assign_ont(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "assign_ont")

    def push_config(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "push_config")

    def confirm_up(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "confirm_up")

    def _execute(self, context: dict, config: dict | None, step: str) -> ProvisioningResult:
        config = config or {}
        conn = _resolve_connection(context)
        commands = config.get("commands") or []
        if commands:
            outputs = _run_ssh_commands(conn, commands)
            return ProvisioningResult(status="ok", detail="huawei ssh ok", payload={"outputs": outputs})
        edit_config = config.get("edit_config")
        rpc = config.get("rpc")
        get_filter = config.get("get_filter")
        if not any([edit_config, rpc, get_filter]):
            raise ValueError(f"{step} requires commands, edit_config, rpc, or get_filter")
        with _netconf_manager(conn) as mgr:
            netconf_outputs: list[str] = []
            if edit_config:
                target = config.get("target", "running")
                mgr.edit_config(target=target, config=edit_config)
                netconf_outputs.append("edit_config ok")
            if rpc:
                result = mgr.dispatch(rpc)
                netconf_outputs.append(str(result))
            if get_filter:
                result = mgr.get(filter=get_filter)
                netconf_outputs.append(str(result))
        return ProvisioningResult(
            status="ok",
            detail="huawei netconf ok",
            payload={"outputs": netconf_outputs},
        )


class ZteProvisioner(HuaweiProvisioner, ListResponseMixin):
    vendor = ProvisioningVendor.zte


class GenieACSProvisioner(Provisioner, ListResponseMixin):
    vendor = ProvisioningVendor.genieacs

    def assign_ont(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "assign_ont")

    def push_config(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "push_config")

    def confirm_up(self, context: dict, config: dict | None) -> ProvisioningResult:
        return self._execute(context, config, "confirm_up")

    def _resolve_device_id(self, context: dict, config: dict) -> str | None:
        device_id = config.get("device_id") or context.get("genieacs_device_id")
        if isinstance(device_id, str) and device_id:
            return device_id
        oui = config.get("oui") or context.get("tr069_oui")
        product_class = config.get("product_class") or context.get("tr069_product_class")
        serial_number = (
            config.get("serial_number")
            or context.get("tr069_serial_number")
            or context.get("cpe_serial_number")
        )
        if (
            isinstance(oui, str)
            and isinstance(product_class, str)
            and isinstance(serial_number, str)
            and oui
            and product_class
            and serial_number
        ):
            return f"{oui}-{product_class}-{serial_number}"
        return None

    def _execute(self, context: dict, config: dict | None, step: str) -> ProvisioningResult:
        config = config or {}
        device_id = self._resolve_device_id(context, config)
        if not device_id:
            raise ValueError("GenieACS device_id is required")
        connector = context.get("connector") or {}
        auth_config = connector.get("auth_config") or {}
        base_url = config.get("base_url") or connector.get("base_url") or auth_config.get("host")
        if not base_url:
            raise ValueError("GenieACS base_url is required")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        headers = dict(connector.get("headers") or {})
        headers.update(config.get("headers") or {})
        timeout = config.get("timeout_sec") or connector.get("timeout_sec") or 30.0
        client = GenieACSClient(base_url, timeout=float(timeout), headers=headers)

        connection_request = bool(config.get("connection_request", True))
        results: list[dict] = []
        tasks = config.get("tasks") or []
        parameters = config.get("parameters") or {}
        get_parameters = config.get("get_parameters") or []
        refresh_object = config.get("refresh_object")

        if parameters:
            result = client.set_parameter_values(
                device_id, parameters, connection_request=connection_request
            )
            results.append({"task": "setParameterValues", "result": result})
        if get_parameters:
            result = client.get_parameter_values(
                device_id, list(get_parameters), connection_request=connection_request
            )
            results.append({"task": "getParameterValues", "result": result})
        if refresh_object:
            result = client.refresh_object(
                device_id, refresh_object, connection_request=connection_request
            )
            results.append({"task": "refreshObject", "result": result})
        for task in tasks:
            if isinstance(task, str):
                payload = {"name": task}
            else:
                payload = dict(task or {})
            if not payload.get("name"):
                raise ValueError("GenieACS tasks require a name field")
            result = client.create_task(
                device_id, payload, connection_request=connection_request
            )
            results.append({"task": payload["name"], "result": result})

        if not results:
            raise ValueError(f"{step} requires tasks or parameters")
        return ProvisioningResult(
            status="ok", detail="genieacs task ok", payload={"results": results}
        )


_PROVISIONERS: dict[ProvisioningVendor, Provisioner] = {}


def register_provisioner(provisioner: Provisioner) -> None:
    _PROVISIONERS[provisioner.vendor] = provisioner


def get_provisioner(vendor: ProvisioningVendor) -> Provisioner:
    provisioner = _PROVISIONERS.get(vendor)
    if provisioner:
        return provisioner
    return StubProvisioner(vendor)


def register_default_provisioners() -> None:
    register_provisioner(MikrotikProvisioner())
    register_provisioner(HuaweiProvisioner())
    register_provisioner(ZteProvisioner())
    register_provisioner(GenieACSProvisioner())


register_default_provisioners()
