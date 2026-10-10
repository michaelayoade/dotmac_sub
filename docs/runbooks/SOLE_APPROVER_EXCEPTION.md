# Sole-approver exception

Three finance flows are two-person flows that refuse self-approval. Michael is
the company's sole finance and admin decision-maker and decided: "I only
approve is enough". This runbook is the governed, time-boxed exception that
lets him approve his own request in those flows, and only those flows.

Owner of the decision: `app/services/sole_approver_exception.py` (one typed
policy, used by all three flows). The flows still own their own transactions,
approval records and audit evidence.

## Governance note

This mirrors the temporary admin review exception in
[`STAGING_PROMOTION.md`](STAGING_PROMOTION.md) ("Branch protection and the
temporary admin review exception", Governance decision 53). It is an exception
to a control, not a replacement for it, and it expires on its own.

- **Authority.** Michael's decision of 2026-10-10 ("I only approve is
  enough"). Record the Governance decision reference in
  `sole_approver_exception_decision_ref` when you enable it; the flows refuse
  the exception while that reference is empty, and copy it into every approval.
- **Scope.** Self-approval (requester == approver, or reviewer == approver)
  in exactly three flows listed below. Permissions, fingerprints, staleness,
  idempotency and every other guard are unchanged. The two-person rule stays
  the default for everyone and for every other flow.
- **Review date.** The exception is inert on or after
  `sole_approver_exception_review_due` (compared against the UTC date). Choose
  a short interval; aligning it with the decision 53 review (2026-10-27) keeps
  both exceptions reviewed together. Extending it is a new, deliberate
  settings write.
- **Rollback.** Any one of these turns it off, effective at the next command:
  set `sole_approver_exception_enabled` to `false`; clear
  `sole_approver_exception_principal`; or let the review date pass. Restore
  the independent approver as soon as a second finance approver exists.
  Approvals already recorded stay valid and keep their exception evidence.

## Settings

All four are `billing` domain settings, typed, seeded off, with no environment
bootstrap. They change only through the settings owner (Admin settings, which
requires `control:settings:write`), which audits every write.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `sole_approver_exception_enabled` | boolean | `false` | Master switch. |
| `sole_approver_exception_principal` | string (UUID) | empty | The `system_users.id` allowed to self-approve. |
| `sole_approver_exception_review_due` | string (`YYYY-MM-DD`) | empty | First UTC date on which the exception is inert. |
| `sole_approver_exception_decision_ref` | string | empty | Governance decision reference recorded as evidence. |

A malformed UUID or date is read as unset, which refuses the exception.

## When the exception applies

The exception allows requester == approver only when all of these hold; any
other case keeps the existing refusal (`self_approval_forbidden` or
`reviewer_conflict`) unchanged:

1. `sole_approver_exception_enabled` is `true`.
2. Today (UTC) is strictly before `sole_approver_exception_review_due`.
3. The approver is the system user named by
   `sole_approver_exception_principal`, is active, is a staff user
   (`user_type = system_user`), and the command actor is exactly
   `user:<that id>`. API keys, services and automated actors never qualify.
4. A non-empty justification (at most 1000 characters) is supplied, and a
   decision reference is set.

## Evidence

When the exception is used:

- the flow's approval record carries `sole_approver_exception=true`, the
  decision reference, the review date and the justification (an event payload
  for the renewal-term record and the period repair; the adjudication row and
  its audit event for the carried-source review);
- a distinct audit event `approval.sole_approver_exception_used` is staged in
  the same transaction, with the flow, entity, approver and the same evidence;
- every approval that did not use it records `sole_approver_exception=false`.

Find uses with: audit action `approval.sole_approver_exception_used`.

## Enable it

1. In Admin settings (billing domain), set, in this order:
   `sole_approver_exception_decision_ref` to the Governance decision
   reference, `sole_approver_exception_review_due` to the review date,
   `sole_approver_exception_principal` to Michael's `system_users.id`, then
   `sole_approver_exception_enabled` to `true`.
2. Run the flow's approve command as Michael with the justification argument
   (below). Without the argument the command refuses exactly as before.

## Turn it off

Set `sole_approver_exception_enabled` to `false` in Admin settings (and
optionally clear the principal). Verify with a self-approval attempt, which
must return `self_approval_forbidden` / `reviewer_conflict`.

## Flow 1: prepaid renewal-terms record

Owner `financial.prepaid_renewal_terms_backfill`. The requester approves the
request they raised. Add `--sole-approver-justification` to the approve step
of [`PREPAID_RENEWAL_TERMS_FINANCE_REVIEW.md`](PREPAID_RENEWAL_TERMS_FINANCE_REVIEW.md):

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  approve-renewal-term-record \
  --request <request-id> --amount <same amount> \
  --approver <Michael's SystemUser id> \
  --idempotency-key renewal-term-approve:<request_id> \
  --sole-approver-justification "<why own approval is sufficient>"
```

Evidence: event `prepaid_renewal_terms.recorded` payload, plus the audit event
on the subscription.

## Flow 2: carried-source identity review (for example ACC-005070)

Owner `billing.carried_source_identity_adjudication`. The reviewer and the
approver are the same staff user. Pass the same UUID for both, an actor of
exactly `user:<that UUID>`, and the justification; see the confirmation step
in [`PREPAID_FUNDING_AUDIT_RESTORE.md`](PREPAID_FUNDING_AUDIT_RESTORE.md):

```bash
python -m scripts.one_off.review_carried_source_identity \
  --account-id ACCOUNT_UUID --apply \
  --confirm RECORD_REVIEWED_NATIVE_BEFORE_HANDOFF \
  --fingerprint PREVIEW_SHA256 \
  --evidence-ref APPROVED_EVIDENCE_POINTER --evidence-sha256 EVIDENCE_SHA256 \
  --reviewed-by-id MICHAEL_STAFF_UUID --approved-by-id MICHAEL_STAFF_UUID \
  --actor user:MICHAEL_STAFF_UUID \
  --reason REVIEWED_REASON --idempotency-key UNIQUE_BUSINESS_KEY \
  --sole-approver-justification "<why own approval is sufficient>"
```

Evidence: migration 668 adds `sole_approver_exception`,
`sole_approver_exception_ref` and `sole_approver_justification` to the
append-only adjudication row; the database CHECK admits reviewer == approver
only on a row that carries them. The audit event
`carried_source_identity_adjudicated` carries the same evidence.

## Flow 3: paid-invoice period repair

Owner `financial.prepaid_paid_invoice_period_repair`. Add
`--sole-approver-justification` to the approve step of
[`PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md`](PREPAID_COVERAGE_QUARANTINE_FINANCE_REVIEW.md):

```bash
poetry run python -m scripts.billing.repair_prepaid_paid_invoice_period approve \
  --request <request-id> --fingerprint <same sha256> \
  --approver <Michael's SystemUser id> --idempotency-key <unique-key> \
  --sole-approver-justification "<why own approval is sufficient>"
```

Evidence: event `prepaid_paid_invoice_period.repaired` payload and the
`repair_paid_prepaid_invoice_period` audit event.

## Review checklist

- [ ] The review date has not passed, or was extended deliberately.
- [ ] A second finance approver exists: turn the exception off.
- [ ] Every `approval.sole_approver_exception_used` event has a justification
      that Finance accepts.
