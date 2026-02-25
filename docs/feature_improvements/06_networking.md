# Section 6: Splynx Networking & IP Management

## Source: Splynx ISP Management Platform

---

## Screenshot Analysis

### Folder 1: Splynx Networking Folder (19 screenshots)

**Screenshot 102124 -- Network Sites List**
Shows a comprehensive "Network sites" table with columns for ID, Title, Partners, Location, Postal address, and status. The list contains approximately 20+ network site entries across multiple locations (Abuja, Lagos, etc.) with partner assignments (Chibuokem, Ohakwe, Main). Each site has location data, physical address, and contact information. The table supports filtering by partner, searching, and pagination.

**Screenshot 102418 -- Network Site Detail (Information Tab)**
Shows the detail page for "Garki-Abj-Bts #2" network site with four tabs: Information, Hardware, Customer services, Map. The Information tab displays:
- Main information: Title, Description, Partners (Main), Location (Abuja), full physical address
- An embedded OpenStreetMap with GPS coordinates (9.040382, 7.484955)
- Contacts section: Full name, Email, Phone, Contact details for multiple contacts
- Documents section with "Add documents" link
- "Add photos" link at the bottom

**Screenshot 102447 -- Network Site Hardware Tab**
Shows the Hardware tab for "Garki-Abj-Bts #2" listing 13 hardware devices at this site. Table columns: ID, Title, Type (Access Point, Router, Switch), Vendor/Model (Ubiquiti, MikroTik), IP address, Ping status (green OK or red Error badges), Status (green/yellow/red badges), Access device toggle, Parent device, Product/Model, Ops count, Uptime, and Actions. Devices include GPON units, routers, access points, and switches with real-time ping and uptime monitoring.

**Screenshot 102856 -- Routers List**
Shows the main Routers list page under Networking > Routers. A dense table with columns for ID, Title, NAS type (all MikroTik), Vendor/Model, IP/Host, Physical address, Product/model, and various status indicators. Contains approximately 25+ router entries with location information, IP addresses, and partner assignments. The table supports filtering by Partner, Location, and searching.

**Screenshot 102933 -- Router Detail (Information Tab)**
Shows detail for "Abuja Core | CBD (160.119.127.251)" router with tabs: Information, Connection rules, MikroTik, MikroTik log, Map. The Information tab displays:
- Title, NAS type (MikroTik), Vendor/Model (CCR-1072)
- Partners (21 of 23 selected), Location (Abuja), Physical address
- IP/Host with Ping indicator (red "Unreachable" badge)
- Authorization: PPP/DHCP (Radius), Accounting: Radius accounting
- GPS coordinates with View/Set button
- Radius section: Radius secret, NAS IP, Pools configuration

**Screenshot 103003 -- Add Router Form**
Shows the "Add" form for creating a new router under Networking > Routers. Fields: Title (required), NAS type (MikroTik dropdown), Vendor/Model, Partners (All selected), Location (All), Physical address, IP/Host (required), NAS IP, Authorization (None dropdown), Accounting (None dropdown), GPS with View/Set button.

**Screenshot 103101 -- Add Router Form with Validation Errors**
Shows the same Add Router form with validation errors displayed as toast notifications: "Invalid IP/Host" (when entering 172.16.300.5, an invalid IP) and "NAS IP is required". Demonstrates real-time validation for IP address format and required field enforcement.

**Screenshot 103155 -- Router MikroTik Tab (API Configuration)**
Shows the MikroTik-specific tab for a router with:
- API configuration: Enable API toggle, Login (API), Password (API) with Show button, Port (8728), Test API connection button
- Shaper configuration: Enable Shaper toggle, Shaper (This router), Shaping type (Simple queue)
- Wireless Access-List toggle, Disabled customers to Address-List toggle, Blocking rules toggle
- Live bandwidth usage button
- MikroTik status section showing: Status (green "API OK"), Platform, Board name (CCR1072-1G-8S+), RouterOS Version (7.10.1 stable), CPU usage (2), IPv6 (Enabled badge), Last status timestamp
- Backups button and Check status now button

**Screenshot 103437 -- Add Hardware Device Form**
Shows the "Add" form under Networking > Hardware with fields: Title, Network site (dropdown), Parent (None), Access device toggle, Vendor (MikroTik dropdown), Product/Model, IP address, Port (161), Ping this device toggle, SNMP Monitoring toggle, SNMP community (public), SNMP Version (2), Type (Router dropdown), Group (Main), Partners (All selected), Location (All), Address, Send notifications toggle, Delay timer for notification, GPS with View/Set.

