"""The walled-garden router module renders deterministically and safely."""

from __future__ import annotations

import copy

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.walled_garden import DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES
from app.services.router_management.connection import check_dangerous_commands
from app.services.router_management.write_adapter import (
    parse_routeros_rest_commands,
)
from app.services.walled_garden_router_module import (
    ALLOW_LIST,
    LEGACY_QUARANTINE_JUMP_COMMENT,
    TAG_PREFIX,
    LegacyQuarantineRole,
    WalledGardenModuleError,
    WalledGardenModuleErrorCode,
    build_module_config,
    render_walled_garden_module,
    render_walled_garden_module_from_settings,
    rest_safety_errors,
    script_safety_errors,
)

PORTAL_URL = "https://selfcare.dotmac.io/portal/billing"
PORTAL_IP = "94.72.107.76"


def _resources(*, paystack: bool = False, extra: list[dict] | None = None) -> dict:
    value = copy.deepcopy(DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES)
    value["entries"][0]["enabled"] = paystack
    value["entries"].extend(extra or [])
    return value


def _module(**kwargs):
    return render_walled_garden_module(
        build_module_config(
            portal_url=kwargs.get("portal_url", PORTAL_URL),
            portal_ip=kwargs.get("portal_ip", PORTAL_IP),
            suspended_address_list=kwargs.get("suspended", "suspended"),
            allowed_resources=kwargs.get("resources", _resources()),
        )
    )


def test_default_render_has_portal_entry_only_and_paystack_disabled() -> None:
    module = _module()

    allow = [e for e in module.elements if e.resource.value == "address_list"]
    assert [e.field_value("address") for e in allow] == [
        PORTAL_IP,
        "selfcare.dotmac.io",
    ]
    assert {e.entry_key for e in allow} == {"portal"}
    paystack = module.entry("paystack")
    assert paystack is not None and paystack.enabled is False
    assert module.elements_for_entry("paystack") == ()
    portal = module.entry("portal")
    assert portal is not None and portal.derived and portal.enabled


def test_empty_allowed_resources_still_renders_the_portal() -> None:
    module = _module(resources={"entries": []})

    assert [e.name for e in module.elements][:2] == [
        f"allow:portal:{PORTAL_IP}",
        "allow:portal:selfcare.dotmac.io",
    ]
    assert len(module.entries) == 1


def test_core_rules_follow_the_settings() -> None:
    module = _module(suspended="dunning")
    by_name = {e.name: e for e in module.elements}

    jump = by_name["forward:jump"]
    assert dict(jump.fields) == {
        "chain": "forward",
        "action": "jump",
        "jump-target": "dotmac-wg",
        "src-address-list": "dunning",
    }
    nat = dict(by_name["nat:http-redirect"].fields)
    assert nat["src-address-list"] == "dunning"
    assert nat["dst-address-list"] == f"!{ALLOW_LIST}"
    assert nat["to-addresses"] == PORTAL_IP
    assert (nat["protocol"], nat["dst-port"], nat["to-ports"]) == ("tcp", "80", "80")
    chain_names = [e.name for e in module.elements if e.name.startswith("chain:")]
    assert chain_names == [
        "chain:dns-udp",
        "chain:dns-tcp",
        "chain:allow-web",
        "chain:reject",
        "chain:drop",
    ]
    web = dict(by_name["chain:allow-web"].fields)
    assert web["dst-port"] == "80,443" and web["dst-address-list"] == ALLOW_LIST


def test_tags_are_unique_and_render_is_idempotent() -> None:
    first = _module(resources=_resources(paystack=True))
    second = _module(resources=_resources(paystack=True))

    assert len(set(first.tags)) == len(first.tags)
    assert all(tag.startswith(TAG_PREFIX) for tag in first.tags)
    assert first.rest_commands() == second.rest_commands()
    assert first.routeros_script() == second.routeros_script()


def test_toggling_an_entry_changes_only_that_entrys_elements() -> None:
    off = _module(resources=_resources(paystack=False))
    on = _module(resources=_resources(paystack=True))

    added = set(on.rest_commands()) - set(off.rest_commands())
    removed = set(off.rest_commands()) - set(on.rest_commands())
    assert removed == set()
    assert {e.rest_command() for e in on.elements_for_entry("paystack")} == added
    assert all(":allow:paystack:" in command for command in added)
    assert sorted(
        e.field_value("address") for e in on.elements_for_entry("paystack")
    ) == [
        "api.paystack.co",
        "checkout.paystack.com",
        "js.paystack.co",
        "standard.paystack.co",
    ]


