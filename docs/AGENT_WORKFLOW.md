# One Writer, Many Planners Workflow

## 1) Role Split (Hard Boundaries)

- `Sentinel`: detection only (incidents, CI failures, vulnerabilities, regressions). No spawning, no code edits.
- `Planner(s)`: analysis only. Output strict `TaskSpec` JSON.
- `Executor`: only agent allowed to edit code, commit, and open PRs.
- `Coordinator`: deterministic state machine runner, no freeform orchestration decisions.

## 2) Coordinator State Machine

`DETECTED -> TRIAGED -> TASKSPEC_READY -> QUEUED -> LEASED -> CODING -> VERIFYING -> PR_DRAFT -> PR_READY -> MERGED -> DEPLOYED -> OBSERVED`

Guardrails:

- Exactly one active coding lease when `single_executor_mode=true`.
- File scope lease must be acquired before execution.
- Retries create new attempt IDs (`-vN`) and preserve history.

## 3) TaskSpec Contract

Required fields:

- `id`
- `goal`
- `scope_files`
- `steps`
- `acceptance_checks`
- `risk`
- `engine_hint`

Optional planning/routing fields:

- `track`: `reactive|feature|journey|quality|coverage|refactor|integration|reliability|maintenance`
- `priority_score`: `0..100`
- `kpi_target`
- `depends_on` (task IDs)
- `estimated_effort` (`1..5`)
- `impact` (`1..5`)
- `risk_reduction` (`1..5`)
- `urgency` (`1..5`)

## 4) Deterministic Priority Scoring

If planner does not set `priority_score`, compute:

```text
priority_score =
((impact*35) + (risk_reduction*25) + (urgency*25) + ((6-estimated_effort)*15)) / 5
```

Then map to dispatch priority bands:

- `>=85` => priority `1` (urgent)
- `>=70` => priority `2` (high)
- `>=45` => priority `5` (normal)
- `<45` => priority `10` (low)

## 5) Queue Rules (Deterministic)

Dispatch order:

1. Higher `priority_score`
2. Lower numeric `priority`
3. Earlier `queued_at`

Admission rules before spawn:

1. Task dependencies in `depends_on` must be at least `pr_created` (`pr_created|merged|completed|no_changes`).
2. Track capacity rule must allow the task (config-driven percentages).
3. File/category scope must not conflict with active reservations.
4. Fleet lease must be acquirable.

Default track capacities:

- `feature: 50`
- `quality: 10`
- `coverage: 8`
- `refactor: 7`
- `reliability: 15`
- `integration: 10`
- `journey: 10`
- `maintenance: 5`
- `reactive: 100` (always allowed)

## 6) Versioning and Branching

- Trunk-based development on `main`.
- Executor uses short-lived branches from TaskSpec IDs.
- Conventional commits for all executor commits.
- SemVer tags cut from merged `main`.
- Release branches only for supported minors and hotfixes.

## 7) Testing Strategy

- Pre-commit: format, lint, typecheck, fast unit tests.
- PR CI: unit + changed-area integration + security scan + dependency audit.
- Merge CI: full integration + smoke e2e + migration checks.
- Post-deploy: canary checks + synthetic journey checks + rollback gates.

Local executor quality gates (diff-aware):

- Changed `*.sh|*.bash|*.zsh` => `shellcheck -x <changed files>`
- Changed `*.py` => `python -m pytest ...`
- Changed `*.js|*.jsx|*.ts|*.tsx|*.mjs|*.cjs` => `eslint`
- Changed `*.go` => `go vet ./...`

Gates are blocking for PR creation. Failing gates set task status to `quality_failed`.

Iterative review refinement (`aider`):

- After PR creation, DeepSeek reviews the PR diff.
- Review output is parsed for `actionable_issues`.
- If issues exist, Aider is re-invoked in the same worktree/branch with that feedback.
- Loop repeats up to `max_review_cycles` from `.seabone/config.json`.

## 8) CI/CD Strategy

- Build once, promote same artifact (`preview -> staging -> production`).
- Progressive rollout (`5% -> 25% -> 100%`) with automated rollback thresholds.
- Expand/contract DB migration pattern for safe deploys.
- Fixed merge windows to reduce conflict churn.

## 9) Operating Cadence

- Planner cycles continuously and batch related findings into fewer TaskSpecs.
- Executor implements batched TaskSpecs and opens draft PR at stable checkpoints.
- PR becomes ready when acceptance checks and CI are green.
- Merge in scheduled windows (1-2/day) to reduce conflicts.

## 10) Shared Context

- Shared file: `.seabone/shared-context.json`
- CLI: `scripts/shared-context.sh`
  - `summary`: show latest cross-agent findings
  - `add`: append a reusable finding (`source`, `kind`, `scope`, `note`, `confidence`)
- Executor prompts include shared-context summary before each task.
- Agents should append reusable discoveries during execution to reduce duplicated investigation.
