#!/usr/bin/env bash
#
# Run the Flutter apps on a local iOS simulator or Android emulator.
#
# Usage:
#   scripts/mobile_sim.sh <mobile|field_mobile> <ios|android> [test|run] [flutter args...]
#
#   test (default)  flutter test integration_test/ on the device
#   run             flutter run on the device (hot reload)
#
# Environment:
#   API_BASE_URL     backend to target (default: the app's in-code default host)
#   LOCAL_BACKEND=1  target the local backend on :8001 (10.0.2.2 on Android)
#   SUB_USER/SUB_PASS            customer login for the mobile smoke test
#   DEMO_USERNAME/DEMO_PASSWORD  technician login for the field smoke test
#   IOS_DEVICE_TYPE  simulator model (default: newest available iPhone)
#   AVD_NAME         Android virtual device (default: dotmac_pixel)
#
# Toolchain is looked up in ~/development/flutter, ~/development/ruby (+ gems
# for CocoaPods) and ~/Library/Android/sdk when not already on PATH.
set -euo pipefail

APP="${1:-}"; PLATFORM="${2:-}"; MODE="${3:-test}"
shift $(( $# < 3 ? $# : 3 ))
case "$APP" in mobile|field_mobile) ;; *) sed -n 3,20p "$0"; exit 2 ;; esac
case "$PLATFORM" in ios|android) ;; *) sed -n 3,20p "$0"; exit 2 ;; esac
case "$MODE" in test|run) ;; *) sed -n 3,20p "$0"; exit 2 ;; esac

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ANDROID_SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export PATH="$HOME/development/flutter/bin:$ANDROID_SDK/platform-tools:$ANDROID_SDK/emulator:$PATH"
# CocoaPods for iOS, on a portable Ruby (the macOS system Ruby is too old).
if [ -x "$HOME/development/ruby/bin/ruby" ]; then
  export GEM_HOME="${GEM_HOME:-$HOME/development/gems}"
  export PATH="$GEM_HOME/bin:$HOME/development/ruby/bin:$PATH"
fi
command -v flutter >/dev/null || { echo "flutter not found (expected ~/development/flutter)"; exit 1; }

boot_ios() {
  command -v xcrun >/dev/null && xcrun simctl help >/dev/null 2>&1 \
    || { echo "Xcode is not installed/selected (sudo xcode-select -s /Applications/Xcode.app)"; exit 1; }
  local name="dotmac-iphone" udid type
  udid=$(xcrun simctl list devices available -j | python3 -c '
import json, sys
for devs in json.load(sys.stdin)["devices"].values():
    for d in devs:
        if d["name"] == "dotmac-iphone": print(d["udid"]); sys.exit()')
  if [ -z "$udid" ]; then
    type="${IOS_DEVICE_TYPE:-$(xcrun simctl list devicetypes | grep -E '^iPhone [0-9]+ Pro \(' | tail -1 | sed -E 's/.*\((.*)\)$/\1/')}"
    runtime=$(xcrun simctl list runtimes available | grep '^iOS' | tail -1 | awk '{print $NF}')
    [ -n "$runtime" ] || { echo "No iOS simulator runtime (xcodebuild -downloadPlatform iOS)"; exit 1; }
    udid=$(xcrun simctl create "$name" "$type" "$runtime")
  fi
  xcrun simctl bootstatus "$udid" -b >/dev/null
  open -a Simulator
  DEVICE="$udid"
}

boot_android() {
  local avd="${AVD_NAME:-dotmac_pixel}"
  DEVICE=$(adb devices | awk '/^emulator-[0-9]+\tdevice/{print $1; exit}')
  if [ -z "$DEVICE" ]; then
    emulator -avd "$avd" -no-snapshot-save -no-boot-anim >/dev/null 2>&1 &
    adb wait-for-device
    DEVICE=$(adb devices | awk '/^emulator-[0-9]+/{print $1; exit}')
  fi
  until [ "$(adb -s "$DEVICE" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = 1 ]; do sleep 2; done
}

"boot_$PLATFORM"
echo "Device: $DEVICE"

if [ "${LOCAL_BACKEND:-}" = 1 ] && [ -z "${API_BASE_URL:-}" ]; then
  [ "$PLATFORM" = android ] && API_BASE_URL=http://10.0.2.2:8001 || API_BASE_URL=http://localhost:8001
fi

DEFINES=()
[ -n "${API_BASE_URL:-}" ] && DEFINES+=(--dart-define=API_BASE_URL="$API_BASE_URL")
for var in SUB_USER SUB_PASS DEMO_USERNAME DEMO_PASSWORD; do
  [ -n "${!var:-}" ] && DEFINES+=(--dart-define="$var=${!var}")
done

cd "$ROOT/$APP"
# pub get and iOS builds (pod install / SwiftPM resolution) rewrite the
# committed Xcode project. Put tracked ios/ files back afterwards — but only if
# they were clean to begin with, so real edits are never discarded.
if git diff --quiet -- ios; then
  trap 'git checkout -- ios' EXIT
fi
flutter pub get >/dev/null
if [ "$PLATFORM" = android ] && [ "$MODE" = test ]; then
  # flutter test's own cleanup uninstalls by Gradle namespace, which differs
  # from the applicationId, so the app (and its stored session) would survive
  # into the next run. Start every Android test run from a clean install.
  app_id=$(sed -nE 's/^ *applicationId = "([^"]+)".*/\1/p' android/app/build.gradle.kts)
  [ -n "$app_id" ] && adb -s "$DEVICE" uninstall "$app_id" >/dev/null 2>&1 || true
fi
if [ "$MODE" = test ]; then
  flutter test integration_test/app_smoke_test.dart -d "$DEVICE" ${DEFINES[@]+"${DEFINES[@]}"} "$@"
else
  flutter run -d "$DEVICE" ${DEFINES[@]+"${DEFINES[@]}"} "$@"
fi
