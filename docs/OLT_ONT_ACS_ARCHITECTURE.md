# OLT / ONT / ACS Architecture

This document maps the relationships between OLT, ONT, and ACS services in the DotMac Sub codebase.

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                    ENTRY POINTS                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐                   │
│  │  Web Routes      │  │  API Routes      │  │  Celery Tasks    │                   │
│  │  (admin/network) │  │  (api/v1)        │  │  (tasks/)        │                   │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘                   │
└───────────┼─────────────────────┼─────────────────────┼─────────────────────────────┘
            │                     │                     │
            ▼                     ▼                     ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              EXECUTION LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    authorization_executor.py                                    │ │
│  │         execute_authorization() / execute_authorization_batch()                 │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                  olt_authorization_workflow.py (2,429 lines)                    │ │
│  │    authorize_autofind_ont_and_provision_network_audited()                       │ │
│  │    ├─ Validate autofind candidate freshness                                     │ │
│  │    ├─ Delete existing registration (if force_reauthorize)                       │ │
│  │    ├─ Authorize via protocol adapter                                            │ │
│  │    ├─ Create ONT database record                                                │ │
│  │    └─ Trigger network provisioning (optional)                                   │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
└───────────────────────────────────┼─────────────────────────────────────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
┌───────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
│  OLT PROTOCOL     │   │  PROVISIONING         │   │  ACS/TR-069           │
│  ADAPTERS         │   │  COORDINATOR          │   │  LAYER                │
└───────────────────┘   └───────────────────────┘   └───────────────────────┘
```

## OLT Protocol Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              OLT PROTOCOL LAYER                                      │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │              olt_protocol_adapters.py (1,991 lines)                             │ │
│  │  OltProtocol: SSH | NETCONF | REST | AUTO                                       │ │
│  │  Auto-selects protocol based on OLT capabilities                                │ │
│  │  Falls back to next protocol on failure                                         │ │
│  └─────────┬──────────────────────┬──────────────────────┬────────────────────────┘ │
│            │                      │                      │                          │
│            ▼                      ▼                      ▼                          │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐                   │
│  │    SSH          │   │   NETCONF       │   │    REST         │                   │
│  │  (Primary)      │   │  (GPON YANG)    │   │   (API)         │                   │
│  └────────┬────────┘   └─────────────────┘   └─────────────────┘                   │
│           │                                                                          │
└───────────┼──────────────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              OLT SSH LAYER                                           │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    olt_ssh_pool.py (Connection Pool)                            │ │
│  │  PooledConnection: transport, channel, policy, OLT metadata                     │ │
│  │  SshConnectionPool: Thread-safe, per-OLT max 2, TTL 5min, 100 reuses           │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    olt_ssh.py (1,567 lines)                                     │ │
│  │  Low-level SSH: Paramiko, CLI parsing, TextFSM                                  │ │
│  │  _run_huawei_cmd(), _read_until_prompt(), _open_shell()                         │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    olt_ssh_ont/ (Subpackage)                                    │ │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐    │ │
│  │  │lifecycle.py│ │ status.py  │ │ iphost.py  │ │omci_config │ │  tr069.py  │    │ │
│  │  │authorize   │ │get_status  │ │configure_ip│ │wan/pppoe   │ │bind_profile│    │ │
│  │  │deauthorize │ │find_serial │ │clear_ip    │ │wifi/lan    │ │unbind      │    │ │
│  │  │reboot      │ │            │ │            │ │            │ │            │    │ │
│  │  │factory_rst │ │            │ │            │ │            │ │            │    │ │
│  │  └────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘    │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Provisioning Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           PROVISIONING LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │              provisioning_coordinator.py (1,041 lines)                          │ │
│  │  Phases: olt_registration → service_port → mgmt_ip → tr069_bind →              │ │
│  │          acs_discovery → acs_config_push → verification                         │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │              ont_provisioning/executor.py (875 lines)                           │ │
│  │  Execute delta steps with compensation-based rollback                           │ │
│  │  Single SSH session for all commands                                            │ │
│  │  CompensationEntry: undo commands registered BEFORE execution                   │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## ACS / TR-069 Layer

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           ACS / TR-069 LAYER                                         │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐ │
│  │                    acs_client.py (Protocol Definition)                          │ │
│  │  AcsClient Protocol: Structural interface for ACS backends                      │ │
│  │  create_acs_client(): Factory for server-specific client                        │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    genieacs.py (HTTP Client)                                    │ │
│  │  GenieACSClient: REST API for GenieACS NBI                                      │ │
│  │  ├─ Devices: list, get, delete, count                                           │ │
│  │  ├─ Tasks: create, list, delete, wait_for_completion                            │ │
│  │  ├─ Parameters: get, set, set_and_wait, refresh_object                          │ │
│  │  ├─ Device ops: reboot, factory_reset, download                                 │ │
│  │  ├─ Presets/Provisions: CRUD                                                    │ │
│  │  └─ Faults: list, delete, retry                                                 │ │
│  └────────────────────────────────┬───────────────────────────────────────────────┘ │
│                                   │                                                  │
│  ┌────────────────────────────────▼───────────────────────────────────────────────┐ │
│  │                    tr069.py (2,373 lines)                                       │ │
│  │  Tr069AcsServers: ACS endpoint CRUD                                             │ │
│  │  Tr069CpeDevices: Device registration                                           │ │
│  │  Tr069Jobs: Task queue                                                          │ │
│  │  Tr069Sessions: Communication sessions                                          │ │
│  │  Tr069Parameters: Parameter cache                                               │ │
│  └────────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                      │
│  ┌─────────────────────────────────────────────────────────────────────────────────┐│
│  │  Supporting Services                                                             ││
│  │  ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐    ││
│  │  │olt_tr069_admin │ │tr069_profile_  │ │tr069_parameter_│ │ ont_tr069.py   │    ││
│  │  │resolve ACS     │ │matching.py     │ │adapter.py      │ │param aggregator│    ││
│  │  │apply defaults  │ │match profiles  │ │type inference  │ │fetch from ACS  │    ││
│  │  └────────────────┘ └────────────────┘ └────────────────┘ └────────────────┘    ││
│  └─────────────────────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Adapter Registry

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           ADAPTER REGISTRY                                           │
│                                                                                      │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐                     │
│  │OltActionAdapter  │ │OltDetailAdapter  │ │OltProfileAdapter │                     │
│  │UI operational    │ │Dashboard summary │ │Live profile data │                     │
│  │actions           │ │                  │ │                  │                     │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘                     │
│  ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐                     │
│  │AcsConfigAdapter  │ │AcsServiceIntent  │ │SubscriberOnt    │                     │
│  │Build config      │ │Adapter           │ │Adapter          │                     │
│  │payloads          │ │Intent→ACS tasks  │ │ONT→Customer link│                     │
│  └──────────────────┘ └──────────────────┘ └──────────────────┘                     │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Data Layer (ORM Models)

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           DATA LAYER (ORM Models)                                    │
│                                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐               │
│  │  OLTDevice   │ │  PonPort     │ │  OntUnit     │ │OntAssignment │               │
│  │  id, name    │ │  fsp, olt_id │ │  serial, fsp │ │  ont→pon     │               │
│  │  ssh creds   │ │  capacity    │ │  olt_ont_id  │ │  customer    │               │
│  │  acs_server  │ │              │ │  status      │ │              │               │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘               │
│                                                                                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐               │
│  │Tr069AcsServer│ │Tr069CpeDevice│ │  Tr069Job    │ │Tr069Parameter│               │
│  │  base_url    │ │  device_id   │ │  command     │ │  path, value │               │
│  │  cwmp_url    │ │  serial      │ │  status      │ │  last_update │               │
│  │  credentials │ │  acs_server  │ │  result      │ │              │               │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘               │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

## Key Data Flows

### 1. ONT Authorization Flow

```
User clicks "Authorize"
    → authorization_executor.execute_authorization()
    → olt_authorization_workflow.authorize_autofind_ont_and_provision_network_audited()
        → Validate autofind candidate (freshness check)
        → olt_protocol_adapters.authorize_ont()
            → olt_ssh_ont/lifecycle.authorize_ont() [via SSH]
        → Create OntUnit record in DB
        → (Optional) provisioning_coordinator → executor
        → (Optional) ACS binding
