# Admin workflow guidance

`ui.admin_workflow_guidance` owns the read-only, deploy-versioned registry of
plain-language Admin workflow guidance.  It projects content to the protected
Help Center and supplies contextual explanations to Admin templates.

Workflow and domain owners continue to own facts, permissions, eligibility,
state transitions, and outcomes. Guidance never enables an action or changes
state. The Help Center route remains an adapter with its existing permission
guard.

The authoritative input is the typed `WORKFLOW_GUIDANCE` registry. It is fresh
at the deployed application revision; there is no cache or external fallback.
Drift is detected by `workflow_guidance_gate`, which requires guidance evidence
when an Admin workflow changes. Repair is a reviewed application change and
redeployment of the exact revision.