**Screenshot 103715 -- Hardware List**
Shows the complete Hardware list page with a dense table of network devices. Columns: ID, Type (Router, Switch, AP, OLT), Group, Title, Vendor/Model, IP address, Product model, Network site, Location, Ping status, SNMP status, and backup status. Contains approximately 30+ entries with color-coded status badges for ping and SNMP monitoring.

**Screenshot 104017 -- Hardware Device Detail (Information Tab)**
Shows detail for "Abuja Medallion" hardware device with tabs: Information, SNMP OID, Logs, Graphs, Backup, Map, Customer services. The Information tab shows all device properties including:
- Network site, Parent (None), Access device toggle
- Vendor (MikroTik), Product/Model (CCR1072), IP (160.119.127.254) with Ping indicator (green "119 ms")
- Last ping statuses: row of green "OK" badges
- SNMP Monitoring enabled, Last SNMP status (OK), SNMP Uptime (42 days, 19:01:36)
- SNMP community, version, Type (Router), Group (Main), Partners, Location
- Address, Send notifications toggle, Delay timer for notification, GPS

**Screenshot 104218 -- Hardware Device Tree View**
Shows the bottom portion of a hardware device detail page with a "Tree view" section displaying the hierarchical device topology. The tree shows parent-child relationships between network devices with color-coded status badges:
- AGG SWITCH - GARKI (OK) at top
  - Gudu Access (OK) with child devices
    - Lokogoma Access (OK) with switches, APs, and links
    - Eagle FM Access (OK) with switches and APs
  - Gudu Huawei OLT (Unknown), Gudu Switch (OK), GPON devices, etc.
Each device shows OK (green), Timeout (orange), or Unknown (dark) status badges.

**Screenshot 104516 -- SNMP OID Tab**
Shows the SNMP OID tab for a hardware device listing 12 monitored OIDs. Table columns: ID, Title (interface names like "vlan120=Eagle FM in/out", "sfp-sfpplus9=BOI Fiber in/out"), OID (SNMP OID strings like 1.3.6.1.2.1.31.1.1.1.6.x), Last status (all green "OK"). Actions include edit and delete. Features: SNMPWalk button, Add SNMP OID button, and table search.

**Screenshot 104611 -- Create OID Dialog**
Shows a modal dialog for creating a new SNMP OID with fields: Title, OID, Check every (1 dropdown), RRD Data Source Type (RRD not written dropdown), Enabled toggle. Close and Add buttons.

**Screenshot 104709 -- Graphs Tab**
Shows the Graphs tab for a hardware device listing 6 configured bandwidth graphs. Table columns: ID, Title (Eagle FM Airfiber, BOI Fiber, Garki Fiber, Gudu Switch, Lokogoma Fiber, Huawei OLT), Vertical title (all "Bandwidth"), and Actions (edit, clone, delete).

**Screenshot 104900 -- Edit Graph Dialog**
Shows the "Edit graph" dialog for configuring a bandwidth graph with fields:
- Title, Vertical title (Bandwidth), Height (150), Public toggle, View URL
- Add data source: select device and OID
- OID configuration table: OID ID, Title, Factor, Colour (hex), Draw type (LINE1/AREA), Stack, Value in (Bps)
- Close, Save, Preview buttons

**Screenshot 104922 -- Graph Preview**
Shows the same Edit Graph dialog with a live preview of the "Eagle FM Airfiber (Daily graph)" bandwidth chart. The graph displays in/out traffic over time (Mon 12:00 through Tue 08:00) with:
- Red line for vlan120=Lokogoma in (max 9.14 MBps, avg 3.17 MBps)
- Green area for vlan120=Lokogoma out (max 14.27 MBps, avg 10.45 MBps)
- Y-axis in Bandwidth units, proper time axis

**Screenshot 105042 -- Hardware Backup Tab**
Shows the Backup tab for "Gudu Access (160.119.127.80)" with:
- Backup configuration: Enabled toggle, Login (SSH), Password (SSH) with Show button, Port (120), Type (Commands), Commands textarea ("export"), Hours to backup at (2,8,14,20)
- Test connection and Test backup configuration buttons
- Backups list: Date column, Message column (showing IP, timestamp, "backup" entries), Operations (download and view code buttons)
- Period filter for backup history, Compare button for diff between backups

