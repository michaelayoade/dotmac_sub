from __future__ import annotations

from app.services.router_health.routeros_export import (
    MIN_PARSE_COVERAGE,
    normalize_identity,
    parse_export,
)


def test_header_version_and_identity() -> None:
    doc = parse_export(
        '# 2026-10-12 by RouterOS 7.22.2\n/system identity\nset name="Gudu Access"\n'
    )
    assert doc.version == "7.22.2"
    assert doc.version_tuple == (7, 22, 2)
    assert doc.identity == "Gudu Access"
    assert doc.parsed


def test_continuation_lines_are_joined() -> None:
    doc = parse_export(
        "/ip firewall filter\n"
        "add action=accept chain=input \\\n"
        '    comment="long rule" protocol=tcp\n'
        "/system identity\nset name=x\n"
    )
    (item,) = doc.section("/ip firewall filter")
    assert item.get("comment") == "long rule"
    assert item.get("protocol") == "tcp"
    assert item.line == 2


def test_quoted_values_escapes_and_equals_inside_quotes() -> None:
    doc = parse_export(
        '/ip firewall filter\nadd comment="say \\"hi\\" a=b" chain=input\n'
        "/system identity\nset name=x\n"
    )
    (item,) = doc.section("/ip firewall filter")
    assert item.get("comment") == 'say "hi" a=b'
    assert item.get("chain") == "input"


def test_selectors_and_positional_words() -> None:
    doc = parse_export(
        "/snmp community\nset [ find default=yes ] addresses=192.0.2.1/32\n"
        "/ip service\nset telnet disabled=yes\n"
        "/interface ethernet\nset [ find where name=ether1 ] comment=up\n"
        "/system identity\nset name=x\n"
    )
    (community,) = doc.section("/snmp community")
    assert community.selector == {"default": "yes"}
    (service,) = doc.section("/ip service")
    assert service.positional == ("telnet",)
    assert service.get("disabled") == "yes"
    (iface,) = doc.section("/interface ethernet")
    assert iface.selector == {"name": "ether1"}


def test_negated_values_are_kept_verbatim() -> None:
    doc = parse_export(
        "/ip firewall filter\nadd chain=forward dst-address-list=!splynx-allowed\n"
        "/system identity\nset name=x\n"
    )
    (item,) = doc.section("/ip firewall filter")
    assert item.get("dst-address-list") == "!splynx-allowed"


def test_dotted_continuation_keys_inherit_the_group_prefix() -> None:
    doc = parse_export(
        "/routing bgp connection\n"
        "add local.address=192.0.2.1 .role=ebgp name=p remote.address=192.0.2.2/32 .as=65000\n"
        "/system identity\nset name=x\n"
    )
    (item,) = doc.section("/routing bgp connection")
    assert item.get("local.role") == "ebgp"
    assert item.get("remote.as") == "65000"


def test_enabled_defaults_true_unless_disabled_yes() -> None:
    doc = parse_export(
        "/ip firewall filter\nadd chain=input\nadd chain=input disabled=yes\n"
        "/system identity\nset name=x\n"
    )
    first, second = doc.section("/ip firewall filter")
    assert first.enabled and not second.enabled


def test_trailing_space_identity_normalises() -> None:
    doc = parse_export('/system identity\nset name="Ilupeju Access "\n')
    assert doc.identity == "Ilupeju Access "
    assert normalize_identity(doc.identity or "") == normalize_identity(
        "ilupeju  ACCESS"
    )


def test_unterminated_quote_lowers_coverage_and_refuses_to_judge() -> None:
    lines = ["/ip firewall filter"] + ['add comment="oops chain=input'] * 5
    doc = parse_export("\n".join(lines) + "\n/system identity\nset name=x\n")
    assert doc.coverage < MIN_PARSE_COVERAGE
    assert not doc.parsed


def test_missing_identity_is_not_parsed() -> None:
    doc = parse_export("/ip service\nset telnet disabled=yes\n")
    assert doc.identity is None
    assert not doc.parsed


def test_user_section_is_not_required_and_is_ignored_by_checks() -> None:
    doc = parse_export("/user\nadd name=a group=full\n/system identity\nset name=x\n")
    assert doc.identity == "x"
