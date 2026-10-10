"""Readiness of the walled-garden module from RouterOS export snapshots."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.router_management import (
    Router,
    RouterConfigSnapshot,
    RouterSnapshotSource,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.walled_garden import DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES
from app.services.walled_garden_router_module import (
    WalledGardenElement,
    WalledGardenRouterModule,
    build_module_config,
    render_walled_garden_module,
)
from app.services.walled_garden_router_readiness import (
    FleetWalledGardenReadinessQuery,
    WalledGardenFindingIssue,
    WalledGardenReadinessError,
    WalledGardenReadinessQuery,
    WalledGardenReadinessStatus,
    evaluate_export,
    parse_routeros_export,
    resolve_fleet_walled_garden_readiness,
    resolve_router_walled_garden_readiness,
)

PORTAL_URL = "https://selfcare.dotmac.io/portal/billing"
PORTAL_IP = "94.72.107.76"

HEADER = """\
# 2026-10-09 02:00:04 by RouterOS 7.15.3
# software id = ABCD-1234
#
# model = CCR2116-12G-4S+
# serial number = HFA09XXXXXX
"""

LEGACY_FILTERS = [
    "add action=fasttrack-connection chain=forward connection-state=established,related hw-offload=yes",
    "add action=accept chain=forward connection-state=established,related",
    'add action=jump chain=forward comment="dotmac suspended quarantine" dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended src-address-list=suspended',
    'add action=accept chain=dotmac-suspended comment="dotmac suspended allow limited DNS" dst-limit=20,40,src-address/1m dst-port=53 protocol=udp',
    'add action=accept chain=dotmac-suspended comment="dotmac suspended portal" dst-address=94.72.107.76 dst-port=80,443 protocol=tcp',
    'add action=reject chain=dotmac-suspended comment="dotmac suspended reject limited" limit=20,40:packet reject-with=icmp-admin-prohibited',
    'add action=drop chain=dotmac-suspended comment="dotmac suspended drop"',
    'add action=accept chain=splynx-blocked comment="dotmac portal allow (suspended)" dst-address=94.72.107.76 dst-port=80,443 protocol=tcp',
]
STATIC_SUSPENDED = [
    "add address=100.64.10.5 list=suspended",
    "add address=100.64.10.6 disabled=yes list=suspended",
    'add address=100.64.10.7 comment="splynx 4411" disabled=yes list=suspended',
    "add address=100.64.10.8 disabled=yes list=suspended",
    "add address=10.0.0.0/8 list=bogons",
]


def _resources(*, paystack: bool) -> dict:
    value = copy.deepcopy(DEFAULT_WALLED_GARDEN_ALLOWED_RESOURCES)
    value["entries"][0]["enabled"] = paystack
    return value


def _module(*, paystack: bool = False) -> WalledGardenRouterModule:
    return render_walled_garden_module(
        build_module_config(
            portal_url=PORTAL_URL,
            portal_ip=PORTAL_IP,
            suspended_address_list="suspended",
            allowed_resources=_resources(paystack=paystack),
        )
    )


def _export_value(value: str) -> str:
    if any(ch in value for ch in ' ()"') or ":" in value:
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _export_line(element: WalledGardenElement, **overrides: str) -> str:
    """Render an element as RouterOS /export would: sorted keys, quoted."""

    props = {**dict(element.fields), "comment": element.tag, **overrides}
    words = " ".join(
        f"{key}={_export_value(value)}" for key, value in sorted(props.items())
    )
    return f"add {words}"


def _wrap(line: str, width: int = 80) -> str:
    """Wrap like RouterOS: break at spaces with `` \\`` + 4-space indent."""

    out: list[str] = []
    current = ""
    for word in line.split(" "):
        candidate = f"{current} {word}" if current else word
        if len(candidate) > width and current:
            out.append(current + " \\")
            current = "    " + word
        else:
            current = candidate
    out.append(current)
    return "\n".join(out)


def _export(
    module: WalledGardenRouterModule | None,
    *,
    legacy: bool = True,
    module_jump_first: bool = True,
    skip: frozenset[str] = frozenset(),
    extra_tagged: tuple[str, ...] = (),
    overrides: dict[str, dict[str, str]] | None = None,
) -> str:
    overrides = overrides or {}
    elements = [e for e in (module.elements if module else ()) if e.name not in skip]
    lines = [e for e in elements if e.resource.value == "address_list"]
    filters = [e for e in elements if e.resource.value == "filter"]
    nats = [e for e in elements if e.resource.value == "nat"]
    jump = [e for e in filters if e.name == "forward:jump"]
    chain = [e for e in filters if e.name != "forward:jump"]

    def line(element: WalledGardenElement) -> str:
        return _wrap(_export_line(element, **overrides.get(element.name, {})))

    legacy_filters = [_wrap(item) for item in LEGACY_FILTERS] if legacy else []
    forward = [line(e) for e in jump]
    if module_jump_first:
        filter_lines = forward + legacy_filters + [line(e) for e in chain]
    else:
        filter_lines = legacy_filters + forward + [line(e) for e in chain]
    return "\n".join(
        [
            HEADER.rstrip("\n"),
            "/interface bridge",
            "add name=bridge-lan",
            "/ip firewall address-list",
            *STATIC_SUSPENDED,
            *(line(e) for e in lines),
            *extra_tagged,
            "/ip firewall filter",
            *filter_lines,
            "/ip firewall nat",
            "add action=masquerade chain=srcnat out-interface-list=WAN",
            *(line(e) for e in nats),
            "/system identity",
            "set name=SPDC",
        ]
    )


def test_parser_joins_backslash_continuations() -> None:
    text = (
        "/ip firewall filter\n"
        'add action=jump chain=forward comment="dotmac suspended quarantine" \\\n'
        "    dst-address-list=!splynx-allowed-resources jump-target=dotmac-suspended \\\n"
        "    src-address-list=suspended\n"
        'add action=drop chain=x comment="a \\"quoted\\" \\\n'
        '    comment"\n'
    )
    first, second = parse_routeros_export(text)

    assert first.section == "/ip firewall filter"
    assert first.comment == "dotmac suspended quarantine"
    assert first.get("src-address-list") == "suspended"
    assert first.get("dst-address-list") == "!splynx-allowed-resources"
    assert second.position == 1
    assert second.comment == 'a "quoted" comment'


def test_wrapped_fixture_actually_uses_continuations() -> None:
    assert " \\\n    " in _export(_module())


def test_ready_router_with_module_before_legacy() -> None:
    module = _module()
    evaluation = evaluate_export(export_text=_export(module), module=module)

    assert evaluation.findings == ()
    assert evaluation.legacy_rule_count == 6
    assert "dotmac suspended quarantine" in evaluation.legacy_rules_present
    assert evaluation.static_suspended_enabled == 1
    assert evaluation.static_suspended_disabled == 3


def test_missing_elements_are_named() -> None:
    module = _module()
    export = _export(module, skip=frozenset({"nat:http-redirect", "chain:drop"}))
    evaluation = evaluate_export(export_text=export, module=module)

    missing = {
        f.element_tag
        for f in evaluation.findings
        if f.issue is WalledGardenFindingIssue.missing
    }
    assert missing == {"dotmac-wg:v1:nat:http-redirect", "dotmac-wg:v1:chain:drop"}


def test_legacy_only_router_is_missing_every_module_element() -> None:
    module = _module()
    evaluation = evaluate_export(export_text=_export(None), module=module)

    missing = [
        f.element_tag
        for f in evaluation.findings
        if f.issue is WalledGardenFindingIssue.missing
    ]
    assert missing == list(module.tags)
    assert evaluation.legacy_rule_count == 6
    assert (
        evaluation.static_suspended_enabled + evaluation.static_suspended_disabled == 4
    )


def test_module_after_legacy_is_misplaced() -> None:
    module = _module()
    export = _export(module, module_jump_first=False)
    evaluation = evaluate_export(export_text=export, module=module)

    assert [(f.element_tag, f.issue) for f in evaluation.findings] == [
        ("dotmac-wg:v1:forward:jump", WalledGardenFindingIssue.misplaced)
    ]


def test_drifted_and_disabled_and_duplicated_elements() -> None:
    module = _module()
    jump = next(e for e in module.elements if e.name == "forward:jump")
    export = _export(
        module,
        overrides={
            "nat:http-redirect": {"to-addresses": "10.9.9.9"},
            "chain:drop": {"disabled": "yes"},
        },
    ).replace(
        "/ip firewall nat",
        "/ip firewall filter\n" + _export_line(jump) + "\n/ip firewall nat",
    )
    issues = {
        (f.element_tag, f.issue)
        for f in evaluate_export(export_text=export, module=module).findings
    }

    assert (
        "dotmac-wg:v1:nat:http-redirect",
        WalledGardenFindingIssue.drifted,
    ) in issues
    assert ("dotmac-wg:v1:chain:drop", WalledGardenFindingIssue.disabled) in issues
    assert ("dotmac-wg:v1:forward:jump", WalledGardenFindingIssue.duplicated) in issues


def test_enabled_entry_missing_on_router_is_reported_by_key() -> None:
    applied = _module(paystack=False)
    desired = _module(paystack=True)
    evaluation = evaluate_export(export_text=_export(applied), module=desired)

    keys = {
        f.entry_key
        for f in evaluation.findings
        if f.issue is WalledGardenFindingIssue.missing
    }
    assert keys == {"paystack"}
    assert len(evaluation.findings) == 4


def test_disabled_entry_still_present_is_drift() -> None:
    applied = _module(paystack=True)
    desired = _module(paystack=False)
    evaluation = evaluate_export(export_text=_export(applied), module=desired)

    assert {f.issue for f in evaluation.findings} == {
        WalledGardenFindingIssue.disabled_entry_present
    }
    assert {f.entry_key for f in evaluation.findings} == {"paystack"}
    assert len(evaluation.findings) == 4


def test_unknown_tagged_rows_are_unexpected() -> None:
    module = _module()
    stray = 'add address=old.example.com comment="dotmac-wg:v1:allow:retired:old.example.com" list=dotmac-wg-allow'
    evaluation = evaluate_export(
        export_text=_export(module, extra_tagged=(stray,)), module=module
    )

    assert [(f.issue, f.entry_key) for f in evaluation.findings] == [
        (WalledGardenFindingIssue.unexpected, None)
    ]


# ── query owner (fast SQLite unit lane, not deployed-schema evidence) ────────


def _settings(db, *, paystack: bool = False) -> None:
    for key, value_type, values in (
        ("captive_portal_url", SettingValueType.string, {"value_text": PORTAL_URL}),
        ("captive_portal_ip", SettingValueType.string, {"value_text": PORTAL_IP}),
        (
            "walled_garden_allowed_resources",
            SettingValueType.json,
            {"value_json": _resources(paystack=paystack)},
        ),
    ):
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
    db.flush()


def _router(db, name: str, *, active: bool = True) -> Router:
    router = Router(
        name=name,
        hostname=name.lower(),
        management_ip="192.0.2.1",
        rest_api_username="readonly",
        rest_api_password="not-used-in-tests",  # noqa: S106 - fixture only
        is_active=active,
    )
    db.add(router)
    db.flush()
    return router


def _snapshot(db, router: Router, text: str, captured_at: datetime) -> None:
    db.add(
        RouterConfigSnapshot(
            router_id=router.id,
            config_export=text,
            config_hash="0" * 64,
            source=RouterSnapshotSource.scheduled,
            created_at=captured_at,
        )
    )
    db.flush()


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_router_query_ready_stale_and_no_snapshot(db_session) -> None:
    _settings(db_session)
    module = _module()
    ready = _router(db_session, "SPDC")
    _snapshot(db_session, ready, _export(None), NOW - timedelta(days=3))
    _snapshot(db_session, ready, _export(module), NOW - timedelta(hours=2))
    stale = _router(db_session, "OLD")
    _snapshot(db_session, stale, _export(module), NOW - timedelta(hours=49))
    bare = _router(db_session, "NEW")

    def status(router: Router) -> WalledGardenReadinessStatus:
        return resolve_router_walled_garden_readiness(
            db_session,
            query=WalledGardenReadinessQuery(router_id=router.id, evaluated_at=NOW),
        ).status

    assert status(ready) is WalledGardenReadinessStatus.ready
    assert status(stale) is WalledGardenReadinessStatus.stale
    assert status(bare) is WalledGardenReadinessStatus.no_snapshot
    looser = resolve_router_walled_garden_readiness(
        db_session,
        query=WalledGardenReadinessQuery(
            router_id=stale.id, evaluated_at=NOW, max_snapshot_age=timedelta(hours=72)
        ),
    )
    assert looser.status is WalledGardenReadinessStatus.ready


def test_router_query_not_ready_reports_entries_and_counts(db_session) -> None:
    _settings(db_session, paystack=True)
    router = _router(db_session, "SPDC")
    _snapshot(db_session, router, _export(_module()), NOW - timedelta(hours=1))

    result = resolve_router_walled_garden_readiness(
        db_session,
        query=WalledGardenReadinessQuery(router_id=router.id, evaluated_at=NOW),
    )

    assert result.status is WalledGardenReadinessStatus.not_ready
    assert result.missing_entry_keys == ("paystack",)
    assert result.legacy_rule_count == 6
    assert (result.static_suspended_enabled, result.static_suspended_disabled) == (1, 3)


def test_unrenderable_settings_are_not_configured(db_session) -> None:
    router = _router(db_session, "SPDC")
    result = resolve_router_walled_garden_readiness(
        db_session,
        query=WalledGardenReadinessQuery(router_id=router.id, evaluated_at=NOW),
    )

    assert result.status is WalledGardenReadinessStatus.not_configured
    assert result.configuration_error is not None


def test_unknown_router_is_a_domain_error(db_session) -> None:
    with pytest.raises(WalledGardenReadinessError) as excinfo:
        resolve_router_walled_garden_readiness(
            db_session, query=WalledGardenReadinessQuery(router_id=uuid.uuid4())
        )
    assert excinfo.value.code.value == "router_not_found"


def test_fleet_query_covers_active_routers(db_session) -> None:
    _settings(db_session)
    module = _module()
    ready = _router(db_session, "A-READY")
    _snapshot(db_session, ready, _export(module), NOW - timedelta(hours=1))
    legacy = _router(db_session, "B-LEGACY")
    _snapshot(db_session, legacy, _export(None), NOW - timedelta(hours=1))
    _router(db_session, "C-RETIRED", active=False)

    fleet = resolve_fleet_walled_garden_readiness(
        db_session, query=FleetWalledGardenReadinessQuery(evaluated_at=NOW)
    )

    assert [item.router_name for item in fleet.routers] == ["A-READY", "B-LEGACY"]
    assert fleet.ready_router_ids == frozenset({ready.id})
    assert fleet.status_counts[WalledGardenReadinessStatus.not_ready] == 1
    everyone = resolve_fleet_walled_garden_readiness(
        db_session,
        query=FleetWalledGardenReadinessQuery(evaluated_at=NOW, include_inactive=True),
    )
    assert len(everyone.routers) == 3


def test_operator_adapter_delegates_to_the_owners(
    db_session, monkeypatch, capsys
) -> None:
    from contextlib import contextmanager

    from scripts.network import walled_garden_router_module as cli

    @contextmanager
    def _session():
        yield db_session

    monkeypatch.setattr(cli, "read_only_snapshot_session", _session)
    assert cli.main(["render"]) == cli.EXIT_REFUSED  # no portal settings yet

    _settings(db_session)
    router = _router(db_session, "SPDC")
    _snapshot(db_session, router, _export(None), datetime.now(UTC))
    assert cli.main(["render", "--format", "rest"]) == cli.EXIT_OK
    assert "/ip/firewall/filter/add" in capsys.readouterr().out
    assert cli.main(["readiness", "--router", "SPDC"]) == cli.EXIT_NOT_READY
    assert '"status": "not_ready"' in capsys.readouterr().out
    assert cli.main(["readiness", "--router", "MISSING"]) == cli.EXIT_REFUSED
