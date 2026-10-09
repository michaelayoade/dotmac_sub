import 'package:dotmac_field/main.dart' as app;
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

/// On-device smoke test for the field app (iOS simulator / Android emulator).
///
/// Builds the production provider graph and checks the login screen renders.
/// When DEMO_USERNAME/DEMO_PASSWORD are supplied it also signs in and waits for
/// the Today tab. Run via `scripts/mobile_sim.sh field_mobile <ios|android>`.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  const username = String.fromEnvironment('DEMO_USERNAME');
  const password = String.fromEnvironment('DEMO_PASSWORD');

  testWidgets('launches to the login screen', (tester) async {
    await tester.pumpWidget(await app.buildFieldAppRoot());
    await _pumpUntil(tester, _fieldByLabel('Email or username'));

    expect(_fieldByLabel('Password'), findsOneWidget);
    expect(find.text('Sign in'), findsOneWidget);

    if (username.isEmpty || password.isEmpty) return;

    await tester.enterText(_fieldByLabel('Email or username'), username);
    await tester.enterText(_fieldByLabel('Password'), password);
    await tester.pump();
    await tester.tap(find.text('Sign in'));

    await _pumpUntil(tester, find.byIcon(Icons.assignment_outlined));
  });
}

/// Matches a Material text field (TextField, or the inner field of a
/// TextFormField) by its InputDecoration label.
Finder _fieldByLabel(String label) => find.byWidgetPredicate(
  (w) => w is TextField && w.decoration?.labelText == label,
);

/// Pump until [finder] matches, ~30s max.
Future<void> _pumpUntil(
  WidgetTester tester,
  Finder finder, {
  int tries = 100,
}) async {
  for (var i = 0; i < tries; i++) {
    await tester.pump(const Duration(milliseconds: 300));
    if (finder.evaluate().isNotEmpty) return;
  }
  throw StateError('Timed out waiting for $finder.');
}
