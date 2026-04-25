"""Compatibility facade for the legacy OLT authorization workflow module.

The authorization workflow now lives in ``app.services.network.ont_authorization``.
Keep this module importable for older tasks, config, and long-running workers
that still reference the previous path.
"""

from app.services.network.ont_authorization import (
    AuthorizationStepResult,
    AuthorizationWorkflowResult,
    authorize_autofind_ont,
    authorize_autofind_ont_and_provision_network_audited,
    create_or_find_ont_for_authorized_serial,
    ensure_assignment_and_pon_port_for_authorized_ont,
    get_autofind_candidate_by_serial,
    refresh_pool_availability,
    run_post_authorization_follow_up,
)

__all__ = [
    "AuthorizationStepResult",
    "AuthorizationWorkflowResult",
    "authorize_autofind_ont",
    "authorize_autofind_ont_and_provision_network_audited",
    "create_or_find_ont_for_authorized_serial",
    "ensure_assignment_and_pon_port_for_authorized_ont",
    "get_autofind_candidate_by_serial",
    "refresh_pool_availability",
    "run_post_authorization_follow_up",
]
