# LangGraph AI Intake Pre-Activation Runbook

Status: pre-activation only. Do not enable `langgraph_v1` until the acceptance
gate below is green and an approved activation window is open.

## Scope

- Channel: WhatsApp.
- Business account: Dotmac Support.
- Current limitation: no safe contact-level canary isolation exists for this
  WhatsApp account.
- Production policy draft target: scoped Dotmac Support WhatsApp policy,
  inactive, `conversation_engine_mode=langgraph_v1`.
- Normal routing, FIFO queue, round-robin assignment, agent capacity, queue
  promotion, and human ownership remain Team Inbox owned.

## Pre-Activation Gate

All items must be true before activation:

- CI green on the selected source revision.
- LangGraph imports and `ai_intake_graph.langgraph_available()` verified on the
  intended production image.
- Automated/simulator scenario matrix A-X passes.
- Natural conversation and queue transition acceptance suite passes.
- Production policy draft validates while inactive.
- Rollback to `custom_v1` is verified and documented.
- Observability is confirmed for engine, policy, identity, monitoring, handoff,
  routing, assignment/queue, duplicate, takeover, and stuck-session signals.
- Queue/routing/assignment regressions are green.
- Media-first handoff, human takeover, and duplicate-message protection are
  green.

## Inactive Policy Draft

The intended draft configuration is represented in
`app.services.ai_intake_rollout_readiness.LANGGRAPH_POLICY_DRAFT` and must
remain inactive until the activation window.

Required draft properties:

- `status=draft`
- `is_active=false`
- `conversational_engine_enabled=true`
- `conversation_engine_mode=langgraph_v1`
- fallback engine `custom_v1`
- support-safe identity tool only
- support-safe monitoring projection only
- directory-wide customer lookup disabled
- media-first handoff when there is no usable customer text
- customer-facing wording uses Dotmac Support identity and avoids repeated AI,
  bot, or workflow-engine phrasing
- Team Inbox owns routing, FIFO queue, round-robin, agent capacity, and queue
  position
- monitoring `no_data` and `unavailable` remain distinct from `offline`

## Rollback

Exact mechanism:

1. Disable the scoped LangGraph AI Intake policy through the Team Inbox AI
   Intake policy owner, or publish/switch the same exact provider/account scope
   back to `conversation_engine_mode=custom_v1`.
2. Confirm new sessions request and actually run `custom_v1`.
3. Preserve session, generation-attempt, routing, delivery, and log evidence.
4. Do not reset queues or repair data as part of rollback.

Rollback must not require database repair, queue reset, worker restart where
avoidable, or loss of active human-owned conversations.

## Observability Required

Before activation, confirm non-PII logs/metrics exist for:

- requested engine
- actual engine
- graph execution
- policy version
- customer identity status
- monitoring result status
- tool failures
- handoff reason
- private note creation
- routing result
- assignment or queue result
- AI stopped after human ownership
- duplicate outbound detection
- stale queue notification suppression
- stuck sessions
- conversation quality score

## Natural Conversation Acceptance

The canary fails even if the workflow is technically correct when customer
messages are robotic, repetitive, or expose internal system terminology.

Score these dimensions for greeting-only, issue-first, rich first message,
correction, intent-change, frustrated-customer, human-request, queue, queue
position change, and agent-available scenarios:

- naturalness
- context awareness
- repetition
- robotic wording
- unnecessary questions
- ownership transition
- duplicate queue messaging

Customer-facing messages must not expose terms such as intent, confidence,
LangGraph, monitoring node, escalation rule, routing decision, queue worker, or
classification complete. LangGraph must not generate queue numbers, queue
position changes, heartbeat updates, or replies after human ownership.

## One-Time Controlled Activation Plan

Do not execute this plan until approved.

- Activation window: explicitly approved low-traffic support window.
- Responsible operator: named operator with `support:ticket:update`.
- Monitoring owner: named observer watching logs, sessions, outbound messages,
  routing, queue, and worker health.
- Rollback owner: named operator authorized to disable/switch the scoped policy.
- First acceptance scenarios: normal technical, rich first message, linked
  subscriber, no-data monitoring, explicit human request, media-only first
  message, agent available, all agents busy, human takeover, duplicate inbound.

Stop conditions:

- duplicate AI replies
- wrong customer identification
- privacy leakage
- incorrect monitoring claims
- wrong routing
- missing handoff note
- AI continuing after human takeover
- stale queue notices
- stuck sessions
- excessive errors or fallbacks

If any stop condition occurs:

1. Disable LangGraph for the scoped policy or switch it back to `custom_v1`.
2. Preserve evidence.
3. Mark rollout `HOLD`.
4. Do not expand.
