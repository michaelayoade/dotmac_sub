# Application Automations

This document describes automation found in the application source code.

It does **not** confirm which optional automations are enabled in a live
environment. Many schedules and integrations are controlled by database
settings, capability switches, credentials, or active rules.

## How to read this document

- **When**: the event or schedule that starts the automation.
- **If**: the conditions that must be true.
- **Then**: what the application does automatically.
- **Status**: whether the code schedules it by default, makes it configurable,
  starts it only after a user action, or has retired it.

The Automation Center also has an owner-declared catalogue for Support, Team
Inbox, Messaging, service-level, billing, subscription, usage/access,
provisioning, customer-identity, network-monitoring, sales, field operations,
integrations, reporting, exports, and maintenance entries. Its page status
describes whether an item can be used for a new Center rule, remains on its
current page, lacks a safe Center contract, or has been retired. It does not
change whether existing automation is enabled in a live environment. These
catalogue batches inventory current processes; they do not move existing rules
or scheduled jobs into the Center.

## 1. Billing, invoices, and collections

### Recurring invoice cycle

- **When:** The billing scheduler reaches its configured interval.
- **If:** A subscription is eligible for billing and has reached its billing
  date.
- **Then:** The application calculates the recurring plan, add-ons, taxes,
  discounts, and any proration, then creates the invoice.
- **Status:** Scheduled by default.

### Invoice reminders

- **When:** The billing-notification schedule runs during the configured sending
  window.
- **If:** An invoice is approaching or has reached a configured reminder date,
  and notifications are allowed for the account.
- **Then:** The application creates the appropriate invoice reminder message.
- **Status:** Scheduled by default.

### Mark invoices overdue

- **When:** The overdue checker runs.
- **If:** An unpaid invoice is past its due date.
- **Then:** The invoice is marked overdue and the overdue event is passed to the
  enforcement and notification processes.
- **Status:** Scheduled by default.

### Automatic payment collection

- **When:** The autopay schedule runs.
- **If:** A customer has an active payment mandate and an eligible open invoice.
- **Then:** The application asks the payment provider to charge the invoice and
  records the result.
- **Status:** Scheduled by default.

### Payment webhook settlement

- **When:** Paystack or Flutterwave sends a verified payment webhook.
- **If:** The signature is valid, the event is not a duplicate, and the payment
  can be matched safely.
- **Then:** The application records the payment and starts the related renewal,
  allocation, receipt, notification, and access-restoration consequences.
- **Status:** Event-driven.

### Stranded top-up reconciliation

- **When:** The top-up reconciliation schedule runs.
- **If:** A top-up was started but no final trusted result was recorded.
- **Then:** The application verifies it with the payment gateway and applies the
  confirmed result.
- **Status:** Scheduled by default.

### Prepaid service renewal after funding

- **When:** A confirmed payment or account-credit deposit is recorded.
- **If:** The account has an eligible prepaid service and sufficient applicable
  funding.
- **Then:** The application renews service coverage and advances the billing
  anchor.
- **Status:** Event-driven.

### Funding reversal correction

- **When:** A payment is refunded, reversed, or charged back.
- **If:** That payment previously funded prepaid service coverage.
- **Then:** The application removes the affected billing anchors and re-derives
  the account's valid coverage.
- **Status:** Event-driven.

### Prepaid balance enforcement

- **When:** The prepaid balance sweep runs.
- **If:** A prepaid account has insufficient balance or expired funded coverage.
- **Then:** The application arms the relevant timers, warns the customer, or
  suspends service according to policy.
- **Status:** Scheduled by default.

### Billing enforcement (dunning)

- **When:** The enforcement schedule runs or an invoice becomes overdue.
- **If:** The account meets the configured overdue rules and has no valid reason
  to remain open.
- **Then:** The application creates the enforcement decision and applies the
  required service restriction.
- **Status:** Scheduled and event-driven.

### Restore service after payment

- **When:** A qualifying payment or credit is confirmed.
- **If:** The account's debt condition has cleared and no other access block
  remains.
- **Then:** The application resolves the overdue lock, restores the subscription,
  rebuilds network access, and can notify the customer.
- **Status:** Event-driven, with scheduled reconciliation as a safety net.

### Payment arrangements

- **When:** A payment is received or the arrangement-overdue checker runs.
- **If:** The payment belongs to an active arrangement, or an instalment has
  passed its due date.
- **Then:** The application applies the payment, advances the arrangement, or
  defaults it after the permitted missed payments.
- **Status:** Event-driven and scheduled.

### Bundle consistency repair

- **When:** The bundle reconciliation schedule runs.
- **If:** A base subscription and one or more bundle members have conflicting
  active/suspended states.
- **Then:** The application brings the bundle members back to the anchor
  subscription's state.
- **Status:** Scheduled by default.

### Billing approval repair

- **When:** The billing-approval reconciliation schedule runs.
- **If:** An active account has an invalid or missing billing approval state.
- **Then:** The application repairs the state through the account lifecycle
  rules.
- **Status:** Scheduled by default.

### Billing health snapshot

- **When:** The billing-health schedule runs.
- **If:** The previous single-flight run is not still active.
- **Then:** The application records a current summary of billing, notification,
  enforcement, and worker health for monitoring.
- **Status:** Scheduled by default.

### Billing safety audits

- **When:** The relevant audit schedule runs.
- **If:** The optional audit is enabled.
- **Then:** The application reports cutover-balance drift, stale overdue locks,
  or inactive accounts carrying positive funding without changing customer
  balances automatically.
- **Status:** Configurable, read-only.

## 2. Subscriptions, plans, and service dates

