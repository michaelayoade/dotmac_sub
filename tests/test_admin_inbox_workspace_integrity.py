"""Guards for the inbox workspace contract fixed in slices 1 and 2.

These are markup/wiring assertions rather than behaviour tests: each defect
they cover was a case where the owning service already did the right thing and
the workspace simply failed to call it, so the regression surface is the
template and the composer script, not the command boundary.

See docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md §5.
"""

from __future__ import annotations

from pathlib import Path

from app.services import team_inbox_projection

CONVERSATION = Path("templates/admin/inbox/_conversation.html").read_text()
DRAWER = Path("templates/admin/inbox/_contact_drawer.html").read_text()
SIDEBAR = Path("templates/admin/inbox/_sidebar.html").read_text()
JAVASCRIPT = Path("static/js/admin-inbox.js").read_text()
ROUTES = Path("app/web/admin/inbox.py").read_text()


# --- Slice 1: read state -------------------------------------------------


def test_thread_publishes_unread_state_and_composer_clears_it():
    """The workspace rendered unread state it never cleared server-side."""
    assert "data-conversation-unread=" in CONVERSATION
    assert 'thread.dataset.conversationUnread === "true"' in JAVASCRIPT
    assert "markConversationRead" in JAVASCRIPT
    assert "/read" in JAVASCRIPT


def test_mark_read_posts_with_csrf_header():
    marker = JAVASCRIPT.index("async markConversationRead")
    body = JAVASCRIPT[marker : marker + 500]
    assert '"X-CSRF-Token": csrfToken()' in body
    assert 'method: "POST"' in body


# --- Slice 1: reply provenance ------------------------------------------


def test_reply_form_submits_macro_and_template_identity():
    """reply() accepts macro_id/template_id; the composer must send them.

    Without template_id an outbound WhatsApp send carries no approved
    provider-template identity.
    """
    assert 'name="macro_id"' in CONVERSATION
    assert 'name="template_id"' in CONVERSATION
    assert "resolvedMacroId()" in CONVERSATION
    assert "resolvedTemplateId()" in CONVERSATION


def test_macro_menu_dispatches_identity_not_just_text():
    assert '"macroId"' in CONVERSATION


def test_identity_is_released_when_the_agent_edits_the_body():
    """reply() substitutes the template body server-side, so a stale identity
    claim would discard the agent's edits and write a false audit record."""
    for fn in ("resolvedMacroId()", "resolvedTemplateId()"):
        marker = JAVASCRIPT.index(fn.rstrip("()") + "(")
        body = JAVASCRIPT[marker : marker + 160]
        assert "this.draft === this.identityBody" in body

    assert "releaseIdentity()" in JAVASCRIPT
    assert "claimIdentity(" in JAVASCRIPT


def test_ad_hoc_insertions_do_not_claim_identity():
    marker = JAVASCRIPT.index("insertQuickResponse(payload)")
    body = JAVASCRIPT[marker : marker + 700]
    assert "this.releaseIdentity()" in body


# --- Response cohorts ---------------------------------------------------


def test_assignment_counts_expose_distinct_response_cohorts():
    fields = team_inbox_projection.InboxAssignmentCounts.__dataclass_fields__
    assert "unreplied" in fields
    assert "needs_attention" in fields


def test_needs_attention_has_its_own_filter_token_and_count():
    assert "assignment_counts.needs_attention" in SIDEBAR
    assert "applyAssignmentFilter('attention')" in SIDEBAR
    assert 'needs_attention: "true"' in JAVASCRIPT
    assert 'filters.get("needs_attention") === "true"' in JAVASCRIPT


# --- Slice 2: collaboration ---------------------------------------------


def test_drawer_exposes_comment_thread_and_resolution():
    assert "/comments" in DRAWER
    assert "/resolve" in DRAWER
    assert "timeline.comments" in DRAWER


def test_composer_can_save_draft_as_macro_or_template():
    assert "/macros/create" in CONVERSATION
    assert "/templates/create" in CONVERSATION


# --- Slice 2: customer context sensitivity -------------------------------


def test_financial_and_network_detail_are_permission_gated():
    """Arrears and session IP are more sensitive than the conversation."""
    assert "can_view_financials" in DRAWER
    assert "can_view_network_detail" in DRAWER

    routes = Path("app/web/admin/inbox.py").read_text()
    assert 'can(request, "billing:account:read")' in routes
    assert 'can(request, "network:ip:read")' in routes


