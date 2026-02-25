# SmartOLT Feature Implementation Plan (Revised)

## Architecture Decision: Integration Strategy

DotMac Sub already has direct OLT/ONT infrastructure management (models, CRUD, SNMP, GenieACS TR-069). Rather than building a SmartOLT API connector, we **enhance the existing system** with the operational visibility and UX features that SmartOLT provides. Data comes from:

| Data Source | What It Provides |
|-------------|-----------------|
| **PostgreSQL (existing models)** | OLT/ONT inventory, assignments, splitters, fiber plant |
| **SNMP polling (existing)** | Device interface status, traffic counters, CPU/memory |
| **GenieACS (existing)** | TR-069 CPE parameters, firmware, WiFi, LAN ports |
| **VictoriaMetrics (existing)** | Time-series bandwidth, signal levels, uptime history |
| **OLT SNMP OIDs (existing)** | Optical signal levels per vendor (Huawei, ZTE, Nokia) |

No new external service dependency is needed.

---

## Gap Analysis: What Exists vs What's Missing

### ALREADY BUILT (no work needed)

| SmartOLT Feature | DotMac Sub Status | Key Files |
|-----------------|-------------------|-----------|
| OLT list with vendor/model/IP/status | **Complete** | `services/network/olt.py`, `templates/admin/network/olts/index.html` |
| OLT detail with tabs (Overview, Ports, ONTs, Activity) | **Complete** | `templates/admin/network/olts/detail.html` |
| OLT shelf/card/port hierarchy | **Complete** | `OltShelf`, `OltCard`, `OltCardPort` models + UI |
| ONT inventory with serial tracking | **Complete** | `OntUnit` model with vendor/model/firmware |
| ONT assignment to PON ports + subscribers | **Complete** | `OntAssignment` model + UI |
| ONT signal fields (ONU Rx, OLT Rx, distance) | **Complete** | `OntUnit.onu_rx_signal_dbm`, `olt_rx_signal_dbm`, `distance_meters` |
| ONT online status + offline reason | **Complete** | `OntUnit.online_status`, `offline_reason`, `last_seen_at` |
| Signal threshold classification (good/warning/critical) | **Complete** | `olt_polling.py` — `classify_signal()`, `get_signal_thresholds()` |
| Vendor-specific SNMP OID mappings | **Complete** | `olt_polling.py` — Huawei, ZTE, Nokia OIDs |
| Periodic signal polling Celery task | **Complete** | `tasks/olt_polling.py` — `poll_all_olt_signals()` |
| ONT list with signal/status/filtering | **Complete** | Advanced filters: OLT, status, signal quality, vendor, search |
| ONT list diagnostics view toggle | **Complete** | List view / Diagnostics view toggle button |
| ONT detail page with signal cards | **Complete** | Tabs: Overview, Network, History + signal quality display |
| Monitoring dashboard ONU KPI cards | **Complete** | `get_onu_status_summary()` — online/offline/low_signal |
| Monitoring dashboard PON outage table | **Complete** | `get_pon_outage_summary()` — grouped offline ONTs by port |
| Monitoring dashboard with bandwidth/NAS throughput | **Complete** | VictoriaMetrics PromQL queries |
| Core device health table (CPU/memory/uptime) | **Complete** | `NetworkDevice` with ping/SNMP checks |
| Alert rules and alarm management | **Complete** | `AlertRule`, `Alert`, `AlertEvent` models + UI |
| Zone management | **Complete** | `NetworkZone` model + `services/network/zones.py` |
| IP management (IPAM) | **Complete** | `IpPool`, `IpBlock`, `IPv4Address`, `IPv6Address` + UI |
| VLAN management | **Complete** | `Vlan` model + CRUD + UI |
| Splitter/ODB management | **Complete** | `Splitter`, `SplitterPort`, `SplitterPortAssignment` |
| Fiber plant (strands, splices, closures) | **Complete** | Full fiber topology models + UI |
| VictoriaMetrics metrics push | **Complete** | `services/metrics_store.py` — `write_samples()`, `write_aggregates()` |

### REMAINING GAPS (work to do)

