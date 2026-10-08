# Survey lifecycle and creation source of truth

Status: implemented

## Owner and page contract

`communications.surveys` (`app.services.surveys`) owns Survey content,
lifecycle, invitation identity, response validation, metrics, idempotency, and
public-answer eligibility. Admin, API, public, and event-handler modules are
adapters. They create and close sessions, build typed commands, map domain
errors, and never write Survey records or complete business transactions.

The admin creation screen is `/admin/surveys/new`. Its audience is an
authenticated general administrator and its job is to save a reusable Survey
definition as a draft. The first viewport shows the breadcrumb, the `Create
Survey` heading, editable basic information, and the start of the ordered
question builder. `POST /admin/surveys` is the only primary action; Cancel is a
secondary navigation action. Both cards are part of one CSRF-protected form.
Validation errors render the same HTML projection with status 400 and preserve
the safely parsed form state. Success redirects with HTTP 303 to the canonical
Survey detail page.

The list screen `/admin/surveys` supports scanning identity, question count,
and lifecycle state. `New Survey` is its single page-level primary action.
Mobile keeps identity, state, and actions reachable without horizontal page
scrolling; the table itself uses a bounded horizontal work surface when needed.

### Response review page contract

The decision layer is a feedback report owned by
`communications.surveys.survey_report`, with typed `SurveyReportQuery`,
`SurveyReport`, question reports and count/percentage buckets. It aggregates
every persisted submission, independently of the 200-row recent-response limit.
No report sample data enters the application. Because public submissions do not
establish unique customer identity, counts are submissions and percentages are
of eligible answers, never of all customers or verified unique people.