def test_extra_entry_renders_with_its_own_key() -> None:
    module = _module(
        resources=_resources(
            extra=[
                {
                    "key": "support",
                    "label": "Support desk",
                    "kind": "support",
                    "hosts": ["Help.Dotmac.io."],
                    "enabled": True,
                }
            ]
        )
    )

    (element,) = module.elements_for_entry("support")
    assert element.tag == f"{TAG_PREFIX}allow:support:help.dotmac.io"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"portal_url": "http://selfcare.dotmac.io"}, "portal_url_invalid"),
        ({"portal_url": "https:///nohost"}, "portal_url_invalid"),
        ({"portal_ip": "94.72.107.0/24"}, "portal_ip_invalid"),
        ({"portal_ip": "2001:db8::1"}, "portal_ip_invalid"),
        ({"portal_ip": ""}, "portal_ip_invalid"),
        ({"suspended": "bad list"}, "suspended_address_list_invalid"),
        ({"suspended": ALLOW_LIST}, "suspended_address_list_invalid"),
        (
            {
                "resources": _resources(
                    extra=[
                        {
                            "key": "evil",
                            "label": "x",
                            "kind": "other",
                            "hosts": ['a.com" ; /system reset-configuration'],
                        }
                    ]
                )
            },
            "allowed_resources_invalid",
        ),
        (
            {
                "resources": _resources(
                    extra=[
                        {
                            "key": "ip",
                            "label": "x",
                            "kind": "other",
                            "hosts": ["1.2.3.4"],
                        }
                    ]
                )
            },
            "allowed_resources_invalid",
        ),
    ],
)
def test_invalid_settings_fail_closed(kwargs: dict, code: str) -> None:
    with pytest.raises(WalledGardenModuleError) as excinfo:
        _module(**kwargs)
    assert excinfo.value.code is WalledGardenModuleErrorCode(code)


def test_rendered_set_passes_router_safety_gates() -> None:
    module = _module(resources=_resources(paystack=True))
    commands = list(module.rest_commands())

    check_dangerous_commands(commands)
    plans = parse_routeros_rest_commands(commands)
    assert {plan.action for plan in plans} == {"add"}
    assert {plan.resource_path for plan in plans} == {
        "/ip/firewall/address-list",
        "/ip/firewall/filter",
        "/ip/firewall/nat",
    }
    assert rest_safety_errors(commands) == ()
    script = module.routeros_script()
    assert script_safety_errors(script) == ()
    lowered = script.lower()
    for forbidden in ("/system", "/user", "/ip service", "reset"):
        assert forbidden not in lowered
    removes = [line for line in script.splitlines() if " remove " in line]
    assert len(removes) == 3
    assert all(
        line.endswith(f'[find where comment~"^{TAG_PREFIX}"]') for line in removes
    )


def test_safety_gates_reject_out_of_surface_commands() -> None:
    assert rest_safety_errors(['/system/reset-configuration {"x":"y"}'])
    assert rest_safety_errors(['/ip/firewall/filter/remove {"numbers":"*1"}'])
    assert rest_safety_errors(['/ip/firewall/filter/add {"chain":"forward"}'])
    assert script_safety_errors("/ip firewall filter remove [find]\n")
    assert script_safety_errors("/user add name=x\n")
    assert script_safety_errors('/ip firewall filter add chain=forward comment="x"\n')


def test_forward_jump_is_placed_before_the_legacy_quarantine() -> None:
    module = _module()
    jump = next(e for e in module.elements if e.name == "forward:jump")

    assert jump.placement is not None
    assert jump.placement.chain == "forward"
    assert jump.placement.before_comment == LEGACY_QUARANTINE_JUMP_COMMENT
    script = module.routeros_script()
    anchor_line = (
        ":local wgFwdAnchor [/ip firewall filter find where chain=forward "
        f'comment="{LEGACY_QUARANTINE_JUMP_COMMENT}"]'
    )
    assert anchor_line in script
    assert "place-before=[:pick $wgFwdAnchor 0]" in script
    # The jump is added last, after the chain it targets exists.
    assert module.elements[-1] is jump


def test_legacy_retirement_is_a_typed_worklist_never_rendered() -> None:
    module = _module()

    comments = {item.comment for item in module.legacy_elements_to_retire}
    assert comments == {
        "dotmac suspended quarantine",
        "dotmac suspended allow limited DNS",
        "dotmac suspended portal",
        "dotmac suspended reject limited",
        "dotmac suspended drop",
        "dotmac portal allow (suspended)",
    }
    roles = {item.role for item in module.legacy_elements_to_retire}
    assert LegacyQuarantineRole.forward_jump in roles
    rendered = module.routeros_script() + "\n".join(module.rest_commands())
    for comment in comments - {LEGACY_QUARANTINE_JUMP_COMMENT}:
        assert comment not in rendered
    assert "list=suspended" not in rendered.replace("src-address-list", "")


def _radius_setting(db, key: str, value_type: SettingValueType, **values) -> None:
    db.query(DomainSetting).filter(
        DomainSetting.domain == SettingDomain.radius, DomainSetting.key == key
    ).delete(synchronize_session=False)
    db.add(
        DomainSetting(
            domain=SettingDomain.radius,
            key=key,
            value_type=value_type,
            is_active=True,
            **values,
        )
    )


def test_render_from_settings_uses_the_settings_owner(db_session) -> None:
    _radius_setting(
        db_session, "captive_portal_url", SettingValueType.string, value_text=PORTAL_URL
    )
    _radius_setting(
        db_session, "captive_portal_ip", SettingValueType.string, value_text=PORTAL_IP
    )
    _radius_setting(
        db_session,
        "walled_garden_allowed_resources",
        SettingValueType.json,
        value_json=_resources(paystack=True),
    )
    db_session.flush()

    module = render_walled_garden_module_from_settings(db_session)

    assert module.config.portal_host == "selfcare.dotmac.io"
    assert module.config.suspended_address_list == "suspended"
    assert len(module.elements_for_entry("paystack")) == 4


def test_render_from_settings_without_portal_fails_closed(db_session) -> None:
    with pytest.raises(WalledGardenModuleError):
        render_walled_garden_module_from_settings(db_session)
