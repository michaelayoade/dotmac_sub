# Event orchestration source of truth

`app.services.control_relationships` owns event execution policy. Handlers own
their business behavior and expose their event scope through the mapping or set
already used by that behavior. The dispatcher builds one executable plan from
those sources before invoking any handler.

## Modes

- **Fanout**: consequences are independent. A failure is recorded and retried,
  but it does not suppress other handlers.
- **Chain**: state handlers run first, customer communication runs after all
  required state outcomes, and external delivery runs after state and
  communication. Handlers in the same stage remain independent unless the
  registry declares a specific dependency.

Payment receipt and invoice overdue are fanout events. Payment arrangement
progression, access restoration/suspension, customer receipts/reminders, and
external integrations are separately valuable consequences; one failure must
not suppress the others.

Subscription transitions, usage exhaustion, service-order assignment, and
provisioning outcomes are chains. Activation and resume have an additional
in-stage rule: enforcement depends on provisioning so RADIUS/session work
cannot run before IP/service provisioning has completed.

## Retry contract

`EventStore.failed_handlers` is the current failure manifest. Historical
`EventHandlerAttempt` rows remain an audit trail and are only a compatibility
fallback for old records without a manifest. A retry:

1. does not rerun handlers that already succeeded;
2. treats those successes as satisfied dependencies;
3. retries failed predecessors before their blocked dependants;
4. records dependency suppression as `blocked`, not as a handler failure;
5. replaces the failure manifest with only the current attempt's failures.

## Change rules

Every production handler must be declared once in `HANDLER_CONTROLS`, own a
unique capability, and expose a valid event scope. Every explicit dependency
must target a handler subscribed to the same chained event. Startup and CI fail
on duplicate handlers, missing scopes, unknown event types, missing dependency
targets, dependency cycles, or a chain with fewer than two handlers.
