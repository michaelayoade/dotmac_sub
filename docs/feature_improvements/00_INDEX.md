# Feature Improvements Index

## Source Material
Screenshots from **Splynx** and **SmartOLT** reviewed on 2026-02-24 to identify feature gaps and improvements for DotMac Sub.

**Total:** 276 screenshots across 11 sections, ~4,600 lines of analysis

---

## Documents

| # | Section | File | Items | Priority Breakdown |
|---|---------|------|-------|--------------------|
| 01 | [SmartOLT Features](01_smartolt_features.md) | OLT/ONT management, dashboard, diagnostics | ~40 | 4 P0, 4 P1, 4 P2, 4 P3 |
| 02 | [Selfcare Portal & Messaging](02_selfcare_and_messages.md) | Tariff plans, bundles, mass messaging, delivery tracking | ~60 | 5 P0, 8 P1, 8 P2, 8 P3 |
| 03 | [Customer Module](03_customer_module.md) | Tabbed customer detail, services, billing, statistics, CPE, documents | ~73 | 5 P0, 10 P1, 12 P2, 15 P3 |
| 04 | [Administration](04_administration.md) | Admin dashboard, roles, logs, 20+ report types | ~43 | 7 P0, 8 P1, 10 P2, 18 P3 |
| 05 | [Finance & Billing](05_finance.md) | Finance dashboard, MRR/ARPU, bank import, billing runs, aging | ~60 | 5 P0, 10 P1, 15 P2, ~25 P3 |
| 06 | [Networking & IP Management](06_networking.md) | Network sites, IPAM, device inventory, backups, SNMP, bandwidth | ~67 | 5 P0, 10 P1, 10 P2, 17 P3 |
| 07 | [Maps, SpeedTest, Weathermap, DNS](07_maps_speedtest_dns.md) | GIS layers, speed test tracking, NOC weathermap, DNS threat monitoring | ~55 | 5 P0, 6 P1, 7 P2, 8 P3 |
| 08 | [System Configuration](08_config.md) | 29 config areas: billing, RADIUS, email, tax, portal, CPE, FUP | ~140 | 7 P0, 8 P1, 9 P2, 10 P3 |
| 09 | [Helpdesk & Scheduling](09_helpdesk_scheduling.md) | Ticketing, field scheduling, checklists, automation, notifications | ~86 | High: ticketing core, Medium: scheduling |
| 10 | [Leads/CRM & Inventory](10_leads_inventory.md) | Sales pipeline, quoting, lead conversion, stock management | ~50 | 6-phase implementation recommended |
| 11 | [Integrations & Admin Tools](11_integrations_tools.md) | Module marketplace, webhooks, data import/export, migration tools | ~55 | 4 P0, 5 P1, 5 P2, 4 P3 |

**Estimated total improvement items: ~730**

---

## Cross-Section P0 (Critical) Summary

These are the highest-impact items across all sections:

### Operational Visibility
- Network dashboard with ONU online/offline/signal KPIs (Section 01)
- Finance dashboard with MRR, ARPU, period comparison (Section 05)
- Overdue invoice aging report (0-30, 30-60, 60-90, 90+ days) (Section 05)
- Status change audit logs for subscribers, services, invoices (Section 04)

### Revenue & Billing
- Billing cycle run management with preview mode (Section 05)
- Bank statement import with payment pairing (Section 05)
- Invoice PDF batch generation and email delivery (Section 05)
- Tax configuration and reporting (Section 08)
- Billing automation settings (charge day, grace period, suspension rules) (Section 08)

### Customer Management
- Tabbed customer detail page (info, services, billing, statistics, documents) (Section 03)
- Per-subscriber billing configuration overrides (Section 03)
- Customer statement generation (Section 03)
- Mass messaging with advanced recipient targeting (Section 02)

### Network & Infrastructure
- Full IPAM (IPv4/IPv6 subnet management with utilization tracking) (Section 06)
- NAS device form validation and RADIUS config (Section 06)
- RADIUS configuration settings (NAS type, attributes, CoA) (Section 08)
- PON outage table for fault identification (Section 01)

### Configuration & Platform
- Email/SMTP configuration UI (Section 08)
- Company information and branding settings (Section 08)
- API key management (Section 04)
- Webhook/event hook configuration (Section 11)

---

## New Module Candidates

These sections describe entirely new modules not currently in DotMac Sub:

| Module | Section | Effort Estimate |
|--------|---------|-----------------|
| **Helpdesk/Ticketing** | 09 | Large (5 phases) |
| **Field Scheduling** | 09 | Large (ties to helpdesk) |
| **Lead/CRM Pipeline** | 10 | Medium (6 phases) |
| **Inventory/Stock** | 10 | Medium |
| **Speed Test Tracking** | 07 | Small-Medium |
| **Network Weathermap** | 07 | Medium |
| **DNS Threat Monitoring** | 07 | Small (integration) |
| **Module Marketplace** | 11 | Medium |

---

## Implementation Strategy

### Phase 1 — Foundation (existing module enhancements)
- Finance dashboard improvements (Section 05)
- Customer detail tabbed layout (Section 03)
- System configuration UI (Section 08)
- IPAM enhancements (Section 06)

### Phase 2 — Operational Tools
- Helpdesk/ticketing module (Section 09)
- Bank statement import (Section 05)
- Mass messaging (Section 02)
- Network monitoring dashboard (Section 01)

### Phase 3 — Growth Features
- Lead/CRM pipeline (Section 10)
- Field scheduling (Section 09)
- Inventory management (Section 10)
- SmartOLT deep integration (Section 01)

### Phase 4 — Advanced
- Network weathermap (Section 07)
- Module marketplace (Section 11)
- DNS threat monitoring (Section 07)
- Service migration tools (Section 11)