The Customer Service Week `service_satisfaction` and `internet_reliability`
questions have explicitly supported ordered multiple-choice scales. Only an
exact match to those scales enables three-way sentiment grouping (positive 4/5,
neutral 3, negative 1/2). Service's `Haven't contacted customer service` and
reliability's `Not sure` are excluded and separately counted. Other multiple
choice, rating and NPS questions show option distributions; free text remains in
individual responses. Unsupported edited scales never infer sentiment from
labels or arbitrary customer text. Current options determine eligible answers;
unknown historical options and missing answers are separately disclosed.

Each distribution shows its own denominator and percentages rounded to one
decimal place; zero eligible answers show no percentage. Rounding may make a
displayed sum differ slightly from 100%. Summary service percentages appear
only for the supported scale. Charts are server-rendered, with the same textual
values accessible without color or JavaScript. Mobile stacks report panels.
Individual responses are collapsed below the report. Report freshness is the
database read transaction; it has no persisted cache. Rerunning the owner query
is the idempotent rebuild path. No sentiment writes, schema migration, new
export, notification, or identity attribution is added.

The Survey detail page serves customer-service administrators reviewing feedback.
`communications.surveys.response_reviews` accepts `SurveyResponseReviewQuery` and
returns immutable `SurveyResponseReview` rows; templates do not interpret saved
JSON. The first viewport keeps Survey identity, status, response count and Edit
with the beginning of recent submissions. Each submission presents a UTC time
and ordered question/answer pairs. Desktop uses two columns; mobile stacks each
answer below its question. Missing answers say `Not answered`; absent rating/NPS
fields are hidden, and a recorded NPS of zero remains visible.

This is a bounded recent-response detail surface, displaying the latest 200 rows
ordered by submission time and UUID descending. It explicitly shows how many are
displayed. Search, filters, bulk actions and export are outside this slice.
Answers are non-public customer feedback, visible under existing admin access;
no new public endpoint or identity inference is introduced. Raw response UUIDs
are available through a collapsed reference disclosure. Empty state says
`No responses yet`; existing error handling and authorization remain in place.

Labels come from the current Survey definition; historical question wording is
not stored. Saved keys absent from that definition remain visible under their
original key with `Earlier question — original wording unavailable`. Editing a
Survey never discards those saved answers. The review projection is rebuilt on
each request, has no cache or stored derived copy, and uses no mutation, audit
write or external side effect. Owner repair is simply rerunning the query.
All labels and answers use Jinja autoescaping. Tests cover removed questions,
missing answers, UTC conversion, zero NPS and unsafe HTML.

UI review: information ownership, desktop/mobile depth, empty states, semantic
markup, dark-mode contrast and progressive disclosure are addressed. New actions,
filters, exports, async operations and database migrations are not applicable.

## Typed content

Editable Survey fields are `name`, `description`, `trigger_type`,
`public_slug`, `thank_you_message`, and the ordered JSON question array.
Question objects use the closed `rating`, `nps`, `multiple_choice`, and
`free_text` vocabulary. Keys use one case-sensitive identifier policy and must
be unique. Multiple-choice options are ordered, trimmed, nonblank,
case-insensitively unique, and bounded to 2-50 values of at most 200 characters.

The browser serializes its complete ordered state into `questions_json`.
`SurveyCreate`/`SurveyUpdate` and `SurveyQuestion` reparse and validate the
complete untrusted payload. Invalid objects are never dropped. A draft may
contain no questions, but activation and distribution fail closed until at
least one valid question exists.

Public slugs normalize spaces and underscore-separated words into lowercase
hyphenated form. Existing hyphen mistakes remain visible validation failures;
the owner does not silently repair repeated, leading, or trailing separators.
The database unique constraint arbitrates concurrent slug creation and the
adapter receives a stable friendly domain error.

## Lifecycle and public access

The lifecycle is `draft -> active -> paused or closed`; a paused Survey may be
reactivated and a closed Survey may not. Creation always sets `draft` and
`is_active=true`. `is_active` is a row-level administrative availability fact,
not lifecycle state.

Every public and tracked response lookup requires all of:

- `status=active`;
- `is_active=true`;
- no expired `expires_at`; and
- at least one valid question.

A public slug never bypasses those checks. Unavailable and unknown references
share the same safe public response. Response labels, options, names,
descriptions, and thank-you messages render through Jinja autoescaping; no
user-supplied Survey value uses `safe`.

## Creator, transactions, and idempotency

Creation resolves the authenticated `SystemUser` inside the owner transaction
and requires its reviewed `person_party_id`. Browser-supplied creator,
lifecycle, expiry, segment, and metric values are absent from the command and
cannot be mass-assigned.

Every mutation enters `execute_owner_command` once. Creation keys are generated
on the server-rendered form and bind durably to a canonical content
fingerprint. A repeated identical POST returns the original Survey; reuse with
different content fails. Public slug, creation key, invitation source, and
tracked-response database constraints arbitrate concurrency.

Creation stages one Survey row, one audit record, and one identifier-only
domain event. It creates no invitation, response, notification, Ticket,
Work Order, or Project.

## Invitations and event triggers

Manual Send may activate a valid draft or paused Survey inside the same Survey
owner command. Automatic invitations consume the existing authoritative
events:

- `ticket.resolution_confirmed` for `ticket_closed`; and
- `work_order.field_outcome_recorded` with exact outcome `complete` for
  `work_order_completed`.

`SurveyTriggerHandler` is a delivery adapter. It does not infer status or poll
Tickets and Work Orders. The Survey owner queries only active, active-row,
unexpired Surveys with the matching trigger, validates that questions remain
distributable, and creates at most one invitation for a Survey, recipient, and
source event. Notification delivery is requested through the existing
communication-intent owner with a dedupe key; transport and retry remain in the
durable notification pipeline.

Tracked responses lock and complete their invitation and can be recorded only
once. Response validation is authoritative in the Survey owner. Answer payloads
are never written to audit or event metadata. Aggregate counts and rating/NPS
projections are recomputed from persisted responses in the response
transaction.

## Migration and repair

Migration `464_survey_lifecycle_and_creation` is additive. It preserves legacy
rows as drafts, adds lifecycle/content/provenance/metric columns, creates the
invitation ledger, and links tracked responses. A nullable creator on legacy
rows is migration evidence; every new create requires a resolved Person.

The old `app.services.comms` Survey and SurveyResponse writers are removed.
`rebuild_survey_projections` idempotently recomputes invitation/response totals,
average rating, and NPS from canonical invitation and response rows. Invitation
repair replays durable ticket/work-order events. Repair never sets a Survey
active from `is_active`, reconstructs a trigger from timestamps, or re-enables
the retired writer.
