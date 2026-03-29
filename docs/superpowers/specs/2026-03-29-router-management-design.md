# Router Management Module — Design Spec

**Date:** 2026-03-29
**Module:** `router_management` (within dotmac_sub)
**Status:** Approved

## Overview

Central management module for 16+ MikroTik access routers within dotmac_sub. Provides router inventory, bulk config push via RouterOS REST API, config snapshots for audit, live health monitoring, and admin web UI. Routers are accessed directly or via SSH jump hosts.

This is a new module in dotmac_sub, not a standalone app. It leverages existing infrastructure: credential encryption, NetworkDevice monitoring, NetworkOperation state machine, RBAC, audit logging, Celery workers, and the admin portal.

## Architecture

### How It Fits

A physical MikroTik device may already exist as:
- **NasDevice** — for RADIUS/PPPoE subscriber management
- **NetworkDevice** — for SNMP monitoring, alerts, dashboards

The new **Router** model represents the **config management concern**. It links to those existing records via optional FKs but doesn't duplicate them. One device, three roles, clean separation.

### Communication Flow

- **App to Router (direct):** `httpx` async HTTP client to RouterOS REST API (`https://<ip>/rest/...`)
- **App to Router (via jump host):** `sshtunnel.SSHTunnelForwarder` opens local port forward, then REST API hits `localhost:<tunnel_port>`
- **Credentials:** Encrypted at rest via existing `credential_crypto`, decrypted only at connection time

### Approach Decision

**Hybrid (Approach C):** The app handles monitoring, inventory, and config push directly via REST API. Oxidized integration for config backup/git versioning is deferred to a future phase. Config snapshots (app-captured pre/post change) provide audit trail in the interim.

## Data Models

### Router

Central record for each managed router.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `name` | str, unique | Human-friendly label |
| `hostname` | str | RouterOS system identity |
| `management_ip` | str | IP or hostname for REST API |
| `rest_api_port` | int, default 443 | |
| `rest_api_username` | str, encrypted | via credential_crypto |
| `rest_api_password` | str, encrypted | via credential_crypto |
| `use_ssl` | bool, default True | |
| `verify_tls` | bool, default False | Self-signed certs common |
| `routeros_version` | str, nullable | Synced from device |
| `board_name` | str, nullable | Synced from device |
| `architecture` | str, nullable | Synced from device |
| `serial_number` | str, nullable | Synced from device |
| `firmware_type` | str, nullable | Synced from device |
| `location` | str, nullable | Physical location |
| `notes` | text, nullable | |
| `tags` | JSON, nullable | Arbitrary key-value |
| `access_method` | enum: direct, jump_host | |
| `jump_host_id` | UUID FK -> JumpHost, nullable | |
| `nas_device_id` | UUID FK -> NasDevice, nullable | Links to RADIUS config |
| `network_device_id` | UUID FK -> NetworkDevice, nullable | Links to monitoring |
| `status` | enum: online, offline, degraded, maintenance, unreachable | |
| `last_seen_at` | datetime, nullable | Last successful API call |
| `last_config_sync_at` | datetime, nullable | Last config snapshot |
| `last_config_change_at` | datetime, nullable | Last push or detected change |
| `reseller_id` | UUID FK, nullable | Multi-tenancy |
| `organization_id` | UUID FK, nullable | Multi-tenancy |
| `is_active` | bool, default True | Soft delete |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### JumpHost

SSH tunnel endpoints for routers behind NAT/firewalls.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `name` | str, unique | |
| `hostname` | str | SSH host |
| `port` | int, default 22 | |
| `username` | str | |
| `ssh_key` | text, encrypted, nullable | Private key |
| `ssh_password` | str, encrypted, nullable | Fallback |
| `is_active` | bool, default True | |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### RouterInterface

Cached snapshot of each router's interfaces, periodically synced.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `router_id` | UUID FK -> Router | |
| `name` | str | e.g. `ether1`, `sfp-sfpplus1` |
| `type` | str | e.g. `ether`, `wlan`, `bridge`, `vlan` |
| `mac_address` | str, nullable | |
| `is_running` | bool | |
| `is_disabled` | bool | |
| `rx_byte` | bigint, default 0 | Counter |
| `tx_byte` | bigint, default 0 | Counter |
| `rx_packet` | bigint, default 0 | Counter |
| `tx_packet` | bigint, default 0 | Counter |
| `last_link_up_time` | str, nullable | RouterOS uptime format |
| `speed` | str, nullable | e.g. `1Gbps` |
| `comment` | str, nullable | |
| `synced_at` | datetime | Last sync timestamp |

Unique constraint: `(router_id, name)`

### RouterConfigSnapshot

Point-in-time config captures for audit trail.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `router_id` | UUID FK -> Router | |
| `config_export` | text | `/export` output |
| `config_hash` | str | SHA256 of config_export |
| `source` | enum: manual, scheduled, pre_change, post_change | |
| `captured_by` | UUID, nullable | User ID or null for system |
| `created_at` | datetime | |