def test_arrears_hidden_without_billing_access_shows_reason():
    assert "Billing detail hidden" in DRAWER


def test_ip_is_gated_independently_of_connection_status():
    """Offline/last-seen answers the support question; the IP is separate."""
    # Anchor on the connection panel, not the identity badge that also reads
    # `subscriber_summary.connection` higher up the drawer.
    marker = DRAWER.index("{% set connection = subscriber_summary.connection %}")
    block = DRAWER[marker : marker + 1400]
    assert "Offline" in block
    assert "connection.last_seen_at" in block
    assert "can_view_network_detail and connection.ip" in block


# --- Stats and filters refactor -----------------------------------------


def test_stats_filter_header_uses_the_page_scoped_amber_contract():
    for class_name in (
        "border-amber-200",
        "bg-amber-50",
        "text-amber-800",
        "hover:border-amber-300",
        "hover:bg-amber-100",
        "dark:border-amber-500/30",
        "dark:bg-amber-500/10",
        "dark:hover:border-amber-400/50",
        "dark:hover:bg-amber-500/20",
    ):
        assert class_name in SIDEBAR


def test_status_and_assignment_filters_use_flexible_wrapping_groups():
    group_classes = (
        "flex flex-wrap gap-1 rounded-xl bg-slate-100 p-1 dark:bg-slate-800/50"
    )
    assert SIDEBAR.count(group_classes) == 2
    assert 'name="status" value="open"' in SIDEBAR
    assert 'name="status" value="pending"' in SIDEBAR
    assert 'name="has_ticket" value="true"' in SIDEBAR
    assert 'name="status" value="resolved"' in SIDEBAR
    assert "bg-emerald-500" in SIDEBAR
    assert "bg-amber-500" in SIDEBAR


def test_assignment_filter_colours_and_counts_are_present():
    for label in (
        "Assigned to me",
        "My Team",
        "AI handling",
        "Unassigned",
        "Unreplied",
        "Needs attention",
        "By Agent",
    ):
        assert label in SIDEBAR
    for selected_colour in (
        "text-emerald-700",
        "text-cyan-700",
        "text-indigo-700",
        "text-amber-700",
        "text-rose-700",
        "text-orange-700",
        "text-violet-700",
    ):
        assert selected_colour in SIDEBAR
    assert "assignmentFilterActive" in SIDEBAR
    assert "assignmentFilterActive(value)" in JAVASCRIPT


def test_needs_attention_is_a_live_saved_filter():
    assert "showDemoNotice('Needs-attention filtering')" not in SIDEBAR
    assert 'needs_attention: "needs_attention"' in JAVASCRIPT
    assert "needs_attention: bool = Query(default=False)" in ROUTES
    assert "needs_attention: bool = Form(default=False)" in ROUTES


def test_channel_and_team_remain_separate_and_inbox_selector_stays_hidden():
    assert 'name="channel_type"' in SIDEBAR
    assert 'name="service_team_id"' in SIDEBAR
    assert ">Channel</span>" in SIDEBAR
    assert ">Team</span>" in SIDEBAR
    assert 'name="receiving_account_id"' not in SIDEBAR
    assert 'name="mailbox_id"' not in SIDEBAR


def test_by_agent_panel_uses_live_agent_and_activity_filters():
    assert 'id="inbox-by-agent-panel"' in SIDEBAR
    assert 'name="assigned_person_id"' in SIDEBAR
    assert 'name="activity_from"' in SIDEBAR
    assert 'name="activity_to"' in SIDEBAR
    assert "border-slate-200/60 bg-slate-50" in SIDEBAR
    assert "focus:border-violet-500" in SIDEBAR


def test_saved_views_have_active_state_and_save_all_live_filter_fields():
    assert "savedViewIsActive" in SIDEBAR
    assert "border-amber-300 bg-amber-50" in SIDEBAR
    assert "border-slate-200 bg-white" in SIDEBAR
    assert "bg-amber-600" in SIDEBAR
    assert "hover:text-red-500" in SIDEBAR
    for field_name in (
        "service_team_ids",
        "assigned_person_id",
        "needs_attention",
        "unread",
        "ai_handling",
        "has_ticket",
        "activity_from",
        "activity_to",
    ):
        assert f'{field_name}: "{field_name}"' in JAVASCRIPT
        assert f"{field_name}: " in ROUTES
