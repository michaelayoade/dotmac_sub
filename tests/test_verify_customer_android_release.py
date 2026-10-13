"""Customer artifact verifier behavior, using tool output fixtures, never keys."""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from scripts.verify_customer_android_release import (
    ReleaseArtifact,
    bundle_application_id,
    verify_release,
)

FINGERPRINT = "ab" * 32


def _message(field: int, value: bytes) -> bytes:
    assert len(value) < 128
    return bytes([field * 8 + 2, len(value)]) + value


def _manifest(package: str) -> bytes:
    attribute = _message(2, b"package") + _message(3, package.encode())
    return _message(1, _message(3, b"manifest") + _message(4, attribute))


def test_bundle_package_is_exact_root_attribute() -> None:
    assert (
        bundle_application_id(_manifest("io.dotmac.selfcare")) == "io.dotmac.selfcare"
    )
    assert (
        bundle_application_id(_manifest("io.dotmac.selfcare.fake"))
        != "io.dotmac.selfcare"
    )
    with pytest.raises(ValueError):
        bundle_application_id(_message(1, _message(3, b"other")))
    with pytest.raises(ValueError):
        bundle_application_id(b"\x12\x7ftruncated")


@pytest.mark.parametrize("extension", ["apk", "aab"])
@pytest.mark.parametrize(
    "failure",
    ["none", "missing", "mismatch", "debug", "package", "unsigned", "tampered"],
)
def test_artifact_requires_valid_signer_and_exact_identity(
    extension: str, failure: str
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        artifact = root / f"app-release.{extension}"
        package = "io.dotmac.field" if failure == "package" else "io.dotmac.selfcare"
        with ZipFile(artifact, "w") as archive:
            archive.writestr("base/manifest/AndroidManifest.xml", _manifest(package))
        fingerprint = "cd" * 32 if failure == "mismatch" else FINGERPRINT
        subject = "CN=Android Debug" if failure == "debug" else "CN=Upload"
        signer = "" if failure in ("missing", "unsigned") else fingerprint

        def run(*args: str) -> str:
            if "-list" in args:
                return f"SHA256: {FINGERPRINT}"
            if "jarsigner" == args[0]:
                if failure == "tampered":
                    raise ValueError("jarsigner verification failed")
                return (
                    "jar verified. unsigned entries"
                    if failure == "unsigned"
                    else "jar verified."
                )
            if "-printcert" in args:
                return f"Owner: {subject}\nSHA256: {signer}"
            if "apksigner" in args[0]:
                if failure == "tampered":
                    raise ValueError("apksigner verification failed")
                return f"Signer #1 certificate DN: {subject}\nSigner #1 certificate SHA-256 digest: {signer}"
            return f"package: name='{package}'"

        with patch("scripts.verify_customer_android_release._run", side_effect=run):
            request = ReleaseArtifact(artifact, root / "upload.jks", "upload", root)
            if failure == "none":
                assert verify_release(request).certificate_sha256 == FINGERPRINT
            else:
                with pytest.raises(ValueError):
                    verify_release(request)


def test_missing_artifact_is_refused() -> None:
    with pytest.raises(ValueError, match="artifact is missing"):
        verify_release(
            ReleaseArtifact(
                Path("/nonexistent/app.aab"),
                Path("upload.jks"),
                "upload",
                Path("tools"),
            )
        )
