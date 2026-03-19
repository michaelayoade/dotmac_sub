"""Smoke tests for the modular ONT/OLT action layout."""

from app.services.network.ont_actions import OntActions


def test_ont_actions_facade_exposes_split_methods() -> None:
    assert callable(OntActions.reboot)
    assert callable(OntActions.get_running_config)
    assert callable(OntActions.set_wifi_ssid)
    assert callable(OntActions.set_pppoe_credentials)
    assert callable(OntActions.run_ping_diagnostic)


def test_focused_olt_action_modules_importable() -> None:
    from app.services.network.olt_ssh_ont import (
        bind_tr069_server_profile,
        configure_ont_iphost,
    )
    from app.services.network.olt_ssh_profiles import (
        get_line_profiles,
        get_tr069_server_profiles,
    )
    from app.services.network.olt_ssh_service_ports import (
        create_single_service_port,
        delete_service_port,
        get_service_ports_for_ont,
    )

    assert callable(create_single_service_port)
    assert callable(delete_service_port)
    assert callable(get_service_ports_for_ont)
    assert callable(configure_ont_iphost)
    assert callable(bind_tr069_server_profile)
    assert callable(get_line_profiles)
    assert callable(get_tr069_server_profiles)