### Subscription expiration

- **When:** The expiration schedule runs.
- **If:** An active subscription has passed its end date.
- **Then:** The application expires it and starts the related notification and
  access-control consequences.
- **Status:** Scheduled by default.

### Subscription expiry reminders

- **When:** The expiry-reminder schedule runs.
- **If:** A subscription will expire within the configured warning period.
- **Then:** The customer is queued for a renewal reminder.
- **Status:** Scheduled by default.

### Scheduled plan changes

- **When:** The plan-change schedule runs.
- **If:** An approved future plan change has reached its effective date.
- **Then:** The application applies the new plan through the normal subscription
  lifecycle.
- **Status:** Scheduled by default.

### Scheduled subscription status changes

- **When:** The status-command schedule runs.
- **If:** A deferred activate, suspend, resume, disable, cancel, or similar
  command is due.
- **Then:** The application executes the command through the subscription owner.
- **Status:** Scheduled by default.

### Vacation-hold resumption

- **When:** The vacation-hold schedule runs.
- **If:** A subscription's approved hold has expired.
- **Then:** The application resumes the subscription through the normal restore
  process.
- **Status:** Scheduled by default.

### Paid service-change completion

- **When:** Payment for a pending change is confirmed, or its service order is
  completed.
- **If:** The exact change request has the required payment or completion
  evidence.
- **Then:** The application finalizes the requested service change.
- **Status:** Event-driven.

## 3. Usage, RADIUS, and customer access

### RADIUS accounting import

- **When:** The accounting importer reaches its configured interval.
- **If:** RADIUS accounting import is enabled and new records are available.
- **Then:** The application imports session and usage facts.
- **Status:** Configurable schedule.

### Usage metering

- **When:** The usage-metering schedule runs.
- **If:** New accounting usage exists for an active quota period.
- **Then:** The application adds that usage to the customer's quota bucket.
- **Status:** Configurable schedule.

### Usage rating

- **When:** The usage-rating schedule runs.
- **If:** Usage processing is enabled and eligible usage exists.
- **Then:** The application calculates the customer's rated usage position.
- **Status:** Configurable schedule.

### Fair Usage Policy evaluation

- **When:** The FUP schedule runs after usage has been metered.
- **If:** A customer crosses a configured usage threshold.
- **Then:** The application applies the configured throttle or block.
- **Status:** Configurable schedule.

### Expired FUP removal

- **When:** The independent FUP safety-net schedule runs.
- **If:** A throttle or block has passed its reset time.
- **Then:** The application removes the expired restriction even if the billing
  worker is delayed.
- **Status:** Scheduled by default.

### Expiring data-bundle warning

- **When:** The daily bundle-expiry scan runs.
- **If:** A purchased bundle will expire within the next 24 hours.
- **Then:** The application queues the configured customer warning.
- **Status:** Configurable with usage processing.

### Stale session cleanup

- **When:** The RADIUS and usage session reapers run.
- **If:** A session remains open after its source stopped reporting, such as
  after a NAS restart or a lost stop record.
- **Then:** The application closes the stale session so it no longer appears
  online or continues accumulating usage.
- **Status:** Configurable schedule.

### Active-session reconstruction

- **When:** The active-session reconciliation schedule runs.
- **If:** Stored active-session state differs from current open RADIUS facts.
- **Then:** The application rebuilds the active-session view.
- **Status:** Scheduled by default.

### Device-login RADIUS synchronization

- **When:** The device-login synchronization schedule runs.
- **If:** Active staff device-login assignments have changed or drifted.
- **Then:** The application rebuilds the administrative RADIUS login records.
- **Status:** Scheduled by default.

### Access enforcement reconciliation

- **When:** The enforcement reconciler runs.
- **If:** A subscription's intended access state differs from RADIUS, NAS, IP,
  or active-session state.
- **Then:** The application repairs the derived network-access state.
- **Status:** Scheduled by default.

### Access consistency audits

- **When:** The suspension, IP-consistency, or connectivity-shadow audit runs.
- **If:** The relevant audit switch is enabled.
- **Then:** The application reports customers whose actual access, IP address,
  or projected connection state disagrees with the authoritative state.
- **Status:** Configurable, read-only.

## 4. Provisioning and activation

### Subscription activation provisioning

- **When:** A subscription is activated or a service-order activation is
  requested.
- **If:** The event contains a valid subscription and required provisioning
  information.
- **Then:** The application allocates an IP address, rebuilds RADIUS access, and
  sends any required NAS commands.
- **Status:** Event-driven.

### Subscription-resume provisioning

- **When:** A suspended subscription is resumed.
- **If:** Its IP or RADIUS projection needs to be restored.
- **Then:** The application restores the IP assignment and rebuilds network
  authentication data.
- **Status:** Event-driven.

### Service-order provisioning workflow

- **When:** A service order is assigned.
- **If:** No successful or active provisioning run already exists and a matching
  workflow is available.
- **Then:** The application starts the provisioning workflow.
- **Status:** Event-driven.

### Provisioning readiness decision

- **When:** A provisioning run completes or fails.
- **If:** The event identifies the exact service order and provisioning run.
- **Then:** The application re-evaluates whether the service order can advance.
- **Status:** Event-driven.

### Stale provisioning-run cleanup

- **When:** The provisioning-run reaper runs.
- **If:** A run has remained in `running` longer than the allowed timeout.
- **Then:** The application marks it failed so it cannot block later work
  indefinitely.
- **Status:** Scheduled by default.

### Compensation retry

