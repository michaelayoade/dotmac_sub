"""Install reviewed SmartDNS candidates without changing DNS or kernel rules."""

from dataclasses import dataclass
from pathlib import Path

from deploy.agent.provision import (
    Asset,
    CapturedInstall,
    ProvisionError,
    Target,
    VerifiedArtifact,
    host_guard,
)
from deploy.egress.render import EgressConfiguration, render


@dataclass(frozen=True)
class ResolverPlan:
    target: Target
    binary: VerifiedArtifact
    trusted_manifest_signer: bytes
    configuration: EgressConfiguration
    # Exact reviewed upstream source/build evidence, not a claimed live TTL PASS.
    ttl_build_revision: str


def apply(plan: ResolverPlan) -> tuple[tuple[Asset, str], ...]:
    import hashlib

    host_guard(plan.target)
    if plan.configuration.enforce or plan.binary.asset is not Asset.RESOLVER:
        raise ProvisionError("initial_log_only_resolver_required")
    plan.binary.verify(plan.trusted_manifest_signer)
    if plan.ttl_build_revision != plan.binary.revision:
        raise ProvisionError("reviewed_ttl_build_required")
    transaction = CapturedInstall()
    rendered = render(plan.configuration)
    files = (
        (Asset.RESOLVER, plan.binary.content, 0o755),
        (Asset.DNS_CONFIG, rendered["smartdns.conf"].encode(), 0o644),
        (Asset.NFT_CONFIG, rendered["dotmac-egress.nft"].encode(), 0o644),
    )
    try:
        for asset, content, mode in files:
            transaction.install(Path(asset.value), content, mode=mode)
        return tuple(
            (asset, hashlib.sha256(Path(asset.value).read_bytes()).hexdigest())
            for asset, _, _ in files
        )
    except BaseException:
        transaction.rollback()
        raise
