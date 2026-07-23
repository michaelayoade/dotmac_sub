# Network support diagnostics (G1–G3)

Design for the first slice of `docs/NETWORK_SUPPORT_GAP_LIST.md`. Scope is **G1**
(serving access endpoint), **G2** (composed service diagnosis) and **G3** (ticket
prefill). G4–G14 are explicitly out of scope and are referenced only where this design
must leave a seam for them.

Status: **design for review**. No implementation has landed.

---

## 1. Problem restated in one line

Support can see *that* a customer is offline, but not *where* they attach or *why* they
are offline, so every diagnosis is a human round trip through the Network Support channel.

---

## 2. Findings that change the gap list

Verified against `origin/main` @ `808b1af75` while writing this design.

| Gap-list claim | Reality | Consequence |
|---|---|---|
| G6/G13: "PPPoE credentials invisible; no reveal-with-audit" | **Already built.** `web_customer_details.reveal_customer_pppoe_password` + route `app/web/admin/customers.py:810`, rate-limited, `record_audit_event` on both grant and denial. | G6 shrinks to the *ownership* half (dual-NAS projection). G13's product fix already exists; the residual work is rotation and telling the team the button is there. |
| G1: "endpoint not modelled" | **Fully resolved, then discarded.** `CustomerPath` already carries `ont`, `pon_port`, `splitter`, `fdh`, `access_device`, `access_device_kind`, `radio`, `node`, `basestation`, `upstream_chain`, `gap`, `live_session`. | G1 is a **projection and presentation** fix, not a modelling one. Much smaller than estimated. |
| G8/G11: optical telemetry | Fields exist (`onu_rx_signal_dbm`, `olt_rx_signal_dbm`, `rx_power_dbm`). `get_ont_diagnostic_snapshot` reaches the OLT over SSH per call. | Diagnosis must not SSH by default — see §5.4. |

The gap list has been amended to match.

---

## 3. Ownership

Per `docs/SOT_RELATIONSHIP_MAP.md`, this slice introduces exactly one new named owner and
it is a **derivation**, not an authority.

| Concern | Owner | Change |
|---|---|---|
| Intended access path (which node/PON/AP serves a subscription) | `network.access_path` | extended projection, unchanged authority |
| Live session / online-now | `network.radius_sessions` | consumed |
| Commercial + financial access state | `access.subscription_lifecycle` | consumed |
| RADIUS credential projection | `access.radius_projection` | consumed |
| ONT/OLT observed state | `network.olt_observed_state` / stored telemetry | consumed |
| **Composed service diagnosis** | **`network.service_diagnosis` (new)** | new derivation |
| Ticket identity, validation, duplicates | `support.tickets` (`ticket_validation`) | unchanged; receives a suggested payload |

**Hard constraints on `network.service_diagnosis`:**

1. It **writes nothing** — no DB mutation, no device mutation, no ticket creation.
2. It **decides nothing that another owner owns.** It never re-derives access state, never
   re-implements path resolution, never re-implements affected-customer expansion. It asks
   the owner and reports the answer with attribution.
3. It **must not become a cache.** Every field it returns carries its source and
   `observed_at`; nothing it produces is persisted as truth.

A violation of (2) is the specific failure mode to guard against: a diagnosis service that
starts inferring "probably suspended" from its own heuristics becomes a second authority
on access state. Architecture tests below enforce this.

---

## 4. G1 — serving access endpoint

### 4.1 What is wrong today

`_build_network_access_cards` (`app/services/web_customer_details.py:1280`) renders:

```python
nas = getattr(sub, "provisioning_nas_device", None)
pop_site = getattr(nas, "pop_site", None) if nas else None
...
"pop_site_name": pop_site.name if pop_site else None,
```

That is the **static provisioning NAS's** site — intended assignment — presented to
support as though it were where the customer is currently served. For a fibre customer it
omits the OLT and PON port entirely, which is why the channel constantly asks "which
cabinet is this customer on?".

### 4.2 Change

Extend `AccessPathSummary` (`app/services/network/access_path.py:32`) with the endpoint
detail the resolver already computes but drops:

```python
@dataclass(frozen=True)
class AccessPathSummary:
    ...existing fields...
    access_device_id: object | None       # OLT / NAS / AP device id
    access_device_name: str | None
    pon_port_label: str | None            # e.g. "0/1/3"
    ont_serial: str | None
    radio_name: str | None                # wireless arm
    endpoint_display: str | None          # "Gudu OLT (0/1/3)" | "D-LUGBE-3"
    endpoint_source: str                  # "live_session" | "provisioning" | "unresolved"
    observed_at: datetime | None
```

