#!/bin/sh

# Xcode Cloud post-clone hook for the dotmac_field TECHNICIAN app.
#
# Xcode Cloud clones the repo and runs `xcodebuild` on Runner.xcworkspace, but it
# has no knowledge of Flutter - the Flutter SDK, Generated.xcconfig, the Flutter
# framework, generated Dart (drift), and the CocoaPods/SwiftPM dependency graph
# are all absent, so the build fails. This script installs Flutter and generates
# those artifacts before Xcode Cloud's build step runs.
#
# Lives at field_mobile/ios/ci_scripts/ci_post_clone.sh - Xcode Cloud auto-runs
# any ci_scripts/ci_post_clone.sh adjacent to the Xcode project.
#
# THIS SCRIPT BUILDS field_mobile AND ONLY field_mobile. It previously pointed at
# the customer selfcare app's project directory, so the field app's Xcode Cloud
# workflow installed Flutter and then archived the wrong product. That directory
# has its own post-clone hook; the two must never reference each other, and the
# Field App Release preflight job asserts that statically.
#
# Everything below is a knob with a documented default (no hardcoded hosts,
# paths or versions):
#   CI_PRIMARY_REPOSITORY_PATH  repo checkout root      (Xcode Cloud sets this)
#   FIELD_APP_DIR               Flutter project dir     (default $REPO/field_mobile)
#   FIELD_BRAND_FILE            brand config            (default $FIELD_APP_DIR/brand.json)
#   FLUTTER_ROOT                reuse a pre-installed SDK instead of cloning
#   FLUTTER_REVISION            Flutter pin             (default: field_mobile/.metadata)
#   FLUTTER_GIT_URL             SDK source              (default flutter/flutter)
#   API_BASE_URL                backend                 (default: brand.json's value)
#   SENTRY_DSN                  crash reporting         (default: disabled)
#   GOOGLE_SERVICE_INFO_PLIST_B64  base64 plist         (default: push disabled)

set -e

REPO="${CI_PRIMARY_REPOSITORY_PATH:-$(cd "$(dirname "$0")/../../.." && pwd)}"
FIELD_APP_DIR="${FIELD_APP_DIR:-$REPO/field_mobile}"
FIELD_BRAND_FILE="${FIELD_BRAND_FILE:-$FIELD_APP_DIR/brand.json}"

# --- Fail early and clearly on a missing input ---------------------------------
if [ ! -f "$FIELD_APP_DIR/pubspec.yaml" ]; then
  echo "ERROR: no Flutter project at $FIELD_APP_DIR (expected pubspec.yaml)." >&2
  echo "       Set FIELD_APP_DIR, or check out the repository root." >&2
  exit 1
fi
if [ ! -f "$FIELD_BRAND_FILE" ]; then
  echo "ERROR: brand config not found at $FIELD_BRAND_FILE." >&2
  echo "       Without it the app silently falls back to Flutter's default theme," >&2
  echo "       so this is a hard failure rather than a warning. Commit" >&2
  echo "       field_mobile/brand.json or set FIELD_BRAND_FILE." >&2
  exit 1
fi

# --- Flutter SDK ---------------------------------------------------------------
# The pin is single-sourced from field_mobile/.metadata (version controlled,
# authoritative, and the file the Flutter tool itself maintains) - not a literal
# repeated across CI files. Xcode Cloud disables automatic SwiftPM resolution, so
# the regenerated plugin package graph must match what the committed lockfiles
# were produced with; a floating "stable" can pull newer plugin versions and
# break resolution. Nothing here names an individual plugin, so a dependency
# change (e.g. swapping the SQLite implementation) needs no edit to this script.
if [ -n "${FLUTTER_ROOT:-}" ] && [ -x "$FLUTTER_ROOT/bin/flutter" ]; then
  # A CI runner that already installed a pinned SDK (see the Field App Release
  # workflow) exports FLUTTER_ROOT so this script can be exercised end to end
  # without a 15-minute SDK clone. Xcode Cloud does not set it.
  echo "=== Using the pre-installed Flutter at $FLUTTER_ROOT ==="
  export PATH="$FLUTTER_ROOT/bin:$PATH"
