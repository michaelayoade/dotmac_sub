# Ticket Settings page contract

The settings editor at /admin/system/ticket-settings serves authorized support
administrators maintaining ticket taxonomy, routing, assignment and SLA policy.
The existing support ticket settings service owns the page context, validation
and settings commands. Assignment Rules use their existing dedicated commands.
The existing route guards and service audit behavior govern all writes.

The first viewport shows the page identity, section navigation and General
settings. Open Ticket Form is a secondary action. Save and Cancel appear in the
normal page flow only after a draft change, an inline ticket-type edit, or a
failed save that needs review. Save submits the full draft; Cancel reloads the
saved settings without submitting changes. Add, edit and remove operate on
settings rows. Assignment rules have their own create and confirmed delete
actions. Submitting an assignment action with an unsaved main draft prompts
before leaving the page. There are no bulk actions or exports.

General shows statuses, priorities and regions in their configured order.
Ticket Types shows the configured names in order with case-insensitive search
and an inline editor. When supplied by the route, infrastructure eligibility
and allowed targets are displayed and edited on the same row. SLA shows priority, response, resolution and aging hours.
The override list shows active overrides by default, with a search and a view
that includes inherited values. Routing and Assignment show the existing
routing and assignment fields. Staff and team choices come from the server.
Existing routing identifiers remain visible only to authorized administrators.

Section switches and filters change visibility. All named controls stay in the
form so off-screen draft edits are submitted. Empty lists and searches have
dedicated messages. After a failed save, the server returns the submitted draft
and selected section for correction; Cancel discards that draft. Saved routing
rules retain their site coordinator when other settings are saved. Validation
and success feedback come from the server.
Authorization failures use the existing route response. The page is
server-rendered with no separate loading or stale-data states. On narrow
screens, cards stack, controls wrap, and wide tables scroll locally.
