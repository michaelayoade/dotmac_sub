"""OLT NETCONF operations using ncclient."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager

from ncclient import manager
from ncclient.transport.errors import SSHError

from app.models.network import OLTDevice
from app.services.credential_crypto import decrypt_credential

logger = logging.getLogger(__name__)


@contextmanager
def _connect(olt: OLTDevice) -> Generator[manager.Manager, None, None]:
    """Open a NETCONF session to an OLT. Caller uses as context manager."""
    host = (olt.mgmt_ip or olt.hostname or "").strip()
    if not host:
        raise ValueError("Management IP or hostname is required")
    if not olt.ssh_username:
        raise ValueError("SSH username is required for NETCONF")
    if not olt.ssh_password:
        raise ValueError("SSH password is required for NETCONF")
    if not olt.netconf_enabled:
        raise ValueError("NETCONF is not enabled on this OLT")

    password = decrypt_credential(olt.ssh_password)
    if not password:
        raise ValueError("SSH password could not be decrypted")

    port = int(olt.netconf_port or 830)
    mgr = manager.connect(
        host=host,
        port=port,
        username=olt.ssh_username,
        password=password,
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=30,
    )
    try:
        yield mgr
    finally:
        mgr.close_session()


def test_connection(olt: OLTDevice) -> tuple[bool, str, list[str]]:
    """Test NETCONF connectivity and return server capabilities.

    Returns:
        Tuple of (success, message, list of capability URNs).
    """
    try:
        with _connect(olt) as mgr:
            capabilities = [str(c) for c in mgr.server_capabilities]
            logger.info(
                "NETCONF connection to OLT %s successful: %d capabilities",
                olt.name,
                len(capabilities),
            )
            return (
                True,
                f"NETCONF connected — {len(capabilities)} capabilities",
                capabilities,
            )
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}", []
    except ValueError as exc:
        return False, str(exc), []
    except Exception as exc:
        logger.error("NETCONF error on OLT %s: %s", olt.name, exc)
        return False, f"NETCONF error: {type(exc).__name__}: {exc}", []


def get_running_config(
    olt: OLTDevice, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    """Fetch running configuration via NETCONF get-config.

    Args:
        olt: The OLT device.
        filter_xpath: Optional XPath filter to narrow the config section.

    Returns:
        Tuple of (success, message, config_xml).
    """
    try:
        with _connect(olt) as mgr:
            if filter_xpath:
                result = mgr.get_config(
                    source="running",
                    filter=("xpath", filter_xpath),
                )
            else:
                result = mgr.get_config(source="running")
            config_xml = str(result)
            if len(config_xml) < 20:
                return False, "Empty or minimal config returned", config_xml
            return True, "Configuration retrieved via NETCONF", config_xml
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}", ""
    except Exception as exc:
        logger.error("NETCONF get-config error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}", ""


def get_operational_data(
    olt: OLTDevice, filter_xpath: str | None = None
) -> tuple[bool, str, str]:
    """Fetch operational state data via NETCONF get.

    Args:
        olt: The OLT device.
        filter_xpath: Optional XPath filter.

    Returns:
        Tuple of (success, message, data_xml).
    """
    try:
        with _connect(olt) as mgr:
            if filter_xpath:
                result = mgr.get(filter=("xpath", filter_xpath))
            else:
                result = mgr.get()
            data_xml = str(result)
            return True, "Operational data retrieved", data_xml
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}", ""
    except Exception as exc:
        logger.error("NETCONF get error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}", ""


def edit_config(
    olt: OLTDevice,
    config_xml: str,
    *,
    target: str = "running",
) -> tuple[bool, str]:
    """Push configuration changes via NETCONF edit-config.

    Args:
        olt: The OLT device.
        config_xml: XML configuration payload to apply.
        target: Config datastore target (default: running).

    Returns:
        Tuple of (success, message).
    """
    try:
        with _connect(olt) as mgr:
            mgr.edit_config(target=target, config=config_xml)
            logger.info("NETCONF edit-config applied to OLT %s", olt.name)
            return True, "Configuration applied via NETCONF"
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}"
    except Exception as exc:
        logger.error("NETCONF edit-config error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}"


def get_config_filtered(
    olt: OLTDevice,
    filter_xml: str,
) -> tuple[bool, str, str]:
    """Fetch configuration via NETCONF get-config with subtree filter.

    Args:
        olt: The OLT device.
        filter_xml: XML subtree filter.

    Returns:
        Tuple of (success, message, config_xml).
    """
    try:
        with _connect(olt) as mgr:
            result = mgr.get_config(
                source="running",
                filter=("subtree", filter_xml),
            )
            config_xml = str(result)
            return True, "Configuration retrieved via NETCONF", config_xml
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}", ""
    except Exception as exc:
        logger.error("NETCONF get-config error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}", ""


def get_filtered(
    olt: OLTDevice,
    filter_xml: str,
) -> tuple[bool, str, str]:
    """Fetch operational data via NETCONF get with subtree filter.

    Args:
        olt: The OLT device.
        filter_xml: XML subtree filter.

    Returns:
        Tuple of (success, message, data_xml).
    """
    try:
        with _connect(olt) as mgr:
            result = mgr.get(filter=("subtree", filter_xml))
            data_xml = str(result)
            return True, "Operational data retrieved", data_xml
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}", ""
    except Exception as exc:
        logger.error("NETCONF get error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}", ""


def dispatch_rpc(
    olt: OLTDevice,
    rpc_xml: str,
) -> tuple[bool, str]:
    """Dispatch custom RPC via NETCONF.

    Args:
        olt: The OLT device.
        rpc_xml: RPC XML payload (inner content without <rpc> wrapper).

    Returns:
        Tuple of (success, message).
    """
    try:
        with _connect(olt) as mgr:
            mgr.dispatch(rpc_xml)
            logger.info("NETCONF RPC dispatched to OLT %s", olt.name)
            return True, "RPC executed via NETCONF"
    except SSHError as exc:
        return False, f"NETCONF SSH error: {exc}"
    except Exception as exc:
        logger.error("NETCONF RPC error on OLT %s: %s", olt.name, exc)
        return False, f"Error: {type(exc).__name__}: {exc}"
