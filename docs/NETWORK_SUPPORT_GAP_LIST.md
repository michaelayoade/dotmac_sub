# Network Support gap list

Derived from the Nextcloud Talk **Network Support** channel (room `jefpvsco`), window
**2026-07-11 → 2026-07-23**: 710 messages, 571 human, ~48 human messages/day, 26 active
participants. Referenced ticket IDs span **#21761 → #22339** (~578 tickets in 12 days).

The channel operates as a manual substitute for diagnostics Sub does not expose. The
repeated loop is: an agent posts a customer name → NOC manually checks UISP / OLT / RADIUS
→ replies in prose → a third person is asked to create a ticket. Every gap below is one
step of that loop that Sub could own.

Each gap records the observed evidence, what already exists in the codebase, the owning
service under the source-of-truth standard, and the fix. **"Exists"** claims below were
verified against the working tree on 2026-07-23.

---

## Priority summary

| ID | Gap | Impact | Effort | Depends on |
|---|---|---|---|---|
| **G1** | Serving base-station / OLT-PON endpoint not shown | High | S | — |
| **G2** | No composed "why can't this customer browse?" verdict | Highest | L | G1 |
| **G3** | Ticket creation is manual and human-typed | High | M | G2 |
| **G4** | No infrastructure/mass-incident ticket with affected-customer linking | High | L | G1 |
| **G5** | Access-denial reason (billing) not surfaced | High | S | — |
| **G6** | PPPoE credentials invisible; dual-NAS projection unresolved | High | M | — |
| **G7** | Observed vs derived connectivity conflated ("selfcare says down, UISP says up") | Medium | M | G1 |
| **G8** | CPE/ONT live state not surfaced → agents ask customers for photos | Medium | S | — |
| **G9** | IP allocation happens in chat; no conflict detection | Medium | M | — |
| **G10** | Ticket aging has no system escalation | Medium | S | — |
| **G11** | ONT optical telemetry coverage holes | Medium | M | — |
| **G12** | Shift handover and close-out reports are manual | Low | S | G3 |
| **G13** | Credentials pasted into chat (security) | High | S* | G6 |
| **G14** | Bandwidth/utilization not surfaced for "slow browsing" | Low | S | G2 |

Suggested order: **G1 → G2 → G3 → G4 → G5/G6 → G7/G8 → G9/G10/G11 → G12/G14**.

`S*` on G13: the product fix already exists (see G13/G6). What remains is rotation and
discoverability, not engineering.

**Amended 2026-07-23** after verifying against `origin/main` @ `808b1af75`: G1 dropped
M→S (the endpoint is already resolved, just not projected), and G6/G13 shrank
substantially (PPPoE reveal-with-audit is built). Design for G1–G3 is in
`docs/designs/NETWORK_SUPPORT_DIAGNOSTICS.md`.

---

## G1 — Serving base-station / OLT-PON endpoint not shown

**Evidence (13 messages):** "which AP is he on?", "help me check which cabinet this
customer is on", "I am unable to find their device details in UISP", "I can't find where
the customer is connected", "I'm unable to locate this customer's exact base station —
customer has been down for 8 days", "there is no ont assignment for this customer".

**Already exists:** `app/services/network/access_path.py`
(`resolve_subscription_access_path`, `summarize_subscription_access_path`,
`resolve_fiber_end_to_end_path`), `app/services/customer_network_context.py`.
**`CustomerPath` already resolves the full endpoint** — `ont`, `pon_port`, `splitter`,
`fdh`, `access_device`, `access_device_kind`, `radio`, `node`, `basestation`,
`upstream_chain`, `gap`, `live_session`.

**Gap (verified 2026-07-23):** this is a **projection and presentation** gap, not a
modelling one. `AccessPathSummary` drops `pon_port` and the access-device identity, and
`_build_network_access_cards` (`app/services/web_customer_details.py:1297-1298`) renders
`sub.provisioning_nas_device.pop_site` — the *static provisioning* NAS site — as though it
were the serving location. For a fibre customer the OLT and PON port are never shown.
Requirement was specced 2026-07-17 (display form: `Jabi OLT-1 (Port 15)`,
`Gudu OLT (Port 1)`) and remains unbuilt.

**Owner:** `network.access_path` owns intended access path; `network.radius_sessions`
owns online-now; UISP AP association is an observation, never authority.