`endpoint_display` is composed **in the service**, not the template, so portal, admin and
API render the identical string. `endpoint_source` is mandatory and makes the
live-vs-intended distinction explicit rather than implied — this is what stops the class of
escalation where selfcare and UISP disagree and nobody can tell which is being displayed.

When `CustomerPath.gap` is set, `endpoint_display` is `None` and `endpoint_source` is
`"unresolved"`, with `gap` carried through. **"Cannot resolve" becomes a first-class
diagnostic answer**, not a blank field — the current blank is what sends the agent to chat.

### 4.3 Subscriber topology trace

A single endpoint string answers "where is this customer?" but not "where does it break?".
Since the trace already resolves end to end, render it as a chain.

**Scope: active path only — ONT → PON port → OLT → upstream → NAS/BNG.** Passive plant
(splitter, FDH, drop) is deliberately excluded from the support view.

Rationale for the exclusion: passive assets have no observable live state. A splitter
cannot be "up" or "down" from Sub's perspective, so including it adds width to the diagram
without adding diagnostic value to a "why is this customer offline/slow" question. Passive
plant matters to the fibre/field team and stays in the existing fibre-plant view, which is
the right owner for it. Keeping the two views separate is what stops the support diagram
from becoming an everything-diagram nobody reads.

**Source — assembly, not new resolution.** `resolve_fiber_end_to_end_path` already returns
`FiberEndToEndPath.hops`, each tagged with a `domain`:

| domain | kinds | in support view |
|---|---|---|
| `passive_fiber` | splitter, FDH, drop | **excluded** |
| `physical_core` | core continuity hops | excluded (collapsed into "upstream") |
| `forwarding` | `access_network_device` (OLT), `network_device`, `nas` | **included** |

So the trace is `hops` filtered to `domain == "forwarding"`, prefixed with the ONT and PON
port taken from `CustomerPath.ont` / `CustomerPath.pon_port`.

**Wireless arm** is the same shape with different nodes: CPE radio → AP → node →
base station → NAS, from `CustomerPath.radio` / `.node` / `.basestation`.

**Rendering.** A linear chain, each element carrying:

- label (`ONT UBNT58508c30`, `0/1/3`, `Gudu OLT`, `Abuja BNG`)
- state chip — `up` / `down` / `degraded` / `unknown`, each with `observed_at` and source
- the relevant G2 finding, inline, when a stage produced one

`FiberEndToEndPath.gaps` render **as breaks in the chain** at their `after_asset_id`, with
the gap `code` and message. A trace that stops at the OLT with a gap is a far more useful
support answer than a blank field, and it is exactly the "I can't find where this customer
is connected" case made legible.

The chain is read-only and derives from `deep=False` data. It never triggers a device probe
on render.

### 4.4 Presentation

`_build_network_access_cards` consumes the summary and replaces `pop_site_name` with the
endpoint fields. The template renders `endpoint_display` plus a provenance chip
(`live` / `provisioned` / `unresolved`) and the `observed_at` timestamp. The existing
`nas_name` stays, relabelled as the provisioning NAS, so the intended-vs-serving
distinction is visible rather than collapsed.

No template may re-derive or parse the endpoint. Enforced by an architecture test.

---

## 5. G2 — composed service diagnosis

### 5.1 Shape

New module `app/services/network/service_diagnosis.py`.

```python
@dataclass(frozen=True)
class DiagnosticFinding:
    code: str                     # stable machine code, see §5.3
    severity: str                 # "blocking" | "degraded" | "informational"
    summary: str                  # one line, support-readable
    source: str                   # owning service that supplied the fact
    observed_at: datetime | None
    stale: bool                   # observation older than its freshness budget
    evidence: dict[str, object]   # raw values behind the finding
    suggested_ticket_type: str | None   # from the ticket_validation taxonomy

@dataclass(frozen=True)
class ServiceDiagnosis:
    subscription_id: object
    subscriber_id: object
    evaluated_at: datetime
    verdict: DiagnosticFinding | None      # first blocking finding, None = no fault found
    findings: tuple[DiagnosticFinding, ...]  # every finding, in evaluation order
    endpoint: AccessPathSummary
    deep: bool                             # whether live device probes ran
```

### 5.2 Evaluation ladder

Evaluated **in order**. The first `blocking` finding becomes `verdict`, but **evaluation
does not stop** — every stage still runs and contributes to `findings`. This is deliberate:
the channel is full of cases where the link is optimal *and* the service is disabled
("The link is optimal but the service was disabled"), and support needs both facts in one
reply.