**Screenshot 105142 -- Backups List Page**
Shows a global Backups list page under Networking > Hardware. Dense table with columns: ID, Type (Router/Switch/AP/OLT), Group, Title, Vendor/Model, IP address, Port, Last backup date/time, Backup message, and status. Lists approximately 25+ devices with their backup status and timing. Shows which devices have recent backups and which may need attention.

---

### Folder 2: Networks (6 screenshots)

**Screenshot 100641 -- Tariff Plans / Recurring Plans List**
Shows the Tariff Plans > Recurring page with a table listing 13 recurring plans. Columns: ID, Title, Price (in Nigerian Naira), Customers count. Plans include:
- Internet plans: Fiber Last Mile (130,000 NGN), 45Mbps Leased Line (130,000 NGN), Unlimited 10 (75,250 NGN), unlimited 1.5 (10,750 NGN), unlimited 3 (18,812.50 NGN), Unlimited midi 5MBPS (37,625 NGN), 15mbps (1,032,000 NGN)
- IP address plans: /32 IP (2,687.50 NGN, 41 customers), /30 IP (10,750 NGN, 29 customers), /29 IP (21,500 NGN, 14 customers), /28 IP (37,625 NGN, 0 customers)
- Device Replacement plans (26,875 and 14,781.25 NGN)
Sidebar shows full Networking menu: Network sites, Routers, CPE (MikroTik), TR-069 (ACS), Hardware, IPv4 networks, IPv6 networks, Maps, SpeedTest result, DNS threats, Network Weathermap.

**Screenshot 104942 -- Add IPv4 Network Form**
Shows the "Add" form for IPv4 networks with fields: Network (e.g., 10.0.0.0), BM/Subnet Mask (24 dropdown showing "255.255.255.0 - 254 hosts, 256 IP"), Allow usage of network and broadcast IPs toggle, Title, Comment, Location (All), Network category (Dev dropdown), Network type (EndNet dropdown), Type of usage (Static dropdown). Calculator button and Add network button. Sidebar shows full Networking menu structure including IPv4 networks and IPv6 networks sections.

**Screenshot 105039 -- IPv4 Networks List (Top)**
Shows the IPv4 Networks > List page with a comprehensive table. Columns: ID, Network (CIDR notation), BM (subnet mask), RouterName, Used (usage bar showing IP utilization percentage with blue fill), Title, Location, Network type (EndNet/PoolNet), Network category (EndNet/Dev), and Actions. Entries include ranges like "Range 1" through "Range 7", "Abur IP Range", "Point To Point IPs", "Core Router IP Block", "CBD IP Range 2", "Eagle Pol Range" etc. Location filter shows "All" and individual locations. The usage bars provide visual indication of IP address utilization.

**Screenshot 105100 -- IPv4 Networks List (Middle Section)**
Continuation of the IPv4 networks list showing additional entries: "Unlimited 5", various location-specific IP ranges (Jabi, Kubwa, Lokogoma, BOI, Lugbe, Garki, Karuana, Gwagwada), IP blocks, and named ranges. The "Used" column shows usage bars -- some heavily utilized (blue fill), some empty. Network categories alternate between EndNet and Dev. All shown as /24 subnets.

**Screenshot 105118 -- IPv4 Networks List (Lower Section)**
Further continuation showing more IPv4 network entries including: "BOI IP Range 2", "CBD Fallback IP", "Kubwa Fallback IP", "Gudu Fallback IP", "Lokogoma Fallback IP", "Maitama Fallback IP", "Karu Fallback IP", "Idu Fallback IP", "Jinjani Fallback IP", "Abapo Fallback IP", "Lugbe Fallback IP", "CSS Fallback IP", "Akon Fallback IP", "Abuje Eagle Fallback IP", "Agwara Fallback IP". Network types include EndNet. Some entries categorized as "Dev" or "EndNet" categories.

**Screenshot 105138 -- IPv4 Networks List (Bottom Section)**
Final portion of the IPv4 networks list showing: "Kausa Fallback IP", "SPDC Fallback IP", "Kwara IP Range", "Kwara IP Range 2", "Ilupeju IP Ranges", "Point to Point IPs", "Gusape Fallback IP", "Dopemu Fallback IP", "AirFiber IP Management Range", "Megamme Block 2 (Reserved)", "AirFiber IP Management Range" duplicate, "DSAMN8 IP RANGE-3", "Apo IP Range 2", "Dell Server IP Block", "Fallback ip", "Lagos Medalion FW3 IPV Private". Total: 97 entries across multiple pages. The comprehensive list demonstrates extensive IP address management for a multi-location ISP.

---

### Folder 3: Networks IPv (9 screenshots)

