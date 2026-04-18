import pytest

from app.services.router_management.connection import (
    DANGEROUS_COMMANDS,
    RouterConnectionService,
    check_dangerous_commands,
)


def test_check_dangerous_commands_blocks_reset():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/reset-configuration"])


def test_check_dangerous_commands_blocks_shutdown():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/system/shutdown"])


def test_check_dangerous_commands_allows_safe():
    check_dangerous_commands(
        [
            "/queue simple set [find] queue=sfq/sfq",
            "/ip address add address=10.0.0.1/24 interface=ether1",
        ]
    )


def test_check_dangerous_commands_case_insensitive():
    with pytest.raises(ValueError, match="Dangerous command blocked"):
        check_dangerous_commands(["/System/Reset-Configuration"])


def test_build_base_url_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=443, use_ssl=True
    )
    assert url == "https://10.0.0.1:443"


def test_build_base_url_no_ssl():
    url = RouterConnectionService._build_base_url(
        management_ip="10.0.0.1", port=80, use_ssl=False
    )
    assert url == "http://10.0.0.1:80"


def test_dangerous_commands_list_is_not_empty():
    assert len(DANGEROUS_COMMANDS) >= 4
