# Inbox SLA rule definition and enforcement

Owner: `communications.inbox_sla`

The evaluator's reliability classification is owned by
`observability.task_reliability`: failures are logged and the next scheduled
sweep re-evaluates eligible clocks. Clock locks and persisted warning/breach
evidence make repeated evaluation idempotent; the transport does not blindly
autoretry the task.

Selfcare Inbox SLA clocks are separate from the retired CRM conversation
history and from customer service-level availability scoring.  A clock is
created only when an active Selfcare policy and matching rule exist.  The
default policy is an explicit fallback; there is no hard-coded runtime target.

Rules match service team, channel, and integer Inbox priority.  More specific
matches win; a default policy wins only when specificity is tied.  The first
customer inbound event starts one clock.  Duplicate inbound events do not
restart first response.  Only an outbound message carrying a human Inbox
person identity or human sender type satisfies response timing; AI and system
messages do not.  Resolution completes the clock.  Reopening clears only the
completion fact and preserves prior breach evidence.

Business calendars use UTC storage and a policy timezone, defaulting to
`Africa/Lagos`, Monday-Friday 09:00-17:00, with explicit ISO holiday dates.
The current CRM evidence did not prove a next-response or pause contract, so
next-response is optional and pause/resume remains an explicit future command;
the importer never invents either value.

CRM import accepts configuration-only JSON and explicit team/channel/priority
maps.  It is dry-run by default and rejects unresolved mappings.  It must not
copy CRM conversations, messages, users, customers, breach history, or
credentials.  Operators must retain the sanitised dry-run output and a
configuration backup before applying a reviewed import.