- **When:** The compensation watchdog runs.
- **If:** A failed provisioning consequence is due for another attempt.
- **Then:** The application retries it with controlled backoff.
- **Status:** Scheduled by default.

### Background bulk activation and migration

- **When:** An authorized user starts a bulk activation or service-migration
  job.
- **If:** The submitted records remain valid when the worker processes them.
- **Then:** The application executes each item in the background and records
  individual outcomes.
- **Status:** Manual-start background automation.

## 5. ONT, OLT, routers, NAS, and TR-069

### ONT commissioning

- **When:** An ONT commissioning request is queued.
- **If:** The ONT identity and requested configuration are valid.
- **Then:** The application authorizes the ONT and applies the management-only
  baseline configuration.
- **Status:** Event/manual-start background automation.

### Commissioning verification

- **When:** Commissioning reaches its verification step.
- **If:** The ONT can be found through the configured ACS.
- **Then:** The application checks management readiness and records success or a
  retryable result.
- **Status:** Background automation.

### Commissioned-ONT expiry cleanup

- **When:** The commissioning reconciliation schedule runs.
- **If:** A commissioned ONT remains unassigned beyond its permitted window.
- **Then:** The application returns it to inventory.
- **Status:** Scheduled by default.

### ONT intent reconciliation

- **When:** The ONT reconciliation sweep runs.
- **If:** Reconciliation is enabled and an active ONT's intended and observed
  states differ.
- **Then:** The application applies or records the required repair and verifies
  its result.
- **Status:** Configurable schedule.

### Overdue ONT hold alert

- **When:** The hourly hold check runs.
- **If:** An ONT reconciliation hold has passed its review date.
- **Then:** The application surfaces it for operator attention.
- **Status:** Scheduled by default.

### ONT status and signal collection

- **When:** The Huawei status and signal-observation schedules run.
- **If:** Active supported OLTs and ONTs are available.
- **Then:** The application records online state, signal readings, and other
  observed device facts.
- **Status:** Scheduled/background automation.

### ONT service configuration

- **When:** A tracked ONT configuration command is queued.
- **If:** The exact assignment and configuration revision are still current.
- **Then:** The application applies the command and performs read-back
  verification.
- **Status:** Event/manual-start background automation.

### ONT firmware upgrade

- **When:** An authorized firmware upgrade is started.
- **If:** The image and target ONT are valid.
- **Then:** The application delivers the image, waits for reboot, and verifies
  the reported version.
- **Status:** Manual-start background automation.

### OLT firmware upgrade or rollback

- **When:** An authorized upgrade or rollback is started.
- **If:** The OLT and selected image/standby image are eligible.
- **Then:** The application performs the operation in the background and records
  the outcome.
- **Status:** Manual-start background automation.

### OLT connection retry

- **When:** The retry schedule runs or a single OLT failure requests an
  immediate retry.
- **If:** The OLT is in a failed health state.
- **Then:** The application retries its reachability check.
- **Status:** Scheduled and event-driven.

### OLT MAC harvesting

- **When:** A fleet harvest or single-OLT harvest is queued.
- **If:** The OLT is active and supported.
- **Then:** The application reads learned MAC information with an independent
  lock and timeout for each OLT.
- **Status:** Background automation.

### OLT profile synchronization

- **When:** The approved profile-sync schedule becomes due.
- **If:** Profile synchronization is enabled and the task is approved.
- **Then:** The application applies the due OLT profile synchronization.
- **Status:** Configurable schedule.

### Device configuration backups

- **When:** The OLT, router, or NAS backup schedule runs.
- **If:** The device is active and backups are enabled for it.
- **Then:** The application captures and stores its current configuration.
- **Status:** Scheduled by default, subject to device settings.

### NAS backup retention

- **When:** The backup-retention schedule runs.
- **If:** Retention cleanup is enabled and backups exceed the retention period.
- **Then:** The application removes the expired backup records/files according
  to policy.
- **Status:** Configurable schedule.

### Router configuration read-back

- **When:** The read-back repair schedule runs.
- **If:** A router write has an incomplete or ambiguous recorded result.
- **Then:** The application reads the live configuration and settles the
  operation outcome.
- **Status:** Scheduled by default.

### Router source-of-truth drift audit

- **When:** The drift audit runs.
- **If:** A router has a current intended configuration to compare.
- **Then:** The application reports differences between intent and live owned
  resources.
- **Status:** Scheduled by default, read-only.

### MikroTik NAS VLAN read-back

- **When:** The VLAN read-back schedule runs.
- **If:** A VLAN operation is waiting for verification.
- **Then:** The application compares live RouterOS state and completes or flags
  the operation.
- **Status:** Scheduled by default.

### GenieACS device synchronization

- **When:** The TR-069 device-sync schedule runs.
- **If:** TR-069 synchronization is enabled and an active ACS is configured.
- **Then:** The application imports current ACS device observations.
- **Status:** Configurable schedule.

### GenieACS command reconciliation

- **When:** The command reconciler runs.
- **If:** An ACS task was accepted but has no final local outcome.
- **Then:** The application checks the remote result and completes, fails, or
  keeps the command pending.
- **Status:** Scheduled by default.

### TR-069 health and runtime refresh

- **When:** The health or runtime schedule runs.
- **If:** The relevant feature is enabled.
- **Then:** The application checks last-contact freshness and refreshes WAN,
  Wi-Fi, LAN, and device runtime information.
- **Status:** Configurable schedule.

### TR-069 cleanup and metrics

- **When:** The cleanup or metrics schedule runs.
- **If:** The corresponding switch is enabled.
- **Then:** The application removes old session/task records or publishes
  GenieACS fleet metrics.
- **Status:** Configurable schedule.

