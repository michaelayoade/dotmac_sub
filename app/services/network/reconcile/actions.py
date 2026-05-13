"""Action types — one frozen dataclass per OLT/ACS write the reconciler can emit.

The planner consumes ``OntDesiredState`` + ``OntObservedState`` and emits a
sequence of these. The applier (a follow-up commit) executes them by matching
on type and calling the appropriate adapter/client method. Keeping actions as
data — no ``execute`` methods here — means the planner can be tested without
any I/O, and the applier can be tested without the planner.

Two surface families:

* ``Olt*`` actions execute via the OLT SSH adapter.
* ``Acs*`` actions execute via the GenieACS NBI client.

Each action declares its ``surface`` and ``requires_reset`` as class-level
``ClassVar``s so the planner can sort actions and decide whether to append
a trailing ``OltReset``. Constructor arguments carry the data the applier
needs to perform the write — including encrypted password references
(``*_ref``), never plaintext.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .state import WriteSurface

# ── OLT-side actions ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OltAuthorize:
    """``ont add ... sn-auth ... ont-lineprofile-id ... ont-srvprofile-id ... desc ...``.

    Emitted when an ONT is in the desired-state but absent from the OLT's
    table (fresh authorization, or migration of a deauthorized ONT). Triggers
    a final ``ont reset`` so the device picks up its profile bindings.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = True

    fsp: str
    ont_id: int
    line_profile_id: int
    service_profile_id: int
    serial_number: str
    description: str


@dataclass(frozen=True)
class OltModifyDescription:
    """``ont modify <fsp> <id> desc "..."``.

    Cosmetic but visible everywhere in OLT diagnostics. Doesn't need a reset.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    description: str


@dataclass(frozen=True)
class OltModifyLineProfile:
    """``ont modify <fsp> <id> ont-lineprofile-id <N>``.

    Profile changes alter GEM-port/TCONT mappings; the device must reset to
    re-evaluate its OMCI tree.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = True

    fsp: str
    ont_id: int
    line_profile_id: int


@dataclass(frozen=True)
class OltModifyServiceProfile:
    """``ont modify <fsp> <id> ont-srvprofile-id <N>``.

    Same rationale as line-profile changes — reset required.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = True

    fsp: str
    ont_id: int
    service_profile_id: int


@dataclass(frozen=True)
class OltClearIphost:
    """``undo ont ipconfig <fsp> <id> ip-index <N>``.

    Wipes any stale IP-host config at the given index before writing fresh
    values. Critical for reuse-registration paths (today's Fix #2 — a previous
    SmartOLT-era ONT may carry IPHOST entries at different indices than the
    new baseline writes).
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    ip_index: int


@dataclass(frozen=True)
class OltIpconfig:
    """``ont ipconfig <fsp> <id> static ip-address ... vlan ... gateway ... pri-dns ... slave-dns ...``.

    Writes the mgmt-VLAN IP host that the ACS reaches the ONT through. The
    only way the ONT gets onto VLAN 201 with a routable address — without
    this step the device authorizes but never informs the ACS.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    ip_index: int
    ip_address: str
    subnet_mask: str
    gateway: str
    vlan: int
    priority: int
    dns_primary: str | None
    dns_secondary: str | None


@dataclass(frozen=True)
class OltTr069ServerConfig:
    """``ont tr069-server-config <fsp> <id> profile-id <N>``.

    Binds the ONT to the DotMac ACS profile. Reset required for the ONT to
    pick up the new URL on its next BOOTSTRAP.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = True

    fsp: str
    ont_id: int
    profile_id: int


@dataclass(frozen=True)
class OltCreateServicePort:
    """``service-port <idx> vlan <V> gpon <fsp> ont <id> gemport <G> ...``.

    Two slots per ONT — one for mgmt (VLAN 201), one for WAN (VLAN 203). The
    allocator picks the index when the ONT is first provisioned; planner emits
    this action only when the index is set in the desired-state but the
    corresponding service-port doesn't exist on the OLT.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    service_port_index: int
    vlan: int
    gem_index: int
    slot: str  # "mgmt" or "wan" — informational, not CLI-emitted


@dataclass(frozen=True)
class OltDeleteServicePort:
    """``undo service-port <idx>``.

    Removes a stale service-port that the desired-state doesn't account for.
    The reconciler is strict about this — service-ports are system-managed,
    not user-managed.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    service_port_index: int


@dataclass(frozen=True)
class OltOmciPppoe:
    """``ont ipconfig <fsp> <id> ip-index <N> pppoe vlan <V> user-account username <U> password <P>``.

    Step 1 of the OMCI WAN sequence. Only emitted when ``wan_config_profile_id``
    is set (i.e. the per-OLT mapping has a usable profile-id, not 0/None).
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    ip_index: int
    vlan: int
    username: str
    password_ref: str  # OpenBao path; applier resolves at execute time


@dataclass(frozen=True)
class OltOmciInternetConfig:
    """``ont internet-config <fsp> <id> ip-index <N>``.

    Step 2 of the OMCI WAN sequence. Activates the TCP stack on the ip-index
    slot. Without it, OMCI PPPoE writes are mgmt-style and don't route LAN
    traffic.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    ip_index: int


