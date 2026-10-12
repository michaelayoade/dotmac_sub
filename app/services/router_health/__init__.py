"""Router health reconciler (``network.router_health``).

Import submodules directly (``reconciler``, ``commands``, ``coa_canary`` ...); this
package initialiser stays import-light so ``app.services.enforcement`` can import
``coa_evidence`` without a cycle.
"""