**Screenshot 100641 -- Tariff Plans / Recurring (Duplicate)**
Same as Networks folder screenshot -- shows recurring tariff plans with Internet and IP address pricing tiers.

**Screenshot 104942 -- Add IPv4 Network Form (Duplicate)**
Same as Networks folder -- shows the IPv4 network creation form with subnet mask selection and network categorization.

**Screenshot 105039 -- IPv4 Networks List Top (Duplicate)**
Same as Networks folder -- shows the top portion of the IPv4 networks list with usage bars.

**Screenshot 105100 -- IPv4 Networks List Middle (Duplicate)**
Same as Networks folder -- continuation of IPv4 networks list.

**Screenshot 105118 -- IPv4 Networks List Lower (Duplicate)**
Same as Networks folder -- further continuation with fallback IP entries.

**Screenshot 105138 -- IPv4 Networks List Bottom (Duplicate)**
Same as Networks folder -- final portion showing 97 total entries.

**Screenshot 111554 -- Add IPv6 Network Form**
Shows the "Add" form for IPv6 networks under Networking > IPv6 networks with fields: Network (example: 2001:db8::), Prefix (32 dropdown showing "4,294,967,296 x /64"), Title, Comment, Location (All), Network category (Dev), Network type (EndNet), Type of usage (Static). Calculator button and Add network button. Sidebar shows IPv6 networks expanded with "Add" and "List" sub-items, plus Maps, SpeedTest result, DNS threats, and Network Weathermap links.

**Screenshot 111611 -- IPv6 Networks List**
Shows the IPv6 Networks > List page with approximately 23 entries. Columns: ID, Network (IPv6 addresses like 2c0f:e888:xxxx::), Prefix (48 or 64), RouterName, Used (utilization bar), Title (e.g., "Garki IPv6 Range", "Lugbe IPv6 range", "SMCC IPv6 range", "Kubura IPv6 range", "Dunampp IPv6 range", "Lokogoma IPv6 range", etc.), Location type (All or Abuja), Network type (EndNet), Network category (Dev). Actions include info, edit, clone, stats, and delete buttons per row. Location filter dropdown available.

**Screenshot 111623 -- IPv6 Networks List (Bottom)**
Shows the bottom of the IPv6 networks list with entries 25-30: "CSS IPv6 Range", "Surulere IPv6 Range", "Ilupeju IPv6 Range", "Dopemu IPv6 Range" (all /48 prefix), and "Server IPv6 Range" (/64 prefix, Location: Abuja). Total: 23 entries. All are EndNet type, Dev category. Actions include info, edit, clone, stats, and delete buttons.

---

## Proposed Feature Improvements for DotMac Sub

### 6.1 Network Sites Management

- [ ] **Add Network Sites module** -- Create a dedicated "Network Sites" section under Networking that represents physical locations (towers, POPs, data centers, cabinets). Each site should have: title, description, partner/organization assignment, location reference, full physical address, GPS coordinates with embedded map view (OpenStreetMap/Leaflet), and contact details (name, email, phone, additional notes).
- [ ] **Site-to-hardware relationship** -- Link hardware devices to network sites so operators can view all equipment at a given physical location. Include a "Hardware" tab on each site detail page showing filtered devices with their ping/SNMP status.
- [ ] **Site-to-customer-services mapping** -- Add a "Customer Services" tab on network sites showing all subscribers served from that site, enabling impact analysis when a site goes down.
- [ ] **Site map tab** -- Add a "Map" tab per network site showing the site's GPS location on an interactive map with surrounding subscriber/device markers.
- [ ] **Site photo gallery** -- Allow operators to upload and manage photos of network sites (tower photos, cabinet photos, installation shots) for field reference.
- [ ] **Site document management** -- Attach documents to network sites (lease agreements, permits, site surveys, as-built drawings) with file upload and categorization.
- [ ] **Multi-contact support per site** -- Store multiple contact persons per network site with name, phone, and role information for site access and escalation.

### 6.2 Router / NAS Device Management

