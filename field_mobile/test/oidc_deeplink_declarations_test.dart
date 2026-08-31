import 'dart:io';

import 'package:dotmac_field/core/deeplink/oidc_redirect.dart';
import 'package:flutter_test/flutter_test.dart';

/// Static assertions over the NATIVE declarations.
///
/// The Dart boundary in `oidc_redirect.dart` decides nothing on its own: the
/// operating system decides whether a link reaches this app at all, using the
/// Android intent filter, the iOS Associated Domains entitlement, and the two
/// association documents served from the callback origin. Those four are
/// invisible to the Dart type system and to `flutter analyze`, and when they
/// drift the failure is SILENT -- the link quietly starts opening a browser.
///
/// So they are asserted here, from the outside, against the same constants the
/// app compiles in. `scripts/check_field_applinks.py` runs the wider
/// cross-artifact version of this in CI (it also reaches the association
/// documents under `deploy/links.dotmac.io/`, which live outside this package).
/// Reads a declaration file with its XML comments removed.
///
/// Each of these files carries a comment explaining why a particular shape is
/// forbidden -- and such a comment necessarily NAMES the forbidden shape. A
/// scan over the raw text reads its own explanation as the defect it
/// describes, so `isNot(contains('CFBundleURLTypes'))` would fail on the very
/// comment saying there must not be one. Strip first, then assert.
String _declarations(String path) => File(
  path,
).readAsStringSync().replaceAll(RegExp(r'<!--.*?-->', dotAll: true), '');

void main() {
  final manifest = _declarations('android/app/src/main/AndroidManifest.xml');
  final entitlements = _declarations('ios/Runner/Runner.entitlements');
  final infoPlist = _declarations('ios/Runner/Info.plist');
  final pbxproj = File(
    'ios/Runner.xcodeproj/project.pbxproj',
  ).readAsStringSync();

  group('Android App Links declaration', () {
    test('declares an autoVerify HTTPS intent filter', () {
      expect(manifest, contains('<intent-filter android:autoVerify="true">'));
      expect(manifest, contains('android:name="android.intent.action.VIEW"'));
      expect(
        manifest,
        contains('android:name="android.intent.category.BROWSABLE"'),
      );
      expect(
        manifest,
        contains('android:name="android.intent.category.DEFAULT"'),
      );
      expect(manifest, contains('android:scheme="https"'));
    });

    test('pins the exact host and the exact path the app compiles in', () {
      expect(
        manifest,
        contains(
          'android:host="${kOidcCallbackOriginDefault.replaceFirst('https://', '')}"',
        ),
      );
      expect(manifest, contains('android:path="$kOidcCallbackPathDefault"'));
    });

    test('uses no widening path matcher', () {
      // pathPrefix/pathPattern would make every path under the callback a legal
      // place to deliver an authorization code.
      expect(manifest, isNot(contains('android:pathPrefix')));
      expect(manifest, isNot(contains('android:pathPattern')));
      expect(manifest, isNot(contains('android:pathAdvancedPattern')));
      expect(manifest, isNot(contains('android:pathSuffix')));
    });

    test('declares no custom scheme', () {
      final schemes = RegExp(
        r'android:scheme="([^"]+)"',
      ).allMatches(manifest).map((m) => m.group(1)).toSet();
      expect(schemes, {'https'});
    });

    test('declares no wildcard host', () {
      final hosts = RegExp(
        r'android:host="([^"]+)"',
      ).allMatches(manifest).map((m) => m.group(1)!).toList();
      expect(hosts, isNotEmpty);
      for (final host in hosts) {
        expect(host, isNot(contains('*')), reason: host);
      }
    });

    test('lets the verified link reach the Dart router', () {
      expect(manifest, contains('android:name="flutter_deeplinking_enabled"'));
      expect(
        RegExp(
          r'flutter_deeplinking_enabled"\s*\n?\s*android:value="true"',
        ).hasMatch(manifest),
        isTrue,
        reason: 'flutter_deeplinking_enabled must be "true"',
      );
    });
  });

  group('iOS Universal Links declaration', () {
    test('declares the associated domain for the callback host', () {
      expect(
        entitlements,
        contains('<key>com.apple.developer.associated-domains</key>'),
      );
      final host = kOidcCallbackOriginDefault.replaceFirst('https://', '');
      expect(entitlements, contains('<string>applinks:$host</string>'));
    });

    test('declares exactly one associated domain, with no wildcard', () {
      final domains = RegExp(
        r'<string>(applinks:[^<]*)</string>',
      ).allMatches(entitlements).map((m) => m.group(1)!).toList();
      expect(domains, hasLength(1));
      expect(domains.single, isNot(contains('*')));
    });

    test('the comment stripper is load-bearing', () {
      // A check over already-clean text passes for the wrong reason. These
      // files DO name the forbidden shapes in their comments, so the stripper
      // must actually be removing something -- otherwise the assertion below
      // is running against the raw text and would fail.
      final raw = File('ios/Runner/Info.plist').readAsStringSync();
      expect(raw, contains('CFBundleURLTypes'));
      expect(infoPlist, isNot(contains('CFBundleURLTypes')));
    });

    test('declares no custom URL scheme', () {
      // A CFBundleURLTypes entry is claimable by any app on the device and is
      // exactly what this wave replaces. There must not be one, not even as a
      // fallback.
      expect(infoPlist, isNot(contains('CFBundleURLTypes')));
      expect(infoPlist, isNot(contains('CFBundleURLSchemes')));
    });

    test('lets the Universal Link reach the Dart router', () {
      expect(
        RegExp(
          r'<key>FlutterDeepLinkingEnabled</key>\s*<true/>',
        ).hasMatch(infoPlist),
        isTrue,
      );
    });

    test('the Runner target actually signs with the entitlements file', () {
      // An entitlements file the target does not sign with is inert, and the
      // Universal Link then fails with no error anywhere. Before this wave the
      // setting was applied only by the CI-only Firebase wiring script, so a
      // build without GoogleService-Info.plist carried no entitlements at all.
      final configured = RegExp(
        r'CODE_SIGN_ENTITLEMENTS = ([^;]+);',
      ).allMatches(pbxproj).map((m) => m.group(1)!.trim()).toList();
      expect(configured, hasLength(3));
      expect(configured.toSet(), {'Runner/Runner.entitlements'});
    });
  });
}