**Fix:** render a presentation DTO on the customer detail Location card carrying the
serving base station / OLT + PON port, with provenance. The web snapshot and template
must consume the DTO and must not infer or parse the endpoint independently.

**Why first:** it is the input to G2, G4 and G7.

---

## G2 — No composed "why can't this customer browse?" verdict

**Evidence (44 messages — the single largest category):** "connected but cannot browse",
"online but can't browse", "connected and online, but they are unable to browse", "his
router is showing green and he's not connected".

**Already exists — every input, composed nowhere:**
`app/services/customer_network_context.py`, `app/services/network/pppoe_health.py`,
`app/services/network/radius_sessions.py`, `app/services/network/olt_diagnostics.py`,
`app/services/network/access_path.py`, `app/services/radius_access_state.py`.
`pppoe_health` is currently surfaced only in `app/web/admin/network_onts_inventory.py`,
not on the customer page.

**Gap:** no single action runs the chain and returns a ranked cause.

**Fix:** one diagnostic action on the customer page that evaluates, in order, and returns
the first blocking cause with evidence and timestamp:

1. subscription / access state (active, suspended, expired, restricted)
2. RADIUS authentication + live session (`radius_sessions`)
3. PPPoE health (`pppoe_health`) — credential present, NAS reachable
4. ONT / CPE reachability and optical rx power
5. serving AP / OLT-PON status (from G1)
6. correlation against any open infrastructure incident (from G4)

Output is a verdict plus the recommended ticket type, pre-filled.

**Impact:** collapses G2, G5, G6, G8 and G14 into one screen and removes the bulk of the
channel traffic.

---

## G3 — Ticket creation is manual and human-typed

**Evidence (57 messages mention tickets):** "please create the ticket", "create a
realignment ticket so the radio engineers can investigate further", "create a cabinet
disconnection ticket (GPON-GW-1-PON-5)", "Ticket has been created Ticket #22154".

**Already exists:** `app/services/ticket_validation.py` defines the taxonomy and it
matches the channel's vocabulary exactly —
`_SUBSCRIBER_REQUIRED_TICKET_TYPES` (customer realignment, power optimization, slow
browsing / intermittent connectivity, customer link disconnection, router
troubleshooting, bandwidth complaint, LAN troubleshooting, router replacement) and
`_BASE_STATION_REQUIRED_TICKET_TYPES` (bts outage, access point outage, multiple cabinet
disconnection, multiple customer link disconnection, core link disconnection). Duplicate
detection thresholds already exist.

**Gap:** the vocabulary is right; the *selection and description* are human work, so
type accuracy depends on who is on shift.

**Fix:** from the G2 verdict, a one-click create with ticket type, subscriber or
base-station link, and diagnosis text pre-filled. Reuse the existing duplicate-candidate
scoring to prevent the repeat tickets visible in the channel.

---

## G4 — No infrastructure/mass-incident ticket with affected-customer linking

**Evidence (10+ messages):** "DLUGBE-3 is experiencing an outage and there is a ticket for
it #22284", "let me add it to the AP gudu 4 outage ticket", "create a multiple cabinet
disconnection ticket for the Gwarimpa huawei OLT (2/3 - 2/12 - 2/6 - 2/1 - 2/11 - 2/10)",
"Ilupeju BTS is down due to power", "the same issue affecting DJabi-6 AP is affecting
DJabi-5".

Two failure modes that argue for system-generated tickets:
- **Wrong-node tickets** — "gudu-4 is not down, why do we have a ticket for it".
- **Deliberate ticket-splitting to avoid premature closure** — "it will require a
  different ticket so they don't close it when eagle 4 comes up".

**Already exists:** `app/services/topology/affected.py` — `subscriptions_for_node`,
`subscriptions_for_fdh`, `downstream_nodes`, `forwarding_graph_projection`, surfaced via
`app/services/crm_api.py`. **No** outage/incident service exists (`ls app/services | grep
outage|incident|mass` returns nothing).

**Fix:** when an AP / OLT-PON / BTS is observed down, open **one** infrastructure ticket
keyed to the real node, attach `affected_customers()`, auto-link incoming individual
complaints for those subscribers, and expose "known outage" to agents, selfcare and
automated replies. Individual complaint tickets close when the parent closes.

