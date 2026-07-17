# Loyalty and data capture — one slice

Status: proposed, 2026-07-17.

## The problem this solves twice

Two open problems turn out to be the same build.

**Regulatory:** the NCC complaints return requires State/LGA/Town per row. Sub
holds none of it as a captured fact — 3,558 subscriber locations are
*inferred* from address text, 496 are absent, and **zero are confirmed by
anyone**. Rows fail the workbook's own validator until someone tells us where
they live.

**Commercial:** Sub knows how customers actually live with the product — when
they were last online, whether they are throttled, whether their ONT is
sick, whether they pay early, whether they refer friends — and does nothing
with any of it.

The link: **a customer will not log in to fix our data, but they will log in
for a reward.** The loyalty programme is the capture vehicle. One build; two
problems.

## Measured constraints — read these before designing anything

Every number here is from production, not assumption.

| Fact | Value | Consequence |
|---|---|---|
| `marketing_opt_in = true` | **0 of 15,291** | **A marketing campaign reaches NOBODY.** |
| Portal accounts | 3,983 of 4,054 active (98%) | The account exists. |
| Have *ever* logged in | **595 (15%)** | The account is unused. |
| Locations captured | **0** | Nothing is confirmed. |
| `last_seen_at`, RADIUS sessions | present | Dark-line detection needs no new data. |
| `usage`, `fup_usage`, `fup_state` | present | Throttle/underuse signals exist. |
| `Subscriber.created_at` | 15,291 | Tenure is free. |

**The zero opt-in is the design.** An anniversary offer sent as marketing
would be suppressed for every subscriber we have. Any design that mails a
reward is dead on arrival.

## Decision: the ask is transactional, the reward is in-product

`communications.eligibility` already owns this distinction, and states it
plainly: *"An unsubscribe is a refusal of marketing. It is not permission to
stop sending the invoice."*

So:

1. **The outbound message is TRANSACTIONAL** — "confirm your service address;
   we are required to report it accurately." That is a regulatory/service
   communication, not a promotion. It reaches customers regardless of
   marketing consent, because they never refused *this*.
2. **The reward is revealed IN THE PORTAL**, not in the message. The customer
   arrives to confirm their details and finds their anniversary reward
   waiting. It is in-product, not an outbound promotion, so it needs no
   marketing consent.
3. **The message must not sell.** If it advertises the reward, it becomes
   marketing in substance, and dressing a promotion as a service notice to
   evade consent is exactly the abuse consent rules exist to prevent. The ask
   stands on its own regulatory footing; the reward is a thank-you the
   customer discovers.

This is not a loophole hunt. It is the honest classification: we genuinely
must report accurate locations, and we genuinely owe long-tenured customers
something. Keeping them separate in the *channel* is what keeps both honest.

## Ownership (source-of-truth)