- [ ] **Enhanced NAS device form** -- Extend the existing NAS device creation form to include: NAS type dropdown (MikroTik, Ubiquiti, Cisco, Huawei, Other), Vendor/Model free-text field, partner/organization multi-select, location dropdown, physical address, separate NAS IP field distinct from management IP/Host, Authorization type dropdown (PPP/DHCP Radius, Hotspot, None), Accounting type dropdown (Radius accounting, None), and GPS coordinates with map picker.
- [ ] **Router detail tabbed interface** -- Implement a tabbed detail view for NAS/router devices with tabs: Information, Connection Rules, Vendor-Specific (e.g., MikroTik API), Device Log, Map. This replaces the current flat detail view with a richer, organized interface.
- [ ] **IP address validation on NAS forms** -- Add real-time IP address format validation on the NAS device form, rejecting invalid octets (e.g., 172.16.300.5) and displaying inline error messages. Also validate that NAS IP is provided when authorization is set to a RADIUS mode.
- [ ] **MikroTik API integration panel** -- For MikroTik-type NAS devices, add a vendor-specific configuration tab with: Enable API toggle, API login/password, API port (default 8728), "Test API connection" button, and a live status panel showing Platform, Board name, RouterOS Version, CPU usage, IPv6 status, and last status check timestamp.
- [ ] **Bandwidth shaper configuration** -- Add shaper/QoS configuration per router: Enable Shaper toggle, Shaper target (this router or remote), Shaping type (Simple queue, Queue tree, HTB), Wireless Access-List toggle, Disabled-customers-to-Address-List toggle, and Blocking rules toggle.
- [ ] **Live bandwidth usage button** -- Add a "Live bandwidth usage" action button on router detail pages that opens a real-time bandwidth monitoring view for the device.
- [ ] **Router ping status indicator** -- Display a real-time ping badge (green "Reachable X ms" or red "Unreachable") next to the IP/Host field on router detail and list pages.
- [ ] **RADIUS configuration per router** -- Add a RADIUS section on router detail pages showing: Radius secret, NAS IP, and Pool assignment (with multi-select for IP pools). Include a note: "Use only these pools, if selected (in service set as Any pool)".
- [ ] **Connection rules tab** -- Add a "Connection rules" tab on router detail pages to define and manage PPPoE/DHCP connection rules, IP assignments, and rate-limit profiles associated with the router.
- [ ] **Router list filtering** -- Enhance the routers/NAS list page with filters for: Partner, Location, NAS type, online/offline status, and table search.

### 6.3 Hardware / Network Device Inventory

- [ ] **Unified hardware inventory** -- Create a comprehensive hardware inventory module separate from NAS routers that tracks all network devices: routers, switches, access points, OLTs, ONTs, and CPE. Each device should store: title, network site reference, parent device, access device toggle, vendor (MikroTik, Ubiquiti, Huawei, Other), product/model, IP address, SNMP port, and device type (Router, Switch, AP, OLT).
- [ ] **ICMP ping monitoring per device** -- Add "Ping this device" toggle with automatic periodic ping checks. Display last N ping statuses as colored badges (green OK, red Timeout) on device detail and list views. Store ping latency values for historical trending.
- [ ] **SNMP monitoring integration** -- Add SNMP monitoring configuration per hardware device: SNMP community string, SNMP version (1, 2c, 3), monitoring enabled toggle. Display SNMP uptime, last SNMP status, and poll results on device detail pages.
- [ ] **Device notification configuration** -- Per-device notification settings: "Send notifications" toggle and "Delay timer for notification" (minutes) field. The delay timer prevents flapping alerts -- only send notification if the device remains down for the configured duration.
- [ ] **Hierarchical device tree view** -- Implement a tree/topology view showing parent-child device relationships with color-coded status badges (OK=green, Timeout=orange, Unknown=gray). Allow operators to visualize the network hierarchy from aggregation switches down to access points and CPE.
- [ ] **Device parent-child relationships** -- Add a "Parent" dropdown on hardware device forms to establish hierarchy. When viewing a parent device, show all child devices in a tree view. Propagate status information up the tree for impact analysis.
- [ ] **Hardware device list with status columns** -- Enhance the hardware/core-devices list to show: Type icon/badge, Group, Title, Vendor/Model, IP address, Ping status (colored badge), SNMP status (colored badge), Uptime, Backup status, Last backup date, and action buttons. Support filtering by type, group, status, network site, and location.
- [ ] **Device uptime tracking** -- Display and store device uptime from SNMP polling. Show human-readable uptime (e.g., "42 days, 19:01:36") on device detail pages and in list columns.

### 6.4 SNMP OID Management & Monitoring