## 6. Monitoring, topology, and outages

### Infrastructure polling

- **When:** The native monitoring interval is reached.
- **If:** Active devices have supported management addresses and credentials.
- **Then:** The application performs ping/SNMP checks and records current health.
- **Status:** Scheduled by default.

### RADIUS health monitoring

- **When:** The RADIUS health schedule runs.
- **If:** The health probe can access its configured dependencies.
- **Then:** The application records health and publishes monitoring metrics.
- **Status:** Scheduled by default.

### Topology status refresh

- **When:** The topology warming schedule runs.
- **If:** Recent native poll results exist.
- **Then:** The application refreshes the cached live status of topology nodes.
- **Status:** Scheduled by default.

### LLDP topology polling

- **When:** The LLDP schedule runs.
- **If:** Supported devices can be polled.
- **Then:** The application reads neighbour information and reconciles network
  links.
- **Status:** Scheduled by default.

### UISP topology synchronization

- **When:** The UISP topology schedule runs.
- **If:** UISP integration is available.
- **Then:** The application updates customer-device topology and management-IP
  information.
- **Status:** Scheduled by default.

### UFiber match reporting

- **When:** The UFiber linking schedule runs.
- **If:** Possible ONU and subscription matches are found.
- **Then:** The application reports the candidates without automatically linking
  them.
- **Status:** Scheduled by default, read-only.

### Monitoring inventory synchronization

- **When:** The inventory synchronization schedule runs.
- **If:** Active NAS or RouterOS inventory exists.
- **Then:** The application projects that inventory into the native monitoring
  system.
- **Status:** Scheduled by default.

### Monitoring coverage refresh

- **When:** The coverage schedule runs.
- **If:** Current managed-device and address information exists.
- **Then:** The application recalculates and caches reachable management
  networks.
- **Status:** Scheduled by default.

### Unified device projection repair

- **When:** The device-projection reconciliation schedule runs.
- **If:** Device information derived from authoritative NAS, router, OLT, ONT,
  or related records is missing or stale.
- **Then:** The application rebuilds the unified device view used by monitoring
  and administration.
- **Status:** Scheduled by default.

### Forwarding-control observation collection

- **When:** The forwarding-observation schedule runs.
- **If:** The fail-closed collection control is enabled.
- **Then:** The application records time-limited observations of forwarding
  state for later comparison and safety decisions.
- **Status:** Configurable schedule.

### UISP configuration read-back

- **When:** The UISP read-back schedule runs.
- **If:** A UISP configuration action is awaiting confirmation.
- **Then:** The application reads the observed UISP state and completes or flags
  the action without assuming that the original write succeeded.
- **Status:** Scheduled by default.

### Outage detection and lifecycle

- **When:** The outage classifier reconciliation schedule runs.
- **If:** Device/customer observations remain consistent long enough to pass the
  debounce and confidence rules.
- **Then:** The application creates, confirms, clears, reopens, reroots, discards,
  or resolves the outage incident as appropriate.
- **Status:** Scheduled by default.

### Outage consequences

- **When:** An outage is created, confirmed, cleared, reopened, discarded, or
  resolved.
- **If:** The event identifies a valid incident.
- **Then:** The application updates customer downtime accrual, communications,
  and operational escalations. Resolution does not automatically close support
  tickets or work orders.
- **Status:** Event-driven.

### Automatic outage notification

- **When:** The outage-notification schedule runs.
- **If:** Automatic notification is enabled and a settled outage has enough
  confidence and affected customers.
- **Then:** The application queues customer-safe outage or restoration messages.
- **Status:** Configurable schedule.

### Infrastructure administrator alerts

- **When:** The alert evaluator runs.
- **If:** Current device or platform health meets an active alert rule.
- **Then:** The application opens or updates an administrator-facing alert; it
  resolves it when the condition clears.
- **Status:** Scheduled by default.

### Channel health observer

- **When:** The channel-health interval is reached.
- **If:** Messaging/integration freshness and worker queue information are
  available.
- **Then:** The application records inbound-channel freshness and queue depth so
  stalled channels can be detected.
- **Status:** Scheduled by default.

### Availability and impact metrics

- **When:** The relevant snapshot or export schedule runs.
- **If:** Current device and topology observations exist.
- **Then:** The application records infrastructure availability and publishes
  customer-impact, topology, and network-operation metrics.
- **Status:** Scheduled by default or configurable by metric type.

## 7. Notifications, campaigns, and outgoing messages

### Event-based customer notification

- **When:** A supported business event is committed.
- **If:** A matching template exists, the customer can be resolved, the channel
  is enabled, and notifications are not suppressed.
- **Then:** The application creates delivery requests for the allowed channels.
- **Status:** Event-driven.

Supported events include:

- customer account created or updated;
- subscription created, activated, suspended, resumed, cancelled, upgraded,
  downgraded, expiring, or expired;
- invoice created, sent, paid, or overdue;
- payment received, failed, refunded, or reversed;
- prepaid service renewed;
- payment arrangement defaulted;
- usage warning, exhausted quota, top-up, or expiring add-on;
- provisioning or service-order progress;
- ONT discovery, offline, online, or degraded signal;
- area and last-mile outages; and
- referral reward issuance.

### Notification delivery queue

- **When:** A notification is committed or the recovery schedule runs.
- **If:** The notification is due and has not already been delivered.
- **Then:** The application sends it immediately or retries it through the
  appropriate provider.
- **Status:** Event-driven with scheduled recovery.

### ZeptoMail delivery reconciliation

- **When:** A signed ZeptoMail delivery webhook arrives or the tracking schedule
  runs.