| Concern | Owner | Note |
|---|---|---|
| Who is due a milestone | **`loyalty.milestones`** (new) | DERIVES from `Subscriber.created_at` + payment + referrals. Stores no tier. **Proposes only.** |
| Whether to grant | **a human** | See "Grants are reviewed" below. The system never decides. |
| What was granted | **`loyalty.grants`** (new) | The only durable loyalty fact: *this customer was granted this, on this date, for this reason, **by this person**.* |
| Sending | `communications.intents` / `comms_campaigns` | Existing. Requests an outcome; does not decide eligibility. |
| Consent | `communications.eligibility` | Existing. **Sole** arbiter of transactional vs marketing. Never bypassed. |
| The captured location | `customer.data_completeness` + the verification ledger | Existing (PR #1423/#1430). Capture writes ledger rows; the reconciler adjudicates. |
| The reward itself | the owning domain | A bandwidth boost is the catalog/provisioning owner's decision. Loyalty **requests**, never provisions. |

**The invariant:** a loyalty tier is never stored. It is derived from tenure +
payment behaviour + advocacy, on read. The moment we persist "gold customer"
as a fact, we have built a second authority for something Sub already knows —
the mirror pattern, in a party hat. Only the **grant** is a fact, because a
grant is an event that happened.

## Grants are reviewed, never automatic (Michael, 2026-07-17)

`loyalty.milestones` produces a **review queue**, not a decision. A human
approves each grant; `loyalty.grants` records what that person approved and
who they were.

This is the same rule the rest of this codebase now runs on — the geocoder
proposes and a human confirms; a suggestion never auto-applies; an AI insight
advises and a person acts. A reward is money or capacity leaving the
business, so it is the *last* thing that should fire because a date rolled
over.

It also removes the worst failure mode by construction: no automatic grant
can be triggered by a bad `created_at`, a backfilled subscriber, or a
migration that touches tenure. The review queue is a projection; approving is
the command; the grant is the fact. Everything else about the design stays —
the derivation, the cooldown, and the good-standing gate become *inputs to
the reviewer's judgement* and filters on the queue, not gates on an
autonomous action.

## Configurability

Nothing about this is hardcoded. All of it resolves through the settings
registry, and the whole feature sits behind a default-OFF control.

**Control:** `loyalty.campaigns` — feature layer, `default=False`,
`on_missing=False`, canonical `modules.loyalty_campaigns`. Off means: no
milestone evaluation, no sends, no portal prompt. Inert.

**Sub-controls** (each default-OFF, independently flippable):
- `loyalty.capture_prompt` — the portal confirm-your-details prompt
- `loyalty.anniversary` — milestone detection + the transactional ask
- `loyalty.dark_line` — offline-customer detection

**Settings** (`SettingDomain.subscriber` unless noted):
- `loyalty_milestone_years` — which anniversaries count (default `[1, 2, 3, 5]`)
- `loyalty_reward_kind` / `loyalty_reward_value` / `loyalty_reward_duration_days`
- `loyalty_grant_cooldown_days` — no customer is rewarded twice inside this
- `loyalty_require_good_standing` — exclude customers in arrears (bool)
- `loyalty_capture_prompt_snooze_days` — "remind me later" duration
- `loyalty_capture_prompt_at_payment` — re-prompt at payment regardless of snooze (bool, default true)
- `dark_line_days` — offline for this long counts as dark (default 3)
- `dark_line_exclude_suspended` — do not chase customers we disconnected (bool, default true)

**No magic numbers in code.** Every threshold above is a setting with a
default and a rationale. The one exception, stated openly: the *list* of
milestone years has a default because an empty default means the feature
silently does nothing when switched on.

## The prompt

Shown when `customer.data_completeness` reports anything absent, inferred, or
stale — **confirm or correct**, not fill-blanks. Confirming an inferred value
is what converts it to a captured fact; that is the entire point of the
provenance ledger.

Dismissible ("remind me later" → snooze). **Re-prompted at payment
initiation regardless of snooze** — payment is the one flow the engaged 15%
reliably use, attention is already high, and "confirm your address while
you're here" reads as admin, not interrogation.

Snooze state is per-subscriber-per-prompt. It does **not** live in
`portal_onboarding_states` — that is a legacy Splynx import artifact (zero
rows, unwired, shaped as a step counter). Wrong home; it gets its own small
model.

## Dark-line detection

`last_seen_at` + `RadiusActiveSession` already exist. A customer dark for
`dark_line_days` who has not called is churning or suffering in silence.
Output is a **report projection** — which means it slots into the
`AdvisorSpec` shape shipped in #1427: the owner computes it, AI advises on
it, nobody re-derives.

Exclusions matter: do not chase customers we suspended ourselves, or whose
subscription is prepaid-expired. Chasing someone about an outage we caused by
disconnecting them is worse than silence.

## Slices

One PR, feature-sliced, everything default-OFF:

1. **`loyalty.milestones`** — derive who is due. Pure, read-only, no sends.
   Output is a **review queue projection**, not a decision.
2. **`loyalty.grants`** — the review surface + the grant ledger. A human
   approves; the ledger records what was approved and by whom. Cooldown and
   good-standing filter the queue rather than gate an autonomous action.
   Requests the reward from the owning domain; never provisions.
3. **The transactional ask** — through `communications.eligibility` as
   transactional. Tests must prove it is NOT classified as marketing, and
   that it reaches a subscriber with `marketing_opt_in = false`, because
   that is every subscriber we have.
4. **The portal prompt** — confirm-or-correct, snooze, payment re-prompt,
   writing through the capture path (#1430) so the reconciler adjudicates.
5. **Dark-line projection** + its advisor.

## Non-goals

- **Automatic grants.** A human approves every one (Michael, 2026-07-17).
- Storing a loyalty tier. Derive it.
- Marketing sends. Zero opt-ins; and this is not a promotion.
- Provisioning rewards inside loyalty. It requests; the catalog/provisioning
  owner decides and applies.
- Per-entity AI assistance. Separate lane (#1427's deferred second lane).

## Open questions for Michael

1. **What is the reward?** (Michael is thinking on this.) A speed boost costs
   capacity; a bill credit costs money and touches the ledger's single
   canonical writer. The design requests from the owning domain either way,
   but the choice changes who we integrate with. Manual review lowers the
   stakes of getting this wrong first time — a bad reward is refused at the
   queue, not discovered in the ledger.
2. **Does the anniversary ask need Legal's eye?** The classification is
   defensible — the location requirement is real and NDPR-relevant — but
   "transactional message, in-product reward" is a judgement, and it is worth
   one review before 15,291 people receive it.
3. **Good standing:** exclude customers in arrears from rewards? Arguable
   both ways — they are also the ones most worth retaining.