- [ ] **SNMP OID configuration per device** -- Add an "SNMP OID" tab on hardware device detail pages. Allow adding custom SNMP OIDs to monitor with fields: Title (human-readable name like "sfp-sfpplus9=BOI Fiber in"), OID string (e.g., 1.3.6.1.2.1.31.1.1.1.6.x), Check interval, RRD Data Source Type (Gauge, Counter, Derive), and Enabled toggle.
- [ ] **SNMP Walk button** -- Add an "SNMP Walk" action button that performs an SNMP walk on the device and displays discovered OIDs, allowing operators to select which OIDs to monitor without manually entering OID strings.
- [ ] **SNMP OID status tracking** -- Display last poll status (OK/Error) per OID in the OID list. Color-code status badges for quick visual assessment.
- [ ] **Interface-level SNMP monitoring** -- Automatically discover and monitor network interfaces (physical ports, VLANs, SFP modules) via SNMP, creating in/out traffic OID pairs for each interface.

### 6.5 Bandwidth Graphs & Visualization

- [ ] **Per-device bandwidth graphs** -- Add a "Graphs" tab on hardware device detail pages listing configured bandwidth graphs. Each graph should have: Title, Vertical axis title (Bandwidth), customizable height, public/private toggle, and a shareable View URL.
- [ ] **Graph editor with data source selection** -- Implement a graph editor dialog allowing operators to: select a data source device, choose specific SNMP OIDs (in/out pairs), configure display options per OID (color hex, draw type LINE1/AREA, stack toggle, value unit Bps/bps/pps), and set a scaling factor.
- [ ] **Live graph preview** -- Add a "Preview" button in the graph editor that renders a real-time RRD-style graph showing daily bandwidth utilization with in/out traffic, maximum/average/last statistics, and proper time-axis labels.
- [ ] **Public graph URLs** -- Generate shareable public URLs for bandwidth graphs that can be embedded in external dashboards or shared with partners without requiring authentication.
- [ ] **Graph cloning** -- Allow cloning an existing graph configuration to quickly create similar graphs for other devices or interfaces.
- [ ] **Aggregate bandwidth dashboard** -- Create a dashboard view showing multiple bandwidth graphs on a single page, allowing operators to monitor all critical links at a glance.

### 6.6 Device Configuration Backup

- [ ] **Automated configuration backup system** -- Implement scheduled device configuration backups via SSH with configuration: Enabled toggle, SSH login/password (encrypted at rest using credential_crypto), SSH port, Backup type (Commands/SCP/TFTP), Commands textarea (e.g., "export"), and schedule (hours to backup at, e.g., 2,8,14,20).
- [ ] **Backup history with download** -- Store backup history per device with: Date/time, Message (IP, timestamp, status), and Operations (download raw config, view formatted config). Support date-range filtering of backup history.
- [ ] **Configuration comparison/diff** -- Add a "Compare" button that allows selecting two backup snapshots and displaying a side-by-side or unified diff view highlighting configuration changes between versions.
- [ ] **Test connection and test backup buttons** -- Add "Test connection" (verify SSH connectivity) and "Test backup configuration" (run a test backup) buttons before committing backup schedules, reducing misconfiguration.
- [ ] **Global backup status overview** -- Create a global "Backups" page under Networking showing all devices with backup status: last backup date, backup message, success/failure status, device type, group, vendor/model, and IP. Allow sorting by last backup date to identify stale backups.
- [ ] **Backup failure alerts** -- Trigger notifications when scheduled backups fail, allowing operators to address connectivity or credential issues promptly.

### 6.7 IPv4 Network Management (IPAM)

- [ ] **IPv4 network (subnet) management module** -- Create a dedicated IPv4 Networks section under Networking with full IPAM (IP Address Management) capabilities. Support adding networks with: Network address (CIDR), Subnet mask/BM dropdown (showing host count and total IPs, e.g., "/24 - 254 hosts, 256 IP"), Allow usage of network and broadcast IPs toggle, Title, Comment, Location, Network category (Dev, Production), Network type (EndNet, PoolNet), Type of usage (Static, Dynamic/DHCP).
- [ ] **Subnet calculator** -- Add a "Calculator" button on the IPv4 network form that computes subnet details: network address, broadcast address, first/last usable host, total hosts, wildcard mask. Help operators plan subnetting without external tools.
- [ ] **IP utilization bars** -- Display a visual utilization bar per subnet in the network list showing the percentage of IPs in use (blue fill). This provides instant visibility into which subnets are near exhaustion and which have capacity.
- [ ] **Network list with rich columns** -- Display IPv4 networks in a sortable table with columns: ID, Network (CIDR), Subnet Mask, Router assignment, Utilization bar, Title, Location, Network type, Network category, and Actions (info, edit, clone, stats, delete).
- [ ] **Network categorization** -- Support categorizing networks as: EndNet (end-user subnets), PoolNet (RADIUS/DHCP pools), Management, Point-to-Point, Infrastructure. Allow filtering the list by category.
- [ ] **Location-based network filtering** -- Filter IPv4 networks by location to quickly find all subnets assigned to a specific POP, city, or region.
- [ ] **IP address detail view per subnet** -- Click into a subnet to see individual IP address assignments: which IPs are assigned to subscribers, which are reserved, which are available. Show assignment details (subscriber name, service, device).
- [ ] **Fallback IP range management** -- Support creating "fallback" IP ranges for each location (as seen in Splynx with entries like "Garki Fallback IP", "Lokogoma Fallback IP") that are used when primary pools are exhausted or for temporary assignments.
- [ ] **IP address conflict detection** -- Detect and alert on overlapping subnets or duplicate IP assignments across the network. Display warnings when adding a network that overlaps with an existing entry.
- [ ] **Bulk subnet operations** -- Support importing multiple subnets from CSV and bulk-updating network category, location, or type assignments.