- **If:** Tracking is enabled and a submitted email lacks a final delivery
  result.
- **Then:** The application updates the email's delivered, bounced, or failed
  state.
- **Status:** Webhook-driven with configurable scheduled recovery.

### Campaign processing

- **When:** The campaign schedule runs.
- **If:** A campaign or nurture-sequence step is active and due.
- **Then:** The application selects the eligible audience and queues the next
  message batch/step.
- **Status:** Scheduled by default.

### Operational escalation delivery

- **When:** The escalation-delivery schedule runs.
- **If:** An escalation is due, enabled, and has not already been delivered.
- **Then:** The application delivers it through the configured operational
  channel.
- **Status:** Configurable schedule.

### Outgoing platform webhooks

- **When:** A supported application event is committed.
- **If:** An active webhook endpoint subscribes to that event.
- **Then:** The application creates a durable delivery record and sends the
  webhook, with retry evidence if delivery fails.
- **Status:** Event-driven and administrator-configurable.

## 8. Support tickets and assignment rules

### Ticket assignment rules

- **When:** A qualifying ticket or project is created or evaluated for
  assignment.
- **If:** An active rule matches its type, region, team, tags, or other configured
  scope.
- **Then:** The application assigns the configured team or selects a person by
  round-robin or least-loaded strategy.
- **Status:** Rule-driven.

### Legacy ticket automation rules

- **When:** A ticket is created, its status changes, or its priority changes.
- **If:** An active rule's equality conditions match the ticket and identity
  review has not paused automation.
- **Then:** The application can assign a team, assign a technician, set priority,
  set status, set a due time, or add a tag.
- **Status:** Rule-driven.

### Automation Center support rules

- **When:** A `support.ticket.created` event is committed.
- **If:** A published rule matches priority, customer, ticket type, channel, or
  region.
- **Then:** The application assigns a service team or sets the ticket priority.
- **Status:** Rule-driven. These are the currently registered executable
  Automation Center actions.

### Identity-review safety stop

- **When:** Ticket automation evaluates a ticket.
- **If:** Customer identity resolution requires manual review.
- **Then:** Ticket and account-sensitive automation is paused for that ticket.
- **Status:** Automatic safety rule.

### Work-order result projection

- **When:** Field staff records a work-order outcome.
- **If:** The work order came from a support ticket.
- **Then:** The application attaches the field result to the support handoff.
  Field completion alone does not automatically resolve the ticket.
- **Status:** Event-driven.

### Resolution confirmation

- **When:** A ticket-resolution confirmation timer becomes due.
- **If:** The ticket is still waiting for customer confirmation and no dispute
  has interrupted the process.
- **Then:** The application confirms the resolution through the support
  lifecycle.
- **Status:** Event-driven through durable timers.

### Survey invitation

- **When:** A ticket resolution is confirmed or a qualifying field outcome is
  recorded.
- **If:** An applicable active survey is configured and the invitation has not
  already been created.
- **Then:** The application creates the customer survey invitation.
- **Status:** Event-driven.

## 9. Team Inbox, WhatsApp, Meta, and AI intake

### Inbound message processing

- **When:** A verified WhatsApp, Meta social, email/widget, or other supported
  channel message arrives.
- **If:** The message is valid and not a duplicate.
- **Then:** The application records it, finds or creates the conversation, and
  updates routing state.
- **Status:** Webhook/event-driven.

### Inbox automation rules

- **When:** A conversation is created or an inbound message is received.
- **If:** An active rule matches channel, status, priority, team, or contact
  resolution state, and AI does not currently own the conversation.
- **Then:** The application assigns a named agent, automatically selects an
  available agent, queues the conversation, or adds a tag.
- **Status:** Rule-driven.

### FIFO queue promotion

- **When:** The queue-promotion schedule runs.
- **If:** A queued conversation has an eligible available agent.
- **Then:** The application assigns the next conversation in queue order.
- **Status:** Scheduled by default.

### Queue-position notification

- **When:** The queue-notification scan runs.
- **If:** The feature is enabled and a queued customer is due an update.
- **Then:** The application sends the customer's current queue-position update.
- **Status:** Configurable schedule.

### Scheduled reply release

- **When:** The reply-release schedule runs.
- **If:** A scheduled reply's send time has arrived.
- **Then:** The application releases it to the outbound messaging process.
- **Status:** Scheduled by default.

### Snooze wake-up

- **When:** A conversation's durable snooze timer or recovery schedule becomes
  due.
- **If:** The conversation is still snoozed.
- **Then:** The application returns it to the open queue.
- **Status:** Scheduled/event-driven.

### WhatsApp service-window expiry

- **When:** The window-expiry schedule runs.
- **If:** The permitted WhatsApp service window has closed.
- **Then:** The application releases or updates routing state so later replies
  follow the correct template/window rules.
- **Status:** Scheduled by default.

### AI intake processing

- **When:** A new AI-intake session is ready or the processing schedule runs.
- **If:** AI intake is enabled and the session is eligible for automated
  handling.
- **Then:** The application advances the AI intake conversation and records its
  ownership/routing result.
- **Status:** Scheduled/event-driven.

### AI intake recovery

- **When:** The recovery schedule runs.
- **If:** An AI-intake session has remained in processing beyond the allowed
  period.
- **Then:** The application recovers or releases the stale session.
- **Status:** Scheduled by default.

### Failed outbound retry

- **When:** The outbound retry schedule runs.
- **If:** A message failed in a retryable way and is due for another attempt.
- **Then:** The application retries delivery.
- **Status:** Scheduled by default.

### Media promotion

