"""A published port is governed by its declared bind, in BOTH address families.

The incident: ``postgres-local`` published 9001 with a bare ``9001:5432``.
Docker started one docker-proxy on ``0.0.0.0`` and a second on ``[::]``. The v4
side was source-restricted to the replication standby by a ``DOCKER-USER``
rule; the v6 side was governed by nothing, because IPv6 published traffic is
served in userland and terminates on ``INPUT`` rather than traversing
``DOCKER-USER``. The port was reachable from the public internet for at least
41 days, and it appeared in no file -- grepping compose for ``0.0.0.0`` finds
nothing, because the exposure came from the ABSENCE of an address.

So the checks here are written to fail on exactly that shape, and -- just as
importantly -- to PASS on the corrected one. A gate that only ever refuses
proves nothing about what it admits, so every refusal below has an admission
planted beside it.

The sharpest test in this file is
``test_a_v4_only_comparison_would_have_admitted_the_real_defect``: it shows the
production listener set passing a v4-only comparison and failing the real one.
That is the property that makes this gate worth having.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from scripts import published_ports as pp

ROOT = Path(__file__).resolve().parents[1]

# The shape production actually had on 2026-08-31, read back from
# `docker inspect`. Not synthetic: this is the defect, as measured.
PRODUCTION_LISTENERS = [
    {
        "service": "postgres-local",
        "container": "dotmac_pg_local",
        "ports": {
            "5432/tcp": [
                {"HostIp": "0.0.0.0", "HostPort": "9001"},
                {"HostIp": "::", "HostPort": "9001"},
            ]
        },
    },
    {
        "service": "victoriametrics",
        "container": "dotmac_sub_victoriametrics",
        "ports": {"8428/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8428"}]},
    },
    {
        "service": "app",
        "container": "dotmac_sub_app",
        "ports": {"8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8001"}]},
    },
]


@pytest.fixture(scope="module")
def declaration() -> pp.Declaration:
    return pp.load_declaration()


def _postgres_only(listeners: list[dict]) -> list[dict]:
    return [row for row in listeners if row["service"] == "postgres-local"]


# --------------------------------------------------------------------------
# the mechanism: a bare publish is two listeners
# --------------------------------------------------------------------------


def test_a_bare_publish_is_dual_family() -> None:
    """The whole defect in one assertion."""
    assert pp.expected_listeners("") == {"0.0.0.0", "::"}


def test_an_explicit_ipv4_bind_publishes_one_listener() -> None:
    assert pp.expected_listeners("0.0.0.0:") == {"0.0.0.0"}
    assert pp.expected_listeners("127.0.0.1:") == {"127.0.0.1"}


def test_a_bracketed_ipv6_bind_is_understood() -> None:
    assert pp.expected_listeners("[::1]:") == {"::1"}


# --------------------------------------------------------------------------
# client admission -- the rule that stops a fix becoming an outage
# --------------------------------------------------------------------------


def test_a_loopback_bind_does_not_admit_the_replication_standby() -> None:
    assert not pp.bind_admits("127.0.0.1:", "75.119.157.91/32")


def test_a_wildcard_bind_admits_the_replication_standby() -> None:
    assert pp.bind_admits("0.0.0.0:", "75.119.157.91/32")


def test_a_loopback_bind_admits_a_loopback_client() -> None:
    assert pp.bind_admits("127.0.0.1:", "127.0.0.1/32")


def test_a_bind_does_not_admit_a_client_in_the_other_family() -> None:
    assert not pp.bind_admits("0.0.0.0:", "::1/128")
    assert not pp.bind_admits("[::]:", "75.119.157.91/32")


# --------------------------------------------------------------------------
# the declaration and the compose file agree
# --------------------------------------------------------------------------


def test_the_declaration_loads(declaration: pp.Declaration) -> None:
    assert declaration.publishes
    assert "production" in declaration.environments


def test_compose_matches_the_declaration(declaration: pp.Declaration) -> None:
    problems = pp.check_compose(declaration, pp.parse_compose_publishes())
    assert problems == [], problems


def test_no_compose_publish_is_bare() -> None:
    bare = [p.spec for p in pp.parse_compose_publishes() if p.is_bare]
    assert bare == [], (
        f"compose publishes {bare} with no host address. Docker will start a "
        "second listener on [::] that no DOCKER-USER rule can reach."
    )


def test_every_declared_bind_knob_is_documented_in_env_example(
    declaration: pp.Declaration,
) -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    missing = sorted(
        {p.bind_env for p in declaration.publishes if p.bind_env not in example}
    )
    assert missing == [], f"bind knobs absent from .env.example: {missing}"


def test_the_compose_gate_bites_on_a_bare_publish(
    declaration: pp.Declaration,
) -> None:
    """Sensitivity: the gate above passes -- prove it would not pass anything."""
    reverted = pp.parse_publish_spec("postgres-local", "9001:5432")
    assert reverted.is_bare
    problems = pp.check_compose(declaration, (reverted,))
    assert any("bare publish" in problem for problem in problems), problems


def test_the_compose_gate_bites_on_a_changed_default(
    declaration: pp.Declaration,
) -> None:
    widened = pp.parse_publish_spec(
        "postgres-local", "${PG_LOCAL_BIND:-0.0.0.0:}9001:5432"
    )
    problems = pp.check_compose(declaration, (widened,))
    assert any("default_bind" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# the listener gate -- both directions, on the real production shape
# --------------------------------------------------------------------------


def test_the_real_production_listener_set_is_refused(
    declaration: pp.Declaration,
) -> None:
    """The failing direction, using the shape production actually had."""
    problems = pp.check_listeners(
        declaration,
        "production",
        _postgres_only(PRODUCTION_LISTENERS),
        service="postgres-local",
    )
    assert any(
        "postgres-local:9001/tcp" in problem and "::" in problem for problem in problems
    ), problems


def test_the_corrected_listener_set_is_admitted(
    declaration: pp.Declaration,
) -> None:
    """The passing direction. Without this the refusal above proves nothing."""
    corrected = copy.deepcopy(_postgres_only(PRODUCTION_LISTENERS))
    corrected[0]["ports"]["5432/tcp"] = [{"HostIp": "0.0.0.0", "HostPort": "9001"}]
    problems = pp.check_listeners(
        declaration, "production", corrected, service="postgres-local"
    )
    assert problems == [], problems


def test_a_v4_only_comparison_would_have_admitted_the_real_defect(
    declaration: pp.Declaration,
) -> None:
    """This is why the gate reads both families.

    Strip the IPv6 listeners from the production shape -- which is precisely
    what a v4-only check sees -- and the very same input passes. The defect
    lived entirely in the half a v4-only check does not look at.
    """
    v4_only = copy.deepcopy(_postgres_only(PRODUCTION_LISTENERS))
    v4_only[0]["ports"]["5432/tcp"] = [
        binding
        for binding in v4_only[0]["ports"]["5432/tcp"]
        if ":" not in binding["HostIp"]
    ]
    assert (
        pp.check_listeners(declaration, "production", v4_only, service="postgres-local")
        == []
    )
    assert pp.check_listeners(
        declaration,
        "production",
        _postgres_only(PRODUCTION_LISTENERS),
        service="postgres-local",
    )


def test_an_admitted_service_and_a_refused_one_are_judged_in_one_pass(
    declaration: pp.Declaration,
) -> None:
    """victoriametrics is correct and postgres-local is not, in one input."""
    problems = pp.check_listeners(declaration, "production", PRODUCTION_LISTENERS)
    assert any("postgres-local" in problem for problem in problems)
    assert not any("victoriametrics" in problem for problem in problems)
    assert not any("dotmac_sub_app" in problem for problem in problems)


def test_a_missing_declared_listener_is_refused(
    declaration: pp.Declaration,
) -> None:
    """The narrowing direction: the standby losing its path must also fail."""
    narrowed = copy.deepcopy(_postgres_only(PRODUCTION_LISTENERS))
    narrowed[0]["ports"]["5432/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "9001"}]
    problems = pp.check_listeners(
        declaration, "production", narrowed, service="postgres-local"
    )
    assert any("127.0.0.1" in problem for problem in problems), problems
    assert any("is absent" in problem for problem in problems), problems


def test_an_undeclared_publisher_is_refused(declaration: pp.Declaration) -> None:
    stray = [
        {
            "service": "some-new-thing",
            "container": "dotmac_sub_some_new_thing",
            "ports": {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "15432"}]},
        }
    ]
    problems = pp.check_listeners(declaration, "production", stray)
    assert any("no declaration" in problem for problem in problems), problems


def test_an_exposed_but_unpublished_port_is_not_a_finding(
    declaration: pp.Declaration,
) -> None:
    """Sensitivity: `8001/tcp: null` is every worker. It must not be noise."""
    workers = [
        {
            "service": "celery-worker",
            "container": "dotmac_sub_celery_worker",
            "ports": {"8001/tcp": None},
        }
    ]
    assert not any(
        "celery-worker" in problem
        for problem in pp.check_listeners(declaration, "production", workers)
    )


def test_a_declared_service_that_is_not_running_is_refused(
    declaration: pp.Declaration,
) -> None:
    problems = pp.check_listeners(
        declaration, "production", [], service="postgres-local"
    )
    assert any("no running container" in problem for problem in problems), problems


def test_an_undeclared_environment_is_refused(declaration: pp.Declaration) -> None:
    """Fail closed: an unmeasured environment is not assumed to take defaults."""
    problems = pp.check_listeners(declaration, "somewhere-else", PRODUCTION_LISTENERS)
    assert any("not declared" in problem for problem in problems), problems


def test_normalise_inspect_keys_on_the_compose_label() -> None:
    normalised = pp.normalise_inspect(
        [
            {
                "Name": "/dotmac_pg_local",
                "Config": {"Labels": {"com.docker.compose.service": "postgres-local"}},
                "NetworkSettings": {
                    "Ports": {"5432/tcp": [{"HostIp": "::", "HostPort": "9001"}]}
                },
            }
        ]
    )
    assert normalised[0]["service"] == "postgres-local"
    assert normalised[0]["container"] == "dotmac_pg_local"


def test_a_container_with_no_compose_label_is_not_silently_skipped(
    declaration: pp.Declaration,
) -> None:
    normalised = pp.normalise_inspect(
        [
            {
                "Name": "/hand_started_thing",
                "Config": {"Labels": {}},
                "NetworkSettings": {
                    "Ports": {"5432/tcp": [{"HostIp": "::", "HostPort": "15432"}]}
                },
            }
        ]
    )
    problems = pp.check_listeners(declaration, "production", normalised)
    assert any("no declaration" in problem for problem in problems), problems


# --------------------------------------------------------------------------
# plan -- refusing a bind that would strand a live client
# --------------------------------------------------------------------------


def test_plan_produces_the_production_bind_for_postgres(
    declaration: pp.Declaration,
) -> None:
    result = pp.plan(declaration, "postgres-local", "production")
    assert result["assignments"] == {"PG_LOCAL_BIND": "0.0.0.0:"}
    assert result["targets"][0]["expected_listeners"] == ["0.0.0.0"]
    assert result["recreated_by_deploy"] is False


def test_plan_refuses_a_bind_that_strands_a_required_client() -> None:
    """A loopback bind on a port a live off-host client streams through.

    This is the failure that would turn the security fix into a replication
    outage, and it is refused before anything reaches a host.
    """
    stranding = pp.Declaration(
        environments=("production",),
        publishes=(
            pp.DeclaredPublish(
                service="postgres-local",
                host_port=9001,
                container_port=5432,
                protocol="tcp",
                bind_env="PG_LOCAL_BIND",
                default_bind="127.0.0.1:",
                reach="offhost",
                recreated_by_deploy=False,
                reason="standby streams through this port",
                required_clients=("75.119.157.91/32",),
            ),
        ),
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.plan(stranding, "postgres-local", "production")
    assert "does not admit required clients" in str(error.value)


def test_plan_refuses_an_undeclared_environment(
    declaration: pp.Declaration,
) -> None:
    with pytest.raises(pp.DeclarationError):
        pp.plan(declaration, "postgres-local", "somewhere-else")


def test_plan_refuses_an_unknown_service(declaration: pp.Declaration) -> None:
    with pytest.raises(pp.DeclarationError):
        pp.plan(declaration, "not-a-service", "production")


def test_plan_refuses_one_knob_declared_with_two_values() -> None:
    conflicting = pp.Declaration(
        environments=("production",),
        publishes=(
            pp.DeclaredPublish(
                service="freeradius",
                host_port=1812,
                container_port=1812,
                protocol="udp",
                bind_env="FREERADIUS_BIND",
                default_bind="0.0.0.0:",
                reach="offhost",
                recreated_by_deploy=False,
                reason="NAS devices",
                required_clients=("160.119.127.0/24",),
            ),
            pp.DeclaredPublish(
                service="freeradius",
                host_port=1813,
                container_port=1813,
                protocol="udp",
                bind_env="FREERADIUS_BIND",
                default_bind="127.0.0.1:",
                reach="loopback",
                recreated_by_deploy=False,
            ),
        ),
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.plan(conflicting, "freeradius", "production")
    assert "One knob, one value" in str(error.value)


# --------------------------------------------------------------------------
# the declaration refuses to declare an ungoverned shape
# --------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "published_ports.toml"
    path.write_text(body, encoding="utf-8")
    return path


BASE = """
schema_version = 1
declared_environments = ["production"]

