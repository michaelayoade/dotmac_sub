# Website and social-DM campaign readiness

Campaign destinations are the website coverage form and Instagram/Facebook DMs.
The primary conversion is a committed Selfcare Lead, not a click, message,
coverage result, account creation or activated subscription.

## Ownership and daily operation

Sales (call center) owns new-connection and coverage sales enquiries. Support
retains complaints. Existing-customer identity alone does not make a complaint
a new Lead; a genuine additional-service opportunity follows the canonical
Sales capture path. Staff review uncertain identity or customer type rather
than guessing or resolving the conversation without handling the sales candidate.

At shift start, the Sales supervisor assigns a named responder for the sales
queue and reviews open enquiries, unassigned Leads and pending identity review.
The responder claims the conversation through the existing assignment action,
records the Lead owner through canonical Sales actions, and records the next
action and due time. A team route does not guarantee an individual Lead owner.

During campaign staffing hours, propose a five-minute first response. Before
launch the supervisor must approve actual staffed hours, the response target,
backup responder and escalation contact. Outside staffed hours, give an accurate
return time and carry the enquiry into the next staffed queue. Do not imply a
24-hour five-minute SLA without staffing approval.

End each shift by handing over uncontacted enquiries, failed deliveries, pending
review and overdue next actions. Escalate an unowned sales enquiry to the Sales
supervisor; escalate integration failures to the integration operator.

## Conversion reporting and reconciliation

Count distinct saved Lead IDs created in a declared UTC reporting window, using
immutable LeadOriginCapture evidence. Separate website `fiber-coverage-v1`,
Instagram DM and Facebook Messenger cohorts. Preserve unknown-source counts
rather than guessing channel from mutable display labels. DM origin interaction
keys include `inbox-message:` or `lead-intake:` prefixes; these are not bare UUIDs.

Report saved Leads, missing owners, pending staff review, next-action overdue,
and current qualification/won status separately. Won is not paid installation
or activated service. The existing visitor milestone written when a Lead is
created is not a count of all website visitors and cannot serve as that denominator.

Use `scripts/support/reconcile_inbox_classified_leads.py --days 7 --limit 500`
in read-only mode through the approved operator environment to find missing
social sales links. Review bounds and older windows so a limited result is not
represented as a complete audit. Any `--apply` requires separate authorization;
uncertain customer-type findings require staff review and are not auto-repaired.

Website Meta CAPI configuration is separate from DM capture. Follow
`META_CAPI_FIBER_LEADS.md`; verify the actual dataset before enabling delivery.
Do not reuse customer-converted/Lead Ads settings as website Lead measurement,
or encode an Instagram identifier as a phone number. Selfcare reporting remains
authoritative even when external conversion delivery is delayed or unavailable.

## Outstanding launch gates

- [ ] Deferred signing secret, secure WordPress exchange, settings and existing
  binding activation verified by the authorized operator.
- [ ] Website durable retry/recovery acceptance in `website/fiber/README.md`.
- [ ] Capture safeguard PR deployed after full GitHub CI and staging acceptance.
- [ ] Instagram/Facebook coverage-request routes configured to Sales after the
  corrected selector is deployed; existing new-connection routes verified.
- [ ] Named staff owners, hours, response target and escalation confirmed.
- [ ] Automated source-specific saved-Lead report implemented and reconciled
  with authoritative Leads/origins, including completeness/unknown-source signals.
- [ ] Website and DM synthetic journeys accepted on staging, including complaint,
  uncertain customer type, coverage failure, duplicate and timeout scenarios.
- [ ] Exact immutable image digest accepted on staging before production approval.

This runbook is an operating proposal and acceptance checklist. Its presence
does not prove production configuration, automated reporting or rollout is complete.
