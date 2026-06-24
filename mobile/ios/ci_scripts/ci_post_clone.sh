#!/bin/sh

# Xcode Cloud post-clone hook.
#
# Xcode Cloud clones the repo and then runs `xcodebuild` on Runner.xcworkspace,
# but it has no knowledge of Flutter — so the Flutter SDK, Generated.xcconfig,
# the Flutter framework, and the CocoaPods that the Runner target depends on are
# all absent, and the build fails. This script installs Flutter and generates
# those artifacts before Xcode Cloud's build step runs.
#
# Lives at mobile/ios/ci_scripts/ci_post_clone.sh — Xcode Cloud auto-runs any
# ci_scripts/ci_post_clone.sh adjacent to the Xcode project.

set -e

# Pin the Flutter version to the one that generated the committed SwiftPM
# Package.resolved. Xcode Cloud disables automatic package resolution, so the
# regenerated plugin package graph must match the lockfile exactly — a floating
# "stable" can pull newer plugin versions and break resolution ("dependencies
# were added: 'flutterfire'"). Bump this tag in lockstep with Package.resolved.
FLUTTER_VERSION="3.44.1"
echo "=== Installing Flutter ($FLUTTER_VERSION) ==="
git clone https://github.com/flutter/flutter.git --depth 1 -b "$FLUTTER_VERSION" "$HOME/flutter"
export PATH="$HOME/flutter/bin:$PATH"
flutter --version

echo "=== Preparing the Flutter iOS build ==="
# Xcode Cloud clones to $CI_PRIMARY_REPOSITORY_PATH; the Flutter project is mobile/.
cd "$CI_PRIMARY_REPOSITORY_PATH/mobile"

flutter precache --ios
flutter pub get

# Generates ios/Flutter/Generated.xcconfig, the App.framework / Flutter.framework,
# the plugin registrant, and runs `pod install` — everything Xcode Cloud's
# subsequent xcodebuild needs. API_BASE_URL defaults to production so Xcode Cloud
# builds (e.g. TestFlight) point at the live backend; override via an Xcode Cloud
# environment variable if needed.
#
# --dart-define-from-file=../brand.json injects the white-label brand config
# (BRAND_PRIMARY_COLOR, name, etc.) — the SAME source the Android release build
# and local `flutter run` use. Without it the app falls back to Flutter's default
# theme (blue) instead of the brand green. A later --dart-define overrides the
# same key from the file, so the explicit API_BASE_URL still wins.
flutter build ios --release --no-codesign \
  --dart-define-from-file=../brand.json \
  --dart-define=API_BASE_URL="${API_BASE_URL:-https://selfcare.dotmac.io}"

# --- FCM push (operator-gated) -------------------------------------------------
# GoogleService-Info.plist and the push capability are per-deployment and kept
# out of the repo. When the operator supplies the plist as a base64 Xcode Cloud
# secret, materialize it, switch the entitlement to the production APNs
# environment (this is a distribution/TestFlight build), and wire it into the
# Runner target. Without the secret the app builds normally with push disabled.
# Runs AFTER `flutter build` so Flutter's project regeneration can't clobber it.
if [ -n "$GOOGLE_SERVICE_INFO_PLIST_B64" ]; then
  echo "=== Wiring FCM push (GoogleService-Info.plist provided) ==="
  echo "$GOOGLE_SERVICE_INFO_PLIST_B64" | base64 --decode > ios/Runner/GoogleService-Info.plist
  /usr/libexec/PlistBuddy -c "Set :aps-environment production" ios/Runner/Runner.entitlements
  export GEM_HOME="$HOME/.gem"
  export PATH="$GEM_HOME/bin:$PATH"
  gem install xcodeproj --no-document
  ruby ios/ci_scripts/wire_firebase.rb
else
  echo "=== GOOGLE_SERVICE_INFO_PLIST_B64 not set — building with push disabled ==="
fi

echo "=== Flutter setup complete ==="
exit 0
