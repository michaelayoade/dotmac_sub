import 'package:dotmac_portal/main.dart' as app;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

/// On-device smoke test for the self-care app (iOS simulator / Android emulator).
///
/// Boots the real `main()` and checks the login screen renders. When
/// SUB_USER/SUB_PASS are supplied it also signs in and waits for the
/// authenticated shell. Run via `scripts/mobile_sim.sh mobile <ios|android>`:
///   --dart-define=API_BASE_URL=... --dart-define=SUB_USER=... --dart-define=SUB_PASS=...
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  const username = String.fromEnvironment('SUB_USER');
  const password = String.fromEnvironment('SUB_PASS');

  testWidgets('launches to the login screen', (tester) async {
    await app.main();
    await _pumpUntil(tester, _fieldByLabel('Email or username'));

    expect(_fieldByLabel('Password'), findsOneWidget);
    expect(find.widgetWithText(FilledButton, 'Sign in'), findsOneWidget);

    if (username.isEmpty || password.isEmpty) return;

    await tester.enterText(_fieldByLabel('Email or username'), username);
    await tester.enterText(_fieldByLabel('Password'), password);
    await tester.pump();
    await tester.tap(find.widgetWithText(FilledButton, 'Sign in'));

    // Signed in once the login form is gone.
    await _pumpUntil(tester, _fieldByLabel('Email or username'), gone: true);
  });
}

/// Matches a Material text field (TextField, or the inner field of a
/// TextFormField) by its InputDecoration label.
Finder _fieldByLabel(String label) => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.labelText == label,
    );

/// Pump until [finder] matches (or stops matching when [gone]), ~30s max.
Future<void> _pumpUntil(
  WidgetTester tester,
  Finder finder, {
  bool gone = false,
  int tries = 100,
}) async {
  for (var i = 0; i < tries; i++) {
    await tester.pump(const Duration(milliseconds: 300));
    if (finder.evaluate().isNotEmpty != gone) return;
  }
  throw StateError('Timed out waiting for $finder (gone: $gone).');
}