### RouterConfigTemplate

Reusable config snippets for bulk push.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `name` | str, unique | |
| `description` | text, nullable | |
| `template_body` | text | Jinja2 template text |
| `category` | enum: firewall, queue, address_list, routing, dns, ntp, snmp, system, custom | |
| `variables` | JSON | Schema of expected variables |
| `is_active` | bool, default True | |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### RouterConfigPush

Audit trail of every config push.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `template_id` | UUID FK -> RouterConfigTemplate, nullable | Null for ad-hoc pushes |
| `commands` | JSON | Array of commands sent |
| `variable_values` | JSON, nullable | Rendered template variables |
| `initiated_by` | UUID | User who triggered push |
| `status` | enum: pending, running, completed, partial_failure, failed, rolled_back | |
| `created_at` | datetime | |
| `completed_at` | datetime, nullable | |

### RouterConfigPushResult

Per-router outcome of a push.

| Field | Type | Notes |
|-------|------|-------|
| `id` | UUID PK | |
| `push_id` | UUID FK -> RouterConfigPush | |
| `router_id` | UUID FK -> Router | |
| `status` | enum: pending, success, failed, skipped | |
| `response_data` | JSON, nullable | API response |
| `error_message` | text, nullable | |
| `pre_snapshot_id` | UUID FK -> RouterConfigSnapshot, nullable | |
| `post_snapshot_id` | UUID FK -> RouterConfigSnapshot, nullable | |
| `duration_ms` | int, nullable | |
| `created_at` | datetime | |

### Extended Enums

Add to existing `NetworkOperationType`:
- `router_config_push`
- `router_config_backup`
- `router_reboot`
- `router_firmware_upgrade`
- `router_bulk_push`

## Services

### RouterConnectionService

Handles all REST API communication with routers.

**Methods:**
- `connect(router) -> httpx.AsyncClient` — builds authenticated client (direct or via tunnel)
- `_create_tunnel(router) -> SSHTunnelForwarder` — opens SSH tunnel through JumpHost
- `execute(router, method, path, payload) -> dict` — single REST API call with retry, timeout
- `execute_batch(router, commands: list[dict]) -> list[dict]` — multiple commands in sequence
- `test_connection(router) -> ConnectionTestResult` — verify reachability + auth

**Behavior:**
- `httpx` timeouts: 10s connect, 30s read
- Retry with exponential backoff (3 attempts) on transient failures
- Tunnel pool: reuses open tunnels, Celery task closes idle ones
- All credentials decrypted via `credential_crypto.decrypt_credential()`

### RouterInventoryService

CRUD + device sync.

**Methods:**
- `create`, `get`, `list`, `update`, `delete` — standard CRUD (soft delete)
- `sync_system_info(router)` — pulls `/rest/system/resource` and `/rest/system/routerboard`, updates Router fields
- `sync_interfaces(router)` — pulls `/rest/interface`, upserts RouterInterface records
- `bulk_import(csv_data)` — import routers from CSV

**Behavior:**
- Emits audit events on all mutations
- List supports filters: status, access_method, jump_host, search text
- Multi-tenancy enforced on all queries

### RouterConfigService

Config snapshots and push.

**Methods:**
- `capture_snapshot(router, source)` — calls `/rest/export`, stores as RouterConfigSnapshot
- `push_commands(router, commands)` — sends commands via REST API, returns results
- `push_template(template, routers, variables)` — renders Jinja2 template per-router, creates RouterConfigPush + per-router results, executes via Celery
- `rollback(push_result)` — re-applies pre-change snapshot (best-effort)

**Behavior:**
- Pre-change snapshot captured automatically before every push
- Post-change snapshot captured after successful push
- Dangerous command blocklist enforced before execution (see Security section)

### RouterMonitoringService

Extends existing monitoring infrastructure.

**Methods:**
- `sync_to_network_device(router)` — creates/updates linked NetworkDevice record
- `get_dashboard_summary()` — aggregates: total routers, online/offline/degraded counts, recent changes, active alerts
- `get_router_health(router)` — live pull of `/rest/system/resource` (CPU, memory, uptime, disk)

**Behavior:**
- No new monitoring infrastructure — piggybacks on existing AlertRule + Alert system
- Linking to NetworkDevice enables existing SNMP polling, alerts, and dashboards automatically

## Celery Tasks

| Task | Schedule | Description |
|------|----------|-------------|
| `sync_all_routers_system_info` | Every 6 hours | Updates routeros_version, board_name, etc. |
| `sync_all_routers_interfaces` | Every 15 minutes | Refreshes RouterInterface cache |
| `capture_scheduled_snapshots` | Daily | Captures config export for all routers |
| `cleanup_idle_tunnels` | Every 5 minutes | Closes unused SSH tunnel connections |

## API Endpoints