| # | Stage | Asks | Blocking codes |
|---|---|---|---|
| 1 | Commercial state | `access.subscription_lifecycle` | `no_active_subscription`, `subscription_expired`, `service_suspended_financial`, `service_restricted` |
| 2 | Credential readiness | `access.radius_projection`, `AccessCredential` | `pppoe_credential_missing`, `pppoe_secret_missing` |
| 3 | Authentication | RADIUS auth observations | `authentication_rejected` |
| 4 | Session | `network.radius_sessions` | `no_active_session`; degraded: `session_stale` |
| 5 | Access path | `network.access_path` (G1) | `access_path_unresolved` |
| 6 | Upstream health | serving node/AP/OLT state; seam for G4 | `upstream_node_down`, `known_infrastructure_incident` |
| 7 | Last mile | ONT/CPE state + optical | `ont_offline`; degraded: `optical_power_out_of_range`; informational: `optical_telemetry_unavailable` |
| 8 | Capacity | throughput vs plan | degraded: `plan_bandwidth_exhausted` |

`verdict is None` → `no_fault_found`, returned **with the full evidence list**. That is the
honest answer to "the link is fine" and it replaces today's unattributed "ask the client to
confirm, they are pulling in mbps".

### 5.3 Codes → ticket types

Mapping is a module-level constant validated at import against
`ticket_validation.subscriber_required_ticket_types()` and
`base_station_required_ticket_types()`, so a typo or a taxonomy rename fails a test rather
than silently producing an invalid ticket.

| Code | Ticket type | Link |
|---|---|---|
| `service_suspended_financial`, `subscription_expired` | *(none — not a fault)* | — |
| `pppoe_credential_missing`, `pppoe_secret_missing` | `router troubleshooting` | subscriber |
| `authentication_rejected` | `router troubleshooting` | subscriber |
| `access_path_unresolved` | `subscriber-system issue` | base station |
| `upstream_node_down` | `access point outage` / `bts outage` / `multiple cabinet disconnection` by node kind | base station |
| `ont_offline` | `customer link disconnection` | subscriber |
| `optical_power_out_of_range` | `power optimization (if specific to customer premises)` | subscriber |
| `plan_bandwidth_exhausted` | `bandwidth complaint` | subscriber |
| poor radio signal (stage 7, wireless arm) | `customer realignment` | subscriber |

### 5.4 Freshness and device probes

Two modes:

- **Default (`deep=False`)** — stored telemetry and DB state only. Cheap, safe to run on
  page load. Every finding carries `observed_at` and a per-stage freshness budget; over
  budget sets `stale=True` and the UI must show it as stale rather than current.
- **Deep (`deep=True`)** — explicit operator action. May call
  `olt_diagnostics.get_ont_diagnostic_snapshot`, which opens an **SSH session to the OLT**.
  Never on page load, never in bulk, rate-limited per OLT.

This distinction is the single most important operational property of the design: a naive
implementation that SSHes to an OLT on every customer page view will melt the OLTs the
moment the support team adopts it.

`optical_telemetry_unavailable` (G11) is an explicit informational finding, so a coverage
hole reads as "we cannot see this" instead of "this is fine".

### 5.5 Surfaces

- **Admin customer detail** — verdict banner plus a findings list, each with source and
  timestamp. Deep-probe is a button.
- **API** — `GET /api/v1/subscriptions/{id}/diagnosis?deep=false`, returning the DTO. This
  is what CRM agents and, later, automated first-line replies consume.
- **Not selfcare** in this slice. Customer-facing phrasing of a diagnosis is a separate
  decision with support-policy implications.

---

## 6. G3 — ticket prefill

Ticket creation authority is unchanged: `support.tickets` + `ticket_validation`. Diagnosis
only *proposes*.

