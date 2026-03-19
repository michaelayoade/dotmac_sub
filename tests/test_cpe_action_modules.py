"""Smoke tests for the modular CPE action layout."""

from app.services.network.cpe_actions import CpeActions


def test_cpe_actions_facade_exposes_split_methods() -> None:
    assert callable(CpeActions.reboot)
    assert callable(CpeActions.refresh_status)
    assert callable(CpeActions.get_running_config)
    assert callable(CpeActions.factory_reset)
    assert callable(CpeActions.set_wifi_ssid)
    assert callable(CpeActions.set_wifi_password)
    assert callable(CpeActions.toggle_lan_port)
    assert callable(CpeActions.send_connection_request)
    assert callable(CpeActions.set_connection_request_credentials)
    assert callable(CpeActions.run_ping_diagnostic)
    assert callable(CpeActions.run_traceroute_diagnostic)


def test_cpe_action_device_module_importable() -> None:
    from app.services.network.cpe_action_device import (
        factory_reset,
        get_running_config,
        reboot,
        refresh_status,
    )

    assert callable(reboot)
    assert callable(refresh_status)
    assert callable(get_running_config)
    assert callable(factory_reset)


def test_cpe_action_wifi_module_importable() -> None:
    from app.services.network.cpe_action_wifi import (
        set_wifi_password,
        set_wifi_ssid,
        toggle_lan_port,
    )

    assert callable(set_wifi_ssid)
    assert callable(set_wifi_password)
    assert callable(toggle_lan_port)


def test_cpe_action_network_module_importable() -> None:
    from app.services.network.cpe_action_network import (
        send_connection_request,
        set_connection_request_credentials,
    )

    assert callable(send_connection_request)
    assert callable(set_connection_request_credentials)


def test_cpe_action_diagnostics_module_importable() -> None:
    from app.services.network.cpe_action_diagnostics import (
        run_ping_diagnostic,
        run_traceroute_diagnostic,
    )

    assert callable(run_ping_diagnostic)
    assert callable(run_traceroute_diagnostic)


def test_cpe_tr069_summary_importable() -> None:
    from app.services.network.cpe_tr069 import CpeTR069, CpeTR069Summary

    assert callable(CpeTR069.get_device_summary)
    assert CpeTR069Summary is not None


def test_cpe_genieacs_resolution_importable() -> None:
    from app.services.network._resolve import (
        resolve_genieacs_for_cpe,
        resolve_genieacs_for_cpe_with_reason,
    )

    assert callable(resolve_genieacs_for_cpe)
    assert callable(resolve_genieacs_for_cpe_with_reason)


def test_cpe_common_helpers_importable() -> None:
    from app.services.network.ont_action_common import (
        get_cpe_or_error,
        resolve_cpe_client_or_error,
    )

    assert callable(get_cpe_or_error)
    assert callable(resolve_cpe_client_or_error)
