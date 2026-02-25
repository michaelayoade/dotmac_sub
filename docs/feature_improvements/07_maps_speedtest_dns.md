# Section 7: Maps, Speed Tests, Network Weathermap & DNS Threats

## Source: Splynx ISP Management Platform & Related Tools
These screenshots show Splynx's networking modules for GIS mapping, subscriber speed test tracking, network weathermap visualization, and DNS threat detection. Each feature is analyzed against DotMac Sub's current capabilities, and actionable improvements are proposed.

---

## 7.1 Maps -- GIS Network Mapping

**What Splynx has:**
- Full-page interactive map (OpenStreetMap tiles) displayed under Networking > Maps
- Right-side "Show" filter panel with toggleable layer checkboxes: Hardware/Routers, Partner, Customers (Online/Offline), Leads (Active/Not Active), Sectors, Fiber/Cables, Splitters, Markers
- A "Legend" panel at the bottom-right explaining the meaning of each marker color and icon
- "Apply" button to refresh the map with selected layer filters
- Map covers an entire country view (Nigeria shown) with clustered markers for subscriber/customer locations, hardware devices, and network infrastructure
- Breadcrumb navigation: Networking > Maps
- Clean integration within the Splynx sidebar under the Networking section alongside Network sites, Routers, CPE, IP networks, SpeedTest, DNS threats, and Network Weathermap

**What DotMac Sub already has:**
- A Leaflet-based network map at `/admin/network/map` with pop-site markers, fiber routes, OLT/ONT/CPE pins, and subscriber geocoded locations
- Popup details with links to device detail pages
- A legend for device types (Pop Sites, OLTs, ONTs, CPEs, FDH Cabinets, Splice Closures, Subscribers)
- Fiber route lines overlaid on the map
- Dark mode support for map popups
- An asset detail sidebar panel for quick info viewing
- Separate fiber-specific map at `/admin/network/fiber/map`
- GIS source sync module (`app/services/gis.py`, `app/services/gis_sync.py`)
- PostGIS-enabled database for geospatial queries

**Feature improvements for DotMac Sub:**

### Map Layer Controls
- [ ] **Toggleable layer filter panel**: Add a collapsible right-side panel (like Splynx) with checkboxes to show/hide: Subscribers (Online), Subscribers (Offline), OLTs, ONTs, CPEs, Pop Sites, FDH Cabinets, Splice Closures, Fiber Routes, NAS Devices, Leads/Prospects
- [ ] **Apply/refresh button for filters**: Allow users to select multiple layers and apply them in a batch rather than individual toggles, reducing map re-renders
- [ ] **Remember layer preferences**: Persist the user's last-selected layer combination in localStorage or a user preference setting so it loads their preferred view on return
- [ ] **Cluster markers at zoom levels**: Implement Leaflet.markercluster to group dense subscriber/device clusters at country/region zoom levels, expanding on zoom-in

### Subscriber Status on Map
- [ ] **Online/offline subscriber pins**: Color-code subscriber markers by connection status (emerald for online via RADIUS session, rose for offline), pulling real-time status from RADIUS accounting or device polling
- [ ] **Subscriber count overlay**: At zoomed-out levels, show cluster badges with counts (e.g., "247 online, 13 offline") for each geographic region
- [ ] **Lead/prospect layer**: Show potential customer locations from a leads pipeline (if/when a CRM leads feature is added) with distinct markers, allowing field teams to plan door-to-door sales routes

### Map Data Enrichment
- [ ] **Device status heatmap layer**: Overlay a heatmap showing areas with high device failure rates or low signal quality, using opacity gradients based on alarm density
- [ ] **Coverage area polygons**: Draw service coverage zones (e.g., FTTH coverage by POP site radius, wireless sector coverage areas) as semi-transparent polygons on the map
- [ ] **Signal quality color coding**: Color ONT markers by optical signal level (green > -25 dBm, yellow -25 to -28 dBm, red < -28 dBm) for at-a-glance fiber health
- [ ] **Search/locate on map**: Add a search bar that lets users type a subscriber name, address, or device serial number and fly to that location on the map
- [ ] **Map export/print**: Provide a "Print map" or "Export as image" button for including network map views in reports and proposals

### Map UX Enhancements
- [ ] **Full-screen toggle**: Add a full-screen button to maximize the map view beyond the admin layout constraints
- [ ] **Satellite/terrain tile switching**: Allow users to switch between OpenStreetMap street view, satellite imagery, and terrain views for field planning
- [ ] **Distance measurement tool**: A point-to-point distance ruler overlay for planning fiber runs or estimating cable lengths
- [ ] **Draw/annotate mode**: Let technicians draw temporary annotations (planned routes, problem areas) on the map that can be saved as planning notes

