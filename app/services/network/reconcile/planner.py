"""compute_plan(desired, observed, mode) -> Plan.

Pure function. Given the desired state and the last-read observed state,
emits an ordered list of actions that, when applied successfully, leaves the
device matching the desired state.

Ordering rules
==============

* **OLT actions before ACS actions.** The OLT establishes the device on the
  PON and routes its mgmt VLAN; without that the ACS can't reach it.
* **Authorize first.** A device that isn't in the OLT's table has nothing
  else applied to it.
* **Service-port repair before IPHOST.** IPHOST writes depend on the
  mgmt-VLAN service-port being present.
* **TR-069 binding after IPHOST.** The TR-069 URL profile is only useful
  once the mgmt IP can route to it.
* **OMCI WAN sequence (PPPoE → internet-config → wan-config) as a unit.**
  Only emitted when ``wan_config_profile_id`` is set; otherwise TR-069
  owns WAN PPP.
* **Trailing OltReset** if any earlier action sets ``requires_reset``.
* **ACS addObject before setParameterValues.** Setting a parameter on a
  non-existent object silently queues forever on HG8546M.
* **NAT/DHCP defensives unconditional in routed mode.** Today's Fix #4
  follow-up — explicit pushes guard against firmware variation.
* **ManagementServer (CR creds + inform interval) last.** Last in the
  sequence so a failure earlier doesn't leave the device in a state where
  the next NBI write can't be delivered.

What the planner doesn't do
===========================

* No I/O — pure function.
* No write-secret resolution — passwords carry ``*_ref`` references to be
  resolved at apply time.
* No batching/chunking — the applier groups adjacent ACS writes if needed.
* No retries — the applier handles failures.
* No locking — the locking primitive lives in ``locking.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .actions import (
    AcsAddObject,
    AcsDeleteObject,
    AcsSetDhcpServer,
    AcsSetManagementServer,
    AcsSetNatEnabled,
    AcsSetPppoe,
    AcsSetWifiPassword,
    AcsSetWifiSsid,
    Action,
    OltAuthorize,
    OltClearIphost,
    OltCreateServicePort,
    OltDeleteServicePort,
    OltIpconfig,
    OltModifyDescription,
    OltModifyLineProfile,
    OltModifyServiceProfile,
    OltOmciInternetConfig,
    OltOmciPppoe,
    OltOmciWanConfig,
    OltReset,
    OltTr069ServerConfig,
)
from .state import (
    Drift,
    OntDesiredState,
    OntObservedState,
    ReconcileMode,
    WriteSurface,
)

# ── Plan ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Plan:
    """Output of ``compute_plan``.

    ``required_surfaces`` is the set of write surfaces the ``actions`` list
    will touch. The precondition layer in ``reconcile_ont`` fast-fails the
    entire reconcile if any of these surfaces was unreachable at read time —
    so a plan that needs ACS but the ACS is down returns up front, before
    any OLT write is attempted.
    """

    actions: tuple[Action, ...]
    drifts: tuple[Drift, ...]
    required_surfaces: frozenset[WriteSurface]

    @property
    def is_empty(self) -> bool:
        return not self.actions


# ── compute_plan ────────────────────────────────────────────────────────────

# Fields a narrow WiFi UI edit may touch. When an operator's proposed change is
# confined to these, the plan is scoped to the matching ACS writes so unrelated
# OLT drift doesn't block the action.
_WIFI_ONLY_FIELDS = frozenset({"wifi_ssid", "wifi_password_ref"})


def _is_wifi_only_change(mode: ReconcileMode, proposed_fields: frozenset[str]) -> bool:
    return mode == "sync" and bool(proposed_fields) and proposed_fields <= _WIFI_ONLY_FIELDS


def compute_plan(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    *,
    proposed_fields: frozenset[str] | None = None,
    force_proposed_writes: bool = True,
) -> Plan:
    """Diff desired vs observed; emit the ordered action list.

    Determinism guarantee: ``compute_plan(d, o, m) == compute_plan(d, o, m)``
    for all inputs. No randomness, no time-of-day dependence, no I/O.

    ``proposed_fields`` carries the operator's current requested mutations.
    For narrow WiFi edits, it scopes the plan to the relevant ACS writes so
    unrelated drift does not block the UI action. ``force_proposed_writes`` is
    used for write-only values, where observed state cannot prove drift; verify
    calls keep the scope but disable the force.
    """
    actions: list[Action] = []
    drifts: list[Drift] = []
    proposed_fields = proposed_fields or frozenset()
    wifi_only_change = _is_wifi_only_change(mode, proposed_fields)

    if wifi_only_change:
        omci_wan_planned = False
    else:
        _plan_olt_side(desired, observed, mode, actions, drifts)
        omci_wan_planned = _plan_olt_omci_wan(
            desired, observed, mode, actions, drifts
        )
        _append_reset_if_needed(desired, actions)
    _plan_acs_side(
        desired,
        observed,
        mode,
        actions,
        drifts,
        omci_wan_planned,
        proposed_fields,
        force_proposed_writes,
    )

    required_surfaces = frozenset(a.surface for a in actions)
    return Plan(
        actions=tuple(actions),
        drifts=tuple(drifts),
        required_surfaces=required_surfaces,
    )


# ── OLT-side planning ───────────────────────────────────────────────────────


def _plan_olt_side(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    olt_obs = observed.olt

    # 1. Authorize if absent.
    if not olt_obs.olt_present:
        actions.append(
            OltAuthorize(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                line_profile_id=desired.line_profile_id,
                service_profile_id=desired.service_profile_id,
                serial_number=desired.serial_number,
                description=desired.description,
            )
        )
        drifts.append(
            Drift(
                field="olt_present",
                surface="olt",
                desired=True,
                observed=False,
                repairable=True,
            )
        )
        # When we authorize, we provide line/srv profile + description in the
        # same `ont add` — no need to emit separate modify actions.
    else:
        # Present — diff individual fields.
        if _observed_differs(olt_obs.olt_line_profile_id, desired.line_profile_id):
            actions.append(
                OltModifyLineProfile(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    line_profile_id=desired.line_profile_id,
                )
            )
            drifts.append(
                Drift(
                    field="olt_line_profile_id",
                    surface="olt",
                    desired=desired.line_profile_id,
                    observed=olt_obs.olt_line_profile_id,
                    repairable=True,
                )
            )

        if _observed_differs(
            olt_obs.olt_service_profile_id, desired.service_profile_id
        ):
            actions.append(
                OltModifyServiceProfile(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    service_profile_id=desired.service_profile_id,
                )
            )
            drifts.append(
                Drift(
                    field="olt_service_profile_id",
                    surface="olt",
                    desired=desired.service_profile_id,
                    observed=olt_obs.olt_service_profile_id,
                    repairable=True,
                )
            )

        if _observed_differs(olt_obs.olt_description, desired.description):
            actions.append(
                OltModifyDescription(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    description=desired.description,
                )
            )
            drifts.append(
                Drift(
                    field="olt_description",
                    surface="olt",
                    desired=desired.description,
                    observed=olt_obs.olt_description,
                    repairable=True,
                )
            )

    # 2. Service-port repair — strict, system-managed. Stale ports removed,
    # missing ports created.
    _plan_service_ports(desired, observed, actions, drifts)

    # 3. IPHOST — only meaningful when mgmt VLAN is set.
    if desired.mgmt_vlan is not None and desired.mgmt_ip is not None:
        if _iphost_differs(desired, observed):
            # Stale-clear at both common indices before writing the new one.
            # Mirrors today's Fix #2 (the baseline sweep clear).
            for ip_index in (0, 1):
                actions.append(
                    OltClearIphost(
                        fsp=desired.fsp,
                        ont_id=desired.olt_ont_id,
                        ip_index=ip_index,
                    )
                )
            actions.append(
                OltIpconfig(
                    fsp=desired.fsp,
                    ont_id=desired.olt_ont_id,
                    ip_index=0,
                    ip_address=desired.mgmt_ip,
                    subnet_mask=desired.mgmt_subnet_mask or "255.255.255.0",
                    gateway=desired.mgmt_gateway or "",
                    vlan=desired.mgmt_vlan,
                    priority=desired.mgmt_iphost_priority,
                    dns_primary=desired.mgmt_dns_primary,
                    dns_secondary=desired.mgmt_dns_secondary,
                )
            )
            drifts.append(
                Drift(
                    field="olt_mgmt_ip",
                    surface="olt",
                    desired=desired.mgmt_ip,
                    observed=olt_obs.olt_mgmt_ip,
                    repairable=True,
                )
            )

    # 4. TR-069 binding. We re-emit on every fresh authorize because the
    # observed-side doesn't yet parse the TR-069 profile (parser TODO in
    # the OLT reader).
    if not olt_obs.olt_present:
        actions.append(
            OltTr069ServerConfig(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                profile_id=desired.tr069_profile_id,
            )
        )


def _plan_service_ports(
    desired: OntDesiredState,
    observed: OntObservedState,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    desired_indices = {
        desired.mgmt_service_port_index,
        desired.wan_service_port_index,
    }
    desired_indices.discard(None)

    # Delete stale service-ports that aren't in the desired set.
    for sp in observed.olt.olt_service_ports:
        idx = sp.get("index") if isinstance(sp, dict) else None
        if idx is None or idx in desired_indices:
            continue
        actions.append(OltDeleteServicePort(service_port_index=int(idx)))
        drifts.append(
            Drift(
                field=f"olt_service_ports[{idx}]",
                surface="olt",
                desired=None,
                observed=sp,
                repairable=True,
            )
        )

    # Create missing service-ports — but only when the allocator has assigned
    # an index. If indices are still None, the allocator hasn't run yet and
    # this is the planner's job to flag (handled by reconcile_ont with a
    # specific failure reason; not a plannable action here).
    observed_indices = {
        sp.get("index") for sp in observed.olt.olt_service_ports if isinstance(sp, dict)
    }
    if (
        desired.mgmt_service_port_index is not None
        and desired.mgmt_service_port_index not in observed_indices
        and desired.mgmt_vlan is not None
    ):
        actions.append(
            OltCreateServicePort(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                service_port_index=desired.mgmt_service_port_index,
                vlan=desired.mgmt_vlan,
                gem_index=2,  # GEM 2 is the mgmt slot in line profile 40
                slot="mgmt",
            )
        )
    if (
        desired.wan_service_port_index is not None
        and desired.wan_service_port_index not in observed_indices
        and desired.wan_vlan is not None
        and desired.wan_mode == "pppoe"
    ):
        actions.append(
            OltCreateServicePort(
                fsp=desired.fsp,
                ont_id=desired.olt_ont_id,
                service_port_index=desired.wan_service_port_index,
                vlan=desired.wan_vlan,
                gem_index=desired.wan_gem_index or 1,
                slot="wan",
            )
        )


def _plan_olt_omci_wan(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
) -> bool:
    """Emit the three-command OMCI WAN sequence when ``wan_config_profile_id``
    is set. Returns ``True`` if OMCI owns WAN PPP — used downstream to decide
    whether TR-069 WAN actions should also fire."""
    if (
        desired.wan_mode != "pppoe"
        or desired.wan_pppoe_provisioning_method == "tr069"
        or desired.wan_config_profile_id is None
        or desired.wan_config_profile_id <= 0
        or desired.wan_internet_config_ip_index is None
    ):
        return False

    ip_index = desired.wan_internet_config_ip_index
    actions.append(
        OltOmciPppoe(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
            vlan=desired.wan_vlan or 0,
            username=desired.wan_pppoe_username or "",
            password_ref=desired.wan_pppoe_password_ref or "",
        )
    )
    actions.append(
        OltOmciInternetConfig(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
        )
    )
    actions.append(
        OltOmciWanConfig(
            fsp=desired.fsp,
            ont_id=desired.olt_ont_id,
            ip_index=ip_index,
            profile_id=desired.wan_config_profile_id,
        )
    )
    return True


def _append_reset_if_needed(desired: OntDesiredState, actions: list[Action]) -> None:
    if any(getattr(a, "requires_reset", False) for a in actions):
        actions.append(OltReset(fsp=desired.fsp, ont_id=desired.olt_ont_id))


# ── ACS-side planning ───────────────────────────────────────────────────────


def _plan_acs_side(
    desired: OntDesiredState,
    observed: OntObservedState,
    mode: ReconcileMode,
    actions: list[Action],
    drifts: list[Drift],
    omci_wan_planned: bool,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> None:
    if not desired.acs_server_id:
        # Without an ACS server bound, no ACS actions to plan. Reconciler
        # callers would normally refuse this in routed mode, but the planner
        # itself stays pure.
        return

    device_id = _acs_device_id(desired)

    wifi_only_change = _is_wifi_only_change(mode, proposed_fields)

    # TR-069 WAN PPP — skipped when OMCI owns the WAN.
    if not wifi_only_change and desired.wan_mode == "pppoe" and not omci_wan_planned:
        _plan_acs_wan_ppp(desired, observed, device_id, actions, drifts)

    # WiFi SSID — observable. Push when the read value differs OR when this
    # is a fresh bring-up (no ACS record yet — we have to seed it).
    push_ssid = _observed_differs(
        observed.acs.acs_observed_ssid, desired.wifi_ssid
    ) or (not observed.acs.acs_present)
    if push_ssid:
        actions.append(AcsSetWifiSsid(device_id=device_id, ssid=desired.wifi_ssid))
        drifts.append(
            Drift(
                field="wifi_ssid",
                surface="acs",
                desired=desired.wifi_ssid,
                observed=observed.acs.acs_observed_ssid,
                repairable=True,
            )
        )

    # WiFi password — unobservable, mode-gated (Hole 3 resolution).
    if _should_push_wifi_password(
        mode,
        observed,
        proposed_fields,
        force_proposed_writes,
    ):
        actions.append(
            AcsSetWifiPassword(
                device_id=device_id,
                password_ref=desired.wifi_password_ref,
            )
        )

    if wifi_only_change:
        return

    # Defensive NAT on routed mode (Fix #4 follow-up).
    if desired.wan_mode == "pppoe" and not omci_wan_planned:
        wcd = desired.wan_pppoe_wcd_index
        inst = (
            _desired_wan_ppp_instance(desired, observed)
            or desired.wan_pppoe_instance_index
        )
        if _wan_ppp_needs_heal(desired, observed) or _observed_differs(
            observed.acs.acs_observed_nat_enabled, desired.nat_enabled
        ):
            actions.append(
                AcsSetNatEnabled(
                    device_id=device_id,
                    wcd_index=wcd,
                    instance_index=inst,
                    enabled=desired.nat_enabled,
                )
            )
            drifts.append(
                Drift(
                    field="nat_enabled",
                    surface="acs",
                    desired=desired.nat_enabled,
                    observed=observed.acs.acs_observed_nat_enabled,
                    repairable=True,
                )
            )

        for stale_wcd, stale_inst in _stale_wan_ppp_locations(desired, observed):
            actions.append(
                AcsDeleteObject(
                    device_id=device_id,
                    object_path=(
                        "InternetGatewayDevice.WANDevice.1."
                        f"WANConnectionDevice.{stale_wcd}."
                        f"WANPPPConnection.{stale_inst}."
                    ),
                )
            )

    # DHCP server — push the whole block when any field differs.
    if _dhcp_differs(desired, observed):
        actions.append(
            AcsSetDhcpServer(
                device_id=device_id,
                enabled=desired.dhcp_enabled,
                pool_min=desired.dhcp_pool_min,
                pool_max=desired.dhcp_pool_max,
                subnet_mask=desired.dhcp_subnet_mask,
            )
        )
        drifts.append(
            Drift(
                field="dhcp_enabled",
                surface="acs",
                desired=desired.dhcp_enabled,
                observed=observed.acs.acs_observed_dhcp_enabled,
                repairable=True,
            )
        )

    # ManagementServer (CR creds + inform interval) — last in the ACS
    # sequence. Critical for the next reconcile's NBI calls to deliver
    # synchronously.
    if _management_server_differs(desired, observed):
        actions.append(
            AcsSetManagementServer(
                device_id=device_id,
                cr_username=desired.cr_username or "admin",
                cr_password_ref=desired.cr_password_ref or "",
                inform_interval_sec=desired.periodic_inform_interval_sec,
            )
        )


def _plan_acs_wan_ppp(
    desired: OntDesiredState,
    observed: OntObservedState,
    device_id: str,
    actions: list[Action],
    drifts: list[Drift],
) -> None:
    """Plan WAN PPP via TR-069 — addObject if missing, then PPPoE params."""
    target_inst = _desired_wan_ppp_instance(desired, observed)
    # If ACS doesn't have a WAN PPP instance, addObject first.
    if target_inst is None:
        actions.append(
            AcsAddObject(
                device_id=device_id,
                object_path=(
                    f"InternetGatewayDevice.WANDevice.1."
                    f"WANConnectionDevice.{desired.wan_pppoe_wcd_index}."
                    f"WANPPPConnection"
                ),
            )
        )
        drifts.append(
            Drift(
                field="acs_wan_ppp_instance",
                surface="acs",
                desired=desired.wan_pppoe_instance_index,
                observed=None,
                repairable=True,
            )
        )
        target_inst = desired.wan_pppoe_instance_index

    # PPPoE params — diff or set.
    if _wan_ppp_needs_heal(desired, observed) or _wan_ppp_differs(desired, observed):
        actions.append(
            AcsSetPppoe(
                device_id=device_id,
                wcd_index=desired.wan_pppoe_wcd_index,
                instance_index=target_inst,
                username=desired.wan_pppoe_username or "",
                password_ref=desired.wan_pppoe_password_ref or "",
                vlan=desired.wan_vlan or 0,
            )
        )
        drifts.append(
            Drift(
                field="wan_pppoe_username",
                surface="acs",
                desired=desired.wan_pppoe_username,
                observed=observed.acs.acs_observed_pppoe_username,
                repairable=True,
            )
        )


# ── Diff helpers ────────────────────────────────────────────────────────────


def _observed_differs(observed_value, desired_value) -> bool:
    """A field is in drift when the observed value is set AND differs.

    Crucially: an observed value of ``None`` means "we didn't read this
    field" — not "the field is empty on the device". Treat None-observed as
    "no drift signal" to avoid emitting writes against unknown state.

    Exception: fresh authorizations (where olt_present was False) get fully
    bootstrapped in ``_plan_olt_side`` regardless of this helper.
    """
    if observed_value is None:
        return False
    return observed_value != desired_value


def _iphost_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    olt = observed.olt
    if olt.olt_mgmt_ip is not None and olt.olt_mgmt_ip != desired.mgmt_ip:
        return True
    if olt.olt_mgmt_vlan is not None and olt.olt_mgmt_vlan != desired.mgmt_vlan:
        return True
    # Fresh authorization always needs the iphost write — even when the OLT
    # reader hasn't populated mgmt_ip yet (None).
    if not olt.olt_present:
        return True
    return False


def _wan_ppp_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    if _observed_differs(acs.acs_observed_pppoe_username, desired.wan_pppoe_username):
        return True
    if _observed_differs(acs.acs_observed_wan_vlan, desired.wan_vlan):
        return True
    if acs.acs_observed_pppoe_enable is False:
        return True
    # Fresh device: ACS has nothing yet; planner must establish the PPP.
    if not acs.acs_present:
        return True
    return False


def _observed_wan_ppp_locations(
    observed: OntObservedState,
) -> tuple[tuple[int, int], ...]:
    acs = observed.acs
    if acs.acs_observed_wan_ppp_locations:
        return acs.acs_observed_wan_ppp_locations
    if (
        acs.acs_observed_wan_wcd_index is not None
        and acs.acs_observed_wan_instance_index is not None
    ):
        return ((acs.acs_observed_wan_wcd_index, acs.acs_observed_wan_instance_index),)
    return ()


def _primary_observed_wan_ppp_location(
    observed: OntObservedState,
) -> tuple[int, int] | None:
    acs = observed.acs
    if (
        acs.acs_observed_wan_wcd_index is not None
        and acs.acs_observed_wan_instance_index is not None
    ):
        return (
            acs.acs_observed_wan_wcd_index,
            acs.acs_observed_wan_instance_index,
        )
    return None


def _desired_wan_ppp_instance(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> int | None:
    primary = _primary_observed_wan_ppp_location(observed)
    if primary and primary[0] == desired.wan_pppoe_wcd_index:
        return primary[1]
    instances = [
        inst
        for wcd, inst in _observed_wan_ppp_locations(observed)
        if wcd == desired.wan_pppoe_wcd_index
    ]
    if not instances:
        return None
    # Prefer the lowest discovered instance when the reader did not expose a
    # primary location on the desired WCD. This avoids steering toward a
    # later duplicate child and keeps deleteObject disabled for ambiguous
    # layouts until we have a confirmed primary target.
    return min(instances)


def _target_wan_ppp_location(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> tuple[int, int] | None:
    inst = _desired_wan_ppp_instance(desired, observed)
    if inst is None:
        return None
    return (desired.wan_pppoe_wcd_index, inst)


def _stale_wan_ppp_locations(
    desired: OntDesiredState,
    observed: OntObservedState,
) -> tuple[tuple[int, int], ...]:
    target = _target_wan_ppp_location(desired, observed)
    primary = _primary_observed_wan_ppp_location(observed)
    # Only prune duplicates when the target matches the same live child the
    # reader used for observed values. Otherwise the layout is ambiguous and
    # deleteObject risks removing the active session.
    if target is None or primary != target:
        return ()
    return tuple(
        pair for pair in _observed_wan_ppp_locations(observed) if pair != target
    )


def _wan_ppp_needs_heal(desired: OntDesiredState, observed: OntObservedState) -> bool:
    locations = _observed_wan_ppp_locations(observed)
    if not locations:
        return False
    if _desired_wan_ppp_instance(desired, observed) is None:
        return True
    return bool(_stale_wan_ppp_locations(desired, observed))


def _dhcp_differs(desired: OntDesiredState, observed: OntObservedState) -> bool:
    acs = observed.acs
    if _observed_differs(acs.acs_observed_dhcp_enabled, desired.dhcp_enabled):
        return True
    # DHCP pool min/max/mask are write-only on most HG8546M firmwares — once
    # set we accept what's on the device. The defensive enable on every fresh
    # bring-up covers the no-DHCP-by-default case from feedback_ont_setup_defaults.
    if not acs.acs_present:
        return True
    return False


def _management_server_differs(
    desired: OntDesiredState, observed: OntObservedState
) -> bool:
    acs = observed.acs
    if (
        acs.acs_observed_periodic_inform_interval_sec
        != desired.periodic_inform_interval_sec
    ):
        return True
    # Empty/missing/mismatched CR username drives every NBI write toward 202
    # (queued, not delivered) — always emit when not confirmed equal.
    if acs.acs_observed_cr_username != desired.cr_username:
        return True
    if not acs.acs_observed_cr_password_set:
        return True
    return False


def _should_push_wifi_password(
    mode: ReconcileMode,
    observed: OntObservedState,
    proposed_fields: frozenset[str],
    force_proposed_writes: bool,
) -> bool:
    """Push at explicit desired-state change, fresh sync, and BOOTSTRAP.

    The sweeper never pushes the PSK — no observable value can confirm drift.
    In sync mode, an operator-proposed password change is an explicit write
    request and must emit exactly one ACS action on the apply pass. Verification
    calls omit ``proposed_fields`` so the write-only password is not re-emitted.
    """
    if mode == "bootstrap":
        return True
    if (
        force_proposed_writes
        and mode == "sync"
        and "wifi_password_ref" in proposed_fields
    ):
        return True
    if mode == "sync" and not observed.olt.olt_present:
        return True
    return False


def _acs_device_id(desired: OntDesiredState) -> str:
    """Construct the GenieACS device-id. OUI and ProductClass aren't on the
    OntUnit today (see adapters.py — TODO).  Reconciler uses Huawei-default
    OUI + HG8546M as the placeholder; the actual writer resolves the real
    device-id via the serial-suffix query in the reader.
    """
    return f"00259E-HG8546M-{desired.serial_number}"


__all__ = ("Plan", "compute_plan")