else
  FLUTTER_REV="${FLUTTER_REVISION:-$(awk -F'"' '/^  revision: /{print $2; exit}' "$FIELD_APP_DIR/.metadata")}"
  if [ -z "$FLUTTER_REV" ]; then
    echo "ERROR: could not read the Flutter revision from $FIELD_APP_DIR/.metadata." >&2
    echo "       Set FLUTTER_REVISION explicitly to override." >&2
    exit 1
  fi
  echo "=== Installing Flutter (revision $FLUTTER_REV) ==="
  git clone "${FLUTTER_GIT_URL:-https://github.com/flutter/flutter.git}" "$HOME/flutter"
  (cd "$HOME/flutter" && git checkout -q "$FLUTTER_REV")
  export PATH="$HOME/flutter/bin:$PATH"
fi
flutter --version

echo "=== Preparing the Flutter iOS build for $FIELD_APP_DIR ==="
cd "$FIELD_APP_DIR"
flutter precache --ios
flutter pub get

# The app uses drift; generated *.g.dart must exist before the iOS build. The
# generated sources are committed, so this is a consistency regeneration rather
# than a bootstrap - it stays here so a stale checkout cannot ship mismatched
# generated code.
echo "=== Drift codegen ==="
dart run build_runner build --delete-conflicting-outputs

# Generates ios/Flutter/Generated.xcconfig, the App/Flutter frameworks, the
# plugin registrant, and resolves pods/SwiftPM - everything Xcode Cloud's
# xcodebuild needs.
#
# --dart-define-from-file supplies the FIELD app's brand config (colours, API
# base). Without it iOS builds got Flutter's default blue theme while Android
# got the brand green. A later --dart-define overrides the same key from the
# file, so an explicit API_BASE_URL still wins.
echo "=== Building (no codesign) with brand file $FIELD_BRAND_FILE ==="
set -- --dart-define-from-file="$FIELD_BRAND_FILE"
if [ -n "${API_BASE_URL:-}" ]; then
  set -- "$@" --dart-define=API_BASE_URL="$API_BASE_URL"
fi
if [ -n "${SENTRY_DSN:-}" ]; then
  set -- "$@" --dart-define=SENTRY_DSN="$SENTRY_DSN"
fi
flutter build ios --release --no-codesign "$@"

# --- Assert the right product was built ---------------------------------------
# Cheap structural guard against the exact defect this script used to have:
# building the customer app from the field app's CI hook. The bundle id in the
# produced .app is compared with the field app's Xcode project setting.
BUILT_PLIST="build/ios/iphoneos/Runner.app/Info.plist"
EXPECTED_BUNDLE_ID="${FIELD_BUNDLE_ID:-io.dotmac.field}"
if [ -f "$BUILT_PLIST" ]; then
  BUILT_BUNDLE_ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$BUILT_PLIST" 2>/dev/null || echo "")
  echo "Built bundle id: ${BUILT_BUNDLE_ID:-<unreadable>} (expected $EXPECTED_BUNDLE_ID)"
  if [ -n "$BUILT_BUNDLE_ID" ] && [ "$BUILT_BUNDLE_ID" != "$EXPECTED_BUNDLE_ID" ]; then
    echo "ERROR: this hook built '$BUILT_BUNDLE_ID', not the field app." >&2
    exit 1
  fi
else
  echo "WARNING: $BUILT_PLIST not found; skipping the bundle-id assertion." >&2
fi

# --- FCM push (operator-gated) -------------------------------------------------
# GoogleService-Info.plist and the push capability are per-deployment and kept
# out of the repo. When the operator supplies the plist as a base64 Xcode Cloud
# secret, materialize it, switch the entitlement to the production APNs
# environment (this is a distribution/TestFlight build), and wire it into the
# Runner target. Without the secret the app builds normally with push disabled
# (FcmPushSource.tryCreate() returns null -> NoopPushSource). Runs AFTER
# `flutter build` so Flutter's project regeneration cannot clobber it.
if [ -n "${GOOGLE_SERVICE_INFO_PLIST_B64:-}" ]; then
  echo "=== Wiring FCM push (GoogleService-Info.plist provided) ==="
  echo "$GOOGLE_SERVICE_INFO_PLIST_B64" | base64 --decode > ios/Runner/GoogleService-Info.plist
  /usr/libexec/PlistBuddy -c "Set :aps-environment production" ios/Runner/Runner.entitlements
  export GEM_HOME="$HOME/.gem"
  export PATH="$GEM_HOME/bin:$PATH"
  gem install xcodeproj --no-document
  ruby ios/ci_scripts/wire_firebase.rb
else
  echo "=== GOOGLE_SERVICE_INFO_PLIST_B64 not set - building with push disabled ==="
fi

echo "=== Flutter setup complete ==="
exit 0