---

## 7.2 SpeedTest Results -- Subscriber Speed Test Tracking

**What Splynx has:**
- A tabular list view under Networking > SpeedTest result (Module "SpeedTest result")
- Table columns: Status (checkbox), ID, IP, Date and time of the test, Download speed (Mbit/s), Upload speed (Mbit/s), Ping (ms)
- Multiple rows of speed test results with timestamps, each tied to a subscriber IP address
- Example data shows tests performed over several months (October-December 2020 range visible)
- IP addresses in the range 100.73.x.x suggesting CGNAT or internal test infrastructure
- Speed results vary widely (download 0-5 Mbps, upload 0-0.5 Mbps, ping 0-167 ms), indicating real subscriber test data
- "Add" button in the top-right corner for manual entry
- Sortable columns and pagination controls
- Results linked to subscriber sessions via IP address

**What DotMac Sub already has:**
- No speed test tracking module currently exists
- Bandwidth monitoring exists via `app/services/network_monitoring.py` with aggregate Rx/Tx metrics and top consumer views
- RADIUS accounting data is available, which could be correlated with speed tests

**Feature improvements for DotMac Sub:**

### Speed Test Data Collection
- [ ] **Speed test results model**: Create a `SpeedTestResult` model with fields: `id` (UUID), `subscriber_id` (FK), `ip_address`, `tested_at` (timestamp), `download_mbps` (Decimal), `upload_mbps` (Decimal), `ping_ms` (Decimal), `jitter_ms` (Decimal, optional), `server_location` (str), `test_source` (enum: manual, scheduled, subscriber_portal, api), `created_at`
- [ ] **Speed test list view**: Admin page at `/admin/network/speedtest` showing a sortable, filterable table of all speed test results with columns: Subscriber, IP, Date, Download (Mbps), Upload (Mbps), Ping (ms), Source
- [ ] **Speed test API endpoint**: REST endpoint `POST /api/v1/speedtest/results` for ingesting speed test results from external tools (LibreSpeed, Ookla embedded, or custom scripts running on CPE devices)
- [ ] **Manual speed test entry**: "Add Result" form for technicians to manually log speed test results with subscriber lookup, date/time picker, and speed fields

### Speed Test Integration
- [ ] **Subscriber-linked results**: Link speed test results to subscriber records so tests appear on the subscriber detail page under a "Speed Tests" tab, showing historical test results for that customer
- [ ] **Auto-correlation by IP**: When a speed test result arrives with only an IP address, automatically match it to the subscriber via active RADIUS session or DHCP lease mapping
- [ ] **Scheduled speed test tasks**: Celery task to trigger periodic speed tests against subscriber CPE devices (via TR-069/ACS or embedded test agents), storing results automatically
- [ ] **Customer portal speed test**: Embed a LibreSpeed or custom speed test widget in the customer portal (`/portal/speedtest`) so subscribers can run tests from their browser, with results automatically saved to their account

### Speed Test Analytics
- [ ] **Speed test dashboard**: Summary cards showing: average download/upload across all tests, worst-performing subscribers, tests below plan threshold, trend over time
- [ ] **Plan vs actual comparison**: Compare speed test results against the subscriber's catalog plan speed (e.g., plan is 50 Mbps, tests average 42 Mbps) and flag subscribers consistently below threshold (e.g., < 80% of plan speed)
- [ ] **Speed test trend chart**: Per-subscriber line chart showing download/upload speed over time, with the plan speed as a reference line
- [ ] **Geographic speed heatmap**: Overlay average speed test results on the network map to identify areas with poor performance, colored from green (fast) to red (slow)
- [ ] **Speed test export**: CSV/Excel export of speed test results with date range and subscriber filters for regulatory reporting or internal QA

### Speed Test Alerting
- [ ] **Low-speed alert rule**: Configurable monitoring rule that triggers an alert when a subscriber's speed test results drop below a configurable percentage of their plan speed for N consecutive tests
- [ ] **Bulk speed test report**: Scheduled report showing all subscribers whose latest speed test is below threshold, useful for NOC morning reviews

---

## 7.3 Network Weathermap -- Topology & Traffic Visualization

