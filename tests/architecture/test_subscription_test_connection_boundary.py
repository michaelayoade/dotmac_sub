"""The troubleshooting slice has one owner and no financial restoration shortcut."""

from pathlib import Path

from app.services.sot_manifest import contract_validation_errors
from app.services.sot_registry.registry import all_services


def test_test_connection_owner_has_a_complete_contract():
    services = all_services()
    owner = next(
        service for service in services if service.name == "access.test_connection"
    )
    assert owner.contract is not None
    assert not contract_validation_errors(
        owner, service_names={service.name for service in services}
    )


def test_grant_owner_never_changes_commercial_or_funding_fields():
    source = Path("app/services/test_connection.py").read_text(encoding="utf-8")
    for forbidden in (
        "subscription.status =",
        "billing_enabled =",
        "next_billing_at =",
        "resolve_funding",
        "restore_subscription(",
        "ServiceEntitlement(",
    ):
        assert forbidden not in source


def test_radius_sql_deadline_applies_to_check_reply_and_group_selection():
    source = Path("config/freeradius/mods-enabled/sql").read_text(encoding="utf-8")
    for key in (
        "group_membership_query",
        "authorize_check_query",
        "authorize_reply_query",
    ):
        query = source.split(f'{key} = "', 1)[1].split('"', 1)[0]
        assert "Dotmac-Test-Until" in query
        assert "CURRENT_TIMESTAMP" in query
        assert "<> 'Dotmac-Test-'" in query
        assert "LIKE 'Dotmac-Test-%'" not in query
    assert "'Session-Timeout'" in source


def test_network_deadline_is_capped_after_group_reply_processing():
    site = Path("config/freeradius/sites-enabled/default").read_text(encoding="utf-8")
    post_auth = site.split("post-auth {", 1)[1]
    assert "Tmp-Integer-0" in post_auth
    assert "clock_timestamp()" in post_auth
    assert 'Session-Timeout := "%{control:Tmp-String-0}"' in post_auth
    assert "reject" in post_auth