| # | SmartOLT Feature | Gap Description | Priority |
|---|-----------------|-----------------|----------|
| G1 | ONU status time-series chart | 24h line chart of online/offline/signal trends on monitoring dashboard | Medium |
| G2 | OLT health metrics (CPU/temp/uptime) | OLT-specific hardware health polling via SNMP | Medium |
| G3 | Remote ONT actions (reboot, config view) | Execute actions on ONTs via GenieACS TR-069 tasks | High |
| G4 | Per-ONT traffic/signal history graphs | Historical charts on ONT detail page from VictoriaMetrics | Medium |
| G5 | TR-069 deep view on ONT detail | Structured display of CPE parameters (WAN, LAN, WiFi, firmware) | High |
| G6 | WiFi/LAN remote configuration | Configure WiFi SSID/password, enable/disable LAN ports via TR-069 | Low |
| G7 | VLAN purpose tagging | Add purpose enum (internet/mgmt/tr069/iptv/voip) and DHCP snooping flag to VLANs | Low |
| G8 | OLT config backup history | Track and display when OLT configs were backed up | Low |
| G9 | ONU authorization trend chart | Bar chart of new ONU authorizations per day | Low |
| G10 | Activity feed on monitoring dashboard | Live feed of recent network events (backups, reboots, status changes) | Low |

---

## Phase 1: Remote ONT Actions via GenieACS (High — G3)

**Goal:** Allow operators to reboot, refresh status, and view running config for ONTs directly from DotMac Sub, eliminating the need to switch to SmartOLT or GenieACS UI.

### 1A. Service: ONT action dispatcher

**New file:** `app/services/network/ont_actions.py`

```python
"""Remote ONT management actions via GenieACS TR-069."""

import logging
from sqlalchemy.orm import Session
from app.services.genieacs import GenieACSClient

logger = logging.getLogger(__name__)


class OntActions:
    """Execute remote actions on ONT devices via TR-069 (GenieACS)."""

    @staticmethod
    def reboot(db: Session, ont_id: str) -> dict:
        """Send reboot task to ONT via GenieACS.
        Returns: {success: bool, task_id: str, message: str}
        """

    @staticmethod
    def refresh_status(db: Session, ont_id: str) -> dict:
        """Force a connection request to pull latest parameters.
        Returns: {success: bool, device_info: dict}
        """

    @staticmethod
    def get_running_config(db: Session, ont_id: str) -> dict:
        """Fetch current device parameters via GenieACS.
        Returns: {success: bool, config: dict} with structured sections.
        """

    @staticmethod
    def factory_reset(db: Session, ont_id: str) -> dict:
        """Send factory reset task (requires confirmation in UI).
        Returns: {success: bool, task_id: str}
        """

ont_actions = OntActions()
```

**Implementation notes:**
- Use existing `GenieACSClient` from `app/services/genieacs.py`
- Map `OntUnit.serial_number` to GenieACS device ID (format varies: `HWTC-serial` or `serial`)
- Add device ID resolution: query GenieACS by serial number
- Audit log all remote actions

**Size:** M

### 1B. Routes: ONT action endpoints

**File:** `app/web/admin/network.py` — add POST endpoints

```python
@router.post("/onts/{ont_id}/reboot", response_class=HTMLResponse)
def reboot_ont(request: Request, ont_id: str, db: Session = Depends(get_db)):
    result = web_network_ont_actions.handle_reboot(request, db, ont_id)
    # Return HX-Trigger toast notification
    ...

@router.post("/onts/{ont_id}/refresh", response_class=HTMLResponse)
def refresh_ont(request: Request, ont_id: str, db: Session = Depends(get_db)):
    ...

@router.get("/onts/{ont_id}/config", response_class=HTMLResponse)
def ont_running_config(request: Request, ont_id: str, db: Session = Depends(get_db)):
    # Return HTMX partial with formatted config
    ...
```

**Size:** S

### 1C. UI: Action buttons on ONT detail page

**File:** `templates/admin/network/onts/detail.html`

Add action buttons to the ONT detail header area:
- **Get Status** (blue) — POST to refresh endpoint, updates signal/status cards via HTMX swap
- **Reboot** (amber, with confirmation modal) — POST to reboot endpoint
- **View Config** (slate) — GET config, display in expandable code block
- **Factory Reset** (rose, with double-confirmation) — POST to factory reset

Use HTMX for in-page actions:
```html
<button hx-post="/admin/network/onts/{{ ont.id }}/refresh"
        hx-target="#signal-cards"
        hx-swap="outerHTML"
        class="...">
    Get Status
</button>
```

**Size:** S

---

## Phase 2: TR-069 Deep View (High — G5)

**Goal:** Surface structured TR-069 device details within the ONT detail page — system health, WAN config, LAN ports, WiFi, firmware — without needing the GenieACS UI.