- **When:** The media-promotion schedule runs.
- **If:** A message has a verified temporary media asset that has not yet been
  promoted.
- **Then:** The application moves it into the durable message-media record.
- **Status:** Scheduled by default.

### Participant backfill

- **When:** The participant-backfill schedule runs.
- **If:** A conversation is missing its participant projection.
- **Then:** The application reconstructs it from authoritative conversation and
  message data.
- **Status:** Scheduled by default.

### Stale-conversation auto-resolution

- **When:** It would run on an hourly schedule.
- **If:** A tenant deliberately enabled the policy and a conversation met its
  stale-resolution rules.
- **Then:** The application would close the conversation.
- **Status:** Present in code but explicitly disabled by default.

## 10. SLA timers

### Ticket SLA warning and breach

- **When:** A ticket SLA warning or breach timer becomes due.
- **If:** The SLA clock is still active and the qualifying response/resolution
  has not happened.
- **Then:** The application records the near-breach warning or the breach once.
- **Status:** Event-driven through durable timers.

### Project and project-task SLA warning and breach

- **When:** A project or task SLA timer becomes due.
- **If:** The relevant clock is still active and incomplete.
- **Then:** The application records the near-breach warning or breach against
  the project/task.
- **Status:** Event-driven through durable timers.

### Durable timer dispatcher

- **When:** The timer-dispatch schedule runs.
- **If:** A durable timer is due and has not already fired.
- **Then:** The application emits the exact lifecycle event that owns the timed
  consequence.
- **Status:** Scheduled by default.

## 11. Sales and customer fulfilment

### Funding-to-implementation handoff

- **When:** A sales order's funding is satisfied.
- **If:** The order is eligible and the event has not already been consumed.
- **Then:** The application records the payment consequence and advances the
  order into fulfilment.
- **Status:** Event-driven.

### Verified implementation release

- **When:** A vendor installation project is verified.
- **If:** It belongs to a sales fulfilment chain.
- **Then:** The application releases the verified implementation to the next
  service stage.
- **Status:** Event-driven.

### Service-order release

- **When:** A sales-linked service order is released.
- **If:** It belongs to a sales order rather than an independent repair order.
- **Then:** The application advances it into provisioning.
- **Status:** Event-driven.

### Customer-experience handoff

- **When:** A sales-linked service order completes.
- **If:** The sales order requires customer acceptance.
- **Then:** The application creates the customer-experience acceptance handoff.
- **Status:** Event-driven.

### Customer acceptance completion

- **When:** The customer-experience handoff is accepted.
- **If:** It identifies the correct sales order and handoff.
- **Then:** The application marks the sales order fulfilled.
- **Status:** Event-driven.

### Overdue acceptance flag

- **When:** The customer-acceptance timer becomes due.
- **If:** The handoff is still awaiting acceptance.
- **Then:** The application flags it as overdue for attention.
- **Status:** Event-driven through durable timers.

## 12. Field materials, vendors, and ERP

### Approved material request export

- **When:** A field material request is approved.
- **If:** It has not already been consumed and ERP delivery is available.
- **Then:** The application creates the durable ERP issue request.
- **Status:** Event-driven.

### Material-request cancellation export

- **When:** Cancellation of a field material request is approved/requested.
- **If:** The request has an ERP-side consequence to cancel.
- **Then:** The application creates the durable ERP cancellation request.
- **Status:** Event-driven.

### Project-completion purchase invoice

- **When:** A vendor project is completed.
- **If:** The project qualifies for supplier invoicing.
- **Then:** The application prepares the project-completion purchase invoice
  request.
- **Status:** Event-driven.

### Approved vendor invoice export

- **When:** A vendor purchase invoice is approved.
- **If:** It is eligible and has not already been exported.
- **Then:** The application queues it for ERP accounts-payable creation.
- **Status:** Event-driven.

### ERP outbox delivery

- **When:** A qualifying event is committed or the ERP outbox schedule runs.
- **If:** ERP capability and ownership are enabled and a delivery is pending.
- **Then:** The application sends the durable event to ERP and records the
  response.
- **Status:** Configurable scheduled recovery with event-driven creation.

### ERP material catalogue refresh

- **When:** The daily catalogue refresh runs.
- **If:** The ERP inventory capability is enabled.
- **Then:** The application refreshes ERP item and warehouse facts used by Sub.
- **Status:** Configurable schedule.

### ERP expense and material status refresh

- **When:** The relevant status schedule runs.
- **If:** ERP status capability is enabled and claims/requests are still in
  progress.
- **Then:** The application refreshes the mirrored ERP status.
- **Status:** Configurable schedule.

### Purchase-order write-back repair

- **When:** The repair schedule runs.
- **If:** ERP accepted a purchase order but the ERP ID was not saved locally.
- **Then:** The application reapplies the saved ERP response without sending the
  purchase order again.
- **Status:** Configurable schedule.

### Purchase-invoice repair

- **When:** The repair schedule runs.
- **If:** A purchase invoice became eligible after its purchase-order write-back
  or an attachment still needs delivery.
- **Then:** The application queues the missing creation or attachment step.
- **Status:** Configurable schedule.

### Supplier-invoice payment observation

- **When:** The ERP invoice-status schedule runs.
- **If:** A vendor invoice is awaiting settlement information.
- **Then:** The application reads ERP's current settlement status and updates the
  local observation.
- **Status:** Configurable schedule.

### ERP operational context synchronization

- **When:** The operational-domain schedule runs.
- **If:** The ERP operational-sync capability is enabled.
- **Then:** The application sends current project, ticket, project-task, and
  work-order context to ERP.