@dataclass(frozen=True)
class OltOmciWanConfig:
    """``ont wan-config <fsp> <id> ip-index <N> profile-id <P>``.

    Step 3 of the OMCI WAN sequence. Promotes the WAN to routed/NAT mode.
    Requires ``profile_id > 0`` — Huawei silently no-ops on profile_id=0.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False

    fsp: str
    ont_id: int
    ip_index: int
    profile_id: int


@dataclass(frozen=True)
class OltReset:
    """``ont reset <fsp> <id>``.

    Appended once at the end of the OLT action list when any earlier action
    requires it. Multiple reset-requiring actions still produce just one
    final reset.
    """

    surface: ClassVar[WriteSurface] = "olt"
    requires_reset: ClassVar[bool] = False  # this IS the reset

    fsp: str
    ont_id: int


# ── ACS-side actions ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AcsAddObject:
    """``POST /devices/<id>/tasks {addObject: path}``.

    Creates a WANPPPConnection instance on the device. Required before
    ``AcsSetPppoe`` writes can land — setting parameters on a non-existent
    object silently queues forever.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    object_path: str  # e.g. "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection"


@dataclass(frozen=True)
class AcsSetPppoe:
    """``setParameterValues`` for WANPPPConnection.{Username, Password, Enable, X_HW_VLAN, ConnectionType, X_HW_SERVICELIST}``.

    Six params — at the edge of Huawei's 5-6 param 9002 limit. The applier
    splits into sub-batches if it crosses the threshold.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    wcd_index: int
    instance_index: int
    username: str
    password_ref: str  # OpenBao path
    vlan: int


@dataclass(frozen=True)
class AcsSetWifiSsid:
    """``setParameterValues`` for ``WLANConfiguration.1.SSID``.

    SSID is observable (the device reads back the value), so the planner
    only emits this when ``observed.acs_observed_ssid`` differs from
    ``desired.wifi_ssid``.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    ssid: str


@dataclass(frozen=True)
class AcsSetWifiPassword:
    """``setParameterValues`` for ``WLANConfiguration.1.PreSharedKey.1.KeyPassphrase``.

    Write-only field on HG8546M — the firmware doesn't expose the PSK on
    reads, so the planner emits this only on explicit triggers:
    * mode == "bootstrap" — fresh device after factory reset; rebuild full
      config including the password.
    * mode == "sync" and the ONT is being authorized (not previously present
      on the OLT) — same case as bootstrap, just initiated by an operator.
    Sweep mode never emits this — periodic polling for an unobservable
    field is theatre.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    password_ref: str  # OpenBao path


@dataclass(frozen=True)
class AcsSetNatEnabled:
    """``setParameterValues`` for ``WANPPPConnection.NATEnabled``.

    Defensive push on every routed-mode reconcile. Per Fix #4 follow-up,
    DHCP gets a defensive enable everywhere it's relevant — NAT gets the
    same treatment for symmetry.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    wcd_index: int
    instance_index: int
    enabled: bool


@dataclass(frozen=True)
class AcsSetDhcpServer:
    """``setParameterValues`` for ``LANHostConfigManagement.{DHCPServerEnable, MinAddress, MaxAddress, SubnetMask}``.

    Defensive push on every routed-mode reconcile.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    enabled: bool
    pool_min: str
    pool_max: str
    subnet_mask: str


@dataclass(frozen=True)
class AcsSetManagementServer:
    """``setParameterValues`` for ``ManagementServer.{ConnectionRequestUsername, ConnectionRequestPassword, PeriodicInformInterval}``.

    Set CR credentials so future NBI ``?connection_request`` POSTs deliver
    synchronously (return 200) instead of queueing (return 202). Inform
    interval is normalised to 300 s here too. Last in the per-reconcile ACS
    sequence so a failed earlier write doesn't leave CR creds half-set.
    """

    surface: ClassVar[WriteSurface] = "acs"
    requires_reset: ClassVar[bool] = False

    device_id: str
    cr_username: str
    cr_password_ref: str  # OpenBao path
    inform_interval_sec: int


# ── Type alias for everything the applier might see ─────────────────────────


OltAction = (
    OltAuthorize
    | OltModifyDescription
    | OltModifyLineProfile
    | OltModifyServiceProfile
    | OltClearIphost
    | OltIpconfig
    | OltTr069ServerConfig
    | OltCreateServicePort
    | OltDeleteServicePort
    | OltOmciPppoe
    | OltOmciInternetConfig
    | OltOmciWanConfig
    | OltReset
)


AcsAction = (
    AcsAddObject
    | AcsSetPppoe
    | AcsSetWifiSsid
    | AcsSetWifiPassword
    | AcsSetNatEnabled
    | AcsSetDhcpServer
    | AcsSetManagementServer
)


Action = OltAction | AcsAction


__all__ = (
    "AcsAction",
    "Action",
    "OltAction",
    "AcsAddObject",
    "AcsSetDhcpServer",
    "AcsSetManagementServer",
    "AcsSetNatEnabled",
    "AcsSetPppoe",
    "AcsSetWifiPassword",
    "AcsSetWifiSsid",
    "OltAuthorize",
    "OltClearIphost",
    "OltCreateServicePort",
    "OltDeleteServicePort",
    "OltIpconfig",
    "OltModifyDescription",
    "OltModifyLineProfile",
    "OltModifyServiceProfile",
    "OltOmciInternetConfig",
    "OltOmciPppoe",
    "OltOmciWanConfig",
    "OltReset",
    "OltTr069ServerConfig",
)
