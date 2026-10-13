"""Fail-closed Customer Android artifact verification; never reads key passwords."""

from __future__ import annotations

import argparse
import re
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

APPLICATION_ID = "io.dotmac.selfcare"


@dataclass(frozen=True)
class ReleaseArtifact:
    path: Path
    keystore: Path
    key_alias: str
    build_tools: Path


@dataclass(frozen=True)
class VerifiedRelease:
    application_id: str
    certificate_sha256: str


def _run(*args: str) -> str:
    # Tool diagnostics may contain key metadata; fail without forwarding them.
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError(f"{Path(args[0]).name} verification failed")
    return result.stdout


def _fingerprints(output: str, pattern: str) -> tuple[str, ...]:
    values = tuple(
        value.replace(":", "").lower() for value in re.findall(pattern, output)
    )
    if not values or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in values):
        raise ValueError("Missing or malformed signing certificate fingerprint")
    return values


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Malformed manifest protobuf varint")


def _messages(data: bytes, field: int) -> tuple[bytes, ...]:
    """Read length-delimited fields from Android Resources.proto messages."""
    offset = 0
    found: list[bytes] = []
    while offset < len(data):
        tag, offset = _varint(data, offset)
        wire = tag & 7
        if tag >> 3 == 0:
            raise ValueError("Invalid manifest protobuf field")
        if wire == 0:
            _, offset = _varint(data, offset)
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
        elif wire == 2:
            size, offset = _varint(data, offset)
            end = offset + size
            if tag >> 3 == field:
                found.append(data[offset:end])
            offset = end
        else:
            raise ValueError("Unsupported manifest protobuf wire type")
        if offset > len(data):
            raise ValueError("Truncated manifest protobuf")
    return tuple(found)


def bundle_application_id(manifest: bytes) -> str:
    # AOSP tools/aapt2/Resources.proto (XmlNode/XmlElement/XmlAttribute):
    # https://github.com/aosp-mirror/platform_frameworks_base/blob/master/tools/aapt2/Resources.proto
    # XmlNode.element=1; XmlElement.name=3, attribute=4;
    # XmlAttribute.namespace_uri=1, name=2, value=3 (Resources.proto).
    elements = _messages(manifest, 1)
    if len(elements) != 1 or _messages(elements[0], 3) != (b"manifest",):
        raise ValueError("Bundle root is not a manifest element")
    packages: list[str] = []
    for attribute in _messages(elements[0], 4):
        if _messages(attribute, 2) == (b"package",) and not any(
            _messages(attribute, 1)
        ):
            values = _messages(attribute, 3)
            if len(values) != 1:
                raise ValueError("Ambiguous manifest package attribute")
            packages.append(values[0].decode("utf-8"))
    if len(packages) != 1:
        raise ValueError("Missing or ambiguous manifest package")
    return packages[0]


def verify_release(artifact: ReleaseArtifact) -> VerifiedRelease:
    if not artifact.path.is_file():
        raise ValueError("Release artifact is missing")
    expected_output = _run(
        "keytool",
        "-J-Duser.language=en",
        "-list",
        "-v",
        "-keystore",
        str(artifact.keystore),
        "-alias",
        artifact.key_alias,
        "-storepass:env",
        "ANDROID_KEYSTORE_PASSWORD",
    )
    expected = _fingerprints(expected_output, r"SHA256:\s*([0-9A-Fa-f:]+)")
    if len(expected) != 1:
        raise ValueError("Upload key certificate is ambiguous")
    if artifact.path.suffix == ".aab":
        integrity = _run(
            "jarsigner", "-J-Duser.language=en", "-verify", str(artifact.path)
        )
        if "jar verified." not in integrity or "unsigned entries" in integrity:
            raise ValueError("Bundle is unsigned or contains unverified entries")
        certificate = _run(
            "keytool",
            "-J-Duser.language=en",
            "-printcert",
            "-jarfile",
            str(artifact.path),
        )
        actual = _fingerprints(certificate, r"SHA256:\s*([0-9A-Fa-f:]+)")
        with zipfile.ZipFile(artifact.path) as bundle:
            application_id = bundle_application_id(
                bundle.read("base/manifest/AndroidManifest.xml")
            )
    elif artifact.path.suffix == ".apk":
        certificate = _run(
            str(artifact.build_tools / "apksigner"),
            "verify",
            "--verbose",
            "--print-certs",
            str(artifact.path),
        )
        actual = _fingerprints(
            certificate, r"certificate SHA-256 digest:\s*([0-9A-Fa-f:]+)"
        )
        badging = _run(
            str(artifact.build_tools / "aapt2"), "dump", "badging", str(artifact.path)
        )
        packages = re.findall(r"^package: name='([^']+)'", badging, re.MULTILINE)
        if len(packages) != 1:
            raise ValueError("Missing or ambiguous APK application id")
        application_id = packages[0]
    else:
        raise ValueError("Unsupported release artifact type")
    if "CN=Android Debug" in certificate or "CN=Android Debug" in expected_output:
        raise ValueError("Android debug signing certificate refused")
    if actual != expected:
        raise ValueError("Artifact signer does not match the upload key")
    if application_id != APPLICATION_ID:
        raise ValueError(
            f"Expected application id {APPLICATION_ID}, got {application_id}"
        )
    return VerifiedRelease(application_id, actual[0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--keystore", type=Path, required=True)
    parser.add_argument("--key-alias", required=True)
    parser.add_argument("--build-tools", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify_release(
            ReleaseArtifact(
                args.artifact, args.keystore, args.key_alias, args.build_tools
            )
        )
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"::error::{error}\n")
    print(
        f"Verified {result.application_id}; certificate SHA-256: {result.certificate_sha256}"
    )


if __name__ == "__main__":
    main()