### 6.8 IPv6 Network Management

- [ ] **IPv6 network management module** -- Create a dedicated IPv6 Networks section under Networking, mirroring the IPv4 module but with IPv6-specific fields: Network address (e.g., 2001:db8::), Prefix length dropdown (showing delegation count, e.g., "/32 = 4,294,967,296 x /64"), Title, Comment, Location, Network category, Network type (EndNet), Type of usage (Static/Dynamic).
- [ ] **IPv6 subnet calculator** -- Provide an IPv6 subnet calculator button that computes prefix details, delegation breakdowns, and address ranges for the selected prefix length.
- [ ] **IPv6 utilization tracking** -- Track and display IPv6 prefix utilization (delegated vs. available /48s or /64s) with visual utilization bars in the list view.
- [ ] **IPv6 network list** -- Display IPv6 networks in a sortable table with columns: ID, Network (IPv6 prefix), Prefix length, Router assignment, Utilization bar, Title, Location, Network type, Network category, and Actions. Support filtering by location and category.
- [ ] **Dual-stack management view** -- Provide a unified view showing both IPv4 and IPv6 network assignments per location or per subscriber, enabling operators to manage dual-stack deployments cohesively.

### 6.9 Tariff Plans & IP-Based Billing

- [ ] **IP address tariff plans** -- Support creating recurring tariff plans specifically for IP address blocks (/32, /30, /29, /28, etc.) with per-block pricing. Link these to the billing system so subscribers purchasing additional public IPs are automatically billed.
- [ ] **Subscriber count per plan** -- Display the active subscriber/customer count on each tariff plan in the catalog list, showing plan popularity and enabling capacity planning.
- [ ] **Device replacement plans** -- Support one-time or recurring "Device Replacement" plans in the catalog for CPE swap fees, hardware upgrades, and similar non-bandwidth charges.

### 6.10 Additional Networking Features (from Sidebar)

- [ ] **CPE (MikroTik) management** -- Add a dedicated CPE management module for customer-premises equipment, supporting MikroTik devices with remote management via API and Winbox.
- [ ] **TR-069 (ACS) integration** -- Integrate with TR-069 Auto-Configuration Servers for remote CPE provisioning, firmware updates, and diagnostics (especially for non-MikroTik CPE like fiber ONTs, Wi-Fi routers).
- [ ] **Network Maps** -- Implement a visual network map module that displays devices on a geographic map with status indicators, allowing operators to see network topology overlaid on real geography.
- [ ] **SpeedTest results** -- Integrate or log speed test results per subscriber or per link, enabling performance tracking and SLA verification.
- [ ] **DNS threat monitoring** -- Add a DNS threats module that monitors and reports suspicious DNS queries, blocked domains, and security threats detected at the network edge.
- [ ] **Network Weathermap** -- Implement a network weathermap visualization showing inter-device link utilization with color-coded bandwidth indicators (green=low, yellow=moderate, red=high), providing a real-time network health overview.

---

## Priority Summary

### P0 -- Critical (Core ISP Operations)

| Improvement | Subsection | Rationale |
|-------------|------------|-----------|
| Enhanced NAS device form with validation | 6.2 | Foundation for all RADIUS-based authentication and accounting |
| IPv4 network (subnet) management (IPAM) | 6.7 | Essential for IP address allocation and avoiding conflicts |
| IP utilization bars | 6.7 | Prevents subnet exhaustion and service outages |
| Router ping status indicator | 6.2 | Immediate visibility into device reachability |
| RADIUS configuration per router | 6.2 | Required for PPPoE/DHCP subscriber authentication |