- **Status:** Configurable schedule.

### ERP staff-access reconciliation

- **When:** A signed ERP staff-access webhook arrives or the 15-minute repair
  loop runs.
- **If:** The ERP staff-access capability is enabled.
- **Then:** The application applies authoritative leave/access restrictions and
  repairs any missed webhook result.
- **Status:** Webhook-driven with scheduled recovery.

## 13. CRM, lead capture, and other integrations

### CRM ticket pull

- **When:** The configured CRM polling interval is reached.
- **If:** CRM ticket pull is enabled and its credentials/capability are ready.
- **Then:** The application imports new or changed CRM tickets and comments.
- **Status:** Configurable schedule.

### CRM full reconciliation

- **When:** The daily full CRM pull runs.
- **If:** CRM ticket pull is ready.
- **Then:** The application checks records that incremental polling can miss,
  including comments or closed tickets.
- **Status:** Configurable schedule.

### CRM customer and quote webhooks

- **When:** A signed CRM customer, general event, or quote webhook arrives.
- **If:** Its signature and delivery identity are valid.
- **Then:** The application records and applies the matching customer or quote
  mirror update.
- **Status:** Webhook-driven.

### Lead-capture webhook

- **When:** A signed lead-capture webhook arrives.
- **If:** The capability binding, signature, and delivery ID are valid.
- **Then:** The application records the lead through the lead owner without
  duplicating a previous delivery.
- **Status:** Webhook-driven.

### Meta lead generation

- **When:** Meta sends a verified lead-generation event.
- **If:** The page/form integration is recognized and the event is new.
- **Then:** The application queues lead conversion/matching work.
- **Status:** Webhook-driven background automation.

### Fiber inquiry webhook

- **When:** A signed fiber-inquiry event arrives.
- **If:** The binding, signature, and delivery ID are valid.
- **Then:** The application records the inquiry and starts its configured intake
  consequence.
- **Status:** Webhook-driven.

### Configurable integration jobs

- **When:** An enabled integration job reaches its stored schedule.
- **If:** Its connector and credentials are ready.
- **Then:** The application runs the configured integration connector and records
  the result.
- **Status:** Administrator-configurable schedule.

### OAuth token refresh

- **When:** The OAuth refresh schedule runs.
- **If:** Refresh is enabled and a token is close enough to expiry.
- **Then:** The application refreshes each eligible token independently.
- **Status:** Configurable schedule.

## 14. Identity, invitations, credentials, and referrals

### Staff invitation

- **When:** A staff account is provisioned.
- **If:** It has a valid email and has not already received the same invitation.
- **Then:** The application sends a secure account-setup invitation.
- **Status:** Event-driven.

### Reseller invitation

- **When:** A reseller user is provisioned.
- **If:** It has a valid email and the invitation is not a duplicate.
- **Then:** The application sends the reseller account-setup invitation.
- **Status:** Event-driven.

### Password recovery

- **When:** Password recovery is requested.
- **If:** The application resolves one safe eligible recovery target.
- **Then:** It sends the time-limited recovery action without exposing whether
  an unrelated account exists.
- **Status:** Event-driven.

### Session revocation after credential change

- **When:** Password recovery or customer credential enrollment completes.
- **If:** The event identifies the affected customer/reseller principal.
- **Then:** The application invalidates authentication caches and revokes the
  affected active sessions.
- **Status:** Event-driven.

### Invitation expiry

- **When:** An invitation-expiry durable timer becomes due.
- **If:** The invitation is still unused and validly awaiting expiry.
- **Then:** The application marks it expired.
- **Status:** Event-driven through durable timers.

### Credential encryption-key rotation

- **When:** The daily rotation schedule runs.
- **If:** Scheduled rotation is enabled and rotation is due.
- **Then:** The application rotates eligible encrypted credential material using
  the configured key owner.
- **Status:** Configurable schedule.

### NIN verification

- **When:** An authorized verification request is submitted.
- **If:** The request is valid and the verification provider is available.
- **Then:** The application performs verification in a dedicated background
  queue and records the result.
- **Status:** Manual-start background automation.

### Referral qualification

- **When:** A subscriber is created, updated, reactivated, or activated.
- **If:** A captured referral is linked and meets the qualification rules.
- **Then:** The application qualifies it and can issue the configured reward.
- **Status:** Event-driven.

### WireGuard housekeeping

- **When:** The WireGuard log-cleanup or token-cleanup schedule runs.
- **If:** The relevant cleanup is enabled and records/tokens are older than
  their allowed lifetime.
- **Then:** The application deletes expired connection logs or clears expired
  provisioning tokens.
- **Status:** Configurable schedule.

### VPN control and health jobs

- **When:** An authorized VPN operation is queued or a VPN health scan is
  started by a configured schedule/operator process.
- **If:** The VPN target and requested operation are valid.
- **Then:** The application performs the control operation or records current
  VPN health in the background.
- **Status:** Manual-start/background capability; its schedule may be
  database-defined.

### Expired AI operational insights

- **When:** The stale-insight cleanup task is started by a configured schedule
  or operator process.
- **If:** An AI operational insight has passed its validity period.
- **Then:** The application expires it so it is no longer treated as current
  advice.
- **Status:** Background capability; its schedule may be database-defined.

## 15. Reports, imports, exports, and data maintenance

### Scheduled exports

- **When:** An administrator-defined export schedule becomes due.
- **If:** The schedule is enabled and its export definition is valid.
- **Then:** The application generates the export in the background and records
  its result.
- **Status:** Administrator-configurable schedule.

### Manual export jobs