[[publish]]
service = "thing"
host_port = 9999
container_port = 9999
protocol = "tcp"
bind_env = "THING_BIND"
default_bind = "{default_bind}"
reach = "{reach}"
recreated_by_deploy = false
{extra}
"""


def test_a_declaration_cannot_declare_an_empty_bind(tmp_path: Path) -> None:
    path = _write(tmp_path, BASE.format(default_bind="", reach="loopback", extra=""))
    with pytest.raises(pp.DeclarationError) as error:
        pp.load_declaration(path)
    assert "explicit address" in str(error.value)


def test_a_non_loopback_bind_must_be_declared_offhost(tmp_path: Path) -> None:
    path = _write(
        tmp_path, BASE.format(default_bind="0.0.0.0:", reach="loopback", extra="")
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.load_declaration(path)
    assert "must be declared offhost" in str(error.value)


def test_an_offhost_publish_must_name_required_clients(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BASE.format(
            default_bind="0.0.0.0:", reach="offhost", extra='reason = "because"'
        ),
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.load_declaration(path)
    assert "required_clients" in str(error.value)


def test_an_offhost_publish_must_carry_a_reason(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        BASE.format(
            default_bind="0.0.0.0:",
            reach="offhost",
            extra='required_clients = ["10.0.0.0/8"]',
        ),
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.load_declaration(path)
    assert "reason" in str(error.value)


def test_a_declaration_accepts_a_well_formed_offhost_publish(
    tmp_path: Path,
) -> None:
    """Sensitivity: the four refusals above must not be refusing everything."""
    path = _write(
        tmp_path,
        BASE.format(
            default_bind="0.0.0.0:",
            reach="offhost",
            extra='reason = "because"\nrequired_clients = ["10.0.0.0/8"]',
        ),
    )
    assert pp.load_declaration(path).publishes[0].bind_env == "THING_BIND"


def test_a_declaration_cannot_name_an_undeclared_environment(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path,
        BASE.format(
            default_bind="127.0.0.1:",
            reach="loopback",
            extra='environment_bind = { nowhere = "127.0.0.1:" }',
        ),
    )
    with pytest.raises(pp.DeclarationError) as error:
        pp.load_declaration(path)
    assert "undeclared environments" in str(error.value)