### 2A. Service: TR-069 parameter aggregation

**New file:** `app/services/network/ont_tr069.py`

```python
"""TR-069 parameter aggregation for ONT detail display."""

import logging
from app.services.genieacs import GenieACSClient

logger = logging.getLogger(__name__)

# TR-069 parameter path mappings (InternetGatewayDevice / Device data model)
PARAM_GROUPS = {
    "system": {
        "manufacturer": "DeviceInfo.Manufacturer",
        "model": "DeviceInfo.ModelName",
        "firmware": "DeviceInfo.SoftwareVersion",
        "hardware": "DeviceInfo.HardwareVersion",
        "serial": "DeviceInfo.SerialNumber",
        "uptime": "DeviceInfo.UpTime",
        "cpu_usage": "DeviceInfo.ProcessStatus.CPUUsage",
        "memory_total": "DeviceInfo.MemoryStatus.Total",
        "memory_free": "DeviceInfo.MemoryStatus.Free",
    },
    "wan": {
        "connection_type": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionType",
        "wan_ip": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ExternalIPAddress",
        "username": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Username",
        "status": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "uptime": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "dns_servers": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.DNSServers",
        "gateway": "WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.DefaultGateway",
    },
    "lan": {
        "ip": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceIPAddress",
        "subnet": "LANDevice.1.LANHostConfigManagement.IPInterface.1.IPInterfaceSubnetMask",
        "dhcp_enabled": "LANDevice.1.LANHostConfigManagement.DHCPServerEnable",
        "dhcp_start": "LANDevice.1.LANHostConfigManagement.MinAddress",
        "dhcp_end": "LANDevice.1.LANHostConfigManagement.MaxAddress",
    },
    "wireless": {
        "enabled": "LANDevice.1.WLANConfiguration.1.Enable",
        "ssid": "LANDevice.1.WLANConfiguration.1.SSID",
        "channel": "LANDevice.1.WLANConfiguration.1.Channel",
        "standard": "LANDevice.1.WLANConfiguration.1.Standard",
        "security_mode": "LANDevice.1.WLANConfiguration.1.BeaconType",
        "clients": "LANDevice.1.WLANConfiguration.1.TotalAssociations",
    },
}


class OntTR069:
    """Fetch and structure TR-069 parameters for ONT display."""

    @staticmethod
    def get_device_summary(serial_number: str) -> dict:
        """Return structured TR-069 data grouped by section.
        Returns: {
            system: {manufacturer, model, firmware, uptime, cpu, memory},
            wan: {type, ip, username, status, uptime, dns, gateway},
            lan: {ip, subnet, dhcp_enabled, dhcp_start, dhcp_end},
            wireless: {enabled, ssid, channel, standard, security, clients},
            raw_device: GenieACS device object (for fallback display),
            available: bool (True if device found in GenieACS),
        }
        """

    @staticmethod
    def get_lan_hosts(serial_number: str) -> list[dict]:
        """Return connected LAN hosts.
        Returns list of: {hostname, ip, mac, interface, active}
        """

    @staticmethod
    def get_ethernet_ports(serial_number: str) -> list[dict]:
        """Return Ethernet port status.
        Returns list of: {port, admin_enabled, status, speed, duplex}
        """

ont_tr069 = OntTR069()
```

