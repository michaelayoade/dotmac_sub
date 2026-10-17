from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import bump_version


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_update_files_refreshes_dependency_manifest_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "VERSION_FILE", tmp_path / "VERSION")
    # Manifest regeneration is covered by its own test below.
    monkeypatch.setattr(bump_version, "refresh_product_manifest", lambda: None)
    monkeypatch.setattr(
        bump_version,
        "COMPOSITION_OBSERVATION_FILE",
        tmp_path / "docs/kernel-runtime-composition.json",
    )

    _write(tmp_path / "VERSION", "1.2.3\n")
    _write(tmp_path / "package.json", '{"version": "1.2.3"}\n')
    _write(
        tmp_path / "package-lock.json",
        '{"version": "1.2.3", "packages": {"": {"version": "1.2.3"}}}\n',
    )
    _write(tmp_path / "pyproject.toml", 'version = "1.2.3"\n')
    _write(tmp_path / "mobile/pubspec.yaml", "version: 1.2.3+7\n")
    _write(
        tmp_path / "mobile/lib/src/config/env.dart",
        "const version = String.fromEnvironment('VERSION', defaultValue: '1.2.3');\n",
    )
    _write(
        tmp_path / "mobile/lib/main.dart",
        "options.release = 'dotmac-mobile@1.2.3';\n",
    )
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n\n## 1.2.3 - 2026-01-01\n")
    _write(
        tmp_path / "docs/kernel-runtime-composition.json",
        json.dumps(
            {
                "schema_version": "dimensional-composition.v3",
                "observations": [
                    {
                        "id": "dependency-manifest",
                        "source_path": "pyproject.toml",
                        "source_blob_sha256": "stale-source",
                        "selector": "whole-file.v1",
                        "extract_sha256": "stale-extract",
                    },
                    {"id": "unrelated-observation", "evidence": "preserved"},
                ],
            },
            indent=2,
        )
        + "\n",
    )

    bump_version.update_files("1.2.4")

    expected_digest = hashlib.sha256(
        (tmp_path / "pyproject.toml").read_bytes()
    ).hexdigest()
    document = json.loads(
        (tmp_path / "docs/kernel-runtime-composition.json").read_text(encoding="utf-8")
    )
    dependency_manifest = document["observations"][0]
    assert dependency_manifest["source_blob_sha256"] == expected_digest
    assert dependency_manifest["extract_sha256"] == expected_digest
    assert document["observations"][1] == {
        "id": "unrelated-observation",
        "evidence": "preserved",
    }


def test_refresh_dependency_manifest_observation_fails_closed_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(
        bump_version,
        "COMPOSITION_OBSERVATION_FILE",
        tmp_path / "docs/kernel-runtime-composition.json",
    )
    _write(tmp_path / "pyproject.toml", 'version = "1.2.4"\n')
    _write(
        tmp_path / "docs/kernel-runtime-composition.json",
        '{"observations": []}\n',
    )

    with pytest.raises(RuntimeError, match="exactly one dependency-manifest"):
        bump_version.refresh_dependency_manifest_observation()


def test_refresh_product_manifest_regenerates_manifest_and_descriptor_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.product_manifest import verify_product_manifest

    version_file = tmp_path / "VERSION"
    manifest = tmp_path / "deploy/product-manifest.json"
    descriptor = tmp_path / "deploy/product.toml"
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "VERSION_FILE", version_file)
    monkeypatch.setattr(bump_version, "PRODUCT_MANIFEST_FILE", manifest)
    monkeypatch.setattr(bump_version, "PRODUCT_DESCRIPTOR_FILE", descriptor)
    _write(version_file, "1.2.4\n")
    _write(manifest, "stale\n")
    _write(
        descriptor,
        '[assembly]\nmanifest_path = "deploy/product-manifest.json"\n'
        'manifest_digest = "sha256:stale"\n',
    )

    bump_version.refresh_product_manifest()

    snapshot = verify_product_manifest(path=manifest, version_file=version_file)
    assert f'manifest_digest = "{snapshot.digest}"' in descriptor.read_text(
        encoding="utf-8"
    )