- **When:** An authorized user requests an export.
- **If:** The requested dataset and filters are valid.
- **Then:** The application generates the file in the background.
- **Status:** Manual-start background automation.

### Import jobs

- **When:** An authorized user submits an import.
- **If:** The file and import mapping are valid.
- **Then:** The application validates every row and, unless it is a dry run,
  applies accepted rows while recording failures.
- **Status:** Manual-start background automation.

### Invoice PDF generation

- **When:** An invoice PDF export is requested.
- **If:** The invoice exists and the requester is allowed to export it.
- **Then:** The application renders the PDF in the background.
- **Status:** Manual-start background automation.

### GIS synchronization

- **When:** The GIS schedule runs.
- **If:** GIS synchronization is enabled and configured sources are available.
- **Then:** The application synchronizes the configured GIS sources.
- **Status:** Configurable schedule.

### Batch geocoding

- **When:** An authorized user starts a batch geocoding job.
- **If:** The records contain usable location information.
- **Then:** The application geocodes them in the background and records per-item
  results.
- **Status:** Manual-start background automation.

### NCC weekly report

- **When:** The five-minute admission poll runs.
- **If:** Weekly email is enabled, it is the eligible Tuesday/local-time
  occurrence, and that occurrence has not already been sent.
- **Then:** The application queues the NCC report email once.
- **Status:** Configurable schedule.

### Bandwidth processing

- **When:** Bandwidth stream, aggregation, cleanup, and trim schedules run.
- **If:** Bandwidth processing is enabled.
- **Then:** The application stores fresh samples, calculates aggregates, pushes
  metrics, removes expired hot data, and trims the input stream.
- **Status:** Configurable schedule.

### MRR and IP-utilization snapshots

- **When:** Their task is started by a configured schedule or operator process.
- **If:** Eligible billing or IP-pool records exist.
- **Then:** The application records the point-in-time MRR or pool-utilization
  snapshot and can prune expired utilization history.
- **Status:** Background capability; schedule may be database-defined.

### Event outbox recovery

- **When:** The event dispatcher schedule runs.
- **If:** A committed event was left pending, such as after a process crash.
- **Then:** The application dispatches it to its registered consequences.
- **Status:** Scheduled by default.

### Failed-event retry

- **When:** The event retry schedule runs.
- **If:** An event handler failed in a retryable way and is due again.
- **Then:** The application retries the failed consequence.
- **Status:** Scheduled by default.

### Stuck-event recovery

- **When:** The stale-event schedule runs.
- **If:** An event has remained `processing` beyond the allowed lease.
- **Then:** The application marks it failed so normal retry/review can handle it.
- **Status:** Scheduled by default.

### Integration-inbox lease recovery

- **When:** The inbox reclaim schedule runs.
- **If:** A webhook/payment receipt was claimed by a worker whose lease expired.
- **Then:** The application makes the receipt retryable instead of leaving it
  permanently stuck.
- **Status:** Scheduled by default.

### Retention cleanup

- **When:** The relevant hourly/daily retention schedule runs.
- **If:** Records exceed their approved retention period.
- **Then:** The application prunes old completed events, device metrics, field
  location history, infrastructure availability, bandwidth samples, NAS
  backups, TR-069 records, WireGuard logs, and expired WireGuard tokens.
- **Status:** A mixture of default and configurable schedules.

### Cross-application drift detection

- **When:** The daily drift check runs.
- **If:** Drift detection is enabled.
- **Then:** The application compares registered cross-application projections,
  stores findings by fingerprint, and reports unresolved differences.
- **Status:** Configurable, mainly observational.

## 16. Browser-side conveniences

These are automatic user-interface behaviours rather than business workflow
owners:

- session refresh keeps an active browser login current;
- live search and type-ahead update results while the user types;
- forms perform client-side validation;
- invoice forms recalculate totals;
- unsaved-change warnings appear before leaving edited forms;
- operation trackers refresh background-job progress;
- charts refresh displayed operational data; and
- attendance/reminder scripts show configured administrator prompts.

These behaviours assist the user but do not replace server-side validation or
own business decisions.

## 17. Present in code but disabled, retired, or non-authoritative

### Stale Team Inbox auto-resolution

- The task exists, but its schedule is explicitly disabled by default because
  automatically closing customer conversations is a policy decision.

### Old Zabbix device synchronization

- Existing scheduled rows are retired because native monitoring is now used.

### Online-silent ONT healing loop

- The old periodic repair loop is deliberately not scheduled. Recovery is an
  operator-controlled action.

### Old ONT verification schedule

- The former provisioning-state verification schedule is retired.

### OLT deferred-operation/circuit-breaker subsystem

- It was removed because it was not connected to the real write paths.

### Old SLA polling task

- The old queued SLA detector consumes retired messages without doing business
  work. Durable timers now own SLA timing.

### Legacy quote and referral CRM mirror refresh

- These tasks remain compatible with old messages but no longer contact CRM.

### OLT hardware discovery sweep

- The task is currently a safe no-op because its former SNMP inventory source
  was retired.

## Main source locations

- `app/services/scheduler_config.py`: recurring schedule definitions.
- `app/tasks/`: background task adapters.
- `app/services/events/dispatcher.py`: event-driven routing and retry.
- `app/services/events/handlers/`: automatic consequences of committed events.
- `app/services/support_automation.py`: legacy support-ticket rules.
- `app/services/team_inbox_automation.py`: inbox automation rules.
- `app/services/sot_registry/domains/support_operations.py`: current Automation
  Center support trigger and action declarations.
- `app/api/*webhooks.py` and `app/api/billing.py`: inbound webhook triggers.