---

## G5 — Access-denial reason (billing) not surfaced

**Evidence (23 messages):** "the client is currently suspended as a result of outstanding
bill", "customer /30 was disabled due to billing issue", "their subscription has expired",
"payment was added for Zartech Ltd but no connection", "this customer made payment on the
16th of July and the account was not suspended, yet he/she cannot browse".

**Already exists:** `access.subscription_lifecycle` and `access.radius_projection`
converge through the mandatory enforcement loop merged in PR #1545 (2026-07-23);
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

**Gap:** enforcement is now correct, but the *reason* is not visible. Support diagnoses a
network fault for what is a billing state, and cannot see reconciler latency after a
payment.

**Fix:** show the denial reason and its source at the top of the admin customer page and
in selfcare ("Service suspended — outstanding balance since <date>"), plus last and next
enforcement reconciler run so "customer paid, still down" is answerable without an
escalation.

---

## G6 — PPPoE credentials invisible; dual-NAS projection unresolved

**Evidence (18 messages):** "the pppoe is not visible on the account dashboard i can only
find the username no password", "i cant see the ppoe details for the client", "5 customers
have an active subscription but pppoe is not authenticating", "the client was migrated and
has not been configured", "the PPPOE details was not configured on the router it has now
been configured", "the subscription lifecycle state of subscription is not responding and
has been escalated to system admin".

**Known architectural risk this confirms:** subscription activation projects PPPoE through
FreeRADIUS *and* creates a local MikroTik `/ppp secret` without a password
(`app/services/connection_type_provisioning.py:_mikrotik_commands`), creating a parallel
authentication path that can drift from `app/services/radius.py`.

**Correction (verified 2026-07-23):** the reveal half is **already built**.
`web_customer_details.reveal_customer_pppoe_password` plus route
`app/web/admin/customers.py:810` — staff-gated, rate-limited, and `record_audit_event`
fires on both grant and denial. So the chat symptom *"i can only find the username no
password"* is almost certainly `_build_pppoe_access_snapshot` returning
`has_password: False` — an `AccessCredential` row created **without a secret**, which is
the dual-NAS activation bug itself, not a missing UI.

**Remaining fix:** resolve ownership — FreeRADIUS (`access.radius_projection`) is
authoritative; retire the local PPP-secret write or demote it to a reconciled projection
with the password included. Document the migration explicitly (old owner, new owner,
shadow phase, cutover gate, fallback retirement, boundary tests). Separately, confirm the
support team knows the reveal button exists.

---

## G7 — Observed vs derived connectivity conflated

**Evidence (8 messages, each escalated to system admin):** "Hyperia is not down, they are
up from uisp and can be pinged but selfcare is showing them as not connected", "Kenna
Partners is up on Uisp but down on selfcare", "this customer is showing Jabi access on
selfcare but is on gudu olt port 1", "system admin confirm run state was also offline from
the olt".

**Gap:** the UI presents one derived boolean ("connected"), so any disagreement between
RADIUS, UISP and the OLT becomes an escalation instead of a self-explaining display.

**Fix:** show observed facts individually with **source and timestamp** — RADIUS session,
UISP AP association, OLT ONT run-state, ICMP reachability — alongside the derived state,
so drift is visible and attributable. Keeps observations and decisions separated per the
source-of-truth standard.

---

## G8 — CPE/ONT live state not surfaced

**Evidence (24 messages):** "kindly confirm if the client device is powered on", "tell the
customer to send a picture showing the light indicators on their router", "confirm the pon
light status", "kindly provide an image of the device".

**Already exists:** `app/services/network/cpe.py`, `cpe_action_diagnostics.py`,
`acs_reachability.py`; optical fields on the ONT models
(`app/models/network.py`: `rx_power_dbm`, `onu_rx_signal_dbm`, `olt_rx_signal_dbm`,
`onu_tx_signal_dbm`).

**Gap:** none of it appears on the customer page, so the agent's only instrument is the
customer's eyes.

**Fix:** surface last-seen, ONT rx power with recent trend, LAN link state and last RADIUS
stop reason. One channel message already demonstrates the target output — an ONT power
report with current value, status and a multi-day trend — produced manually.

---

## G9 — IP allocation happens in chat; no conflict detection