Flow: verdict → "Create ticket" → the existing create form opens pre-filled with ticket
type, subscriber or base-station link (per the taxonomy's requirement rules), and a
description containing the verdict summary, the evidence, and `evaluated_at`. A human
confirms. No auto-creation in this slice.

`build_pre_create_context` and `find_duplicate_ticket_candidates` run unchanged, so the
existing duplicate scoring catches the repeat tickets the channel shows today. The
prefilled description makes duplicate detection *better*, because descriptions stop being
free-form prose.

The pre-fill payload is built by a thin adapter in the web layer, not by
`service_diagnosis`, keeping the diagnosis service free of ticket concerns.

---

## 7. Testing

**Architecture (boundary) tests — the ones that matter:**

1. `service_diagnosis` performs no writes: no `db.add`/`commit`/`flush`, no calls into
   mutation services. Enforced by AST inspection, consistent with existing architecture
   tests under `tests/architecture/`.
2. `service_diagnosis` does not import provisioning, ticket, or notification modules.
3. Every `suggested_ticket_type` is a member of the live taxonomy.
4. Templates do not compose `endpoint_display` themselves.
5. `deep=False` never reaches `olt_ssh_diagnostics`.

**Behaviour tests** — one per ladder stage, each asserting verdict code, that lower stages
still contributed findings, and that `stale` is set when the observation is aged past
budget. Plus: suspended-but-physically-healthy (verdict is financial, optical finding still
present), unresolved path, no-fault-found, and telemetry-unavailable.

---

## 8. Commit slicing

One commit per slice, integrated into a single PR:

1. `feat(network): carry serving endpoint detail through access path summary` — G1 service layer + tests
2. `feat(admin): show serving access endpoint on customer detail` — G1 presentation + template + architecture test
3. `feat(network): add composed service diagnosis` — G2 service + ladder + tests
4. `feat(admin): surface service diagnosis on customer detail` — G2 UI + API endpoint
5. `feat(support): prefill tickets from a service diagnosis` — G3 adapter + tests
6. `docs: network support gap list and diagnostics design` — this doc + the gap list

---

## 8b. Portal self-service assurance (G15 — separate slice)

Not in this PR. Designed here because it shares the diagnosis service and changes what G14
should become.

**Premise:** "slow browsing" is 16 messages in a 12-day sample, and almost every one is a
NOC round trip that ends in "the link is fine, they are pulling in mbps". If the customer
can see, themselves, that they are online, what they are pulling, and what the link
measures, most of those never reach a human.

### The measurement trap

A browser-based speed test measures **the customer's device over their WiFi**, not the
access link. Shipping only that will make good links look bad, because consumer WiFi is
the bottleneck far more often than the PON is — and it hands the customer a number to argue
with. Done naively, this *increases* disputes.

The fix is to measure in **two places and label them distinctly**:

| Test | Measures | Mechanism | Authority |
|---|---|---|---|
| **Link speed** | ONT/router ↔ network | TR-143 `DownloadDiagnostics` / `UploadDiagnostics` via ACS | authoritative for "is Dotmac delivering?" |
| **This device** | customer device ↔ server, over WiFi | browser | indicative only |

**The delta between them is the diagnosis.** Link test at plan rate + device test poor =
customer-side WiFi, resolved without an engineer and without a ticket. That single
comparison is the highest-value part of this feature, and neither test alone provides it.

### What exists

- `app/services/customer_portal_bandwidth.py` — live bandwidth SSE per subscription.
- `app/services/usage.py`, `usage_summary.py`, `app/models/usage.py` — usage aggregation.
- `app/services/network/tr069_paths.py` — ping and traceroute already mapped for both
  TR-181 and TR-098 device trees, via `tr069_parameter_adapter`.

### What is missing

- **TR-143 paths.** `tr069_paths.py` has `diag.ping.*` and `diag.traceroute.*` but no
  download/upload diagnostics. Adding `diag.download.*` / `diag.upload.*` follows the
  existing two-tree mapping pattern exactly.
- A portal surface combining online state, current/recent throughput, plan rate, and the
  two tests.
- Result persistence, so support sees the customer's own test history instead of asking
  them to run it again while on the phone.

### Guardrails

1. TR-143 is a real transfer over the customer's link — rate-limit per subscription and
   refuse to run it against a PON already flagged saturated. A speed test that degrades the
   neighbours is worse than no speed test.
2. Not all CPE implement TR-143. Absence must render as "link test unavailable for your
   equipment", never as a failure or a zero.
3. Show the plan rate next to the result, so "you are getting what you pay for" is legible
   without arithmetic — and so `plan_bandwidth_exhausted` (G2 stage 8) explains itself.
4. Results are observations. They are stored as evidence with `observed_at`, never fed back
   as authority into plan or access state.

### Consequence for G14

G14 stops being "surface utilization in admin" and becomes "self-service assurance in
portal, with admin reading the same data". The customer-facing half is the part that
actually reduces channel traffic.

## 9. Non-goals

- No auto-created tickets, no auto-notification to customers.
- No mass-incident correlation (G4). Stage 6 leaves the seam: it consults node state today
  and will consult an incident record when G4 lands.
- No change to `access.radius_projection` ownership or the dual-NAS retirement (G6) — that
  needs its own cutover plan.
- No selfcare-facing diagnosis text.
- No IPAM, SLA, or shift-report work.

---

## 10. Open questions for review

1. **Stage 1 vs stage 7 precedence.** A financially suspended customer with a genuinely
   dead ONT: current design's verdict is `service_suspended_financial` with the ONT finding
   listed below. Correct for billing, but a field team dispatched after payment will find a
   dead link. Should a blocking physical finding be promoted into the verdict alongside?
2. **Freshness budgets.** Proposed: session 5 min, optical 60 min, node state 5 min. These
   are guesses and should be set against real polling intervals.
3. **API consumer.** Is the diagnosis endpoint for CRM agents now, or does the CRM
   decommission path make Sub admin the only surface worth building?
