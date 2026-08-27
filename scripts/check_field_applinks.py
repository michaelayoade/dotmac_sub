#!/usr/bin/env python3
"""Gate the field app's VERIFIED REDIRECT BOUNDARY (App Links / Universal Links).

Six artifacts have to agree, and none of them can see the others at build time:

  1. field_mobile/lib/core/deeplink/oidc_redirect.dart  the Dart defaults
  2. field_mobile/brand.json                            the build-time knobs
  3. field_mobile/android/app/src/main/AndroidManifest.xml   the intent filter
  4. field_mobile/ios/Runner/Runner.entitlements        the associated domain
  5. deploy/links.dotmac.io/site/.well-known/assetlinks.json
  6. deploy/links.dotmac.io/site/.well-known/apple-app-site-association

When they disagree the failure is SILENT and total: the link stops opening the
app and starts opening a browser, with no error anywhere. So this runs in CI.

Two identifiers in (5) and (6) are not knowable until the store records exist:
the Android PRODUCTION signing certificate SHA-256 (under Play App Signing that
certificate is Google's, NOT the local upload key's -- substituting the upload
key's fingerprint breaks verification silently) and the Apple Team ID. They are
committed as the placeholders __ANDROID_CERT_SHA256__ and __APPLE_TEAM_ID__.

  default mode   placeholders are ALLOWED and reported; every other property is
                 still proven, so a malformed real value fails the same way a
                 malformed placeholder would.
  --require-real placeholders are a HARD FAILURE. This is what runs before a
                 signed release build and before an association document is
                 published. A placeholder that can ship is worse than no file.

Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import plistlib
import re
import sys
import xml.etree.ElementTree as ET

ANDROID = "android"
ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

CERT_PLACEHOLDER = "__ANDROID_CERT_SHA256__"
TEAM_PLACEHOLDER = "__APPLE_TEAM_ID__"

# 32 colon-separated uppercase hex pairs, e.g. the output of
#   keytool -list -v -keystore ... | grep 'SHA256:'
FINGERPRINT_RE = re.compile(r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$")
# Apple Team IDs are 10 alphanumeric characters, conventionally uppercase.
TEAM_ID_RE = re.compile(r"^[A-Z0-9]{10}$")

APPLINK_RELATION = "delegate_permission/common.handle_all_urls"


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, message: str) -> bool:
        if not ok:
            self.failures.append(message)
        return ok

    def note(self, message: str) -> None:
        self.notes.append(message)


def read_json(report: Report, path: pathlib.Path):
    if not path.exists():
        report.check(False, f"missing: {path}")
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        report.check(False, f"{path}: is not valid JSON ({exc})")
        return None


def dart_default(report: Report, source: str, name: str, path: pathlib.Path) -> str | None:
    """Extract `const String <name> = String.fromEnvironment(..., defaultValue: X)`."""
    literal = re.search(
        rf"const String {name} = String\.fromEnvironment\(\s*'[A-Z0-9_]+',\s*"
        rf"defaultValue: ([A-Za-z0-9_]+),",
        source,
    )
    if not literal:
        report.check(False, f"{path}: no String.fromEnvironment default found for {name}")
        return None
    const_name = literal.group(1)
    value = re.search(rf"const String {const_name} = '([^']*)';", source)
    if not value:
        report.check(False, f"{path}: {name}'s default {const_name} has no string literal")
        return None
    return value.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-real",
        action="store_true",
        help="fail if an identity placeholder survives (release builds, publishes)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repository root (default: the parent of this script's directory)",
    )
    parser.add_argument(
        "--app-dir",
        default="field_mobile",
        help="Flutter project directory (default: field_mobile)",
    )
    parser.add_argument(
        "--origin-dir",
        default="deploy/links.dotmac.io",
        help="directory holding the association documents and serving spec",
    )
    args = parser.parse_args()

    root = pathlib.Path(args.repo_root or pathlib.Path(__file__).resolve().parent.parent)
    app = root / args.app_dir
    origin_dir = root / args.origin_dir
    site = origin_dir / "site"

    r = Report()

    # ------------------------------------------------------------------ Dart
    dart_path = app / "lib/core/deeplink/oidc_redirect.dart"
    if not dart_path.exists():
        r.check(False, f"missing: {dart_path}")
        return finish(r, args)
    dart = dart_path.read_text()
    origin = dart_default(r, dart, "oidcCallbackOrigin", dart_path)
    path_value = dart_default(r, dart, "oidcCallbackPath", dart_path)
    if origin is None or path_value is None:
        return finish(r, args)

    r.check(
        origin.startswith("https://"),
        f"{dart_path}: the callback origin {origin!r} is not https. A custom "
        f"scheme is claimable by any app on the device.",
    )
    r.check(
        "*" not in origin and "*" not in path_value,
        f"{dart_path}: the callback boundary must contain no wildcard "
        f"({origin!r}, {path_value!r})",
    )
    host = origin.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()
    r.check(bool(host), f"{dart_path}: could not read a host out of {origin!r}")
    r.check(
        path_value.startswith("/") and not path_value.endswith("/"),
        f"{dart_path}: the callback path {path_value!r} must be absolute and "
        f"must not end in a slash",
    )

    # ------------------------------------------------------------- brand.json
    brand_path = app / "brand.json"
    brand = read_json(r, brand_path)
    if brand is not None:
        r.check(
            brand.get("OIDC_CALLBACK_ORIGIN") == origin,
            f"{brand_path}: OIDC_CALLBACK_ORIGIN is "
            f"{brand.get('OIDC_CALLBACK_ORIGIN')!r}, but the Dart default is "
            f"{origin!r}",
        )
        r.check(
            brand.get("OIDC_CALLBACK_PATH") == path_value,
            f"{brand_path}: OIDC_CALLBACK_PATH is "
            f"{brand.get('OIDC_CALLBACK_PATH')!r}, but the Dart default is "
            f"{path_value!r}",
        )

    # ------------------------------------------------------ Android manifest
    manifest_path = app / "android/app/src/main/AndroidManifest.xml"
    package = check_android_manifest(r, manifest_path, host, path_value)

    # ----------------------------------------------------------- Gradle id
    gradle_path = app / "android/app/build.gradle.kts"
    if gradle_path.exists():
        m = re.search(r'applicationId\s*=\s*"([^"]+)"', gradle_path.read_text())
        r.check(m is not None, f"{gradle_path}: no applicationId found")
        if m and package:
            r.check(
                m.group(1) == package,
                f"assetlinks.json will be written for {package}, but the app is "
                f"built as {m.group(1)} ({gradle_path})",
            )
        package = package or (m.group(1) if m else None)

    # ------------------------------------------------------ iOS declarations
    bundle_id = check_ios(r, app, host)

    # ------------------------------------------- assetlinks.json (Android)
    assetlinks_path = site / ".well-known/assetlinks.json"
    check_assetlinks(r, assetlinks_path, package, args.require_real)

    # ------------------------------- apple-app-site-association (iOS)
    aasa_path = site / ".well-known/apple-app-site-association"
    check_aasa(r, aasa_path, bundle_id, path_value, args.require_real)

    # ------------------------------------------------------- serving spec
    check_serving_spec(r, origin_dir, host, path_value)

    return finish(r, args)


def check_android_manifest(
    r: Report, path: pathlib.Path, host: str, callback_path: str
) -> str | None:
    if not path.exists():
        r.check(False, f"missing: {path}")
        return None
    tree = ET.parse(path)
    root = tree.getroot()

    def attr(node, name):
        return node.get(ANDROID_NS + name)

    verified = []
    for intent_filter in root.iter("intent-filter"):
        for data in intent_filter.findall("data"):
            scheme = (attr(data, "scheme") or "").lower()
            if scheme in {"", "http", "https"} and (
                attr(data, "host") or attr(data, "pathPrefix") or attr(data, "path")
            ):
                verified.append((intent_filter, data))
            elif scheme not in {"", "http", "https"}:
                r.check(
                    False,
                    f"{path}: intent filter declares the custom scheme "
                    f"{scheme!r}. Custom schemes are claimable by any app on the "
                    f"device and are forbidden here.",
                )

    r.check(
        len(verified) == 1,
        f"{path}: expected exactly one https deep-link intent filter, found "
        f"{len(verified)}",
    )
    if len(verified) != 1:
        return None
    intent_filter, data = verified[0]

    r.check(
        attr(intent_filter, "autoVerify") == "true",
        f'{path}: the deep-link intent filter must set android:autoVerify="true"; '
        f"without it Android never fetches assetlinks.json and the link is "
        f"never verified",
    )
    r.check(
        (attr(data, "scheme") or "").lower() == "https",
        f"{path}: the deep-link intent filter must declare scheme https, found "
        f"{attr(data, 'scheme')!r}",
    )
    r.check(
        (attr(data, "host") or "").lower() == host,
        f"{path}: the intent filter host is {attr(data, 'host')!r}, but the "
        f"callback origin is {host!r}",
    )
    r.check(
        attr(data, "path") == callback_path,
        f"{path}: the intent filter must declare android:path={callback_path!r} "
        f"exactly, found {attr(data, 'path')!r}",
    )
    for widening in ("pathPrefix", "pathPattern", "pathAdvancedPattern", "pathSuffix"):
        r.check(
            attr(data, widening) is None,
            f"{path}: android:{widening} widens the redirect boundary to more "
            f"than one path. Use android:path.",
        )
    r.check(
        attr(data, "host") is not None and "*" not in (attr(data, "host") or ""),
        f"{path}: a wildcard host is not a verified redirect boundary",
    )

    actions = {attr(a, "name") for a in intent_filter.findall("action")}
    categories = {attr(c, "name") for c in intent_filter.findall("category")}
    r.check(
        "android.intent.action.VIEW" in actions,
        f"{path}: the deep-link intent filter is missing action VIEW",
    )
    for required in (
        "android.intent.category.DEFAULT",
        "android.intent.category.BROWSABLE",
    ):
        r.check(
            required in categories,
            f"{path}: the deep-link intent filter is missing category {required}",
        )

    # Flutter's engine only forwards the link into the Dart router when this is
    # set; without it the filter verifies and the link still never reaches the
    # coordinator.
    flags = {
        attr(m, "name"): attr(m, "value")
        for m in root.iter("meta-data")
    }
    r.check(
        flags.get("flutter_deeplinking_enabled") == "true",
        f'{path}: flutter_deeplinking_enabled must be "true" so a verified link '
        f"reaches the Dart router",
    )

    # The application id itself is single-sourced from Gradle; the manifest no
    # longer carries a package attribute.
    return None


def check_ios(r: Report, app: pathlib.Path, host: str) -> str | None:
    ent_path = app / "ios/Runner/Runner.entitlements"
    if not ent_path.exists():
        r.check(False, f"missing: {ent_path}")
        return None
    entitlements = plistlib.loads(ent_path.read_bytes())
    domains = entitlements.get("com.apple.developer.associated-domains")
    if not r.check(
        isinstance(domains, list) and domains,
        f"{ent_path}: no com.apple.developer.associated-domains entitlement; "
        f"without it iOS never fetches the association document",
    ):
        return None
    expected = f"applinks:{host}"
    r.check(
        domains == [expected],
        f"{ent_path}: associated domains are {domains!r}, expected exactly "
        f"[{expected!r}]",
    )
    for domain in domains:
        r.check(
            "*" not in domain,
            f"{ent_path}: {domain!r} is a wildcard associated domain",
        )

    # The entitlements file is inert unless the target actually signs with it.
    pbx_path = app / "ios/Runner.xcodeproj/project.pbxproj"
    pbx = pbx_path.read_text()
    ids = {i.strip() for i in re.findall(r"PRODUCT_BUNDLE_IDENTIFIER = ([^;]+);", pbx)}
    app_ids = {i for i in ids if not i.endswith("RunnerTests")}
    r.check(
        len(app_ids) == 1,
        f"{pbx_path}: expected one app bundle identifier, found {sorted(app_ids)}",
    )
    entitlement_configs = re.findall(r"CODE_SIGN_ENTITLEMENTS = ([^;]+);", pbx)
    r.check(
        len(entitlement_configs) >= 3
        and all(v.strip() == "Runner/Runner.entitlements" for v in entitlement_configs),
        f"{pbx_path}: every Runner build configuration must set "
        f"CODE_SIGN_ENTITLEMENTS = Runner/Runner.entitlements. Found "
        f"{entitlement_configs!r}. An entitlements file the target does not sign "
        f"with is inert, and Universal Links fail silently.",
    )

    info_path = app / "ios/Runner/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    r.check(
        "CFBundleURLTypes" not in info,
        f"{info_path}: CFBundleURLTypes declares a custom URL scheme. Custom "
        f"schemes are claimable by any app on the device and are forbidden here.",
    )
    r.check(
        info.get("FlutterDeepLinkingEnabled") is True,
        f"{info_path}: FlutterDeepLinkingEnabled must be true so a Universal "
        f"Link reaches the Dart router",
    )

    return next(iter(app_ids), None)


def check_assetlinks(
    r: Report, path: pathlib.Path, package: str | None, require_real: bool
) -> None:
    doc = read_json(r, path)
    if doc is None:
        return
    if not r.check(
        isinstance(doc, list) and len(doc) == 1,
        f"{path}: must be a JSON array holding exactly one statement, so exactly "
        f"one application identity is delegated",
    ):
        return
    statement = doc[0]
    r.check(
        statement.get("relation") == [APPLINK_RELATION],
        f"{path}: relation must be exactly [{APPLINK_RELATION!r}], found "
        f"{statement.get('relation')!r}",
    )
    target = statement.get("target") or {}
    r.check(
        target.get("namespace") == "android_app",
        f"{path}: target namespace must be 'android_app', found "
        f"{target.get('namespace')!r}",
    )
    declared_package = target.get("package_name")
    r.check(
        isinstance(declared_package, str) and "*" not in declared_package,
        f"{path}: package_name {declared_package!r} must be one exact package "
        f"with no wildcard",
    )
    if package:
        r.check(
            declared_package == package,
            f"{path}: package_name is {declared_package!r}, but the app is built "
            f"as {package!r}",
        )
    fingerprints = target.get("sha256_cert_fingerprints")
    if not r.check(
        isinstance(fingerprints, list) and len(fingerprints) == 1,
        f"{path}: sha256_cert_fingerprints must hold EXACTLY ONE fingerprint, "
        f"found {fingerprints!r}. Every accepted fingerprint authorizes that "
        f"signing identity to claim the link, so an unnecessary key (the local "
        f"upload key, a retired certificate) widens the trust surface. The "
        f"single entry is the PRODUCTION app-signing certificate. Adding a "
        f"second is a deliberate, reviewed edit to this gate.",
    ):
        return
    for fingerprint in fingerprints:
        if fingerprint == CERT_PLACEHOLDER:
            if require_real:
                r.check(
                    False,
                    f"{path}: the identity placeholder {CERT_PLACEHOLDER} is still "
                    f"present. Substitute the PRODUCTION signing certificate's "
                    f"SHA-256 (under Play App Signing that is Google's app-signing "
                    f"certificate from Play Console > Setup > App integrity, NOT "
                    f"the local upload key). A placeholder that ships breaks "
                    f"verification silently.",
                )
            else:
                r.note(
                    f"{path}: {CERT_PLACEHOLDER} still present (allowed outside a "
                    f"release build / publish; --require-real rejects it)"
                )
            continue
        r.check(
            bool(fingerprint) and isinstance(fingerprint, str),
            f"{path}: an empty fingerprint entry is not a signing identity",
        )
        r.check(
            bool(FINGERPRINT_RE.match(str(fingerprint))),
            f"{path}: {fingerprint!r} is not a SHA-256 certificate fingerprint "
            f"(expected 32 colon-separated UPPERCASE hex pairs). This catches a "
            f"real-but-malformed value, not just the placeholder.",
        )


def check_aasa(
    r: Report,
    path: pathlib.Path,
    bundle_id: str | None,
    callback_path: str,
    require_real: bool,
) -> None:
    if path.suffix:
        r.check(
            False,
            f"{path}: the Apple association document must be served with NO file "
            f"extension",
        )
    doc = read_json(r, path)
    if doc is None:
        return
    r.check(
        set(doc) == {"applinks"},
        f"{path}: must declare applinks and nothing else (no webcredentials, no "
        f"appclips), found {sorted(doc)}",
    )
    applinks = doc.get("applinks") or {}
    details = applinks.get("details")
    if not r.check(
        isinstance(details, list) and len(details) == 1,
        f"{path}: applinks.details must hold exactly one entry, so exactly one "
        f"application identity is delegated",
    ):
        return
    detail = details[0]
    app_ids = detail.get("appIDs")
    if not r.check(
        isinstance(app_ids, list) and len(app_ids) == 1,
        f"{path}: appIDs must hold exactly one identifier, found {app_ids!r}",
    ):
        return
    app_id = app_ids[0]
    r.check(
        "*" not in app_id,
        f"{path}: appID {app_id!r} must be one exact identity with no wildcard",
    )
    team, _, bundle = str(app_id).partition(".")
    if bundle_id:
        r.check(
            bundle == bundle_id,
            f"{path}: appID names bundle {bundle!r}, but the app is built as "
            f"{bundle_id!r}",
        )
    if team == TEAM_PLACEHOLDER:
        if require_real:
            r.check(
                False,
                f"{path}: the identity placeholder {TEAM_PLACEHOLDER} is still "
                f"present. Substitute the Apple Developer Team ID (Developer "
                f"portal > Membership details), and set the same value as "
                f"DEVELOPMENT_TEAM for the Runner target. A placeholder that "
                f"ships breaks verification silently.",
            )
        else:
            r.note(
                f"{path}: {TEAM_PLACEHOLDER} still present (allowed outside a "
                f"release build / publish; --require-real rejects it)"
            )
    else:
        r.check(
            bool(TEAM_ID_RE.match(team)),
            f"{path}: {team!r} is not an Apple Team ID (expected 10 alphanumeric "
            f"characters)",
        )

    components = detail.get("components")
    paths = detail.get("paths")
    r.check(
        components is not None or paths is not None,
        f"{path}: the entry declares neither components nor paths",
    )
    declared: list[str] = []
    if isinstance(components, list):
        for component in components:
            r.check(
                set(component) <= {"/", "?", "#", "comment", "exclude", "caseSensitive",
                                   "percentEncoded"},
                f"{path}: unexpected keys in a components entry: {sorted(component)}",
            )
            r.check(
                "?" not in component,
                f"{path}: a components entry matches on the query string. The "
                f"boundary is a PATH; query matching would make an authorization "
                f"response's own parameters part of the routing decision.",
            )
            if "/" in component:
                declared.append(component["/"])
    if isinstance(paths, list):
        declared.extend(paths)

    r.check(
        declared == [callback_path],
        f"{path}: declares paths {declared!r}, expected exactly "
        f"[{callback_path!r}]. Every extra path is another place an "
        f"authorization code may legally be delivered.",
    )
    for entry in declared:
        r.check(
            "*" not in entry,
            f"{path}: {entry!r} is a wildcard path",
        )


def check_serving_spec(
    r: Report, origin_dir: pathlib.Path, host: str, callback_path: str
) -> None:
    conf = origin_dir / f"{host}.conf"
    if not r.check(
        conf.exists(),
        f"missing serving spec: {conf}. The association documents are only real "
        f"if something is specified to serve them.",
    ):
        return
    text = conf.read_text()
    for required, why in (
        (
            "location = /.well-known/assetlinks.json",
            "an exact-match location, so nginx cannot redirect or normalise the "
            "request (both verifiers follow zero redirects)",
        ),
        (
            "location = /.well-known/apple-app-site-association",
            "an exact-match location for the Apple document",
        ),
        (
            f"location = {callback_path}",
            "an exact-match location for the callback path itself (the browser "
            "fallback)",
        ),
        (
            "default_type application/json",
            "an explicit JSON content type (the Apple file has no extension, so "
            "mime.types cannot infer one)",
        ),
        ("listen 443 ssl", "HTTPS with a real certificate"),
    ):
        r.check(required in text, f"{conf}: expected {required!r} -- {why}")

    # A redirect anywhere in the TLS server block would break verification.
    # Comments are stripped first: the spec's own prose explains why nginx must
    # not rewrite or redirect, and a naive scan reads that explanation as the
    # very defect it describes.
    tls_block = "\n".join(
        line
        for line in text.split("listen 443 ssl", 1)[-1].splitlines()
        if not line.lstrip().startswith("#")
    )
    r.check(
        not re.search(r"\breturn\s+30[1278]\b", tls_block)
        and not re.search(r"^\s*rewrite\s", tls_block, re.M),
        f"{conf}: the HTTPS server block contains a redirect. Android's and "
        f"Apple's verifiers follow zero redirects and read one as 'document "
        f"absent'.",
    )

    fallback = origin_dir / "site" / (callback_path.lstrip("/") + ".html")
    r.check(
        fallback.exists(),
        f"missing browser fallback page: {fallback}. Without it the callback URL "
        f"404s for anyone without a verified install.",
    )
    if fallback.exists():
        r.check(
            "<script" not in fallback.read_text().lower(),
            f"{fallback}: the fallback page must carry no script. It must never "
            f"read, forward or bounce an authorization code.",
        )


def finish(r: Report, args) -> int:
    for note in r.notes:
        print(f"::notice::{note}")
    if r.failures:
        for failure in r.failures:
            print(f"::error::{failure}")
        print(f"\nFAILED: {len(r.failures)} problem(s) with the verified redirect boundary.")
        return 1
    mode = "release/publish (placeholders rejected)" if args.require_real else "structural"
    print(f"Verified redirect boundary OK [{mode}].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
