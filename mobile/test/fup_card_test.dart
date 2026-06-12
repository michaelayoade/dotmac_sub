import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:dotmac_portal/src/features/usage/fup_card.dart';
import 'package:dotmac_portal/src/models/usage.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('FupCard', () {
    testWidgets('throttled: structured until-line and contextual remedy CTAs', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          FupCard(
            serviceId: 'sub-1',
            canBuyData: true,
            fup: FupStatus(
              status: 'throttled',
              isReduced: true,
              speedReductionPercent: 75,
              activeRuleName: 'Monthly 100GB cap',
              resetsAt: DateTime(2026, 7, 1),
              summary: 'Speed reduced to 25% after 100 GB this month',
            ),
          ),
        ),
      );

      expect(find.text('Speed reduced'), findsOneWidget);
      expect(
        find.text('Speed reduced to 25% after 100 GB this month'),
        findsOneWidget,
      );
      expect(find.textContaining('Throttled until'), findsOneWidget);
      expect(find.text('Buy data to restore'), findsOneWidget);
      expect(find.text('Upgrade plan'), findsOneWidget);
      expect(find.byIcon(Icons.speed), findsOneWidget);
    });

    testWidgets('blocked: paused title, block icon, upgrade-only when plan '
        'sells no data bundles', (tester) async {
      await tester.pumpWidget(
        _wrap(
          FupCard(
            serviceId: 'sub-1',
            fup: FupStatus(status: 'blocked', isReduced: true),
          ),
        ),
      );

      expect(find.text('Service paused'), findsOneWidget);
      expect(find.byIcon(Icons.block), findsOneWidget);
      // Falls back to a default explainer when no summary is provided.
      expect(find.textContaining('fair-usage limit'), findsOneWidget);
      // No wallet-cash conflation, and no buy-data on ineligible plans.
      expect(find.textContaining('Top up'), findsNothing);
      expect(find.textContaining('Buy data'), findsNothing);
      expect(find.text('Upgrade plan'), findsOneWidget);
    });

    testWidgets(
      'approaching: pre-warn copy, headroom bar, plain Buy data CTA',
      (tester) async {
        await tester.pumpWidget(
          _wrap(
            FupCard(
              serviceId: 'sub-1',
              canBuyData: true,
              fup: FupStatus(
                status: 'approaching',
                usageRatio: 0.85,
                thresholdGb: 100,
                usedGb: 85,
                gbUntilThrottle: 15,
                summary:
                    '85% of your fair-use allowance used '
                    '— 15 GB until it applies',
              ),
            ),
          ),
        );

        expect(find.text('Approaching your limit'), findsOneWidget);
        expect(find.textContaining('15 GB until it applies'), findsOneWidget);
        expect(find.byType(LinearProgressIndicator), findsOneWidget);
        expect(find.text('Buy data'), findsOneWidget);
        // Not enforced yet — no until-line.
        expect(find.textContaining('Throttled until'), findsNothing);
      },
    );

    testWidgets('omits the until-line when resetsAt is null', (tester) async {
      await tester.pumpWidget(
        _wrap(
          FupCard(
            fup: FupStatus(status: 'throttled', summary: 'Reduced'),
          ),
        ),
      );
      expect(find.textContaining('until'), findsNothing);
      // No serviceId → no CTAs at all.
      expect(find.text('Upgrade plan'), findsNothing);
    });
  });
}