**Evidence (15 messages):** "I need Fall back ip for a new installation", "Fallback IP:
172.20.0.140", "Please use this instead: 172.21.0.76", "these two customers are having an
IP conflict", "E-Barcs MFB 6 IP is conflicting with [another customer]'s", "megamore Apo
has 4 unit of /29 ip on the service kindly verify the one he is using and remove the rest,
it is affecting his service billing".

**Already exists:** IP assignment records (`list_active_ip_assignments`,
`list_assigned_delegated_prefixes` in `customer_network_context.py`).
**No conflict detection found** anywhere in `app/services/network/`.

**Fix:** make Sub the allocator — request/assign fallback and static IPs through the app
with conflict detection at assignment time, and reconcile assigned-vs-billed prefix units
so over-assignment stops silently affecting billing.

---

## G10 — Ticket aging has no system escalation

**Evidence:** "NOC was tagged on the ticket since 11th but no update", "#22108 has been
pending for quite some time", "#22325 has no update yet", "this customer has been down for
weeks now", "customer has been down for 8 days", "please has this been attended to?"

**Already exists:** `app/services/sla_assignment.py`,
`app/services/ticket_sla_reports.py`, `app/services/operational_escalation.py`,
`app/services/operational_escalation_delivery.py`,
`app/services/web_notifications_sla_policies.py`.

**Gap:** the channel is currently the SLA mechanism — a human notices and nags.

**Fix:** wire ticket aging and unacknowledged-assignment into the existing operational
escalation path so breach notification is automatic and attributable.

---

## G11 — ONT optical telemetry coverage holes

**Evidence:** "the cabinet or the customer's link may be experiencing a high power issue
but there is no way to check, as there are no optical signal information recorded for it".

**Gap:** the schema supports optical power (see G8); collection does not cover every link.
This blocks the power-optimisation diagnosis path in G2 for affected customers.

**Fix:** audit ONT optical polling coverage, report links with no recent sample, and treat
"no telemetry" as an explicit diagnostic outcome rather than silence.

---

## G12 — Shift handover and close-out reports are manual

**Evidence:** a hand-typed "Close-Out Report" most evenings; "Network support team, who is
on shift today?, nobody has resumed yet"; "I'm still waiting so we can hand-over to night
shift to resolve and look into pending task"; "I will be waiting on your close-out report
and it should contain task done and pending task".

**Fix:** generate the close-out from ticket and diagnostic activity for the shift window
(resolved, pending, escalated, infrastructure incidents open), and hold the roster in Sub
so "who is on shift" is answerable.

---

## G13 — Credentials pasted into chat (security)

**Evidence:** a live PPPoE username/password pair, customer WiFi SSID/password pairs
including a password-change request, and customer phone numbers — all in cleartext in a
264-user Nextcloud instance with permanent history.

**Root cause (revised):** the reveal-with-audit path already exists (see G6), so this is
**not** an unbuilt feature. Either the team does not know the button is there, or the
credential genuinely has no stored secret and the only copy lives on a router — which is
G6's ownership problem.

**Fix:** confirm which of the two it is. If it is the former, that is a training and
discoverability fix, not code. Separately: rotate any credential known to have been
posted. Do not retroactively scrub Talk history without an explicit decision — deleting
operational history has its own cost.

---

## G14 — Bandwidth/utilization not surfaced for "slow browsing"

**Evidence (16 messages):** "slow browsing", "the customer is maxing out", "browsing below
bandwidth", "connected, but pulling in kilobytes", "the link is optimal, they have
utilized 22GB".

**Gap:** "maxing out" is a manual lookup, so plan-exhaustion and genuine degradation look
identical to the agent.

**Fix:** surface current throughput vs plan, and recent utilisation, as part of the G2
verdict so plan-limit cases resolve without a NOC round trip.

---

## Cross-cutting notes

- **Nothing here requires a new authority.** Every gap is either a projection Sub already
  owns but does not display (G1, G5, G7, G8, G14), a composition of services that already
  exist (G2, G3, G4, G10), or an ownership boundary already flagged as unresolved (G6, G9).
- **`nextcloud service down` is itself a ticket type** in `ticket_validation.py`, which is
  consistent with Talk being load-bearing operational infrastructure.
- The 12-day window is a sample, not the full history. Longer-window figures may shift the
  relative ranking, but the top three categories are unlikely to change.
