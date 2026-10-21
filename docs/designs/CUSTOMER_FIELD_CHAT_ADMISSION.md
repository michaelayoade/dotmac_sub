# Customer field conversation admission

`communications.team_inbox_commands` owns customer inbound message admission.
The customer adapter's read checks are advisory. Its typed command supplies the
authenticated subscriber UUID, visit public ID, body and `CommandContext`; the
owner returns an inert typed message outcome after its executor commits.

The owner locks the native WorkOrder first, then its InboxConversation, refreshing
both identities. Admission requires an active customer visit owned by that
subscriber, a conversation bound to the same subscriber, and an open conversation.
Visit completion locks the conversation before checking the latest exchanged
message, so an admitted unanswered customer message reaches the follow-up queue.
The native visit transition already locks the WorkOrder before this participant.
Helpers flush only; the existing owner executor controls commit and rollback.

The subscriber/visit/idempotency-key external message identity replays an exact
body once; changed content rejects with `idempotency_conflict`. Current scope and
open state are checked even for a replay. Actor/scope mismatches reject. The
message, audit and version-one `field_job.customer_message_created` event stage
atomically. Audit/event payloads contain identifiers, not message contents.
The customer facade accepts the canonical typed send command without rebuilding
its context. Reads accept `CustomerChatThreadQuery` and return immutable typed
thread/message outcomes. HTTP adapters serialize UUIDs and enums explicitly to
the existing response schema. The authenticated principal supplies the subscriber
identity and context actor; request bodies cannot nominate a customer.
Realtime remains a post-commit transport and is suppressed on exact replay.
HTTP sends may carry an optional UUID `request_id`, reused as command/correlation
and idempotency identity across retries. Legacy body-only sends receive a fresh
adapter-generated UUID and retain their existing behavior.
The authenticated HTTP POST applies the shared browser CSRF header dependency;
bearer-authenticated native requests use the existing authenticated transport.

A reparented visit cannot expose its former subscriber's conversation through
availability, history, or send. No second message writer or conversation store
is introduced. Focused deterministic tests simulate scope/status changes between
preflight and admission, exact replay, changed-body conflict, closed replay and
history isolation. PostgreSQL lock scheduling requires the separately authorized
dedicated test server and is not established by SQLite unit tests.
