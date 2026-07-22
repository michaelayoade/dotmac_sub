# AI under the source-of-truth standard

Status: proposed, 2026-07-16.

## Finding

Sub has the AI **store** and none of the **generation**. `AIInsight`
(`app/models/ai_insight.py`) is a complete insight row — persona, domain,
severity, entity link, structured output, confidence, context quality, and
full LLM telemetry (provider/model/tokens/endpoint) — and
`ai_operations.create_insight` is its only writer. `AiIntakeConfig`
(`app/models/ai_intake.py`) carries a per-scope, per-channel gate
(`is_enabled`, `confidence_threshold`, `allow_followup_questions`,
`max_clarification_turns`, `escalate_after_minutes`).

Nothing produces insights. The admin API can create one by hand and a beat
expires stale ones; there is no gateway, no provider client, no personas, no
prompts. `app.services.ai_operations` is not a declared owner — it sits in
the undeclared-writer debt baseline, and AI appears nowhere in
`SOT_RELATIONSHIP_MAP.md`.

CRM holds the engine (~2,000 lines: gateway, client, redaction, security,
provider health, personas, use-cases, context builders) including
ISP-operational personas (`dispatch_planner`, `ticket_analyst`,
`inbox_analyst`, `project_advisor`, `vendor_analyst`, `performance_coach`)
alongside CRM-marketing ones (`campaign_optimizer`, `customer_success`).
CRM leaves the operation; the ISP personas must not leave with it.

## Decision — the ownership shape

AI is **advisory**. It observes, it derives, it recommends. It never decides
domain state. Four named owners, mapping onto the standard's
facts → derived → consequences separation:

1. **`ai.gateway` — transport, not a decision system.** Talks to the LLM
   provider, applies redaction and prompt-injection defences, records
   latency/token/provider-health telemetry. It is the same species as a
   payment gateway or an SMS provider: an external system Sub calls. It
   holds no business rule and owns no domain state. Provider credentials
   resolve through `secrets` (OpenBao), never settings rows.
2. **`ai.personas` — the resolver.** Each persona builds bounded context
   from the owning domain's read models and produces a *candidate* insight:
   a title, a summary, structured output, recommendations, a confidence.
   Personas read; they never write. A persona that needs data must ask the
   owning domain's read surface for it, not query across boundaries.
3. **`ai.insights` — the canonical writer** of derived AI state
   (`ai_operations`, already the sole writer of `AIInsight`). Owns insight
   lifecycle: create, acknowledge, expire. Every generated insight lands
   here and nowhere else.
4. **`ai.intake` — the policy gate.** `AiIntakeConfig` decides, per scope and
   channel, whether AI runs at all, what confidence clears the bar, and when
   to escalate to a human. This is the "should we act" decision, and it is
   AI's *only* decision.

## The consequence rule (the load-bearing invariant)

**An insight never mutates domain state.** Acting on a recommendation means
calling the domain's declared owner — `support.ticket_lifecycle`,
`operations.work_orders`, `operations.project_lifecycle`,
`communications.team_inbox` — which applies its own guards, events, and
audit. AI requests an outcome; the owner decides it.

Concretely: no module under `app/services/ai*` may construct or session-write
a non-AI ORM row. `tests/architecture/test_ai_boundaries.py` enforces this.
The failure this prevents is an LLM's suggestion silently becoming a
transition that bypassed its owner's rules — an unreviewable, untestable
authority leak, and the exact "parallel decision path" the standard forbids.

CRM's `action_insight` route is the pattern to **translate, not copy**: its
actions become owner calls.

## Port plan

Slices, landing together as one PR (feature slices, single review):

1. **Ownership skeleton** (this slice): the four owners declared in
   `sot_relationships` and `SOT_RELATIONSHIP_MAP.md`; `ai_operations`
   removed from the undeclared-writer debt baseline;
   `test_ai_boundaries.py` enforcing the consequence rule and the
   single-writer invariant.
2. **Transport**: port gateway/client/redaction/security/provider-health,
   re-pointed at Sub's `secrets` for credentials and Sub's settings for
   model/provider selection. Declared as a transport, with a kill switch.
3. **Personas (ISP only)**: `ticket_analyst`, `inbox_analyst`,
   `dispatch_planner`, `project_advisor`, `vendor_analyst`,
   `performance_coach`, re-pointed at Sub's read models.
   `campaign_optimizer` and `customer_success` stay behind with CRM.
4. **Generation path**: personas → `ai.insights.create_insight`, gated by
   `ai.intake`, behind a default-off control.
5. **Consequences**: insight actions routed through domain owners, with the
   architecture guard already in place from slice 1 proving the boundary.

## Non-goals

- Voice use-cases (transcription, field extraction) — evaluate separately;
  they are a different data-protection question.
- CRM-marketing personas — they leave with CRM.
- Any AI-initiated domain mutation. Not in this design, not later, without
  an explicit architecture decision replacing this document.
