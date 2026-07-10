# Operations escalation plan

Sub is the operational source of truth. External collaboration and delivery tools
are transports, not decision systems.

## Operating model

Sub owns:

- customer, subscriber, ticket, work-order, outage, device, and service context
- owners, watchers, duty responsibility, and escalation state
- SLA and customer-update timers
- internal notes, customer-visible replies, and official timelines
- escalation decisions, acknowledgements, audit trail, and delivery history

Nextcloud Talk handles:

- real-time team discussion
- incident rooms
- NOC, field, support, and management coordination
- quick collaboration around an issue

WhatsApp handles:

- urgent escalation only
- action-oriented alerts to responsible people after policy thresholds are met
- acknowledgement links/buttons that stop repeat escalation for that watcher

WhatsApp must not be driven directly by raw outage/device events. The flow is:

`event detected -> Sub timeline event -> policy evaluation -> escalation level -> channel delivery`

## Core primitives

Use generic operational primitives instead of outage-only tables:

- `operational_owners`: accountable owner for an entity.
- `operational_watchers`: teams, people, roles, or duty slots that care.
- `operational_room_links`: external collaboration rooms such as Nextcloud Talk.
- `operational_escalation_policies`: thresholds, levels, channels, and cooldowns.
- `operational_escalation_events`: policy decisions and trigger history.
- `operational_escalation_deliveries`: per-channel delivery, dedupe, cooldown, and acknowledgement.
- `operational_customer_update_state`: last update, next due time, and customer-safe status.
- `duty_rosters`: who is responsible now for NOC, field, support, billing, and management escalation.

All primitives should attach to a generic entity reference:

- `entity_type`
- `entity_id`
- optional `scope_type`
- optional `scope_id`

First consumers are outages and inbox conversations, but the model must also support
tickets, work orders, projects, customers, POPs, OLTs, NAS devices, payment incidents,
and provisioning failures.

## Escalation levels

- Level 1: owner/team web notification, Sub inbox, and linked Nextcloud room.
- Level 2: WhatsApp owner or team lead after the first escalation window.
- Level 3: manager escalation after the second window.
- Level 4: executive/account-manager escalation for major or VIP/business impact.

Escalation policies must support:

- severity thresholds
- affected-customer thresholds
- VIP/business impact
- unowned incident age
- stale owner update age
- customer update timer nearing breach
- unresolved duration
- channel list per level
- per-watcher cooldown
- max one delivery per watcher per incident per level unless explicitly re-escalated

## Watcher targeting

Default watcher sources:

- incident owner
- duty NOC lead
- site, POP, OLT, NAS, or BTS owner
- field team lead
- support escalation lead
- account manager only for VIP or business-impacting incidents

Watchers should be team-based first and person-based second. Duty rosters resolve the
current responsible person at delivery time.

## Customer-safe communication

Outage state must feed customer messaging rules:

- do not send expiry, suspension, or blame-shifting messages to customers affected by active infrastructure downtime
- do link inbound customer complaints to active outages where confidence is high
- do track last customer update and next customer update due
- do notify support when affected customers are owed an update

## Fatigue controls

- no WhatsApp for minor flaps
- dedupe child symptoms into parent incidents
- cooldown by watcher, incident, and escalation level
- resolved incidents cancel pending escalation
- acknowledgements stop repeat escalation for that watcher
- maintenance windows suppress emergency escalation while still notifying planned-work watchers
- channel preferences and quiet hours apply except for critical levels

## Implementation order

1. Generic owners, watchers, room links, and escalation state primitives.
2. Outage owners/watchers as the first consumer.
3. Escalation policy evaluation plus acknowledgement endpoints.
4. WhatsApp escalation delivery behind policy/cooldown state.
5. Customer impact mapping and VIP/business-impact signals.
6. Customer update timers and stale-update alerts.
7. Incident grouping and deduplication.
8. Maintenance windows.
9. Runbooks and customer-safe status view.

The first code slice should ship the generic primitives and service helpers only.
It should not send WhatsApp yet.