### Router CRUD — `/api/v1/network/routers/`

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/routers` | `router:read` | List with filters |
| POST | `/routers` | `router:write` | Create with connection test |
| GET | `/routers/{id}` | `router:read` | Detail with linked NAS/NetworkDevice |
| PATCH | `/routers/{id}` | `router:write` | Update |
| DELETE | `/routers/{id}` | `router:write` | Soft delete |
| POST | `/routers/{id}/test-connection` | `router:read` | Verify REST API reachability |
| POST | `/routers/{id}/sync` | `router:write` | Trigger system info + interface sync |

### Config Management

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/routers/{id}/snapshots` | `router:read` | Config snapshot history |
| POST | `/routers/{id}/snapshots` | `router:write` | Capture snapshot now |
| GET | `/routers/{id}/snapshots/{snap_id}` | `router:read` | View config export |

### Config Templates

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/config-templates` | `router:read` | List templates |
| POST | `/config-templates` | `router:write` | Create template |
| PATCH | `/config-templates/{id}` | `router:write` | Update template |
| POST | `/config-templates/{id}/preview` | `router:read` | Render with variables (dry run) |

### Bulk Config Push

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| POST | `/config-pushes` | `router:push_config` | Execute push |
| GET | `/config-pushes` | `router:read` | Push history |
| GET | `/config-pushes/{id}` | `router:read` | Push detail with per-router results |
| POST | `/config-pushes/{id}/rollback` | `router:push_config` | Rollback a push |

### Jump Hosts — `/api/v1/network/jump-hosts/`

| Method | Path | Permission | Description |
|--------|------|------------|-------------|
| GET | `/jump-hosts` | `router:read` | List |
| POST | `/jump-hosts` | `router:admin` | Create |
| PATCH | `/jump-hosts/{id}` | `router:admin` | Update |
| DELETE | `/jump-hosts/{id}` | `router:admin` | Delete |
| POST | `/jump-hosts/{id}/test` | `router:admin` | Test SSH connectivity |

## Admin Web UI

### Pages

| Route | Description |
|-------|-------------|
| `/admin/network/routers/` | Router list — status badges, search, filter by status/access-method/jump-host. Bulk actions: sync all, export CSV |
| `/admin/network/routers/dashboard` | Overview cards: online/offline/degraded counts, recent config changes, active alerts |
| `/admin/network/routers/new` | Create router form with inline connection test button |
| `/admin/network/routers/{id}` | Tabbed detail: Overview (system info, links), Interfaces (live table), Config (snapshot history), Push History (audit trail) |
| `/admin/network/routers/{id}/edit` | Edit router |
| `/admin/network/routers/templates` | Config template list |
| `/admin/network/routers/templates/new` | Template editor with variable schema builder |
| `/admin/network/routers/templates/{id}` | Template detail with preview/dry-run |
| `/admin/network/routers/push` | Bulk push wizard: select template or paste commands, select target routers, preview rendered config, confirm and execute |
| `/admin/network/routers/push/{id}` | Push results: per-router status, expandable response/error, rollback button |
| `/admin/network/routers/jump-hosts` | Jump host management |

### UI Patterns

Follows existing dotmac_sub conventions:
- HTMX for partial page updates (list filtering, tab switching, live status refresh)
- Alpine.js for client-side interactivity (form validation, toggles, modals)
- Tailwind CSS with dark mode pairs (`bg-white dark:bg-slate-800`)
- Reusable macros: `status_badge()`, `empty_state()`, `live_search()`
- Every `{% for %}` loop has `{% else %}` + `empty_state()`

## Error Handling

**Connection failures:**
- httpx timeouts: 10s connect, 30s read
- Retry with exponential backoff (3 attempts) on transient failures (timeout, connection reset)
- Jump host tunnel failures: retry once, then mark router `unreachable`
- All connection errors logged to AuditEvent

**Bulk push failures:**
- Push continues on individual router failure
- Failed router gets `failed` status on its RouterConfigPushResult with error_message
- Overall push status: `completed` (all success), `partial_failure` (some failed), `failed` (all failed)

## Security

**Dangerous command blocklist** — checked before execution, push rejected if matched:
- `/system/reset-configuration`
- `/system/shutdown`
- `/file/remove` (bulk)
- `/user/remove` (removing the API user itself)

**Credential security:**
- All passwords/keys encrypted at rest via credential_crypto
- REST API credentials never logged (even in error messages)
- Jump host SSH keys stored encrypted, decrypted only at tunnel creation time

**RBAC permissions:**
- `router:read` — view routers, templates, push history, snapshots
- `router:write` — create/edit routers, templates, trigger sync/snapshots
- `router:push_config` — execute config pushes and rollbacks (separate from write — destructive action)
- `router:admin` — manage jump hosts, delete routers

**Multi-tenancy:**
- All queries filtered by `reseller_id` / `organization_id`
- Config pushes scoped to accessible routers only

## Out of Scope (Future Phases)

- Oxidized integration for config backup/git versioning and config diff
- Config drift detection
- Firmware upgrade orchestration
- Router discovery (subnet scanning)
- REST API proxy / live terminal in UI