**Implementation notes:**
- Use `GenieACSClient.get_device(device_id)` to fetch all parameters
- Parse the flat parameter tree into structured sections
- Handle both `InternetGatewayDevice` (TR-098) and `Device` (TR-181) root models
- Cache results briefly (parameters don't change rapidly)
- Gracefully handle missing parameters (not all devices support all paths)

**Size:** M — primary work is parameter path mapping and parsing

### 2B. Route: TR-069 data endpoint

**File:** `app/web/admin/network.py`

```python
@router.get("/onts/{ont_id}/tr069", response_class=HTMLResponse)
def ont_tr069_detail(request: Request, ont_id: str, db: Session = Depends(get_db)):
    """HTMX partial: TR-069 device details for ONT detail page tab."""
    context = web_network_ont_detail.tr069_tab_data(request, db, ont_id)
    return templates.TemplateResponse(
        "admin/network/onts/_tr069_partial.html", context
    )
```

Lazy-loaded via HTMX when the user clicks the TR-069 tab.

**Size:** S

### 2C. UI: TR-069 tab on ONT detail page

**File:** `templates/admin/network/onts/detail.html` — add 4th tab: "TR-069 / CPE"

**New file:** `templates/admin/network/onts/_tr069_partial.html`

Expandable sections (Alpine.js `x-show`):
- **System** — Manufacturer, Model, Firmware, Uptime, CPU %, Memory %
- **WAN** — Connection type (PPPoE/DHCP/Static), IP, Username, Status, Gateway, DNS
- **LAN** — IP, Subnet, DHCP range, Connected hosts table
- **Wireless** — Enabled, SSID, Channel, Standard, Security mode, Connected clients
- **Ethernet Ports** — Table: Port, Status (Up/Down), Speed, Duplex

Each section has a refresh button that re-fetches from GenieACS.

If device not found in GenieACS, show info message: "This device is not managed via TR-069."

**Size:** M

---

## Phase 3: Per-ONT Traffic & Signal History Graphs (Medium — G4)

**Goal:** Show historical traffic and signal charts on the ONT detail page, using VictoriaMetrics time-series data.

### 3A. Task: Push per-ONT signal metrics to VictoriaMetrics

**File:** `app/tasks/olt_polling.py` — extend existing task

After the existing signal polling loop, push per-ONT metrics:
```python
from app.services.metrics_store import write_samples

samples = []
for ont in polled_onts:
    labels = {
        "ont_serial": ont.serial_number,
        "olt_name": ont.olt_name,
        "pon_port": ont.pon_port_name,
    }
    if ont.onu_rx_signal_dbm is not None:
        samples.append(("ont_onu_rx_dbm", labels, ont.onu_rx_signal_dbm))
    if ont.olt_rx_signal_dbm is not None:
        samples.append(("ont_olt_rx_dbm", labels, ont.olt_rx_signal_dbm))

write_samples(samples)
```

Also push aggregate status counts:
```
onu_status_total{status="online"} 310
onu_status_total{status="offline"} 431
onu_signal_low{severity="warning"} 27
onu_signal_low{severity="critical"} 7
```

**Size:** S — extend existing task, use existing `metrics_store`

### 3B. Service: Per-ONT metric queries

**New file:** `app/services/network/ont_metrics.py`

```python
"""Query per-ONT time-series metrics from VictoriaMetrics."""

import logging
from app.services.metrics_store import query_range

logger = logging.getLogger(__name__)


def get_signal_history(ont_serial: str, hours: int = 24) -> dict:
    """Query signal level history for an ONT.
    Returns: {
        timestamps: list[str],
        onu_rx: list[float],
        olt_rx: list[float],
    }
    """
    # PromQL: ont_onu_rx_dbm{ont_serial="HWTC12345678"}[24h]


def get_traffic_history(ont_serial: str, hours: int = 24) -> dict:
    """Query traffic history for an ONT (from RADIUS accounting or SNMP).
    Returns: {
        timestamps: list[str],
        rx_bps: list[float],
        tx_bps: list[float],
    }
    """
    # PromQL: rate(ont_rx_bytes_total{ont_serial="..."}[5m])
```

**Size:** S

### 3C. Route + UI: Traffic/Signal chart tab

**File:** `app/web/admin/network.py`

```python
@router.get("/onts/{ont_id}/charts", response_class=HTMLResponse)
def ont_charts(request: Request, ont_id: str, hours: int = 24, ...):
    """HTMX partial: Traffic and signal charts for ONT detail page."""
```

**New file:** `templates/admin/network/onts/_charts_partial.html`

Two Chart.js charts loaded via HTMX when tab is activated:
1. **Signal Trend** — Line chart: ONU Rx and OLT Rx over time, with horizontal threshold lines at -25 dBm (warning) and -28 dBm (critical)
2. **Traffic** — Area chart: Rx/Tx throughput over time

Time range selector: 6h | 24h | 7d | 30d

**File:** `templates/admin/network/onts/detail.html` — add 5th tab: "Charts"

**Size:** M

---

## Phase 4: ONU Status Time-Series on Monitoring Dashboard (Medium — G1)

**Goal:** Add a 24h trend chart showing ONU online/offline/signal-loss counts to the monitoring dashboard.

### 4A. Metrics push (done in Phase 3A)

The aggregate status counts pushed in Phase 3A provide the data:
```
onu_status_total{status="online"}
onu_status_total{status="offline"}
onu_signal_low{severity="warning"}
onu_signal_low{severity="critical"}
```

### 4B. Service: Dashboard chart data

**File:** `app/services/web_network_monitoring.py` — extend `monitoring_page_data()`

New helper:
```python
def _get_onu_status_trend(hours: int = 24) -> dict:
    """Query ONU status time-series from VictoriaMetrics.
    Returns: {timestamps, online, offline, low_signal}
    """
```

**Size:** S

### 4C. UI: Chart on monitoring dashboard

**File:** `templates/admin/network/monitoring/index.html`

Add Chart.js line chart below the existing ONU KPI cards:
- **ONU Status (24h)** — 3 lines: Online (emerald), Offline (rose), Low Signal (amber)
- Auto-refreshes with the rest of the dashboard (30s HTMX poll)

**Size:** S

---

## Phase 5: OLT Hardware Health Metrics (Medium — G2)

**Goal:** Poll OLT-level hardware health (CPU, temperature, uptime, fan status) and display on OLT detail page.

### 5A. Service: OLT health polling

**File:** `app/services/network/olt_polling.py` — extend

```python
# Vendor-specific OLT health OIDs
OLT_HEALTH_OIDS = {
    "huawei": {
        "cpu": "1.3.6.1.4.1.2011.6.3.4.1.2",         # hwAvgDuty1min
        "temperature": "1.3.6.1.4.1.2011.6.3.4.1.3",  # hwEntityTemperature
        "memory": "1.3.6.1.4.1.2011.6.3.4.1.8",       # hwMemoryUtilization
    },
    "zte": { ... },
    "nokia": { ... },
}

def poll_olt_health(db: Session, olt_id: str) -> dict:
    """Poll OLT hardware health metrics.
    Returns: {cpu_percent, temperature_c, memory_percent, uptime_seconds, fan_status}
    """
```

Push results to VictoriaMetrics with `olt_name` label for historical trending.

**Size:** M — vendor OID research + SNMP queries

### 5B. Task: Add OLT health to polling cycle

**File:** `app/tasks/olt_polling.py` — extend `poll_all_olt_signals()`

After signal polling, also poll OLT health for each device. Push to VictoriaMetrics.

**Size:** S

### 5C. UI: OLT health display on detail page

**File:** `templates/admin/network/olts/detail.html` — enhance Overview tab

Add health metrics card:
- **CPU** — percentage with gauge indicator
- **Memory** — percentage with gauge indicator
- **Temperature** — degrees C with color threshold (green < 50, amber 50-65, red > 65)
- **Uptime** — human-readable (e.g., "282 days, 22:24")
- **Last polled** — relative timestamp

**Size:** S

---

## Phase 6: Lower Priority Enhancements (G6-G10)

### 6A. VLAN Purpose Tagging (G7)

**File:** `app/models/network.py` — extend `Vlan`

Add columns:
```python
purpose: Mapped[str | None] = mapped_column(
    Enum(VlanPurpose, name="vlan_purpose", create_type=True), nullable=True
)
dhcp_snooping: Mapped[bool] = mapped_column(Boolean, default=False)

class VlanPurpose(enum.Enum):
    internet = "internet"
    management = "management"
    tr069 = "tr069"
    iptv = "iptv"
    voip = "voip"
    other = "other"
```

Update VLAN form and list templates to show/edit purpose badge and DHCP snooping toggle.

**Size:** S — migration + form fields + badge display

### 6B. WiFi/LAN Remote Configuration (G6)

**File:** `app/services/network/ont_tr069.py` — extend

```python
def set_wifi_ssid(serial_number: str, ssid: str) -> dict:
    """Set WiFi SSID via GenieACS setParameterValues task."""

def set_wifi_password(serial_number: str, password: str) -> dict:
    """Set WiFi password via GenieACS."""

def toggle_lan_port(serial_number: str, port: int, enabled: bool) -> dict:
    """Enable/disable LAN port via TR-069."""
```

Add modal forms on the TR-069 tab (Phase 2C) for WiFi config and LAN port toggle.

**Size:** M — GenieACS task creation + UI modals

### 6C. OLT Config Backup History (G8)

**File:** `app/models/network.py` — new model `OltConfigBackup`

```python
class OltConfigBackup(Base):
    __tablename__ = "olt_config_backups"
    id: UUID PK
    olt_device_id: FK -> olt_devices
    backup_type: Enum (auto/manual)
    file_path: String  # S3 or local path
    file_size_bytes: Integer
    created_at: DateTime
```

Add Celery task to periodically SSH/Telnet into OLTs and pull running config.
Display on OLT detail Activity tab.

**Size:** M

### 6D. ONU Authorization Trend Chart (G9)

**File:** `app/services/web_network_monitoring.py`

Query `OntUnit.created_at` grouped by date for a bar chart:
```python
def _get_onu_auth_trend(days: int = 30) -> dict:
    """Count new ONT registrations per day."""
```

Add bar chart to monitoring dashboard.

**Size:** S

### 6E. Activity Feed on Monitoring Dashboard (G10)

**File:** `templates/admin/network/monitoring/index.html`

Add a "Recent Events" card showing latest audit log entries for network domain:
- OLT created/updated
- ONT assigned/unassigned
- Alert triggered/resolved
- Device status changes

Query from existing audit log tables filtered to network-related entities.

**Size:** S

---

## Dependency Graph

```
Phase 1 (ONT Actions)           ← independent, uses existing GenieACS
Phase 2 (TR-069 Deep View)      ← independent, uses existing GenieACS
Phase 3 (ONT Charts)            ← needs VictoriaMetrics metrics push
  └── Phase 4 (Dashboard Chart) ← depends on Phase 3A metric push
Phase 5 (OLT Health)            ← independent
Phase 6 (Lower priority items)  ← mostly independent
```

---

## Implementation Order

| Sprint | Phase | Effort | Outcome |
|--------|-------|--------|---------|
| **Sprint 1** | 1A, 1B, 1C (ONT Actions) | ~3-4 days | Reboot, refresh status, view config from ONT detail page |
| **Sprint 2** | 2A, 2B, 2C (TR-069 Deep) | ~4-5 days | Structured CPE details (system, WAN, LAN, WiFi) on ONT page |
| **Sprint 3** | 3A, 3B, 3C (ONT Charts) | ~3-4 days | Signal and traffic history graphs on ONT detail |
| **Sprint 4** | 4B, 4C (Dashboard Chart) | ~2 days | ONU status trend chart on monitoring dashboard |
| **Sprint 5** | 5A, 5B, 5C (OLT Health) | ~3-4 days | CPU/temp/memory on OLT detail page |
| **Sprint 6** | 6A-6E (Lower priority) | ~5-7 days | VLAN purpose, WiFi config, backup history, auth trend, feed |

**Total estimated effort: ~20-25 days**

---

## New Files Summary

| File | Type | Phase |
|------|------|-------|
| `app/services/network/ont_actions.py` | Service | 1A |
| `app/services/network/ont_tr069.py` | Service | 2A |
| `app/services/network/ont_metrics.py` | Service | 3B |
| `templates/admin/network/onts/_tr069_partial.html` | Template | 2C |
| `templates/admin/network/onts/_charts_partial.html` | Template | 3C |

## Modified Files Summary

| File | Changes | Phase |
|------|---------|-------|
| `app/web/admin/network.py` | ONT action POST endpoints, TR-069 GET, charts GET | 1B, 2B, 3C |
| `templates/admin/network/onts/detail.html` | Action buttons + 2 new tabs (TR-069, Charts) | 1C, 2C, 3C |
| `app/tasks/olt_polling.py` | Push per-ONT + aggregate metrics to VictoriaMetrics | 3A |
| `app/services/web_network_monitoring.py` | ONU status trend query, auth trend query | 4B, 6D |
| `templates/admin/network/monitoring/index.html` | ONU trend chart, auth chart, activity feed | 4C, 6D, 6E |
| `app/services/network/olt_polling.py` | OLT health SNMP polling | 5A |
| `templates/admin/network/olts/detail.html` | Health metrics card on Overview tab | 5C |
| `app/models/network.py` | Vlan purpose + OltConfigBackup model | 6A, 6C |

---

## Testing Strategy

| Phase | Test File | What to Test |
|-------|-----------|-------------|
| 1 | `tests/test_ont_actions.py` | Mock GenieACS responses, verify action dispatch, audit logging |
| 2 | `tests/test_ont_tr069.py` | Mock GenieACS device data, verify parameter parsing for both TR-098 and TR-181 |
| 3 | `tests/test_ont_metrics.py` | Mock VictoriaMetrics responses, verify chart data formatting |
| 4 | `tests/test_onu_monitoring_chart.py` | Verify trend query returns correct time-series structure |
| 5 | `tests/test_olt_health.py` | Mock SNMP responses per vendor, verify metric extraction |
