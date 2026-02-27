#!/usr/bin/env bash
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"

# ---- Injected at spawn time ----
WORKTREE_DIR=/home/dotmac/projects/dotmac_sub/.worktrees/fix-deps-011
PROJECT_DIR=/home/dotmac/projects/dotmac_sub
SCRIPT_DIR=/home/dotmac/.seabone/scripts
ACTIVE_FILE=/home/dotmac/projects/dotmac_sub/.seabone/active-tasks.json
LOG_FILE=/home/dotmac/projects/dotmac_sub/.seabone/logs/fix-deps-011.log
TASK_ID=fix-deps-011
DESCRIPTION=Upgrade\ redis\ from\ 5.0.4\ to\ \>=5.2.0\ in\ pyproject.toml.\ Run\ make\ check\ \&\&\ make\ test\ after.
BRANCH=agent/fix-deps-011
ENGINE=aider
MODEL=deepseek-chat
EVENT_LOG=/home/dotmac/projects/dotmac_sub/.seabone/logs/events.log
CONFIG_FILE=/home/dotmac/projects/dotmac_sub/.seabone/config.json
PROJECT_NAME=dotmac_sub
PROMPTS_DIR=/home/dotmac/projects/dotmac_sub/.seabone/prompts

if [[ -f "$PROJECT_DIR/.env.agent-swarm" ]]; then
    set -a
    source "$PROJECT_DIR/.env.agent-swarm"
    set +a
fi
source "$SCRIPT_DIR/json-lock.sh"

log_event() {
    local event="$1" status="$2" detail="$3"
    local ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\n' "$(jq -n --arg ts "$ts" --arg project "$PROJECT_NAME" --arg task_id "$TASK_ID" --arg event "$event" --arg status "$status" --arg detail "$detail" '{ts:$ts,project:$project,task_id:$task_id,event:$event,status:$status,detail:$detail}')" >> "$EVENT_LOG"
}

set_status() {
    local status="$1"
    json_update "$ACTIVE_FILE" "(.[] | select(.id == \"$TASK_ID\") | .status) = \"$status\""
    json_update "$ACTIVE_FILE" "(.[] | select(.id == \"$TASK_ID\") | .last_heartbeat) = \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    log_event "status" "$status" "updated"
}

cd "$WORKTREE_DIR"

echo "=== Seabone Agent: $TASK_ID ==="
echo "Engine: $ENGINE"
echo "Model: $MODEL"
echo "Task: $DESCRIPTION"
echo "Branch: $BRANCH"
echo "Started: $(date)"
echo "================================"

# =========================================
#  ENGINE: Claude Code
# =========================================
if [[ "$ENGINE" == "claude" ]]; then
    echo "[RUN] Claude Code (headless)..."

    CLAUDE_ARGS=(
        -p "$DESCRIPTION"
        --dangerously-skip-permissions
        --output-format stream-json
        --model "$MODEL"
        --verbose
    )

    if [[ -f "$PROJECT_DIR/CLAUDE.md" ]]; then
        CLAUDE_ARGS+=(--append-system-prompt "$(cat "$PROJECT_DIR/CLAUDE.md")")
    fi

    claude "${CLAUDE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Claude Frontend Design Specialist
# =========================================
elif [[ "$ENGINE" == "claude-frontend" ]]; then
    echo "[RUN] Claude Frontend Design Specialist..."

    FRONTEND_PROMPT=""
    if [[ -f "$PROMPTS_DIR/frontend-design.md" ]]; then
        FRONTEND_PROMPT=$(cat "$PROMPTS_DIR/frontend-design.md")
    fi

    FULL_TASK="$FRONTEND_PROMPT

---

## Your Task

$DESCRIPTION

## Project Context
- Stack: Python 3.12, FastAPI, Jinja2 templates, Tailwind CSS, Alpine.js, HTMX
- Templates: app/templates/ (Jinja2 .html files)
- Static: app/static/css/, app/static/js/, app/static/img/
- Base template: app/templates/base.html (extend this)
- Use CDN for Tailwind, Alpine.js, HTMX unless local files exist already

