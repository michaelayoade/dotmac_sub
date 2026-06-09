import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/features/usage/fup_card.dart';
import 'package:dotmac_portal/src/models/usage.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('FupCard', () {
    testWidgets('throttled: shows reduced-speed title, summary and restore CTA',
        (tester) async {
      await tester.pumpWidget(_wrap(FupCard(
        fup: FupStatus(
          status: 'throttled',
          isReduced: true,
          speedReductionPercent: 75,
          activeRuleName: 'Monthly 100GB cap',
          resetsAt: DateTime(2026, 7, 1),
          summary: 'Speed reduced to 25% after 100 GB this month',
        ),
      )));

      expect(find.text('Speed reduced'), findsOneWidget);
      expect(find.text('Speed reduced to 25% after 100 GB this month'),
          findsOneWidget);
      expect(find.text('Top up to restore'), findsOneWidget);
      expect(find.textContaining('Resets'), findsOneWidget);
      expect(find.byIcon(Icons.speed), findsOneWidget);
    });

    testWidgets('blocked: shows paused title and block icon', (tester) async {
      await tester.pumpWidget(_wrap(FupCard(
        fup: FupStatus(status: 'blocked', isReduced: true),
      )));

      expect(find.text('Service paused'), findsOneWidget);
      expect(find.byIcon(Icons.block), findsOneWidget);
      // Falls back to a default explainer when no summary is provided.
      expect(find.textContaining('fair-usage limit'), findsOneWidget);
      expect(find.text('Top up to restore'), findsOneWidget);
    });

    testWidgets('omits the reset line when resetsAt is null', (tester) async {
      await tester.pumpWidget(_wrap(FupCard(
        fup: FupStatus(status: 'throttled', summary: 'Reduced'),
      )));
      expect(find.textContaining('Resets'), findsNothing);
    });
  });
}