```

### 2. ACS Configuration Push Flow

```
ONT authorized + provisioning enabled
    → provisioning_coordinator (phase: acs_config_push)
    → acs_service_intent_adapter.push_service_intent_to_acs()
    → acs_config_adapter.build_acs_config()  [WiFi, WAN, LAN params]
    → genieacs.set_parameter_values_and_wait()
    → Poll until complete or timeout
```

### 3. OLT → ACS Relationship

```
OLTDevice
    └─ tr069_acs_server_id → Tr069AcsServer
                                └─ base_url (GenieACS API)
                                └─ cwmp_url (device connects here)
                                └─ periodic_inform_interval ← settings.tr069_periodic_inform_interval

OntUnit
    └─ tr069_acs_server_id → Tr069AcsServer (can override OLT's server)
    └─ Tr069CpeDevice (GenieACS device record)
        └─ genieacs_device_id
```

## File Summary

| Layer | Key Files | Lines | Purpose |
|-------|-----------|-------|---------|
| **Execution** | `authorization_executor.py` | ~200 | Entry point |
| **Workflow** | `olt_authorization_workflow.py` | 2,429 | Orchestration |
| **Protocol** | `olt_protocol_adapters.py` | 1,991 | SSH/NETCONF/REST |
| **SSH Core** | `olt_ssh.py` | 1,567 | Low-level CLI |
| **SSH Pool** | `olt_ssh_pool.py` | ~300 | Connection reuse |
| **SSH ONT** | `olt_ssh_ont/*.py` | ~2,000 | ONT operations |
| **Provisioning** | `provisioning_coordinator.py` | 1,041 | Multi-step orchestration |
| **Executor** | `ont_provisioning/executor.py` | 875 | Delta execution + rollback |
| **TR-069** | `tr069.py` | 2,373 | ACS lifecycle |
| **GenieACS** | `genieacs.py` | ~1,200 | HTTP client |
| **OLT CRUD** | `olt.py` | 1,450 | Database operations |

## Service Descriptions

### OLT Services

| Service | File | Purpose |
|---------|------|---------|
| `OLTDevices` | `olt.py` | CRUD for OLT devices, credential management |
| `PonPorts` | `olt.py` | PON port infrastructure, capacity tracking |
| `OntUnits` | `olt.py` | ONT inventory with advanced filtering |
| `OntAssignments` | `olt.py` | ONT-to-customer mapping via PON ports |
| `olt_ssh` | `olt_ssh.py` | Low-level SSH CLI execution |
| `olt_ssh_pool` | `olt_ssh_pool.py` | Connection pooling (TTL 5min, max 100 reuses) |
| `olt_operations` | `olt_operations.py` | High-level ops (backup, firmware, diagnostics) |
| `olt_protocol_adapters` | `olt_protocol_adapters.py` | Multi-protocol abstraction |

### ONT SSH Operations (`olt_ssh_ont/`)

| Module | Purpose |
|--------|---------|
| `lifecycle.py` | authorize, deauthorize, reboot, factory_reset |
| `status.py` | get_ont_status, find_ont_by_serial |
| `iphost.py` | configure/clear IP host settings |
| `omci_config.py` | WAN, WiFi, LAN config via OMCI |
| `tr069.py` | bind/unbind TR-069 server profile |
| `diagnostics.py` | service port diagnostics, remote ping |

### ACS/TR-069 Services

| Service | File | Purpose |
|---------|------|---------|
| `Tr069AcsServers` | `tr069.py` | ACS endpoint CRUD |
| `Tr069CpeDevices` | `tr069.py` | Device registration |
| `Tr069Jobs` | `tr069.py` | Task queue management |
| `GenieACSClient` | `genieacs.py` | HTTP client for GenieACS NBI |
| `AcsConfigAdapter` | `acs_config_adapter.py` | Config payload building |
| `olt_tr069_admin` | `olt_tr069_admin.py` | ACS resolution for OLT flows |

### Provisioning Services

| Service | File | Purpose |
|---------|------|---------|
| `provisioning_coordinator` | `provisioning_coordinator.py` | Multi-phase orchestration |
| `executor` | `ont_provisioning/executor.py` | Delta execution with rollback |
| `context` | `ont_provisioning/context.py` | ONT→OLT context resolution |

## Adapter Pattern

All adapters register with `adapter_registry` and follow this pattern:

```python
class ExampleAdapter:
    name = "example"

example_adapter = ExampleAdapter()
adapter_registry.register(example_adapter)
```

**Registered Adapters:**
- `OltActionAdapter` - UI operational actions
- `OltDetailAdapter` - Dashboard summary data
- `OltProfileAdapter` - Live OLT profile data
- `OltObservedStateAdapter` - Real-time OLT state
- `SubscriberOntAdapter` - ONT-to-customer linking
- `AcsServiceIntentAdapter` - Service intent to ACS tasks
- `AcsConfigAdapter` - Config payload building
- `AcsStateAdapter` - ACS device state tracking

## Critical Architecture Notes

### Transaction Management
- Service methods commit their own transactions via `db.commit()`
- Use `db.flush()` when creating entities that need IDs for related operations
- Routes MUST NOT call `db.commit()`

### SSH Pool Efficiency
- Reuses connections for 5 minutes (configurable TTL)
- Max 100 reuses per connection before recycling
- Eliminates 2-3 second connection overhead per operation
- Thread-safe with automatic cleanup

### Compensation-Based Rollback
- Provisioning executor registers undo commands BEFORE execution
- On failure, compensation actions run in REVERSE order
- Single SSH session for all commands

### Multi-Protocol Support
- Protocol adapter auto-selects SSH, NETCONF, or REST based on OLT capabilities
- Falls back to next available protocol on operation failure
- Unified result type across all protocols

### ACS Polling vs Async
- `set_parameter_values()` returns immediately (async on ACS)
- `set_parameter_values_and_wait()` polls until completion (timeout configurable)
- `wait_for_task_completion()` polls task status with exponential backoff

### Credential Encryption
- ACS passwords encrypted at rest using Fernet (from `credential_crypto`)
- Format: `enc:<encrypted>` for encrypted, `plain:<value>` for plaintext

## Configuration

### TR-069 Periodic Inform Interval

Single source of truth: `settings.tr069_periodic_inform_interval`

Set via environment variable:
```bash
TR069_PERIODIC_INFORM_INTERVAL=300  # seconds, default 5 minutes
```

## File Paths

### Core OLT/ONT/ACS Services
```
app/services/network/olt.py                    # CRUD infrastructure
app/services/network/olt_ssh.py                # Low-level CLI
app/services/network/olt_ssh_pool.py           # Connection pooling
app/services/network/olt_operations.py         # Operational tasks
app/services/network/olt_authorization_workflow.py  # Main auth flow
app/services/network/olt_protocol_adapters.py  # Multi-protocol abstraction
app/services/network/olt_ssh_ont/              # ONT operations via SSH
    lifecycle.py
    status.py
    iphost.py
    omci_config.py
    tr069.py
    diagnostics.py
app/services/network/ont_provisioning/         # Orchestration
    executor.py
    context.py
    profiles.py
    state.py
```

### TR-069/ACS Services
```
app/services/tr069.py                          # Complete lifecycle
app/services/genieacs.py                       # HTTP client
app/services/acs_client.py                     # Protocol definition
app/services/acs_config_adapter.py             # Config adapter
app/services/network/ont_tr069.py              # Parameter aggregation
app/services/network/olt_tr069_admin.py        # OLT TR-069 admin
app/services/network/tr069_profile_matching.py # Profile matching
app/services/network/tr069_parameter_adapter.py # Type inference
app/services/network/tr069_paths.py            # Path resolution
```

### Adapters & Coordination
```
app/services/olt_action_adapter.py
app/services/olt_detail_adapter.py
app/services/olt_profile_adapter.py
app/services/acs_service_intent_adapter.py
app/services/network/authorization_executor.py
app/services/network/provisioning_coordinator.py
```