## Requirements
- Create working, production-grade frontend code
- Every file must be complete and functional — no placeholders
- Follow existing project patterns for template structure
- Responsive design (mobile-first)
- Dark mode support
- Accessible (ARIA labels, semantic HTML)
- Distinctive design — no generic Bootstrap/AI-slop aesthetics"

    CLAUDE_ARGS=(
        -p "$FULL_TASK"
        --dangerously-skip-permissions
        --output-format stream-json
        --model "$MODEL"
        --max-turns 50
        --verbose
    )

    if [[ -f "$PROJECT_DIR/CLAUDE.md" ]]; then
        CLAUDE_ARGS+=(--append-system-prompt "$(cat "$PROJECT_DIR/CLAUDE.md")")
    fi

    claude "${CLAUDE_ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex
# =========================================
elif [[ "$ENGINE" == "codex" ]]; then
    echo "[RUN] Codex CLI (full-auto)..."

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$DESCRIPTION" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex Testing Specialist
# =========================================
elif [[ "$ENGINE" == "codex-test" ]]; then
    echo "[RUN] Codex Testing Specialist..."

    TEST_PROMPT=""
    if [[ -f "$PROMPTS_DIR/testing-agent.md" ]]; then
        TEST_PROMPT=$(cat "$PROMPTS_DIR/testing-agent.md")
    fi

    FULL_TASK="${TEST_PROMPT}

---

## Your Task

${DESCRIPTION}

## Project Context
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, PostgreSQL
- Test runner: pytest
- Test dir: tests/ (mirrors app/ structure)
- Fixtures: tests/conftest.py
- Run tests with: python -m pytest tests/ -v
- Do NOT import structlog — use stdlib logging only

## Requirements
- Write complete, runnable test files
- Run the tests after writing to verify they pass
- Fix any test failures before finishing
- Use pytest fixtures, not setUp/tearDown
- Use httpx.AsyncClient for API tests
- Mock external services, never call real APIs in tests"

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$FULL_TASK" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Codex Senior Dev (Escalation)
# =========================================
elif [[ "$ENGINE" == "codex-senior" ]]; then
    echo "[RUN] Codex Senior Dev (Escalation)..."

    SENIOR_PROMPT=""
    if [[ -f "$PROMPTS_DIR/senior-dev.md" ]]; then
        SENIOR_PROMPT=$(cat "$PROMPTS_DIR/senior-dev.md")
    fi

    PREV_LOG_CONTEXT=""
    BASE_TASK_ID=$(echo "$TASK_ID" | sed -E 's/-v[0-9]+$//')
    for prev_log in "$LOG_DIR/${BASE_TASK_ID}"*.log; do
        if [[ -f "$prev_log" && "$prev_log" != "$LOG_FILE" ]]; then
            PREV_LOG_CONTEXT="${PREV_LOG_CONTEXT}

--- Previous attempt log: $(basename "$prev_log") ---
$(tail -80 "$prev_log" 2>/dev/null || echo "(empty)")"
        fi
    done

    FULL_TASK="${SENIOR_PROMPT}

---

## Your Task

${DESCRIPTION}

## Project Context
- Stack: Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2, PostgreSQL, Redis
- Do NOT import structlog — use stdlib logging only
- Health/status endpoints are intentionally unauthenticated
- Services use serialize() methods that return dicts
- Schemas use Pydantic BaseModel with org_id as UUID

## Previous Attempts
${PREV_LOG_CONTEXT:-No previous attempts — this is a first escalation.}

## Requirements
- Read the previous agent's log above to understand what went wrong
- Read at least 5-10 relevant source files before making changes
- Fix the root cause, not just the symptom
- Run tests after fixing to verify
- If the task is fundamentally impossible, document why and exit cleanly"

    codex exec \
        --full-auto \
        --model "$MODEL" \
        "$FULL_TASK" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

