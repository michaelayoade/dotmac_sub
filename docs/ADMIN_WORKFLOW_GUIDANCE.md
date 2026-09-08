# Admin workflow guidance

`app.services.admin_workflow_guidance` is the single source for plain-language
Admin help. It explains a workflow but never decides permissions, state, or
financial/service consequences; the route and named domain owner remain
authoritative.

Every staff-facing workflow guide declares its route prefix, audience, purpose,
steps, and safety notes. The Admin layout renders the matching guide in context
and `/admin/help` renders the searchable catalogue.

When an Admin route or workflow changes, update its linked guidance contract in
the same pull request. `scripts/architecture/workflow_guidance_gate.py` is run
by the required **Workflow Guidance Gate** CI job and fails a PR that changes
`app/web/admin` without changing the guidance registry.
