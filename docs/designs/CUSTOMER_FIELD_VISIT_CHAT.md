# Customer mobile field visit conversation

The Customer app consumes the existing subscriber-scoped
`customer.field_job_chat` adapter and `communications.team_inbox_field_job`
conversation owner. It creates no separate conversation or eligibility policy.

The work-order card exposes Message technician only when the server's
`chat_technician` action allows it. The conversation response separately controls
availability and sending. A closed conversation remains readable if the owner
allows history, while sending is disabled. A missing eligibility field fails
closed. The customer never supplies another subscriber or technician identity.

The app refreshes on demand and every 20 seconds while the conversation is the
current route and the app is active. A failed refresh disables sending even if
stale history is displayed. Drafts remain local to this screen and are not
persisted across sign-out. Failed sends retain the draft and refresh history;
they are never automatically replayed. The app retains a request UUID for an
explicit retry of the same draft; the canonical owner returns the exact existing
message and rejects different content with that identity. Editing the draft or
confirming a successful send starts a new request identity. Customers are told
to inspect the conversation before retrying an uncertain send.

No new database state, owner, notification delivery mechanism or access grant is
introduced. Vendor chat eligibility remains controlled by the existing backend.

Validation: `mobile/test/field_visit_chat_test.dart` covers closed-history reads,
fail-closed eligibility, scoped sending and absence of automatic replay. Existing
backend authorization and visit lifecycle coverage remains in
`tests/test_field_job_chat.py`.