# =========================================
#  ENGINE: Aider + DeepSeek
# =========================================
elif [[ "$ENGINE" == "aider" ]]; then
    echo "[RUN] Aider + DeepSeek..."

    export OPENAI_API_KEY="${DEEPSEEK_API_KEY}"
    export OPENAI_API_BASE="https://api.deepseek.com"

    cp "$PROJECT_DIR/.aider.model.settings.yml" "$WORKTREE_DIR/" 2>/dev/null || true
    cp "$PROJECT_DIR/.aider.model.metadata.json" "$WORKTREE_DIR/" 2>/dev/null || true
    cp "$PROJECT_DIR/.aider.conf.yml" "$WORKTREE_DIR/" 2>/dev/null || true
    cp "$PROJECT_DIR/.aiderignore" "$WORKTREE_DIR/" 2>/dev/null || true

    aider --model "openai/$MODEL" \
        --no-auto-commits \
        --yes-always \
        --no-show-model-warnings \
        --no-detect-urls \
        --subtree-only \
        --map-tokens 1024 \
        --model-settings-file "$WORKTREE_DIR/.aider.model.settings.yml" \
        --model-metadata-file "$WORKTREE_DIR/.aider.model.metadata.json" \
        --message "$DESCRIPTION" \
        2>&1 | tee -a "$LOG_FILE"
    AGENT_EXIT=${PIPESTATUS[0]}

    # Self-review loop for aider only
    if [[ $AGENT_EXIT -eq 0 ]]; then
        cd "$WORKTREE_DIR"
        if ! git diff --quiet || ! git diff --cached --quiet; then
            for cycle in 1 2; do
                echo ""
                echo "[REVIEW] Self-review cycle $cycle/2..."
                DIFF=$(git diff)
                [[ -z "$DIFF" ]] && DIFF=$(git diff --cached)
                [[ -z "$DIFF" ]] && break

                REVIEW=$(curl -s "https://api.deepseek.com/chat/completions" \
                    -H "Authorization: Bearer ${DEEPSEEK_API_KEY}" \
                    -H "Content-Type: application/json" \
                    -d "$(jq -n --arg p "Review this diff for bugs, missing imports, security issues. If correct respond LGTM. Otherwise describe fixes concisely.

DIFF:
$DIFF" '{model:"deepseek-chat", messages:[{role:"user",content:$p}], temperature:0.2, max_tokens:1500}')" \
                    | jq -r '.choices[0].message.content // "Review failed"')

                echo "Review: $REVIEW" | tee -a "$LOG_FILE"
                echo "$REVIEW" | grep -qi "LGTM" && break

                aider --model "openai/$MODEL" \
                    --no-auto-commits --yes-always --no-show-model-warnings --no-detect-urls \
                    --subtree-only --map-tokens 1024 \
                    --model-settings-file "$WORKTREE_DIR/.aider.model.settings.yml" \
                    --model-metadata-file "$WORKTREE_DIR/.aider.model.metadata.json" \
                    --message "Fix these issues: $REVIEW" \
                    2>&1 | tee -a "$LOG_FILE"
                cd "$WORKTREE_DIR"
            done
        fi
    fi
fi

# =========================================
#  Common: check result, commit, push, PR
# =========================================
if [[ ${AGENT_EXIT:-1} -ne 0 ]]; then
    set_status failed
    log_event "agent" "failed" "exit-${AGENT_EXIT}"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone*: \`$TASK_ID\` failed (${ENGINE}, exit ${AGENT_EXIT})." 2>/dev/null || true
    exit 1
fi

cd "$WORKTREE_DIR"
if git diff --quiet && git diff --cached --quiet && [[ -z "$(git ls-files --others --exclude-standard)" ]]; then
    set_status no_changes
    log_event "completion" "no_changes" "No diff produced"
    "$SCRIPT_DIR/notify-telegram.sh" "⚠️ *Seabone*: \`$TASK_ID\` no changes (${ENGINE})." 2>/dev/null || true
    exit 0
fi

echo ""
echo "[COMMIT] Staging and committing..."
git add -A
git commit -m "feat($TASK_ID): $DESCRIPTION

Automated by Seabone ($ENGINE + $MODEL)"

echo "[PUSH] Pushing to origin..."
git push -u origin "$BRANCH"

echo "[PR] Creating pull request..."
PR_URL=$(gh pr create \
    --title "[$TASK_ID] $DESCRIPTION" \
    --body "## Summary
Automated PR by Seabone agent swarm.

**Task:** $DESCRIPTION
**Engine:** \`$ENGINE\`
**Model:** \`$MODEL\`
**Branch:** \`$BRANCH\`

---
🤖 Seabone Agent Swarm" \
    --head "$BRANCH" 2>&1) || PR_URL="PR creation failed"

if [[ "$PR_URL" == "PR creation failed" ]]; then
    set_status failed
    log_event "completion" "failed" "PR creation failed"
    "$SCRIPT_DIR/notify-telegram.sh" "❌ *Seabone*: \`$TASK_ID\` PR creation failed." 2>/dev/null || true
    exit 1
fi

echo "[OK] PR created: $PR_URL"
set_status pr_created
log_event "completion" "pr_created" "$PR_URL"
"$SCRIPT_DIR/notify-telegram.sh" "✅ *Seabone*: \`$TASK_ID\` done ($ENGINE)
PR: $PR_URL" 2>/dev/null || true
exit 0