**What Splynx has:**
- A "Network Weathermap" page under Networking in the sidebar
- The page currently shows a 404 error ("Oops! This page could not be found"), indicating the feature exists in the UI navigation but may require additional addon configuration or is not yet deployed for this instance
- Sidebar position: listed between DNS threats and Tariff plans
- "Reload" and "Open in new window" buttons in the top-right, suggesting the weathermap is designed to be viewed standalone (e.g., on a NOC screen)
- Network weathermaps are typically visualizations showing network topology with real-time traffic bandwidth on each link, using color gradients (green to red) to indicate utilization levels

**What DotMac Sub already has:**
- Network monitoring dashboard with bandwidth overview (Rx/Tx totals) at `/admin/network/monitoring`
- Device online/offline status tracking
- Per-device interface metrics and top bandwidth consumers
- Alert rules with severity levels
- No topology visualization or link utilization map

**Feature improvements for DotMac Sub:**

### Network Weathermap Core
- [ ] **Weathermap topology editor**: Admin tool to define the network topology as a visual diagram -- drag and drop network devices (routers, switches, OLTs, core devices) onto a canvas and draw links between them to represent physical/logical connections
- [ ] **Link bandwidth overlays**: Color-code each link on the weathermap based on real-time bandwidth utilization: green (0-25%), yellow (25-50%), orange (50-75%), red (75-100%), with numeric labels showing current throughput (e.g., "450 Mbps / 1 Gbps")
- [ ] **Device status nodes**: Show each device on the weathermap as a node with status indicator (green = online, red = offline, amber = degraded), displaying key metrics in a tooltip on hover (CPU, memory, uptime, interface count)
- [ ] **Auto-discovery topology**: Optionally auto-generate the weathermap layout from LLDP/CDP neighbor data collected via SNMP, reducing manual topology drawing

### Weathermap Data Sources
- [ ] **SNMP interface polling integration**: Pull real-time interface bandwidth from SNMP polling tasks (already partially built in `app/tasks/snmp.py`) and feed it into the weathermap link overlays
- [ ] **VictoriaMetrics/Prometheus data source**: Query time-series bandwidth data from VictoriaMetrics (MCP server already configured) to populate weathermap link utilization
- [ ] **Historical playback**: Allow scrubbing through a time slider to replay network utilization over the past 24h/7d, useful for identifying peak-hour congestion patterns

### Weathermap Display
- [ ] **Weathermap page route**: New page at `/admin/network/weathermap` under the Network section, with an "Open in new window" button for NOC wall display
- [ ] **Auto-refresh**: HTMX polling (every 30-60 seconds) to update link colors and bandwidth values without full page reload
- [ ] **Zoom and pan**: Support zooming into sections of large topologies with smooth pan/zoom interactions
- [ ] **Multiple weathermap views**: Support multiple named weathermap configurations (e.g., "Core Network", "Distribution Layer", "Regional POPs") for ISPs with layered network architectures
- [ ] **Threshold alerts on map**: Flash or pulse links that are above a configurable utilization threshold (e.g., > 85%) to draw NOC operator attention

### Weathermap Enhancements
- [ ] **Aggregate bandwidth summary**: Show total aggregate upstream bandwidth and utilization at the top of the weathermap as a summary stat
- [ ] **Click-through to device detail**: Clicking a device node navigates to its full device detail page; clicking a link shows interface-level traffic graphs
- [ ] **Dark mode NOC theme**: A dedicated dark/high-contrast "NOC mode" optimized for wall-mounted displays with large fonts and minimal chrome
- [ ] **Weathermap export**: Export the current weathermap view as PNG/SVG for inclusion in status reports and capacity planning documents

---

## 7.4 DNS Threats -- DNS-Based Security Monitoring

**What Splynx has:**
- A "DNS threats" page under Networking in the sidebar
- The page shows an error: "Error: whalebone_api_region is not set. Please check your addon config!" -- indicating this feature integrates with the Whalebone DNS security platform via an addon/API
- Whalebone is a DNS-based security service that detects malware, phishing, botnet C&C communication, and other threats by analyzing DNS query patterns at the resolver level
- The error message indicates the feature requires: a Whalebone account, API region configuration, and addon activation
- "Reload" and "Open in new window" buttons suggest the feature embeds or proxies data from the Whalebone API

**What DotMac Sub already has:**
- No DNS threat detection or DNS security monitoring features
- Network monitoring with SNMP-based device alerting
- Alert notification policies with escalation chains
- Integration framework for external services (OAuth tokens, webhooks)

**Feature improvements for DotMac Sub:**

