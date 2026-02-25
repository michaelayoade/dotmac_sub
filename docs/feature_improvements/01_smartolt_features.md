# Section 1: SmartOLT Feature Analysis

## Source: SmartOLT Cloud Platform (smartolt.com)
SmartOLT is a cloud-based OLT management platform used alongside DotMac Sub. These screenshots show features that could be integrated or replicated in DotMac Sub to reduce context-switching between platforms.

---

## 1.1 Dashboard — Real-Time ONU/OLT Overview

**What SmartOLT has:**
- Top KPI cards: Waiting Authorization (0), Online (310), Total Offline (431), Low Signals (34)
- Offline breakdown: PwrFail: 37, LoS: 23, N/A: 371
- Low signal breakdown: Warning: 27, Critical: 7
- Daily network status graph (ONUs over time): Online, Power fail, Signal loss, N/A lines
- ONU authorizations per day bar chart
- OLT sidebar list with uptime, temperature, and alert indicators
- PON outage table: OLT name, Board/Port, ONUs affected, LOS count, Power count, Possible cause, Since when
- Info feed: auto config backups, ONU reboots (with timestamps)

**Feature improvements for DotMac Sub:**
- [ ] **Network Dashboard KPI cards**: Add real-time ONU status summary (online/offline/low signal counts) to the network monitoring dashboard
- [ ] **ONU status breakdown**: Show offline reason categories (power fail, loss of signal, N/A) not just total counts
- [ ] **Low signal alerts**: Warning vs Critical thresholds for optical signal levels
- [ ] **Daily ONU status graph**: Time-series chart showing ONU online/offline/signal-loss trends over 24h
- [ ] **ONU authorization trend**: Bar chart of new ONU authorizations per day
- [ ] **OLT health sidebar**: Temperature, uptime, and alert status per OLT at a glance
- [ ] **PON outage table**: Dedicated view showing which PON ports have outages, affected ONU count, cause, duration
- [ ] **Activity feed**: Recent events (config backups, device reboots) in a live feed panel

---

## 1.2 OLT List & Details

**What SmartOLT has:**
- OLT list table: ID, Name, OLT IP, TCP port, UDP port, Hardware version, Software version, Status (enabled/disabled)
- Export OLTs list button
- OLT detail page with tabbed navigation:
  - OLT details (settings: name, IP, telnet creds, SNMP communities, hardware/software version, PON type, TR069 profile)
  - OLT cards (slot/type/ports/SW version/status/role with reboot-card action)
  - PON ports (per-port status: admin state, ONU count, signal levels, range, description)
  - Uplink ports (type, admin state, negotiation, MTU, wavelength, temp, PVID, tagged VLANs)
  - VLANs (VLAN ID, description, purpose flags: IPTV/Mgmt/VoIP, DHCP snooping, ONU count)
  - ONU Mgmt IPs, Remote ACLs, VoIP profiles, Advanced tabs

**Feature improvements for DotMac Sub:**
- [ ] **OLT hardware inventory**: Show card slots, card types, port counts, firmware versions per OLT
- [ ] **OLT card management**: View card status and trigger card reboot from UI
- [ ] **PON port detail view**: Per-port ONU count, admin state, auto-negotiation, signal levels, descriptions
- [ ] **Uplink port monitoring**: MTU, wavelength, temperature, PVID, tagged VLANs per uplink
- [ ] **VLAN management per OLT**: CRUD for VLANs with purpose tagging (TR069, Management, Internet), DHCP snooping toggle, ONU count per VLAN
- [ ] **OLT settings view**: Consolidated view of IP, credentials, SNMP, hardware/software detection
- [ ] **Config backup history**: Show when auto-config backups were saved per OLT

---

## 1.3 Customer ONT Detail Page

**What SmartOLT has:**
- Device identity: OLT, Board, Port, ONU slot, GPON channel, Serial Number, ONU type, Zone, ODB (Splitter)
- Customer info: Name, Address/comment, Contact, Authorization date
- Live status: Online/Offline indicator with last-seen time
- Optical signals: ONU Rx signal and OLT Rx signal in dBm with distance estimate (e.g., -20.41 dBm / -24.09 dBm, 3970m)
- Network config: Attached VLANs, ONU mode (Routing/Bridging), TR069 status, Mgmt IP, WAN setup mode, PPPoE credentials
- Action buttons: Get status, Show running-config, SW info, TR069 Stat, LIVE!
- CLI output panel showing device details (Vendor-ID, ONT Version, Equipment-ID, Software versions, Product description)
- Traffic/Signal graphs: Real-time ONU traffic and signal level charts
- Speed profiles: Service-port ID, User/VLAN, Download/Upload speeds, Configure action
- Ethernet ports: Port name, admin state, mode (LAN), DHCP status, Configure action
- WiFi settings: SSID, mode, admin state, DHCP, Configure action
- VoIP, CATV status
- Bottom actions: Reboot, Delete config, Restore defaults, Disable, Delete

