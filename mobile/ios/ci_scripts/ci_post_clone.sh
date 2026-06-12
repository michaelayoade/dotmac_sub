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
flutter build ios --release --no-codesign \
  --dart-define=API_BASE_URL="${API_BASE_URL:-https://selfcare.dotmac.io}"

echo "=== Flutter setup complete ==="
exit 0