### P1 -- High (Operational Efficiency)

| Improvement | Subsection | Rationale |
|-------------|------------|-----------|
| Network Sites management | 6.1 | Physical infrastructure tracking for field operations |
| Unified hardware inventory | 6.3 | Complete view of all network equipment |
| ICMP ping monitoring per device | 6.3 | Proactive outage detection |
| SNMP monitoring integration | 6.3 | Performance data collection for capacity planning |
| Automated configuration backup | 6.6 | Disaster recovery and change tracking |
| IPv6 network management | 6.8 | Dual-stack readiness for ISP growth |
| Device notification configuration | 6.3 | Alerting on device failures |
| Hierarchical device tree view | 6.3 | Understanding network topology and impact analysis |
| Subnet calculator | 6.7 | Reduces planning errors |
| IP address conflict detection | 6.7 | Prevents double-assignment incidents |

### P2 -- Medium (Enhanced Monitoring & Visualization)

| Improvement | Subsection | Rationale |
|-------------|------------|-----------|
| Per-device bandwidth graphs | 6.5 | Link utilization monitoring |
| SNMP OID configuration per device | 6.4 | Custom metric collection |
| SNMP Walk discovery | 6.4 | Simplifies OID configuration |
| Configuration comparison/diff | 6.6 | Change auditing and troubleshooting |
| Global backup status overview | 6.6 | Backup health at a glance |
| Network Maps (geographic) | 6.10 | Visual network overview |
| MikroTik API integration | 6.2 | Vendor-specific automation |
| Live bandwidth usage | 6.2 | Real-time troubleshooting |
| Graph editor with data source selection | 6.5 | Flexible monitoring dashboards |
| Router list filtering | 6.2 | Faster device lookup |

### P3 -- Low (Advanced Features)

| Improvement | Subsection | Rationale |
|-------------|------------|-----------|
| Network Weathermap | 6.10 | NOC-level visualization |
| SpeedTest results | 6.10 | SLA verification |
| DNS threat monitoring | 6.10 | Security enhancement |
| TR-069 (ACS) integration | 6.10 | Automated CPE management |
| CPE (MikroTik) management | 6.10 | Remote CPE administration |
| Public graph URLs | 6.5 | Partner/customer transparency |
| Aggregate bandwidth dashboard | 6.5 | NOC dashboard |
| IP address tariff plans | 6.9 | IP block billing automation |
| Dual-stack management view | 6.8 | Unified IPv4/IPv6 visibility |
| Bulk subnet operations | 6.7 | Large-scale network management |
| Fallback IP range management | 6.7 | Redundancy planning |
| Bandwidth shaper configuration | 6.2 | QoS management via UI |
| Device replacement plans | 6.9 | CPE swap billing |
| Site photo gallery | 6.1 | Field reference documentation |
| Site document management | 6.1 | Lease/permit storage |
| Backup failure alerts | 6.6 | Proactive backup monitoring |
| Graph cloning | 6.5 | Configuration efficiency |

---

## Implementation Notes

### Alignment with Existing DotMac Sub Architecture

1. **NAS Devices** -- DotMac Sub already has a NAS device model (`NasDevice`) and management module under `/admin/nas`. Many router/NAS improvements (6.2) should extend this existing module rather than creating new models.

2. **Hardware / Core Devices** -- The existing `/admin/network/core-devices` page can be extended to serve as the unified hardware inventory (6.3). The current network monitoring module (`/admin/network/monitoring`) already handles some SNMP and ping functionality.

3. **IP Management** -- This is largely a new module. Create new models (`IPv4Network`, `IPv6Network`, `IPAssignment`) with services under `app/services/network/ipam.py`.

4. **Network Sites** -- This is a new concept. Create a `NetworkSite` model with relationships to hardware devices and subscribers. Web routes under `/admin/network/sites`.

5. **Configuration Backup** -- The existing NAS backup cleanup task (`app/tasks/nas.py`) can be extended. SSH credentials should use `credential_crypto` for encryption at rest.

6. **Bandwidth Graphs** -- Consider integrating with existing monitoring infrastructure (VictoriaMetrics MCP server is already configured). Store graph configurations in the database and render via a charting library (Chart.js or similar) in the HTMX frontend.

7. **Service Layer Pattern** -- All new features must follow the established service-layer pattern: thin web routes calling service methods, business logic in `app/services/`, Pydantic schemas for validation.