**Feature improvements for DotMac Sub:**
- [ ] **ONT detail dashboard**: Comprehensive single-page view combining device identity, customer info, optical signals, and network config
- [ ] **Live optical signal display**: Show ONU/OLT Rx signal in dBm with color-coded thresholds and distance estimate
- [ ] **Remote device actions**: Get status, show running-config, reboot, disable from DotMac Sub UI (via SmartOLT API or direct)
- [ ] **Traffic graphs per ONT**: Real-time and historical traffic charts per customer device
- [ ] **Signal level graphs**: Historical signal strength trending per ONT
- [ ] **Speed profile management**: View/configure speed profiles (bandwidth) per ONT from subscriber detail page
- [ ] **Ethernet port status**: Show LAN port status (up/down, mode, DHCP) per ONT
- [ ] **WiFi management**: View/configure WiFi SSIDs on customer ONTs remotely
- [ ] **TR069 deep view**: CPU usage, RAM, uptime, pending provisions, with expandable sections for PPP interface, port forwarding, LAN DHCP, LAN ports, wireless, hosts, security, voice lines, device logs, firmware management

---

## 1.4 Configured ONT List & Diagnostics

**What SmartOLT has:**
- Searchable/filterable table of all configured ONTs with columns: ONU type, Profile, PON type, OLT, Board, Port, Zone, ODB, Signal, VLAN, WiFi, TV, Type, Auth date
- Multi-filter: search, OLT, ONU type, Profile, PON type, Status, Board, Port, Zone, ODB, VLAN
- Diagnostics view: Signal levels, distance, last seen, status per ONU with quick-filter by signal quality
- Total count display (e.g., "1,500 ONUs of 746 displayed")

**Feature improvements for DotMac Sub:**
- [ ] **Advanced ONT list filtering**: Multi-dimensional filtering (by OLT, zone, signal quality, VLAN, ONT type, profile)
- [ ] **Signal quality column**: Color-coded signal strength in the ONT list view
- [ ] **Bulk diagnostics view**: List all ONTs sorted by signal quality for proactive maintenance
- [ ] **Zone/ODB grouping**: Group ONTs by zone and ODB splitter for field operations

---

## 1.5 Unconfigured Devices & Auto-Authorization

**What SmartOLT has:**
- Unconfigured devices page with OLT filter and refresh
- Auto actions panel: Configure actions, Task history, Start/Stop auto actions
- Authorization presets for quick ONU provisioning
- "Add ONU for later authorization" button

**Feature improvements for DotMac Sub:**
- [ ] **Unconfigured device queue**: Show newly discovered but unprovisioned ONTs from SmartOLT
- [ ] **Auto-authorization rules**: Define rules for automatic ONU authorization based on OLT/port/zone
- [ ] **Authorization presets**: Pre-configured profiles for one-click ONT provisioning
- [ ] **Deferred authorization**: Queue an ONU for later authorization (field tech workflow)

---

## 1.6 SmartOLT Settings & User Management

**What SmartOLT has:**
- Settings categories: Zones, ODBs, ONU types, Speed profiles, OLTs, VPN & TR069, Authorization presets, General, Billing
- Audit log with filters: OLT, User, Action, Date range
- User management: Name, Email, 2FA status, Group (admins/tech_users/readonly_users), Restriction groups, Status, Last login
- Group/role management with restriction groups for OLT-level access control

**Feature improvements for DotMac Sub:**
- [ ] **Zone management**: Define and manage geographic zones for ONT organization
- [ ] **ODB/Splitter management**: Track optical distribution boxes (splitters) and map ONTs to them
- [ ] **ONU type library**: Maintain a catalog of supported ONT hardware types with specifications
- [ ] **Speed profile management**: Define bandwidth profiles (download/upload) reusable across subscribers
- [ ] **SmartOLT audit log integration**: Pull or display SmartOLT action logs within DotMac Sub
- [ ] **OLT-scoped access control**: Restrict technician access to specific OLTs (restriction groups concept)

---

## Summary of Priority Improvements from SmartOLT

### High Priority (operational visibility)
1. Network dashboard with ONU online/offline/signal KPIs
2. ONT detail page with optical signals and device status
3. PON outage table for rapid fault identification
4. Advanced ONT list with signal quality filtering

### Medium Priority (operational efficiency)
5. OLT card/port detail views
6. VLAN management per OLT
7. Unconfigured device queue with auto-authorization
8. Zone and ODB management

### Lower Priority (nice-to-have / deeper integration)
9. Remote device actions (reboot, config view) via SmartOLT API
10. Traffic/signal graphs per ONT
11. WiFi/Ethernet port management
12. TR069 deep device management
