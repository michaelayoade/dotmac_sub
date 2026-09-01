"""Admit demonstrations for the port-reconciliation gates, over DECLARED targets.

A gate that enumerates real targets has to be shown admitting one of them. The
published-port observer was the case that made this concrete: it demanded an
immutable ``name@sha256:...`` reference from every container in the project,
which no declared target could satisfy, so it could never have admitted
anything it would actually be asked to admit -- and nothing noticed, because
acceptance had only ever been exercised against hand-built inputs and refusal
only against planted ones. Synthetic acceptance plus planted refusal is exactly
the pair that misses this.

So the demonstrations here are drawn from DECLARED state:

*   the target enumeration comes from each PLAN workflow's own ``options:``
    list, not from a copy of it kept in this file, so a target the workflow
    does not offer cannot be demonstrated and a target it gains cannot be
    quietly skipped;
*   the image under test is the one ``docker-compose.yml`` really declares --
    the release Compose file the PLAN lane observes.

`deploy/shadow/docker-compose.shadow.yml` is deliberately NOT a source. It does
contain digest-pinned images, including the exact PostGIS digest, but for a
stack whose service names the PLAN workflow does not offer. An admit drawn from
there would be green and would mean nothing; ``test_the_admit_demonstration_
never_draws_from_an_undeclared_stack`` keeps that honest.

WHAT IS DEMONSTRATED, AND WHAT IS OWED
--------------------------------------

The BOOTSTRAP lane's admissibility property -- "the target carries the exact
configured legacy tag" -- is satisfied by real declared state today, so its
real-target admit is demonstrated here, alongside a planted refusal.

The STEADY-STATE lane's immutable-image property is NOT yet demonstrable
against a declared target: both declared services are tag-pinned in
`docker-compose.yml`, which is the whole reason the bootstrap exists. Writing a
digest-pinned stand-in and calling it an admit would be precisely the shape
this file exists to refuse. What is written instead is the real-target
REFUSAL over the live enumeration -- the observation that would have caught the
original defect -- plus the planted refusal and planted admit that keep both of
the validator's branches exercised. The real-target admit for that lane is a
named obligation, recorded in `docs/adr/0015-legacy-image-pin-bootstrap.md`,
and `test_a_declared_target_that_becomes_digest_pinned_must_retire_this_gap`
fails the moment it becomes writable.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
import yaml

import scripts.legacy_image_pin_observer as bootstrap_observer
import scripts.published_port_reconcile_v2 as v2
from scripts.legacy_image_pin_contracts import LegacyImagePinBootstrapSnapshotV1

ROOT = Path(__file__).resolve().parents[2]
STEADY_PLAN_WORKFLOW = ROOT / ".github/workflows/infrastructure-reconcile-plan.yml"
BOOTSTRAP_PLAN_WORKFLOW = ROOT / ".github/workflows/legacy-image-pin-bootstrap-plan.yml"
RELEASE_COMPOSE = ROOT / "docker-compose.yml"
SHADOW_COMPOSE = ROOT / "deploy/shadow/docker-compose.shadow.yml"
DIGEST_PINNED_EXAMPLE = (
    "postgis/postgis@sha256:"
    "681931a625df344215e9b8998bf34daf146b6a395ceacee4439eb9c85869239f"
)


def _workflow_inputs(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML resolves a bare `on:` key to the boolean True.
    trigger = document[True] if True in document else document["on"]
    return trigger["workflow_dispatch"]["inputs"]


def declared_services(path: Path) -> tuple[str, ...]:
    """The services a PLAN workflow actually offers to be run against.

    Read from the workflow, never copied into this file: a hand-kept list can
    drift into naming a target the workflow does not offer, which is how an
    admit demonstration becomes meaningless without anyone editing it.
    """

    service = _workflow_inputs(path).get("service")
    if service is None:
        return ()
    options = service.get("options")
    if not options:
        raise AssertionError(f"{path.name} declares a service input with no options")
    return tuple(options)


def declared_image(service: str) -> str:
    compose = yaml.safe_load(RELEASE_COMPOSE.read_text(encoding="utf-8"))
    definition = compose["services"][service]
    image = definition.get("image")
    if not isinstance(image, str) or not image:
        raise AssertionError(f"{service} declares no image in the release Compose")
    return image


def _effective_document(service: str, image: str) -> dict[str, object]:
    """The real declared service definition, in the shape the validator reads."""

    compose = yaml.safe_load(RELEASE_COMPOSE.read_text(encoding="utf-8"))
    definition = dict(compose["services"][service])
    definition["image"] = image
    return {"services": {service: definition}}


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The enumeration must be live
# ---------------------------------------------------------------------------


def test_the_declared_target_enumeration_is_live_and_names_real_services() -> None:
    services = declared_services(STEADY_PLAN_WORKFLOW)
    assert services, "the steady-state PLAN workflow declares no targets"
    compose = yaml.safe_load(RELEASE_COMPOSE.read_text(encoding="utf-8"))
    for service in services:
        assert service in compose["services"], service
        assert declared_image(service)


def test_the_enumeration_helper_refuses_a_workflow_with_no_options(
    tmp_path: Path,
) -> None:
    """Sensitivity: an emptied enumeration must fail rather than pass vacuously."""

    document = yaml.safe_load(STEADY_PLAN_WORKFLOW.read_text(encoding="utf-8"))
    trigger = document[True] if True in document else document["on"]
    trigger["workflow_dispatch"]["inputs"]["service"]["options"] = []
    planted = tmp_path / "planted.yml"
    planted.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(AssertionError, match="no options"):
        declared_services(planted)


# ---------------------------------------------------------------------------
# Bootstrap lane: a real-target ADMIT, and a planted refusal
# ---------------------------------------------------------------------------


def _bootstrap_snapshot(*, image: str) -> dict[str, object]:
    running = "sha256:" + "6" * 64
    return {
        "schema": "LegacyImagePinBootstrapSnapshotV1",
        "target_server_name": "dotmac-sub-prod",
        "service": "postgres-local",
        "observer_digest": f"sha256:{'1' * 64}",
        "legacy_image_reference": image,
        "desired_image_reference": f"{image.rsplit(':', 1)[0]}@{running}",
        "resolution": {
            "schema": "LegacyImagePinLocalResolutionV1",
            "reference": f"{image.rsplit(':', 1)[0]}@{running}",
            "resolved_image_id": running,
            "running_image_id": running,
            "pulled": False,
        },
        "target_container_id": "a" * 64,
        "target_image_id": running,
        "listeners": [
            {
                "container_port": 5432,
                "host_ip": "0.0.0.0",
                "host_port": 9001,
                "protocol": "tcp",
            },
            {
                "container_port": 5432,
                "host_ip": "::",
                "host_port": 9001,
                "protocol": "tcp",
            },
        ],
        "non_port_projection": "DockerComposeServiceProjectionV1",
        "non_port_definition_digest": f"sha256:{'2' * 64}",
        "image_free_definition_digest": f"sha256:{'3' * 64}",
        "effective_image_reference": image,
        "deployed_compose_files": [
            {
                "path": "/root/dotmac_sub/docker-compose.override.yml",
                "digest": f"sha256:{'b' * 64}",
            },
            {
                "path": "/root/dotmac_sub/docker-compose.yml",
                "digest": f"sha256:{'a' * 64}",
            },
        ],
        "bind_knob": {
            "schema": "LegacyImagePinBindKnobProofV1",
            "env_key": "PG_LOCAL_BIND",
            "wildcard_injection": "0.0.0.0:",
            "wildcard_host_ip": "0.0.0.0",
            "control_injection": "127.0.0.1:",
            "control_host_ip": "127.0.0.1",
            "host_port": 9001,
            "container_port": 5432,
            "protocol": "tcp",
        },
        "non_targets": [],
    }


def test_the_bootstrap_gate_admits_its_real_declared_target() -> None:
    """The real-target ADMIT.

    The bootstrap PLAN workflow runs against a fixed service, and the image it
    must be able to admit is the one ``docker-compose.yml`` really declares for
    that service. This feeds exactly that string -- not a hand-written example
    -- through the real admissibility regex and the real snapshot contract.
    """

    assert declared_services(BOOTSTRAP_PLAN_WORKFLOW) == ()
    image = declared_image("postgres-local")

    # The observer's own admissibility predicate, on real declared state.
    assert bootstrap_observer.LEGACY_TAG.fullmatch(image), image

    snapshot = LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(
        bootstrap_observer._canonical(_bootstrap_snapshot(image=image))
    )
    assert snapshot.legacy_image_reference == image
    assert snapshot.effective_image_reference == image
    assert snapshot.service == "postgres-local"


def test_the_bootstrap_gate_refuses_a_planted_non_legacy_target() -> None:
    """Planted refusal, through the same validator that just admitted."""

    assert not bootstrap_observer.LEGACY_TAG.fullmatch(DIGEST_PINNED_EXAMPLE)
    with pytest.raises(Exception) as refusal:
        LegacyImagePinBootstrapSnapshotV1.from_canonical_bytes(
            bootstrap_observer._canonical(
                _bootstrap_snapshot(image=DIGEST_PINNED_EXAMPLE)
            )
        )
    assert "legacy_image_reference" in str(refusal.value)


# ---------------------------------------------------------------------------
# Steady-state lane: the real-target refusal, and both validator branches
# ---------------------------------------------------------------------------


def test_no_declared_steady_state_target_can_currently_be_admitted(
    tmp_path: Path,
) -> None:
    """The observation that would have caught the original defect.

    Every service the steady-state PLAN workflow offers is fed, as declared, to
    the real immutable-image validator. Every one is refused, because every one
    is tag-pinned. That is not a bug in this test: it is the fact the bootstrap
    exists to change, stated from declared state rather than inferred.
    """

    services = declared_services(STEADY_PLAN_WORKFLOW)
    assert services
    for index, service in enumerate(services):
        image = declared_image(service)
        document = _write(
            tmp_path / f"declared-{index}.json",
            _effective_document(service, image),
        )
        with pytest.raises(v2.ReconcileV2Error, match="immutable digest-pinned"):
            v2._effective_service_projection(document, service)


def test_a_declared_target_that_becomes_digest_pinned_must_retire_this_gap(
    tmp_path: Path,
) -> None:
    """A ratchet on the repository-observable half of the outstanding obligation.

    The real-target admit for the steady-state lane is owed, not delivered:
    no declared target can satisfy it yet.

    Be precise about what this ratchet can and cannot see. The bootstrap does
    not pin the release Compose file -- it applies a host-side overlay -- so
    after it executes the admissible target exists in the HOST's effective
    Compose, which CI cannot observe. This test therefore fires only on the
    half that IS repository-observable: a declared target becoming digest-
    pinned in `docker-compose.yml`. The host-side half is discharged by
    executed evidence from the maintenance window, and is tracked in
    `docs/adr/0015-legacy-image-pin-bootstrap.md` rather than here, because a
    check that cannot observe a condition must not claim to guard it.
    """

    pinned = [
        service
        for service in declared_services(STEADY_PLAN_WORKFLOW)
        if "@sha256:" in declared_image(service)
    ]
    assert not pinned, (
        f"{pinned} is now digest-pinned in the release Compose. The steady-state "
        "real-target admit is now writable: replace this ratchet with an admit "
        "demonstration over that service and retire the obligation recorded in "
        "docs/adr/0015-legacy-image-pin-bootstrap.md."
    )


def test_the_immutable_image_validator_has_both_branches_exercised(
    tmp_path: Path,
) -> None:
    """A refusal-only validator is indistinguishable from one that refuses all.

    Neither branch of this validator was exercised before. The planted admit is
    what proves the refusal above is discriminating rather than unconditional;
    the planted refusal is what proves the admit is not accidental.
    """

    service = declared_services(STEADY_PLAN_WORKFLOW)[0]

    admitted = _write(
        tmp_path / "planted-admit.json",
        _effective_document(service, DIGEST_PINNED_EXAMPLE),
    )
    digest, image = v2._effective_service_projection(admitted, service)
    assert image == DIGEST_PINNED_EXAMPLE
    assert digest.startswith("sha256:")

    refused = _write(
        tmp_path / "planted-refusal.json",
        _effective_document(service, "postgis/postgis:16-3.4-alpine"),
    )
    with pytest.raises(v2.ReconcileV2Error, match="immutable digest-pinned"):
        v2._effective_service_projection(refused, service)


# ---------------------------------------------------------------------------
# The admit may not be borrowed from a stack nobody can plan against
# ---------------------------------------------------------------------------


def test_the_admit_demonstration_never_draws_from_an_undeclared_stack() -> None:
    """The shadow stack has the right-looking images for the wrong reason.

    `deploy/shadow/docker-compose.shadow.yml` is digest-pinned throughout and
    even carries the exact PostGIS digest this bootstrap will adopt. It is also
    a stack the PLAN workflow does not offer, so an admit drawn from it would
    prove nothing about the gate. The service names must stay disjoint from the
    declared set, so the two cannot be confused.
    """

    if not SHADOW_COMPOSE.exists():
        pytest.skip("no shadow stack in this tree")
    shadow = yaml.safe_load(SHADOW_COMPOSE.read_text(encoding="utf-8"))
    shadow_services = set(shadow.get("services") or {})
    declared = set(declared_services(STEADY_PLAN_WORKFLOW))
    assert not (shadow_services & declared), shadow_services & declared
    # And the two helpers every demonstration draws its image through read the
    # RELEASE Compose only -- counting occurrences of a name would just track
    # this file's own prose.
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    helpers = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"declared_image", "_effective_document"}
    }
    assert set(helpers) == {"declared_image", "_effective_document"}
    for name, body in helpers.items():
        assert "RELEASE_COMPOSE" in body, name
        assert "SHADOW" not in body, name
