"""Keep Customer release gates ahead of builds and publication."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/mobile-release.yml"
SECRETS = (
    "ANDROID_KEYSTORE_BASE64",
    "ANDROID_KEYSTORE_PASSWORD",
    "ANDROID_KEY_ALIAS",
    "ANDROID_KEY_PASSWORD",
)


def _preflight() -> str:
    source = WORKFLOW.read_text()
    step = source.split("- name: Require all Customer signing secrets", 1)[1]
    return step.split("run: |\n", 1)[1].split("\n      - name:", 1)[0]


@pytest.mark.parametrize("missing", SECRETS)
def test_each_missing_secret_blocks_before_any_build(missing: str) -> None:
    environment = {
        key: value for key, value in os.environ.items() if key not in SECRETS
    }
    environment.update({name: "fixture-present" for name in SECRETS if name != missing})
    result = subprocess.run(
        ["bash", "-c", _preflight()],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert missing in result.stdout
    assert "fixture-present" not in result.stdout


def test_verification_gates_artifact_upload() -> None:
    source = WORKFLOW.read_text()
    assert source.index("Require all Customer signing secrets") < source.index(
        "Set up Java"
    )
    assert source.index("verify_customer_android_release.py") < source.index(
        "- name: Upload artifact"
    )
    assert "if-no-files-found: error" in source
    assert "if: always()" in source
    assert "app-release.aab" in source and "app-release.apk" in source


def test_only_verified_artifact_is_uploaded_with_pinned_tools() -> None:
    source = WORKFLOW.read_text()
    upload = source.split("- name: Upload artifact", 1)[1].split("- name:", 1)[0]
    assert "path: ${{ steps.verified-artifact.outputs.path }}" in upload
    assert "*.aab" not in upload and "*.apk" not in upload
    assert source.index("verify_customer_android_release.py") < source.index(
        '>> "$GITHUB_OUTPUT"'
    )
    assert 'CUSTOMER_ANDROID_BUILD_TOOLS_VERSION: "36.0.0"' in source
    assert source.index(
        '"$SDKMANAGER" "build-tools;$CUSTOMER_ANDROID_BUILD_TOOLS_VERSION"'
    ) < source.index("Install dependencies")
