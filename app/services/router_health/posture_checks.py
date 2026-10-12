"""Pure posture checks over a parsed RouterOS export (layer 1, no router contact).

Every function here is deterministic and free of I/O. Evidence is structured and
allowlisted: community names, secrets and raw config lines are never emitted.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence

from app.services.router_health.contracts import (
    POSTURE_CHECKS_OFF_ALIASES,
    CheckId,
    EvidenceValue,
    HealthLayer,
    HealthPriority,
    PostureFinding,
    PostureResult,
)
from app.services.router_health.policy import (
    EffectiveInput,
    EffectiveRouterPolicy,
    InputMode,
    Role,
)
from app.services.router_health.routeros_export import (
    ExportDocument,
    ExportItem,
    is_true,
    parse_version,
)

SECTION_FILTER = "/ip firewall filter"
SECTION_FILTER_V6 = "/ipv6 firewall filter"
_FIREWALL_SECTIONS = (
    SECTION_FILTER,
    "/ip firewall nat",
    "/ip firewall mangle",
    "/ip firewall raw",
)
_NON_MATCHER_KEYS = frozenset(
    {"chain", "action", "comment", "log", "log-prefix", "place-before", "disabled"}
)
_TERMINAL_ALLOWED_KEYS = frozenset(
    {
        "chain",
        "action",
        "comment",
        "reject-with",
        "log",
        "log-prefix",
        "dst-address",
        "dst-address-list",
        "disabled",
    }
)
_SHADOW_GUARD_KEYS = (
    "connection-state",
    "src-address",
    "src-address-list",
    "in-interface",
    "in-interface-list",
)
_COMMENT_MAX = 80
_QUALIFIER_RE = re.compile(r"[^a-z0-9.-]")
_ADDRESS_LIST_KEYS = (
    "src-address-list",
    "dst-address-list",
    "address-list",
)


def qualifier_for(text: str) -> str:
    cleaned = _QUALIFIER_RE.sub("-", text.lower()).strip("-")[:40]
    return cleaned or "0"


def _comment(item: ExportItem | None) -> str:
    if item is None:
        return ""
    return (item.get("comment") or "")[:_COMMENT_MAX]


def _enabled_chain_items(
    doc: ExportDocument, section: str, chain: str
) -> list[ExportItem]:
    return [
        item
        for item in doc.section(section)
        if item.verb == "add" and item.get("chain") == chain and item.enabled
    ]


def _all_firewall_items(doc: ExportDocument) -> list[ExportItem]:
    return [
        item
        for section in _FIREWALL_SECTIONS
        for item in doc.section(section)
        if item.verb == "add" and item.enabled
    ]


# ---------------------------------------------------------------- input chain


def _final_drop_ok(last: ExportItem | None, rule: EffectiveInput) -> bool:
    if last is None or last.get("action") != "drop":
        return False
    if not rule.final_drop_regex.match(last.get("comment") or ""):
        return False
    return set(last.props) <= _NON_MATCHER_KEYS


def _input_chain_failure(
    items: Sequence[ExportItem], rule: EffectiveInput
) -> tuple[bool, dict[str, EvidenceValue]]:
    accepts = [
        item
        for item in items
        if item.get("action") == "accept"
        and (item.get("comment") or "").startswith(rule.accept_comment_prefix)
    ]
    last = items[-1] if items else None
    final_ok = _final_drop_ok(last, rule)
    counter_ok = any(
        item.get("action") == "passthrough"
        and (item.get("comment") or "").startswith(rule.accept_comment_prefix)
        and "count would-drop" in (item.get("comment") or "").lower()
        for item in items
    )
    enough = len(accepts) >= rule.min_accepts
    if rule.mode is InputMode.observe:
        passed = enough and (final_ok or counter_ok)
    else:
        passed = enough and final_ok
    evidence: dict[str, EvidenceValue] = {
        "mode": rule.mode.value,
        "input_items": len(items),
        "accept_items": len(accepts),
        "min_accepts": rule.min_accepts,
        "last_action": last.get("action") if last else None,
        "last_comment": _comment(last) if last else None,
        "final_drop_ok": final_ok,
        "observe_counter": counter_ok,
    }
    return (not passed), evidence


def _check_input_v4(
    doc: ExportDocument,
    eff: EffectiveRouterPolicy,
    label: str,
) -> tuple[list[PostureFinding], bool]:
    rule = eff.input_v4
    if rule.mode is InputMode.exempt:
        return [], True
    items = _enabled_chain_items(doc, SECTION_FILTER, "input")
    failed, evidence = _input_chain_failure(items, rule)
    findings: list[PostureFinding] = []
    if failed:
        border_observe = eff.role is Role.border and rule.mode is InputMode.observe
        findings.append(
            PostureFinding(
                check=CheckId.input_v4_missing,
                qualifier="0",
                priority=(
                    HealthPriority.medium if border_observe else HealthPriority.high
                ),
                title=f"{label}: router management plane is not firewalled (IPv4)",
                impact=(
                    "Anyone on the internet can reach SSH, Winbox, API, SNMP and "
                    "BGP listeners on this router."
                ),
                evidence=evidence,
                fix=(
                    "Apply or enforce the INPUT-PROTECT rule set from "
                    ".cutover-private/router-input-firewall/fleet/."
                ),
            )
        )
    elif rule.mode in {InputMode.enforce, InputMode.custom} and items:
        for item in items[:-1]:
            if item.get("action") == "accept" and set(item.props) <= _NON_MATCHER_KEYS:
                findings.append(
                    PostureFinding(
                        check=CheckId.input_v4_bypass,
                        qualifier="0",
                        priority=HealthPriority.high,
                        title=f"{label}: an unconditional accept defeats the input firewall",
                        impact=(
                            "An input accept with no matcher sits before the final "
                            "drop, so every packet is accepted."
                        ),
                        evidence={
                            "line": item.line,
                            "comment": _comment(item),
                            "mode": rule.mode.value,
                        },
                        fix="Remove or restrict the unconditional input accept rule.",
                    )
                )
                break
    return findings, True


def _check_input_v6(
    doc: ExportDocument,
    eff: EffectiveRouterPolicy,
    label: str,
) -> tuple[list[PostureFinding], bool]:
    rule = eff.input_v6
    if rule.mode is InputMode.exempt:
        return [], True
    items = _enabled_chain_items(doc, SECTION_FILTER_V6, "input")
    failed, evidence = _input_chain_failure(items, rule)
    if not failed:
        return [], True
    high = eff.role is Role.border and rule.mode is not InputMode.observe
    return (
        [
            PostureFinding(
                check=CheckId.input_v6_missing,
                qualifier="0",
                priority=HealthPriority.high if high else HealthPriority.medium,
                title=f"{label}: router is not firewalled over IPv6",
                impact="NTP, btest and the BGP listener answer the IPv6 internet.",
                evidence=evidence,
                fix=(
                    "Apply the INPUT6-PROTECT rule set from "
                    ".cutover-private/router-input-firewall/fleet/."
                ),
            )
        ],
        True,
    )


# ----------------------------------------------------------------------- SNMP


def _snmp_enabled(doc: ExportDocument) -> bool:
    return any(
        item.verb == "set" and is_true(item.get("enabled"))
        for item in doc.section("/snmp")
    )


def _is_subset(
    address: str, allowed: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        network = ipaddress.ip_network(address.strip(), strict=False)
    except ValueError:
        return False
    for ok in allowed:
        if network.version == ok.version and network.subnet_of(ok):  # type: ignore[arg-type]
            return True
    return False


def _check_snmp(
    doc: ExportDocument, eff: EffectiveRouterPolicy, label: str
) -> list[PostureFinding]:
    if not _snmp_enabled(doc):
        return []
    communities: list[ExportItem | None] = []
    default_modified = False
    for entry in doc.section("/snmp community"):
        if entry.verb == "set" and entry.selector.get("default") == "yes":
            default_modified = True
            communities.append(entry)
        elif entry.verb == "add":
            communities.append(entry)
    if not default_modified:
        communities.append(None)  # implicit default community, addresses ::/0
    offending: list[str] = []
    bad_indexes: list[int] = []
    writable: list[int] = []
    for index, item in enumerate(communities):
        if item is not None and is_true(item.get("disabled")):
            continue
        addresses = list(item.value_list("addresses")) if item is not None else []
        if not addresses:
            addresses = ["::/0"]
        bad = [a for a in addresses if not _is_subset(a, eff.snmp_allowed_sources)]
        if bad:
            bad_indexes.append(index)
            offending.extend(bad)
        if item is not None and is_true(item.get("write-access")):
            writable.append(index)
    if not bad_indexes and not writable:
        return []
    critical = bool(writable)
    return [
        PostureFinding(
            check=CheckId.snmp_exposed,
            qualifier="0",
            priority=HealthPriority.critical if critical else HealthPriority.high,
            title=(
                f"{label}: SNMP community is writable"
                if critical
                else f"{label}: SNMP is answerable from outside the poller allowlist"
            ),
            impact=(
                "Anyone who guesses or learns the community can read, and with "
                "write access change, the router."
            ),
            evidence={
                "community_indexes": bad_indexes,
                "writable_community_indexes": writable,
                "offending_addresses": sorted(set(offending))[:20],
            },
            fix="Set every community to addresses=<pollers> write-access=no.",
        )
    ]


# ------------------------------------------------------------- small services


def _check_reverse_proxy(
    doc: ExportDocument, eff: EffectiveRouterPolicy, label: str
) -> tuple[list[PostureFinding], bool]:
    version = doc.version_tuple
    minimum = parse_version(eff.reverse_proxy_min_ros)
    if version is None or minimum is None:
        return [], False
    if version < minimum:
        return [], True
    disabled = any(
        item.verb == "set"
        and "reverse-proxy" in item.positional
        and is_true(item.get("disabled"))
        for item in doc.section("/ip service")
    ) or any(
        item.verb == "set" and item.get("enabled") in {"no", "false"}
        for item in doc.section("/ip reverse-proxy")
    )
    if disabled:
        return [], True
    return (
        [
            PostureFinding(
                check=CheckId.reverse_proxy_enabled,
                qualifier="0",
                priority=HealthPriority.medium,
                title=f"{label}: unused HTTPS reverse-proxy service is enabled",
                impact="Port 443 listener with no rules or certificate.",
                evidence={"routeros_version": doc.version},
                fix="/ip service disable reverse-proxy.",
            )
        ],
        True,
    )


def _check_ntp(doc: ExportDocument, label: str) -> list[PostureFinding]:
    clients = [item for item in doc.section("/system ntp client") if item.verb == "set"]
    enabled = any(is_true(item.get("enabled")) for item in clients)
    has_servers = any(item.get("servers") for item in clients) or any(
        item.verb == "add" and item.get("address")
        for item in doc.section("/system ntp client servers")
    )
    if enabled and has_servers:
        return []
    return [
        PostureFinding(
            check=CheckId.ntp_client_off,
            qualifier="0",
            priority=HealthPriority.medium,
            title=f"{label}: router clock is not synchronised by configuration",
            impact="Logs, schedulers (auto-rollback) and certificate checks drift.",
            evidence={"client_enabled": enabled, "servers_configured": has_servers},
            fix="Enable the NTP client and configure at least one server.",
        )
    ]


def _check_radius_incoming(doc: ExportDocument, label: str) -> list[PostureFinding]:
    if any(
        item.verb == "set" and is_true(item.get("accept"))
        for item in doc.section("/radius incoming")
    ):
        return []
    return [
        PostureFinding(
            check=CheckId.radius_incoming_off,
            qualifier="0",
            priority=HealthPriority.high,
            title=f"{label}: router ignores CoA/Disconnect from Sub",
            impact=(
                "Suspensions and plan changes cannot be pushed live; Sub falls "
                "back to API kicks."
            ),
            evidence={"incoming_accept": False},
            fix="/radius incoming set accept=yes.",
        )
    ]


# ------------------------------------------------------------- suspension path


def _chain_summary(doc: ExportDocument, chain: str) -> dict[str, EvidenceValue]:
    items = _enabled_chain_items(doc, SECTION_FILTER, chain)
    return {
        "target_chain": chain,
        "target_rule_count": len(items),
        "target_has_terminal_block": _has_terminal_block(items),
    }


def _has_terminal_block(items: Sequence[ExportItem]) -> bool:
    for item in items:
        if item.get("action") not in {"drop", "reject"}:
            continue
        if not set(item.props) <= _TERMINAL_ALLOWED_KEYS:
            continue
        dst = [item.get("dst-address"), item.get("dst-address-list")]
        if any(value is not None and not value.startswith("!") for value in dst):
            continue
        return True
    return False


def _suspension_jumps(doc: ExportDocument, suspended_list: str) -> list[ExportItem]:
    return [
        item
        for item in _enabled_chain_items(doc, SECTION_FILTER, "forward")
        if item.get("action") == "jump"
        and item.get("src-address-list") == suspended_list
    ]


def _check_suspension(
    doc: ExportDocument,
    eff: EffectiveRouterPolicy,
    label: str,
    suspended_list: str,
    *,
    has_pppoe_or_nas: bool,
) -> tuple[list[PostureFinding], set[CheckId], set[str]]:
    findings: list[PostureFinding] = []
    evaluated: set[CheckId] = set()
    reported_targets: set[str] = set()
    jumps = _suspension_jumps(doc, suspended_list)
    allowed = eff.suspension_target_chains
    if not eff.check_off("suspension-jump"):
        evaluated.add(CheckId.suspension_jump)
        bad = [j for j in jumps if (j.get("jump-target") or "") not in allowed]
        applicable = has_pppoe_or_nas or bool(jumps)
        if applicable and (not jumps or bad):
            if bad:
                target = bad[0].get("jump-target") or ""
                title = (
                    f"{label}: suspended customers are not blocked "
                    f"(jump points at legacy chain {target})"
                )
                evidence: dict[str, EvidenceValue] = {
                    "jump_comment": _comment(bad[0]),
                    "jump_target": target,
                    **_chain_summary(doc, target),
                }
                reported_targets.add(target)
            else:
                title = f"{label}: suspended customers are not blocked"
                evidence = {"jump_present": False, "suspended_list": suspended_list}
            findings.append(
                PostureFinding(
                    check=CheckId.suspension_jump,
                    qualifier="0",
                    priority=HealthPriority.critical,
                    title=title,
                    impact=(
                        "Suspended or captive customers keep internet access; "
                        "revenue leakage."
                    ),
                    evidence=evidence,
                    fix=(
                        "Point the jump at dotmac-suspended (or apply the "
                        "walled-garden module) and verify with a suspended test "
                        "account."
                    ),
                )
            )
    if not eff.check_off("suspension-chain"):
        evaluated.add(CheckId.suspension_chain)
        for target in sorted({j.get("jump-target") or "" for j in jumps} & allowed):
            chain_items = _enabled_chain_items(doc, SECTION_FILTER, target)
            if not _has_terminal_block(chain_items):
                findings.append(
                    PostureFinding(
                        check=CheckId.suspension_chain,
                        qualifier=qualifier_for(target),
                        priority=HealthPriority.critical,
                        title=f"{label}: suspension chain {target} has no final drop",
                        impact=(
                            "Suspended or captive customers keep internet access; "
                            "revenue leakage."
                        ),
                        evidence=_chain_summary(doc, target),
                        fix=(
                            "Add an unconditional drop (or reject) as the last "
                            "rule of the suspension chain."
                        ),
                    )
                )
    if not eff.check_off("suspension-jump"):
        evaluated.add(CheckId.suspension_shadowed)
        first_line = min((j.line for j in jumps), default=None)
        for item in _enabled_chain_items(doc, SECTION_FILTER, "forward"):
            if first_line is None or item.line >= first_line:
                break
            if item.get("action") in {"accept", "fasttrack-connection"} and not any(
                item.has(key) for key in _SHADOW_GUARD_KEYS
            ):
                findings.append(
                    PostureFinding(
                        check=CheckId.suspension_shadowed,
                        qualifier="0",
                        priority=HealthPriority.medium,
                        title=(
                            f"{label}: a forward rule may let suspended traffic "
                            "bypass the block"
                        ),
                        impact="Suspended customers may still pass this earlier rule.",
                        evidence={"line": item.line, "comment": _comment(item)},
                        fix="Move the suspension jump above the unconditional accept.",
                    )
                )
                break
    return findings, evaluated, reported_targets


# -------------------------------------------------------------- legacy Splynx


def _check_splynx(
    doc: ExportDocument, label: str, reported_targets: set[str]
) -> list[PostureFinding]:
    findings: list[PostureFinding] = []
    items = _all_firewall_items(doc)
    referenced = {
        item.get("jump-target") for item in items if item.get("action") == "jump"
    }
    chains: dict[str, bool] = {}
    for item in items:
        for name in (item.get("chain"), item.get("jump-target")):
            if name and name.startswith("splynx-"):
                chains[name] = name in referenced
    for name in sorted(chains):
        if name in reported_targets:
            continue
        in_use = chains[name]
        findings.append(
            PostureFinding(
                check=CheckId.splynx_legacy_chain,
                qualifier=qualifier_for(name),
                priority=HealthPriority.medium if in_use else HealthPriority.low,
                title=f"{label}: legacy Splynx firewall chain still present",
                impact=(
                    "A retired Splynx chain is still configured"
                    + (" and referenced by a jump." if in_use else " but unused.")
                ),
                evidence={"chain": name, "referenced": in_use},
                fix="Remove the legacy chain once nothing references it.",
            )
        )
    lists: set[str] = set()
    for item in items:
        for key in _ADDRESS_LIST_KEYS:
            value = item.get(key)
            if value:
                for part in value.split(","):
                    name = part.lstrip("!")
                    if name.startswith("splynx-"):
                        lists.add(name)
    for name in sorted(lists):
        findings.append(
            PostureFinding(
                check=CheckId.splynx_legacy_list,
                qualifier=qualifier_for(name),
                priority=HealthPriority.low,
                title=f"{label}: rule still depends on Splynx address list {name}",
                impact="A firewall rule depends on an address list Splynx used to fill.",
                evidence={"address_list": name},
                fix="Replace the reference with the Sub-managed list, then remove it.",
            )
        )
    return findings


# --------------------------------------------------------------- orchestration


def evaluate_posture(
    doc: ExportDocument,
    eff: EffectiveRouterPolicy,
    *,
    router_label: str,
    suspended_list: str,
    has_pppoe_or_nas: bool | None = None,
) -> PostureResult:
    """Judge one parsed export against the effective policy."""
    label = router_label
    pppoe = bool(doc.section("/interface pppoe-server server"))
    applicable = pppoe or bool(has_pppoe_or_nas)
    findings: list[PostureFinding] = []
    evaluated: set[CheckId] = set()

    v4, _ = _check_input_v4(doc, eff, label)
    findings += v4
    evaluated |= {CheckId.input_v4_missing, CheckId.input_v4_bypass}
    v6, _ = _check_input_v6(doc, eff, label)
    findings += v6
    evaluated.add(CheckId.input_v6_missing)
    findings += _check_snmp(doc, eff, label)
    evaluated.add(CheckId.snmp_exposed)
    rp, rp_evaluated = _check_reverse_proxy(doc, eff, label)
    findings += rp
    if rp_evaluated:
        evaluated.add(CheckId.reverse_proxy_enabled)
    findings += _check_ntp(doc, label)
    evaluated.add(CheckId.ntp_client_off)
    if not eff.check_off("radius-incoming"):
        findings += _check_radius_incoming(doc, label)
        evaluated.add(CheckId.radius_incoming_off)
    susp, susp_evaluated, reported = _check_suspension(
        doc, eff, label, suspended_list, has_pppoe_or_nas=applicable
    )
    findings += susp
    evaluated |= susp_evaluated
    findings += _check_splynx(doc, label, reported)
    evaluated |= {CheckId.splynx_legacy_chain, CheckId.splynx_legacy_list}

    kept: list[PostureFinding] = []
    for finding in findings:
        if eff.excepted(finding.check.value, finding.qualifier):
            continue
        if any(
            finding.check in POSTURE_CHECKS_OFF_ALIASES.get(name, ())
            for name in eff.checks_off
        ):
            continue
        issue = eff.known_issue(finding.check.value)
        kept.append(
            PostureFinding(
                check=finding.check,
                qualifier=finding.qualifier,
                priority=finding.priority,
                title=finding.title[:120],
                impact=finding.impact,
                evidence=finding.evidence,
                fix=finding.fix,
                known_issue=issue,
                layer=HealthLayer.posture,
            )
        )
    return PostureResult(
        findings=tuple(kept),
        evaluated_checks=frozenset(evaluated),
        parse_coverage=doc.coverage,
    )
