from __future__ import annotations

import os
import subprocess
from pathlib import Path

REVISION = "32eebc1a6ac05a21275ed4db6f3d1dd28514a045"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_deploy(
    tmp_path: Path,
    *,
    revision: str = REVISION,
    health_success: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    deploy_dir = tmp_path / "deploy"
    bin_dir = tmp_path / "bin"
    deploy_dir.mkdir()
    bin_dir.mkdir()
    (deploy_dir / ".env").write_text(
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000\n"
        "GIT_SHA=old0000000000000000000000000000000000000\n"
    )
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1 $2" == "image inspect" ]]; then
  printf '%s\\n' "{revision}"
fi
exit 0
""",
    )
    curl_exit_code = 0 if health_success else 1
    _write_executable(bin_dir / "curl", f"#!/usr/bin/env bash\nexit {curl_exit_code}\n")
    _write_executable(bin_dir / "pgrep", "#!/usr/bin/env bash\nexit 1\n")

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "REPO_DIR": str(repo_root),
        "DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        "SKIP_BACKUP": "1",
        "IMAGE_RETAIN_COUNT": "0",
        "HEALTH_TIMEOUT_SECONDS": "0" if not health_success else "180",
    }
    result = subprocess.run(
        ["bash", str(repo_root / "scripts/deploy.sh"), "sha-32eebc1"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, deploy_dir / ".env"


def test_deploy_pins_git_sha_from_image_revision(tmp_path: Path) -> None:
    result, env_file = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in env_text
    assert f"GIT_SHA={REVISION}" in env_text


def test_deploy_rejects_tag_revision_mismatch_without_changing_env(
    tmp_path: Path,
) -> None:
    result, env_file = _run_deploy(tmp_path, revision="f" * 40)

    assert result.returncode != 0
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text


def test_deploy_restores_image_and_git_sha_after_health_failure(tmp_path: Path) -> None:
    result, env_file = _run_deploy(tmp_path, health_success=False)

    assert result.returncode != 0
    assert "Health gate FAILED" in result.stdout
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text