### DNS Security Integration
- [ ] **DNS threat provider integration framework**: Build an abstract integration layer for DNS security providers (Whalebone, Cisco Umbrella, Cloudflare Gateway, NextDNS) with a common data model for threat events: `subscriber_ip`, `domain_queried`, `threat_type` (malware, phishing, botnet, cryptomining, adware), `action_taken` (blocked, allowed, redirected), `timestamp`, `severity`
- [ ] **Whalebone API connector**: First-class integration with Whalebone's API to pull DNS threat data, configurable under System > Settings with fields: API key, API region, polling interval
- [ ] **DNS threat dashboard**: Admin page at `/admin/network/dns-threats` showing: total threats blocked (24h/7d/30d), top threat categories (pie chart), top targeted subscribers, top blocked domains, threat trend line chart
- [ ] **DNS threat settings page**: Configuration form for setting up DNS security provider credentials, enabling/disabling the feature, and selecting the API region

### Per-Subscriber DNS Threat Visibility
- [ ] **Subscriber DNS threat tab**: On the subscriber detail page, add a "DNS Security" tab showing threat events associated with that subscriber's IP addresses, with counts by category and a table of recent blocked queries
- [ ] **Infected device detection**: Flag subscribers whose IPs generate an unusually high volume of malware/botnet DNS queries, suggesting a compromised device on their network
- [ ] **Subscriber notification on infection**: Optionally send an email or in-app notification to subscribers when their connection shows signs of malware activity, with remediation guidance

### DNS Threat Alerting
- [ ] **DNS threat alert rules**: Create monitoring alert rules that trigger when: a subscriber exceeds N blocked DNS queries in a time window, a new botnet C&C domain is detected, or a critical-severity threat is identified
- [ ] **NOC DNS threat feed**: Real-time feed of DNS threat events in the network monitoring dashboard, filterable by severity and threat type
- [ ] **Threat escalation to alert policies**: Route DNS threat alerts through the existing alert notification policy system (email, on-call rotation) for critical threats like active botnet participation

### DNS Analytics & Reporting
- [ ] **DNS threat trend report**: Periodic report (daily/weekly) showing DNS threat volumes, new threat categories, most-affected subscribers, and comparison to previous period
- [ ] **Top blocked domains list**: Table of most frequently blocked domains across all subscribers, with threat category and first/last seen timestamps
- [ ] **Geographic threat map**: Overlay DNS threat density on the network map, showing which geographic areas have the highest concentration of security events
- [ ] **Regulatory compliance export**: Export DNS threat data in formats suitable for regulatory reporting (e.g., national CERT incident reporting requirements)

---

## Priority Summary

### Critical Priority (Core ISP operations, high user impact)
- [ ] Speed test results model and list view -- fundamental for service quality management
- [ ] Subscriber-linked speed test results -- essential for customer support troubleshooting
- [ ] Plan vs actual speed comparison -- identifies service delivery issues proactively
- [ ] Toggleable map layer filter panel -- improves usability of the existing map
- [ ] Online/offline subscriber status pins on map -- immediate operational visibility

### High Priority (Significant operational value)
- [ ] Speed test API endpoint for external tool integration -- enables automated data collection
- [ ] Customer portal speed test widget -- reduces support calls, empowers subscribers
- [ ] Network weathermap core (topology + link bandwidth overlays) -- standard NOC tool
- [ ] DNS threat provider integration framework -- growing ISP security requirement
- [ ] DNS threat dashboard with blocked query statistics -- security visibility
- [ ] Speed test trend chart per subscriber -- supports QoS troubleshooting

### Medium Priority (Enhances existing features meaningfully)
- [ ] Weathermap auto-refresh and NOC display mode -- operational efficiency
- [ ] Speed test geographic heatmap -- capacity planning tool
- [ ] Subscriber DNS threat tab -- support agent context
- [ ] Low-speed alert rules -- proactive quality management
- [ ] Marker clustering on map -- performance at scale
- [ ] Coverage area polygons on map -- sales and planning tool
- [ ] VictoriaMetrics data source for weathermap -- leverages existing infrastructure

### Lower Priority (Nice-to-have, future roadmap)
- [ ] Auto-discovery topology from LLDP/CDP -- reduces manual setup
- [ ] Weathermap historical playback -- capacity planning
- [ ] DNS geographic threat map -- advanced analytics
- [ ] Map distance measurement tool -- field planning aid
- [ ] Map draw/annotate mode -- planning collaboration
- [ ] Multiple named weathermap configurations -- large network support
- [ ] Scheduled speed test tasks via TR-069 -- automated QA
- [ ] Regulatory DNS threat compliance export -- market-specific requirement
